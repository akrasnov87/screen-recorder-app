"""Конвертер DOCX → HTML для отображения в QTextBrowser.

Извлекает из .docx структуру и форматирование:
  • заголовки (Heading 1..6, русские «Заголовок 1..6»);
  • обычные параграфы;
  • списки (List Bullet / List Number, а также абзацы с numPr);
  • таблицы с границами;
  • inline-форматирование: bold, italic, underline;
  • моно-шрифт (Consolas / Courier New) — как <code>;
  • выравнивание (left / center / right / justify);
  • выравнивание параграфа, отступы первой строки;
  • цвет текста — сохраняется как есть (hex).

Почему это лучше read_docx_file():
  read_docx_file() возвращает только p.text, теряя всю разметку.
  Этот модуль сохраняет структуру и делает протокол читаемым.

Использование:
    from ..docx_to_html import docx_to_html
    html_body = docx_to_html("/path/to/protocol.docx")
    browser.setHtml(
        "<html><body style='font-family:sans-serif'>"
        + html_body
        + "</body></html>"
    )
"""
from __future__ import annotations

import html
import os
from typing import Any, List, Optional

from .logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Стили
# ---------------------------------------------------------------------------
_CSS = """
<style>
  body {
    font-family: sans-serif;
    font-size: 11pt;
    color: #1f1f1f;
    line-height: 1.4;
  }
  h1 { font-size: 18pt; color: #0d47a1;
       margin: 18px 0 8px 0; }
  h2 { font-size: 15pt; color: #1565c0;
       margin: 16px 0 6px 0; }
  h3 { font-size: 13pt; color: #1976d2;
       margin: 14px 0 6px 0; }
  h4 { font-size: 12pt; color: #1e88e5;
       margin: 12px 0 4px 0; }
  h5 { font-size: 11pt; color: #2196f3;
       margin: 10px 0 4px 0; }
  h6 { font-size: 11pt; color: #42a5f5;
       margin: 10px 0 4px 0; font-style: italic; }
  p  { margin: 4px 0; }
  ul, ol { margin: 4px 0 4px 24px; padding: 0; }
  li { margin: 2px 0; }
  code, pre {
    font-family: Consolas, "Courier New", monospace;
    background: #f4f4f4;
    border-radius: 3px;
  }
  code { padding: 1px 4px; }
  pre {
    padding: 8px;
    white-space: pre-wrap;
    font-size: 10pt;
  }
  table {
    border-collapse: collapse;
    margin: 8px 0;
  }
  table td, table th {
    border: 1px solid #999;
    padding: 4px 8px;
    vertical-align: top;
    font-size: 10pt;
  }
  table th {
    background: #f0f0f0;
    font-weight: bold;
  }
</style>
"""


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------
def docx_to_html(path: str) -> str:
    """
    Конвертирует .docx в HTML-фрагмент (без обёртки <html>/<body>).

    Возвращает "" при ошибке (файл не найден, python-docx
    недоступен и т.п.).
    """
    if not path or not os.path.isfile(path):
        log.warning("docx_to_html: файл не найден: %s", path)
        return ""

    try:
        from docx import Document  # type: ignore
    except ImportError:
        log.warning(
            "docx_to_html: python-docx не установлен — "
            "использую fallback (plain text)"
        )
        return _fallback_plain(path)

    try:
        doc = Document(path)
    except Exception as exc:
        log.warning(
            "docx_to_html: не удалось открыть %s: %s", path, exc
        )
        return _fallback_plain(path)

    try:
        return _docx_document_to_html(doc)
    except Exception as exc:
        log.exception(
            "docx_to_html: ошибка рендера %s: %s", path, exc
        )
        return _fallback_plain(path)


# ---------------------------------------------------------------------------
# Внутренние функции
# ---------------------------------------------------------------------------
def _fallback_plain(path: str) -> str:
    """Аварийный вариант — читаем как plain text."""
    try:
        from .file_readers import read_docx_file
        text = read_docx_file(path)
    except Exception:
        text = ""

    if not text:
        return (
            "<p style='color:#c62828'>"
            "Не удалось прочитать содержимое документа.</p>"
        )

    return (
        "<pre style='white-space:pre-wrap; "
        "font-family:Consolas, monospace; font-size:10pt;'>"
        + html.escape(text)
        + "</pre>"
    )


