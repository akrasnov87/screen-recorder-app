"""Окно настроек с вкладками (локальный режим).

Изменения:
  • На вкладке «Проекты и чаты Bitrix24» добавлен блок
    «Проект и чат по умолчанию».
  • Добавлена вкладка «Теги» — справочник меток для записей.
"""
from __future__ import annotations

import asyncio
import html
import json
import os
import shutil
from typing import Dict, List, Optional
from datetime import datetime

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QColorDialog, QComboBox, QDialog,
    QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QPushButton, QSpinBox, QTabWidget, QTableWidget,
    QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget,
)

from .config_manager import ConfigManager, DEFAULT_NAME_TEMPLATES
from .logger import get_current_log_path, get_logger
from .tooltips import attach_tooltip, make_info_icon, with_info
from .transcribe_client import TranscribeClient
from .utils import get_system_monitors

log = get_logger(__name__)


class SettingsWindow(QDialog):
    """Модальное окно настроек."""

    def __init__(
        self, config: ConfigManager,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.config_manager = config
        self.setWindowTitle("Настройки Screen Recorder")
        self.setMinimumSize(1000, 820)
        self.setModal(True)
        log.info("Открытие окна настроек")
        self._build_ui()
        self.load_settings()
        log.debug("Окно настроек готово")

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs)

        self.tabs.addTab(
            self._build_projects_tab(), "Проекты и чаты Bitrix24"
        )
        self.tabs.addTab(self._build_tags_tab(), "Теги")
        self.tabs.addTab(self._build_employees_tab(), "Сотрудники")
        self.tabs.addTab(self._build_bitrix_tab(), "Bitrix24")
        self.tabs.addTab(self._build_metadata_tab(), "Промпты и имена")
        self.tabs.addTab(self._build_transcribe_tab(), "Транскрибация")
        self.tabs.addTab(self._build_summarizer_tab(), "Суммаризация")
        self.tabs.addTab(self._build_glossary_tab(), "Глоссарий")
        self.tabs.addTab(self._build_recording_tab(), "Запись")
        self.tabs.addTab(self._build_queue_tab(), "Очередь")
        self.tabs.addTab(self._build_scrum_tab(), "Скрам")
        self.tabs.addTab(
            self._build_yandex_vm_tab(), "ВМ Yandex"
        )
        self.tabs.addTab(
            self._build_compression_tab(), "Форматы и сжатие"
        )
        self.tabs.addTab(self._build_storage_tab(), "Хранилище")
        self.tabs.addTab(self._build_logging_tab(), "Логи")

        buttons = QHBoxLayout()
        self.help_btn = QPushButton("Помощь")
        self.help_btn.clicked.connect(self._show_help)

        self.export_btn = QPushButton("Экспорт настроек…")
        self.export_btn.clicked.connect(self._export_settings)
        buttons.addWidget(
            with_info(self.export_btn, "settings_export", stretch=False)
        )

        self.import_btn = QPushButton("Импорт настроек…")
        self.import_btn.clicked.connect(self._import_settings)
        buttons.addWidget(
            with_info(self.import_btn, "settings_import", stretch=False)
        )

        buttons.addStretch()

        self.reset_btn = QPushButton("Сбросить")
        self.reset_btn.clicked.connect(self.reset_to_defaults)
        self.save_btn = QPushButton("Сохранить")
        self.save_btn.clicked.connect(self.save_settings)
        self.close_btn = QPushButton("Закрыть")
        self.close_btn.clicked.connect(self.reject)

        buttons.addWidget(self.reset_btn)
        buttons.addWidget(self.save_btn)
        buttons.addWidget(self.close_btn)
        root.addLayout(buttons)

    def _build_yandex_vm_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        info = QLabel(
            "Корневая папка, внутри которой лежат подпапки с "
            "конфигурациями виртуальных машин Yandex Cloud.\n\n"
            "Каждая подпапка — одна ВМ. Внутри каждой папки "
            "ожидаются файлы:\n"
            "  • <code>schedule.cron</code> — расписание "
            "работы ВМ;\n"
            "  • <code>exceptions.txt</code> — исключения "
            "(переопределения).\n\n"
            "Через окно «ВМ Yandex» (в трее) эти файлы можно "
            "редактировать и сохранять прямо из приложения."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        form = QFormLayout()
        form.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight
            | Qt.AlignmentFlag.AlignVCenter
        )

        self.yandex_vm_root_input = QLineEdit()
        self.yandex_vm_root_input.setPlaceholderText(
            "Например: /home/user/vm_manager/schedules"
        )

        browse_btn = QPushButton("Обзор…")
        browse_btn.clicked.connect(
            self._browse_yandex_vm_root
        )

        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(self.yandex_vm_root_input, 1)
        row.addWidget(browse_btn, 0)
        icon = make_info_icon("yandex_vm_root")
        if icon is not None:
            row.addWidget(icon, 0)

        form.addRow("Корневая папка ВМ:", container)
        layout.addLayout(form)

        hint = QLabel(
            "<span style='color:#666'>Пример структуры:</span>\n"
            "<pre style='font-family:monospace; color:#444'>"
            "vm_manager/\n"
            "├── vm-prod-01/\n"
            "│   ├── schedule.cron\n"
            "│   └── exceptions.txt\n"
            "├── vm-test-02/\n"
            "│   ├── schedule.cron\n"
            "│   └── exceptions.txt\n"
            "└── vm-dev-03/\n"
            "    ├── schedule.cron\n"
            "    └── exceptions.txt"
            "</pre>"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        layout.addStretch()
        return w

    def _browse_yandex_vm_root(self) -> None:
        start = (
            self.yandex_vm_root_input.text().strip()
            or os.path.expanduser("~")
        )
        folder = QFileDialog.getExistingDirectory(
            self,
            "Выберите корневую папку с конфигурациями ВМ",
            start,
        )
        if folder:
            self.yandex_vm_root_input.setText(folder)

    # ------------------------------------------------------------------
    # Проекты и чаты Bitrix24
    # ------------------------------------------------------------------
    def _build_projects_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        info = QLabel(
            "Список проектов, доступных при вводе метаданных записи. "
            "Для каждого проекта можно указать ID чата Bitrix24 — "
            "тогда в окне «Записи» можно будет быстро отправить "
            "протокол и/или summary в этот чат."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        hint = QLabel(
            "ID чата — это идентификатор диалога в Bitrix24. "
            "Найти его можно через API методом "
            "<code>im.recent.get</code> или из URL чата в "
            "веб-интерфейсе.<br><br>"
            "Формат: <code>chat2101</code> или просто "
            "<code>2101</code>."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("QLabel { color: #666; }")
        layout.addWidget(hint)

        header = QHBoxLayout()
        header.addWidget(QLabel("<b>Проекты</b>"))
        header.addStretch()
        header.addWidget(make_info_icon("projects_list"))
        layout.addLayout(header)

        self.projects_table = QTableWidget(0, 2)
        self.projects_table.setHorizontalHeaderLabels([
            "Проект", "Чат Bitrix24",
        ])
        self.projects_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.projects_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        hv = self.projects_table.horizontalHeader()
        hv.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hv.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.projects_table.setMinimumHeight(240)
        layout.addWidget(self.projects_table)

        btns = QHBoxLayout()
        add_btn = QPushButton("Добавить")
        add_btn.clicked.connect(self._project_add)
        del_btn = QPushButton("Удалить")
        del_btn.clicked.connect(self._project_delete)
        up_btn = QPushButton("Вверх")
        up_btn.clicked.connect(lambda: self._project_move(-1))
        down_btn = QPushButton("Вниз")
        down_btn.clicked.connect(lambda: self._project_move(1))
        btns.addWidget(add_btn)
        btns.addWidget(del_btn)
        btns.addWidget(up_btn)
        btns.addWidget(down_btn)
        btns.addStretch()
        layout.addLayout(btns)

        # --- Блок «Проект и чат по умолчанию» ---
        sep = QLabel("<hr>")
        layout.addWidget(sep)

        default_header = QHBoxLayout()
        default_header.addWidget(
            QLabel("<b>Проект и чат по умолчанию</b>")
        )
        default_header.addStretch()
        layout.addLayout(default_header)

        default_hint = QLabel(
            "Эти значения используются при создании НОВЫХ записей "
            "и при отправке протоколов/summary в Bitrix24.<br><br>"
            "• <b>Проект по умолчанию</b> — какой проект подставлять "
            "в карточку метаданных. Если выбрать «— первый из списка —», "
            "берётся проект, стоящий первым в таблице выше.<br>"
            "• <b>Чат по умолчанию</b> — ID чата Bitrix24, куда "
            "уходят протоколы и summary. Если пусто — используется "
            "чат, привязанный к проекту записи."
        )
        default_hint.setWordWrap(True)
        default_hint.setStyleSheet("QLabel { color: #666; }")
        layout.addWidget(default_hint)

        default_form = QFormLayout()
        default_form.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight
            | Qt.AlignmentFlag.AlignVCenter
        )

        self.default_project_combo = QComboBox()
        self.default_project_combo.setEditable(False)
        self.default_project_combo.setMinimumWidth(280)
        default_form.addRow(
            "Проект по умолчанию:",
            with_info(
                self.default_project_combo, "default_project"
            ),
        )

        self.default_chat_id_input = QLineEdit()
        self.default_chat_id_input.setPlaceholderText(
            "Например: chat2101 или 2101 — оставьте пустым, чтобы "
            "использовать чат проекта записи"
        )
        default_form.addRow(
            "Чат по умолчанию:",
            with_info(
                self.default_chat_id_input, "default_chat_id"
            ),
        )

        layout.addLayout(default_form)
        layout.addStretch()
        return w

    def _refresh_default_project_combo(
        self, current: str = "",
    ) -> None:
        if not hasattr(self, "default_project_combo"):
            return

        self.default_project_combo.blockSignals(True)
        self.default_project_combo.clear()
        self.default_project_combo.addItem(
            "— первый из списка —", ""
        )
        for row in range(self.projects_table.rowCount()):
            item = self.projects_table.item(row, 0)
            name = item.text().strip() if item else ""
            if name:
                self.default_project_combo.addItem(name, name)

        if current:
            idx = self.default_project_combo.findData(current)
            if idx >= 0:
                self.default_project_combo.setCurrentIndex(idx)
        self.default_project_combo.blockSignals(False)

    def _project_add(self) -> None:
        row = self.projects_table.rowCount()
        self.projects_table.insertRow(row)
        self.projects_table.setItem(
            row, 0, QTableWidgetItem("Новый проект")
        )
        self.projects_table.setItem(row, 1, QTableWidgetItem(""))
        self.projects_table.editItem(self.projects_table.item(row, 0))
        self._refresh_default_project_combo(
            current=self.default_project_combo.currentData() or ""
        )
        log.debug("Добавлен пустой проект (строка %d)", row)

    def _project_delete(self) -> None:
        row = self.projects_table.currentRow()
        if row < 0:
            return
        item = self.projects_table.item(row, 0)
        name = item.text() if item else ""
        if QMessageBox.question(
            self, "Удалить проект",
            f"Удалить проект «{name}»?",
        ) == QMessageBox.StandardButton.Yes:
            log.info("Удаление проекта «%s» (строка %d)", name, row)
            self.projects_table.removeRow(row)
            self._refresh_default_project_combo(
                current=self.default_project_combo.currentData() or ""
            )

    def _project_move(self, delta: int) -> None:
        row = self.projects_table.currentRow()
        if row < 0:
            return
        new_row = row + delta
        if new_row < 0 or new_row >= self.projects_table.rowCount():
            return
        for col in range(self.projects_table.columnCount()):
            a = self.projects_table.takeItem(row, col)
            b = self.projects_table.takeItem(new_row, col)
            self.projects_table.setItem(row, col, b)
            self.projects_table.setItem(new_row, col, a)
        self.projects_table.setCurrentCell(new_row, 0)
        self._refresh_default_project_combo(
            current=self.default_project_combo.currentData() or ""
        )
        log.debug("Проект перемещён: %d → %d", row, new_row)

    # ------------------------------------------------------------------
    # Теги
    # ------------------------------------------------------------------
    def _build_tags_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        info = QLabel(
            "Справочник тегов — меток, которыми можно помечать "
            "записи. Теги используются в карточке метаданных "
            "(можно выбрать несколько) и в фильтре раздела "
            "«Библиотека»."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        hint = QLabel(
            "Название тега — произвольное, например: «важное», "
            "«риски», «для клиента», «решения».<br>"
            "Цвет используется для подсветки тега в интерфейсе — "
            "двойной клик по ячейке «Цвет» открывает палитру.<br>"
            "Порядок тегов в таблице влияет на порядок в списке "
            "карточки метаданных."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("QLabel { color: #666; }")
        layout.addWidget(hint)

        header = QHBoxLayout()
        header.addWidget(QLabel("<b>Теги</b>"))
        header.addStretch()
        header.addWidget(make_info_icon("tags_list"))
        layout.addLayout(header)

        self.tags_table = QTableWidget(0, 2)
        self.tags_table.setHorizontalHeaderLabels([
            "Тег", "Цвет",
        ])
        self.tags_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.tags_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.tags_table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        hv = self.tags_table.horizontalHeader()
        hv.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hv.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.tags_table.setMinimumHeight(320)
        self.tags_table.cellDoubleClicked.connect(
            self._on_tag_cell_double_clicked
        )
        layout.addWidget(self.tags_table)

        btns = QHBoxLayout()
        add_btn = QPushButton("Добавить")
        add_btn.clicked.connect(self._tag_add)
        del_btn = QPushButton("Удалить")
        del_btn.clicked.connect(self._tag_delete)
        up_btn = QPushButton("Вверх")
        up_btn.clicked.connect(lambda: self._tag_move(-1))
        down_btn = QPushButton("Вниз")
        down_btn.clicked.connect(lambda: self._tag_move(1))
        color_btn = QPushButton("Выбрать цвет…")
        color_btn.setToolTip(
            "Открыть палитру для выбранного тега"
        )
        color_btn.clicked.connect(self._tag_pick_color)
        btns.addWidget(add_btn)
        btns.addWidget(del_btn)
        btns.addWidget(up_btn)
        btns.addWidget(down_btn)
        btns.addSpacing(12)
        btns.addWidget(color_btn)
        btns.addStretch()
        layout.addLayout(btns)

        layout.addStretch()
        return w

    def _tag_add(self) -> None:
        row = self.tags_table.rowCount()
        self.tags_table.insertRow(row)
        self.tags_table.setItem(
            row, 0, QTableWidgetItem("новый-тег")
        )
        color_item = QTableWidgetItem("")
        color_item.setFlags(
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
        )
        self.tags_table.setItem(row, 1, color_item)
        self.tags_table.editItem(self.tags_table.item(row, 0))
        log.debug("Добавлен пустой тег (строка %d)", row)

    def _tag_delete(self) -> None:
        row = self.tags_table.currentRow()
        if row < 0:
            return
        item = self.tags_table.item(row, 0)
        name = item.text() if item else ""
        if QMessageBox.question(
            self, "Удалить тег",
            f"Удалить тег «{name}» из справочника?\n\n"
            f"У уже сохранённых записей метка останется, "
            f"но исчезнет из списка для выбора.",
        ) == QMessageBox.StandardButton.Yes:
            log.info("Удаление тега «%s» (строка %d)", name, row)
            self.tags_table.removeRow(row)

    def _tag_move(self, delta: int) -> None:
        row = self.tags_table.currentRow()
        if row < 0:
            return
        new_row = row + delta
        if new_row < 0 or new_row >= self.tags_table.rowCount():
            return
        for col in range(self.tags_table.columnCount()):
            a = self.tags_table.takeItem(row, col)
            b = self.tags_table.takeItem(new_row, col)
            self.tags_table.setItem(row, col, b)
            self.tags_table.setItem(new_row, col, a)
        self.tags_table.setCurrentCell(new_row, 0)

    def _on_tag_cell_double_clicked(self, row: int, col: int) -> None:
        if col == 1:
            self._tag_pick_color()

    def _tag_pick_color(self) -> None:
        row = self.tags_table.currentRow()
        if row < 0:
            QMessageBox.information(
                self, "Теги", "Выберите тег в таблице."
            )
            return

        current_item = self.tags_table.item(row, 1)
        current = current_item.text().strip() if current_item else ""

        initial = QColor("#4a90d9")
        if current:
            c = QColor(current)
            if c.isValid():
                initial = c

        color = QColorDialog.getColor(
            initial, self, "Выберите цвет тега"
        )
        if not color.isValid():
            return

        hex_color = color.name()
        if current_item is None:
            current_item = QTableWidgetItem(hex_color)
            current_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
            )
            self.tags_table.setItem(row, 1, current_item)
        else:
            current_item.setText(hex_color)

        # Подсветим превью-плашку в ячейке цвета
        pixmap = QColor(hex_color)
        current_item.setBackground(pixmap)
        name_item = self.tags_table.item(row, 0)
        if name_item:
            name_item.setForeground(
                QColor("#FFFFFF")
                if pixmap.lightness() < 128
                else QColor("#000000")
            )

        log.info("Цвет тега «%s»: %s",
                 name_item.text() if name_item else "—", hex_color)

    # ------------------------------------------------------------------
    # Сотрудники
    # ------------------------------------------------------------------
    def _build_employees_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        info = QLabel(
            "Справочник сотрудников с их личными чатами Bitrix24. "
            "Используется в окне отправки в чат — можно выбрать "
            "не проект, а конкретного сотрудника."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        hint = QLabel(
            "ID чата — идентификатор личного диалога с сотрудником в "
            "Bitrix24. Найти его можно через API методом "
            "<code>im.recent.get</code>.<br><br>"
            "Формат: <code>123</code> — числовой ID пользователя."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("QLabel { color: #666; }")
        layout.addWidget(hint)

        header = QHBoxLayout()
        header.addWidget(QLabel("<b>Сотрудники</b>"))
        header.addStretch()
        header.addWidget(make_info_icon("employees_list"))
        layout.addLayout(header)

        self.employees_table = QTableWidget(0, 2)
        self.employees_table.setHorizontalHeaderLabels([
            "ФИО", "Чат Bitrix24 (ID пользователя)",
        ])
        self.employees_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.employees_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        hv = self.employees_table.horizontalHeader()
        hv.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hv.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.employees_table.setMinimumHeight(320)
        layout.addWidget(self.employees_table)

        btns = QHBoxLayout()
        add_btn = QPushButton("Добавить")
        add_btn.clicked.connect(self._employee_add)
        del_btn = QPushButton("Удалить")
        del_btn.clicked.connect(self._employee_delete)
        up_btn = QPushButton("Вверх")
        up_btn.clicked.connect(lambda: self._employee_move(-1))
        down_btn = QPushButton("Вниз")
        down_btn.clicked.connect(lambda: self._employee_move(1))
        btns.addWidget(add_btn)
        btns.addWidget(del_btn)
        btns.addWidget(up_btn)
        btns.addWidget(down_btn)
        btns.addStretch()
        layout.addLayout(btns)

        layout.addStretch()
        return w

    def _employee_add(self) -> None:
        row = self.employees_table.rowCount()
        self.employees_table.insertRow(row)
        self.employees_table.setItem(
            row, 0, QTableWidgetItem("Фамилия И.О.")
        )
        self.employees_table.setItem(row, 1, QTableWidgetItem(""))
        self.employees_table.editItem(
            self.employees_table.item(row, 0)
        )

    def _employee_delete(self) -> None:
        row = self.employees_table.currentRow()
        if row < 0:
            return
        item = self.employees_table.item(row, 0)
        name = item.text() if item else ""
        if QMessageBox.question(
            self, "Удалить сотрудника",
            f"Удалить сотрудника «{name}»?",
        ) == QMessageBox.StandardButton.Yes:
            self.employees_table.removeRow(row)

    def _employee_move(self, delta: int) -> None:
        row = self.employees_table.currentRow()
        if row < 0:
            return
        new_row = row + delta
        if new_row < 0 or new_row >= self.employees_table.rowCount():
            return
        for col in range(self.employees_table.columnCount()):
            a = self.employees_table.takeItem(row, col)
            b = self.employees_table.takeItem(new_row, col)
            self.employees_table.setItem(row, col, b)
            self.employees_table.setItem(new_row, col, a)
        self.employees_table.setCurrentCell(new_row, 0)

    # ------------------------------------------------------------------
    # Bitrix24
    # ------------------------------------------------------------------
    def _build_bitrix_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        info = QLabel(
            "Настройки интеграции с Bitrix24. Используется для "
            "отправки протоколов и кратких описаний в чаты команд "
            "из окна «Записи»."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        form = QFormLayout()

        self.bitrix_enabled_check = QCheckBox(
            "Включить интеграцию с Bitrix24"
        )
        attach_tooltip(self.bitrix_enabled_check, "bitrix_enabled")
        form.addRow("", self.bitrix_enabled_check)

        self.bitrix_webhook_input = QLineEdit()
        self.bitrix_webhook_input.setEchoMode(
            QLineEdit.EchoMode.Password
        )
        self.bitrix_webhook_input.setPlaceholderText(
            "https://portal.bitrix24.ru/rest/1/token/"
        )

        self.bitrix_show_webhook = QCheckBox("Показать")
        self.bitrix_show_webhook.toggled.connect(
            lambda checked: self.bitrix_webhook_input.setEchoMode(
                QLineEdit.EchoMode.Normal if checked
                else QLineEdit.EchoMode.Password
            )
        )

        wh_row = QWidget()
        wh_l = QHBoxLayout(wh_row)
        wh_l.setContentsMargins(0, 0, 0, 0)
        wh_l.setSpacing(6)
        wh_l.addWidget(self.bitrix_webhook_input, 1)
        wh_l.addWidget(self.bitrix_show_webhook, 0)
        form.addRow("Вебхук:", with_info(wh_row, "bitrix_webhook"))

        self.bitrix_connect_timeout = QSpinBox()
        self.bitrix_connect_timeout.setRange(1, 600)
        self.bitrix_connect_timeout.setSuffix(" сек")
        self.bitrix_connect_timeout.setValue(15)
        form.addRow(
            "Таймаут соединения:",
            with_info(
                self.bitrix_connect_timeout, "bitrix_connect_timeout"
            ),
        )

        self.bitrix_read_timeout = QSpinBox()
        self.bitrix_read_timeout.setRange(5, 3600)
        self.bitrix_read_timeout.setSuffix(" сек")
        self.bitrix_read_timeout.setValue(60)
        form.addRow(
            "Таймаут чтения:",
            with_info(self.bitrix_read_timeout, "bitrix_read_timeout"),
        )

        self.bitrix_default_send_combo = QComboBox()
        self.bitrix_default_send_combo.addItem("Протокол", "protocol")
        self.bitrix_default_send_combo.addItem("Summary", "summary")
        self.bitrix_default_send_combo.addItem(
            "И то и другое", "both"
        )
        form.addRow(
            "Что отправлять по умолчанию:",
            with_info(
                self.bitrix_default_send_combo, "bitrix_default_send"
            ),
        )

        self.bitrix_header_check = QCheckBox(
            "Добавлять заголовок (название записи + дата)"
        )
        attach_tooltip(self.bitrix_header_check, "bitrix_header")
        form.addRow("", self.bitrix_header_check)

        self.bitrix_system_check = QCheckBox(
            "Системное сообщение (SYSTEM=Y)"
        )
        attach_tooltip(self.bitrix_system_check, "bitrix_system")
        form.addRow("", self.bitrix_system_check)

        self.bitrix_no_preview_check = QCheckBox(
            "Отключить предпросмотр ссылок"
        )
        attach_tooltip(
            self.bitrix_no_preview_check, "bitrix_no_preview"
        )
        form.addRow("", self.bitrix_no_preview_check)

        files_header = QHBoxLayout()
        files_header.addWidget(QLabel("<b>Отправка файлов</b>"))
        files_header.addStretch()
        files_header.addWidget(
            make_info_icon("bitrix_files_section")
        )
        files_row = QWidget()
        files_row.setLayout(files_header)
        form.addRow("", files_row)

        self.bitrix_file_threshold = QSpinBox()
        self.bitrix_file_threshold.setRange(500, 100000)
        self.bitrix_file_threshold.setSingleStep(500)
        self.bitrix_file_threshold.setSuffix(" символов")
        self.bitrix_file_threshold.setValue(3000)
        form.addRow(
            "Порог «текст → файл»:",
            with_info(
                self.bitrix_file_threshold, "bitrix_file_threshold"
            ),
        )

        self.bitrix_upload_folder = QSpinBox()
        self.bitrix_upload_folder.setRange(0, 999999999)
        self.bitrix_upload_folder.setValue(0)
        self.bitrix_upload_folder.setSpecialValueText(
            "Папка чата (авто)"
        )
        form.addRow(
            "Папка для загрузки:",
            with_info(
                self.bitrix_upload_folder, "bitrix_upload_folder"
            ),
        )

        self.bitrix_max_message_chars = QSpinBox()
        self.bitrix_max_message_chars.setRange(1000, 20000)
        self.bitrix_max_message_chars.setSingleStep(500)
        self.bitrix_max_message_chars.setSuffix(" символов")
        self.bitrix_max_message_chars.setValue(15000)
        form.addRow(
            "Максимальная длина сообщения:",
            with_info(
                self.bitrix_max_message_chars,
                "bitrix_max_message_chars",
            ),
        )

        self.bitrix_retry_count = QSpinBox()
        self.bitrix_retry_count.setRange(0, 20)
        self.bitrix_retry_count.setValue(3)
        form.addRow(
            "Попыток при ошибке:",
            with_info(self.bitrix_retry_count, "bitrix_retry_count"),
        )

        self.bitrix_retry_delay = QDoubleSpinBox()
        self.bitrix_retry_delay.setRange(0.5, 60.0)
        self.bitrix_retry_delay.setSingleStep(0.5)
        self.bitrix_retry_delay.setDecimals(1)
        self.bitrix_retry_delay.setSuffix(" сек")
        self.bitrix_retry_delay.setValue(2.0)
        form.addRow(
            "Пауза между повторами:",
            with_info(self.bitrix_retry_delay, "bitrix_retry_delay"),
        )

        self.bitrix_test_btn = QPushButton("Проверить подключение")
        self.bitrix_test_btn.clicked.connect(
            self._test_bitrix_connection
        )
        form.addRow("", self.bitrix_test_btn)

        layout.addLayout(form)
        layout.addStretch()
        return w

    def _test_bitrix_connection(self) -> None:
        webhook = self.bitrix_webhook_input.text().strip()
        if not webhook:
            QMessageBox.warning(
                self, "Bitrix24",
                "Укажите URL вебхука Bitrix24.",
            )
            return

        from .bitrix_client import Bitrix24Client, Bitrix24Error

        connect_timeout = float(self.bitrix_connect_timeout.value())
        read_timeout = float(self.bitrix_read_timeout.value())
        max_message_chars = int(
            self.bitrix_max_message_chars.value()
        )

        QGuiApplication.setOverrideCursor(
            Qt.CursorShape.WaitCursor
        )

        async def _run_ping() -> str:
            async with Bitrix24Client(
                webhook_url=webhook,
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
                max_message_chars=max_message_chars,
            ) as client:
                return await client.ping()

        try:
            name = asyncio.run(_run_ping())
        except Bitrix24Error as exc:
            log.warning("Проверка Bitrix24 не удалась: %s", exc)
            QMessageBox.warning(
                self, "Bitrix24",
                f"Не удалось подключиться:\n\n{exc}",
            )
            return
        except Exception as exc:
            log.exception("Ошибка при проверке Bitrix24: %s", exc)
            QMessageBox.critical(self, "Bitrix24", f"Ошибка:\n{exc}")
            return
        finally:
            if QGuiApplication.overrideCursor() is not None:
                QGuiApplication.restoreOverrideCursor()

        QMessageBox.information(
            self, "Bitrix24",
            f"Подключение успешно.\n\nПользователь вебхука: {name}",
        )

    # ------------------------------------------------------------------
    # Промпты и шаблоны имён
    # ------------------------------------------------------------------
    def _build_metadata_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        info = QLabel(
            "Библиотека промптов и шаблоны названий записи."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        prompts_header = QHBoxLayout()
        prompts_header.addWidget(QLabel("<b>Библиотека промптов</b>"))
        prompts_header.addStretch()
        prompts_header.addWidget(make_info_icon("prompts_table"))
        layout.addLayout(prompts_header)

        self.prompts_table = QTableWidget(0, 2)
        self.prompts_table.setHorizontalHeaderLabels(
            ["Название", "Текст"]
        )
        self.prompts_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        header = self.prompts_table.horizontalHeader()
        header.setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.prompts_table.setMinimumHeight(150)
        layout.addWidget(self.prompts_table)

        prompt_btns = QHBoxLayout()
        add_btn = QPushButton("Добавить")
        add_btn.clicked.connect(self._prompt_add)
        del_btn = QPushButton("Удалить")
        del_btn.clicked.connect(self._prompt_delete)
        up_btn = QPushButton("Вверх")
        up_btn.clicked.connect(lambda: self._prompt_move(-1))
        down_btn = QPushButton("Вниз")
        down_btn.clicked.connect(lambda: self._prompt_move(1))
        prompt_btns.addWidget(add_btn)
        prompt_btns.addWidget(del_btn)
        prompt_btns.addWidget(up_btn)
        prompt_btns.addWidget(down_btn)
        prompt_btns.addStretch()
        layout.addLayout(prompt_btns)

        default_header = QHBoxLayout()
        default_header.addWidget(QLabel("<b>Промпт по умолчанию</b>"))
        default_header.addStretch()
        default_header.addWidget(make_info_icon("default_prompt"))
        layout.addLayout(default_header)

        self.default_prompt_edit = QPlainTextEdit()
        self.default_prompt_edit.setFixedHeight(90)
        layout.addWidget(self.default_prompt_edit)

        names_header = QHBoxLayout()
        names_header.addWidget(
            QLabel("<b>Шаблоны названий записи</b>")
        )
        names_header.addStretch()
        names_header.addWidget(make_info_icon("name_templates"))
        layout.addLayout(names_header)

        self.name_templates_table = QTableWidget(0, 2)
        self.name_templates_table.setHorizontalHeaderLabels(
            ["Название", "Шаблон"]
        )
        self.name_templates_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        header = self.name_templates_table.horizontalHeader()
        header.setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.name_templates_table.setMinimumHeight(150)
        layout.addWidget(self.name_templates_table)

        name_btns = QHBoxLayout()
        n_add_btn = QPushButton("Добавить")
        n_add_btn.clicked.connect(self._name_tpl_add)
        n_del_btn = QPushButton("Удалить")
        n_del_btn.clicked.connect(self._name_tpl_delete)
        n_up_btn = QPushButton("Вверх")
        n_up_btn.clicked.connect(lambda: self._name_tpl_move(-1))
        n_down_btn = QPushButton("Вниз")
        n_down_btn.clicked.connect(lambda: self._name_tpl_move(1))
        n_default_btn = QPushButton("Вернуть стандартные")
        n_default_btn.clicked.connect(
            self._name_tpl_restore_defaults
        )
        name_btns.addWidget(n_add_btn)
        name_btns.addWidget(n_del_btn)
        name_btns.addWidget(n_up_btn)
        name_btns.addWidget(n_down_btn)
        name_btns.addWidget(n_default_btn)
        name_btns.addStretch()
        layout.addLayout(name_btns)

        layout.addStretch()
        return w

    def _prompt_add(self) -> None:
        row = self.prompts_table.rowCount()
        self.prompts_table.insertRow(row)
        self.prompts_table.setItem(
            row, 0, QTableWidgetItem("Новый промпт")
        )
        self.prompts_table.setItem(row, 1, QTableWidgetItem(""))
        self.prompts_table.editItem(self.prompts_table.item(row, 0))

    def _prompt_delete(self) -> None:
        row = self.prompts_table.currentRow()
        if row < 0:
            return
        name = self.prompts_table.item(row, 0)
        name = name.text() if name else ""
        if QMessageBox.question(
            self, "Удалить промпт",
            f"Удалить промпт «{name}»?",
        ) == QMessageBox.StandardButton.Yes:
            self.prompts_table.removeRow(row)

    def _prompt_move(self, delta: int) -> None:
        row = self.prompts_table.currentRow()
        if row < 0:
            return
        new_row = row + delta
        if new_row < 0 or new_row >= self.prompts_table.rowCount():
            return
        for col in range(self.prompts_table.columnCount()):
            a = self.prompts_table.takeItem(row, col)
            b = self.prompts_table.takeItem(new_row, col)
            self.prompts_table.setItem(row, col, b)
            self.prompts_table.setItem(new_row, col, a)
        self.prompts_table.setCurrentCell(new_row, 0)

    def _name_tpl_add(self) -> None:
        row = self.name_templates_table.rowCount()
        self.name_templates_table.insertRow(row)
        self.name_templates_table.setItem(
            row, 0, QTableWidgetItem("Новый шаблон")
        )
        self.name_templates_table.setItem(
            row, 1, QTableWidgetItem("{name} — {date}")
        )
        self.name_templates_table.editItem(
            self.name_templates_table.item(row, 0)
        )

    def _name_tpl_delete(self) -> None:
        row = self.name_templates_table.currentRow()
        if row < 0:
            return
        item = self.name_templates_table.item(row, 0)
        label = item.text() if item else ""
        if QMessageBox.question(
            self, "Удалить шаблон",
            f"Удалить шаблон «{label}»?",
        ) == QMessageBox.StandardButton.Yes:
            self.name_templates_table.removeRow(row)

    def _name_tpl_move(self, delta: int) -> None:
        row = self.name_templates_table.currentRow()
        if row < 0:
            return
        new_row = row + delta
        if (new_row < 0
                or new_row >= self.name_templates_table.rowCount()):
            return
        for col in range(self.name_templates_table.columnCount()):
            a = self.name_templates_table.takeItem(row, col)
            b = self.name_templates_table.takeItem(new_row, col)
            self.name_templates_table.setItem(row, col, b)
            self.name_templates_table.setItem(new_row, col, a)
        self.name_templates_table.setCurrentCell(new_row, 0)

    def _name_tpl_restore_defaults(self) -> None:
        if QMessageBox.question(
            self, "Стандартные шаблоны",
            "Заменить текущий список шаблонов стандартными?",
        ) != QMessageBox.StandardButton.Yes:
            return
        self.name_templates_table.setRowCount(0)
        for item in DEFAULT_NAME_TEMPLATES:
            row = self.name_templates_table.rowCount()
            self.name_templates_table.insertRow(row)
            self.name_templates_table.setItem(
                row, 0, QTableWidgetItem(item.get("label", ""))
            )
            self.name_templates_table.setItem(
                row, 1, QTableWidgetItem(item.get("template", ""))
            )

    # ------------------------------------------------------------------
    # Транскрибация
    # ------------------------------------------------------------------
    def _build_transcribe_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        info = QLabel(
            "Параметры подключения к серверу транскрибации."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        tr_box = QFormLayout()
        self.tr_url_input = QLineEdit()
        self.tr_url_input.setPlaceholderText("http://localhost:8000")

        self.tr_key_input = QLineEdit()
        self.tr_key_input.setEchoMode(QLineEdit.EchoMode.Password)

        self.tr_show_key = QCheckBox("Показать ключ")
        self.tr_show_key.toggled.connect(
            lambda checked: self.toggle_password_visibility(
                self.tr_key_input, self.tr_show_key
            )
        )

        self.tr_connect_timeout = QSpinBox()
        self.tr_connect_timeout.setRange(1, 600)
        self.tr_connect_timeout.setSuffix(" сек")
        self.tr_connect_timeout.setValue(15)

        self.tr_read_timeout = QSpinBox()
        self.tr_read_timeout.setRange(5, 3600)
        self.tr_read_timeout.setSuffix(" сек")
        self.tr_read_timeout.setValue(120)

        self.tr_max_wait = QSpinBox()
        self.tr_max_wait.setRange(60, 24 * 3600)
        self.tr_max_wait.setSuffix(" сек")
        self.tr_max_wait.setValue(7200)

        self.test_tr_btn = QPushButton("Проверить подключение")
        self.test_tr_btn.clicked.connect(
            self.test_transcribe_connection
        )

        tr_box.addRow(
            "URL сервера:",
            with_info(self.tr_url_input, "tr_url"),
        )
        tr_box.addRow(
            "Access key:",
            with_info(self.tr_key_input, "tr_key"),
        )
        tr_box.addRow("", self.tr_show_key)
        tr_box.addRow(
            "Таймаут соединения:",
            with_info(self.tr_connect_timeout, "tr_connect_timeout"),
        )
        tr_box.addRow(
            "Таймаут чтения:",
            with_info(self.tr_read_timeout, "tr_read_timeout"),
        )
        tr_box.addRow(
            "Максимум ожидания:",
            with_info(self.tr_max_wait, "tr_max_wait"),
        )
        tr_box.addRow("", self.test_tr_btn)

        layout.addLayout(tr_box)
        layout.addStretch()
        return w

    # ------------------------------------------------------------------
    # Суммаризация
    # ------------------------------------------------------------------
    def _build_summarizer_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        info = QLabel(
            "Выберите, где формируется протокол/резюме (summary)."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        self.sum_enabled_check = QCheckBox(
            "Формировать summary для новых записей"
        )
        self.sum_enabled_check.setChecked(False)
        attach_tooltip(self.sum_enabled_check, "sum_enabled")
        layout.addWidget(self.sum_enabled_check)

        form_top = QFormLayout()
        self.sum_provider_combo = QComboBox()
        self.sum_provider_combo.addItem(
            "Сервер транскрибации (промпт уходит в API)", "server"
        )
        self.sum_provider_combo.addItem(
            "LiteLLM (OpenAI-совместимый API)", "litellm"
        )
        form_top.addRow(
            "Провайдер:",
            with_info(self.sum_provider_combo, "sum_provider"),
        )
        layout.addLayout(form_top)

        lite_header = QHBoxLayout()
        lite_header.addWidget(QLabel("<b>LiteLLM</b>"))
        lite_header.addStretch()
        lite_header.addWidget(make_info_icon("sum_litellm"))
        layout.addLayout(lite_header)

        lite_form = QFormLayout()

        self.sum_litellm_url = QLineEdit()
        self.sum_litellm_url.setPlaceholderText(
            "http://localhost:4000"
        )

        self.sum_litellm_key = QLineEdit()
        self.sum_litellm_key.setEchoMode(QLineEdit.EchoMode.Password)

        self.sum_show_key = QCheckBox("Показать ключ")
        self.sum_show_key.toggled.connect(
            lambda checked: self.toggle_password_visibility(
                self.sum_litellm_key, self.sum_show_key
            )
        )

        self.sum_litellm_model = QLineEdit()
        self.sum_litellm_model.setPlaceholderText("gpt-4o-mini")

        self.sum_litellm_temperature = QDoubleSpinBox()
        self.sum_litellm_temperature.setRange(0.0, 2.0)
        self.sum_litellm_temperature.setSingleStep(0.05)
        self.sum_litellm_temperature.setDecimals(2)

        self.sum_litellm_max_tokens = QSpinBox()
        self.sum_litellm_max_tokens.setRange(64, 32000)

        self.sum_litellm_connect_timeout = QSpinBox()
        self.sum_litellm_connect_timeout.setRange(1, 600)
        self.sum_litellm_connect_timeout.setSuffix(" сек")

        self.sum_litellm_read_timeout = QSpinBox()
        self.sum_litellm_read_timeout.setRange(10, 3600)
        self.sum_litellm_read_timeout.setSuffix(" сек")

        self.sum_litellm_system = QPlainTextEdit()
        self.sum_litellm_system.setMinimumHeight(120)

        self.sum_test_btn = QPushButton("Проверить подключение")
        self.sum_test_btn.clicked.connect(
            self._test_summarizer_connection
        )

        lite_form.addRow(
            "Base URL:",
            with_info(self.sum_litellm_url, "sum_litellm_url"),
        )
        lite_form.addRow(
            "API key:",
            with_info(self.sum_litellm_key, "sum_litellm_key"),
        )
        lite_form.addRow("", self.sum_show_key)
        lite_form.addRow(
            "Модель:",
            with_info(self.sum_litellm_model, "sum_litellm_model"),
        )
        lite_form.addRow(
            "Temperature:",
            with_info(
                self.sum_litellm_temperature,
                "sum_litellm_temperature",
            ),
        )
        lite_form.addRow(
            "Max tokens:",
            with_info(
                self.sum_litellm_max_tokens,
                "sum_litellm_max_tokens",
            ),
        )
        lite_form.addRow(
            "Таймаут соединения:",
            with_info(
                self.sum_litellm_connect_timeout,
                "sum_litellm_connect_timeout",
            ),
        )
        lite_form.addRow(
            "Таймаут чтения:",
            with_info(
                self.sum_litellm_read_timeout,
                "sum_litellm_read_timeout",
            ),
        )
        lite_form.addRow(
            "System prompt:",
            with_info(self.sum_litellm_system, "sum_litellm_system"),
        )
        lite_form.addRow("", self.sum_test_btn)

        layout.addLayout(lite_form)
        layout.addStretch()
        return w

    def _test_summarizer_connection(self) -> None:
        base_url = self.sum_litellm_url.text().strip()
        api_key = self.sum_litellm_key.text()
        model = self.sum_litellm_model.text().strip() or "gpt-4o-mini"

        if not base_url:
            QMessageBox.warning(
                self, "Суммаризация",
                "Не задан base_url для LiteLLM",
            )
            return

        from .litellm_client import LiteLLMClient, LiteLLMError

        connect_timeout = float(
            self.sum_litellm_connect_timeout.value()
        )
        read_timeout = float(self.sum_litellm_read_timeout.value())

        QGuiApplication.setOverrideCursor(
            Qt.CursorShape.WaitCursor
        )

        async def _run_ping():
            async with LiteLLMClient(
                base_url=base_url,
                api_key=api_key,
                model=model,
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
            ) as client:
                return await client.ping(timeout=8.0)

        method = ""
        try:
            method = asyncio.run(_run_ping())
        except LiteLLMError as exc:
            log.warning("Тест LiteLLM провален: %s", exc)
            QMessageBox.warning(
                self, "Суммаризация", f"Ошибка: {exc}"
            )
            return
        except Exception as exc:
            log.exception(
                "Тест LiteLLM: неожиданная ошибка: %s", exc
            )
            QMessageBox.warning(
                self, "Суммаризация", f"Ошибка: {exc}"
            )
            return
        finally:
            if QGuiApplication.overrideCursor() is not None:
                QGuiApplication.restoreOverrideCursor()

        if method == "models":
            msg = (
                "Подключение успешно.\n\n"
                "Проверено через GET /v1/models."
            )
        else:
            msg = (
                "Подключение успешно.\n\n"
                "Сервер не поддерживает /v1/models — проверено "
                "коротким запросом генерации."
            )

        QMessageBox.information(self, "Суммаризация", msg)

    # ------------------------------------------------------------------
    # Глоссарий
    # ------------------------------------------------------------------
    def _build_glossary_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        info = QLabel(
            "Список терминов и аббревиатур, которые будут "
            "переданы ИИ."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        terms_header = QHBoxLayout()
        terms_header.addWidget(QLabel("<b>Термины</b>"))
        terms_header.addStretch()
        terms_header.addWidget(make_info_icon("glossary_terms"))
        layout.addLayout(terms_header)

        self.glossary_table = QTableWidget(0, 2)
        self.glossary_table.setHorizontalHeaderLabels(
            ["Термин", "Пояснение"]
        )
        self.glossary_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        header = self.glossary_table.horizontalHeader()
        header.setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.glossary_table.setMinimumHeight(240)
        layout.addWidget(self.glossary_table)

        g_btns = QHBoxLayout()
        g_add_btn = QPushButton("Добавить")
        g_add_btn.clicked.connect(self._glossary_add)
        g_del_btn = QPushButton("Удалить")
        g_del_btn.clicked.connect(self._glossary_delete)
        g_up_btn = QPushButton("Вверх")
        g_up_btn.clicked.connect(lambda: self._glossary_move(-1))
        g_down_btn = QPushButton("Вниз")
        g_down_btn.clicked.connect(lambda: self._glossary_move(1))
        g_btns.addWidget(g_add_btn)
        g_btns.addWidget(g_del_btn)
        g_btns.addWidget(g_up_btn)
        g_btns.addWidget(g_down_btn)
        g_btns.addStretch()
        layout.addLayout(g_btns)

        send_header = QHBoxLayout()
        send_header.addWidget(
            QLabel("<b>Куда передавать глоссарий</b>")
        )
        send_header.addStretch()
        send_header.addWidget(
            make_info_icon("glossary_send_to_summarizer")
        )
        layout.addLayout(send_header)

        self.glossary_send_to_summarizer_check = QCheckBox(
            "Передавать в суммаризацию"
        )
        attach_tooltip(
            self.glossary_send_to_summarizer_check,
            "glossary_send_to_summarizer",
        )
        layout.addWidget(self.glossary_send_to_summarizer_check)

        self.glossary_send_to_deepseek_check = QCheckBox(
            "Передавать в файл промпта DeepSeek"
        )
        attach_tooltip(
            self.glossary_send_to_deepseek_check,
            "glossary_send_to_deepseek",
        )
        layout.addWidget(self.glossary_send_to_deepseek_check)

        layout.addStretch()
        return w

    def _glossary_add(self) -> None:
        row = self.glossary_table.rowCount()
        self.glossary_table.insertRow(row)
        self.glossary_table.setItem(row, 0, QTableWidgetItem(""))
        self.glossary_table.setItem(row, 1, QTableWidgetItem(""))
        self.glossary_table.editItem(
            self.glossary_table.item(row, 0)
        )

    def _glossary_delete(self) -> None:
        row = self.glossary_table.currentRow()
        if row < 0:
            return
        item = self.glossary_table.item(row, 0)
        term = item.text() if item else ""
        if QMessageBox.question(
            self, "Удалить термин",
            f"Удалить термин «{term}» из глоссария?",
        ) == QMessageBox.StandardButton.Yes:
            self.glossary_table.removeRow(row)

    def _glossary_move(self, delta: int) -> None:
        row = self.glossary_table.currentRow()
        if row < 0:
            return
        new_row = row + delta
        if new_row < 0 or new_row >= self.glossary_table.rowCount():
            return
        for col in range(self.glossary_table.columnCount()):
            a = self.glossary_table.takeItem(row, col)
            b = self.glossary_table.takeItem(new_row, col)
            self.glossary_table.setItem(row, col, b)
            self.glossary_table.setItem(new_row, col, a)
        self.glossary_table.setCurrentCell(new_row, 0)

    # ------------------------------------------------------------------
    # Запись
    # ------------------------------------------------------------------
    def _build_recording_tab(self) -> QWidget:
        w = QWidget()
        layout = QFormLayout(w)

        self.monitor_combo = QComboBox()
        for m in get_system_monitors():
            self.monitor_combo.addItem(
                f"{m['name']} ({m['width']}x{m['height']})",
                m["display"],
            )

        self.mic_check = QCheckBox("Записывать микрофон")
        attach_tooltip(self.mic_check, "mic")

        self.watermark_check = QCheckBox(
            "Наложение водяного знака"
        )
        attach_tooltip(self.watermark_check, "watermark")

        self.hotkey_start_input = QLineEdit()
        self.hotkey_start_input.setPlaceholderText("Ctrl+Shift+R")

        self.hotkey_stop_input = QLineEdit()
        self.hotkey_stop_input.setPlaceholderText("Ctrl+Shift+S")

        self.metadata_on_start_check = QCheckBox(
            "Показывать окно метаданных при старте записи"
        )
        attach_tooltip(
            self.metadata_on_start_check, "metadata_on_start"
        )

        self.metadata_on_stop_check = QCheckBox(
            "Показывать окно метаданных при остановке записи"
        )
        attach_tooltip(
            self.metadata_on_stop_check, "metadata_on_stop"
        )

        self.overlay_panel_check = QCheckBox(
            "Показывать плавающую панель управления"
        )
        attach_tooltip(self.overlay_panel_check, "overlay_panel")

        self.start_notification_check = QCheckBox(
            "Показывать уведомление о начале записи"
        )
        attach_tooltip(
            self.start_notification_check, "start_notification"
        )

        layout.addRow(
            "Монитор:",
            with_info(self.monitor_combo, "monitor"),
        )
        layout.addRow("", self.mic_check)
        layout.addRow("", self.watermark_check)
        layout.addRow(
            "Горячая клавиша старт/пауза:",
            with_info(self.hotkey_start_input, "hotkey_start"),
        )
        layout.addRow(
            "Горячая клавиша стоп:",
            with_info(self.hotkey_stop_input, "hotkey_stop"),
        )
        layout.addRow("", self.metadata_on_start_check)
        layout.addRow("", self.metadata_on_stop_check)
        layout.addRow("", self.overlay_panel_check)
        layout.addRow("", self.start_notification_check)

        self.ffmpeg_start_check_delay = QDoubleSpinBox()
        self.ffmpeg_start_check_delay.setRange(0.05, 5.0)
        self.ffmpeg_start_check_delay.setSingleStep(0.05)
        self.ffmpeg_start_check_delay.setDecimals(2)
        self.ffmpeg_start_check_delay.setSuffix(" сек")
        self.ffmpeg_start_check_delay.setValue(0.3)

        self.ffmpeg_stop_timeout = QSpinBox()
        self.ffmpeg_stop_timeout.setRange(1, 120)
        self.ffmpeg_stop_timeout.setSuffix(" сек")
        self.ffmpeg_stop_timeout.setValue(10)

        self.ffmpeg_kill_timeout = QSpinBox()
        self.ffmpeg_kill_timeout.setRange(1, 60)
        self.ffmpeg_kill_timeout.setSuffix(" сек")
        self.ffmpeg_kill_timeout.setValue(5)

        layout.addRow(
            "Задержка проверки ffmpeg:",
            with_info(
                self.ffmpeg_start_check_delay,
                "app_ffmpeg_start_check_delay",
            ),
        )
        layout.addRow(
            "Таймаут остановки ffmpeg:",
            with_info(
                self.ffmpeg_stop_timeout,
                "app_ffmpeg_stop_timeout",
            ),
        )
        layout.addRow(
            "Таймаут SIGKILL:",
            with_info(
                self.ffmpeg_kill_timeout,
                "app_ffmpeg_kill_timeout",
            ),
        )

        self.overlay_hide_delay_ms = QSpinBox()
        self.overlay_hide_delay_ms.setRange(0, 60000)
        self.overlay_hide_delay_ms.setSuffix(" мс")
        self.overlay_hide_delay_ms.setSingleStep(500)
        self.overlay_hide_delay_ms.setValue(5000)

        self.overlay_log_lines = QSpinBox()
        self.overlay_log_lines.setRange(1, 50)
        self.overlay_log_lines.setValue(5)

        layout.addRow(
            "Панель: скрывать через:",
            with_info(
                self.overlay_hide_delay_ms,
                "app_overlay_hide_delay_ms",
            ),
        )
        layout.addRow(
            "Панель: строк лога:",
            with_info(
                self.overlay_log_lines,
                "app_overlay_log_lines",
            ),
        )

        return w

    # ------------------------------------------------------------------
    # Очередь
    # ------------------------------------------------------------------
    def _build_queue_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        info = QLabel(
            "Настройки автоматической обработки очереди."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        form = QFormLayout()
        self.auto_retry_check = QCheckBox(
            "Автоматически повторять неудачные задачи"
        )
        attach_tooltip(self.auto_retry_check, "auto_retry")

        self.retry_interval_spin = QSpinBox()
        self.retry_interval_spin.setRange(1, 24 * 60)
        self.retry_interval_spin.setSuffix(" мин")
        self.retry_interval_spin.setValue(5)

        self.max_retries_spin = QSpinBox()
        self.max_retries_spin.setRange(1, 100)
        self.max_retries_spin.setValue(10)

        self.pause_when_recording_check = QCheckBox(
            "Не обрабатывать очередь во время записи"
        )
        attach_tooltip(
            self.pause_when_recording_check,
            "queue_pause_when_recording",
        )
        self.pause_when_recording_check.setChecked(True)

        form.addRow("", self.auto_retry_check)
        form.addRow(
            "Интервал проверки:",
            with_info(self.retry_interval_spin, "retry_interval"),
        )
        form.addRow(
            "Максимум попыток:",
            with_info(self.max_retries_spin, "max_retries"),
        )
        form.addRow("", self.pause_when_recording_check)

        layout.addLayout(form)
        layout.addStretch()
        return w

    # ------------------------------------------------------------------
    # Скрам
    # ------------------------------------------------------------------
    def _build_scrum_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        info = QLabel(
            "Шаблон промпта для DeepSeek и формат файла."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        scrum_header = QHBoxLayout()
        scrum_header.addWidget(QLabel("<b>Шаблон промпта</b>"))
        scrum_header.addStretch()
        scrum_header.addWidget(make_info_icon("scrum_template"))
        layout.addLayout(scrum_header)

        self.scrum_template_edit = QPlainTextEdit()
        self.scrum_template_edit.setMinimumHeight(260)
        layout.addWidget(self.scrum_template_edit)

        form = QFormLayout()
        self.scrum_format_combo = QComboBox()
        self.scrum_format_combo.addItems(["docx", "md", "txt"])
        form.addRow(
            "Формат экспорта:",
            with_info(self.scrum_format_combo, "scrum_format"),
        )
        layout.addLayout(form)

        layout.addStretch()
        return w

    # ------------------------------------------------------------------
    # Форматы и сжатие
    # ------------------------------------------------------------------
    def _build_compression_tab(self) -> QWidget:
        w = QWidget()
        layout = QFormLayout(w)

        self.audio_fmt_combo = QComboBox()
        self.audio_fmt_combo.addItems(["mp3", "aac", "wav", "opus"])

        self.audio_bitrate_spin = QSpinBox()
        self.audio_bitrate_spin.setRange(32, 512)
        self.audio_bitrate_spin.setSuffix(" kbps")

        self.video_bitrate_spin = QSpinBox()
        self.video_bitrate_spin.setRange(500, 20000)
        self.video_bitrate_spin.setSuffix(" kbps")

        self.compression_spin = QSpinBox()
        self.compression_spin.setRange(0, 9)

        layout.addRow(
            "Формат аудио:",
            with_info(self.audio_fmt_combo, "audio_format"),
        )
        layout.addRow(
            "Битрейт аудио:",
            with_info(self.audio_bitrate_spin, "audio_bitrate"),
        )
        layout.addRow(
            "Битрейт видео:",
            with_info(self.video_bitrate_spin, "video_bitrate"),
        )
        layout.addRow(
            "Уровень сжатия:",
            with_info(self.compression_spin, "compression_level"),
        )
        return w

    # ------------------------------------------------------------------
    # Хранилище
    # ------------------------------------------------------------------
    def _build_storage_tab(self) -> QWidget:
        w = QWidget()
        layout = QFormLayout(w)

        self.temp_path_input = QLineEdit()

        self.browse_btn = QPushButton("Обзор...")
        self.browse_btn.clicked.connect(self.browse_folder)

        temp_path_container = QWidget()
        temp_path_row = QHBoxLayout(temp_path_container)
        temp_path_row.setContentsMargins(0, 0, 0, 0)
        temp_path_row.setSpacing(6)
        temp_path_row.addWidget(self.temp_path_input, 1)
        temp_path_row.addWidget(self.browse_btn, 0)
        temp_path_icon = make_info_icon("temp_path")
        if temp_path_icon is not None:
            temp_path_row.addWidget(temp_path_icon, 0)

        self.retention_spin = QSpinBox()
        self.retention_spin.setRange(1, 24 * 30)
        self.retention_spin.setSuffix(" ч")

        layout.addRow("Временная папка:", temp_path_container)
        layout.addRow(
            "Хранить (часов):",
            with_info(self.retention_spin, "retention_hours"),
        )
        return w

    # ------------------------------------------------------------------
    # Логи
    # ------------------------------------------------------------------
    def _build_logging_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        form = QFormLayout()
        self.log_path_input = QLineEdit()

        browse_log_btn = QPushButton("Обзор...")
        browse_log_btn.clicked.connect(self.browse_log_file)

        log_path_container = QWidget()
        log_path_row = QHBoxLayout(log_path_container)
        log_path_row.setContentsMargins(0, 0, 0, 0)
        log_path_row.setSpacing(6)
        log_path_row.addWidget(self.log_path_input, 1)
        log_path_row.addWidget(browse_log_btn, 0)
        log_path_icon = make_info_icon("log_path")
        if log_path_icon is not None:
            log_path_row.addWidget(log_path_icon, 0)
        form.addRow("Путь к логу:", log_path_container)

        self.log_level_combo = QComboBox()
        self.log_level_combo.addItems(
            ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        )
        form.addRow(
            "Уровень логирования:",
            with_info(self.log_level_combo, "log_level"),
        )

        self.log_max_bytes_spin = QSpinBox()
        self.log_max_bytes_spin.setRange(1, 1024)
        self.log_max_bytes_spin.setSuffix(" МБ")
        self.log_max_bytes_spin.setValue(10)
        form.addRow(
            "Максимальный размер лога:",
            with_info(self.log_max_bytes_spin, "log_max_bytes_mb"),
        )

        self.log_backup_count_spin = QSpinBox()
        self.log_backup_count_spin.setRange(1, 100)
        self.log_backup_count_spin.setValue(5)
        form.addRow(
            "Сколько архивов хранить:",
            with_info(
                self.log_backup_count_spin, "log_backup_count"
            ),
        )

        layout.addLayout(form)

        actions = QHBoxLayout()
        self.open_log_btn = QPushButton("Открыть лог")
        self.open_log_btn.clicked.connect(self.open_log_file)
        self.open_log_dir_btn = QPushButton("Открыть папку")
        self.open_log_dir_btn.clicked.connect(self.open_log_folder)
        self.clear_log_btn = QPushButton("Очистить лог")
        self.clear_log_btn.clicked.connect(self.clear_log_file)
        actions.addWidget(self.open_log_btn)
        actions.addWidget(self.open_log_dir_btn)
        actions.addWidget(self.clear_log_btn)
        actions.addStretch()
        layout.addLayout(actions)

        current = get_current_log_path()
        self.current_log_label = QLabel(
            f"<span style='color: gray;'>Текущий логгер пишет в:</span> "
            f"<code>{current}</code>"
        )
        self.current_log_label.setWordWrap(True)
        layout.addWidget(self.current_log_label)
        layout.addStretch()
        return w

    # ------------------------------------------------------------------
    # Загрузка/сохранение
    # ------------------------------------------------------------------
    def load_settings(self) -> None:
        cfg = self.config_manager.config
        log.debug("Загрузка настроек в окно")

        # --- Проекты ---
        projects = self.config_manager.get_projects()
        self.projects_table.setRowCount(0)
        for p in projects:
            row = self.projects_table.rowCount()
            self.projects_table.insertRow(row)
            self.projects_table.setItem(
                row, 0, QTableWidgetItem(p["name"])
            )
            self.projects_table.setItem(
                row, 1, QTableWidgetItem(p["chat_id"])
            )

        # --- Проект и чат по умолчанию ---
        default_project = self.config_manager.get_default_project()
        self._refresh_default_project_combo(current=default_project)
        self.default_chat_id_input.setText(
            self.config_manager.get_default_chat_id()
        )

        # --- Теги ---
        tags = self.config_manager.get_tags()
        self.tags_table.setRowCount(0)
        for t in tags:
            row = self.tags_table.rowCount()
            self.tags_table.insertRow(row)
            name_item = QTableWidgetItem(t.get("name", ""))
            self.tags_table.setItem(row, 0, name_item)

            color = (t.get("color") or "").strip()
            color_item = QTableWidgetItem(color)
            color_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
            )
            if color:
                try:
                    qcolor = QColor(color)
                    if qcolor.isValid():
                        color_item.setBackground(qcolor)
                        name_item.setForeground(
                            QColor("#FFFFFF")
                            if qcolor.lightness() < 128
                            else QColor("#000000")
                        )
                except Exception:
                    pass
            self.tags_table.setItem(row, 1, color_item)

        # --- Сотрудники ---
        employees = self.config_manager.get_employees()
        self.employees_table.setRowCount(0)
        for e in employees:
            row = self.employees_table.rowCount()
            self.employees_table.insertRow(row)
            self.employees_table.setItem(
                row, 0, QTableWidgetItem(e["name"])
            )
            self.employees_table.setItem(
                row, 1, QTableWidgetItem(e["chat_id"])
            )

        # --- Bitrix24 ---
        bitrix = self.config_manager.get_bitrix_settings()
        self.bitrix_enabled_check.setChecked(bitrix["enabled"])
        self.bitrix_webhook_input.setText(bitrix["webhook_url"])
        self.bitrix_connect_timeout.setValue(
            bitrix["connect_timeout"]
        )
        self.bitrix_read_timeout.setValue(bitrix["read_timeout"])
        idx = self.bitrix_default_send_combo.findData(
            bitrix["default_send"]
        )
        if idx >= 0:
            self.bitrix_default_send_combo.setCurrentIndex(idx)
        self.bitrix_header_check.setChecked(bitrix["include_header"])
        self.bitrix_system_check.setChecked(bitrix["system_message"])
        self.bitrix_no_preview_check.setChecked(
            bitrix["disable_url_preview"]
        )
        self.bitrix_file_threshold.setValue(
            int(bitrix.get("file_message_max_chars", 3000))
        )
        self.bitrix_upload_folder.setValue(
            int(bitrix.get("upload_folder_id", 0))
        )
        self.bitrix_max_message_chars.setValue(
            int(bitrix.get("max_message_chars", 15000))
        )
        self.bitrix_retry_count.setValue(
            int(bitrix.get("retry_count", 3))
        )
        self.bitrix_retry_delay.setValue(
            float(bitrix.get("retry_delay", 2.0))
        )

        # --- Промпты ---
        prompts = self.config_manager.get_prompts()
        self.prompts_table.setRowCount(0)
        for p in prompts:
            row = self.prompts_table.rowCount()
            self.prompts_table.insertRow(row)
            self.prompts_table.setItem(
                row, 0, QTableWidgetItem(p.get("name", ""))
            )
            self.prompts_table.setItem(
                row, 1, QTableWidgetItem(p.get("text", ""))
            )

        self.default_prompt_edit.setPlainText(
            cfg.get("metadata", {}).get("default_prompt", "")
        )

        # --- Шаблоны названий ---
        name_tpls = self.config_manager.get_name_templates()
        if not name_tpls:
            name_tpls = list(DEFAULT_NAME_TEMPLATES)
        self.name_templates_table.setRowCount(0)
        for tpl in name_tpls:
            row = self.name_templates_table.rowCount()
            self.name_templates_table.insertRow(row)
            self.name_templates_table.setItem(
                row, 0, QTableWidgetItem(tpl.get("label", ""))
            )
            self.name_templates_table.setItem(
                row, 1, QTableWidgetItem(tpl.get("template", ""))
            )

        # --- Транскрибация ---
        tr = cfg.get("transcribe", {})
        self.tr_url_input.setText(tr.get("url", ""))
        self.tr_key_input.setText(tr.get("access_key", ""))
        self.tr_connect_timeout.setValue(
            int(tr.get("connect_timeout", 15))
        )
        self.tr_read_timeout.setValue(int(tr.get("read_timeout", 120)))
        self.tr_max_wait.setValue(int(tr.get("max_wait", 7200)))

        # --- Суммаризация ---
        sum_cfg = self.config_manager.get_summarizer_settings()
        self.sum_enabled_check.setChecked(
            bool(sum_cfg.get("enabled", False))
        )
        idx = self.sum_provider_combo.findData(sum_cfg["provider"])
        if idx >= 0:
            self.sum_provider_combo.setCurrentIndex(idx)

        l = sum_cfg["litellm"]
        self.sum_litellm_url.setText(l["base_url"])
        self.sum_litellm_key.setText(l["api_key"])
        self.sum_litellm_model.setText(l["model"])
        self.sum_litellm_temperature.setValue(
            float(l["temperature"])
        )
        self.sum_litellm_max_tokens.setValue(int(l["max_tokens"]))
        self.sum_litellm_connect_timeout.setValue(
            int(l["connect_timeout"])
        )
        self.sum_litellm_read_timeout.setValue(
            int(l["read_timeout"])
        )
        self.sum_litellm_system.setPlainText(l["system_prompt"])

        # --- Глоссарий ---
        g = self.config_manager.get_glossary_settings()
        self.glossary_table.setRowCount(0)
        for item in g["terms"]:
            row = self.glossary_table.rowCount()
            self.glossary_table.insertRow(row)
            self.glossary_table.setItem(
                row, 0, QTableWidgetItem(item.get("term", ""))
            )
            self.glossary_table.setItem(
                row, 1,
                QTableWidgetItem(item.get("description", "")),
            )
        self.glossary_send_to_summarizer_check.setChecked(
            bool(g["send_to_summarizer"])
        )
        self.glossary_send_to_deepseek_check.setChecked(
            bool(g["send_to_deepseek"])
        )

        # --- Запись ---
        rec = cfg.get("recording", {})
        idx = int(rec.get("monitor", 0))
        if 0 <= idx < self.monitor_combo.count():
            self.monitor_combo.setCurrentIndex(idx)
        self.mic_check.setChecked(
            bool(rec.get("with_microphone", True))
        )
        self.watermark_check.setChecked(
            bool(rec.get("show_watermark", True))
        )
        self.hotkey_start_input.setText(
            rec.get("hotkey_start", "Ctrl+Shift+R")
        )
        self.hotkey_stop_input.setText(
            rec.get("hotkey_stop", "Ctrl+Shift+S")
        )
        self.metadata_on_start_check.setChecked(
            bool(rec.get("show_metadata_on_start", True))
        )
        self.metadata_on_stop_check.setChecked(
            bool(rec.get("show_metadata_on_stop", True))
        )
        self.overlay_panel_check.setChecked(
            bool(rec.get("show_overlay_panel", True))
        )
        self.start_notification_check.setChecked(
            bool(rec.get("show_start_notification", True))
        )

        # --- app ---
        app_cfg = cfg.get("app", {})
        self.ffmpeg_start_check_delay.setValue(
            float(app_cfg.get("ffmpeg_start_check_delay", 0.3))
        )
        self.ffmpeg_stop_timeout.setValue(
            int(app_cfg.get("ffmpeg_stop_timeout", 10))
        )
        self.ffmpeg_kill_timeout.setValue(
            int(app_cfg.get("ffmpeg_kill_timeout", 5))
        )
        self.overlay_hide_delay_ms.setValue(
            int(app_cfg.get("overlay_hide_delay_ms", 5000))
        )
        self.overlay_log_lines.setValue(
            int(app_cfg.get("overlay_log_lines", 5))
        )

        # --- Очередь ---
        q = cfg.get("queue", {})
        self.auto_retry_check.setChecked(
            bool(q.get("auto_retry_enabled", True))
        )
        self.retry_interval_spin.setValue(
            int(q.get("retry_interval_minutes", 5))
        )
        self.max_retries_spin.setValue(
            int(q.get("max_retries", 10))
        )
        self.pause_when_recording_check.setChecked(
            bool(q.get("pause_when_recording", True))
        )

        # --- Скрам ---
        scrum = cfg.get("scrum", {})
        from .config_manager import DEFAULT_SCRUM_PROMPT
        self.scrum_template_edit.setPlainText(
            scrum.get("prompt_template", DEFAULT_SCRUM_PROMPT)
        )
        fmt = scrum.get("export_format", "docx")
        i = self.scrum_format_combo.findText(fmt)
        if i >= 0:
            self.scrum_format_combo.setCurrentIndex(i)

        # --- ВМ Yandex ---
        ycfg = cfg.get("yandex_vm", {}) or {}
        self.yandex_vm_root_input.setText(
            str(ycfg.get("root_path", "") or "")
        )

        # --- Сжатие ---
        comp = cfg.get("compression", {})
        self.audio_fmt_combo.setCurrentText(
            comp.get("audio_format", "mp3")
        )
        self.audio_bitrate_spin.setValue(
            int(comp.get("audio_bitrate", 192))
        )
        self.video_bitrate_spin.setValue(
            int(comp.get("video_bitrate", 4000))
        )
        self.compression_spin.setValue(
            int(comp.get("compression_level", 5))
        )

        # --- Хранилище ---
        st = cfg.get("storage", {})
        self.temp_path_input.setText(
            st.get("temp_path", "/tmp/screen-recorder")
        )
        self.retention_spin.setValue(
            int(st.get("retention_hours", 24))
        )

        # --- Логи ---
        logging_cfg = cfg.get("logging", {})
        self.log_path_input.setText(
            logging_cfg.get(
                "log_path", "/tmp/screen-recorder/app.log"
            )
        )
        level = logging_cfg.get("level", "DEBUG")
        i = self.log_level_combo.findText(level)
        if i >= 0:
            self.log_level_combo.setCurrentIndex(i)
        self.log_max_bytes_spin.setValue(
            int(logging_cfg.get("max_bytes_mb", 10))
        )
        self.log_backup_count_spin.setValue(
            int(logging_cfg.get("backup_count", 5))
        )

        log.info(
            "Настройки загружены в окно: projects=%d, "
            "default_project=%r, default_chat_id=%r, "
            "tags=%d, employees=%d, prompts=%d",
            len(projects), default_project,
            self.config_manager.get_default_chat_id(),
            len(tags), len(employees), len(prompts),
        )

    def _apply_form_to_config(self) -> Dict[str, Any]:
        """Собирает значения из формы в self.config_manager.config."""
        cfg = self.config_manager.config

        # --- Проекты ---
        projects: List[Dict[str, str]] = []
        for row in range(self.projects_table.rowCount()):
            name_item = self.projects_table.item(row, 0)
            chat_item = self.projects_table.item(row, 1)
            name = name_item.text().strip() if name_item else ""
            chat_id = chat_item.text().strip() if chat_item else ""
            if not name:
                continue
            projects.append({"name": name, "chat_id": chat_id})
        cfg["projects"] = projects

        cfg["yandex_vm"] = {
            "root_path": self.yandex_vm_root_input.text().strip(),
        }

        # --- Проект и чат по умолчанию ---
        default_project = (
            self.default_project_combo.currentData() or ""
        )
        cfg["default_project"] = str(default_project).strip()
        cfg["default_chat_id"] = (
            self.default_chat_id_input.text().strip()
        )

        # --- Теги ---
        tags: List[Dict[str, str]] = []
        seen = set()
        for row in range(self.tags_table.rowCount()):
            name_item = self.tags_table.item(row, 0)
            color_item = self.tags_table.item(row, 1)
            name = name_item.text().strip() if name_item else ""
            color = color_item.text().strip() if color_item else ""
            if not name or name in seen:
                continue
            tags.append({"name": name, "color": color})
            seen.add(name)
        cfg["tags"] = tags

        # --- Сотрудники ---
        employees: List[Dict[str, str]] = []
        for row in range(self.employees_table.rowCount()):
            name_item = self.employees_table.item(row, 0)
            chat_item = self.employees_table.item(row, 1)
            name = name_item.text().strip() if name_item else ""
            chat_id = chat_item.text().strip() if chat_item else ""
            if not name:
                continue
            employees.append({"name": name, "chat_id": chat_id})
        cfg["employees"] = employees

        # --- Bitrix24 ---
        cfg["bitrix"] = {
            "enabled": self.bitrix_enabled_check.isChecked(),
            "webhook_url": self.bitrix_webhook_input.text().strip(),
            "connect_timeout": int(
                self.bitrix_connect_timeout.value()
            ),
            "read_timeout": int(self.bitrix_read_timeout.value()),
            "default_send": (
                self.bitrix_default_send_combo.currentData()
                or "protocol"
            ),
            "include_header": self.bitrix_header_check.isChecked(),
            "system_message": self.bitrix_system_check.isChecked(),
            "disable_url_preview": (
                self.bitrix_no_preview_check.isChecked()
            ),
            "file_message_max_chars": int(
                self.bitrix_file_threshold.value()
            ),
            "upload_folder_id": int(
                self.bitrix_upload_folder.value()
            ),
            "max_message_chars": int(
                self.bitrix_max_message_chars.value()
            ),
            "retry_count": int(self.bitrix_retry_count.value()),
            "retry_delay": float(self.bitrix_retry_delay.value()),
        }

        # --- Промпты ---
        prompts: List[dict] = []
        for row in range(self.prompts_table.rowCount()):
            name_item = self.prompts_table.item(row, 0)
            text_item = self.prompts_table.item(row, 1)
            name = name_item.text().strip() if name_item else ""
            text = text_item.text().strip() if text_item else ""
            if not name and not text:
                continue
            if not name:
                name = f"Промпт {row + 1}"
            prompts.append({"name": name, "text": text})

        # --- Шаблоны названий ---
        name_templates: List[dict] = []
        for row in range(self.name_templates_table.rowCount()):
            label_item = self.name_templates_table.item(row, 0)
            tpl_item = self.name_templates_table.item(row, 1)
            label = label_item.text().strip() if label_item else ""
            template = tpl_item.text().strip() if tpl_item else ""
            if not template:
                continue
            if not label:
                label = template
            name_templates.append(
                {"label": label, "template": template}
            )

        meta_cfg = cfg.setdefault("metadata", {})
        meta_cfg["prompts"] = prompts
        meta_cfg["default_prompt"] = (
            self.default_prompt_edit.toPlainText().strip()
        )
        meta_cfg["name_templates"] = name_templates

        cfg["transcribe"] = {
            "url": self.tr_url_input.text().strip(),
            "access_key": self.tr_key_input.text(),
            "connect_timeout": self.tr_connect_timeout.value(),
            "read_timeout": self.tr_read_timeout.value(),
            "max_wait": self.tr_max_wait.value(),
        }
        cfg["recording"] = {
            "monitor": self.monitor_combo.currentIndex(),
            "with_microphone": self.mic_check.isChecked(),
            "show_watermark": self.watermark_check.isChecked(),
            "hotkey_start": (
                self.hotkey_start_input.text().strip()
                or "Ctrl+Shift+R"
            ),
            "hotkey_stop": (
                self.hotkey_stop_input.text().strip()
                or "Ctrl+Shift+S"
            ),
            "show_metadata_on_start": (
                self.metadata_on_start_check.isChecked()
            ),
            "show_metadata_on_stop": (
                self.metadata_on_stop_check.isChecked()
            ),
            "show_overlay_panel": (
                self.overlay_panel_check.isChecked()
            ),
            "show_start_notification": (
                self.start_notification_check.isChecked()
            ),
        }
        cfg["queue"] = {
            "auto_retry_enabled": (
                self.auto_retry_check.isChecked()
            ),
            "retry_interval_minutes": (
                self.retry_interval_spin.value()
            ),
            "max_retries": self.max_retries_spin.value(),
            "pause_when_recording": (
                self.pause_when_recording_check.isChecked()
            ),
        }
        cfg["scrum"] = {
            "prompt_template": (
                self.scrum_template_edit.toPlainText()
            ),
            "export_format": (
                self.scrum_format_combo.currentText()
            ),
        }
        cfg["summarizer"] = {
            "enabled": self.sum_enabled_check.isChecked(),
            "provider": (
                self.sum_provider_combo.currentData() or "server"
            ),
            "litellm": {
                "base_url": (
                    self.sum_litellm_url.text().strip().rstrip("/")
                    or "http://localhost:4000"
                ),
                "api_key": self.sum_litellm_key.text(),
                "model": (
                    self.sum_litellm_model.text().strip()
                    or "gpt-4o-mini"
                ),
                "temperature": float(
                    self.sum_litellm_temperature.value()
                ),
                "max_tokens": int(
                    self.sum_litellm_max_tokens.value()
                ),
                "connect_timeout": int(
                    self.sum_litellm_connect_timeout.value()
                ),
                "read_timeout": int(
                    self.sum_litellm_read_timeout.value()
                ),
                "system_prompt": (
                    self.sum_litellm_system.toPlainText().strip()
                ),
            },
        }

        # --- Глоссарий ---
        glossary_terms: List[dict] = []
        for row in range(self.glossary_table.rowCount()):
            term_item = self.glossary_table.item(row, 0)
            desc_item = self.glossary_table.item(row, 1)
            term = term_item.text().strip() if term_item else ""
            desc = desc_item.text().strip() if desc_item else ""
            if not term:
                continue
            glossary_terms.append(
                {"term": term, "description": desc}
            )

        cfg["glossary"] = {
            "terms": glossary_terms,
            "send_to_summarizer": (
                self.glossary_send_to_summarizer_check.isChecked()
            ),
            "send_to_deepseek": (
                self.glossary_send_to_deepseek_check.isChecked()
            ),
        }

        cfg["compression"] = {
            "audio_format": self.audio_fmt_combo.currentText(),
            "audio_bitrate": self.audio_bitrate_spin.value(),
            "video_bitrate": self.video_bitrate_spin.value(),
            "compression_level": self.compression_spin.value(),
        }
        cfg["storage"] = {
            "temp_path": (
                self.temp_path_input.text().strip()
                or "/tmp/screen-recorder"
            ),
            "retention_hours": self.retention_spin.value(),
        }
        new_log_path = (
            self.log_path_input.text().strip()
            or "/tmp/screen-recorder/app.log"
        )
        cfg["logging"] = {
            "log_path": new_log_path,
            "level": self.log_level_combo.currentText(),
            "max_bytes_mb": int(
                self.log_max_bytes_spin.value()
            ),
            "backup_count": int(
                self.log_backup_count_spin.value()
            ),
        }

        # --- app ---
        app_cfg = cfg.setdefault("app", {})
        app_cfg["ffmpeg_start_check_delay"] = float(
            self.ffmpeg_start_check_delay.value()
        )
        app_cfg["ffmpeg_stop_timeout"] = int(
            self.ffmpeg_stop_timeout.value()
        )
        app_cfg["ffmpeg_kill_timeout"] = int(
            self.ffmpeg_kill_timeout.value()
        )
        app_cfg["overlay_hide_delay_ms"] = int(
            self.overlay_hide_delay_ms.value()
        )
        app_cfg["overlay_log_lines"] = int(
            self.overlay_log_lines.value()
        )

        return cfg

    def save_settings(self) -> bool:
        try:
            cfg = self._apply_form_to_config()
            self.config_manager.save(cfg)

            log.info(
                "Настройки сохранены: projects=%d, "
                "default_project=%r, default_chat_id=%r, tags=%d",
                len(cfg.get("projects", [])),
                cfg.get("default_project"),
                cfg.get("default_chat_id"),
                len(cfg.get("tags", [])),
            )

            current = get_current_log_path()
            new_log_path = cfg["logging"]["log_path"]
            if os.path.abspath(current) != os.path.abspath(
                new_log_path
            ):
                QMessageBox.information(
                    self, "Настройки",
                    "Настройки сохранены.\n\n"
                    "Путь к логу изменён — перезапустите "
                    "приложение.",
                )
            else:
                QMessageBox.information(
                    self, "Настройки", "Настройки сохранены"
                )
            return True
        except Exception as exc:
            log.exception("Ошибка сохранения настроек: %s", exc)
            QMessageBox.critical(
                self, "Ошибка",
                f"Не удалось сохранить: {exc}",
            )
            return False

    # ------------------------------------------------------------------
    # Экспорт / импорт настроек
    # ------------------------------------------------------------------
    def _export_settings(self) -> None:
        try:
            self._apply_form_to_config()
        except Exception as exc:
            log.exception(
                "Экспорт: не удалось собрать конфиг: %s", exc
            )
            QMessageBox.critical(
                self, "Экспорт настроек",
                f"Не удалось собрать настройки:\n{exc}",
            )
            return

        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        default_name = f"screen-recorder-config_{stamp}.json"
        default_path = os.path.join(
            os.path.expanduser("~"), default_name
        )

        target, _ = QFileDialog.getSaveFileName(
            self,
            "Сохранить настройки как",
            default_path,
            "JSON-файлы (*.json);;Все файлы (*)",
        )
        if not target:
            log.info("Экспорт настроек отменён пользователем")
            return

        if not target.lower().endswith(".json"):
            target += ".json"

        target = self._deduplicate_path(target)

        try:
            with open(target, "w", encoding="utf-8") as f:
                json.dump(
                    self.config_manager.config,
                    f,
                    indent=2,
                    ensure_ascii=False,
                )
            log.info("Настройки экспортированы: %s", target)
            QMessageBox.information(
                self, "Экспорт настроек",
                f"Настройки сохранены:\n{target}\n\n"
                "Файл содержит чувствительные данные — "
                "webhook, access key, API-ключи. Храните его в "
                "безопасном месте.",
            )
        except Exception as exc:
            log.exception("Ошибка экспорта настроек: %s", exc)
            QMessageBox.critical(
                self, "Экспорт настроек",
                f"Не удалось сохранить файл:\n{exc}",
            )

    @staticmethod
    def _deduplicate_path(path: str) -> str:
        if not os.path.exists(path):
            return path

        base, ext = os.path.splitext(path)
        i = 1
        while True:
            candidate = f"{base}_{i}{ext}"
            if not os.path.exists(candidate):
                return candidate
            i += 1
            if i > 10000:
                return (
                    f"{base}_{int(datetime.now().timestamp())}{ext}"
                )

    def _import_settings(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Выберите файл настроек",
            os.path.expanduser("~"),
            "JSON-файлы (*.json);;Все файлы (*)",
        )
        if not path:
            log.info("Импорт настроек отменён пользователем")
            return

        if not os.path.isfile(path):
            QMessageBox.warning(
                self, "Импорт настроек",
                f"Файл не найден:\n{path}",
            )
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                imported = json.load(f)
        except json.JSONDecodeError as exc:
            log.error("Импорт: не удалось разобрать JSON: %s", exc)
            QMessageBox.critical(
                self, "Импорт настроек",
                f"Не удалось прочитать JSON:\n{exc}",
            )
            return
        except Exception as exc:
            log.exception("Импорт: ошибка чтения файла: %s", exc)
            QMessageBox.critical(
                self, "Импорт настроек",
                f"Не удалось открыть файл:\n{exc}",
            )
            return

        if not isinstance(imported, dict):
            QMessageBox.warning(
                self, "Импорт настроек",
                "Файл не похож на конфиг приложения.",
            )
            return

        top_keys = sorted(imported.keys())
        preview_keys = ", ".join(top_keys[:20])
        if len(top_keys) > 20:
            preview_keys += f"… (+{len(top_keys) - 20})"

        reply = QMessageBox.question(
            self, "Импорт настроек",
            "<b>Импортировать настройки из файла?</b><br><br>"
            f"Файл: <code>{html.escape(path)}</code><br>"
            f"Секций верхнего уровня: <b>{len(top_keys)}</b><br>"
            f"<span style='color:#666'>Ключи: "
            f"{html.escape(preview_keys)}</span><br><br>"
            "<b>Внимание:</b> текущие настройки будут "
            "перезаписаны.<br><br>"
            "Продолжить?",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            log.info("Импорт настроек отменён")
            return

        try:
            src = self.config_manager.config_path
            if src.exists():
                stamp = datetime.now().strftime(
                    "%Y-%m-%d_%H-%M-%S"
                )
                backup = src.with_name(
                    f"{src.stem}.{stamp}.bak"
                )
                shutil.copy2(src, backup)
                log.info("Резервная копия конфига: %s", backup)
        except Exception as exc:
            log.warning(
                "Импорт: не удалось создать резервную копию: %s",
                exc,
            )

        try:
            merged = ConfigManager._merge(
                self.config_manager.get_defaults(),
                imported,
            )
        except Exception as exc:
            log.exception(
                "Импорт: не удалось смержить конфиг: %s", exc
            )
            QMessageBox.critical(
                self, "Импорт настроек",
                f"Не удалось применить настройки:\n{exc}",
            )
            return

        try:
            self.config_manager.save(merged)
            log.info(
                "Настройки импортированы: файл=%s, секций=%d",
                path, len(top_keys),
            )
        except Exception as exc:
            log.exception(
                "Импорт: не удалось сохранить конфиг: %s", exc
            )
            QMessageBox.critical(
                self, "Импорт настроек",
                f"Не удалось сохранить настройки:\n{exc}",
            )
            return

        try:
            self.load_settings()
        except Exception as exc:
            log.exception(
                "Импорт: не удалось перечитать форму: %s", exc
            )

        QMessageBox.information(
            self, "Импорт настроек",
            "Настройки успешно импортированы.\n\n"
            "Перезапустите приложение, чтобы изменения "
            "вступили в силу.",
        )

    # ------------------------------------------------------------------
    # Действия
    # ------------------------------------------------------------------
    def reset_to_defaults(self) -> None:
        log.warning("Сброс настроек к значениям по умолчанию")
        self.config_manager.config = (
            self.config_manager.get_defaults()
        )
        self.load_settings()

    def toggle_password_visibility(
        self, field: QLineEdit, toggle: QCheckBox,
    ) -> None:
        if toggle.isChecked():
            field.setEchoMode(QLineEdit.EchoMode.Normal)
        else:
            field.setEchoMode(QLineEdit.EchoMode.Password)

    def browse_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Выберите папку"
        )
        if folder:
            self.temp_path_input.setText(folder)

    def browse_log_file(self) -> None:
        current = (
            self.log_path_input.text().strip()
            or "/tmp/screen-recorder/app.log"
        )
        filename, _ = QFileDialog.getSaveFileName(
            self, "Выберите файл лога", current,
            "Log files (*.log *.txt);;All files (*)",
        )
        if filename:
            self.log_path_input.setText(filename)

    def open_log_file(self) -> None:
        path = (
            self.log_path_input.text().strip()
            or get_current_log_path()
        )
        path = os.path.expanduser(path)
        if not os.path.exists(path):
            QMessageBox.warning(
                self, "Лог", f"Файл не найден:\n{path}"
            )
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def open_log_folder(self) -> None:
        path = (
            self.log_path_input.text().strip()
            or get_current_log_path()
        )
        path = os.path.expanduser(path)
        folder = os.path.dirname(path) or "/tmp"
        if not os.path.isdir(folder):
            QMessageBox.warning(
                self, "Лог", f"Папка не найдена:\n{folder}"
            )
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def clear_log_file(self) -> None:
        path = (
            self.log_path_input.text().strip()
            or get_current_log_path()
        )
        path = os.path.expanduser(path)
        if not os.path.exists(path):
            QMessageBox.information(
                self, "Лог", f"Файл не найден:\n{path}"
            )
            return
        if QMessageBox.question(
            self, "Очистить лог",
            f"Очистить содержимое файла:\n{path}?",
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            with open(path, "w", encoding="utf-8"):
                pass
            QMessageBox.information(self, "Лог", "Файл очищен")
        except Exception as exc:
            QMessageBox.critical(
                self, "Ошибка", f"Не удалось очистить: {exc}"
            )

    def test_transcribe_connection(self) -> None:
        url = self.tr_url_input.text().strip()
        key = self.tr_key_input.text()
        connect_timeout = float(self.tr_connect_timeout.value())
        read_timeout = float(self.tr_read_timeout.value())

        if not url:
            QMessageBox.warning(
                self, "Транскрибация",
                "URL сервера пустой.",
            )
            return

        async def _run():
            async with TranscribeClient(
                base_url=url,
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
            ) as client:
                return await client.login(key)

        try:
            asyncio.run(_run())
            QMessageBox.information(
                self, "Транскрибация", "Подключение успешно"
            )
        except Exception as exc:
            QMessageBox.warning(
                self, "Транскрибация", f"Ошибка: {exc}"
            )

    # ------------------------------------------------------------------
    # Справка
    # ------------------------------------------------------------------
    _HELP_TEXTS: Dict[str, str] = {
        "ВМ Yandex": (
            "<b>ВМ Yandex</b><br><br>"
            "Корневая папка с подпапками виртуальных машин "
            "Yandex Cloud. Каждая подпапка — одна ВМ.<br><br>"
            "Внутри каждой подпапки ожидаются файлы "
            "<code>schedule.cron</code> (расписание) и "
            "<code>exceptions.txt</code> (исключения).<br><br>"
            "Открыть окно управления: <b>трей → ВМ Yandex</b>. "
            "Там можно редактировать оба файла и сразу сохранять "
            "их на диск."
        ),
        "Проекты и чаты Bitrix24": (
            "<b>Проекты и чаты Bitrix24</b><br><br>"
            "Таблица «Проект | Чат Bitrix24» — это реестр "
            "проектов и привязка каждого к конкретному чату.<br><br>"
            "<b>Блок «Проект и чат по умолчанию»:</b><br>"
            "• <b>Проект по умолчанию</b> — какой проект "
            "подставлять в карточку метаданных для новых "
            "записей.<br>"
            "• <b>Чат по умолчанию</b> — ID чата Bitrix24, куда "
            "по умолчанию отправляются протоколы и summary. "
            "Если пусто — используется чат проекта записи."
        ),
        "Теги": (
            "<b>Справочник тегов</b><br><br>"
            "Теги — произвольные метки, которыми можно помечать "
            "записи. Одна запись может иметь несколько тегов.<br><br>"
            "<b>Где используются:</b><br>"
            "• В карточке метаданных записи — блок «Теги» "
            "с чекбоксами.<br>"
            "• В фильтре раздела «Библиотека» — можно искать "
            "только среди записей с выбранным тегом.<br><br>"
            "<b>Столбцы таблицы:</b><br>"
            "• <b>Тег</b> — название. Двойной клик — редактирование.<br>"
            "• <b>Цвет</b> — HEX-код цвета (например "
            "<code>#C62828</code>). Двойной клик открывает "
            "палитру. Цвет используется для подсветки тега "
            "в интерфейсе.<br><br>"
            "Порядок тегов в таблице влияет на порядок в списке "
            "карточки метаданных."
        ),
        "Сотрудники": (
            "<b>Сотрудники</b><br><br>"
            "Справочник сотрудников с личными чатами Bitrix24."
        ),
        "Bitrix24": (
            "<b>Bitrix24 — параметры подключения</b><br><br>"
            "Параметры вебхука, таймауты, форматы сообщений."
        ),
        "Промпты и имена": (
            "<b>Промпты и шаблоны имён</b>"
        ),
        "Транскрибация": (
            "<b>Транскрибация</b>"
        ),
        "Суммаризация": (
            "<b>Суммаризация</b>"
        ),
        "Глоссарий": (
            "<b>Глоссарий терминов</b>"
        ),
        "Запись": (
            "<b>Запись экрана</b>"
        ),
        "Очередь": (
            "<b>Очередь задач</b>"
        ),
        "Скрам": (
            "<b>Скрам</b>"
        ),
        "Форматы и сжатие": (
            "<b>Форматы и сжатие</b>"
        ),
        "Хранилище": (
            "<b>Хранилище</b>"
        ),
        "Логи": (
            "<b>Логи</b>"
        ),
    }

    _HELP_DEFAULT: str = (
        "<b>Screen Recorder & Transcriber</b><br><br>"
        "Наведите курсор на иконку <b>ⓘ</b> рядом с любым полем — "
        "появится краткая справка."
    )

    def _show_help(self) -> None:
        idx = self.tabs.currentIndex()
        title = self.tabs.tabText(idx) if idx >= 0 else ""
        text = self._HELP_TEXTS.get(title, self._HELP_DEFAULT)

        dlg = QDialog(self)
        dlg.setWindowTitle(
            f"Справка — {title or 'Screen Recorder'}"
        )
        dlg.setModal(True)
        dlg.setMinimumSize(640, 520)

        layout = QVBoxLayout(dlg)

        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        browser.setHtml(text)
        layout.addWidget(browser, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Close,
            parent=dlg,
        )
        buttons.button(
            QDialogButtonBox.StandardButton.Close
        ).setText("Закрыть")
        buttons.rejected.connect(dlg.reject)
        buttons.accepted.connect(dlg.accept)
        layout.addWidget(buttons)

        dlg.exec()