"""Окно «Синхронизация» — управление обменом с удалённым сервером.

Функции:
  • Проверка подключения.
  • Публикация выбранных записей / всех (с фильтром).
    Если включена передача медиа — видео и аудио уходят
    на сервер как артефакты (kind=video / kind=audio).
  • Скачивание записей с сервера (с опцией «Перезаписывать
    локальные файлы» и «Скачивать медиа»).
  • Дельта-синхронизация + настройки.
  • Синхронизация справочников проектов и тегов.
  • Журнал операций.
"""
from __future__ import annotations

import asyncio
import os
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout,
    QWidget,
)

from ..file_readers import read_json_file
from ..logger import get_logger
from ..screc_client import ScrecClient, ScrecError
from ..sync_manager import (
    SyncManager,
    get_record_id,
    is_record_published,
)
from .tooltips import attach_tooltip, make_info_icon

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Фоновый воркер
# ---------------------------------------------------------------------------
class _SyncWorker(QThread):
    """Универсальный воркер для асинхронных операций."""

    finished_ok = Signal(object)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, coro_factory, parent=None) -> None:
        super().__init__(parent)
        self._coro_factory = coro_factory

    def run(self) -> None:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                result = loop.run_until_complete(
                    self._coro_factory(self._emit_progress)
                )
            finally:
                try:
                    loop.run_until_complete(loop.shutdown_asyncgens())
                finally:
                    loop.close()
            self.finished_ok.emit(result)
        except ScrecError as exc:
            log.error("SyncWorker: ScrecError: %s", exc)
            self.failed.emit(str(exc))
        except Exception as exc:
            log.exception("SyncWorker: ошибка: %s", exc)
            self.failed.emit(
                f"{exc}\n\n{traceback.format_exc()}"
            )

    def _emit_progress(self, message: str) -> None:
        self.progress.emit(message)


