"""Единый модуль чтения текста из файлов разных форматов.

Устраняет дублирование логики чтения .txt/.md/.docx/.pdf/.json
между library_search, main, processor, send_to_bitrix_dialog и другими.

Все функции устойчивы к ошибкам: при невозможности чтения возвращают
пустую строку и логируют предупреждение.

Изменения:
  • Удалены неиспользуемые _DOC_EXTS и read_any_text_quiet().
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Optional

from .logger import get_logger

log = get_logger(__name__)

# Расширения, которые читаются как plain text
_TEXT_EXTS = {".txt", ".md", ".csv", ".log", ".srt", ".vtt", ".html", ".htm"}

# Максимальный размер читаемого текста (по умолчанию)
DEFAULT_MAX_CHARS = 5_000_000


def read_text_file(
    path: str,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """Читает plain-text файл с ограничением длины."""
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(max_chars)
    except Exception as exc:
        log.warning("Не удалось прочитать %s: %s", path, exc)
        return ""


def read_docx_file(
    path: str,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """Извлекает текст из .docx. Требует python-docx."""
    if not path or not os.path.isfile(path):
        return ""
    try:
        from docx import Document  # type: ignore
    except ImportError:
        log.warning("python-docx не установлен, .docx не прочитан: %s", path)
        return ""

    try:
        doc = Document(path)
        text = "\n".join(p.text for p in doc.paragraphs)
        return text[:max_chars]
    except Exception as exc:
        log.warning("Ошибка чтения .docx %s: %s", path, exc)
        return ""


def read_docx_file(
    path: str,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """
    Извлекает текст из .docx с сохранением таблиц.

    Обходит документ в порядке следования элементов
    (параграфы и таблицы идут вперемешку), таблицы выводит
    в Markdown-формате — так, чтобы их можно было отдать LLM
    и они корректно рендерились в промпт.

    Требует python-docx.
    """
    if not path or not os.path.isfile(path):
        return ""
    try:
        from docx import Document  # type: ignore
    except ImportError:
        log.warning(
            "python-docx не установлен, .docx не прочитан: %s", path
        )
        return ""

    try:
        doc = Document(path)
    except Exception as exc:
        log.warning("Ошибка чтения .docx %s: %s", path, exc)
        return ""

    # Пытаемся обойти body в порядке следования элементов.
    # Если получится — параграфы и таблицы будут в правильном
    # порядке. Если нет — fallback на раздельные обходы.
    parts: list[str] = []
    try:
        from docx.document import Document as _DocClass  # type: ignore
        from docx.table import Table  # type: ignore
        from docx.text.paragraph import Paragraph  # type: ignore

        body = doc.element.body
        for child in body.iterchildren():
            tag = child.tag
            if tag.endswith("}p"):
                paragraph = Paragraph(child, doc)
                text = paragraph.text or ""
                if text.strip():
                    parts.append(text)
                else:
                    # Пустой параграф — сохраняем как разделитель.
                    parts.append("")
            elif tag.endswith("}tbl"):
                table = Table(child, doc)
                md = _docx_table_to_markdown(table)
                if md:
                    if parts and parts[-1] != "":
                        parts.append("")
                    parts.append(md)
                    parts.append("")
    except Exception as exc:
        log.warning(
            "Не удалось обойти .docx в порядке следования, "
            "использую fallback (%s): %s", path, exc,
        )
        # --- Fallback: сначала все параграфы, потом таблицы ---
        parts = [p.text for p in doc.paragraphs if p.text]
        for table in doc.tables:
            md = _docx_table_to_markdown(table)
            if md:
                parts.append("")
                parts.append(md)

    text = "\n".join(parts)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:max_chars]


def _docx_table_to_markdown(table) -> str:
    """
    Преобразует таблицу python-docx в Markdown.

    Первая строка считается заголовком. Если в таблице одна
    строка — выводится без разделителя заголовка.

    Ячейки очищаются от переводов строк (для Markdown-таблицы
    это критично: перенос строки внутри ячейки ломает разметку).
    """
    try:
        rows = list(table.rows)
    except Exception:
        return ""

    if not rows:
        return ""

    def _cell_text(cell) -> str:
        # Собираем текст из всех параграфов ячейки.
        try:
            txt = "\n".join(
                (p.text or "") for p in cell.paragraphs
            )
        except Exception:
            txt = cell.text or ""
        # Схлопываем переводы строк и лишние пробелы —
        # внутри Markdown-ячейки это недопустимо.
        txt = txt.replace("\n", " ").replace("\r", " ")
        txt = re.sub(r"\s{2,}", " ", txt)
        # Экранируем вертикальную черту (символ разделителя
        # в Markdown-таблице).
        txt = txt.replace("|", "\\|")
        return txt.strip()

    # --- Определяем число колонок как максимум по строкам ---
    row_cells: list[list[str]] = []
    for r in rows:
        try:
            cells = list(r.cells)
        except Exception:
            cells = []
        row_cells.append([_cell_text(c) for c in cells])

    if not row_cells:
        return ""

    n_cols = max(len(r) for r in row_cells)
    if n_cols == 0:
        return ""

    # --- Выравниваем строки по числу колонок ---
    def _pad(row: list[str]) -> list[str]:
        if len(row) < n_cols:
            return row + [""] * (n_cols - len(row))
        return row[:n_cols]

    header = _pad(row_cells[0])
    body = [_pad(r) for r in row_cells[1:]]

    lines: list[str] = []
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join(["---"] * n_cols) + " |")
    for r in body:
        lines.append("| " + " | ".join(r) + " |")

    return "\n".join(lines)


def read_json_file(path: str) -> Optional[Dict[str, Any]]:
    """Читает JSON-файл. Возвращает None при ошибке."""
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception as exc:
        log.warning("Не удалось прочитать JSON %s: %s", path, exc)
        return None


def read_json_as_text(
    path: str,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """
    Читает JSON и извлекает из него осмысленный текст.
    Если внутри есть ключ script/text/transcript/content — вернёт его.
    Иначе — pretty-printed JSON целиком.
    """
    data = read_json_file(path)
    if data is None:
        return ""
    for key in ("script", "text", "transcript", "content"):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return v[:max_chars]
    try:
        return json.dumps(data, ensure_ascii=False, indent=2)[:max_chars]
    except Exception:
        return ""


def read_any_text(
    path: str,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """
    Универсальный ридер: определяет формат по расширению.

    Поддерживает: .txt/.md/.csv/.json/.log/.srt/.vtt/.docx/.pdf.
    Для неподдерживаемых расширений возвращает пустую строку.
    """
    if not path or not os.path.isfile(path):
        return ""

    ext = os.path.splitext(path)[1].lower()

    if ext in _TEXT_EXTS:
        return read_text_file(path, max_chars)
    if ext == ".json":
        return read_json_as_text(path, max_chars)
    if ext == ".docx":
        return read_docx_file(path, max_chars)
    if ext == ".pdf":
        return read_pdf_file(path, max_chars)

    log.warning("Неподдерживаемое расширение: %s (%s)", ext, path)
    return ""