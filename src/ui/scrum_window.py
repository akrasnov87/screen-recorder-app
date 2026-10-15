"""Окно «Скрам» — рабочее место для скрам-митинга.

Открывается из окна «Записи» (пункт меню «Скрам»), доступно
только для записей, помеченных как скрам-митинг (is_scrum=true).

Структура:
  ┌──────────────────────────────────────────────────────────────┐
  │  QSplitter (Horizontal)                                       │
  │  ┌────────────────────────┐ ┌──────────────────────────────┐ │
  │  │ Left (Vertical Splitter)│ │ Right: QTabWidget            │ │
  │  │  ┌──────────────────┐  │ │  ┌────────────────────────┐  │ │
  │  │  │ Поручения        │  │ │  │ Протокол               │  │ │
  │  │  │ (TasksEditor)    │  │ │  │                        │  │ │
  │  │  └──────────────────┘  │ │  └────────────────────────┘  │ │
  │  │  ┌──────────────────┐  │ │  ┌────────────────────────┐  │ │
  │  │  │ Задачи TFS       │  │ │  │ Записи за день         │  │ │
  │  │  │ (TfsPanel)       │  │ │  │                        │  │ │
  │  │  └──────────────────┘  │ │  └────────────────────────┘  │ │
  │  └────────────────────────┘ └──────────────────────────────┘ │
  └──────────────────────────────────────────────────────────────┘

Изменения:
  • Модуль создан.
  • ИСПРАВЛЕНИЕ: _session_meta загружается ДО _build_ui().
  • _detect_current_date() защищён через getattr.
  • УЛУЧШЕНО: протокол (.docx) рендерится через docx_to_html
    с сохранением форматирования.
  • УБРАНА всплывающая подсказка с таб-виджета.
  • НОВОЕ: вкладка «Записи за день» умеет показывать либо
    summary, либо протокол. Выбор сохраняется в настройках
    (app.scrum_day_view_mode).
  • ИСПРАВЛЕНО: рендеринг summary во вкладке «Записи за день»
    (было — BB-код, стало — Markdown через QTextDocument.setMarkdown).
  • НОВОЕ: сортировка таблиц «Поручения» и «Задачи TFS» по
    колонке «Исполнитель». Управляется настройкой
    app.scrum_sort_by_assignee (по умолчанию включена).
    Настройка читается один раз при открытии окна и
    прокидывается в TasksEditorWidget и TfsPanelWidget.
  • УДАЛЕНО: кнопка «Анализ поручений и TFS…» и встроенная
    панель результата анализа (AnalysisResultWidget).
    Модули analysis_prompt_dialog, analysis_result_widget,
    analysis_worker_dialog удалены. Настройка
    app.scrum_analysis_collapsed_by_default больше не
    используется. Логика работы с LLM (литellm_client)
    сохранена для суммаризации и извлечения поручений в
    processor.py.
"""
from __future__ import annotations

import html
import json
import os
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import (
    QFont, QKeySequence, QShortcut,
)
from PySide6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QMessageBox,
    QPlainTextEdit, QPushButton, QSizePolicy, QSplitter,
    QTabWidget, QTextBrowser, QVBoxLayout, QWidget,
)

from ..bbcode_editor import bbcode_to_html
from ..docx_to_html import docx_to_html
from ..file_readers import read_any_text, read_json_file
from ..logger import get_logger
from .tasks_editor_widget import TasksEditorWidget
from .tfs_panel_widget import TfsPanelWidget
from .tooltips import (
    attach_tooltip, make_info_icon, with_info,
)

log = get_logger(__name__)


# Кандидаты протоколов в папке сессии (порядок важен).
_PROTOCOL_CANDIDATES: List[str] = [
    "manual_protocol.docx",
    "manual_protocol.md",
    "manual_protocol.txt",
    "manual_protocol.pdf",
    "protocol.docx",
    "protocol.md",
    "protocol.txt",
    "protocol.pdf",
]


# Режимы отображения вкладки «Записи за день».
VIEW_MODE_SUMMARY = "summary"
VIEW_MODE_PROTOCOL = "protocol"
_VIEW_MODES = (VIEW_MODE_SUMMARY, VIEW_MODE_PROTOCOL)


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------
def _find_protocol_path(session_dir: str) -> str:
    """
    Ищет протокол в папке записи.

    Порядок: manual_protocol.* → protocol.* (в корне и в
    attachments/).
    """
    if not session_dir or not os.path.isdir(session_dir):
        return ""

    for name in _PROTOCOL_CANDIDATES:
        p = os.path.join(session_dir, name)
        if os.path.isfile(p):
            return p

    att_dir = os.path.join(session_dir, "attachments")
    if os.path.isdir(att_dir):
        for name in _PROTOCOL_CANDIDATES:
            p = os.path.join(att_dir, name)
            if os.path.isfile(p):
                return p

    return ""


