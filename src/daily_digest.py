"""Формирование «свода за день» (daily digest) из записей за период.

Модуль собирает протоколы (и опционально — summary/стенограммы)
из выбранных записей и формирует единый промпт, который можно
отдать ИИ для получения сводки по дню.

Логика:
  1. Принимает список папок сессий за выбранный период.
  2. Для каждой сессии ищет протокол в порядке приоритета:
     manual_protocol.* → protocol.* → deepseek_prompt.*.
  3. Читает текст через file_readers.read_any_text.
  4. Собирает единый документ: инструкция + метаданные сессий
     + тексты протоколов.
  5. Сохраняет в выбранном формате (docx/md/txt).
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .file_readers import read_any_text, read_json_file
from .logger import get_logger

log = get_logger(__name__)


# Порядок приоритетов при поиске протокола в папке сессии.
_PROTOCOL_CANDIDATES: List[Tuple[str, str]] = [
    ("manual_protocol", "manual_protocol.docx"),
    ("manual_protocol", "manual_protocol.md"),
    ("manual_protocol", "manual_protocol.txt"),
    ("manual_protocol", "manual_protocol.pdf"),
    ("protocol", "protocol.docx"),
    ("protocol", "protocol.md"),
    ("protocol", "protocol.txt"),
    ("protocol", "protocol.pdf"),
    ("deepseek_prompt", "deepseek_prompt.docx"),
    ("deepseek_prompt", "deepseek_prompt.md"),
    ("deepseek_prompt", "deepseek_prompt.txt"),
]

# Максимум символов при чтении одного файла — защита от гигантских PDF.
DEFAULT_MAX_CHARS = 5_000_000


# ---------------------------------------------------------------------------
# Поиск и чтение данных сессии
# ---------------------------------------------------------------------------
def find_protocol_in_session(
    session_dir: str,
) -> Tuple[str, str]:
    """
    Ищет протокол в папке сессии.

    Returns:
        (путь_к_протоколу, метка_типа)
        ("", "") — если ничего не найдено.
    """
    if not session_dir or not os.path.isdir(session_dir):
        return "", ""

    for kind, fname in _PROTOCOL_CANDIDATES:
        path = os.path.join(session_dir, fname)
        if os.path.isfile(path):
            return path, kind
    return "", ""


def load_session_meta(session_dir: str) -> Dict[str, Any]:
    """Читает session.json, возвращает {} при ошибке."""
    path = os.path.join(session_dir, "session.json")
    data = read_json_file(path)
    return data or {}


def collect_digest_entries(
    session_dirs: List[str],
    *,
    max_chars_per_file: int = DEFAULT_MAX_CHARS,
) -> List[Dict[str, Any]]:
    """
    Собирает данные по списку сессий.

    Возвращает список словарей:
        {
          "session_dir": str,
          "name": str,
          "project": str,
          "date": str,
          "time": str,
          "protocol_path": str,   # путь к протоколу ("" если нет)
          "protocol_kind": str,   # "manual_protocol" / "protocol" / ...
          "protocol_text": str,   # извлечённый текст
          "tags": List[str],
          "summary_bb": str,      # краткое описание из session.json
        }
    """
    entries: List[Dict[str, Any]] = []

    for sdir in session_dirs:
        if not sdir or not os.path.isdir(sdir):
            continue

        meta = load_session_meta(sdir)
        protocol_path, protocol_kind = find_protocol_in_session(sdir)

        protocol_text = ""
        if protocol_path:
            try:
                protocol_text = read_any_text(
                    protocol_path, max_chars_per_file
                )
            except Exception as exc:
                log.warning(
                    "Не удалось прочитать протокол %s: %s",
                    protocol_path, exc,
                )

        # --- Теги ---
        tags: List[str] = []
        raw_tags = meta.get("tags")
        if isinstance(raw_tags, list):
            for t in raw_tags:
                if isinstance(t, str) and t.strip():
                    tags.append(t.strip())
                elif isinstance(t, dict):
                    name = str(t.get("name") or "").strip()
                    if name:
                        tags.append(name)

        # --- Дата и время ---
        date_str = str(meta.get("date") or "").strip()
        time_str = str(meta.get("time") or "").strip()
        if not date_str:
            # Fallback: имя папки обычно «YYYY-MM-DD_HH-MM-SS».
            base = os.path.basename(sdir.rstrip("/"))
            parts = base.split("_")
            if parts and len(parts[0]) == 10:
                date_str = parts[0]
            if not time_str and len(parts) >= 2:
                time_str = parts[1].replace("-", ":")

        entries.append({
            "session_dir": sdir,
            "name": str(meta.get("name") or os.path.basename(sdir)),
            "project": str(meta.get("project") or "").strip(),
            "date": date_str,
            "time": time_str,
            "protocol_path": protocol_path,
            "protocol_kind": protocol_kind,
            "protocol_text": protocol_text,
            "tags": tags,
            "summary_bb": str(meta.get("summary_bb") or "").strip(),
        })

    # Сортировка по дате и времени.
    entries.sort(key=lambda e: (e["date"], e["time"]))
    return entries


# ---------------------------------------------------------------------------
# Сборка промпта
# ---------------------------------------------------------------------------
def build_digest_prompt(
    entries: List[Dict[str, Any]],
    *,
    user_instruction: str,
    period_label: str = "",
    include_summary: bool = False,
) -> str:
    """
    Собирает единый Markdown-промпт из записей за период.

    Args:
        entries:          список записей (из collect_digest_entries).
        user_instruction: инструкция для ИИ (пользовательский промпт).
        period_label:     человекочитаемая метка периода, например
                          «2026-10-05» или «2026-10-01 … 2026-10-05».
        include_summary:  добавлять ли краткое описание (summary_bb)
                          каждой записи.

    Returns:
        Готовый Markdown-текст.
    """
    parts: List[str] = []

    # --- Инструкция ---
    parts.append("# Сводка за период")
    parts.append("")
    if period_label:
        parts.append(f"**Период:** {period_label}")
        parts.append("")
    parts.append(f"**Записей в своде:** {len(entries)}")
    parts.append("")

    if user_instruction and user_instruction.strip():
        parts.append("## Инструкция для ИИ")
        parts.append("")
        parts.append(user_instruction.strip())
        parts.append("")

    # --- Содержание ---
    parts.append("## Протоколы совещаний")
    parts.append("")

    for idx, e in enumerate(entries, start=1):
        header_bits: List[str] = [e["name"] or "Без названия"]
        meta_bits: List[str] = []
        if e.get("date"):
            dt = e["date"]
            if e.get("time"):
                dt += f" {e['time']}"
            meta_bits.append(f"дата: {dt}")
        if e.get("project"):
            meta_bits.append(f"проект: {e['project']}")
        if e.get("tags"):
            meta_bits.append(f"теги: {', '.join(e['tags'])}")

        parts.append(f"### {idx}. {header_bits[0]}")
        parts.append("")
        if meta_bits:
            parts.append("*" + " · ".join(meta_bits) + "*")
            parts.append("")

        if include_summary and e.get("summary_bb"):
            parts.append("**Краткое описание:**")
            parts.append("")
            parts.append(e["summary_bb"])
            parts.append("")

        if e.get("protocol_text"):
            parts.append("**Протокол:**")
            parts.append("")
            parts.append(e["protocol_text"].strip())
            parts.append("")
        elif e.get("protocol_path"):
            parts.append(
                "*Протокол найден, но пустой или нечитаемый.*"
            )
            parts.append("")
        else:
            parts.append("*Протокол не приложен к записи.*")
            parts.append("")

        parts.append("---")
        parts.append("")

    parts.append(
        "*Промпт сформирован автоматически из записей за указанный "
        "период. Проанализируй протоколы и составь сводку по "
        "инструкции выше.*"
    )

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Сохранение
# ---------------------------------------------------------------------------
def save_digest_prompt(
    prompt_text: str,
    output_dir: str,
    *,
    fmt: str = "docx",
    base_name: str = "",
) -> str:
    """
    Сохраняет промпт в файл.

    Args:
        prompt_text: текст промпта (Markdown).
        output_dir:  папка для сохранения.
        fmt:         "docx" | "md" | "txt".
        base_name:   базовое имя файла без расширения. Если пусто —
                     генерируется «daily_digest_YYYY-MM-DD_HH-MM-SS».

    Returns:
        Полный путь к сохранённому файлу или "" при ошибке.
    """
    if not prompt_text.strip():
        log.warning("save_digest_prompt: пустой текст")
        return ""

    os.makedirs(output_dir, exist_ok=True)

    fmt = (fmt or "docx").lower()
    if fmt not in ("docx", "md", "txt"):
        fmt = "docx"

    if not base_name:
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        base_name = f"daily_digest_{stamp}"

    path = os.path.join(output_dir, f"{base_name}.{fmt}")

    if fmt == "txt":
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(prompt_text)
            log.info("Свод за день сохранён (txt): %s", path)
            return path
        except Exception as exc:
            log.exception("Не удалось сохранить %s: %s", path, exc)
            return ""

    if fmt == "md":
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(prompt_text)
            log.info("Свод за день сохранён (md): %s", path)
            return path
        except Exception as exc:
            log.exception("Не удалось сохранить %s: %s", path, exc)
            return ""

    # --- DOCX ---
    try:
        from .markdown_docx import markdown_to_docx

        markdown_to_docx(
            prompt_text,
            path,
            title="Сводка за период",
        )
        log.info("Свод за день сохранён (docx): %s", path)
        return path
    except Exception as exc:
        log.exception("Не удалось сохранить DOCX %s: %s", path, exc)
        return ""