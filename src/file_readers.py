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


def read_pdf_file(
    path: str,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """Извлекает текст из .pdf. Требует pypdf."""
    if not path or not os.path.isfile(path):
        return ""
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        log.warning("pypdf не установлен, .pdf не прочитан: %s", path)
        return ""

    try:
        reader = PdfReader(path)
        chunks = []
        total = 0
        for page in reader.pages:
            t = page.extract_text() or ""
            chunks.append(t)
            total += len(t)
            if total >= max_chars:
                break
        return "\n".join(chunks)[:max_chars]
    except Exception as exc:
        log.warning("Ошибка чтения .pdf %s: %s", path, exc)
        return ""


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