"""Диалог создания/редактирования промпта из библиотеки.

Позволяет удобно редактировать многострочный текст промпта и
привязанный к нему промпт для генерации summary (вместо прямого
ввода в ячейки таблицы в настройках).

Поля:
  • Название            — обязательное;
  • Текст промпта       — обязательный (многострочный);
  • Промпт для summary  — опциональный (многострочный). Если пуст,
                          для генерации summary используется
                          глобальный «Промпт для генерации summary»
                          из настроек.
"""
from __future__ import annotations

from typing import Optional, Tuple

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QLineEdit, QMessageBox, QPlainTextEdit, QVBoxLayout, QWidget,
)

from ..logger import get_logger
from .tooltips import attach_tooltip, make_info_icon

log = get_logger(__name__)


class PromptEditDialog(QDialog):
    """Окно создания/редактирования промпта и его summary."""

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        name: str = "",
        text: str = "",
        summary_prompt: str = "",
        title: str = "Промпт",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(640)
        self.setMinimumHeight(520)

        self._build_ui()
        self._apply_values(name, text, summary_prompt)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(8)

        # --- Название ---
        name_row = QHBoxLayout()
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("Например: Протокол совещания")
        attach_tooltip(self.name_input, "prompt_edit_name")
        name_row.addWidget(self.name_input, 1)
        icon = make_info_icon("prompt_edit_name")
        if icon is not None:
            name_row.addWidget(icon)
        form.addRow("Название:", name_row)

        # --- Текст промпта ---
        self.text_input = QPlainTextEdit()
        self.text_input.setPlaceholderText(
            "Текст промпта (инструкция для модели)…"
        )
        self.text_input.setMinimumHeight(180)
        attach_tooltip(self.text_input, "prompt_edit_text")
        form.addRow("Текст промпта:", self.text_input)

        # --- Промпт для summary ---
        self.summary_input = QPlainTextEdit()
        self.summary_input.setPlaceholderText(
            "Если пусто — используется «Промпт для генерации summary» "
            "из настроек."
        )
        self.summary_input.setMinimumHeight(120)
        attach_tooltip(self.summary_input, "prompt_edit_summary")
        form.addRow("Промпт для summary:", self.summary_input)

        layout.addLayout(form)

        # --- Кнопки ---
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _apply_values(
        self, name: str, text: str, summary_prompt: str,
    ) -> None:
        self.name_input.setText(name or "")
        self.text_input.setPlainText(text or "")
        self.summary_input.setPlainText(summary_prompt or "")

    # ------------------------------------------------------------------
    # Обработчики
    # ------------------------------------------------------------------
    def _on_accept(self) -> None:
        name = self.name_input.text().strip()
        text = self.text_input.toPlainText().strip()
        if not name:
            QMessageBox.warning(
                self, "Промпт", "Укажите название промпта."
            )
            return
        if not text:
            QMessageBox.warning(
                self, "Промпт", "Текст промпта не может быть пустым."
            )
            return
        self.accept()

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------
    def get_values(self) -> Tuple[str, str, str]:
        """Возвращает (name, text, summary_prompt)."""
        return (
            self.name_input.text().strip(),
            self.text_input.toPlainText().strip(),
            self.summary_input.toPlainText().strip(),
        )
