"""Управление иконкой в системном трее."""
from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from .logger import get_logger

log = get_logger(__name__)


def _icon_path(name: str) -> str:
    base = os.path.join(os.path.dirname(__file__), "..", "resources", "icons")
    for ext in (".svg", ".png"):
        p = os.path.join(base, name + ext)
        if os.path.exists(p):
            return p
    log.warning("Иконка не найдена: %s", name)
    return ""


class TrayManager(QObject):
    """Менеджер системного трея."""

    toggle_recording_requested = Signal()
    open_settings_requested = Signal()
    open_queue_requested = Signal()
    open_sessions_requested = Signal()
    open_library_requested = Signal()
    import_requested = Signal()
    upload_video_requested = Signal()
    quit_requested = Signal()

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._tray: Optional[QSystemTrayIcon] = None
        self._menu: Optional[QMenu] = None
        self._state = "idle"
        self._record_action: Optional[QAction] = None

    def create_tray_icon(self) -> None:
        log.info("Создание иконки в системном трее")
        self._tray = QSystemTrayIcon(QIcon(_icon_path("app")), self.parent())
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

        # --- Загрузить видео ---
        upload_action = QAction("Загрузить видео", menu)
        upload_action.setToolTip(
            "Выбрать видеофайл и поставить его в очередь обработки"
        )
        upload_action.triggered.connect(self.upload_video)
        menu.addAction(upload_action)

        # --- Импорт готовых материалов (новое) ---
        import_action = QAction("Импорт", menu)
        import_action.setToolTip(
            "Импортировать готовые видео, стенограммы и протоколы"
        )
        import_action.triggered.connect(self.open_import)
        menu.addAction(import_action)

        menu.addSeparator()

        # --- Записи ---
        sessions_action = QAction("Записи", menu)
        sessions_action.triggered.connect(self.open_sessions)
        menu.addAction(sessions_action)

        # --- Библиотека ---
        library_action = QAction("Библиотека…", menu)
        library_action.setToolTip(
            "Полнотекстовый поиск по стенограммам, протоколам, "
            "summary и вложениям"
        )
        library_action.triggered.connect(self.open_library)
        menu.addAction(library_action)

        # --- Очередь ---
        queue_action = QAction("Очередь задач", menu)
        queue_action.triggered.connect(self.open_queue)
        menu.addAction(queue_action)

        menu.addSeparator()

        settings_action = QAction("Настройки", menu)
        settings_action.triggered.connect(self.open_settings)
        menu.addAction(settings_action)

        menu.addSeparator()

        quit_action = QAction("Выход", menu)
        quit_action.triggered.connect(self.quit_app)
        menu.addAction(quit_action)

        self._tray.setContextMenu(menu)
        log.info("Контекстное меню создано: %d действий", len(menu.actions()))

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
        self._tray.setIcon(QIcon(_icon_path(icon_map.get(state, "app"))))
        if self._record_action:
            if state in ("recording", "paused"):
                self._record_action.setText("Остановить запись")
            else:
                self._record_action.setText("Начать запись")

    def show_notification(self, title: str, message: str,
                          icon: QSystemTrayIcon.MessageIcon = QSystemTrayIcon.MessageIcon.Information) -> None:
        log.info("Уведомление: %s — %s", title, message)
        if self._tray is None:
            return
        self._tray.showMessage(title[:50], message[:100], icon, 4000)

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

    def open_import(self) -> None:
        log.debug("Клик: открыть импорт материалов")
        self.import_requested.emit()

    def upload_video(self) -> None:
        log.debug("Клик: загрузить видео")
        self.upload_video_requested.emit()

    def quit_app(self) -> None:
        log.info("Запрошен выход из приложения")
        self.quit_requested.emit()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        log.debug("Активация трея: %s", reason)
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.open_settings()