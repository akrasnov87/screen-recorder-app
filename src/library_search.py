"""Поиск по стенограммам и протоколам записей без использования БД.

Изменения:
  • Удалены неиспользуемые SearchHit.token_count и _SUMMARY_KEYS.
  • Добавлено поле SearchFilters.fuzzy_max_word_distance.
  • Добавлены поля SearchHit.tags и SearchFilters.tag /
    tag_includes_untagged.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from typing import (
    Any, Callable, Dict, Iterable, List, Optional, Tuple,
)

from .file_readers import read_any_text, read_json_file
from .logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Модель результата
# ---------------------------------------------------------------------------
@dataclass
class SearchHit:
    """Одно совпадение."""
    session_dir: str
    session_name: str
    project: str
    date: str
    source: str            # "transcript" | "protocol" | "summary" | "attachment"
    file_path: str
    file_label: str
    snippet: str
    score: float
    match_kind: str        # "exact" | "fuzzy"
    tags: Tuple[str, ...] = ()


@dataclass
class SearchFilters:
    """Фильтры поиска."""
    query: str = ""
    search_transcripts: bool = True
    search_protocols: bool = True
    search_summaries: bool = True
    search_attachments: bool = False
    project: str = ""
    date_from: Optional[datetime] = None
    date_to: Optional[datetime] = None
    fuzzy_threshold: float = 0.82
    max_results: int = 500
    # Контекст вокруг совпадения для сниппета в результатах
    # поиска. Не путать с before_chars/after_chars в
    # build_prompt_from_hits — это разные вещи.
    snippet_context_chars: int = 120
    fuzzy_max_word_distance: int = 200
    # --- Новый фильтр по тегу ---
    # Если пусто — фильтр не применяется.
    # Если задан — оставляем только записи, у которых есть
    # указанный тег.
    tag: str = ""
    # Если True и tag == "" — оставляем только записи БЕЗ тегов.
    tag_includes_untagged: bool = False


# ---------------------------------------------------------------------------
# Сканирование сессий
# ---------------------------------------------------------------------------
_PROTOCOL_PATTERNS = (
    "manual_protocol.docx", "manual_protocol.txt",
    "manual_protocol.md", "manual_protocol.pdf",
    "protocol.docx", "protocol.txt", "protocol.md",
    "deepseek_prompt.docx", "deepseek_prompt.txt",
    "deepseek_prompt.md",
)

_TRANSCRIPT_NAMES = ("video.txt",)


def _iter_session_dirs(sessions_root: str) -> Iterable[str]:
    if not os.path.isdir(sessions_root):
        return []
    result = []
    for name in sorted(os.listdir(sessions_root)):
        d = os.path.join(sessions_root, name)
        if os.path.isdir(d):
            result.append(d)
    return result


def _load_session_meta(session_dir: str) -> Dict[str, Any]:
    """Читает session.json через file_readers."""
    path = os.path.join(session_dir, "session.json")
    data = read_json_file(path)
    return data or {}


def _extract_tags_from_meta(meta: Dict[str, Any]) -> Tuple[str, ...]:
    """Достаёт список тегов из session.json. Всегда возвращает кортеж."""
    raw = meta.get("tags")
    if not raw:
        return ()
    result: List[str] = []
    seen = set()
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                name = item.strip()
            elif isinstance(item, dict):
                name = str(item.get("name") or "").strip()
            else:
                continue
            if name and name not in seen:
                result.append(name)
                seen.add(name)
    return tuple(result)


def _parse_session_date(
    session_dir: str, meta: Dict[str, Any],
) -> Optional[datetime]:
    """Пытается извлечь дату сессии из meta или имени папки."""
    date_str = meta.get("date") or ""
    name = os.path.basename(session_dir)
    if not date_str:
        m = re.match(r"(\d{4}-\d{2}-\d{2})", name)
        if m:
            date_str = m.group(1)
    if not date_str:
        try:
            ts = os.path.getmtime(session_dir)
            return datetime.fromtimestamp(ts)
        except Exception:
            return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(date_str)
    except Exception:
        return None


def list_projects(sessions_root: str) -> List[str]:
    """Возвращает уникальные проекты из всех сессий (для фильтра)."""
    projects = set()
    for d in _iter_session_dirs(sessions_root):
        meta = _load_session_meta(d)
        p = (meta.get("project") or "").strip()
        if p:
            projects.add(p)
    return sorted(projects)


def list_tags(sessions_root: str) -> List[str]:
    """
    Возвращает уникальные теги из всех сессий.

    Дополнительно можно объединять со справочником тегов —
    это делает вызывающий код.
    """
    tags = set()
    for d in _iter_session_dirs(sessions_root):
        meta = _load_session_meta(d)
        for t in _extract_tags_from_meta(meta):
            tags.add(t)
    return sorted(tags)


# ---------------------------------------------------------------------------
# Нормализация и токенизация
# ---------------------------------------------------------------------------
_TOKEN_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9]+")


def _normalize_word(w: str) -> str:
    """Lowercase + схлопывание повторяющихся букв."""
    w = w.lower().strip()
    w = re.sub(r"(.)\1{2,}", r"\1", w)
    return w


def _tokenize(text: str) -> List[str]:
    return [_normalize_word(t) for t in _TOKEN_RE.findall(text)]


# ---------------------------------------------------------------------------
# Поиск
# ---------------------------------------------------------------------------
def _make_snippet(text: str, start: int, end: int, ctx: int) -> str:
    left = max(0, start - ctx)
    right = min(len(text), end + ctx)
    prefix = "…" if left > 0 else ""
    suffix = "…" if right < len(text) else ""
    frag = text[left:right].replace("\n", " ")
    return f"{prefix}{frag}{suffix}"


def _exact_search(
    text: str,
    query: str,
    case_sensitive: bool = False,
    context_chars: int = 120,
) -> List[Tuple[int, int, str]]:
    """Возвращает список (start, end, snippet) для точных совпадений."""
    if not query:
        return []
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        pattern = re.compile(re.escape(query), flags)
    except re.error:
        return []
    hits = []
    for m in pattern.finditer(text):
        hits.append((
            m.start(), m.end(),
            _make_snippet(
                text, m.start(), m.end(), context_chars
            ),
        ))
    return hits


def _fuzzy_search_words(
    text: str,
    query: str,
    threshold: float,
    context_chars: int,
    max_word_distance: int = 200,
) -> List[Tuple[float, int, int, str, int]]:
    """
    Fuzzy-поиск по отдельным словам запроса.
    """
    q_words = [w for w in _tokenize(query) if len(w) >= 3]
    if not q_words:
        return []

    word_positions: List[Tuple[str, int, int]] = []
    for m in _TOKEN_RE.finditer(text):
        w = _normalize_word(m.group())
        if w:
            word_positions.append((w, m.start(), m.end()))

    matched_per_word: List[List[Tuple[float, int, int]]] = [
        [] for _ in q_words
    ]

    for wi, qw in enumerate(q_words):
        for w, s, e in word_positions:
            r = SequenceMatcher(None, qw, w).ratio()
            if r >= threshold:
                matched_per_word[wi].append((r, s, e))

    all_matches: List[Tuple[float, int, int, int]] = []
    for wi, matches in enumerate(matched_per_word):
        for r, s, e in matches:
            all_matches.append((r, s, e, wi))

    if not all_matches:
        return []

    all_matches.sort(key=lambda x: x[1])

    grouped: List[List[Tuple[float, int, int, int]]] = []
    cur: List[Tuple[float, int, int, int]] = []
    for m in all_matches:
        if not cur or m[1] - cur[-1][1] < max_word_distance:
            cur.append(m)
        else:
            grouped.append(cur)
            cur = [m]
    if cur:
        grouped.append(cur)

    results: List[Tuple[float, int, int, str, int]] = []
    total_q = len(q_words)
    for group in grouped:
        words_found = {m[3] for m in group}
        if len(words_found) < max(1, total_q // 2):
            continue
        avg = sum(m[0] for m in group) / len(group)
        coverage = len(words_found) / total_q
        score = avg * coverage
        s = group[0][1]
        e = group[-1][2]
        snippet = _make_snippet(text, s, e, context_chars)
        results.append((score, s, e, snippet, len(words_found)))

    return results


def _search_in_text(
    text: str,
    query: str,
    fuzzy_threshold: float,
    context_chars: int,
    max_word_distance: int = 200,
) -> List[Tuple[float, str, str]]:
    """
    Ищет в тексте. Сначала — точные совпадения, потом fuzzy.
    """
    results: List[Tuple[float, str, str]] = []

    exact = _exact_search(
        text, query, context_chars=context_chars
    )
    for _s, _e, snippet in exact:
        results.append((1.0, snippet, "exact"))

    if not exact:
        fuzzy = _fuzzy_search_words(
            text, query, fuzzy_threshold, context_chars,
            max_word_distance,
        )
        seen = set()
        for score, _s, _e, snippet, _wc in fuzzy:
            key = snippet[:80]
            if key in seen:
                continue
            seen.add(key)
            results.append((score, snippet, "fuzzy"))

    results.sort(key=lambda x: x[0], reverse=True)
    return results


# ---------------------------------------------------------------------------
# Основная функция поиска
# ---------------------------------------------------------------------------
def search(
    sessions_root: str,
    filters: SearchFilters,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    is_cancelled: Optional[Callable[[], bool]] = None,
) -> List[SearchHit]:
    """Ищет по всем сессиям в sessions_root согласно фильтрам."""
    query = (filters.query or "").strip()
    if not query:
        return []

    sessions = list(_iter_session_dirs(sessions_root))
    total = len(sessions)
    hits: List[SearchHit] = []

    # --- Нормализация фильтра по тегу ---
    filter_tag = (filters.tag or "").strip()
    only_untagged = bool(
        not filter_tag and filters.tag_includes_untagged
    )

    for i, session_dir in enumerate(sessions):
        if is_cancelled is not None:
            try:
                if is_cancelled():
                    log.info(
                        "Поиск прерван пользователем: обработано "
                        "%d/%d сессий, найдено %d hits",
                        i, total, len(hits),
                    )
                    break
            except Exception as exc:
                log.warning("Ошибка в is_cancelled: %s", exc)

        if progress_cb is not None:
            try:
                progress_cb(i, total)
            except Exception:
                pass

        if len(hits) >= filters.max_results:
            break

        meta = _load_session_meta(session_dir)

        # --- Фильтр по проекту ---
        if filters.project:
            if (meta.get("project") or "") != filters.project:
                continue

        # --- Фильтр по тегу ---
        session_tags = _extract_tags_from_meta(meta)
        if filter_tag:
            if filter_tag not in session_tags:
                continue
        elif only_untagged:
            if session_tags:
                continue

        # --- Фильтр по дате ---
        sdate = _parse_session_date(session_dir, meta)
        if (filters.date_from and sdate
                and sdate.date() < filters.date_from.date()):
            continue
        if (filters.date_to and sdate
                and sdate.date() > filters.date_to.date()):
            continue

        session_name = (
            meta.get("name") or os.path.basename(session_dir)
        )
        project = meta.get("project") or ""
        date_str = sdate.strftime("%Y-%m-%d") if sdate else ""
        mwd = filters.fuzzy_max_word_distance

        if filters.search_transcripts:
            for fname in _TRANSCRIPT_NAMES:
                fp = os.path.join(session_dir, fname)
                if not os.path.exists(fp):
                    continue
                text = read_any_text(fp)
                if not text:
                    continue
                for score, snippet, kind in _search_in_text(
                    text, query, filters.fuzzy_threshold,
                    filters.snippet_context_chars, mwd,
                ):
                    hits.append(SearchHit(
                        session_dir=session_dir,
                        session_name=session_name,
                        project=project,
                        date=date_str,
                        source="transcript",
                        file_path=fp,
                        file_label=fname,
                        snippet=snippet,
                        score=score,
                        match_kind=kind,
                        tags=session_tags,
                    ))
                    if len(hits) >= filters.max_results:
                        break

        if filters.search_protocols:
            for fname in _PROTOCOL_PATTERNS:
                fp = os.path.join(session_dir, fname)
                if not os.path.exists(fp):
                    continue
                text = read_any_text(fp)
                if not text:
                    continue
                for score, snippet, kind in _search_in_text(
                    text, query, filters.fuzzy_threshold,
                    filters.snippet_context_chars, mwd,
                ):
                    hits.append(SearchHit(
                        session_dir=session_dir,
                        session_name=session_name,
                        project=project,
                        date=date_str,
                        source="protocol",
                        file_path=fp,
                        file_label=fname,
                        snippet=snippet,
                        score=score,
                        match_kind=kind,
                        tags=session_tags,
                    ))
                    if len(hits) >= filters.max_results:
                        break

        if filters.search_summaries:
            summary_bb = str(
                meta.get("summary_bb") or ""
            ).strip()
            if summary_bb:
                from .markdown_to_bitrix import (
                    markdown_to_plain_with_bb,
                )
                plain = markdown_to_plain_with_bb(summary_bb)
                for score, snippet, kind in _search_in_text(
                    plain, query, filters.fuzzy_threshold,
                    filters.snippet_context_chars, mwd,
                ):
                    hits.append(SearchHit(
                        session_dir=session_dir,
                        session_name=session_name,
                        project=project,
                        date=date_str,
                        source="summary",
                        file_path=os.path.join(
                            session_dir, "session.json"
                        ),
                        file_label="summary (session.json)",
                        snippet=snippet,
                        score=score,
                        match_kind=kind,
                        tags=session_tags,
                    ))
                    if len(hits) >= filters.max_results:
                        break

        if filters.search_attachments:
            att_dir = os.path.join(
                session_dir, "attachments"
            )
            if os.path.isdir(att_dir):
                for fname in sorted(os.listdir(att_dir)):
                    fp = os.path.join(att_dir, fname)
                    if not os.path.isfile(fp):
                        continue
                    text = read_any_text(fp)
                    if not text:
                        continue
                    for score, snippet, kind in _search_in_text(
                        text, query, filters.fuzzy_threshold,
                        filters.snippet_context_chars, mwd,
                    ):
                        hits.append(SearchHit(
                            session_dir=session_dir,
                            session_name=session_name,
                            project=project,
                            date=date_str,
                            source="attachment",
                            file_path=fp,
                            file_label=f"attachments/{fname}",
                            snippet=snippet,
                            score=score,
                            match_kind=kind,
                            tags=session_tags,
                        ))
                        if len(hits) >= filters.max_results:
                            break

    if progress_cb is not None:
        try:
            progress_cb(total, total)
        except Exception:
            pass

    hits.sort(key=lambda h: (h.date, h.score), reverse=True)
    return hits


# ---------------------------------------------------------------------------
# Формирование промпта из найденных совпадений
# ---------------------------------------------------------------------------
@dataclass
class PromptContext:
    """Расширенный контекст одного совпадения для промпта."""
    hit: SearchHit
    before_text: str
    after_text: str
    full_context: str
    match_start_in_full: int
    match_end_in_full: int
    truncated_before: bool
    truncated_after: bool
    dedup_key: str = ""


def _find_match_position(
    text: str, query: str, case_sensitive: bool = False,
) -> Tuple[int, int]:
    """Возвращает (start, end) первого точного вхождения query."""
    if not query:
        return -1, -1
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        m = re.search(re.escape(query), text, flags)
    except re.error:
        return -1, -1
    if not m:
        return -1, -1
    return m.start(), m.end()


def extract_hit_context(
    hit: SearchHit,
    query: str,
    before_chars: int = 400,
    after_chars: int = 400,
) -> Optional[PromptContext]:
    """Извлекает расширенный контекст вокруг совпадения."""
    if not hit.file_path or not os.path.exists(hit.file_path):
        log.warning(
            "Файл совпадения не найден: %s", hit.file_path
        )
        return None

    if (hit.source == "summary"
            and hit.file_path.endswith("session.json")):
        meta = _load_session_meta(hit.session_dir)
        raw = str(meta.get("summary_bb") or "")
        if not raw:
            return None
        from .markdown_to_bitrix import markdown_to_plain_with_bb
        text = markdown_to_plain_with_bb(raw)
    else:
        text = read_any_text(hit.file_path)

    if not text:
        return None

    start, end = _find_match_position(text, query)
    if start < 0:
        tokens = [t for t in _tokenize(query) if len(t) >= 3]
        for tok in tokens:
            start, end = _find_match_position(text, tok)
            if start >= 0:
                break
    if start < 0:
        start, end = 0, min(len(text), 200)

    left = max(0, start - before_chars)
    right = min(len(text), end + after_chars)

    before_text = text[left:start]
    after_text = text[end:right]
    full = text[left:right]

    match_start_in_full = start - left
    match_end_in_full = end - left

    norm_before = re.sub(
        r"\s+", " ", before_text[-80:]
    ).strip().lower()
    norm_after = re.sub(
        r"\s+", " ", after_text[:80]
    ).strip().lower()
    dedup_key = f"{hit.file_path}|{norm_before}|{norm_after}"

    return PromptContext(
        hit=hit,
        before_text=before_text,
        after_text=after_text,
        full_context=full,
        match_start_in_full=match_start_in_full,
        match_end_in_full=match_end_in_full,
        truncated_before=(left > 0),
        truncated_after=(right < len(text)),
        dedup_key=dedup_key,
    )


def _deduplicate_contexts(
    contexts: List[PromptContext],
    overlap_ratio: float = 0.7,
) -> List[PromptContext]:
    """Схлопывает контексты, которые сильно пересекаются."""
    by_file: Dict[str, List[PromptContext]] = {}
    for c in contexts:
        by_file.setdefault(c.hit.file_path, []).append(c)

    result: List[PromptContext] = []
    for fpath, ctxs in by_file.items():
        ctxs_sorted = sorted(
            ctxs, key=lambda x: x.hit.score, reverse=True
        )
        kept: List[PromptContext] = []
        for c in ctxs_sorted:
            is_dup = False
            for k in kept:
                a, b = c.full_context, k.full_context
                if not a or not b:
                    continue
                sample_a = a[:500]
                sample_b = b[:500]
                r = SequenceMatcher(
                    None, sample_a, sample_b
                ).ratio()
                if r >= overlap_ratio:
                    is_dup = True
                    break
            if not is_dup:
                kept.append(c)
        result.extend(kept)

    order = {id(c): i for i, c in enumerate(contexts)}
    result.sort(key=lambda c: order.get(id(c), 0))
    return result


# ---------------------------------------------------------------------------
# Сборка промпта
# ---------------------------------------------------------------------------
_SOURCE_RU = {
    "transcript": "стенограмма",
    "protocol": "протокол",
    "summary": "краткое описание",
    "attachment": "вложение",
}


def build_prompt_from_hits(
    hits: List[SearchHit],
    query: str,
    user_instruction: str = "",
    before_chars: int = 400,
    after_chars: int = 400,
    max_hits: int = 100,
    deduplicate: bool = True,
    include_meta: bool = True,
) -> str:
    """Собирает структурированный текст промпта из совпадений."""
    if not hits:
        return ""

    log.info(
        "Сборка промпта: hits=%d, query=%r, before=%d, after=%d, "
        "max=%d, dedup=%s, meta=%s",
        len(hits), query, before_chars, after_chars,
        max_hits, deduplicate, include_meta,
    )

    contexts: List[PromptContext] = []
    for h in hits[:max_hits]:
        ctx = extract_hit_context(
            h, query=query,
            before_chars=before_chars,
            after_chars=after_chars,
        )
        if ctx is not None:
            contexts.append(ctx)

    if not contexts:
        log.warning(
            "Не удалось извлечь ни одного контекста из hits"
        )
        return ""

    if deduplicate:
        before = len(contexts)
        contexts = _deduplicate_contexts(contexts)
        log.info(
            "Дедупликация: %d → %d контекстов",
            before, len(contexts),
        )

    grouped: Dict[Tuple[str, str], List[PromptContext]] = {}
    for c in contexts:
        key = (c.hit.session_name, c.hit.file_path)
        grouped.setdefault(key, []).append(c)

    parts: List[str] = []

    if include_meta:
        parts.append("# Промпт по результатам поиска")
        parts.append("")
        parts.append(f"**Поисковый запрос:** `{query}`")
        parts.append("")
        parts.append(
            f"**Найдено совпадений:** {len(contexts)}"
        )
        parts.append(
            f"**Уникальных источников:** {len(grouped)}"
        )
        parts.append(
            f"**Контекст:** {before_chars} символов до, "
            f"{after_chars} символов после"
        )
        parts.append("")

        parts.append("## Источники")
        parts.append("")
        for i, ((sname, fpath), ctxs) in enumerate(
            grouped.items(), start=1
        ):
            src_label = _SOURCE_RU.get(
                ctxs[0].hit.source, ctxs[0].hit.source
            )
            date = ctxs[0].hit.date or "—"
            project = ctxs[0].hit.project or "—"
            file_label = ctxs[0].hit.file_label
            tags = ", ".join(ctxs[0].hit.tags) if ctxs[0].hit.tags else ""
            tags_part = f", теги: {tags}" if tags else ""
            parts.append(
                f"{i}. **{sname}** — {date}, проект: {project} "
                f"({src_label}, файл: `{file_label}`{tags_part}, "
                f"совпадений: {len(ctxs)})"
            )
        parts.append("")

    if user_instruction and user_instruction.strip():
        parts.append("## Инструкция для ИИ")
        parts.append("")
        parts.append(user_instruction.strip())
        parts.append("")

    parts.append("## Найденные фрагменты")
    parts.append("")

    for i, ((sname, fpath), ctxs) in enumerate(
        grouped.items(), start=1
    ):
        src_label = _SOURCE_RU.get(
            ctxs[0].hit.source, ctxs[0].hit.source
        )
        date = ctxs[0].hit.date or "—"
        project = ctxs[0].hit.project or "—"
        file_label = ctxs[0].hit.file_label

        parts.append(f"### Источник {i}: {sname}")
        parts.append("")
        parts.append(
            f"*Дата:* {date} · *Проект:* {project} · "
            f"*Тип:* {src_label} · *Файл:* `{file_label}`"
        )
        if ctxs[0].hit.tags:
            parts.append(
                f"*Теги:* {', '.join(ctxs[0].hit.tags)}"
            )
        parts.append("")

        for j, c in enumerate(ctxs, start=1):
            kind_label = (
                "точное совпадение"
                if c.hit.match_kind == "exact"
                else f"нечёткое совпадение (fuzzy, "
                     f"{int(c.hit.score * 100)}%)"
            )
            parts.append(f"**Фрагмент {j}** — {kind_label}")
            parts.append("")

            prefix_marker = "…\n" if c.truncated_before else ""
            suffix_marker = "\n…" if c.truncated_after else ""

            fragment = c.full_context.strip()
            fragment = re.sub(r"\n{3,}", "\n\n", fragment)

            parts.append("```")
            if prefix_marker:
                parts.append(prefix_marker.rstrip("\n"))
            parts.append(fragment)
            if suffix_marker:
                parts.append(suffix_marker.lstrip("\n"))
            parts.append("```")
            parts.append("")

    parts.append("---")
    parts.append("")
    parts.append(
        "*Промпт сформирован автоматически из результатов поиска. "
        f"Всего фрагментов: {len(contexts)}.*"
    )

    result = "\n".join(parts)
    log.info(
        "Промпт собран: %d символов, %d строк",
        len(result), result.count("\n") + 1,
    )
    return result


def save_prompt_docx(
    prompt_text: str,
    output_path: str,
    title: str = "Промпт по результатам поиска",
) -> str:
    """Сохраняет промпт в .docx."""
    from .markdown_docx import markdown_to_docx

    try:
        markdown_to_docx(prompt_text, output_path, title=title)
    except Exception as exc:
        log.exception(
            "Не удалось сохранить промпт в .docx: %s", exc
        )
        raise

    log.info("Промпт сохранён в DOCX: %s", output_path)
    return output_path


def save_prompt_markdown(
    prompt_text: str, output_path: str,
) -> str:
    """Сохраняет промпт в .md."""
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(prompt_text)
    except Exception as exc:
        log.exception(
            "Не удалось сохранить промпт в .md: %s", exc
        )
        raise
    log.info("Промпт сохранён в Markdown: %s", output_path)
    return output_path