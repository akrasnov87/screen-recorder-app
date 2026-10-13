"""Виджет-обёртка вокруг редактора поручений.

Используется внутри окна «Скрам». В отличие от TasksEditorDialog,
это QWidget, а не QDialog, и не имеет кнопок «Сохранить/Отмена» —
все изменения применяются сразу (как в TasksEditorDialog при
нажатии «Сохранить»).

Реализация: композиция — создаём QDialog-объект без показа,
извлекаем из него ключевые виджеты и встраиваем их.

Изменения:
  • Модуль создан.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout,
    QWidget,
)

from ..logger import get_logger
from .tasks_editor_dialog import TasksEditorDialog

log = get_logger(__name__)


class TasksEditorWidget(QWidget):
    """
    Встроенный редактор поручений записи.

    Обёртка над TasksEditorDialog: переиспользует всю его логику,
    но предоставляет интерфейс QWidget для встраивания.

    Автосохранение: при любых изменениях данные пишутся в
    action_items.json сразу (см. TasksEditorDialog._save).
    """

    # Сигнал: список поручений изменился (для обновления других
    # блоков при необходимости).
    items_changed = Signal()

    def __init__(
        self,
        *,
        session_dir: str,
        session_name: str = "",
        employees: Optional[List[Dict[str, str]]] = None,
        compact: bool = False,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)

        self._session_dir = session_dir
        self._session_name = session_name
        self._employees = list(employees or [])
        self._compact = bool(compact)

        # Создаём внутренний TasksEditorDialog, но НЕ показываем его.
        # Мы позаимствуем из него логику и данные, а UI строим сами
        # (переиспользуя его виджеты).
        self._dlg = TasksEditorDialog(
            session_dir=session_dir,
            session_name=session_name,
            employees=self._employees,
            parent=self,
        )

        self._build_ui()

        # Перехватываем закрытие диалога — не даём ему закрыться.
        # (Мы используем его только как «движок».)
        self._dlg.reject = self._noop_reject  # type: ignore
        self._dlg.accept = self._noop_accept  # type: ignore

    def _noop_reject(self) -> None:
        """Не даём внутреннему диалогу закрыться по reject."""
        pass

    def _noop_accept(self) -> None:
        """Не даём внутреннему диалогу закрыться по accept."""
        pass

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # --- Заголовок ---
        header = QHBoxLayout()
        header.addWidget(QLabel("<b>Поручения записи</b>"))
        header.addStretch()
        from .tooltips import make_info_icon
        icon = make_info_icon("scrum_tasks_section")
        if icon is not None:
            header.addWidget(icon)
        layout.addLayout(header)

        # --- Кнопки действий (верхняя панель) ---
        actions = QHBoxLayout()

        self.add_btn = QPushButton("Создать")
        self.add_btn.clicked.connect(self._on_add)
        actions.addWidget(self.add_btn)

        self.edit_btn = QPushButton("Редактировать")
        self.edit_btn.clicked.connect(self._on_edit)
        actions.addWidget(self.edit_btn)

        self.delete_btn = QPushButton("Удалить")
        self.delete_btn.clicked.connect(self._on_delete)
        actions.addWidget(self.delete_btn)

        self.duplicate_btn = QPushButton("Дублировать")
        self.duplicate_btn.clicked.connect(self._on_duplicate)
        actions.addWidget(self.duplicate_btn)

        actions.addStretch()
        layout.addLayout(actions)

        # --- Таблица из внутреннего диалога ---
        # Забираем table-виджет из _dlg и встраиваем.
        self._dlg.table.setParent(self)
        layout.addWidget(self._dlg.table, 1)

        # --- Фильтры (из _dlg) — компактный режим ---
        if not self._compact:
            # Добавляем блок фильтров.
            filters_row = QHBoxLayout()

            filters_row.addWidget(self._dlg.status_filter)
            filters_row.addWidget(self._dlg.assignee_filter)
            filters_row.addWidget(self._dlg.number_search_input)
            filters_row.addWidget(self._dlg.search_input, 1)

            self._dlg.status_filter.setParent(self)
            self._dlg.assignee_filter.setParent(self)
            self._dlg.number_search_input.setParent(self)
            self._dlg.search_input.setParent(self)

            layout.addLayout(filters_row)

        # --- Счётчик ---
        self._dlg.stats_label.setParent(self)
        layout.addWidget(self._dlg.stats_label)

    # ------------------------------------------------------------------
    # Действия
    # ------------------------------------------------------------------
    def _on_add(self) -> None:
        self._dlg._add_item()
        self.items_changed.emit()

    def _on_edit(self) -> None:
        self._dlg._edit_selected()
        self.items_changed.emit()

    def _on_delete(self) -> None:
        self._dlg._delete_selected()
        self.items_changed.emit()

    def _on_duplicate(self) -> None:
        self._dlg._duplicate_selected()
        self.items_changed.emit()

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------
    def reload(self) -> None:
        """Перечитывает action_items.json и обновляет таблицу."""
        try:
            self._dlg._reload()
        except Exception as exc:
            log.warning(
                "TasksEditorWidget.reload: %s", exc
            )