def _docx_document_to_html(doc: Any) -> str:
    """
    Проходит по элементам документа в порядке их следования
    (параграфы и таблицы идут вперемешку) и возвращает HTML.
    """
    parts: List[str] = []

    # Собираем "flow" документа в правильном порядке.
    try:
        from docx.document import Document as _DocClass  # type: ignore
        from docx.table import Table  # type: ignore
        from docx.text.paragraph import Paragraph  # type: ignore
    except ImportError:
        # Крайне старый python-docx — идём по paragraphs+table
        # отдельно (потеряем порядок, но не сломаемся).
        for p in doc.paragraphs:
            parts.append(_paragraph_to_html(p))
        for t in doc.tables:
            parts.append(_table_to_html(t))
        return _CSS + "".join(parts)

    body = doc.element.body
    for child in body.iterchildren():
        tag = child.tag
        if tag.endswith("}p"):
            paragraph = Paragraph(child, doc)
            parts.append(_paragraph_to_html(paragraph))
        elif tag.endswith("}tbl"):
            table = Table(child, doc)
            parts.append(_table_to_html(table))

    return _CSS + "".join(parts)


# ---------------------------------------------------------------------------
# Параграфы
# ---------------------------------------------------------------------------
def _paragraph_to_html(paragraph: Any) -> str:
    """Рендерит один параграф как HTML."""
    text_html = _runs_to_html(paragraph.runs)

    style_name = ""
    if paragraph.style is not None:
        style_name = (paragraph.style.name or "").strip()

    # --- Заголовок ---
    level = _heading_level(style_name)
    if level > 0:
        return f"<h{level}>{text_html}</h{level}>"

    # --- Список ---
    list_tag, list_level = _list_info(paragraph, style_name)
    if list_tag:
        # Без <ul>/<ol> обёртки — используем <li> в <ul> снизу
        # через псевдоструктуру: каждый элемент оборачиваем в
        # <ul><li>…</li></ul>, чтобы Qt корректно отрисовал
        # буллеты (Qt не умеет сливать соседние <li>).
        bullet_char = "•" if list_tag == "ul" else "▸"
        indent = "&nbsp;" * (list_level * 4)
        return (
            f"<p style='margin:2px 0 2px {list_level * 20}px;'>"
            f"<span style='color:#555'>{indent}{bullet_char}</span> "
            f"{text_html}</p>"
        )

    # --- Цитата ---
    style_low = style_name.lower()
    if "quote" in style_low or "цитата" in style_low:
        return (
            "<blockquote style='border-left:3px solid #bbb; "
            "margin:6px 0; padding:4px 12px; color:#444;'>"
            f"{text_html}</blockquote>"
        )

    # --- Обычный параграф ---
    align = _paragraph_align(paragraph)
    align_css = f"text-align:{align};" if align else ""
    if not text_html.strip():
        # Пустая строка — уменьшаем отступ.
        return f"<p style='margin:4px 0; {align_css}'>&nbsp;</p>"
    return f"<p style='margin:4px 0; {align_css}'>{text_html}</p>"


def _heading_level(style_name: str) -> int:
    """Определяет уровень заголовка (0 — не заголовок)."""
    if not style_name:
        return 0
    name = style_name.strip()

    if name.startswith("Heading "):
        try:
            return min(6, max(1, int(name.split(" ", 1)[1])))
        except (IndexError, ValueError):
            return 1

    low = name.lower()
    for ru_prefix in ("заголовок ",):
        if low.startswith(ru_prefix):
            try:
                return min(
                    6, max(1, int(low[len(ru_prefix):].strip()))
                )
            except ValueError:
                return 1
    return 0


def _list_info(paragraph: Any, style_name: str):
    """
    Определяет, является ли параграф элементом списка.

    Возвращает (tag, level), где tag ∈ {"ul", "ol", ""}.
    """
    name_low = (style_name or "").lower()

    # 1) Явные стили List Bullet / List Number.
    if "list bullet" in name_low or "маркированный список" in name_low:
        return "ul", _list_level_from_style(style_name)
    if "list number" in name_low or "нумерованный список" in name_low:
        return "ol", _list_level_from_style(style_name)

    # 2) Проверка по numPr в XML (иногда стиль = Normal, но
    # абзац отформатирован как список).
    try:
        ppr = paragraph._p.pPr
        if ppr is None:
            return "", 0
        numpr = ppr.numPr
        if numpr is None:
            return "", 0

        # Уровень отступа.
        level = 0
        ilvl = numpr.ilvl
        if ilvl is not None and ilvl.val is not None:
            try:
                level = int(ilvl.val)
            except (TypeError, ValueError):
                level = 0

        # Тип нумерации определяем по abstractNum — но это
        # тяжело без доступа к numbering part. Компромисс:
        # считаем такие списки маркированными.
        return "ul", level
    except Exception:
        return "", 0