# ---------------------------------------------------------------------------
# Окно
# ---------------------------------------------------------------------------
class SyncWindow(QDialog):
    """Окно управления синхронизацией с сервером."""

    def __init__(
        self,
        sessions_root: str,
        config_manager,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.sessions_root = sessions_root
        self.config_manager = config_manager

        self._worker: Optional[_SyncWorker] = None
        self._remote_tree: List[Dict[str, Any]] = []
        self._remote_records: List[Dict[str, Any]] = []
        self._local_rows: List[Dict[str, Any]] = []

        self.setWindowTitle("Синхронизация с сервером")
        self.setMinimumSize(1180, 820)
        self.setModal(False)

        self._build_ui()
        self._load_state()

        log.info(
            "SyncWindow открыто, sessions_root=%s", sessions_root
        )

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        status_box = QGroupBox("Подключение")
        status_layout = QHBoxLayout(status_box)

        self.status_label = QLabel("—")
        self.status_label.setStyleSheet(
            "QLabel { font-weight: bold; }"
        )
        status_layout.addWidget(QLabel("Статус:"))
        status_layout.addWidget(self.status_label, 1)

        self.test_btn = QPushButton("Проверить подключение")
        self.test_btn.setToolTip(
            "Запросить /health и /api/v1/tree у сервера"
        )
        self.test_btn.clicked.connect(self._on_test_connection)
        status_layout.addWidget(self.test_btn)

        root.addWidget(status_box)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        self.tabs.addTab(self._build_upload_tab(), "На сервер")
        self.tabs.addTab(
            self._build_download_tab(), "С сервера"
        )
        self.tabs.addTab(self._build_delta_tab(), "Дельта")
        self.tabs.addTab(
            self._build_configs_tab(), "Справочники"
        )
        self.tabs.addTab(self._build_log_tab(), "Журнал")

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        bottom = QHBoxLayout()
        bottom.addStretch()

        self.refresh_btn = QPushButton("Обновить списки")
        self.refresh_btn.clicked.connect(self._on_refresh)
        bottom.addWidget(self.refresh_btn)

        self.close_btn = QPushButton("Закрыть")
        self.close_btn.clicked.connect(self.close)
        bottom.addWidget(self.close_btn)

        root.addLayout(bottom)

    # ------------------------------------------------------------------
    # Вкладка «На сервер»
    # ------------------------------------------------------------------
    def _build_upload_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(6)

        info = QLabel(
            "Публикация локальных записей на сервер. "
            "Повторная публикация обновляет запись. "
            "Если включена опция «Передавать видео и аудио "
            "на сервер» (Настройки → Синхронизация), медиафайлы "
            "уходят на сервер как артефакты "
            "(kind=video / kind=audio)."
        )
        info.setWordWrap(True)
        info.setStyleSheet("QLabel { color: #666; }")
        layout.addWidget(info)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Проект:"))

        self.upload_project_combo = QComboBox()
        self.upload_project_combo.addItem("— все проекты —", "")
        self.upload_project_combo.currentIndexChanged.connect(
            self._refresh_local_sessions
        )
        filter_row.addWidget(self.upload_project_combo)

        self.upload_only_new_check = QCheckBox(
            "Только ещё не опубликованные"
        )
        self.upload_only_new_check.setToolTip(
            "Если включено — на сервер уйдут только те записи, "
            "которых там ещё нет."
        )
        self.upload_only_new_check.setChecked(True)
        self.upload_only_new_check.toggled.connect(
            self._refresh_local_sessions
        )
        filter_row.addWidget(self.upload_only_new_check)

        filter_row.addStretch()

        self.upload_refresh_btn = QPushButton("Обновить список")
        self.upload_refresh_btn.clicked.connect(
            self._refresh_local_sessions
        )
        filter_row.addWidget(self.upload_refresh_btn)

        layout.addLayout(filter_row)

        self.upload_table = QTableWidget(0, 7)
        self.upload_table.setHorizontalHeaderLabels([
            "Дата", "Название", "Проект", "Локально",
            "На сервере", "Готово", "Record ID",
        ])
        self.upload_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.upload_table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.upload_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        hv = self.upload_table.horizontalHeader()
        hv.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hv.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hv.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hv.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hv.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        hv.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        hv.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.upload_table, 1)

        btns = QHBoxLayout()

        self.upload_selected_btn = QPushButton(
            "Опубликовать выбранные"
        )
        self.upload_selected_btn.clicked.connect(
            self._on_upload_selected
        )
        btns.addWidget(self.upload_selected_btn)

        self.upload_all_btn = QPushButton(
            "Опубликовать все (с учётом фильтра)"
        )
        self.upload_all_btn.clicked.connect(self._on_upload_all)
        btns.addWidget(self.upload_all_btn)

        btns.addSpacing(12)

        self.mark_ready_btn = QPushButton("Отметить готовыми")
        self.mark_ready_btn.setToolTip(
            "Проставить sync_ready=true у выбранных записей.\n\n"
            "После этого их можно публиковать на сервер и "
            "разрешить фоновую синхронизацию (pull будет "
            "перезаписывать локальные файлы серверной версией)."
        )
        self.mark_ready_btn.clicked.connect(self._on_mark_ready)
        btns.addWidget(self.mark_ready_btn)

        self.unmark_ready_btn = QPushButton("Снять отметку")
        self.unmark_ready_btn.setToolTip(
            "Снять sync_ready у выбранных записей.\n\n"
            "Запись снова станет черновиком: автопубликация "
            "не запускается, фоновый pull игнорирует, "
            "локальные артефакты не удаляются."
        )
        self.unmark_ready_btn.clicked.connect(self._on_unmark_ready)
        btns.addWidget(self.unmark_ready_btn)

        btns.addStretch()

        self.upload_stats_label = QLabel("")
        self.upload_stats_label.setStyleSheet(
            "QLabel { color: #444; font-weight: bold; }"
        )
        btns.addWidget(self.upload_stats_label)

        layout.addLayout(btns)
        return w

    # ------------------------------------------------------------------
    # Вкладка «С сервера»
    # ------------------------------------------------------------------
    def _build_download_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(6)

        info = QLabel(
            "Просмотр записей на сервере и их скачивание. "
            "Артефакты раскладываются по папке sessions/. "
            "Медиа (kind=video / kind=audio) сохраняется как "
            "video.<ext>, если оно есть на сервере и включена "
            "опция «Скачивать медиа»."
        )
        info.setWordWrap(True)
        info.setStyleSheet("QLabel { color: #666; }")
        layout.addWidget(info)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Проект:"))

        self.download_project_combo = QComboBox()
        self.download_project_combo.addItem("— все проекты —", "")
        self.download_project_combo.currentIndexChanged.connect(
            self._on_download_project_changed
        )
        filter_row.addWidget(self.download_project_combo)

        filter_row.addWidget(QLabel("Год:"))
        self.download_year_combo = QComboBox()
        self.download_year_combo.addItem("— все —", "")
        self.download_year_combo.currentIndexChanged.connect(
            self._on_download_year_changed
        )
        filter_row.addWidget(self.download_year_combo)

        filter_row.addWidget(QLabel("Месяц:"))
        self.download_month_combo = QComboBox()
        self.download_month_combo.addItem("— все —", "")
        filter_row.addWidget(self.download_month_combo)

        self.download_load_btn = QPushButton("Загрузить список")
        self.download_load_btn.clicked.connect(
            self._on_load_remote_records
        )
        filter_row.addWidget(self.download_load_btn)

        filter_row.addStretch()
        layout.addLayout(filter_row)

        # --- Опции скачивания ---
        override_row = QHBoxLayout()

        self.force_overwrite_check = QCheckBox(
            "Перезаписывать локальные файлы (игнорировать sha256)"
        )
        self.force_overwrite_check.setToolTip(
            "По умолчанию скачивание использует «умную» стратегию: "
            "если sha256 локального файла совпадает с серверным — "
            "файл не перезаписывается.\n\n"
            "Включите эту галочку, если хотите гарантированно "
            "перезаписать локальные артефакты серверной версией."
        )
        self.force_overwrite_check.setChecked(
            bool(self._load_force_overwrite_setting())
        )
        self.force_overwrite_check.toggled.connect(
            self._on_force_overwrite_toggled
        )
        override_row.addWidget(self.force_overwrite_check)

        override_row.addSpacing(16)

        self.download_media_check = QCheckBox(
            "Скачивать медиа (video.<ext>)"
        )
        self.download_media_check.setToolTip(
            "Если включено — при скачивании записи с сервера "
            "видео и аудио (kind=video / kind=audio) сохраняются "
            "локально как video.<ext>.\n\n"
            "Если на сервере медиа нет — ничего не скачивается, "
            "локальные файлы не трогаются."
        )
        self.download_media_check.setChecked(True)
        override_row.addWidget(self.download_media_check)

        override_row.addStretch()
        layout.addLayout(override_row)

        self.download_table = QTableWidget(0, 6)
        self.download_table.setHorizontalHeaderLabels([
            "Дата", "Название", "Проект", "Folder",
            "Артефактов", "Record ID",
        ])
        self.download_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.download_table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.download_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        hv = self.download_table.horizontalHeader()
        hv.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hv.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hv.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hv.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        hv.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        hv.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.download_table, 1)

        manual_row = QHBoxLayout()
        manual_row.addWidget(QLabel("Или введите Record ID:"))

        self.manual_record_input = QLineEdit()
        self.manual_record_input.setPlaceholderText(
            "770e8400-e29b-41d4-a716-446655440111"
        )
        attach_tooltip(
            self.manual_record_input, "sync_manual_record_id"
        )
        manual_row.addWidget(self.manual_record_input, 1)

        self.download_manual_btn = QPushButton("Скачать по ID")
        self.download_manual_btn.clicked.connect(
            self._on_download_manual
        )
        manual_row.addWidget(self.download_manual_btn)

        layout.addLayout(manual_row)

        btns = QHBoxLayout()

        self.download_selected_btn = QPushButton(
            "Скачать выбранные"
        )
        self.download_selected_btn.clicked.connect(
            self._on_download_selected
        )
        btns.addWidget(self.download_selected_btn)

        btns.addStretch()

        self.download_stats_label = QLabel("")
        self.download_stats_label.setStyleSheet(
            "QLabel { color: #444; font-weight: bold; }"
        )
        btns.addWidget(self.download_stats_label)

        layout.addLayout(btns)
        return w

    def _load_force_overwrite_setting(self) -> bool:
        try:
            cfg = self.config_manager.get_sync_settings()
            return bool(cfg.get("force_overwrite_on_download", False))
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Вкладка «Дельта»
    # ------------------------------------------------------------------
    def _build_delta_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        info = QLabel(
            "Дельта-синхронизация: запрашивает у сервера "
            "изменения начиная с последней сохранённой ревизии "
            "и применяет их локально."
        )
        info.setWordWrap(True)
        info.setStyleSheet("QLabel { color: #666; }")
        layout.addWidget(info)

        form = QFormLayout()

        self.last_revision_label = QLabel("0")
        self.last_revision_label.setStyleSheet(
            "QLabel { font-weight: bold; }"
        )
        form.addRow("Последняя ревизия:", self.last_revision_label)

        self.auto_pull_check = QCheckBox(
            "Автоматически подтягивать изменения в фоне"
        )
        attach_tooltip(self.auto_pull_check, "sync_auto_pull")
        self.auto_pull_check.toggled.connect(
            self._on_auto_pull_toggled
        )
        form.addRow("", self.auto_pull_check)

        self.delete_local_check = QCheckBox(
            "Удалять локальную папку, если запись удалена "
            "на сервере"
        )
        self.delete_local_check.setToolTip(
            "Если выключено (по умолчанию) — при удалении записи "
            "на сервере локальная папка остаётся, но помечается "
            "маркером deleted_on_server.\n\n"
            "Если включено — папка удаляется с диска без "
            "возможности восстановления (кроме бэкапов)."
        )
        self.delete_local_check.toggled.connect(
            self._on_delete_local_toggled
        )
        form.addRow("", self.delete_local_check)

        layout.addLayout(form)

        btns = QHBoxLayout()

        self.pull_btn = QPushButton("Подтянуть изменения")
        self.pull_btn.clicked.connect(self._on_pull_changes)
        btns.addWidget(self.pull_btn)

        self.pull_full_btn = QPushButton(
            "Полная синхронизация (snapshot)"
        )
        self.pull_full_btn.clicked.connect(self._on_pull_snapshot)
        btns.addWidget(self.pull_full_btn)

        self.reset_revision_btn = QPushButton("Сбросить ревизию в 0")
        self.reset_revision_btn.clicked.connect(
            self._on_reset_revision
        )
        btns.addWidget(self.reset_revision_btn)

        btns.addStretch()
        layout.addLayout(btns)

        layout.addStretch()
        return w

    # ------------------------------------------------------------------
    # Вкладка «Справочники»
    # ------------------------------------------------------------------
    def _build_configs_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        info = QLabel(
            "Синхронизация справочников <b>проектов</b> и "
            "<b>тегов</b> между локальным конфигом и сервером.\n\n"
            "Справочники хранятся на сервере в виде специальных "
            "записей в проекте <code>_config</code>."
        )
        info.setWordWrap(True)
        info.setStyleSheet("QLabel { color: #666; }")
        layout.addWidget(info)

        btns = QHBoxLayout()

        self.push_configs_btn = QPushButton(
            "Отправить справочники на сервер (push)"
        )
        self.push_configs_btn.clicked.connect(
            self._on_push_configs
        )
        btns.addWidget(self.push_configs_btn)

        self.pull_configs_btn = QPushButton(
            "Скачать справочники с сервера (pull)"
        )
        self.pull_configs_btn.clicked.connect(
            self._on_pull_configs
        )
        btns.addWidget(self.pull_configs_btn)

        btns.addStretch()
        layout.addLayout(btns)

        btns2 = QHBoxLayout()

        self.sync_configs_btn = QPushButton(
            "Синхронизировать (push + pull)"
        )
        self.sync_configs_btn.clicked.connect(
            self._on_sync_configs
        )
        btns2.addWidget(self.sync_configs_btn)

        btns2.addStretch()
        layout.addLayout(btns2)

        preview_header = QHBoxLayout()
        preview_header.addWidget(
            QLabel("<b>Текущие справочники</b>")
        )
        preview_header.addStretch()
        layout.addLayout(preview_header)

        self.configs_preview = QPlainTextEdit()
        self.configs_preview.setReadOnly(True)
        self.configs_preview.setMaximumBlockCount(500)
        layout.addWidget(self.configs_preview, 1)

        self._refresh_configs_preview()
        return w

    def _refresh_configs_preview(self) -> None:
        try:
            projects = self.config_manager.get_projects()
            tags = self.config_manager.get_tags()
        except Exception as exc:
            self.configs_preview.setPlainText(
                f"Не удалось прочитать справочники: {exc}"
            )
            return

        lines: List[str] = []
        lines.append(f"=== Проекты ({len(projects)} шт.) ===")
        for p in projects:
            chat = p.get("chat_id") or "—"
            lines.append(f"  • {p['name']}  →  чат: {chat}")
        lines.append("")
        lines.append(f"=== Теги ({len(tags)} шт.) ===")
        for t in tags:
            color = t.get("color") or "—"
            lines.append(f"  • {t['name']}  →  цвет: {color}")

        self.configs_preview.setPlainText("\n".join(lines))

    # ------------------------------------------------------------------
    # Вкладка «Журнал»
    # ------------------------------------------------------------------
    def _build_log_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(12, 12, 12, 12)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setPlaceholderText(
            "Здесь будут появляться сообщения о ходе "
            "синхронизации…"
        )
        layout.addWidget(self.log_view, 1)

        btns = QHBoxLayout()
        btns.addStretch()

        self.clear_log_btn = QPushButton("Очистить журнал")
        self.clear_log_btn.clicked.connect(self.log_view.clear)
        btns.addWidget(self.clear_log_btn)

        layout.addLayout(btns)
        return w

    # ------------------------------------------------------------------
    # Загрузка состояния
    # ------------------------------------------------------------------
    def _load_state(self) -> None:
        cfg = self.config_manager.get_sync_settings()
        enabled = bool(cfg.get("enabled"))
        base_url = cfg.get("base_url") or ""
        has_key = bool(cfg.get("api_key"))

        if enabled and base_url and has_key:
            self.status_label.setText(
                f"<span style='color:#2E7D32'>"
                f"Настроено: {base_url}</span>"
            )
        elif not enabled:
            self.status_label.setText(
                "<span style='color:#c62828'>"
                "Синхронизация отключена в настройках.</span>"
            )
        else:
            self.status_label.setText(
                "<span style='color:#c62828'>"
                "Не заданы base_url и/или api_key.</span>"
            )

        try:
            last_rev = self._get_manager().get_last_revision()
        except Exception:
            last_rev = 0
        self.last_revision_label.setText(str(last_rev))

        self.auto_pull_check.blockSignals(True)
        self.auto_pull_check.setChecked(
            bool(cfg.get("auto_pull_enabled", True))
        )
        self.auto_pull_check.blockSignals(False)

        self.delete_local_check.blockSignals(True)
        self.delete_local_check.setChecked(
            bool(cfg.get("delete_local_on_server_delete", False))
        )
        self.delete_local_check.blockSignals(False)

        self.force_overwrite_check.blockSignals(True)
        self.force_overwrite_check.setChecked(
            bool(cfg.get("force_overwrite_on_download", False))
        )
        self.force_overwrite_check.blockSignals(False)

        self._refresh_local_sessions()
        self._refresh_configs_preview()

    def _get_manager(self) -> SyncManager:
        cfg = self.config_manager.get_sync_settings()
        return SyncManager(
            sessions_root=self.sessions_root,
            sync_settings=cfg,
            config_manager=self.config_manager,
        )

    def _append_log(self, message: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{ts}] {message}")

    # ------------------------------------------------------------------
    # Общие обработчики
    # ------------------------------------------------------------------
    def _start_worker(self, coro_factory, *, on_ok, on_fail=None) -> None:
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.information(
                self, "Синхронизация",
                "Операция уже выполняется. Дождитесь завершения.",
            )
            return

        self._set_busy(True)
        self._worker = _SyncWorker(coro_factory, parent=self)
        self._worker.progress.connect(self._append_log)
        self._worker.finished_ok.connect(on_ok)
        if on_fail is not None:
            self._worker.failed.connect(on_fail)
        else:
            self._worker.failed.connect(self._on_worker_failed)
        self._worker.finished.connect(
            lambda: self._set_busy(False)
        )
        self._worker.start()

    def _set_busy(self, busy: bool) -> None:
        self.progress.setVisible(busy)
        if busy:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 100)
        for w in (
            self.test_btn, self.refresh_btn,
            self.upload_selected_btn, self.upload_all_btn,
            self.upload_refresh_btn,
            self.download_load_btn, self.download_selected_btn,
            self.download_manual_btn,
            self.pull_btn, self.pull_full_btn,
            self.reset_revision_btn,
            self.push_configs_btn, self.pull_configs_btn,
            self.sync_configs_btn,
        ):
            w.setEnabled(not busy)

    def _on_worker_failed(self, error: str) -> None:
        self._append_log(f"ОШИБКА: {error}")
        QMessageBox.critical(
            self, "Синхронизация", f"Ошибка:\n\n{error}"
        )

    def _on_auto_pull_toggled(self, checked: bool) -> None:
        try:
            cfg = self.config_manager.get_sync_settings()
            cfg["auto_pull_enabled"] = bool(checked)
            self.config_manager.set_sync_settings(cfg)
            log.info(
                "Автоподтягивание изменений: %s",
                "включено" if checked else "выключено",
            )
        except Exception as exc:
            log.warning(
                "Не удалось сохранить auto_pull_enabled: %s", exc
            )

    def _on_delete_local_toggled(self, checked: bool) -> None:
        try:
            cfg = self.config_manager.get_sync_settings()
            cfg["delete_local_on_server_delete"] = bool(checked)
            self.config_manager.set_sync_settings(cfg)
            log.info(
                "Удаление локальной папки при удалении на сервере: %s",
                "включено" if checked else "выключено",
            )
        except Exception as exc:
            log.warning(
                "Не удалось сохранить delete_local_on_server_delete: %s",
                exc,
            )

    def _on_force_overwrite_toggled(self, checked: bool) -> None:
        try:
            cfg = self.config_manager.get_sync_settings()
            cfg["force_overwrite_on_download"] = bool(checked)
            self.config_manager.set_sync_settings(cfg)
            log.info(
                "Перезапись локальных файлов при скачивании: %s",
                "включена" if checked else "выключена",
            )
        except Exception as exc:
            log.warning(
                "Не удалось сохранить force_overwrite_on_download: %s",
                exc,
            )

    def _on_test_connection(self) -> None:
        manager = self._get_manager()
        if not manager.is_configured():
            QMessageBox.warning(
                self, "Синхронизация",
                "Укажите base_url и api_key в "
                "Настройки → Синхронизация.",
            )
            return

        self._append_log("Проверка подключения…")

        async def _factory(progress):
            async with ScrecClient(
                base_url=manager.settings["base_url"],
                api_key=manager.settings["api_key"],
                connect_timeout=float(
                    manager.settings.get("connect_timeout", 15)
                ),
                read_timeout=float(
                    manager.settings.get("read_timeout", 120)
                ),
            ) as client:
                return await client.ping()

        def _on_ok(result: str) -> None:
            self._append_log(f"Подключение успешно: {result}")
            self.status_label.setText(
                f"<span style='color:#2E7D32'>"
                f"OK: {result}</span>"
            )
            QMessageBox.information(
                self, "Синхронизация",
                f"Подключение успешно.\n\n{result}",
            )

        self._start_worker(_factory, on_ok=_on_ok)

    def _on_refresh(self) -> None:
        self._refresh_local_sessions()
        self._refresh_configs_preview()
        self._load_state()
        self._append_log("Списки обновлены")

    # ------------------------------------------------------------------
    # Локальные сессии
    # ------------------------------------------------------------------
    def _refresh_local_sessions(self) -> None:
        if not os.path.isdir(self.sessions_root):
            self._local_rows = []
            self.upload_table.setRowCount(0)
            return

        project_filter = (
            self.upload_project_combo.currentData() or ""
        )
        only_new = self.upload_only_new_check.isChecked()

        projects_set = set()
        rows: List[Dict[str, Any]] = []
        try:
            entries = sorted(os.listdir(self.sessions_root))
        except OSError:
            entries = []

        for name in entries:
            full = os.path.join(self.sessions_root, name)
            if not os.path.isdir(full):
                continue
            if not os.path.isfile(os.path.join(full, "session.json")):
                continue

            meta = read_json_file(
                os.path.join(full, "session.json")
            ) or {}
            project = (meta.get("project") or "").strip()
            if project:
                projects_set.add(project)

            if project_filter and project != project_filter:
                continue

            sync_ready = bool(meta.get("sync_ready", False))

            published = is_record_published(full)
            if only_new and published:
                continue

            date_str = (meta.get("date") or "").strip() or name
            has_video = any(
                os.path.isfile(os.path.join(full, f"video{ext}"))
                for ext in (".mp4", ".mkv", ".mov", ".avi",
                            ".webm", ".mp3", ".wav", ".m4a")
            )
            has_txt = os.path.isfile(
                os.path.join(full, "video.txt")
            )
            has_protocol = any(
                os.path.isfile(os.path.join(full, f))
                for f in ("manual_protocol.docx",
                          "manual_protocol.md",
                          "manual_protocol.txt",
                          "protocol.docx", "protocol.md",
                          "protocol.txt")
            )

            local_parts = []
            if has_video:
                local_parts.append("video")
            if has_txt:
                local_parts.append("txt")
            if has_protocol:
                local_parts.append("protocol")

            rows.append({
                "dir": full,
                "date": date_str,
                "name": meta.get("name") or name,
                "project": project or "—",
                "local": ", ".join(local_parts) or "—",
                "published": published,
                # --- Флаг готовности к синхронизации ---
                "sync_ready": sync_ready,
                "record_id": get_record_id(full),
            })

        current = self.upload_project_combo.currentData() or ""
        self.upload_project_combo.blockSignals(True)
        self.upload_project_combo.clear()
        self.upload_project_combo.addItem("— все проекты —", "")
        for p in sorted(projects_set):
            self.upload_project_combo.addItem(p, p)
        idx = self.upload_project_combo.findData(current)
        if idx >= 0:
            self.upload_project_combo.setCurrentIndex(idx)
        self.upload_project_combo.blockSignals(False)

        self.upload_table.setRowCount(0)
        published_count = 0
        for r in rows:
            row = self.upload_table.rowCount()
            self.upload_table.insertRow(row)
            self.upload_table.setItem(
                row, 0, QTableWidgetItem(r["date"])
            )
            self.upload_table.setItem(
                row, 1, QTableWidgetItem(r["name"])
            )
            self.upload_table.setItem(
                row, 2, QTableWidgetItem(r["project"])
            )
            self.upload_table.setItem(
                row, 3, QTableWidgetItem(r["local"])
            )
            published_item = QTableWidgetItem(
                "да" if r["published"] else "нет"
            )
            if r["published"]:
                published_item.setForeground(
                    Qt.GlobalColor.darkGreen
                )
                published_count += 1
            self.upload_table.setItem(row, 4, published_item)

            # --- Колонка «Готово» ---
            if r.get("sync_ready"):
                ready_item = QTableWidgetItem("готово")
                ready_item.setForeground(Qt.GlobalColor.darkYellow)
                ready_item.setToolTip(
                    "Запись помечена как «готова к синхронизации»."
                )
            else:
                ready_item = QTableWidgetItem("черновик")
                ready_item.setForeground(Qt.GlobalColor.gray)
                ready_item.setToolTip(
                    "Черновик (sync_ready=false). Автопубликация "
                    "не запускается, фоновый pull игнорирует."
                )
            self.upload_table.setItem(row, 5, ready_item)

            self.upload_table.setItem(
                row, 6, QTableWidgetItem(r["record_id"] or "—")
            )

        ready_count = sum(
            1 for r in rows
            if r.get("sync_ready") and not r.get("published")
        )
        self.upload_stats_label.setText(
            f"Показано: {len(rows)} | "
            f"Уже на сервере: {published_count} | "
            f"Готовы к синхр.: {ready_count}"
        )
        self._local_rows = rows

    # ------------------------------------------------------------------
    # Публикация
    # ------------------------------------------------------------------
    def _selected_upload_rows(self) -> List[Dict[str, Any]]:
        rows = self._local_rows or []
        result: List[Dict[str, Any]] = []
        for i in self.upload_table.selectionModel().selectedRows():
            r = i.row()
            if 0 <= r < len(rows):
                result.append(rows[r])
        return result

    def _on_upload_selected(self) -> None:
        rows = self._selected_upload_rows()
        if not rows:
            QMessageBox.information(
                self, "Синхронизация",
                "Выберите одну или несколько записей в таблице.",
            )
            return

        manager = self._get_manager()
        if not manager.is_configured():
            QMessageBox.warning(
                self, "Синхронизация",
                "Укажите base_url и api_key в "
                "Настройки → Синхронизация.",
            )
            return

        # --- Предупреждение о черновиках ---
        drafts = [r for r in rows if not r.get("sync_ready")]
        if drafts:
            reply = QMessageBox.question(
                self, "Синхронизация",
                f"Среди выбранных записей есть {len(drafts)} "
                f"черновиков (sync_ready=false).\n\n"
                f"Публикация возможна, но фоновый pull будет "
                f"игнорировать эти записи, пока вы не поставите "
                f"галочку «Готово к синхронизации».\n\n"
                f"Отметить их готовыми и продолжить?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply == QMessageBox.StandardButton.Yes:
                for r in drafts:
                    session_json = os.path.join(
                        r["dir"], "session.json"
                    )
                    meta = read_json_file(session_json) or {}
                    meta["sync_ready"] = True
                    meta["sync_ready_at"] = datetime.now().isoformat()
                    try:
                        tmp = session_json + ".tmp"
                        with open(tmp, "w", encoding="utf-8") as f:
                            json.dump(
                                meta, f, indent=2, ensure_ascii=False
                            )
                        os.replace(tmp, session_json)
                    except Exception as exc:
                        log.warning(
                            "Не удалось отметить %s: %s",
                            r["dir"], exc,
                        )
                self._refresh_local_sessions()
                # Обновляем локальный список rows
                for r in rows:
                    r["sync_ready"] = True

        send_media = bool(
            manager.settings.get("send_media_to_server", False)
        )
        media_note = (
            " Медиа будет передано как артефакты."
            if send_media else
            " Медиа НЕ будет передано (включите в Настройки → "
            "Синхронизация)."
        )
        self._append_log(
            f"Публикация {len(rows)} записей на сервер…{media_note}"
        )

        async def _factory(progress):
            results = []
            for i, r in enumerate(rows, start=1):
                progress(
                    f"[{i}/{len(rows)}] "
                    f"{os.path.basename(r['dir'])}"
                )
                try:
                    res = await manager.publish_session(
                        r["dir"], progress_cb=progress
                    )
                    results.append({
                        "session": r["dir"], "ok": True,
                        "result": res,
                    })
                except ScrecError as exc:
                    results.append({
                        "session": r["dir"], "ok": False,
                        "error": str(exc),
                    })
            return results

        def _on_ok(results: List[Dict[str, Any]]) -> None:
            ok = sum(1 for r in results if r["ok"])
            fail = len(results) - ok

            media_up_total = 0
            media_skip_total = 0
            for r in results:
                if not r["ok"]:
                    continue
                res = r.get("result") or {}
                media_up_total += len(
                    res.get("media_uploaded") or []
                )
                media_skip_total += len(
                    res.get("media_skipped") or []
                )

            self._append_log(
                f"Публикация завершена: {ok} ок, {fail} ошибок, "
                f"медиа загружено: {media_up_total}, "
                f"медиа пропущено: {media_skip_total}"
            )
            self._refresh_local_sessions()

            if fail == 0:
                QMessageBox.information(
                    self, "Синхронизация",
                    f"Опубликовано записей: {ok}\n"
                    f"Медиа загружено: {media_up_total}\n"
                    f"Медиа пропущено: {media_skip_total}",
                )
            else:
                lines = []
                for r in results:
                    if not r["ok"]:
                        lines.append(
                            f"• {os.path.basename(r['session'])}: "
                            f"{r['error']}"
                        )
                QMessageBox.warning(
                    self, "Синхронизация",
                    f"Опубликовано: {ok}\nОшибок: {fail}\n"
                    f"Медиа загружено: {media_up_total}\n"
                    f"Медиа пропущено: {media_skip_total}\n\n"
                    + "\n".join(lines[:20]),
                )

        self._start_worker(_factory, on_ok=_on_ok)

    def _on_upload_all(self) -> None:
        rows = self._local_rows or []
        if not rows:
            QMessageBox.information(
                self, "Синхронизация",
                "Нет записей для публикации.",
            )
            return

        manager = self._get_manager()
        send_media = bool(
            manager.settings.get("send_media_to_server", False)
        )

        drafts = [r for r in rows if not r.get("sync_ready")]
        draft_note = (
            f"\nЧерновиков (sync_ready=false): {len(drafts)}"
            if drafts else ""
        )

        reply = QMessageBox.question(
            self, "Публикация на сервер",
            f"Опубликовать {len(rows)} записей на сервер?\n\n"
            f"Фильтр: проект="
            f"{self.upload_project_combo.currentText()}, "
            f"только новые="
            f"{'да' if self.upload_only_new_check.isChecked() else 'нет'}\n"
            f"Передача медиа: "
            f"{'включена' if send_media else 'выключена'}"
            f"{draft_note}",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # --- Автоматически пометить черновики готовыми ---
        if drafts:
            reply2 = QMessageBox.question(
                self, "Черновики",
                f"Среди записей {len(drafts)} черновиков.\n\n"
                f"Отметить их как «готово к синхронизации» "
                f"перед публикацией?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply2 == QMessageBox.StandardButton.Yes:
                for r in drafts:
                    session_json = os.path.join(
                        r["dir"], "session.json"
                    )
                    meta = read_json_file(session_json) or {}
                    meta["sync_ready"] = True
                    meta["sync_ready_at"] = datetime.now().isoformat()
                    try:
                        tmp = session_json + ".tmp"
                        with open(tmp, "w", encoding="utf-8") as f:
                            json.dump(
                                meta, f, indent=2, ensure_ascii=False
                            )
                        os.replace(tmp, session_json)
                    except Exception as exc:
                        log.warning(
                            "Не удалось отметить %s: %s",
                            r["dir"], exc,
                        )
                self._refresh_local_sessions()

        if not manager.is_configured():
            QMessageBox.warning(
                self, "Синхронизация",
                "Укажите base_url и api_key в "
                "Настройки → Синхронизация.",
            )
            return

        async def _factory(progress):
            async def _progress_cb(cur, total, msg):
                progress(f"[{cur}/{total}] {msg}")

            return await manager.sync_all(
                progress_cb=_progress_cb,
            )

        def _on_ok(result: Dict[str, Any]) -> None:
            self._append_log(
                f"sync_all: total={result['total']}, "
                f"ok={result['ok']}, "
                f"errors={len(result['errors'])}"
            )
            self._refresh_local_sessions()

            if not result["errors"]:
                QMessageBox.information(
                    self, "Синхронизация",
                    f"Опубликовано записей: {result['ok']} "
                    f"из {result['total']}",
                )
            else:
                lines = [
                    f"• {e['session']}: {e['error']}"
                    for e in result["errors"][:20]
                ]
                QMessageBox.warning(
                    self, "Синхронизация",
                    f"Опубликовано: {result['ok']} из "
                    f"{result['total']}\n"
                    f"Ошибок: {len(result['errors'])}\n\n"
                    + "\n".join(lines),
                )

        self._start_worker(_factory, on_ok=_on_ok)

    # ------------------------------------------------------------------
    # Отметка «готово к синхронизации»
    # ------------------------------------------------------------------
    def _on_mark_ready(self) -> None:
        """Проставляет sync_ready=true у выбранных записей.

        Если включена настройка app.sync_publish_on_ready —
        дополнительно запускает публикацию в фоне.
        """
        rows = self._selected_upload_rows()
        if not rows:
            QMessageBox.information(
                self, "Синхронизация",
                "Выберите одну или несколько записей в таблице.",
            )
            return

        reply = QMessageBox.question(
            self, "Готово к синхронизации",
            f"Отметить {len(rows)} записей как готовые "
            f"к синхронизации?\n\n"
            f"После этого:\n"
            f"  • автопубликация может отправить их на сервер;\n"
            f"  • фоновый pull может перезаписывать локальные "
            f"файлы серверной версией;\n"
            f"  • локальные артефакты, которых нет на сервере, "
            f"могут быть удалены.\n\n"
            f"Убедитесь, что протоколы, summary и вложения "
            f"на месте.",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        count = 0
        errors: List[str] = []
        for r in rows:
            session_json = os.path.join(r["dir"], "session.json")
            meta = read_json_file(session_json) or {}
            if meta.get("sync_ready", False):
                continue
            meta["sync_ready"] = True
            meta["sync_ready_at"] = datetime.now().isoformat()
            try:
                tmp = session_json + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(meta, f, indent=2, ensure_ascii=False)
                os.replace(tmp, session_json)
                count += 1
            except Exception as exc:
                log.exception(
                    "Не удалось обновить %s: %s", session_json, exc
                )
                errors.append(f"{os.path.basename(r['dir'])}: {exc}")

        self._refresh_local_sessions()
        self._append_log(
            f"Отмечено как «готово к синхронизации»: {count} "
            f"(ошибок: {len(errors)})"
        )

        if errors:
            QMessageBox.warning(
                self, "Синхронизация",
                f"Отмечено записей: {count}\n"
                f"Ошибок: {len(errors)}\n\n"
                + "\n".join(errors[:20]),
            )
        else:
            QMessageBox.information(
                self, "Синхронизация",
                f"Отмечено записей: {count}",
            )

        # --- НОВОЕ: авто-публикация отмеченных записей ---
        if self._is_publish_on_ready_enabled():
            not_published = [
                r for r in rows if not r.get("published")
            ]
            if not_published:
                self._append_log(
                    f"Авто-публикация включена: запускаю публикацию "
                    f"для {len(not_published)} записей"
                )
                self._publish_rows_after_ready(not_published)

    def _is_publish_on_ready_enabled(self) -> bool:
        """Проверяет настройку app.sync_publish_on_ready."""
        try:
            cfg = self.config_manager.get_app_settings()
            return bool(cfg.get("sync_publish_on_ready", True))
        except Exception as exc:
            log.warning(
                "Не удалось прочитать app-настройки: %s", exc
            )
            return False

    def _publish_rows_after_ready(
        self, rows: List[Dict[str, Any]]
    ) -> None:
        """Запускает публикацию списка записей в фоне."""
        manager = self._get_manager()
        if not manager.is_configured():
            self._append_log(
                "Авто-публикация пропущена: синхронизация не "
                "настроена"
            )
            return

        if self._worker is not None and self._worker.isRunning():
            self._append_log(
                "Авто-публикация отложена: уже выполняется другая "
                "операция синхронизации"
            )
            return

        send_media = bool(
            manager.settings.get("send_media_to_server", False)
        )
        self._append_log(
            f"Авто-публикация {len(rows)} записей "
            f"(медиа={'да' if send_media else 'нет'})…"
        )

        async def _factory(progress):
            results = []
            for i, r in enumerate(rows, start=1):
                progress(
                    f"[{i}/{len(rows)}] "
                    f"{os.path.basename(r['dir'])}"
                )
                try:
                    res = await manager.publish_session(
                        r["dir"], progress_cb=progress
                    )
                    results.append({
                        "session": r["dir"], "ok": True,
                        "result": res,
                    })
                except ScrecError as exc:
                    results.append({
                        "session": r["dir"], "ok": False,
                        "error": str(exc),
                    })
            return results

        def _on_ok(results: List[Dict[str, Any]]) -> None:
            ok = sum(1 for r in results if r["ok"])
            fail = len(results) - ok

            self._append_log(
                f"Авто-публикация завершена: {ok} ок, {fail} ошибок"
            )
            self._refresh_local_sessions()

            if fail == 0:
                QMessageBox.information(
                    self, "Авто-публикация",
                    f"Опубликовано записей: {ok}",
                )
            else:
                lines = [
                    f"• {os.path.basename(r['session'])}: "
                    f"{r['error']}"
                    for r in results if not r["ok"]
                ]
                QMessageBox.warning(
                    self, "Авто-публикация",
                    f"Опубликовано: {ok}\nОшибок: {fail}\n\n"
                    + "\n".join(lines[:20]),
                )

        self._start_worker(_factory, on_ok=_on_ok)

    def _on_unmark_ready(self) -> None:
        """Снимает sync_ready у выбранных записей."""
        rows = self._selected_upload_rows()
        if not rows:
            QMessageBox.information(
                self, "Синхронизация",
                "Выберите одну или несколько записей в таблице.",
            )
            return

        reply = QMessageBox.question(
            self, "Снять отметку",
            f"Снять отметку «готово к синхронизации» у "
            f"{len(rows)} записей?\n\n"
            f"Записи снова станут черновиками: автопубликация "
            f"не запускается, фоновый pull игнорирует, "
            f"локальные артефакты не удаляются.",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        count = 0
        errors: List[str] = []
        for r in rows:
            session_json = os.path.join(r["dir"], "session.json")
            meta = read_json_file(session_json) or {}
            if not meta.get("sync_ready", False):
                continue
            meta["sync_ready"] = False
            meta["sync_ready_at"] = ""
            try:
                tmp = session_json + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(meta, f, indent=2, ensure_ascii=False)
                os.replace(tmp, session_json)
                count += 1
            except Exception as exc:
                log.exception(
                    "Не удалось обновить %s: %s", session_json, exc
                )
                errors.append(f"{os.path.basename(r['dir'])}: {exc}")

        self._refresh_local_sessions()
        self._append_log(
            f"Снято «готово к синхронизации»: {count} "
            f"(ошибок: {len(errors)})"
        )

        if errors:
            QMessageBox.warning(
                self, "Синхронизация",
                f"Снято отметок: {count}\n"
                f"Ошибок: {len(errors)}\n\n"
                + "\n".join(errors[:20]),
            )
        else:
            QMessageBox.information(
                self, "Синхронизация",
                f"Снято отметок: {count}",
            )

    # ------------------------------------------------------------------
    # Скачивание
    # ------------------------------------------------------------------
    def _on_load_remote_records(self) -> None:
        manager = self._get_manager()
        if not manager.is_configured():
            QMessageBox.warning(
                self, "Синхронизация",
                "Укажите base_url и api_key в "
                "Настройки → Синхронизация.",
            )
            return

        project = self.download_project_combo.currentData() or ""
        year = self.download_year_combo.currentData() or ""
        month = self.download_month_combo.currentData() or ""

        self._append_log(
            f"Запрос списка с сервера: project={project!r}, "
            f"year={year!r}, month={month!r}"
        )

        async def _factory(progress):
            async with ScrecClient(
                base_url=manager.settings["base_url"],
                api_key=manager.settings["api_key"],
                connect_timeout=float(
                    manager.settings.get("connect_timeout", 15)
                ),
                read_timeout=float(
                    manager.settings.get("read_timeout", 120)
                ),
            ) as client:
                tree = await client.get_tree()
                records: List[Dict[str, Any]] = []

                if project and year and month:
                    items = await client.get_month_records(
                        project, year, month
                    )
                    for it in items:
                        it["_project"] = project
                        it["_year"] = year
                        it["_month"] = month
                    records = items
                elif project and year:
                    months = await client.get_months(project, year)
                    for m in months:
                        items = await client.get_month_records(
                            project, year, m
                        )
                        for it in items:
                            it["_project"] = project
                            it["_year"] = year
                            it["_month"] = m
                        records.extend(items)
                elif project:
                    years = await client.get_years(project)
                    for y in years:
                        months = await client.get_months(project, y)
                        for m in months:
                            items = await client.get_month_records(
                                project, y, m
                            )
                            for it in items:
                                it["_project"] = project
                                it["_year"] = y
                                it["_month"] = m
                            records.extend(items)
                else:
                    for proj in tree:
                        pname = proj.get("name") or ""
                        if not pname:
                            continue
                        if pname == "_config":
                            continue
                        years = await client.get_years(pname)
                        for y in years:
                            months = await client.get_months(
                                pname, y
                            )
                            for m in months:
                                items = await client.get_month_records(
                                    pname, y, m
                                )
                                for it in items:
                                    it["_project"] = pname
                                    it["_year"] = y
                                    it["_month"] = m
                                records.extend(items)

                return {"tree": tree, "records": records}

        def _on_ok(data: Dict[str, Any]) -> None:
            self._remote_tree = data["tree"]
            self._render_remote_records(data["records"])
            self._refresh_remote_project_combo()
            self._append_log(
                f"Получено записей: {len(data['records'])}"
            )

        self._start_worker(_factory, on_ok=_on_ok)

    def _refresh_remote_project_combo(self) -> None:
        current = self.download_project_combo.currentData() or ""
        self.download_project_combo.blockSignals(True)
        self.download_project_combo.clear()
        self.download_project_combo.addItem("— все проекты —", "")
        for proj in self._remote_tree:
            name = proj.get("name") or ""
            if name and name != "_config":
                self.download_project_combo.addItem(name, name)
        idx = self.download_project_combo.findData(current)
        if idx >= 0:
            self.download_project_combo.setCurrentIndex(idx)
        self.download_project_combo.blockSignals(False)

    def _on_download_project_changed(self) -> None:
        name = self.download_project_combo.currentData() or ""
        self.download_year_combo.blockSignals(True)
        self.download_year_combo.clear()
        self.download_year_combo.addItem("— все —", "")
        self.download_month_combo.blockSignals(True)
        self.download_month_combo.clear()
        self.download_month_combo.addItem("— все —", "")
        self.download_month_combo.blockSignals(False)
        self.download_year_combo.blockSignals(False)

        if not name:
            return
        for proj in self._remote_tree:
            if proj.get("name") == name:
                for y in proj.get("years") or []:
                    self.download_year_combo.addItem(str(y), str(y))
                break

    def _on_download_year_changed(self) -> None:
        project = self.download_project_combo.currentData() or ""
        year = self.download_year_combo.currentData() or ""
        self.download_month_combo.blockSignals(True)
        self.download_month_combo.clear()
        self.download_month_combo.addItem("— все —", "")
        self.download_month_combo.blockSignals(False)

        if not (project and year):
            return

        manager = self._get_manager()
        if not manager.is_configured():
            return

        async def _factory(progress):
            async with ScrecClient(
                base_url=manager.settings["base_url"],
                api_key=manager.settings["api_key"],
            ) as client:
                return await client.get_months(project, year)

        def _on_ok(months: List[str]) -> None:
            for m in months:
                self.download_month_combo.addItem(str(m), str(m))

        self._start_worker(_factory, on_ok=_on_ok)

    def _render_remote_records(
        self, records: List[Dict[str, Any]]
    ) -> None:
        self.download_table.setRowCount(0)
        self._remote_records = list(records or [])
        for r in self._remote_records:
            row = self.download_table.rowCount()
            self.download_table.insertRow(row)
            self.download_table.setItem(
                row, 0, QTableWidgetItem(str(r.get("date") or ""))
            )
            self.download_table.setItem(
                row, 1,
                QTableWidgetItem(str(r.get("name") or "")),
            )
            self.download_table.setItem(
                row, 2,
                QTableWidgetItem(str(r.get("_project") or "")),
            )
            self.download_table.setItem(
                row, 3,
                QTableWidgetItem(str(r.get("folder_name") or "")),
            )
            self.download_table.setItem(
                row, 4,
                QTableWidgetItem(
                    str(r.get("artifacts_count") or 0)
                ),
            )
            self.download_table.setItem(
                row, 5,
                QTableWidgetItem(str(r.get("id") or "")),
            )

        self.download_stats_label.setText(
            f"Записей на сервере: {len(self._remote_records)}"
        )

    def _selected_download_ids(self) -> List[str]:
        records = self._remote_records or []
        result: List[str] = []
        for i in self.download_table.selectionModel().selectedRows():
            r = i.row()
            if 0 <= r < len(records):
                rid = str(records[r].get("id") or "")
                if rid:
                    result.append(rid)
        return result

    def _on_download_selected(self) -> None:
        ids = self._selected_download_ids()
        if not ids:
            QMessageBox.information(
                self, "Синхронизация",
                "Выберите одну или несколько записей в таблице.",
            )
            return
        self._download_records(ids)

    def _on_download_manual(self) -> None:
        rid = self.manual_record_input.text().strip()
        if not rid:
            QMessageBox.information(
                self, "Синхронизация",
                "Введите Record ID.",
            )
            return
        self._download_records([rid])

    def _download_records(self, ids: List[str]) -> None:
        manager = self._get_manager()
        if not manager.is_configured():
            QMessageBox.warning(
                self, "Синхронизация",
                "Укажите base_url и api_key в "
                "Настройки → Синхронизация.",
            )
            return

        force_overwrite = self.force_overwrite_check.isChecked()
        download_media = self.download_media_check.isChecked()

        self._append_log(
            f"Скачивание {len(ids)} записей с сервера "
            f"(force_overwrite={force_overwrite}, "
            f"download_media={download_media})…"
        )

        async def _factory(progress):
            results = []
            for i, rid in enumerate(ids, start=1):
                progress(f"[{i}/{len(ids)}] {rid}")
                try:
                    res = await manager.download_record(
                        rid, progress_cb=progress,
                        force_overwrite=force_overwrite,
                        download_media=download_media,
                    )
                    results.append({
                        "id": rid, "ok": True, "result": res,
                    })
                except ScrecError as exc:
                    results.append({
                        "id": rid, "ok": False, "error": str(exc),
                    })
            return results

        def _on_ok(results: List[Dict[str, Any]]) -> None:
            ok = sum(1 for r in results if r["ok"])
            fail = len(results) - ok
            self._append_log(
                f"Скачано: {ok}, ошибок: {fail}"
            )
            self._refresh_local_sessions()

            if fail == 0:
                QMessageBox.information(
                    self, "Синхронизация",
                    f"Скачано записей: {ok}",
                )
            else:
                lines = [
                    f"• {r['id']}: {r['error']}"
                    for r in results if not r["ok"]
                ]
                QMessageBox.warning(
                    self, "Синхронизация",
                    f"Скачано: {ok}\nОшибок: {fail}\n\n"
                    + "\n".join(lines[:20]),
                )

        self._start_worker(_factory, on_ok=_on_ok)

    # ------------------------------------------------------------------
    # Дельта
    # ------------------------------------------------------------------
    def _on_pull_changes(self) -> None:
        manager = self._get_manager()
        if not manager.is_configured():
            QMessageBox.warning(
                self, "Синхронизация",
                "Укажите base_url и api_key в "
                "Настройки → Синхронизация.",
            )
            return

        self._append_log(
            f"Запрос изменений (since="
            f"{manager.get_last_revision()})…"
        )

        async def _factory(progress):
            return await manager.pull_changes(
                progress_cb=progress
            )

        def _on_ok(result: Dict[str, Any]) -> None:
            self._append_log(
                f"Дельта применена: {result['applied']} "
                f"изменений, ревизия={result['last_revision']}"
            )
            self.last_revision_label.setText(
                str(result["last_revision"])
            )
            self._refresh_local_sessions()

            if result["errors"]:
                QMessageBox.warning(
                    self, "Синхронизация",
                    f"Применено: {result['applied']}\n"
                    f"Ошибок: {len(result['errors'])}\n\n"
                    + "\n".join(result["errors"][:20]),
                )
            else:
                QMessageBox.information(
                    self, "Синхронизация",
                    f"Применено изменений: {result['applied']}\n"
                    f"Новая ревизия: {result['last_revision']}",
                )

        self._start_worker(_factory, on_ok=_on_ok)

    def _on_pull_snapshot(self) -> None:
        manager = self._get_manager()
        if not manager.is_configured():
            QMessageBox.warning(
                self, "Синхронизация",
                "Укажите base_url и api_key в "
                "Настройки → Синхронизация.",
            )
            return

        reply = QMessageBox.question(
            self, "Полная синхронизация",
            "Будет скачан полный список записей с сервера и "
            "разложен по локальным папкам sessions/.\n\n"
            "Это может занять время. Продолжить?",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._append_log("Запрос snapshot с сервера…")

        download_media = self.download_media_check.isChecked()

        async def _factory(progress):
            async with ScrecClient(
                base_url=manager.settings["base_url"],
                api_key=manager.settings["api_key"],
                connect_timeout=float(
                    manager.settings.get("connect_timeout", 15)
                ),
                read_timeout=float(
                    manager.settings.get("read_timeout", 120)
                ),
            ) as client:
                snap = await client.get_snapshot()
                records = snap.get("records") or []
                server_rev = int(snap.get("server_revision", 0))

                applied = 0
                errors: List[str] = []
                for i, rec in enumerate(records, start=1):
                    rid = rec.get("id") or ""
                    path = str(rec.get("path") or "")
                    if path.startswith("_config/"):
                        continue
                    progress(
                        f"[{i}/{len(records)}] {rid} "
                        f"({rec.get('name', '')})"
                    )
                    try:
                        await manager.download_record(
                            rid, download_media=download_media
                        )
                        applied += 1
                    except ScrecError as exc:
                        errors.append(f"{rid}: {exc}")

                manager.set_last_revision(server_rev)
                return {
                    "total": len(records),
                    "applied": applied,
                    "errors": errors,
                    "revision": server_rev,
                }

        def _on_ok(result: Dict[str, Any]) -> None:
            self._append_log(
                f"Snapshot: всего={result['total']}, "
                f"применено={result['applied']}, "
                f"ошибок={len(result['errors'])}, "
                f"ревизия={result['revision']}"
            )
            self.last_revision_label.setText(
                str(result["revision"])
            )
            self._refresh_local_sessions()

            if not result["errors"]:
                QMessageBox.information(
                    self, "Синхронизация",
                    f"Полная синхронизация завершена.\n"
                    f"Скачано: {result['applied']} из "
                    f"{result['total']}\n"
                    f"Ревизия: {result['revision']}",
                )
            else:
                QMessageBox.warning(
                    self, "Синхронизация",
                    f"Скачано: {result['applied']} из "
                    f"{result['total']}\n"
                    f"Ошибок: {len(result['errors'])}\n\n"
                    + "\n".join(result["errors"][:20]),
                )

        self._start_worker(_factory, on_ok=_on_ok)

    def _on_reset_revision(self) -> None:
        reply = QMessageBox.question(
            self, "Сброс ревизии",
            "Сбросить last_synced_revision в 0?\n\n"
            "Следующая дельта-синхронизация подтянет ВСЁ "
            "заново.",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        manager = self._get_manager()
        manager.set_last_revision(0)
        self.last_revision_label.setText("0")
        self._append_log("Ревизия сброшена в 0")

    # ------------------------------------------------------------------
    # Справочники
    # ------------------------------------------------------------------
    def _on_push_configs(self) -> None:
        manager = self._get_manager()
        if not manager.is_configured():
            QMessageBox.warning(
                self, "Синхронизация",
                "Укажите base_url и api_key в "
                "Настройки → Синхронизация.",
            )
            return

        reply = QMessageBox.question(
            self, "Push справочников",
            "Локальные справочники проектов и тегов будут "
            "отправлены на сервер и перезапишут серверные.\n\n"
            "Продолжить?",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._append_log("Push справочников на сервер…")

        async def _factory(progress):
            return await manager.push_configs(
                progress_cb=progress
            )

        def _on_ok(result: Dict[str, Any]) -> None:
            self._append_log(
                f"Push справочников завершён: "
                f"projects={'OK' if result.get('projects') else '—'}, "
                f"tags={'OK' if result.get('tags') else '—'}"
            )
            QMessageBox.information(
                self, "Синхронизация",
                "Справочники отправлены на сервер.",
            )

        self._start_worker(_factory, on_ok=_on_ok)

    def _on_pull_configs(self) -> None:
        manager = self._get_manager()
        if not manager.is_configured():
            QMessageBox.warning(
                self, "Синхронизация",
                "Укажите base_url и api_key в "
                "Настройки → Синхронизация.",
            )
            return

        reply = QMessageBox.question(
            self, "Pull справочников",
            "Серверные справочники проектов и тегов будут "
            "применены локально и перезапишут текущие.\n\n"
            "Продолжить?",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._append_log("Pull справочников с сервера…")

        async def _factory(progress):
            return await manager.pull_configs(
                progress_cb=progress
            )

        def _on_ok(result: Dict[str, Any]) -> None:
            self._append_log(
                f"Pull справочников: "
                f"projects={'OK' if result['projects'] else 'нет'}, "
                f"tags={'OK' if result['tags'] else 'нет'}, "
                f"errors={len(result['errors'])}"
            )
            self._refresh_configs_preview()

            if result["errors"]:
                QMessageBox.warning(
                    self, "Синхронизация",
                    "Ошибки при pull:\n\n"
                    + "\n".join(result["errors"][:20]),
                )
            else:
                QMessageBox.information(
                    self, "Синхронизация",
                    f"Справочники обновлены.\n"
                    f"Проекты: "
                    f"{'да' if result['projects'] else 'нет'}\n"
                    f"Теги: {'да' if result['tags'] else 'нет'}",
                )

        self._start_worker(_factory, on_ok=_on_ok)

    def _on_sync_configs(self) -> None:
        manager = self._get_manager()
        if not manager.is_configured():
            QMessageBox.warning(
                self, "Синхронизация",
                "Укажите base_url и api_key в "
                "Настройки → Синхронизация.",
            )
            return

        reply = QMessageBox.question(
            self, "Синхронизация справочников",
            "Локальные справочники будут отправлены на сервер, "
            "затем серверные — применены локально.\n\n"
            "Продолжить?",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._append_log("Синхронизация справочников (push + pull)…")

        async def _factory(progress):
            return await manager.sync_configs(
                direction="both",
                progress_cb=progress,
            )

        def _on_ok(result: Dict[str, Any]) -> None:
            self._append_log(
                f"sync_configs: "
                f"errors={len(result['errors'])}"
            )
            self._refresh_configs_preview()

            if result["errors"]:
                QMessageBox.warning(
                    self, "Синхронизация",
                    "Ошибки:\n\n"
                    + "\n".join(result["errors"][:20]),
                )
            else:
                QMessageBox.information(
                    self, "Синхронизация",
                    "Справочники синхронизированы.",
                )

        self._start_worker(_factory, on_ok=_on_ok)

    # ------------------------------------------------------------------
    # Закрытие
    # ------------------------------------------------------------------
    def closeEvent(self, event) -> None:
        if self._worker is not None and self._worker.isRunning():
            reply = QMessageBox.question(
                self, "Синхронизация",
                "Операция ещё выполняется. Закрыть окно?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        event.accept()