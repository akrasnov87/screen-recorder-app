"""Поиск по стенограммам и протоколам записей без использования БД.

Особенности (актуальная версия):
  • Поиск по стенограммам (video.txt), протоколам, summary и вложениям.
  • Для summary используется markdown_to_plain_with_bb — функция
    корректно обрабатывает и Markdown (новые записи), и BB-код
    (старые записи, где summary_bb хранился как BB).
  • Нечёткий поиск (fuzzy) для стенограмм, устойчивый к опечаткам.
  • Фильтры по проекту, датам и области поиска.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, List, Optional, Tuple

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
    snippet: str           # фрагмент с подсветкой контекста
    score: float           # 1.0 для точного, <1.0 для fuzzy
    match_kind: str        # "exact" | "fuzzy"
    token_count: int = 0   # сколько слов запроса нашли


@dataclass
class SearchFilters:
    """Фильтры поиска."""
    query: str = ""
    search_transcripts: bool = True
    search_protocols: bool = True
    search_summaries: bool = True
    search_attachments: bool = False
    project: str = ""                      # "" = все проекты
    date_from: Optional[datetime] = None
    date_to: Optional[datetime] = None
    fuzzy_threshold: float = 0.82          # 0..1, для fuzzy
    max_results: int = 500
    context_chars: int = 120               # символов контекста вокруг совпадения


# ---------------------------------------------------------------------------
# Сканирование сессий
# ---------------------------------------------------------------------------
_PROTOCOL_PATTERNS = (
    "manual_protocol.docx", "manual_protocol.txt", "manual_protocol.md",
    "manual_protocol.pdf",
    "protocol.docx", "protocol.txt", "protocol.md",
    "deepseek_prompt.docx", "deepseek_prompt.txt", "deepseek_prompt.md",
)

_TRANSCRIPT_NAMES = ("video.txt",)
_SUMMARY_KEYS = ("summary_bb",)  # Markdown или BB-код в session.json


def _read_text_safe(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".docx":
        try:
            from docx import Document  # type: ignore
            doc = Document(path)
            return "\n".join(p.text for p in doc.paragraphs)
        except Exception as exc:
            log.warning("Не удалось прочитать .docx %s: %s", path, exc)
            return ""
    if ext == ".pdf":
        try:
            from pypdf import PdfReader  # type: ignore
            reader = PdfReader(path)
            return "\n".join((pg.extract_text() or "") for pg in reader.pages)
        except Exception as exc:
            log.warning("Не удалось прочитать .pdf %s: %s", path, exc)
            return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception as exc:
        log.warning("Не удалось прочитать %s: %s", path, exc)
        return ""


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
    path = os.path.join(session_dir, "session.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _parse_session_date(session_dir: str, meta: Dict[str, Any]) -> Optional[datetime]:
    """Пытается извлечь дату сессии из meta или имени папки."""
    date_str = meta.get("date") or ""
    name = os.path.basename(session_dir)
    if not date_str:
        # имя папки: 2026-09-25_14-30-00
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


# ---------------------------------------------------------------------------
# Нормализация и токенизация
# ---------------------------------------------------------------------------
_TOKEN_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9]+")
_WORD_SEP_RE = re.compile(r"\s+")


def _normalize_word(w: str) -> str:
    """Lowercase, убираем пунктуацию. Для fuzzy-сравнения — ещё и
    схлопываем повторяющиеся буквы (опечатки типа 'оооочень')."""
    w = w.lower().strip()
    w = _TOKEN_RE.sub("", w) if not _TOKEN_RE.match(w) else w
    # схлопываем 3+ одинаковые буквы в 1 (устойчивость к растяжкам)
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
        hits.append((m.start(), m.end(),
                     _make_snippet(text, m.start(), m.end(), context_chars)))
    return hits


def _fuzzy_search_words(
    text: str,
    query: str,
    threshold: float,
    context_chars: int,
) -> List[Tuple[float, int, int, str, int]]:
    """
    Fuzzy-поиск по отдельным словам запроса.

    Возвращает список:
        (score, start, end, snippet, matched_words_count)

    Алгоритм: разбиваем текст на слова с сохранением позиций, каждое
    слово нормализуем. Для каждого слова запроса ищем лучший матч среди
    слов текста через SequenceMatcher. Если схожесть >= threshold —
    считаем слово найденным.
    """
    q_words = [w for w in _tokenize(query) if len(w) >= 3]
    if not q_words:
        return []

    # word -> list[(start, end)]
    word_positions: List[Tuple[str, int, int]] = []
    for m in _TOKEN_RE.finditer(text):
        w = _normalize_word(m.group())
        if w:
            word_positions.append((w, m.start(), m.end()))

    # Для каждого слова запроса ищем лучшие совпадения
    # score_per_word[word_idx] = (best_score, best_start, best_end)
    matched_per_word: List[List[Tuple[float, int, int]]] = [[] for _ in q_words]

    for wi, qw in enumerate(q_words):
        for w, s, e in word_positions:
            r = SequenceMatcher(None, qw, w).ratio()
            if r >= threshold:
                matched_per_word[wi].append((r, s, e))

    # Объединяем по близким позициям: если слова запроса встречаются рядом,
    # это более сильное совпадение.
    # Упрощённо: собираем все совпадения, группируем по «окну» 200 символов,
    # для каждой группы считаем score = среднее * (matched_words / total_words)
    all_matches: List[Tuple[float, int, int, int]] = []  # score, s, e, word_idx
    for wi, matches in enumerate(matched_per_word):
        for r, s, e in matches:
            all_matches.append((r, s, e, wi))

    if not all_matches:
        return []

    all_matches.sort(key=lambda x: x[1])  # по позиции

    grouped: List[List[Tuple[float, int, int, int]]] = []
    cur: List[Tuple[float, int, int, int]] = []
    for m in all_matches:
        if not cur or m[1] - cur[-1][1] < 200:
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
            # должно совпасть хотя бы половина слов запроса
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
) -> List[Tuple[float, str, str]]:
    """
    Ищет в тексте. Сначала — точные совпадения. Если их нет или мало —
    добавляет fuzzy-совпадения.
    Возвращает список (score, snippet, match_kind).
    """
    results: List[Tuple[float, str, str]] = []

    exact = _exact_search(text, query, context_chars=context_chars)
    for _s, _e, snippet in exact:
        results.append((1.0, snippet, "exact"))

    if not exact:
        fuzzy = _fuzzy_search_words(text, query, fuzzy_threshold, context_chars)
        # дедуп по началу сниппета
        seen = set()
        for score, _s, _e, snippet, _wc in fuzzy:
            key = snippet[:80]
            if key in seen:
                continue
            seen.add(key)
            results.append((score, snippet, "fuzzy"))

    # сортируем по score убыв.
    results.sort(key=lambda x: x[0], reverse=True)
    return results


# ---------------------------------------------------------------------------
# Основная функция поиска
# ---------------------------------------------------------------------------
def search(
    sessions_root: str,
    filters: SearchFilters,
    progress_cb=None,
) -> List[SearchHit]:
    """
    Ищет по всем сессиям в sessions_root согласно фильтрам.

    progress_cb(current, total) — необязательный колбэк для UI.
    """
    query = (filters.query or "").strip()
    if not query:
        return []

    sessions = list(_iter_session_dirs(sessions_root))
    total = len(sessions)
    hits: List[SearchHit] = []

    for i, session_dir in enumerate(sessions):
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

        # --- Фильтр по дате ---
        sdate = _parse_session_date(session_dir, meta)
        if filters.date_from and sdate and sdate.date() < filters.date_from.date():
            continue
        if filters.date_to and sdate and sdate.date() > filters.date_to.date():
            continue

        session_name = meta.get("name") or os.path.basename(session_dir)
        project = meta.get("project") or ""
        date_str = sdate.strftime("%Y-%m-%d") if sdate else ""

        # --- Стенограмма ---
        if filters.search_transcripts:
            for fname in _TRANSCRIPT_NAMES:
                fp = os.path.join(session_dir, fname)
                if not os.path.exists(fp):
                    continue
                text = _read_text_safe(fp)
                if not text:
                    continue
                for score, snippet, kind in _search_in_text(
                    text, query, filters.fuzzy_threshold, filters.context_chars
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
                    ))
                    if len(hits) >= filters.max_results:
                        break

        # --- Протоколы (ручной, deepseek_prompt, protocol.*) ---
        if filters.search_protocols:
            for fname in _PROTOCOL_PATTERNS:
                fp = os.path.join(session_dir, fname)
                if not os.path.exists(fp):
                    continue
                text = _read_text_safe(fp)
                if not text:
                    continue
                for score, snippet, kind in _search_in_text(
                    text, query, filters.fuzzy_threshold, filters.context_chars
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
                    ))
                    if len(hits) >= filters.max_results:
                        break

        # --- Summary из session.json ---
        # В поле summary_bb может лежать Markdown (новые записи)
        # или BB-код (старые). markdown_to_plain_with_bb корректно
        # снимет оба варианта разметки.
        if filters.search_summaries:
            summary_bb = str(meta.get("summary_bb") or "").strip()
            if summary_bb:
                from .markdown_to_bitrix import markdown_to_plain_with_bb
                plain = markdown_to_plain_with_bb(summary_bb)
                for score, snippet, kind in _search_in_text(
                    plain, query, filters.fuzzy_threshold, filters.context_chars
                ):
                    hits.append(SearchHit(
                        session_dir=session_dir,
                        session_name=session_name,
                        project=project,
                        date=date_str,
                        source="summary",
                        file_path=os.path.join(session_dir, "session.json"),
                        file_label="summary (session.json)",
                        snippet=snippet,
                        score=score,
                        match_kind=kind,
                    ))
                    if len(hits) >= filters.max_results:
                        break

        # --- Вложения ---
        if filters.search_attachments:
            att_dir = os.path.join(session_dir, "attachments")
            if os.path.isdir(att_dir):
                for fname in sorted(os.listdir(att_dir)):
                    fp = os.path.join(att_dir, fname)
                    if not os.path.isfile(fp):
                        continue
                    text = _read_text_safe(fp)
                    if not text:
                        continue
                    for score, snippet, kind in _search_in_text(
                        text, query, filters.fuzzy_threshold, filters.context_chars
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
                        ))
                        if len(hits) >= filters.max_results:
                            break

    if progress_cb is not None:
        try:
            progress_cb(total, total)
        except Exception:
            pass

    # Сортируем: сначала по дате (новые сверху), потом по score
    hits.sort(key=lambda h: (h.date, h.score), reverse=True)
    return hits