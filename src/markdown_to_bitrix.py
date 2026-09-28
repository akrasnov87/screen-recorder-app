"""Конвертер Markdown → BB-код Bitrix24 + сопутствующие утилиты.

Основная функция: markdown_to_bitrix(text) — превращает Markdown
в BB-код, который портал Bitrix24 отрендерит как форматированный текст.

Поддерживается подмножество Markdown:
    # H1..###### H6        → [B]…[/B] (с переносом строки)
    **жирный**             → [B]…[/B]
    *курсив* / _курсив_    → [I]…[/I]
    `инлайн-код`           → [CODE]…[/CODE]
    ```блок кода```        → [CODE]…[/CODE]
    - / * / + список       → [LIST][*]…[/LIST]
    1. список              → [LIST=1][*]…[/LIST]
    > цитата               → [QUOTE]…[/QUOTE]
    ---                    → ───────── (строка)
    [текст](url)           → [URL=url]текст[/URL]
    ![alt](url)            → [IMG]url[/IMG]

Если элемент не удалось сопоставить — текст остаётся как есть
(без BB-тегов). Квадратные скобки в обычном тексте не экранируются:
пользователь может вставлять BB-теги напрямую, они пройдут насквозь.

Дополнительно:
    markdown_to_plain(text) — Markdown → plain text.
    markdown_to_plain_with_bb(text) — сначала удаляет BB-теги
        (для старых записей), затем Markdown → plain.
"""
from __future__ import annotations

import html
import re
from typing import List

from .logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Markdown → BB-код Bitrix24
# ---------------------------------------------------------------------------
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_ULIST_RE = re.compile(r"^[-*+]\s+(.*)$")
_OLIST_RE = re.compile(r"^(\d+)\.\s+(.*)$")
_HR_RE = re.compile(r"^(-{3,}|\*{3,}|_{3,})$")
_CODE_FENCE_RE = re.compile(r"^```")

# Инлайн-разметка. Порядок альтернатив важен: **жирный** должен
# проверяться раньше, чем *курсив*, иначе звёздочки жирного
# распарсятся неправильно.
_INLINE_RE = re.compile(
    r"(\*\*.+?\*\*"                # **bold**
    r"|__.+?__"                    # __bold__
    r"|(?<!\*)\*(?!\*)[^*\n]+?\*(?!\*)"  # *italic* (не часть **)
    r"|(?<!_)_(?!_)[^_\n]+?_(?!_)"  # _italic_ (не часть __)
    r"|`[^`\n]+?`"                 # `code`
    r"|!\[[^\]]*?\]\([^)]+?\)"     # ![alt](url)
    r"|\[[^\]]+?\]\([^)]+?\)"      # [text](url)
    r")"
)


def _inline_to_bb(text: str) -> str:
    """
    Преобразует инлайн-разметку Markdown в BB-код Bitrix24.
    Не трогает текст, который не похож на разметку.
    """
    if not text:
        return ""

    parts = _INLINE_RE.split(text)
    out: List[str] = []

    for part in parts:
        if not part:
            continue

        # **bold** или __bold__
        if (
            (part.startswith("**") and part.endswith("**") and len(part) > 4)
            or (part.startswith("__") and part.endswith("__") and len(part) > 4)
        ):
            out.append(f"[B]{part[2:-2]}[/B]")
            continue

        # *italic* или _italic_
        if (
            (part.startswith("*") and part.endswith("*")
             and len(part) > 2 and not part.startswith("**"))
            or (part.startswith("_") and part.endswith("_")
                and len(part) > 2 and not part.startswith("__"))
        ):
            out.append(f"[I]{part[1:-1]}[/I]")
            continue

        # `code`
        if part.startswith("`") and part.endswith("`") and len(part) > 2:
            out.append(f"[CODE]{part[1:-1]}[/CODE]")
            continue

        # ![alt](url)
        m = re.match(r"!\[([^\]]*?)\]\(([^)]+?)\)$", part)
        if m:
            url = m.group(2)
            out.append(f"[IMG]{url}[/IMG]")
            continue

        # [text](url)
        m = re.match(r"\[([^\]]+?)\]\(([^)]+?)\)$", part)
        if m:
            label = m.group(1)
            url = m.group(2)
            out.append(f"[URL={url}]{label}[/URL]")
            continue

        # Обычный текст
        out.append(part)

    return "".join(out)


