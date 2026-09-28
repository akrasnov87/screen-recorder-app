"""Окно настроек с вкладками (локальный режим)."""
from __future__ import annotations

import asyncio
import os
from typing import Dict, List, Optional

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDoubleSpinBox,
    QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from .config_manager import ConfigManager, DEFAULT_NAME_TEMPLATES
from .logger import get_current_log_path, get_logger
from .tooltips import attach_tooltip, make_info_icon, with_info
from .transcribe_client import TranscribeClient
from .utils import get_system_monitors

log = get_logger(__name__)


class SettingsWindow(QDialog):
    """Модальное окно настроек."""

    def __init__(self, config: ConfigManager, parent: Optional[QWidget] = None) -> None:
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

        self.tabs.addTab(self._build_projects_tab(), "Проекты и чаты Bitrix24")
        self.tabs.addTab(self._build_bitrix_tab(), "Bitrix24")
        self.tabs.addTab(self._build_metadata_tab(), "Промпты и имена")
        self.tabs.addTab(self._build_transcribe_tab(), "Транскрибация")
        self.tabs.addTab(self._build_summarizer_tab(), "Суммаризация")
        self.tabs.addTab(self._build_glossary_tab(), "Глоссарий")
        self.tabs.addTab(self._build_recording_tab(), "Запись")
        self.tabs.addTab(self._build_queue_tab(), "Очередь")
        self.tabs.addTab(self._build_scrum_tab(), "Скрам")
        self.tabs.addTab(self._build_compression_tab(), "Форматы и сжатие")
        self.tabs.addTab(self._build_storage_tab(), "Хранилище")
        self.tabs.addTab(self._build_logging_tab(), "Логи")

        buttons = QHBoxLayout()
        self.help_btn = QPushButton("Помощь")
        self.help_btn.clicked.connect(self._show_help)
        self.reset_btn = QPushButton("Сбросить")
        self.reset_btn.clicked.connect(self.reset_to_defaults)
        self.save_btn = QPushButton("Сохранить")
        self.save_btn.clicked.connect(self.save_settings)
        self.close_btn = QPushButton("Закрыть")
        self.close_btn.clicked.connect(self.reject)

        buttons.addWidget(self.help_btn)
        buttons.addStretch()
        buttons.addWidget(self.reset_btn)
        buttons.addWidget(self.save_btn)
        buttons.addWidget(self.close_btn)
        root.addLayout(buttons)

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
            "<code>im.recent.get</code> или из URL чата в веб-интерфейсе.<br><br>"
            "Формат: <code>chat2101</code> или просто <code>2101</code>. "
            "Обычно достаточно указать число — метод <code>im.message.add</code> "
            "примет оба варианта.<br><br>"
            "Если чат не задан — отправка в Bitrix24 для этого проекта "
            "будет недоступна."
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
        self.projects_table.setMinimumHeight(320)
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

        layout.addStretch()
        return w

    def _project_add(self) -> None:
        row = self.projects_table.rowCount()
        self.projects_table.insertRow(row)
        self.projects_table.setItem(row, 0, QTableWidgetItem("Новый проект"))
        self.projects_table.setItem(row, 1, QTableWidgetItem(""))
        self.projects_table.editItem(self.projects_table.item(row, 0))
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
        log.debug("Проект перемещён: %d → %d", row, new_row)

    # ------------------------------------------------------------------
    # Bitrix24
    # ------------------------------------------------------------------
    def _build_bitrix_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        info = QLabel(
            "Настройки интеграции с Bitrix24. Используется для отправки "
            "протоколов и кратких описаний в чаты команд из окна «Записи»."
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
        self.bitrix_webhook_input.setEchoMode(QLineEdit.EchoMode.Password)
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
            with_info(self.bitrix_connect_timeout, "bitrix_connect_timeout"),
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
        self.bitrix_default_send_combo.addItem("И то и другое", "both")
        form.addRow(
            "Что отправлять по умолчанию:",
            with_info(self.bitrix_default_send_combo, "bitrix_default_send"),
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
        attach_tooltip(self.bitrix_no_preview_check, "bitrix_no_preview")
        form.addRow("", self.bitrix_no_preview_check)

        # --- Загрузка файлов ---
        files_header = QHBoxLayout()
        files_header.addWidget(QLabel("<b>Отправка файлов</b>"))
        files_header.addStretch()
        files_header.addWidget(make_info_icon("bitrix_files_section"))
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
            with_info(self.bitrix_file_threshold, "bitrix_file_threshold"),
        )

        self.bitrix_upload_folder = QSpinBox()
        self.bitrix_upload_folder.setRange(0, 999999999)
        self.bitrix_upload_folder.setValue(0)
        self.bitrix_upload_folder.setSpecialValueText("Папка чата (авто)")
        form.addRow(
            "Папка для загрузки:",
            with_info(self.bitrix_upload_folder, "bitrix_upload_folder"),
        )

        # --- Кнопка проверки ---
        self.bitrix_test_btn = QPushButton("Проверить подключение")
        self.bitrix_test_btn.clicked.connect(self._test_bitrix_connection)
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

        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

        async def _run_ping() -> str:
            async with Bitrix24Client(
                webhook_url=webhook,
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
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
            "Библиотека промптов и шаблоны названий записи. "
            "Промпты используются в диалоге метаданных, "
            "шаблоны — для быстрого формирования имени записи."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        prompts_header = QHBoxLayout()
        prompts_header.addWidget(QLabel("<b>Библиотека промптов</b>"))
        prompts_header.addStretch()
        prompts_header.addWidget(make_info_icon("prompts_table"))
        layout.addLayout(prompts_header)

        self.prompts_table = QTableWidget(0, 2)
        self.prompts_table.setHorizontalHeaderLabels(["Название", "Текст"])
        self.prompts_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        header = self.prompts_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
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
        names_header.addWidget(QLabel("<b>Шаблоны названий записи</b>"))
        names_header.addStretch()
        names_header.addWidget(make_info_icon("name_templates"))
        layout.addLayout(names_header)

        names_hint = QLabel(
            "Используются в окне метаданных записи — выпадающий список "
            "«Название». Плейсхолдеры: "
            "<code>{name}</code> / <code>{название}</code> — введённое имя, "
            "<code>{abbr}</code> / <code>{сокр}</code> — сокращение, "
            "<code>{date}</code> / <code>{дата}</code> — YYYY-MM-DD, "
            "<code>{time}</code> / <code>{время}</code> — HH-MM, "
            "<code>{datetime}</code> — дата и время."
        )
        names_hint.setWordWrap(True)
        names_hint.setStyleSheet("QLabel { color: #666; }")
        layout.addWidget(names_hint)

        self.name_templates_table = QTableWidget(0, 2)
        self.name_templates_table.setHorizontalHeaderLabels(["Название", "Шаблон"])
        self.name_templates_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        header = self.name_templates_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
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
        n_default_btn.setToolTip(
            "Заменить текущий список стандартными шаблонами из поставки"
        )
        n_default_btn.clicked.connect(self._name_tpl_restore_defaults)
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
        self.prompts_table.setItem(row, 0, QTableWidgetItem("Новый промпт"))
        self.prompts_table.setItem(row, 1, QTableWidgetItem(""))
        self.prompts_table.editItem(self.prompts_table.item(row, 0))

    def _prompt_delete(self) -> None:
        row = self.prompts_table.currentRow()
        if row < 0:
            return
        name = self.prompts_table.item(row, 0)
        name = name.text() if name else ""
        if QMessageBox.question(self, "Удалить промпт",
                                f"Удалить промпт «{name}»?") == \
                QMessageBox.StandardButton.Yes:
            log.info("Удаление промпта «%s» (строка %d)", name, row)
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
        log.debug("Промпт перемещён: %d → %d", row, new_row)

    # ------------------------------------------------------------------
    # Шаблоны названий
    # ------------------------------------------------------------------
    def _name_tpl_add(self) -> None:
        row = self.name_templates_table.rowCount()
        self.name_templates_table.insertRow(row)
        self.name_templates_table.setItem(row, 0, QTableWidgetItem("Новый шаблон"))
        self.name_templates_table.setItem(
            row, 1, QTableWidgetItem("{name} — {date}")
        )
        self.name_templates_table.editItem(self.name_templates_table.item(row, 0))
        log.debug("Добавлена пустая строка шаблона имени (строка %d)", row)

    def _name_tpl_delete(self) -> None:
        row = self.name_templates_table.currentRow()
        if row < 0:
            return
        item = self.name_templates_table.item(row, 0)
        label = item.text() if item else ""
        if QMessageBox.question(self, "Удалить шаблон",
                                f"Удалить шаблон «{label}»?") == \
                QMessageBox.StandardButton.Yes:
            log.info("Удаление шаблона имени «%s» (строка %d)", label, row)
            self.name_templates_table.removeRow(row)

    def _name_tpl_move(self, delta: int) -> None:
        row = self.name_templates_table.currentRow()
        if row < 0:
            return
        new_row = row + delta
        if new_row < 0 or new_row >= self.name_templates_table.rowCount():
            return
        for col in range(self.name_templates_table.columnCount()):
            a = self.name_templates_table.takeItem(row, col)
            b = self.name_templates_table.takeItem(new_row, col)
            self.name_templates_table.setItem(row, col, b)
            self.name_templates_table.setItem(new_row, col, a)
        self.name_templates_table.setCurrentCell(new_row, 0)
        log.debug("Шаблон имени перемещён: %d → %d", row, new_row)

    def _name_tpl_restore_defaults(self) -> None:
        if QMessageBox.question(
            self, "Стандартные шаблоны",
            "Заменить текущий список шаблонов стандартными из поставки?",
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
        log.info("Шаблоны имён сброшены к стандартным (%d шт.)",
                 len(DEFAULT_NAME_TEMPLATES))

    # ------------------------------------------------------------------
    # Транскрибация
    # ------------------------------------------------------------------
    def _build_transcribe_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        info = QLabel(
            "Параметры подключения к серверу транскрибации. "
            "Если URL пустой — шаг транскрибации пропускается, "
            "остаётся только конвертация видео в аудио."
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
        self.test_tr_btn.clicked.connect(self.test_transcribe_connection)

        tr_box.addRow("URL сервера:", with_info(self.tr_url_input, "tr_url"))
        tr_box.addRow("Access key:", with_info(self.tr_key_input, "tr_key"))
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
            "Выберите, где формируется протокол/резюме (summary) записи. "
            "Транскрибация всегда выполняется на сервере."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

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
        self.sum_litellm_url.setPlaceholderText("http://localhost:4000")

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
        self.sum_test_btn.clicked.connect(self._test_summarizer_connection)

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
            with_info(self.sum_litellm_temperature, "sum_litellm_temperature"),
        )
        lite_form.addRow(
            "Max tokens:",
            with_info(self.sum_litellm_max_tokens, "sum_litellm_max_tokens"),
        )
        lite_form.addRow(
            "Таймаут соединения:",
            with_info(self.sum_litellm_connect_timeout,
                      "sum_litellm_connect_timeout"),
        )
        lite_form.addRow(
            "Таймаут чтения:",
            with_info(self.sum_litellm_read_timeout,
                      "sum_litellm_read_timeout"),
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
            QMessageBox.warning(self, "Суммаризация",
                                "Не задан base_url для LiteLLM")
            return

        log.info("Тест LiteLLM (ping): url=%s, model=%s", base_url, model)

        from .litellm_client import LiteLLMClient, LiteLLMError

        connect_timeout = float(self.sum_litellm_connect_timeout.value())
        read_timeout = float(self.sum_litellm_read_timeout.value())

        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

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
            QMessageBox.warning(self, "Суммаризация", f"Ошибка: {exc}")
            return
        except Exception as exc:
            log.exception("Тест LiteLLM: неожиданная ошибка: %s", exc)
            QMessageBox.warning(self, "Суммаризация", f"Ошибка: {exc}")
            return
        finally:
            if QGuiApplication.overrideCursor() is not None:
                QGuiApplication.restoreOverrideCursor()

        if method == "models":
            msg = (
                "Подключение успешно.\n\n"
                "Проверено через GET /v1/models (быстрая проверка "
                "связности и авторизации)."
            )
        else:
            msg = (
                "Подключение успешно.\n\n"
                "Сервер не поддерживает /v1/models — проверено "
                "коротким запросом генерации (1 токен)."
            )

        log.info("Тест LiteLLM: успех (метод=%s)", method)
        QMessageBox.information(self, "Суммаризация", msg)

    # ------------------------------------------------------------------
    # Глоссарий
    # ------------------------------------------------------------------
    def _build_glossary_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        info = QLabel(
            "Список терминов и аббревиатур, которые будут переданы ИИ. "
            "Помогает модели правильно понимать специфику: названия систем, "
            "сокращения, ФИО и т.п."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        hint = QLabel(
            "Формат: «Термин» — как модель должна писать это слово; "
            "«Пояснение» — что это значит. Пояснение можно оставить "
            "пустым, тогда в промпт уйдёт только термин.\n\n"
            "Примеры:\n"
            "  • ЕЖД — Единый журнал дежурств\n"
            "  • vNext — платформа vNext\n"
            "  • ПЛ — Планирование\n"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("QLabel { color: #666; }")
        layout.addWidget(hint)

        terms_header = QHBoxLayout()
        terms_header.addWidget(QLabel("<b>Термины</b>"))
        terms_header.addStretch()
        terms_header.addWidget(make_info_icon("glossary_terms"))
        layout.addLayout(terms_header)

        self.glossary_table = QTableWidget(0, 2)
        self.glossary_table.setHorizontalHeaderLabels(["Термин", "Пояснение"])
        self.glossary_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        header = self.glossary_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
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
        send_header.addWidget(QLabel("<b>Куда передавать глоссарий</b>"))
        send_header.addStretch()
        send_header.addWidget(make_info_icon("glossary_send_to_summarizer"))
        layout.addLayout(send_header)

        self.glossary_send_to_summarizer_check = QCheckBox(
            "Передавать в суммаризацию (сервер транскрибации или LiteLLM)"
        )
        attach_tooltip(self.glossary_send_to_summarizer_check,
                       "glossary_send_to_summarizer")
        layout.addWidget(self.glossary_send_to_summarizer_check)

        self.glossary_send_to_deepseek_check = QCheckBox(
            "Передавать в файл промпта DeepSeek (deepseek_prompt.*)"
        )
        attach_tooltip(self.glossary_send_to_deepseek_check,
                       "glossary_send_to_deepseek")
        layout.addWidget(self.glossary_send_to_deepseek_check)

        layout.addStretch()
        return w

    def _glossary_add(self) -> None:
        row = self.glossary_table.rowCount()
        self.glossary_table.insertRow(row)
        self.glossary_table.setItem(row, 0, QTableWidgetItem(""))
        self.glossary_table.setItem(row, 1, QTableWidgetItem(""))
        self.glossary_table.editItem(self.glossary_table.item(row, 0))
        log.debug("Добавлена пустая строка в глоссарий (строка %d)", row)

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
            log.info("Удаление термина «%s» (строка %d)", term, row)
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
        log.debug("Термин перемещён: %d → %d", row, new_row)

    # ------------------------------------------------------------------
    # Запись
    # ------------------------------------------------------------------
    def _build_recording_tab(self) -> QWidget:
        w = QWidget()
        layout = QFormLayout(w)

        self.monitor_combo = QComboBox()
        for m in get_system_monitors():
            self.monitor_combo.addItem(
                f"{m['name']} ({m['width']}x{m['height']})", m["display"]
            )

        self.mic_check = QCheckBox("Записывать микрофон")
        attach_tooltip(self.mic_check, "mic")

        self.watermark_check = QCheckBox("Наложение водяного знака")
        attach_tooltip(self.watermark_check, "watermark")

        self.hotkey_start_input = QLineEdit()
        self.hotkey_start_input.setPlaceholderText("Ctrl+Shift+R")

        self.hotkey_stop_input = QLineEdit()
        self.hotkey_stop_input.setPlaceholderText("Ctrl+Shift+S")

        self.metadata_on_start_check = QCheckBox(
            "Показывать окно метаданных при старте записи"
        )
        attach_tooltip(self.metadata_on_start_check, "metadata_on_start")

        self.metadata_on_stop_check = QCheckBox(
            "Показывать окно метаданных при остановке записи"
        )
        attach_tooltip(self.metadata_on_stop_check, "metadata_on_stop")

        self.overlay_panel_check = QCheckBox(
            "Показывать плавающую панель управления поверх экрана"
        )
        attach_tooltip(self.overlay_panel_check, "overlay_panel")

        self.start_notification_check = QCheckBox(
            "Показывать уведомление о начале записи"
        )
        attach_tooltip(self.start_notification_check, "start_notification")

        layout.addRow("Монитор:", with_info(self.monitor_combo, "monitor"))
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
        return w

    # ------------------------------------------------------------------
    # Очередь
    # ------------------------------------------------------------------
    def _build_queue_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        info = QLabel(
            "Настройки автоматической обработки очереди. "
            "Неудачные задачи будут повторяться автоматически."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        form = QFormLayout()
        self.auto_retry_check = QCheckBox("Автоматически повторять неудачные задачи")
        attach_tooltip(self.auto_retry_check, "auto_retry")

        self.retry_interval_spin = QSpinBox()
        self.retry_interval_spin.setRange(1, 24 * 60)
        self.retry_interval_spin.setSuffix(" мин")
        self.retry_interval_spin.setValue(5)

        self.max_retries_spin = QSpinBox()
        self.max_retries_spin.setRange(1, 100)
        self.max_retries_spin.setValue(10)

        form.addRow("", self.auto_retry_check)
        form.addRow(
            "Интервал проверки:",
            with_info(self.retry_interval_spin, "retry_interval"),
        )
        form.addRow(
            "Максимум попыток:",
            with_info(self.max_retries_spin, "max_retries"),
        )
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
            "Шаблон промпта для DeepSeek и формат файла, в котором "
            "сохраняется готовый промпт.<br>"
            "Промпт собирается автоматически при обработке скрам-митинга "
            "(галочка «Это скрам-митинг» в окне метаданных записи)."
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
        self.scrum_template_edit.setPlaceholderText(
            "Во вложении стенограмма статусного совещания с командой..."
        )
        layout.addWidget(self.scrum_template_edit)

        form = QFormLayout()
        self.scrum_format_combo = QComboBox()
        self.scrum_format_combo.addItems(["docx", "md", "txt"])
        form.addRow(
            "Формат экспорта:",
            with_info(self.scrum_format_combo, "scrum_format"),
        )
        layout.addLayout(form)

        hint = QLabel(
            "Формат экспорта можно изменить в окне «Записи…» при нажатии "
            "«Экспорт промпта…» — этот параметр используется как значение "
            "по умолчанию."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

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
        self.log_level_combo.addItems(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
        form.addRow(
            "Уровень логирования:",
            with_info(self.log_level_combo, "log_level"),
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

        # --- Проекты (новая логика) ---
        projects = self.config_manager.get_projects()
        self.projects_table.setRowCount(0)
        for p in projects:
            row = self.projects_table.rowCount()
            self.projects_table.insertRow(row)
            self.projects_table.setItem(row, 0, QTableWidgetItem(p["name"]))
            self.projects_table.setItem(row, 1, QTableWidgetItem(p["chat_id"]))

        # --- Bitrix24 ---
        bitrix = self.config_manager.get_bitrix_settings()
        self.bitrix_enabled_check.setChecked(bitrix["enabled"])
        self.bitrix_webhook_input.setText(bitrix["webhook_url"])
        self.bitrix_connect_timeout.setValue(bitrix["connect_timeout"])
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

        # --- Промпты ---
        prompts = self.config_manager.get_prompts()
        self.prompts_table.setRowCount(0)
        for p in prompts:
            row = self.prompts_table.rowCount()
            self.prompts_table.insertRow(row)
            self.prompts_table.setItem(row, 0, QTableWidgetItem(p.get("name", "")))
            self.prompts_table.setItem(row, 1, QTableWidgetItem(p.get("text", "")))

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
        self.tr_connect_timeout.setValue(int(tr.get("connect_timeout", 15)))
        self.tr_read_timeout.setValue(int(tr.get("read_timeout", 120)))
        self.tr_max_wait.setValue(int(tr.get("max_wait", 7200)))

        # --- Суммаризация ---
        sum_cfg = self.config_manager.get_summarizer_settings()
        idx = self.sum_provider_combo.findData(sum_cfg["provider"])
        if idx >= 0:
            self.sum_provider_combo.setCurrentIndex(idx)

        l = sum_cfg["litellm"]
        self.sum_litellm_url.setText(l["base_url"])
        self.sum_litellm_key.setText(l["api_key"])
        self.sum_litellm_model.setText(l["model"])
        self.sum_litellm_temperature.setValue(float(l["temperature"]))
        self.sum_litellm_max_tokens.setValue(int(l["max_tokens"]))
        self.sum_litellm_connect_timeout.setValue(int(l["connect_timeout"]))
        self.sum_litellm_read_timeout.setValue(int(l["read_timeout"]))
        self.sum_litellm_system.setPlainText(l["system_prompt"])

        # --- Глоссарий ---
        g = self.config_manager.get_glossary_settings()
        self.glossary_table.setRowCount(0)
        for item in g["terms"]:
            row = self.glossary_table.rowCount()
            self.glossary_table.insertRow(row)
            self.glossary_table.setItem(row, 0, QTableWidgetItem(item.get("term", "")))
            self.glossary_table.setItem(
                row, 1, QTableWidgetItem(item.get("description", ""))
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
        self.mic_check.setChecked(bool(rec.get("with_microphone", True)))
        self.watermark_check.setChecked(bool(rec.get("show_watermark", True)))
        self.hotkey_start_input.setText(rec.get("hotkey_start", "Ctrl+Shift+R"))
        self.hotkey_stop_input.setText(rec.get("hotkey_stop", "Ctrl+Shift+S"))
        self.metadata_on_start_check.setChecked(
            bool(rec.get("show_metadata_on_start", True))
        )
        self.metadata_on_stop_check.setChecked(
            bool(rec.get("show_metadata_on_stop", True))
        )
        self.overlay_panel_check.setChecked(bool(rec.get("show_overlay_panel", True)))
        self.start_notification_check.setChecked(
            bool(rec.get("show_start_notification", True))
        )

        # --- Очередь ---
        q = cfg.get("queue", {})
        self.auto_retry_check.setChecked(bool(q.get("auto_retry_enabled", True)))
        self.retry_interval_spin.setValue(int(q.get("retry_interval_minutes", 5)))
        self.max_retries_spin.setValue(int(q.get("max_retries", 10)))

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

        # --- Сжатие ---
        comp = cfg.get("compression", {})
        self.audio_fmt_combo.setCurrentText(comp.get("audio_format", "mp3"))
        self.audio_bitrate_spin.setValue(int(comp.get("audio_bitrate", 192)))
        self.video_bitrate_spin.setValue(int(comp.get("video_bitrate", 4000)))
        self.compression_spin.setValue(int(comp.get("compression_level", 5)))

        # --- Хранилище ---
        st = cfg.get("storage", {})
        self.temp_path_input.setText(st.get("temp_path", "/tmp/screen-recorder"))
        self.retention_spin.setValue(int(st.get("retention_hours", 24)))

        # --- Логи ---
        logging_cfg = cfg.get("logging", {})
        self.log_path_input.setText(
            logging_cfg.get("log_path", "/tmp/screen-recorder/app.log")
        )
        level = logging_cfg.get("level", "DEBUG")
        i = self.log_level_combo.findText(level)
        if i >= 0:
            self.log_level_combo.setCurrentIndex(i)

        log.info(
            "Настройки загружены в окно: projects=%d, prompts=%d, "
            "name_templates=%d, summarizer=%s, glossary_terms=%d, "
            "bitrix_enabled=%s, log_level=%s",
            len(projects), len(prompts), len(name_tpls),
            sum_cfg["provider"], len(g["terms"]),
            bitrix["enabled"], level,
        )

    def save_settings(self) -> bool:
        try:
            cfg = self.config_manager.config

            # --- Проекты (новая логика) ---
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

            # --- Bitrix24 ---
            cfg["bitrix"] = {
                "enabled": self.bitrix_enabled_check.isChecked(),
                "webhook_url": self.bitrix_webhook_input.text().strip(),
                "connect_timeout": int(self.bitrix_connect_timeout.value()),
                "read_timeout": int(self.bitrix_read_timeout.value()),
                "default_send": (
                    self.bitrix_default_send_combo.currentData() or "protocol"
                ),
                "include_header": self.bitrix_header_check.isChecked(),
                "system_message": self.bitrix_system_check.isChecked(),
                "disable_url_preview": self.bitrix_no_preview_check.isChecked(),
                # --- Отправка файлов ---
                "file_message_max_chars": int(
                    self.bitrix_file_threshold.value()
                ),
                "upload_folder_id": int(self.bitrix_upload_folder.value()),
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
                name_templates.append({"label": label, "template": template})

            meta_cfg = cfg.setdefault("metadata", {})
            meta_cfg["prompts"] = prompts
            meta_cfg["default_prompt"] = self.default_prompt_edit.toPlainText().strip()
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
                "hotkey_start": self.hotkey_start_input.text().strip() or "Ctrl+Shift+R",
                "hotkey_stop": self.hotkey_stop_input.text().strip() or "Ctrl+Shift+S",
                "show_metadata_on_start": self.metadata_on_start_check.isChecked(),
                "show_metadata_on_stop": self.metadata_on_stop_check.isChecked(),
                "show_overlay_panel": self.overlay_panel_check.isChecked(),
                "show_start_notification": self.start_notification_check.isChecked(),
            }
            cfg["queue"] = {
                "auto_retry_enabled": self.auto_retry_check.isChecked(),
                "retry_interval_minutes": self.retry_interval_spin.value(),
                "max_retries": self.max_retries_spin.value(),
            }
            cfg["scrum"] = {
                "prompt_template": self.scrum_template_edit.toPlainText(),
                "export_format": self.scrum_format_combo.currentText(),
            }
            cfg["summarizer"] = {
                "provider": self.sum_provider_combo.currentData() or "server",
                "litellm": {
                    "base_url": self.sum_litellm_url.text().strip().rstrip("/")
                    or "http://localhost:4000",
                    "api_key": self.sum_litellm_key.text(),
                    "model": self.sum_litellm_model.text().strip() or "gpt-4o-mini",
                    "temperature": float(self.sum_litellm_temperature.value()),
                    "max_tokens": int(self.sum_litellm_max_tokens.value()),
                    "connect_timeout": int(self.sum_litellm_connect_timeout.value()),
                    "read_timeout": int(self.sum_litellm_read_timeout.value()),
                    "system_prompt": self.sum_litellm_system.toPlainText().strip(),
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
                glossary_terms.append({"term": term, "description": desc})

            cfg["glossary"] = {
                "terms": glossary_terms,
                "send_to_summarizer": self.glossary_send_to_summarizer_check.isChecked(),
                "send_to_deepseek": self.glossary_send_to_deepseek_check.isChecked(),
            }

            cfg["compression"] = {
                "audio_format": self.audio_fmt_combo.currentText(),
                "audio_bitrate": self.audio_bitrate_spin.value(),
                "video_bitrate": self.video_bitrate_spin.value(),
                "compression_level": self.compression_spin.value(),
            }
            cfg["storage"] = {
                "temp_path": self.temp_path_input.text().strip() or "/tmp/screen-recorder",
                "retention_hours": self.retention_spin.value(),
            }
            new_log_path = self.log_path_input.text().strip() or "/tmp/screen-recorder/app.log"
            cfg["logging"] = {
                "log_path": new_log_path,
                "level": self.log_level_combo.currentText(),
            }

            self.config_manager.save(cfg)
            log.info(
                "Настройки сохранены: projects=%d, prompts=%d, "
                "name_templates=%d, summarizer=%s, glossary_terms=%d, "
                "bitrix_enabled=%s, monitor=%d, log_level=%s, temp_path=%s",
                len(projects), len(prompts), len(name_templates),
                cfg["summarizer"]["provider"], len(glossary_terms),
                cfg["bitrix"]["enabled"],
                cfg["recording"]["monitor"], cfg["logging"]["level"],
                cfg["storage"]["temp_path"],
            )

            current = get_current_log_path()
            if os.path.abspath(current) != os.path.abspath(new_log_path):
                QMessageBox.information(
                    self, "Настройки",
                    "Настройки сохранены.\n\n"
                    "Путь к логу изменён — перезапустите приложение.",
                )
            else:
                QMessageBox.information(self, "Настройки", "Настройки сохранены")
            return True
        except Exception as exc:
            log.exception("Ошибка сохранения настроек: %s", exc)
            QMessageBox.critical(self, "Ошибка", f"Не удалось сохранить: {exc}")
            return False

    # ------------------------------------------------------------------
    # Действия
    # ------------------------------------------------------------------
    def reset_to_defaults(self) -> None:
        log.warning("Сброс настроек к значениям по умолчанию (в окне)")
        self.config_manager.config = self.config_manager.get_defaults()
        self.load_settings()

    def toggle_password_visibility(self, field: QLineEdit, toggle: QCheckBox) -> None:
        if toggle.isChecked():
            field.setEchoMode(QLineEdit.EchoMode.Normal)
        else:
            field.setEchoMode(QLineEdit.EchoMode.Password)

    def browse_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Выберите папку")
        if folder:
            log.info("Выбрана папка: %s", folder)
            self.temp_path_input.setText(folder)

    def browse_log_file(self) -> None:
        current = self.log_path_input.text().strip() or "/tmp/screen-recorder/app.log"
        filename, _ = QFileDialog.getSaveFileName(
            self, "Выберите файл лога", current,
            "Log files (*.log *.txt);;All files (*)",
        )
        if filename:
            log.info("Выбран файл лога: %s", filename)
            self.log_path_input.setText(filename)

    def open_log_file(self) -> None:
        path = self.log_path_input.text().strip() or get_current_log_path()
        path = os.path.expanduser(path)
        if not os.path.exists(path):
            QMessageBox.warning(self, "Лог", f"Файл не найден:\n{path}")
            return
        log.info("Открытие файла лога: %s", path)
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def open_log_folder(self) -> None:
        path = self.log_path_input.text().strip() or get_current_log_path()
        path = os.path.expanduser(path)
        folder = os.path.dirname(path) or "/tmp"
        if not os.path.isdir(folder):
            QMessageBox.warning(self, "Лог", f"Папка не найдена:\n{folder}")
            return
        log.info("Открытие папки логов: %s", folder)
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def clear_log_file(self) -> None:
        path = self.log_path_input.text().strip() or get_current_log_path()
        path = os.path.expanduser(path)
        if not os.path.exists(path):
            QMessageBox.information(self, "Лог", f"Файл не найден:\n{path}")
            return
        if QMessageBox.question(self, "Очистить лог",
                                f"Очистить содержимое файла:\n{path}?") != \
                QMessageBox.StandardButton.Yes:
            return
        try:
            with open(path, "w", encoding="utf-8"):
                pass
            log.info("Файл лога очищен: %s", path)
            QMessageBox.information(self, "Лог", "Файл очищен")
        except Exception as exc:
            log.exception("Не удалось очистить лог %s: %s", path, exc)
            QMessageBox.critical(self, "Ошибка", f"Не удалось очистить: {exc}")

    def test_transcribe_connection(self) -> None:
        url = self.tr_url_input.text().strip()
        key = self.tr_key_input.text()
        connect_timeout = float(self.tr_connect_timeout.value())
        read_timeout = float(self.tr_read_timeout.value())

        if not url:
            QMessageBox.warning(self, "Транскрибация",
                                "URL сервера пустой. Шаг будет пропускаться.")
            return

        log.info("Тест подключения к транскрибации: url=%s, "
                 "connect=%.0f, read=%.0f",
                 url, connect_timeout, read_timeout)

        async def _run():
            async with TranscribeClient(
                base_url=url,
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
            ) as client:
                return await client.login(key)

        try:
            asyncio.run(_run())
            log.info("Тест подключения успешен: %s", url)
            QMessageBox.information(self, "Транскрибация", "Подключение успешно")
        except Exception as exc:
            log.warning("Тест подключения провален (%s): %s", url, exc)
            QMessageBox.warning(self, "Транскрибация", f"Ошибка: {exc}")

    def _show_help(self) -> None:
        QMessageBox.information(
            self,
            "Справка",
            "Screen Recorder & Transcriber\n\n"
            "• Вкладка «Проекты и чаты Bitrix24» — список проектов "
            "с привязкой к чатам Bitrix24. Используется при отправке "
            "протоколов и summary из окна «Записи».\n"
            "• Вкладка «Bitrix24» — параметры подключения к порталу: "
            "вебхук, таймауты, поведение при отправке, режим файлов.\n"
            "• Вкладка «Промпты и имена» — библиотека промптов "
            "и шаблоны названий записи.\n"
            "• Вкладка «Суммаризация» — выбор провайдера формирования "
            "протокола/резюме: сервер транскрибации или LiteLLM.\n"
            "• Вкладка «Глоссарий» — список терминов и аббревиатур, "
            "которые добавляются в промпт транскрибации и/или в файл "
            "deepseek_prompt.*.\n"
            "• Шаблоны названий поддерживают плейсхолдеры:\n"
            "    {name} / {название} — введённое имя,\n"
            "    {abbr} / {сокр} — сокращение,\n"
            "    {date} / {дата} — YYYY-MM-DD,\n"
            "    {time} / {время} — HH-MM,\n"
            "    {datetime} — дата и время.\n"
            "• Кнопка «Вернуть стандартные» сбрасывает список шаблонов "
            "к стандартному набору из поставки.\n\n"
            "Подсказки: наведите курсор на иконку ⓘ рядом с полем, "
            "чтобы увидеть пояснение.",
        )