def _protocol_ext(path: str) -> str:
    return os.path.splitext(path)[1].lower()


def _is_markdown(path: str) -> bool:
    return _protocol_ext(path) in (".md", ".markdown")


def _is_docx(path: str) -> bool:
    return _protocol_ext(path) == ".docx"


def _plain_text_html(path: str, max_chars: int) -> str:
    """
    Рендерит .txt/.pdf/... как HTML с <pre> и моно-шрифтом.
    """
    try:
        text = read_any_text(path, max_chars)
    except Exception as exc:
        log.warning("Не удалось прочитать %s: %s", path, exc)
        text = ""

    if not text:
        return (
            "<p style='color:#c62828; padding:20px'>"
            "Не удалось прочитать содержимое файла.</p>"
        )

    return (
        "<pre style='white-space:pre-wrap; "
        "font-family:Consolas, \"Courier New\", monospace; "
        "font-size:10pt; padding:8px;'>"
        + html.escape(text)
        + "</pre>"
    )


def _markdown_to_html_fragment(md_text: str) -> str:
    """
    Рендерит Markdown в HTML-фрагмент (без обёртки).
    """
    if not md_text:
        return ""

    try:
        from PySide6.QtGui import QTextDocument
        doc = QTextDocument()
        doc.setMarkdown(md_text)
        full_html = doc.toHtml()
    except Exception as exc:
        log.warning("Не удалось отрендерить Markdown: %s", exc)
        return (
            "<pre style='white-space:pre-wrap; "
            "font-family:Consolas, monospace; font-size:10pt;'>"
            + html.escape(md_text)
            + "</pre>"
        )

    body_start = full_html.lower().find("<body")
    if body_start >= 0:
        gt = full_html.find(">", body_start)
        body_end = full_html.lower().rfind("</body>")
        if gt >= 0 and body_end > gt:
            return full_html[gt + 1:body_end]
    return full_html


def _render_protocol_to_html_fragment(
    path: str, max_chars: int = 500_000,
) -> str:
    """
    Универсальный рендер протокола в HTML-фрагмент.
    """
    if not path or not os.path.isfile(path):
        return ""

    ext = _protocol_ext(path)

    if ext == ".docx":
        return docx_to_html(path)

    if ext in (".md", ".markdown"):
        try:
            with open(
                path, "r", encoding="utf-8", errors="replace"
            ) as f:
                md_text = f.read(max_chars)
        except Exception as exc:
            log.warning(
                "Не удалось прочитать MD %s: %s", path, exc
            )
            md_text = ""
        return _markdown_to_html_fragment(md_text)

    return _plain_text_html(path, max_chars)


def _looks_like_markdown(text: str) -> bool:
    """
    Эвристика: похоже ли содержимое на Markdown.
    """
    if not text:
        return False

    import re as _re

    patterns = [
        r"(?m)^#{1,6}\s+\S",
        r"(?m)^\s*[-*+]\s+\S",
        r"(?m)^\s*\d+\.\s+\S",
        r"\*\*[^*\n]+\*\*",
        r"(?<!\*)\*[^*\n]+\*(?!\*)",
        r"(?m)^>\s+\S",
        r"```",
    ]
    for pat in patterns:
        if _re.search(pat, text):
            return True
    return False


def _render_summary_html_fragment(summary_text: str) -> str:
    """
    Рендерит summary в HTML-фрагмент.

    Логика:
      1. Если текст похож на Markdown — рендерим через
         QTextDocument.setMarkdown().
      2. Иначе — через bbcode_to_html() (для старых записей).
      3. Fallback — <pre> с исходным текстом.
    """
    if not summary_text or not summary_text.strip():
        return ""

    text = summary_text.strip()

    if _looks_like_markdown(text):
        try:
            fragment = _markdown_to_html_fragment(text)
            if fragment:
                return fragment
        except Exception as exc:
            log.warning(
                "Не удалось отрендерить summary как Markdown: %s",
                exc,
            )

    try:
        fragment = bbcode_to_html(text)
        if fragment:
            return fragment
    except Exception as exc:
        log.warning(
            "Не удалось отрендерить summary как BB-код: %s", exc
        )

    return (
        "<pre style='white-space:pre-wrap; "
        "font-family:Consolas, monospace; font-size:10pt;'>"
        + html.escape(text)
        + "</pre>"
    )


