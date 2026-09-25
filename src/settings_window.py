"""Окно настроек с вкладками (локальный режим)."""
from __future__ import annotations

import asyncio
import os
from typing import List, Optional

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
        self.setMinimumSize(960, 780)
        self.setModal(True)
        log.info("Открытие окна настроек")
        self._build_ui()
        self.load_settings()
        log.debug("Окно настроек готово")

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs)

        self.tabs.addTab(self._build_metadata_tab(), "Проекты, промпты, имена")
        self.tabs.addTab(self._build_transcribe_tab(), "Транскрибация")
        self.tabs.addTab(self._build_summarizer_tab(), "Суммаризация")
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
    # Проекты, промпты, шаблоны имён
    # ------------------------------------------------------------------
    def _build_metadata_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        info = QLabel(
            "Список проектов (по одному в строке или через запятую). "
            "Промпты используются в диалоге метаданных. "
            "Шаблоны названий — для быстрого формирования имени записи."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        # --- Проекты ---
        projects_header = QHBoxLayout()
        projects_header.addWidget(QLabel("<b>Проекты</b>"))
        projects_header.addStretch()
        projects_header.addWidget(make_info_icon("projects_list"))
        layout.addLayout(projects_header)

        self.projects_edit = QPlainTextEdit()
        self.projects_edit.setPlaceholderText("Россети\niserv\nВнутренние\nТестовые")
        self.projects_edit.setFixedHeight(90)
        layout.addWidget(self.projects_edit)

        # --- Промпты ---
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

        # --- Промпт по умолчанию ---
        default_header = QHBoxLayout()
        default_header.addWidget(QLabel("<b>Промпт по умолчанию</b>"))
        default_header.addStretch()
        default_header.addWidget(make_info_icon("default_prompt"))
        layout.addLayout(default_header)

        self.default_prompt_edit = QPlainTextEdit()
        self.default_prompt_edit.setFixedHeight(90)
        layout.addWidget(self.default_prompt_edit)

        # --- Шаблоны названий записи ---
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
    # Шаблоны названий (в окне настроек)
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

        # --- Выбор провайдера ---
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

        # --- LiteLLM ---
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

        # Показываем пользователю, что процесс пошёл.
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

        # Проекты
        self.projects_edit.setPlainText("\n".join(cfg.get("projects", []) or []))

        # Промпты
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

        # Шаблоны названий
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

        # Транскрибация
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

        # Запись
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

        # Очередь
        q = cfg.get("queue", {})
        self.auto_retry_check.setChecked(bool(q.get("auto_retry_enabled", True)))
        self.retry_interval_spin.setValue(int(q.get("retry_interval_minutes", 5)))
        self.max_retries_spin.setValue(int(q.get("max_retries", 10)))

        # Скрам
        scrum = cfg.get("scrum", {})
        from .config_manager import DEFAULT_SCRUM_PROMPT
        self.scrum_template_edit.setPlainText(
            scrum.get("prompt_template", DEFAULT_SCRUM_PROMPT)
        )
        fmt = scrum.get("export_format", "docx")
        i = self.scrum_format_combo.findText(fmt)
        if i >= 0:
            self.scrum_format_combo.setCurrentIndex(i)

        # Сжатие
        comp = cfg.get("compression", {})
        self.audio_fmt_combo.setCurrentText(comp.get("audio_format", "mp3"))
        self.audio_bitrate_spin.setValue(int(comp.get("audio_bitrate", 192)))
        self.video_bitrate_spin.setValue(int(comp.get("video_bitrate", 4000)))
        self.compression_spin.setValue(int(comp.get("compression_level", 5)))

        # Хранилище
        st = cfg.get("storage", {})
        self.temp_path_input.setText(st.get("temp_path", "/tmp/screen-recorder"))
        self.retention_spin.setValue(int(st.get("retention_hours", 24)))

        # Логи
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
            "name_templates=%d, summarizer=%s, log_level=%s",
            len(cfg.get("projects", []) or []), len(prompts),
            len(name_tpls), sum_cfg["provider"], level,
        )

    def save_settings(self) -> bool:
        try:
            cfg = self.config_manager.config

            raw = self.projects_edit.toPlainText()
            parts = [p.strip() for line in raw.splitlines() for p in line.split(",")]
            cfg["projects"] = [p for p in parts if p]

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
            # --- Суммаризация ---
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
                "name_templates=%d, summarizer=%s, monitor=%d, "
                "log_level=%s, temp_path=%s",
                len(cfg["projects"]), len(prompts), len(name_templates),
                cfg["summarizer"]["provider"],
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
            "• Вкладка «Проекты, промпты, имена» — три секции: проекты, "
            "библиотека промптов и шаблоны названий записи.\n"
            "• Вкладка «Суммаризация» — выбор провайдера формирования "
            "протокола/резюме: сервер транскрибации или LiteLLM.\n"
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