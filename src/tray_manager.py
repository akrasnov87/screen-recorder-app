"""Управление иконкой в системном трее.

Изменения:
  • Таймаут уведомлений и максимальные длины берутся из
    config["app"]: notification_timeout_ms,
    notification_max_title, notification_max_message.
  • Добавлен пункт меню «Синхронизация» и сигнал
    open_sync_requested.
"""
from __future__ import annotations

import os
from typing import Dict, Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from .logger import get_logger

log = get_logger(__name__)


def _icon_path(name: str) -> str:
    base = os.path.join(
        os.path.dirname(__file__), "..", "resources", "icons"
    )
    for ext in (".svg", ".png"):
        p = os.path.join(base, name + ext)
        if os.path.exists(p):
            return p
    log.warning("Иконка не найдена: %s", name)
    return ""


class TrayManager(QObject):
    """Менеджер системного трея."""

    open_yandex_vm_requested = Signal()
    toggle_recording_requested = Signal()
    open_settings_requested = Signal()
    open_queue_requested = Signal()
    open_sessions_requested = Signal()
    open_library_requested = Signal()
    open_sync_requested = Signal()
    import_requested = Signal()
    upload_video_requested = Signal()
    quit_requested = Signal()

    def __init__(
        self,
        config: Optional[Dict] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._config = config or {}
        app_cfg = self._config.get("app", {}) or {}
        self._notify_timeout_ms = int(
            app_cfg.get("notification_timeout_ms", 4000)
        )
        self._max_title = int(app_cfg.get("notification_max_title", 50))
        self._max_message = int(
            app_cfg.get("notification_max_message", 100)
        )

        self._tray: Optional[QSystemTrayIcon] = None
        self._menu: Optional[QMenu] = None
        self._state = "idle"
        self._record_action: Optional[QAction] = None

    def create_tray_icon(self) -> None:
        log.info("Создание иконки в системном трее")
        self._tray = QSystemTrayIcon(
            QIcon(_icon_path("app")), self.parent()
        )
        self._tray.setToolTip("Screen Recorder")
        self.create_context_menu()
        self._tray.activated.connect(self._on_activated)
        self._tray.show()
        log.info("Иконка трея показана")

    def create_context_menu(self) -> None:
        if self._tray is None:
            log.warning("create_context_menu: трей ещё не создан")
            return

        menu = QMenu()
        self._menu = menu

        self._record_action = QAction("Начать запись…", menu)
        self._record_action.triggered.connect(self.toggle_recording)
        menu.addAction(self._record_action)

        menu.addSeparator()

        upload_action = QAction("Загрузить видео", menu)
        upload_action.setToolTip(
            "Выбрать видеофайл и поставить его в очередь обработки"
        )
        upload_action.triggered.connect(self.upload_video)
        menu.addAction(upload_action)

        import_action = QAction("Импорт", menu)
        import_action.setToolTip(
            "Импортировать готовые видео, стенограммы и протоколы"
        )
        import_action.triggered.connect(self.open_import)
        menu.addAction(import_action)

        menu.addSeparator()

        sessions_action = QAction("Записи", menu)
        sessions_action.triggered.connect(self.open_sessions)
        menu.addAction(sessions_action)

        library_action = QAction("Библиотека", menu)
        library_action.setToolTip(
            "Полнотекстовый поиск по стенограммам, протоколам, "
            "summary и вложениям"
        )
        library_action.triggered.connect(self.open_library)
        menu.addAction(library_action)

        yandex_vm_action = QAction("ВМ Yandex", menu)
        yandex_vm_action.setToolTip(
            "Просмотр и редактирование конфигураций виртуальных "
            "машин Yandex Cloud (расписание и исключения)"
        )
        yandex_vm_action.triggered.connect(self.open_yandex_vm)
        menu.addAction(yandex_vm_action)

        queue_action = QAction("Очередь задач", menu)
        queue_action.triggered.connect(self.open_queue)
        menu.addAction(queue_action)

        sync_action = QAction("Синхронизация с сервером", menu)
        sync_action.setToolTip(
            "Публикация записей на удалённый сервер, скачивание "
            "изменений и дельта-синхронизация"
        )
        sync_action.triggered.connect(self.open_sync)
        menu.addAction(sync_action)

        menu.addSeparator()

        settings_action = QAction("Настройки", menu)
        settings_action.triggered.connect(self.open_settings)
        menu.addAction(settings_action)

        menu.addSeparator()

        quit_action = QAction("Выход", menu)
        quit_action.triggered.connect(self.quit_app)
        menu.addAction(quit_action)

        self._tray.setContextMenu(menu)
        log.info("Контекстное меню создано: %d действий",
                 len(menu.actions()))

    def set_recording_state(self, state: str) -> None:
        log.info("Состояние трея: %s → %s", self._state, state)
        self._state = state
        if self._tray is None:
            return
        icon_map = {
            "idle": "app",
            "recording": "recording",
            "paused": "paused",
            "processing": "processing",
        }
        self._tray.setIcon(
            QIcon(_icon_path(icon_map.get(state, "app")))
        )
        if self._record_action:
            if state in ("recording", "paused"):
                self._record_action.setText("Остановить запись")
            else:
                self._record_action.setText("Начать запись")

    def open_yandex_vm(self) -> None:
        log.debug("Клик: открыть окно «ВМ Yandex»")
        self.open_yandex_vm_requested.emit()

    def show_notification(
        self,
        title: str,
        message: str,
        icon: QSystemTrayIcon.MessageIcon = (
            QSystemTrayIcon.MessageIcon.Information
        ),
    ) -> None:
        log.info("Уведомление: %s — %s", title, message)
        if self._tray is None:
            return
        self._tray.showMessage(
            title[:self._max_title],
            message[:self._max_message],
            icon,
            self._notify_timeout_ms,
        )

    def toggle_recording(self) -> None:
        log.debug("Клик: переключить запись")
        self.toggle_recording_requested.emit()

    def open_settings(self) -> None:
        log.debug("Клик: открыть настройки")
        self.open_settings_requested.emit()

    def open_queue(self) -> None:
        log.debug("Клик: открыть очередь задач")
        self.open_queue_requested.emit()

    def open_sessions(self) -> None:
        log.debug("Клик: открыть список записей")
        self.open_sessions_requested.emit()

    def open_library(self) -> None:
        log.debug("Клик: открыть библиотеку")
        self.open_library_requested.emit()

    def open_sync(self) -> None:
        log.debug("Клик: открыть синхронизацию")
        self.open_sync_requested.emit()

    def open_import(self) -> None:
        log.debug("Клик: открыть импорт материалов")
        self.import_requested.emit()

    def upload_video(self) -> None:
        log.debug("Клик: загрузить видео")
        self.upload_video_requested.emit()

    def quit_app(self) -> None:
        log.info("Запрошен выход из приложения")
        self.quit_requested.emit()

    def _on_activated(
        self, reason: QSystemTrayIcon.ActivationReason,
    ) -> None:
        log.debug("Активация трея: %s", reason)
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.open_settings()