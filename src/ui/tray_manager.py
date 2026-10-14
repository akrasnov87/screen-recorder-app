"""Управление иконкой в системном трее.

Изменения:
  • Добавлен пункт меню «Задачи TFS» и сигнал
    open_tfs_requested.
  • УДАЛЕНО: пункт «Начать запись…», сигналы toggle_recording
    и upload_video, метод set_recording_state — механизм записи
    экрана удалён из приложения.
"""
from __future__ import annotations

import os
from typing import Dict, Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import (
    QApplication, QMenu, QSystemTrayIcon,
)

from ..logger import get_logger

log = get_logger(__name__)


def _resources_dir() -> str:
    """
    Возвращает абсолютный путь к папке resources/.

    Модуль лежит в src/ui/tray_manager.py, а resources/ — на два
    уровня выше (рядом с src/).
    """
    return os.path.abspath(
        os.path.join(
            os.path.dirname(__file__),
            "..", "..",
            "resources",
        )
    )


def _icon_path(name: str) -> str:
    """
    Ищет иконку в resources/icons/<name>.svg или .png.

    Возвращает абсолютный путь или "" если файл не найден.
    """
    base = os.path.join(_resources_dir(), "icons")

    for ext in (".png", ".svg"):
        p = os.path.join(base, name + ext)
        if os.path.exists(p):
            return p

    log.warning("Иконка не найдена: %s (искали в %s)", name, base)
    return ""


def _app_icon() -> QIcon:
    """
    Возвращает иконку приложения.

    Приоритет:
      1. QApplication.windowIcon() — если уже задана в main.py
         (одинаковая иконка в доке и в трее).
      2. resources/icons/app.png / .svg — fallback.
      3. Пустая QIcon — тогда трей покажет системную заглушку.
    """
    app = QApplication.instance()
    if app is not None:
        icon = app.windowIcon()
        if not icon.isNull():
            return icon

    path = _icon_path("app")
    if path:
        return QIcon(path)

    log.warning(
        "Не удалось получить иконку приложения: "
        "ни QApplication.windowIcon(), ни resources/icons/app.* "
        "не найдены"
    )
    return QIcon()


class TrayManager(QObject):
    """Менеджер системного трея."""

    open_yandex_vm_requested = Signal()
    open_settings_requested = Signal()
    open_queue_requested = Signal()
    open_sessions_requested = Signal()
    open_library_requested = Signal()
    open_sync_requested = Signal()
    import_requested = Signal()
    quit_requested = Signal()
    open_tasks_requested = Signal()
    open_tasks_editor_requested = Signal()
    # --- НОВОЕ: окно «Задачи TFS» ---
    open_tfs_requested = Signal()
    # --- НОВОЕ: создание записи-документа ---
    create_document_requested = Signal()
    # --- НОВОЕ: загрузка внешнего видео ---
    upload_video_requested = Signal()

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

    def create_tray_icon(self) -> None:
        log.info("Создание иконки в системном трее")

        icon = _app_icon()
        if icon.isNull():
            log.warning(
                "Иконка трея пустая — будет использована "
                "системная заглушка"
            )

        self._tray = QSystemTrayIcon(icon, self.parent())
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

        # --- УДАЛЕНО: пункт «Начать запись…» ---

        upload_action = QAction("Загрузить видео", menu)
        upload_action.setToolTip(
            "Выбрать видеофайл и поставить его в очередь обработки"
        )
        upload_action.triggered.connect(self.upload_video)
        menu.addAction(upload_action)

        # --- Создать запись из документов ---
        create_doc_action = QAction(
            "Создать запись (документ)…", menu
        )
        create_doc_action.setToolTip(
            "Создать запись без видео — только из документов "
            "(Word, PDF, TXT, MD, CSV, XLSX и т.п.).\n\n"
            "Текст документов используется для формирования "
            "summary и DeepSeek-промпта."
        )
        create_doc_action.triggered.connect(
            self.create_document_record
        )
        menu.addAction(create_doc_action)

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

        tasks_action = QAction("Поручения", menu)
        tasks_action.setToolTip(
            "Сводное окно поручений по всем записям: фильтры "
            "по статусу, исполнителю и контексту, отправка в Bitrix24"
        )
        tasks_action.triggered.connect(self.open_tasks)
        menu.addAction(tasks_action)

        # --- Пункт «Задачи TFS» ---
        tfs_action = QAction("Задачи TFS", menu)
        tfs_action.setToolTip(
            "Просмотр списка задач из TFS / Azure DevOps Server "
            "по WIQL-запросу.\n\n"
            "Настраивается в Настройки → TFS."
        )
        tfs_action.triggered.connect(self.open_tfs)
        menu.addAction(tfs_action)

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
        log.info(
            "Контекстное меню создано: %d действий",
            len(menu.actions()),
        )

    def open_tasks(self) -> None:
        log.debug("Клик: открыть окно «Поручения»")
        self.open_tasks_requested.emit()

    def open_tfs(self) -> None:
        log.debug("Клик: открыть окно «Задачи TFS»")
        self.open_tfs_requested.emit()

    # --- УДАЛЕНО: set_recording_state() ---

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

    # --- УДАЛЕНО: toggle_recording() ---

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

    def create_document_record(self) -> None:
        """Клик: создать запись из документов."""
        log.debug("Клик: создать запись-документ")
        self.create_document_requested.emit()

    def quit_app(self) -> None:
        log.info("Запрошен выход из приложения")
        self.quit_requested.emit()

    def _on_activated(
        self, reason: QSystemTrayIcon.ActivationReason,
    ) -> None:
        log.debug("Активация трея: %s", reason)
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.open_settings()