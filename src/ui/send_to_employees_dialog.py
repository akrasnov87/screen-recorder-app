"""Диалог сопоставления исполнителей поручений и сотрудников.

Открывается из окна «Поручения» по кнопке «Отправить сотрудникам…».

Логика:
  1. На входе — список выбранных поручений и справочник сотрудников
     (name, chat_id) из config.
  2. Извлекаем уникальных исполнителей из поля assignee.
  3. Автоматически сопоставляем (fuzzy match ФИО).
  4. Пользователь проверяет/корректирует через ComboBox.
  5. Отправляем каждому сотруднику отдельное сообщение с его
     поручениями.

Диалог использует:
  • EmployeesMatchingWidget — таблица сопоставления и предпросмотр;
  • send_items_to_employees — массовая рассылка в Bitrix24.

Изменения:
  • Модуль переработан: вся логика сопоставления вынесена
    в EmployeesMatchingWidget (src/ui/employees_matching_widget.py).
  • Отправка вынесена в send_items_to_employees
    (src/bitrix_employees_sender.py).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QMessageBox, QVBoxLayout, QWidget,
)

from ..bitrix_employees_sender import send_items_to_employees
from ..logger import get_logger
from .employees_matching_widget import EmployeesMatchingWidget

log = get_logger(__name__)


class SendToEmployeesDialog(QDialog):
    """
    Окно сопоставления исполнителей и сотрудников.

    Самостоятельный диалог: пользователь открывает его из окна
    «Поручения», настраивает сопоставление и нажимает «Отправить» —
    диалог сам рассылает сообщения в Bitrix24.
    """

    def __init__(
        self,
        items: List[Dict[str, Any]],
        employees: List[Dict[str, str]],
        bitrix_cfg: Dict[str, Any],
        *,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Отправка поручений сотрудникам")
        self.setModal(True)
        self.setMinimumSize(1000, 660)

        self._items = list(items or [])
        self._employees = list(employees or [])
        self._bitrix_cfg = bitrix_cfg or {}

        self._build_ui()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        # --- Виджет сопоставления ---
        self.matching_widget = EmployeesMatchingWidget(
            items=self._items,
            employees=self._employees,
            parent=self,
        )
        root.addWidget(self.matching_widget, 1)

        # --- Кнопки ---
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        self.ok_btn = buttons.button(
            QDialogButtonBox.StandardButton.Ok
        )
        self.ok_btn.setText("Отправить")
        buttons.button(
            QDialogButtonBox.StandardButton.Cancel
        ).setText("Отмена")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # ------------------------------------------------------------------
    # Отправка
    # ------------------------------------------------------------------
    def _on_accept(self) -> None:
        messages = self.matching_widget.collect_messages()

        if not messages:
            QMessageBox.warning(
                self, "Поручения",
                "Нет получателей с заданным chat_id. "
                "Проверьте сопоставление.",
            )
            return

        ok = send_items_to_employees(
            messages=messages,
            bitrix_cfg=self._bitrix_cfg,
            parent=self,
            show_confirmation=True,
            confirmation_title="Отправка сотрудникам",
        )

        if ok:
            self.accept()
        # Если не ok — диалог остаётся открытым, чтобы пользователь
        # мог поправить сопоставление и попробовать снова.