def markdown_to_bitrix(text: str) -> str:
    """
    Конвертирует Markdown в BB-код Bitrix24.

    Никакой санитайзации не делает — текст считается доверенным.
    Если элемент не распознан — оставляется как есть.
    """
    if not text:
        return ""

    lines = text.splitlines()
    out: List[str] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # --- Пустая строка ---
        if not stripped:
            out.append("")
            i += 1
            continue

        # --- Блок кода ```...``` ---
        if _CODE_FENCE_RE.match(stripped):
            i += 1
            code_lines: List[str] = []
            while i < n and not _CODE_FENCE_RE.match(lines[i].strip()):
                code_lines.append(lines[i])
                i += 1
            if i < n:
                i += 1
            out.append("[CODE]")
            out.extend(code_lines)
            out.append("[/CODE]")
            continue

        # --- Горизонтальная линия ---
        if _HR_RE.match(stripped):
            out.append("─" * 40)
            i += 1
            continue

        # --- Заголовок: # H1 .. ###### H6 ---
        m = _HEADING_RE.match(stripped)
        if m:
            out.append(f"[B]{_inline_to_bb(m.group(2).strip())}[/B]")
            out.append("")
            i += 1
            continue

        # --- Цитата: строки, начинающиеся с > ---
        if stripped.startswith(">"):
            quote_lines: List[str] = []
            while i < n and lines[i].strip().startswith(">"):
                quote_lines.append(lines[i].strip().lstrip(">").strip())
                i += 1
            body = "\n".join(_inline_to_bb(l) for l in quote_lines)
            out.append("[QUOTE]")
            out.append(body)
            out.append("[/QUOTE]")
            continue

        # --- Маркированный список ---
        if _ULIST_RE.match(stripped):
            out.append("[LIST]")
            while i < n:
                m2 = _ULIST_RE.match(lines[i].strip())
                if not m2:
                    break
                out.append(f"[*]{_inline_to_bb(m2.group(1))}")
                i += 1
            out.append("[/LIST]")
            continue

        # --- Нумерованный список ---
        if _OLIST_RE.match(stripped):
            out.append("[LIST=1]")
            while i < n:
                m2 = _OLIST_RE.match(lines[i].strip())
                if not m2:
                    break
                out.append(f"[*]{_inline_to_bb(m2.group(2))}")
                i += 1
            out.append("[/LIST]")
            continue

        # --- Обычный абзац ---
        out.append(_inline_to_bb(stripped))
        i += 1

    # Схлопываем 3+ пустых строк до одной.
    result = "\n".join(out)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


# ---------------------------------------------------------------------------
# Markdown → plain text
# ---------------------------------------------------------------------------
def markdown_to_plain(text: str) -> str:
    """
    Убирает Markdown-разметку и оставляет чистый текст.
    Используется для передачи summary в суммаризатор и для поиска.
    """
    if not text:
        return ""

    s = text

    # Блоки кода ```...```
    s = re.sub(
        r"```(.*?)```",
        lambda m: m.group(1).strip(),
        s,
        flags=re.DOTALL,
    )

    # Заголовки # ...
    s = re.sub(r"^#{1,6}\s+", "", s, flags=re.MULTILINE)

    # Цитаты >
    s = re.sub(r"^>\s?", "", s, flags=re.MULTILINE)

    # Списки - / * / + / 1.
    s = re.sub(r"^\s*[-*+]\s+", "", s, flags=re.MULTILINE)
    s = re.sub(r"^\s*\d+\.\s+", "", s, flags=re.MULTILINE)

    # Горизонтальные линии
    s = re.sub(r"^[-*_]{3,}\s*$", "", s, flags=re.MULTILINE)

    # ![alt](url) → alt
    s = re.sub(r"!\[([^\]]*?)\]\([^)]+?\)", r"\1", s)
    # [text](url) → text (url)
    s = re.sub(r"\[([^\]]+?)\]\(([^)]+?)\)", r"\1 (\2)", s)

    # Инлайн-разметка
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"__(.+?)__", r"\1", s)
    s = re.sub(r"(?<!\*)\*(?!\*)([^*\n]+?)\*(?!\*)", r"\1", s)
    s = re.sub(r"(?<!_)_(?!_)([^_\n]+?)_(?!_)", r"\1", s)
    s = re.sub(r"`([^`\n]+?)`", r"\1", s)

    return s.strip()


def markdown_to_plain_with_bb(text: str) -> str:
    """
    Убирает и BB-теги, и Markdown-разметку.

    Нужна для обратной совместимости: в старых записях summary
    мог быть в BB-коде, в новых — в Markdown. Функция корректно
    обрабатывает оба случая.
    """
    if not text:
        return ""

    # Импорт здесь, чтобы избежать циклической зависимости:
    # bbcode_editor не импортирует markdown_to_bitrix.
    from .bbcode_editor import bbcode_to_plain

    # Сначала Markdown → plain (снимает #, **, *, `, ссылки),
    # затем BB → plain (снимает [b], [url], [quote] и т.д.).
    return bbcode_to_plain(markdown_to_plain(text))