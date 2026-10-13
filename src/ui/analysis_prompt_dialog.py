"""Диалог выбора промпта из библиотеки для анализа.

Используется в окне «Скрам» для кнопки «Анализ поручений и TFS».

Показывает список промптов из ConfigManager.get_prompts() с
предпросмотром текста. Пользователь выбирает один и нажимает
«Выполнить».

Изменения:
  • Модуль создан.
  • ИСПРАВЛЕНО: сигнал currentRowChanged подключается после
    создания self.preview — иначе при добавлении первого
    элемента QListWidget сразу срабатывает currentRowChanged
    и обработчик падает с AttributeError, потому что preview
    ещё не создан.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout,
    QLabel, QListWidget, QListWidgetItem, QMessageBox,
    QPlainTextEdit, QSplitter, QVBoxLayout, QWidget,
)

from ..logger import get_logger

log = get_logger(__name__)


class AnalysisPromptDialog(QDialog):
    """
    Диалог выбора промпта для анализа.

    Возвращает:
        selected_prompt() — словарь {"name": str, "text": str}
                            или None, если пользователь отменил.
    """

    def __init__(
        self,
        prompts: List[Dict[str, str]],
        *,
        default_name: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Выбор промпта для анализа")
        self.setModal(True)
        self.setMinimumSize(900, 620)

        self._prompts = list(prompts or [])
        self._selected: Optional[Dict[str, str]] = None

        self._build_ui(default_name=default_name)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self, *, default_name: str = "") -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # --- Инфо ---
        info = QLabel(
            "Выберите промпт из библиотеки. К выбранному промпту "
            "будут добавлены данные поручений записи и задачи TFS — "
            "итоговый запрос уйдёт в LLM (провайдер и модель задаются "
            "в Настройки → Суммаризация)."
        )
        info.setWordWrap(True)
        info.setStyleSheet("QLabel { color: #666; }")
        root.addWidget(info)

        # --- Сплиттер: список + предпросмотр ---
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Слева — список промптов.
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(4)

        left_layout.addWidget(QLabel("<b>Промпты:</b>"))

        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.list_widget.setAlternatingRowColors(True)

        # ВАЖНО: сигналы currentRowChanged и itemDoubleClicked
        # подключаем ПОСЛЕ создания self.preview — иначе при
        # добавлении первого элемента QListWidget сразу выставит
        # текущую строку и сигнал вызовет _on_selection_changed,
        # где self.preview ещё не создан.

        for p in self._prompts:
            item = QListWidgetItem(p.get("name") or "(без названия)")
            item.setData(Qt.ItemDataRole.UserRole, p)
            self.list_widget.addItem(item)

        left_layout.addWidget(self.list_widget, 1)
        splitter.addWidget(left)

        # Справа — предпросмотр.
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(4)

        right_layout.addWidget(QLabel("<b>Предпросмотр:</b>"))

        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        right_layout.addWidget(self.preview, 1)

        splitter.addWidget(right)
        splitter.setSizes([300, 600])

        root.addWidget(splitter, 1)

        # --- Кнопки ---
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.button(
            QDialogButtonBox.StandardButton.Ok
        ).setText("Выполнить")
        buttons.button(
            QDialogButtonBox.StandardButton.Cancel
        ).setText("Отмена")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        # --- ВАЖНО: сигналы — после создания всех виджетов ---
        self.list_widget.currentRowChanged.connect(
            self._on_selection_changed
        )
        self.list_widget.itemDoubleClicked.connect(
            lambda _item: self._on_accept()
        )

        # Выбираем начальный промпт:
        #   • по default_name, если он есть в списке;
        #   • иначе — первый в списке.
        selected_row = 0
        if default_name:
            for i, p in enumerate(self._prompts):
                if (p.get("name") or "") == default_name:
                    selected_row = i
                    break
        if self._prompts:
            self.list_widget.setCurrentRow(selected_row)
            # Явно синхронизируем предпросмотр — на случай,
            # если QListWidget не эмитнул сигнал (например,
            # если строка уже была выбрана).
            self._on_selection_changed(selected_row)

    # ------------------------------------------------------------------
    # Обработчики
    # ------------------------------------------------------------------
    def _on_selection_changed(self, row: int) -> None:
        if not hasattr(self, "preview") or self.preview is None:
            return
        if row < 0 or row >= len(self._prompts):
            self.preview.setPlainText("")
            return
        text = self._prompts[row].get("text") or ""
        self.preview.setPlainText(text)

    def _on_accept(self) -> None:
        row = self.list_widget.currentRow()
        if row < 0 or row >= len(self._prompts):
            QMessageBox.information(
                self, "Анализ",
                "Выберите промпт из списка.",
            )
            return
        self._selected = dict(self._prompts[row])
        self.accept()

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------
    def selected_prompt(self) -> Optional[Dict[str, str]]:
        return self._selected