def _read_summary_bb(session_dir: str) -> str:
    """Читает summary_bb из session.json (если есть)."""
    path = os.path.join(session_dir, "session.json")
    data = read_json_file(path) or {}
    return str(data.get("summary_bb") or "").strip()


# ---------------------------------------------------------------------------
# Виджет просмотра протокола
# ---------------------------------------------------------------------------
class _ProtocolView(QWidget):
    """
    Универсальный просмотрщик одного протокола.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        self.header_label = QLabel("")
        self.header_label.setWordWrap(True)
        self.header_label.setStyleSheet(
            "QLabel { color: #444; font-weight: bold; "
            "padding: 4px; }"
        )
        layout.addWidget(self.header_label)

        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(True)
        self.browser.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self.browser.setLineWrapMode(
            QTextBrowser.LineWrapMode.WidgetWidth
        )
        layout.addWidget(self.browser, 1)

    def show_empty(self, reason: str = "Протокол не найден") -> None:
        """Показать сообщение вместо протокола."""
        self.header_label.setText("")
        self.browser.setHtml(
            "<p style='color:#c62828; padding:20px; "
            "font-size:12pt;'>"
            + html.escape(reason)
            + "</p>"
        )

    def show_protocol(
        self, path: str, max_chars: int = 500_000,
    ) -> None:
        """Показать протокол из файла с форматированием."""
        if not path or not os.path.isfile(path):
            self.show_empty("Файл протокола не найден")
            return

        fname = os.path.basename(path)
        try:
            size_kb = os.path.getsize(path) / 1024
        except OSError:
            size_kb = 0

        self.header_label.setText(
            f"<code>{html.escape(fname)}</code> "
            f"<span style='color:#888'>({size_kb:.1f} КБ)</span>"
        )

        fragment = _render_protocol_to_html_fragment(path, max_chars)

        full_html = (
            "<html><head>"
            "<style>"
            "body { font-family: sans-serif; font-size: 11pt; "
            "color: #1f1f1f; }"
            "</style>"
            "</head><body>"
            + (fragment or
               "<p style='color:#888'>Документ пустой.</p>")
            + "</body></html>"
        )
        self.browser.setHtml(full_html)

        try:
            cursor = self.browser.textCursor()
            cursor.movePosition(cursor.MoveOperation.Start)
            self.browser.setTextCursor(cursor)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Вкладка «Протокол выбранной записи»
# ---------------------------------------------------------------------------
class _SelectedProtocolTab(QWidget):
    """Вкладка «Протокол» — показывает протокол выбранной записи."""

    def __init__(
        self,
        session_dir: str,
        max_chars: int = 500_000,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._max_chars = int(max_chars)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        self.view = _ProtocolView(self)
        layout.addWidget(self.view, 1)

        self.set_session(session_dir)

    def set_session(self, session_dir: str) -> None:
        """Обновляет содержимое — показывает протокол из папки."""
        path = _find_protocol_path(session_dir)
        if not path:
            self.view.show_empty(
                "К записи не прикреплён протокол.\n\n"
                "Прикрепите файл через меню «Протокол → "
                "Прикрепить файл протокола…» или через "
                "редактор Markdown."
            )
            return
        self.view.show_protocol(path, self._max_chars)


# ---------------------------------------------------------------------------
# Вкладка «Записи за день»
# ---------------------------------------------------------------------------
class _DayRecordsTab(QWidget):
    """
    Вкладка «Записи за день» — все записи за тот же день,
    кроме выбранной.
    """

    def __init__(
        self,
        *,
        all_rows_provider,
        excluded_session_dir: str,
        current_date: str,
        max_records: int = 50,
        max_chars: int = 500_000,
        initial_view_mode: str = VIEW_MODE_SUMMARY,
        on_view_mode_changed=None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._all_rows_provider = all_rows_provider
        self._excluded_dir = os.path.abspath(
            excluded_session_dir or ""
        )
        self._current_date = (current_date or "").strip()
        self._max_records = int(max_records)
        self._max_chars = int(max_chars)
        self._on_view_mode_changed = on_view_mode_changed

        if initial_view_mode not in _VIEW_MODES:
            initial_view_mode = VIEW_MODE_SUMMARY
        self._view_mode = initial_view_mode

        self._suppress_combo_signal = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # --- Панель управления: переключатель режима ---
        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(6)

        controls.addWidget(QLabel("Показывать:"))

        self.view_mode_combo = QComboBox()
        self.view_mode_combo.addItem(
            "Краткое описание (summary)", VIEW_MODE_SUMMARY
        )
        self.view_mode_combo.addItem(
            "Протокол", VIEW_MODE_PROTOCOL
        )
        self.view_mode_combo.setMinimumWidth(220)

        self._suppress_combo_signal = True
        idx = self.view_mode_combo.findData(self._view_mode)
        if idx >= 0:
            self.view_mode_combo.setCurrentIndex(idx)
        self._suppress_combo_signal = False

        self.view_mode_combo.currentIndexChanged.connect(
            self._on_view_mode_index_changed
        )

        attach_tooltip(
            self.view_mode_combo, "scrum_day_view_combo"
        )
        controls.addWidget(self.view_mode_combo)

        icon = make_info_icon("scrum_day_view_mode")
        if icon is not None:
            controls.addWidget(icon)

        controls.addStretch()
        layout.addLayout(controls)

        # --- Информационная строка ---
        self.info_label = QLabel("")
        self.info_label.setWordWrap(True)
        self.info_label.setStyleSheet(
            "QLabel { color: #666; padding: 4px; }"
        )
        layout.addWidget(self.info_label)

        # --- QTextBrowser для вывода всех записей ---
        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(True)
        self.browser.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self.browser.setLineWrapMode(
            QTextBrowser.LineWrapMode.WidgetWidth
        )
        layout.addWidget(self.browser, 1)

        self.refresh()

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------
    def set_current_date(self, date_str: str) -> None:
        """Меняет дату и перерисовывает содержимое."""
        self._current_date = (date_str or "").strip()
        self.refresh()

    def set_view_mode(self, mode: str) -> None:
        """Программно меняет режим отображения."""
        if mode not in _VIEW_MODES:
            mode = VIEW_MODE_SUMMARY
        if mode == self._view_mode:
            return

        self._view_mode = mode
        self._suppress_combo_signal = True
        idx = self.view_mode_combo.findData(mode)
        if idx >= 0:
            self.view_mode_combo.setCurrentIndex(idx)
        self._suppress_combo_signal = False

        self.refresh()

    def view_mode(self) -> str:
        return self._view_mode

    # ------------------------------------------------------------------
    # Обработчики
    # ------------------------------------------------------------------
    def _on_view_mode_index_changed(self, _index: int) -> None:
        if self._suppress_combo_signal:
            return

        new_mode = self.view_mode_combo.currentData() or ""
        if new_mode not in _VIEW_MODES:
            return
        if new_mode == self._view_mode:
            return

        self._view_mode = new_mode
        log.info(
            "ScrumWindow: режим вкладки «Записи за день» — %r",
            new_mode,
        )

        if self._on_view_mode_changed is not None:
            try:
                self._on_view_mode_changed(new_mode)
            except Exception as exc:
                log.warning(
                    "Не удалось сохранить режим отображения: %s",
                    exc,
                )

        self.refresh()

    # ------------------------------------------------------------------
    # Отрисовка
    # ------------------------------------------------------------------
    def refresh(self) -> None:
        """Перечитывает список записей и обновляет отображение."""
        if not self._current_date:
            self.info_label.setText(
                "<span style='color:#c62828'>"
                "Не удалось определить дату записи.</span>"
            )
            self.browser.clear()
            return

        rows = self._all_rows_provider() or []
        same_day: List[Dict[str, Any]] = []

        for r in rows:
            r_dir = os.path.abspath(r.get("dir") or "")
            if r_dir == self._excluded_dir:
                continue

            dt = (r.get("datetime") or "").strip()
            r_date = dt.split(" ", 1)[0] if dt else ""
            if r_date != self._current_date:
                continue

            same_day.append(r)

        same_day.sort(key=lambda r: r.get("datetime") or "")

        if self._max_records > 0:
            same_day = same_day[: self._max_records]

        if not same_day:
            self.info_label.setText(
                f"<span style='color:#666'>"
                f"За {html.escape(self._current_date)} "
                f"больше нет записей.</span>"
            )
            self.browser.setHtml(
                "<p style='color:#888; padding:20px; "
                "font-size:12pt;'>"
                "В этот день других записей нет.</p>"
            )
            return

        mode = self._view_mode

        with_content = 0
        without_content = 0
        parts: List[str] = []

        for idx, r in enumerate(same_day, start=1):
            name = r.get("name") or "(без названия)"
            dt_full = (r.get("datetime") or "").strip()
            dt_parts = dt_full.split(" ", 1)
            time_str = dt_parts[1] if len(dt_parts) > 1 else ""
            project = r.get("project") or "—"
            session_dir = r.get("dir") or ""

            if mode == VIEW_MODE_SUMMARY:
                summary_bb = _read_summary_bb(session_dir)
                has_content = bool(summary_bb)
                content_label = "Краткое описание"
                content_fragment = (
                    _render_summary_html_fragment(summary_bb)
                    if has_content else ""
                )
                empty_note = (
                    "У записи нет краткого описания (summary_bb "
                    "не заполнено)."
                )
            else:
                proto_path = _find_protocol_path(session_dir)
                has_content = bool(proto_path)
                content_label = "Протокол"
                content_fragment = (
                    _render_protocol_to_html_fragment(
                        proto_path, self._max_chars
                    )
                    if has_content else ""
                )
                empty_note = "К записи не прикреплён протокол."

            if has_content:
                with_content += 1
            else:
                without_content += 1

            parts.append(
                "<div style='margin-top:24px; "
                "border-top:2px solid #ccc; padding-top:12px;'>"
            )
            parts.append(
                f"<h2 style='margin:0 0 4px 0; color:#1565C0;'>"
                f"{idx}. {html.escape(name)}</h2>"
            )
            meta_line = (
                f"<span style='color:#666; font-size:10pt;'>"
                f"Время: {html.escape(time_str or '—')} · "
                f"Проект: {html.escape(project)} · "
                f"<b>{html.escape(content_label)}:</b> "
                f"{'есть' if has_content else 'нет'}"
                f"</span>"
            )
            parts.append(f"<p>{meta_line}</p>")

            if not has_content:
                parts.append(
                    "<p style='color:#c62828; "
                    "font-style:italic;'>"
                    f"{html.escape(empty_note)}</p>"
                )
                parts.append("</div>")
                continue

            if content_fragment:
                parts.append(content_fragment)
            else:
                parts.append(
                    "<p style='color:#c62828'>"
                    "Не удалось прочитать содержимое.</p>"
                )

            parts.append("</div>")

        mode_label = (
            "краткие описания"
            if mode == VIEW_MODE_SUMMARY
            else "протоколы"
        )
        self.info_label.setText(
            f"<span style='color:#444'>"
            f"<b>Записей за {html.escape(self._current_date)}:</b> "
            f"{len(same_day)} · "
            f"с содержимым ({mode_label}): {with_content} · "
            f"без содержимого: {without_content}"
            f"</span>"
        )

        html_doc = (
            "<html><head>"
            "<style>"
            "body { font-family: sans-serif; font-size: 11pt; "
            "color: #1f1f1f; line-height: 1.4; }"
            "h1 { font-size: 18pt; color: #0d47a1; "
            "margin: 18px 0 8px 0; }"
            "h2 { font-size: 15pt; color: #1565c0; "
            "margin: 16px 0 6px 0; }"
            "h3 { font-size: 13pt; color: #1976d2; "
            "margin: 14px 0 6px 0; }"
            "h4 { font-size: 12pt; color: #1e88e5; "
            "margin: 12px 0 4px 0; }"
            "p  { margin: 4px 0; }"
            "code, pre {"
            "  font-family: Consolas, \"Courier New\", monospace;"
            "  background: #f4f4f4; border-radius: 3px;"
            "}"
            "code { padding: 1px 4px; }"
            "pre { padding: 8px; white-space: pre-wrap; "
            "font-size: 10pt; }"
            "table { border-collapse: collapse; margin: 8px 0; }"
            "table td, table th {"
            "  border: 1px solid #999; padding: 4px 8px;"
            "  vertical-align: top; font-size: 10pt;"
            "}"
            "table th { background: #f0f0f0; font-weight: bold; }"
            "blockquote {"
            "  border-left: 3px solid #bbb; margin: 6px 0;"
            "  padding: 4px 12px; color: #444;"
            "}"
            "ul, ol { margin: 4px 0 4px 24px; padding: 0; }"
            "li { margin: 2px 0; }"
            "</style>"
            "</head><body>"
            + "".join(parts)
            + "</body></html>"
        )
        self.browser.setHtml(html_doc)

        try:
            cursor = self.browser.textCursor()
            cursor.movePosition(cursor.MoveOperation.Start)
            self.browser.setTextCursor(cursor)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Основное окно
# ---------------------------------------------------------------------------
class ScrumWindow(QDialog):
    """
    Окно «Скрам» — рабочее место для скрам-митинга.
    """

    def __init__(
        self,
        *,
        session_dir: str,
        session_name: str = "",
        all_rows_provider=None,
        employees: Optional[List[Dict[str, str]]] = None,
        config_manager=None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(
            f"Скрам — "
            f"{session_name or os.path.basename(session_dir)}"
        )

        # --- Модальное окно ---
        self.setModal(False)

        # --- Флаги окна ---
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint
        )

        # Под Wayland окно по умолчанию может быть показано без
        # активации. Явно требуем активацию при показе.
        self.setAttribute(
            Qt.WidgetAttribute.WA_ShowWithoutActivating, False
        )

        self._session_dir = session_dir
        self._session_name = session_name
        self._all_rows_provider = all_rows_provider or (lambda: [])
        self._employees: List[Dict[str, str]] = list(employees or [])
        self._config_manager = config_manager

        # --- ВАЖНО: загружаем метаданные ДО _build_ui() ---
        self._session_meta: Dict[str, Any] = self._load_session_meta()

        # Загружаем настройки отображения.
        app_cfg: Dict[str, Any] = {}
        if config_manager is not None:
            try:
                app_cfg = config_manager.get_app_settings()
            except Exception as exc:
                log.warning(
                    "Не удалось прочитать app-настройки: %s", exc
                )

        self._app_cfg = app_cfg

        # --- Сортировка таблиц по исполнителю ---
        # Читаем один раз при открытии окна. Управляет
        # сортировкой и таблицы поручений, и дерева задач TFS.
        # Значение берётся из Настройки → Скрам.
        self._sort_by_assignee = bool(
            self._app_cfg.get("scrum_sort_by_assignee", True)
        )

        self._max_protocol_chars = int(
            app_cfg.get("scrum_protocol_tab_max_chars", 500_000)
        )
        self._max_day_records = int(
            app_cfg.get("scrum_day_tab_max_records", 50)
        )
        self._left_panel_width = int(
            app_cfg.get("scrum_left_panel_width", 700)
        )
        self._tasks_panel_height = int(
            app_cfg.get("scrum_tasks_panel_height", 380)
        )
        self._tfs_max_items = int(
            app_cfg.get("scrum_tfs_max_items", 200)
        )

        # Начальный режим вкладки «Записи за день».
        initial_mode = str(
            app_cfg.get("scrum_day_view_mode", VIEW_MODE_SUMMARY)
            or VIEW_MODE_SUMMARY
        )
        if initial_mode not in _VIEW_MODES:
            initial_mode = VIEW_MODE_SUMMARY
        self._initial_day_view_mode = initial_mode

        # --- Начальный размер окна ---
        from PySide6.QtGui import QGuiApplication

        desired_w = int(app_cfg.get("scrum_window_width", 1920))
        desired_h = int(app_cfg.get("scrum_window_height", 1024))

        screen = QGuiApplication.primaryScreen()
        avail_w = 1920
        avail_h = 1080
        if screen is not None:
            try:
                geo = screen.availableGeometry()
                avail_w = max(800, geo.width())
                avail_h = max(600, geo.height())
            except Exception:
                pass

        _margin = 40
        max_w = max(800, avail_w - _margin)
        max_h = max(600, avail_h - _margin)

        width = min(desired_w, max_w)
        height = min(desired_h, max_h)

        min_w = min(1200, int(avail_w * 0.9))
        min_h = min(700, int(avail_h * 0.9))
        self.setMinimumSize(min_w, min_h)
        self.resize(width, height)

        log.debug(
            "ScrumWindow: начальный размер %dx%d "
            "(доступно %dx%d, min %dx%d)",
            width, height, avail_w, avail_h, min_w, min_h,
        )

        self._build_ui()

        # Применяем метаданные к TFS-панели.
        self._apply_session_meta_to_tfs()

        log.info(
            "ScrumWindow открыто: session=%s, is_scrum=%s, "
            "day_view_mode=%r, sort_by_assignee=%s",
            os.path.basename(session_dir),
            self._session_meta.get("is_scrum"),
            self._initial_day_view_mode,
            self._sort_by_assignee,
        )

    # ------------------------------------------------------------------
    # Показ окна
    # ------------------------------------------------------------------
    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not getattr(self, "_first_show_done", False):
            self._first_show_done = True
            self.showMaximized()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        # Уменьшили внешние отступы и вертикальный зазор.
        root.setContentsMargins(6, 4, 6, 4)
        root.setSpacing(3)

        # --- Вводная плашка ---
        intro_row = QHBoxLayout()
        intro = QLabel(
            "Рабочее место скрам-митинга: поручения, задачи TFS и "
            "протоколы. Слева — поручения записи и задачи из TFS. "
            "Справа — протокол выбранной записи и данные по "
            "остальным записям за тот же день."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("QLabel { color: #666; }")
        intro_row.addWidget(intro, 1)
        icon = make_info_icon("scrum_window_intro")
        if icon is not None:
            intro_row.addWidget(
                icon, 0, Qt.AlignmentFlag.AlignTop
            )
        root.addLayout(intro_row)

        # --- Основной сплиттер (левая / правая часть) ---
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(self.main_splitter, 1)

        # --- Левая часть: поручения + TFS ---
        self.left_splitter = QSplitter(Qt.Orientation.Vertical)
        self.left_splitter.setChildrenCollapsible(False)
        # Компактный зазор между блоками в левой части.
        self.left_splitter.setStyleSheet(
            "QSplitter::handle { background: #ddd; }"
            "QSplitter::handle:vertical { height: 2px; }"
        )

        self.tasks_widget = TasksEditorWidget(
            session_dir=self._session_dir,
            session_name=self._session_name,
            employees=self._employees,
            compact=True,
            sort_by_assignee=self._sort_by_assignee,
            parent=self,
        )
        self.left_splitter.addWidget(self.tasks_widget)

        # --- Панель управления TFS ---
        # Настройка scrum_tfs_panel_collapsed_by_default
        # управляет тем, будет ли верхняя панель блока
        # «Задачи TFS» (проект, шаблон, WIQL, переменные,
        # фильтры) свёрнута при открытии окна «Скрам».
        start_tfs_collapsed = bool(
            self._app_cfg.get(
                "scrum_tfs_panel_collapsed_by_default", True
            )
        )
        self.tfs_widget = TfsPanelWidget(
            config_manager=self._config_manager,
            max_items=self._tfs_max_items,
            sort_by_assignee=self._sort_by_assignee,
            start_controls_collapsed=start_tfs_collapsed,
            parent=self,
        )
        self.left_splitter.addWidget(self.tfs_widget)

        # Пропорции левой части: поручения и TFS делят место
        # примерно поровну.
        self.left_splitter.setSizes([
            max(200, int(self._tasks_panel_height * 1.0)),
            max(200, int(self._tasks_panel_height * 1.0)),
        ])

        self.main_splitter.addWidget(self.left_splitter)

        # --- Правая часть: вкладки ---
        self.tabs = QTabWidget()
        self.tabs.setTabToolTip(
            0, "Протокол выбранной записи (только чтение)"
        )

        # Вкладка «Протокол».
        self.protocol_tab = _SelectedProtocolTab(
            session_dir=self._session_dir,
            max_chars=self._max_protocol_chars,
            parent=self,
        )
        self.tabs.addTab(self.protocol_tab, "Протокол")

        # Вкладка «Записи за день».
        current_date = self._detect_current_date()
        self.day_tab = _DayRecordsTab(
            all_rows_provider=self._all_rows_provider,
            excluded_session_dir=self._session_dir,
            current_date=current_date,
            max_records=self._max_day_records,
            max_chars=self._max_protocol_chars,
            initial_view_mode=self._initial_day_view_mode,
            on_view_mode_changed=self._on_day_view_mode_changed,
            parent=self,
        )
        self.tabs.addTab(self.day_tab, "Записи за день")
        self.tabs.setTabToolTip(
            1,
            "Данные по остальным записям за тот же день "
            "(summary или протокол — по выбору)"
        )

        self.main_splitter.addWidget(self.tabs)

        self.main_splitter.setSizes(
            [
                self._left_panel_width,
                max(600, self._left_panel_width),
            ]
        )

        # --- Нижние кнопки ---
        bottom = QHBoxLayout()

        bottom.addStretch()

        self.maximize_btn = QPushButton("Развернуть")
        self.maximize_btn.setToolTip(
            "Развернуть окно на весь экран (F11).\n\n"
            "Повторное нажатие возвращает обычный размер."
        )
        self.maximize_btn.clicked.connect(self._toggle_fullscreen)
        bottom.addWidget(self.maximize_btn)

        self.refresh_btn = QPushButton("Обновить")
        self.refresh_btn.setToolTip(
            "Обновить поручения, TFS и вкладки с протоколами."
        )
        self.refresh_btn.clicked.connect(self._on_refresh)
        bottom.addWidget(self.refresh_btn)

        self.close_btn = QPushButton("Закрыть")
        self.close_btn.setToolTip("Закрыть окно (Esc).")
        self.close_btn.clicked.connect(self.close)
        bottom.addWidget(self.close_btn)

        root.addLayout(bottom)

        # --- Горячие клавиши ---
        QShortcut(
            QKeySequence("Escape"), self
        ).activated.connect(self.close)
        QShortcut(
            QKeySequence("F5"), self
        ).activated.connect(self._on_refresh)
        QShortcut(
            QKeySequence("F11"), self
        ).activated.connect(self._toggle_fullscreen)
        QShortcut(
            QKeySequence("Ctrl+M"), self
        ).activated.connect(self._toggle_maximized)

    # ------------------------------------------------------------------
    # Вспомогательные
    # ------------------------------------------------------------------
    def _load_session_meta(self) -> Dict[str, Any]:
        """Читает session.json из папки записи."""
        path = os.path.join(self._session_dir, "session.json")
        data = read_json_file(path)
        return data or {}

    def _detect_current_date(self) -> str:
        """Определяет дату выбранной записи (YYYY-MM-DD)."""
        meta = getattr(self, "_session_meta", None)
        if not isinstance(meta, dict):
            meta = self._load_session_meta()

        date_str = str(meta.get("date") or "").strip()
        if date_str:
            return date_str

        base = os.path.basename(
            (self._session_dir or "").rstrip("/")
        )
        parts = base.split("_")
        if parts and len(parts[0]) == 10:
            return parts[0]
        return ""

    def _apply_session_meta_to_tfs(self) -> None:
        """Передаёт в TFS-панель проект и шаблон из метаданных."""
        if not hasattr(self, "tfs_widget"):
            return

        meta = self._session_meta or {}
        project = str(meta.get("tfs_project") or "").strip()
        template = str(
            meta.get("tfs_template_name") or ""
        ).strip()
        variables = meta.get("tfs_variables") or {}
        if not isinstance(variables, dict):
            variables = {}

        try:
            self.tfs_widget.apply_session_defaults(
                project=project,
                template=template,
                variables=variables,
            )
        except Exception as exc:
            log.warning(
                "Не удалось применить TFS-настройки из "
                "метаданных: %s", exc,
            )

    # ------------------------------------------------------------------
    # Управление размером окна
    # ------------------------------------------------------------------
    def _toggle_maximized(self) -> None:
        """Разворот/восстановление окна."""
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def _toggle_fullscreen(self) -> None:
        """Полноэкранный режим (без рамок)."""
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    # ------------------------------------------------------------------
    # Обработка смены режима отображения
    # ------------------------------------------------------------------
    def _on_day_view_mode_changed(self, mode: str) -> None:
        """
        Сохраняет режим вкладки «Записи за день» в конфиг.
        """
        if self._config_manager is None:
            return

        try:
            cfg = self._config_manager.config
            app_cfg = cfg.setdefault("app", {})
            app_cfg["scrum_day_view_mode"] = str(mode)
            self._config_manager.save()
            log.info(
                "Сохранён режим отображения вкладки "
                "«Записи за день»: %r", mode,
            )
        except Exception as exc:
            log.warning(
                "Не удалось сохранить режим отображения вкладки "
                "«Записи за день»: %s", exc,
            )

    # ------------------------------------------------------------------
    # Действия
    # ------------------------------------------------------------------
    def _on_refresh(self) -> None:
        """Перезагружает данные во всех блоках."""
        log.info("ScrumWindow: обновление данных")

        self._session_meta = self._load_session_meta()

        try:
            self.tasks_widget.reload()
        except Exception as exc:
            log.warning(
                "Не удалось перезагрузить поручения: %s", exc
            )

        try:
            self.tfs_widget.reload_settings()
            self._apply_session_meta_to_tfs()
        except Exception as exc:
            log.warning(
                "Не удалось перезагрузить настройки TFS: %s", exc
            )

        try:
            self.protocol_tab.set_session(self._session_dir)
        except Exception as exc:
            log.warning(
                "Не удалось перезагрузить протокол: %s", exc
            )

        try:
            self.day_tab.set_current_date(
                self._detect_current_date()
            )
        except Exception as exc:
            log.warning(
                "Не удалось перезагрузить записи за день: %s", exc
            )

    # ------------------------------------------------------------------
    # События
    # ------------------------------------------------------------------
    def changeEvent(self, event) -> None:
        """Обновляет текст кнопки «Развернуть»."""
        try:
            from PySide6.QtCore import QEvent
            if event.type() == QEvent.Type.WindowStateChange:
                if hasattr(self, "maximize_btn"):
                    if self.isFullScreen() or self.isMaximized():
                        self.maximize_btn.setText("Свернуть")
                    else:
                        self.maximize_btn.setText("Развернуть")
        except Exception:
            pass
        super().changeEvent(event)

    def closeEvent(self, event) -> None:
        """Аккуратно останавливает воркеры дочерних панелей."""
        try:
            if hasattr(self, "tfs_widget"):
                self.tfs_widget.closeEvent(event)
        except Exception:
            pass
        super().closeEvent(event)