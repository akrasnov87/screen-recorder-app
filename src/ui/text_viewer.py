"""Просмотрщик простого текста (стенограмма, лог, любой .txt).

Отличия от MarkdownViewerDialog:
  • read-only, без панели редактирования;
  • моноширинный шрифт по умолчанию (удобно для стенограмм);
  • встроенный поиск с подсветкой всех совпадений;
  • предупреждение для очень больших файлов (лимит 20 МБ).

Использование:
    dlg = TextViewerDialog(
        text=text,
        title="Стенограмма — Запись 2026-10-01",
        parent=self,
        source_label="video.txt (18 КБ, 2456 символов)",
        default_save_name="transcript.txt",
    )
    dlg.exec()

Изменения:
  • Модуль создан.
"""
from __future__ import annotations

import os
from typing import List, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import (
    QColor, QFont, QKeySequence, QShortcut, QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget,
)

from ..logger import get_logger
from ..utils import safe_local_path
from .tooltips import attach_tooltip, make_info_icon, with_info

log = get_logger(__name__)


# Максимальный размер файла для отображения во встроенном
# просмотрщике. Больше — предлагаем открыть во внешнем редакторе.
MAX_DISPLAY_BYTES = 20 * 1024 * 1024  # 20 МБ


# Цвет подсветки совпадений в режиме поиска.
_HIGHLIGHT_COLOR = QColor("#FFEB3B")  # жёлтый
_HIGHLIGHT_ALPHA = 140


