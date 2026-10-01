"""Просмотрщик DOCX-файлов (только чтение) с поиском.

Отличия от MarkdownViewerDialog:
  • работает с .docx через python-docx;
  • read-only, без редактирования;
  • встроенный поиск с подсветкой всех совпадений;
  • опционально — кнопка «Сохранить как…» (копия файла);
  • опционально — кнопка «Открыть внешним приложением».

Использование:
    dlg = DocxViewerDialog(
        docx_path="/path/to/manual_protocol.docx",
        title="Протокол — Запись 2026-10-01",
        parent=self,
    )
    dlg.exec()

Изменения:
  • Модуль создан.
"""
from __future__ import annotations

import os
import shutil
from typing import List, Optional, Tuple

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import (
    QDesktopServices, QFont, QKeySequence, QShortcut,
    QTextCharFormat, QTextCursor, QColor,
)
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
    QVBoxLayout, QWidget,
)

from ..file_readers import read_docx_file
from ..logger import get_logger
from ..utils import safe_local_path
from .tooltips import attach_tooltip, make_info_icon

log = get_logger(__name__)


# Максимальный размер файла для отображения во встроенном
# просмотрщике.
MAX_DISPLAY_BYTES = 20 * 1024 * 1024  # 20 МБ

# Цвет подсветки совпадений.
_HIGHLIGHT_COLOR = QColor("#FFEB3B")
_HIGHLIGHT_ALPHA = 140


