"""Диалог редактирования одного сервиса транскрибации.

Используется из окна «Настройки» → вкладка «Транскрибация»
для добавления и редактирования сервисов в списке.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit,
    QMessageBox, QSpinBox, QVBoxLayout, QWidget,
)

from ..logger import get_logger

log = get_logger(__name__)


class TranscribeServiceEditDialog(QDialog):
    """
    Модальный диалог создания/редактирования одного сервиса
    транскрибации.

    Поля:
      • Название       — человекочитаемое имя (например,
                          «vNext (локальная ВМ)»);
      • URL            — базовый адрес сервиса;
      • Access key     — ключ авторизации;
      • Таймауты       — connect / read / max_wait.
    """

    def __init__(
        self,
        service: Optional[Dict[str, Any]] = None,
        *,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)

        is_new = service is None
        self.setWindowTitle(
            "Новый сервис транскрибации"
            if is_new
            else "Редактирование сервиса транскрибации"
        )
        self.setModal(True)
        self.setMinimumWidth(560)

        self._initial = dict(service or {})
        self._result: Dict[str, Any] = {}

        self._build_ui()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        info = QLabel(
            "Заполните параметры подключения к серверу "
            "транскрибации. Эти же значения используются при "
            "проверке подключения и при обработке записей."
        )
        info.setWordWrap(True)
        info.setStyleSheet("QLabel { color: #666; }")
        root.addWidget(info)

        form = QFormLayout()
        form.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight
            | Qt.AlignmentFlag.AlignVCenter
        )

        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText(
            "Например: vNext (локальная ВМ) — для своего "
            "удобства"
        )
        form.addRow("Название:", self.name_input)

        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText(
            "https://video-transcriber.vnext.mobwal.com"
        )
        form.addRow("URL сервиса:", self.url_input)

        self.key_input = QLineEdit()
        self.key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_input.setPlaceholderText("access_key")

        self.show_key_check = QLineEdit()  # placeholder
        # Правильный виджет — QCheckBox, но импорт не нужен,
        # используем QLineEdit в качестве заглушки? Нет —
        # используем простой чекбокс через импорт.
        from PySide6.QtWidgets import QCheckBox  # локально

        self.show_key_check = QCheckBox("Показать")
        self.show_key_check.toggled.connect(
            lambda checked: self.key_input.setEchoMode(
                QLineEdit.EchoMode.Normal if checked
                else QLineEdit.EchoMode.Password
            )
        )

        key_row = QWidget()
        key_row_layout = __import__(
            "PySide6.QtWidgets", fromlist=["QHBoxLayout"]
        ).QHBoxLayout(key_row)
        key_row_layout.setContentsMargins(0, 0, 0, 0)
        key_row_layout.setSpacing(6)
        key_row_layout.addWidget(self.key_input, 1)
        key_row_layout.addWidget(self.show_key_check, 0)
        form.addRow("Access key:", key_row)

        self.connect_timeout_spin = QSpinBox()
        self.connect_timeout_spin.setRange(1, 600)
        self.connect_timeout_spin.setSuffix(" сек")
        self.connect_timeout_spin.setValue(15)
        form.addRow(
            "Таймаут соединения:", self.connect_timeout_spin
        )

        self.read_timeout_spin = QSpinBox()
        self.read_timeout_spin.setRange(5, 3600)
        self.read_timeout_spin.setSuffix(" сек")
        self.read_timeout_spin.setValue(120)
        form.addRow(
            "Таймаут чтения:", self.read_timeout_spin
        )

        self.max_wait_spin = QSpinBox()
        self.max_wait_spin.setRange(60, 24 * 3600)
        self.max_wait_spin.setSuffix(" сек")
        self.max_wait_spin.setValue(7200)
        form.addRow(
            "Максимум ожидания:", self.max_wait_spin
        )

        root.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.button(
            QDialogButtonBox.StandardButton.Ok
        ).setText("Сохранить")
        buttons.button(
            QDialogButtonBox.StandardButton.Cancel
        ).setText("Отмена")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._apply_initial()

    def _apply_initial(self) -> None:
        it = self._initial
        self.name_input.setText(str(it.get("name") or ""))
        self.url_input.setText(str(it.get("url") or ""))
        self.key_input.setText(str(it.get("access_key") or ""))
        self.connect_timeout_spin.setValue(
            int(it.get("connect_timeout", 15))
        )
        self.read_timeout_spin.setValue(
            int(it.get("read_timeout", 120))
        )
        self.max_wait_spin.setValue(
            int(it.get("max_wait", 7200))
        )

    # ------------------------------------------------------------------
    # Обработка
    # ------------------------------------------------------------------
    def _on_accept(self) -> None:
        url = self.url_input.text().strip()
        if not url:
            QMessageBox.warning(
                self, "Сервис транскрибации",
                "URL сервиса обязателен.",
            )
            return

        name = self.name_input.text().strip()
        if not name:
            # Если имя пустое — используем host как имя.
            from urllib.parse import urlparse
            try:
                parsed = urlparse(url)
                name = parsed.netloc or url
            except Exception:
                name = url
            self.name_input.setText(name)

        self._result = {
            # id сохраняем, если был (редактирование).
            "id": str(self._initial.get("id") or ""),
            "name": name,
            "url": url,
            "access_key": self.key_input.text(),
            "connect_timeout": int(
                self.connect_timeout_spin.value()
            ),
            "read_timeout": int(
                self.read_timeout_spin.value()
            ),
            "max_wait": int(self.max_wait_spin.value()),
        }
        self.accept()

    def result_service(self) -> Dict[str, Any]:
        return dict(self._result)