def _list_level_from_style(style_name: str) -> int:
    """Возвращает уровень списка из имени стиля (0 для базового)."""
    if not style_name:
        return 0
    # "List Bullet 2" → 1
    parts = style_name.strip().split()
    if len(parts) >= 3:
        try:
            return max(0, int(parts[-1]) - 1)
        except ValueError:
            pass
    return 0


def _paragraph_align(paragraph: Any) -> str:
    """Возвращает CSS-значение выравнивания или ""."""
    try:
        from docx.enum.text import WD_ALIGN_PARAGRAPH  # type: ignore
        align = paragraph.alignment
        if align is None:
            return ""
        mapping = {
            WD_ALIGN_PARAGRAPH.LEFT: "left",
            WD_ALIGN_PARAGRAPH.CENTER: "center",
            WD_ALIGN_PARAGRAPH.RIGHT: "right",
            WD_ALIGN_PARAGRAPH.JUSTIFY: "justify",
        }
        return mapping.get(align, "")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Runs (инлайн-форматирование)
# ---------------------------------------------------------------------------
def _runs_to_html(runs: List[Any]) -> str:
    """Собирает HTML из runs одного параграфа."""
    if not runs:
        return ""

    parts: List[str] = []
    for run in runs:
        parts.append(_run_to_html(run))
    return "".join(parts)


def _run_to_html(run: Any) -> str:
    """Рендерит один run как HTML."""
    text = run.text or ""
    if not text:
        # Пробел без текста: run может быть просто пробелом.
        return ""

    escaped = html.escape(text)

    # Ссылки.
    try:
        if getattr(run, "hyperlink", None) is not None:
            url = getattr(run.hyperlink, "address", "") or ""
            if url:
                escaped = (
                    f"<a href='{html.escape(url)}'>{escaped}</a>"
                )
    except Exception:
        pass

    # Inline-форматирование.
    try:
        if run.bold:
            escaped = f"<b>{escaped}</b>"
        if run.italic:
            escaped = f"<i>{escaped}</i>"
        if run.underline:
            escaped = f"<u>{escaped}</u>"
    except Exception:
        pass

    # Цвет и размер — опционально, чтобы не сломать тему.
    # Применяем, только если есть явное указание.
    style_bits: List[str] = []
    try:
        if run.font is not None:
            # Цвет.
            color = run.font.color
            if color is not None and color.rgb is not None:
                style_bits.append(f"color:#{color.rgb}")
            # Размер.
            size = run.font.size
            if size is not None:
                # size в EMU (1/914400 inch); нам нужны pt:
                # pt = size.pt
                try:
                    pt = size.pt
                    if pt and 6 <= pt <= 72:
                        style_bits.append(f"font-size:{pt:g}pt")
                except Exception:
                    pass
            # Моно-шрифт.
            name = run.font.name or ""
            if name and any(
                x in name.lower()
                for x in ("consolas", "courier", "mono")
            ):
                escaped = f"<code>{escaped}</code>"
    except Exception:
        pass

    if style_bits:
        style_attr = ";".join(style_bits)
        return f"<span style='{style_attr}'>{escaped}</span>"
    return escaped


# ---------------------------------------------------------------------------
# Таблицы
# ---------------------------------------------------------------------------
def _table_to_html(table: Any) -> str:
    """Рендерит таблицу docx в HTML."""
    try:
        rows = list(table.rows)
    except Exception:
        return ""

    if not rows:
        return ""

    parts: List[str] = ["<table>"]
    for r_idx, row in enumerate(rows):
        parts.append("<tr>")
        try:
            cells = list(row.cells)
        except Exception:
            cells = []
        for cell in cells:
            # Заголовки — первая строка.
            tag = "th" if r_idx == 0 else "td"
            # Внутри ячейки может быть несколько параграфов.
            cell_html_parts: List[str] = []
            for p in cell.paragraphs:
                p_html = _paragraph_to_html(p)
                cell_html_parts.append(p_html)
            cell_html = "".join(cell_html_parts) or "&nbsp;"
            parts.append(f"<{tag}>{cell_html}</{tag}>")
        parts.append("</tr>")
    parts.append("</table>")
    return "".join(parts)