class DocxViewerDialog(QDialog):
    """
    Модальный read-only просмотрщик .docx с поиском.

    Возможности:
      • извлекает текст из .docx через python-docx;
      • моноширинный шрифт;
      • поиск по тексту (Ctrl+F) с подсветкой всех совпадений;
      • переход между совпадениями (F3 / Shift+F3);
      • сохранение копии исходного .docx через «Сохранить как…»;
      • открытие исходного .docx системным приложением.
    """

    def __init__(
        self,
        docx_path: str = "",
        title: str = "Просмотр протокола",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumSize(1000, 700)

        self._docx_path = docx_path or ""
        self._text = ""

        # Список позиций совпадений (начало, конец).
        self._matches: List[Tuple[int, int]] = []
        self._current_match: int = -1

        self._build_ui()
        self._load_docx()
        self._update_stats()
        self._refresh_action_buttons()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(6)

        # --- Вводная плашка ---
        intro_row = QHBoxLayout()
        intro = QLabel(
            "Просмотр протокола (.docx) в режиме «только чтение». "
            "Чтобы изменить — сохраните копию и откройте её во "
            "внешнем редакторе (или используйте пункт меню "
            "«Протокол → Создать/редактировать протокол (Markdown)»)."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("QLabel { color: #666; }")
        intro_row.addWidget(intro, 1)
        icon = make_info_icon("docx_viewer_intro")
        if icon is not None:
            intro_row.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(intro_row)

        # --- Панель поиска ---
        root.addWidget(self._build_search_bar())

        # --- Текст ---
        self.editor = QPlainTextEdit()
        mono = QFont("Monospace")
        mono.setStyleHint(QFont.StyleHint.TypeWriter)
        mono.setPointSize(11)
        self.editor.setFont(mono)
        self.editor.setReadOnly(True)
        self.editor.setLineWrapMode(
            QPlainTextEdit.LineWrapMode.WidgetWidth
        )
        self.editor.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        root.addWidget(self.editor, 1)

        # --- Нижняя панель ---
        bottom = QHBoxLayout()

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("QLabel { color: #666; }")
        bottom.addWidget(self.status_label)

        bottom.addStretch()

        self.copy_all_btn = QPushButton("Скопировать всё")
        self.copy_all_btn.setToolTip(
            "Скопировать извлечённый текст в буфер обмена."
        )
        self.copy_all_btn.clicked.connect(self._copy_all)
        bottom.addWidget(self.copy_all_btn)

        self.save_as_btn = QPushButton("Сохранить копию .docx…")
        self.save_as_btn.setToolTip(
            "Сохранить копию исходного .docx-файла в любое место."
        )
        self.save_as_btn.clicked.connect(self._save_copy)
        bottom.addWidget(self.save_as_btn)

        self.open_external_btn = QPushButton("Открыть внешне")
        self.open_external_btn.setToolTip(
            "Открыть исходный .docx системным приложением "
            "(LibreOffice Writer, MS Word и т.п.)."
        )
        self.open_external_btn.clicked.connect(self._open_external)
        bottom.addWidget(self.open_external_btn)

        self.close_btn = QPushButton("Закрыть")
        self.close_btn.setToolTip("Закрыть окно (Esc).")
        self.close_btn.clicked.connect(self.accept)
        bottom.addWidget(self.close_btn)

        root.addLayout(bottom)

        # --- Горячие клавиши ---
        QShortcut(QKeySequence("Ctrl+F"), self).activated.connect(
            self._focus_search
        )
        QShortcut(QKeySequence("Escape"), self).activated.connect(
            self.accept
        )
        QShortcut(QKeySequence("F3"), self).activated.connect(
            self._find_next
        )
        QShortcut(
            QKeySequence("Shift+F3"), self
        ).activated.connect(self._find_prev)

    def _build_search_bar(self) -> QWidget:
        box = QWidget()
        layout = QHBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        layout.addWidget(QLabel("Поиск:"))

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(
            "Введите текст для поиска (Ctrl+F)…"
        )
        self.search_input.textChanged.connect(
            self._on_search_text_changed
        )
        self.search_input.returnPressed.connect(self._find_next)
        attach_tooltip(self.search_input, "docx_viewer_search")
        layout.addWidget(self.search_input, 1)

        self.find_prev_btn = QPushButton("◀")
        self.find_prev_btn.setFixedWidth(36)
        self.find_prev_btn.setToolTip("Предыдущее совпадение (Shift+F3).")
        self.find_prev_btn.clicked.connect(self._find_prev)
        layout.addWidget(self.find_prev_btn)

        self.find_next_btn = QPushButton("▶")
        self.find_next_btn.setFixedWidth(36)
        self.find_next_btn.setToolTip("Следующее совпадение (F3, Enter).")
        self.find_next_btn.clicked.connect(self._find_next)
        layout.addWidget(self.find_next_btn)

        self.clear_search_btn = QPushButton("Сбросить")
        self.clear_search_btn.setToolTip(
            "Очистить поле поиска и снять подсветку."
        )
        self.clear_search_btn.clicked.connect(self._clear_search)
        layout.addWidget(self.clear_search_btn)

        self.matches_label = QLabel("")
        self.matches_label.setStyleSheet(
            "QLabel { color: #444; font-weight: bold; }"
        )
        self.matches_label.setMinimumWidth(140)
        self.matches_label.setAlignment(
            Qt.AlignmentFlag.AlignRight
            | Qt.AlignmentFlag.AlignVCenter
        )
        layout.addWidget(self.matches_label)

        return box

    # ------------------------------------------------------------------
    # Загрузка DOCX
    # ------------------------------------------------------------------
    def _load_docx(self) -> None:
        if not self._docx_path:
            self._text = ""
            self.editor.setPlainText(
                "Путь к файлу не задан."
            )
            return

        if not os.path.isfile(self._docx_path):
            self._text = ""
            self.editor.setPlainText(
                f"Файл не найден:\n{self._docx_path}"
            )
            return

        try:
            self._text = read_docx_file(self._docx_path) or ""
        except Exception as exc:
            log.exception(
                "Не удалось прочитать DOCX %s: %s",
                self._docx_path, exc,
            )
            self._text = ""
            self.editor.setPlainText(
                f"Не удалось прочитать файл:\n{exc}"
            )
            return

        if not self._text.strip():
            self.editor.setPlainText(
                "(Документ пустой или текст не извлекается.)"
            )
        else:
            self.editor.setPlainText(self._text)

        cursor = self.editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        self.editor.setTextCursor(cursor)

    def _update_stats(self) -> None:
        chars = len(self._text)
        lines = self._text.count("\n") + 1 if self._text else 0
        size_bytes = len(self._text.encode("utf-8"))

        parts: List[str] = []
        if self._docx_path and os.path.isfile(self._docx_path):
            try:
                file_size = os.path.getsize(self._docx_path)
                parts.append(
                    f"{os.path.basename(self._docx_path)} "
                    f"({file_size / 1024:.1f} КБ)"
                )
            except OSError:
                parts.append(os.path.basename(self._docx_path))
        parts.append(f"{lines} строк")
        parts.append(f"{chars} символов")
        parts.append(f"текста {size_bytes / 1024:.1f} КБ")

        self.status_label.setText(" · ".join(parts))

    def _refresh_action_buttons(self) -> None:
        has_text = bool(self._text.strip())
        has_file = bool(
            self._docx_path and os.path.isfile(self._docx_path)
        )
        self.copy_all_btn.setEnabled(has_text)
        self.save_as_btn.setEnabled(has_file)
        self.open_external_btn.setEnabled(has_file)

    # ------------------------------------------------------------------
    # Поиск
    # ------------------------------------------------------------------
    def _focus_search(self) -> None:
        self.search_input.setFocus()
        self.search_input.selectAll()

    def _on_search_text_changed(self, _text: str) -> None:
        self._rebuild_matches()

    def _rebuild_matches(self) -> None:
        query = self.search_input.text()
        self._matches.clear()
        self._current_match = -1

        if not query:
            self.editor.setExtraSelections([])
            self.matches_label.setText("")
            return

        text = self._text
        q = query.lower()
        t = text.lower()

        start = 0
        while True:
            idx = t.find(q, start)
            if idx < 0:
                break
            self._matches.append((idx, idx + len(query)))
            start = idx + max(1, len(query))

        self._apply_highlights()

        total = len(self._matches)
        if total == 0:
            self.matches_label.setText("Ничего не найдено")
        else:
            self.matches_label.setText(f"Найдено: {total}")
            self._current_match = -1
            self._find_next()

    def _apply_highlights(self) -> None:
        selections = []

        fmt = QTextCharFormat()
        fmt.setBackground(_HIGHLIGHT_COLOR)
        fmt.setForeground(QColor("#000000"))

        cur = self._current_match
        for i, (start, end) in enumerate(self._matches):
            cursor = QTextCursor(self.editor.document())
            cursor.setPosition(start)
            cursor.setPosition(
                end, QTextCursor.MoveMode.KeepAnchor
            )
            sel = QPlainTextEdit.ExtraSelection()
            sel.cursor = cursor
            if i == cur:
                cur_fmt = QTextCharFormat()
                cur_fmt.setBackground(QColor("#FF9800"))
                cur_fmt.setForeground(QColor("#000000"))
                sel.format = cur_fmt
            else:
                sel.format = fmt
            selections.append(sel)

        self.editor.setExtraSelections(selections)

    def _find_next(self) -> None:
        if not self._matches:
            return
        self._current_match = (
            self._current_match + 1
        ) % len(self._matches)
        self._scroll_to_current()

    def _find_prev(self) -> None:
        if not self._matches:
            return
        self._current_match = (
            self._current_match - 1
        ) % len(self._matches)
        self._scroll_to_current()

    def _scroll_to_current(self) -> None:
        if not self._matches:
            return
        if self._current_match < 0:
            self._current_match = 0
        start, end = self._matches[self._current_match]

        cursor = QTextCursor(self.editor.document())
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        self.editor.setTextCursor(cursor)
        self.editor.ensureCursorVisible()

        self._apply_highlights()

        total = len(self._matches)
        self.matches_label.setText(
            f"{self._current_match + 1} из {total}"
        )

    def _clear_search(self) -> None:
        self.search_input.clear()
        self._matches.clear()
        self._current_match = -1
        self.editor.setExtraSelections([])
        self.matches_label.setText("")

    # ------------------------------------------------------------------
    # Действия
    # ------------------------------------------------------------------
    def _copy_all(self) -> None:
        if not self._text:
            return
        QApplication.clipboard().setText(self._text)
        log.info(
            "DocxViewerDialog: текст скопирован в буфер (%d символов)",
            len(self._text),
        )
        QMessageBox.information(
            self, "Копирование",
            f"Скопировано {len(self._text)} символов в буфер обмена.",
        )

    def _save_copy(self) -> None:
        if not self._docx_path or not os.path.isfile(self._docx_path):
            QMessageBox.warning(
                self, "Сохранение",
                "Исходный .docx-файл не найден.",
            )
            return

        base_name = os.path.basename(self._docx_path)
        target, _ = QFileDialog.getSaveFileName(
            self,
            "Сохранить копию протокола",
            os.path.join(os.path.expanduser("~"), base_name),
            "Документы Word (*.docx);;Все файлы (*)",
        )
        if not target:
            return
        target = safe_local_path(target)

        if not target.lower().endswith(".docx"):
            target += ".docx"

        try:
            shutil.copy2(self._docx_path, target)
        except Exception as exc:
            log.exception(
                "DocxViewerDialog: ошибка сохранения копии: %s", exc
            )
            QMessageBox.critical(
                self, "Сохранение",
                f"Не удалось сохранить копию:\n{exc}",
            )
            return

        log.info(
            "DocxViewerDialog: копия сохранена в %s", target
        )
        QMessageBox.information(
            self, "Сохранение",
            f"Копия сохранена:\n{target}",
        )

    def _open_external(self) -> None:
        if not self._docx_path or not os.path.isfile(self._docx_path):
            QMessageBox.warning(
                self, "Открытие",
                "Исходный .docx-файл не найден.",
            )
            return
        log.info(
            "DocxViewerDialog: открытие внешним приложением: %s",
            self._docx_path,
        )
        QDesktopServices.openUrl(
            QUrl.fromLocalFile(self._docx_path)
        )


# ---------------------------------------------------------------------------
# Хелпер: открыть .docx из файла
# ---------------------------------------------------------------------------
def open_docx_file(
    path: str,
    *,
    title: str = "Просмотр протокола",
    parent: Optional[QWidget] = None,
) -> bool:
    """
    Открывает .docx во встроенном просмотрщике.

    Возвращает True, если файл открыт, False — если не удалось.
    """
    if not path or not os.path.isfile(path):
        QMessageBox.warning(
            None, title,
            f"Файл не найден:\n{path or '(путь не задан)'}",
        )
        return False

    try:
        size_bytes = os.path.getsize(path)
    except OSError:
        size_bytes = 0

    if size_bytes > MAX_DISPLAY_BYTES:
        reply = QMessageBox.question(
            None, title,
            f"Файл очень большой: "
            f"{size_bytes / 1024 / 1024:.1f} МБ.\n\n"
            f"Открытие может занять время и замедлить "
            f"интерфейс.\n\n"
            f"Открыть всё равно?",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return False

    dlg = DocxViewerDialog(
        docx_path=path,
        title=title,
        parent=parent,
    )
    dlg.exec()
    return True