class TextViewerDialog(QDialog):
    """
    Модальный read-only просмотрщик текста.

    Возможности:
      • моноширинный шрифт;
      • поиск по тексту (Ctrl+F) с подсветкой всех совпадений;
      • переход между совпадениями (F3 / Shift+F3);
      • сохранение копии через «Сохранить как…»;
      • корректная обработка очень больших файлов.
    """

    def __init__(
        self,
        text: str = "",
        title: str = "Просмотр текста",
        parent: Optional[QWidget] = None,
        source_label: str = "",
        default_save_name: str = "text.txt",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumSize(900, 640)

        self._source_label = source_label or ""
        self._default_save_name = default_save_name or "text.txt"
        self._text = text or ""

        # Список позиций совпадений (начало, конец).
        self._matches: List[Tuple[int, int]] = []
        self._current_match: int = -1

        self._build_ui()
        self._apply_text()
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
            "Просмотр текста в режиме «только чтение». Чтобы "
            "изменить — сохраните копию и откройте её во внешнем "
            "редакторе."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("QLabel { color: #666; }")
        intro_row.addWidget(intro, 1)
        icon = make_info_icon("text_viewer_intro")
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

        # --- Нижняя панель: статус + кнопки ---
        bottom = QHBoxLayout()

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("QLabel { color: #666; }")
        bottom.addWidget(self.status_label)

        bottom.addStretch()

        self.copy_all_btn = QPushButton("Скопировать всё")
        self.copy_all_btn.setToolTip(
            "Скопировать весь текст в буфер обмена."
        )
        self.copy_all_btn.clicked.connect(self._copy_all)
        bottom.addWidget(self.copy_all_btn)

        self.save_as_btn = QPushButton("Сохранить как…")
        attach_tooltip(self.save_as_btn, "text_viewer_save")
        self.save_as_btn.clicked.connect(self._save_as)
        bottom.addWidget(self.save_as_btn)

        self.close_btn = QPushButton("Закрыть")
        self.close_btn.setToolTip(
            "Закрыть окно просмотра (Esc)."
        )
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
        attach_tooltip(self.search_input, "text_viewer_search")
        layout.addWidget(self.search_input, 1)

        self.find_prev_btn = QPushButton("◀")
        self.find_prev_btn.setFixedWidth(36)
        self.find_prev_btn.setToolTip(
            "Предыдущее совпадение (Shift+F3)."
        )
        self.find_prev_btn.clicked.connect(self._find_prev)
        layout.addWidget(self.find_prev_btn)

        self.find_next_btn = QPushButton("▶")
        self.find_next_btn.setFixedWidth(36)
        self.find_next_btn.setToolTip(
            "Следующее совпадение (F3, Enter в поле поиска)."
        )
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
    # Текст
    # ------------------------------------------------------------------
    def _apply_text(self) -> None:
        self.editor.blockSignals(True)
        self.editor.setPlainText(self._text)
        self.editor.blockSignals(False)

        # В начале — курсор в начало.
        cursor = self.editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        self.editor.setTextCursor(cursor)

    def _update_stats(self) -> None:
        text = self._text
        chars = len(text)
        lines = text.count("\n") + 1 if text else 0
        size_bytes = len(text.encode("utf-8"))

        parts: List[str] = []
        if self._source_label:
            parts.append(self._source_label)
        parts.append(f"{lines} строк")
        parts.append(f"{chars} символов")
        parts.append(f"{size_bytes / 1024:.1f} КБ")

        self.status_label.setText(" · ".join(parts))

    def _refresh_action_buttons(self) -> None:
        has_text = bool(self._text)
        self.copy_all_btn.setEnabled(has_text)
        self.save_as_btn.setEnabled(has_text)

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
            self.matches_label.setText(
                f"Найдено: {total}"
            )
            # Автоматически перейти к первому совпадению от
            # текущей позиции курсора.
            self._current_match = -1
            self._find_next()

    def _apply_highlights(self) -> None:
        selections = []

        fmt = QTextCharFormat()
        fmt.setBackground(_HIGHLIGHT_COLOR)
        fmt.setForeground(QColor("#000000"))

        # Найти текущий индекс (если есть).
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
                # Текущее совпадение — ярче.
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
            "TextViewerDialog: текст скопирован в буфер (%d символов)",
            len(self._text),
        )

    def _save_as(self) -> None:
        if not self._text:
            return

        target, _ = QFileDialog.getSaveFileName(
            self,
            "Сохранить текст как",
            os.path.join(
                os.path.expanduser("~"),
                self._default_save_name,
            ),
            "Текстовые файлы (*.txt);;Все файлы (*)",
        )
        if not target:
            return

        target = safe_local_path(target)

        if not target.lower().endswith(".txt"):
            target += ".txt"

        try:
            with open(target, "w", encoding="utf-8") as f:
                f.write(self._text)
        except Exception as exc:
            log.exception(
                "TextViewerDialog: ошибка сохранения: %s", exc
            )
            QMessageBox.critical(
                self, "Сохранение",
                f"Не удалось сохранить файл:\n{exc}",
            )
            return

        log.info("TextViewerDialog: текст сохранён в %s", target)
        QMessageBox.information(
            self, "Сохранение",
            f"Файл сохранён:\n{target}",
        )


# ---------------------------------------------------------------------------
# Хелпер: открыть текст из файла
# ---------------------------------------------------------------------------
def open_text_file(
    path: str,
    *,
    title: str = "Просмотр текста",
    parent: Optional[QWidget] = None,
) -> bool:
    """
    Открывает файл во встроенном просмотрщике.

    Возвращает True, если файл открыт, False — если не удалось
    (файла нет, слишком большой, ошибка чтения). При False
    показывает соответствующее диалоговое окно.
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

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except Exception as exc:
        log.exception("Не удалось прочитать %s: %s", path, exc)
        QMessageBox.critical(
            None, title,
            f"Не удалось прочитать файл:\n{exc}",
        )
        return False

    source_label = (
        f"{os.path.basename(path)} "
        f"({size_bytes / 1024:.1f} КБ)"
    )

    dlg = TextViewerDialog(
        text=text,
        title=title,
        parent=parent,
        source_label=source_label,
        default_save_name=os.path.basename(path),
    )
    dlg.exec()
    return True