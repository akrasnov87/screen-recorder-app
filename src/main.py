"""Точка входа приложения Screen Recorder & Transcriber.

Изменения:
  • Добавлена интеграция с TFS / Azure DevOps Server.
  • В _ask_metadata() добавлена передача relations_max_in_dialog
    из app-настроек и session_dir в initial.
  • В _build_default_meta() добавлено поле relations.
  • УДАЛЕНО: механизм записи экрана (ScreenRecorder,
    GlobalHotkeyManager, OverlayPanel) и связанные сигналы.
    Приложение работает как «обработчик готовых видео и
    документов»: загрузка видео, импорт, запись-документ.
  • УДАЛЕНО: механизм управления ВМ Yandex — вкладка в
    настройках, окно «ВМ Yandex», сигнал open_yandex_vm_requested,
    обработка vm_session в результате задачи.
  • КРИТИЧНО (fix segfault):
      – Включён faulthandler (пишет C-стек в
        /tmp/screen-recorder/faulthandler.log при segfault).
      – _quit() теперь:
          * закрывает все открытые окна,
          * отменяет sync-puller и processing task,
          * ждёт их завершения,
          * и только потом останавливает loop.
        Это устраняет падение при выходе, когда фоновые
        корутины продолжают работать, а Qt уже разобрал
        свои объекты.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sys
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

if __package__ in (None, ""):
    sys.path.insert(
        0,
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    from src.config_manager import ConfigManager
    from src.ui.import_window import ImportWindow
    from src.ui.library_window import LibraryWindow
    from src.ui.metadata_dialog import MetadataDialog
    from src.ui.queue_window import QueueWindow
    from src.ui.sessions_window import SessionsWindow
    from src.ui.settings_window import SettingsWindow
    from src.ui.sync_window import SyncWindow
    from src.ui.tray_manager import TrayManager
    from src.ui.tasks_window import TasksWindow
    from src.ui.tfs_window import TfsWindow
    from src.logger import (
        get_logger,
        register_gui_handler,
        set_log_path,
        setup_logger,
    )
    from src.processor import VideoProcessor
    from src.sync_manager import SyncManager, is_record_published
    from src.task_queue import TaskQueue
    from src.platform_utils import IS_LINUX, IS_WINDOWS
    from src.utils import (
        check_ffmpeg_installed,
        safe_local_path,
        sanitize_filename,
    )
else:
    from .config_manager import ConfigManager
    from .ui.import_window import ImportWindow
    from .ui.library_window import LibraryWindow
    from .ui.metadata_dialog import MetadataDialog
    from .ui.queue_window import QueueWindow
    from .ui.sessions_window import SessionsWindow
    from .ui.settings_window import SettingsWindow
    from .ui.sync_window import SyncWindow
    from .ui.tray_manager import TrayManager
    from .ui.tasks_window import TasksWindow
    from .ui.tfs_window import TfsWindow
    from .logger import (
        get_logger,
        register_gui_handler,
        set_log_path,
        setup_logger,
    )
    from .processor import VideoProcessor
    from .sync_manager import SyncManager, is_record_published
    from .task_queue import TaskQueue
    from .platform_utils import IS_LINUX, IS_WINDOWS
    from .utils import (
        check_ffmpeg_installed,
        safe_local_path,
        sanitize_filename,
    )

from PySide6.QtCore import QObject, QTimer, QUrl, Slot, Qt
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QMessageBox, QSystemTrayIcon,
)

log = get_logger(__name__)


class AppState(Enum):
    """Явные состояния приложения."""
    IDLE = "idle"
    PROCESSING = "processing"
    QUITTING = "quitting"


def _resource(name: str) -> str:
    base = os.path.join(os.path.dirname(__file__), "..", "resources")
    return os.path.join(base, name)


class ScreenRecorderApp(QObject):
    """Главный контроллер приложения.

    Приложение работает как «пост-обработчик»:
      • загрузка готовых видеофайлов;
      • импорт видео/стенограмм/протоколов;
      • создание записей-документов;
      • транскрибация, суммаризация, DeepSeek-промпт;
      • TFS-выборка, поручения;
      • синхронизация с сервером;
      • отправка в Bitrix24.
    """

    def __init__(self, app: QApplication) -> None:
        super().__init__()
        self.app = app
        log.info("Инициализация ScreenRecorderApp")

        self.config_manager = ConfigManager()
        self.app_cfg = self.config_manager.get_app_settings()
        temp_path = self.config_manager.config["storage"]["temp_path"]
        log.info("Временная папка: %s", temp_path)

        self.task_queue = TaskQueue(
            os.path.join(temp_path, "queue.json")
        )
        log.debug("Очередь: %s", self.task_queue.get_queue_status())

        self.processor = VideoProcessor(
            self.config_manager.config,
            self.task_queue,
            is_recording_cb=None,     # записи экрана больше нет
            config_manager=self.config_manager,
            ask_user_cb=self._ask_local_transcribe_async,
        )
        self.tray_manager = TrayManager(self.config_manager.config, self)

        # --- Диагностика: подтверждаем, что callback установлен ---
        log.info(
            "Проверка: processor._ask_user_cb = %r, "
            "processor.config_manager = %r",
            getattr(self.processor, "_ask_user_cb", "<no attr>"),
            getattr(self.processor, "config_manager", "<no attr>"),
        )

        self.settings_window: Optional[SettingsWindow] = None
        self.queue_window: Optional[QueueWindow] = None
        self.sessions_window: Optional[SessionsWindow] = None
        self.library_window: Optional[LibraryWindow] = None
        self.sync_window: Optional[SyncWindow] = None
        self.tasks_window: Optional[TasksWindow] = None
        self.tfs_window: Optional[TfsWindow] = None

        self._state: AppState = AppState.IDLE

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._processing_task: Optional[asyncio.Task] = None
        self._sync_pull_task: Optional[asyncio.Task] = None

        self._connect_signals()
        self._start_async_loop()
        self._maybe_start_sync_puller()
        log.info("ScreenRecorderApp инициализирован")

    # ------------------------------------------------------------------
    # Состояние
    # ------------------------------------------------------------------
    def _set_state(self, new_state: AppState) -> None:
        if self._state == new_state:
            return
        log.info("Состояние приложения: %s → %s",
                 self._state.value, new_state.value)
        self._state = new_state

    @property
    def state(self) -> AppState:
        return self._state

    # ------------------------------------------------------------------
    # Инициализация
    # ------------------------------------------------------------------
    def _connect_signals(self) -> None:
        log.debug("Подключение сигналов")

        # --- Трей: общие пункты меню ---
        self.tray_manager.open_settings_requested.connect(
            self._open_settings
        )
        self.tray_manager.open_queue_requested.connect(self._open_queue)
        self.tray_manager.open_sessions_requested.connect(
            self._open_sessions
        )
        self.tray_manager.open_library_requested.connect(
            self._open_library
        )
        self.tray_manager.open_tasks_requested.connect(
            self._open_tasks_window
        )
        self.tray_manager.open_sync_requested.connect(
            self._open_sync
        )
        self.tray_manager.open_tfs_requested.connect(
            self._open_tfs
        )
        self.tray_manager.create_document_requested.connect(
            self._create_document_record
        )
        self.tray_manager.import_requested.connect(self._open_import)
        self.tray_manager.upload_video_requested.connect(
            self._open_upload_video
        )
        self.tray_manager.quit_requested.connect(self._quit)

        # --- Обработчик задач ---
        self.processor.task_started.connect(self._on_task_started)
        self.processor.task_progress.connect(self._on_task_progress)
        self.processor.task_completed.connect(self._on_task_completed)
        self.processor.task_failed.connect(self._on_task_failed)

        # --- НОВОЕ: прогресс локальной транскрибации ---
        # Записываем «живой» лог в очередь; UI обновит его
        # при следующем refresh.
        self.processor.local_transcribe_progress.connect(
            self._on_local_transcribe_progress
        )

        register_gui_handler(self._on_log_to_gui)

    def _start_async_loop(self) -> None:
        """Запуск asyncio-loop в фоновом потоке."""
        import threading

        log.debug("Запуск asyncio-loop в фоновом потоке")
        self._loop = asyncio.new_event_loop()

        def _runner() -> None:
            asyncio.set_event_loop(self._loop)
            self._processing_task = self._loop.create_task(
                self.processor.run_forever()
            )
            self._loop.run_forever()

        self._async_thread = threading.Thread(
            target=_runner,
            name="asyncio-loop",
            daemon=True,
        )
        self._async_thread.start()
        log.info("asyncio-loop запущен в фоновом потоке")

    # ------------------------------------------------------------------
    # Фоновый воркер синхронизации
    # ------------------------------------------------------------------
    def _maybe_start_sync_puller(self) -> None:
        try:
            cfg = self.config_manager.get_sync_settings()
        except Exception as exc:
            log.warning("Не удалось прочитать sync-настройки: %s", exc)
            return

        if not (cfg.get("enabled") and cfg.get("auto_pull_enabled")):
            log.debug("Фоновый sync-puller не запускается (выключен)")
            return
        if not (cfg.get("base_url") and cfg.get("api_key")):
            log.debug(
                "Фоновый sync-puller не запускается (нет base_url/api_key)"
            )
            return

        interval = int(cfg.get("auto_pull_interval", 300))
        log.info(
            "Запуск фонового sync-puller (интервал=%d сек)", interval
        )

        async def _pull_loop() -> None:
            manager = SyncManager(
                sessions_root=self._sessions_root(),
                sync_settings=cfg,
                config_manager=self.config_manager,
            )
            while True:
                try:
                    await asyncio.sleep(interval)
                    res = await manager.pull_changes()
                    if res.get("applied"):
                        log.info(
                            "Фоновый sync: применено %d изменений, "
                            "ревизия=%d",
                            res["applied"], res["last_revision"],
                        )
                except asyncio.CancelledError:
                    log.info("Фоновый sync-puller остановлен")
                    raise
                except Exception as exc:
                    log.warning("Ошибка фонового sync: %s", exc)

        if self._loop is not None:
            self._sync_pull_task = self._loop.create_task(_pull_loop())

    # ------------------------------------------------------------------
    # Геттеры конфига
    # ------------------------------------------------------------------
    def _sessions_root(self) -> str:
        return self.config_manager.get_sessions_root()

    # ------------------------------------------------------------------
    # Метаданные
    # ------------------------------------------------------------------
    def _resolve_default_project(self) -> str:
        projects = self.config_manager.get_project_names()
        default_project = (
            self.config_manager.get_default_project()
        )

        if default_project and default_project in projects:
            return default_project

        if default_project and default_project not in projects:
            log.warning(
                "Проект по умолчанию %r не найден в списке "
                "проектов — используется первый из списка",
                default_project,
            )

        if projects:
            return projects[0]
        return "Default"

    def _ask_metadata(
        self,
        title: str = "Метаданные записи",
        initial: Optional[Dict[str, Any]] = None,
        *,
        allow_document_only: bool = False,
    ) -> Optional[Dict[str, Any]]:
        projects = self.config_manager.get_project_names()
        prompts = self.config_manager.get_prompts()
        default_prompt = self.config_manager.get_default_prompt()
        name_templates = self.config_manager.get_name_templates()
        tags = self.config_manager.get_tags()

        init = dict(initial or {})

        if not init.get("project"):
            init["project"] = self._resolve_default_project()

        project = init.get("project")
        if project and project not in projects:
            projects = [project] + projects

        try:
            tfs_settings = self.config_manager.get_tfs_settings()
        except Exception as exc:
            log.warning(
                "Не удалось прочитать TFS-настройки: %s", exc
            )
            tfs_settings = {"enabled": False}

        tfs_templates = list(
            tfs_settings.get("wiql_templates") or []
        )

        def _on_request_tfs_projects() -> List[str]:
            try:
                res = (
                    self.config_manager.get_tfs_projects_cached()
                )
                return list(res.get("projects") or [])
            except Exception as exc:
                log.warning(
                    "Не удалось получить проекты TFS: %s", exc
                )
                return []

        relations_max = int(
            self.app_cfg.get("relations_max_in_dialog", 0) or 0
        )

        dlg = MetadataDialog(
            projects=projects,
            prompts=prompts,
            title=title,
            initial=init,
            default_prompt=default_prompt,
            sessions_root=self._sessions_root(),
            on_save_prompt=self._add_prompt_to_library,
            get_prompts=self.config_manager.get_prompts,
            name_templates=name_templates,
            on_save_name_template=self._add_name_template,
            get_name_templates=self.config_manager.get_name_templates,
            tags=tags,
            on_save_tag=self._add_tag_to_library,
            get_tags=self.config_manager.get_tags,
            tfs_settings=tfs_settings,
            tfs_templates=tfs_templates,
            on_request_tfs_projects=_on_request_tfs_projects,
            default_tfs_save_attachment=bool(
                self.app_cfg.get("tfs_save_attachment", True)
            ),
            allow_document_only=allow_document_only,
            relations_max_in_dialog=relations_max,
            parent=None,
        )
        if dlg.exec() == dlg.DialogCode.Accepted:
            result = dlg.result_data
            new_prompt = result.pop("new_prompt", None)
            if new_prompt and isinstance(new_prompt, dict):
                self._add_prompt_to_library(
                    new_prompt.get("name", ""),
                    new_prompt.get("text", ""),
                )
            log.info(
                "Диалог метаданных завершён: Продолжить "
                "(relations=%d)",
                len(result.get("relations") or []),
            )
            return result
        log.info("Диалог метаданных завершён: Пропустить")
        return None

    def _build_default_meta(self) -> Dict[str, Any]:
        default_project = self._resolve_default_project()
        now = datetime.now()
        now_str = f"{now:%Y-%m-%d %H-%M}"

        sum_cfg = self.config_manager.get_summarizer_settings()
        default_generate_summary = bool(sum_cfg.get("enabled", False))

        meta = {
            "project": default_project,
            "name": f"Запись {now_str}",
            "description": f"Запись {now_str}",
            "name_template": "",
            "name_abbr": "",
            "comment": "",
            "tags": [],
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
            "sync_ready": False,
            "skip_transcription": True,
            "prompt": self.config_manager.get_default_prompt(),
            "prompt_name": "",
            "prompt_edited": False,
            "generate_summary": default_generate_summary,
            "is_scrum": False,
            "generate_deepseek_prompt": True,
            "include_name_in_prompt": True,
            "include_project_in_prompt": True,
            "include_comment_in_prompt": True,
            "include_tags_in_prompt": True,
            "previous_protocol_path": "",
            "attachments": [],
            "send_attachments_to_transcribe": False,
            "send_attachments_to_deepseek": False,
            "tfs_project": "",
            "tfs_template_name": "",
            "tfs_save_attachment": bool(
                self.app_cfg.get("tfs_save_attachment", True)
            ),
            "relations": [],
        }
        log.debug(
            "Сформированы метаданные по умолчанию: project=%r, "
            "date=%s, time=%s, sync_ready=False, "
            "do_transcribe=True, relations=0",
            default_project,
            meta["date"], meta["time"],
        )
        return meta

    def _add_prompt_to_library(self, name: str, text: str) -> None:
        try:
            name = (name or "").strip()
            text = (text or "").strip()
            if not name or not text:
                log.warning("Промпт пустой, сохранение отменено")
                return

            cfg = self.config_manager.config
            meta = cfg.setdefault("metadata", {})
            prompts = meta.setdefault("prompts", [])
            replaced = False
            for i, p in enumerate(prompts):
                if isinstance(p, dict) and p.get("name") == name:
                    prompts[i] = {"name": name, "text": text}
                    replaced = True
                    break
            if not replaced:
                prompts.append({"name": name, "text": text})

            self.config_manager.save()
            self._notify(
                "Промпты",
                f"Промпт «{name}» сохранён в библиотеку",
            )
        except Exception as exc:
            log.exception("Ошибка сохранения промпта: %s", exc)

    def _add_name_template(self, label: str, template: str) -> None:
        try:
            label = (label or "").strip()
            template = (template or "").strip()
            if not template:
                log.warning("Шаблон имени пустой")
                return
            self.config_manager.add_name_template(label, template)
            self._notify(
                "Шаблоны имён",
                f"Шаблон «{label or template}» сохранён",
            )
        except Exception as exc:
            log.exception("Ошибка сохранения шаблона имени: %s", exc)

    def _add_tag_to_library(self, name: str, color: str = "") -> None:
        try:
            name = (name or "").strip()
            if not name:
                log.warning("Имя тега пустое, сохранение отменено")
                return
            self.config_manager.add_tag(name, color)
            self._notify(
                "Теги",
                f"Тег «{name}» добавлен в справочник",
            )
        except Exception as exc:
            log.exception("Ошибка сохранения тега: %s", exc)

    def _copy_attachments_to_session(
        self,
        meta: Dict[str, Any],
        session_dir: str,
    ) -> None:
        src_paths = list(meta.get("attachments", []) or [])
        if not src_paths:
            return

        att_dir = os.path.join(session_dir, "attachments")
        os.makedirs(att_dir, exist_ok=True)

        log.info("Копирование вложений: %d файлов → %s",
                len(src_paths), att_dir)

        max_chars = int(
            self.app_cfg.get("attachment_name_max_chars", 50)
        )

        new_paths: List[str] = []
        for src in src_paths:
            if not src or not os.path.exists(src):
                log.warning("Вложение не найдено, пропуск: %s", src)
                continue

            base = sanitize_filename(
                os.path.basename(src),
                max_chars=max_chars,
            )
            dst = os.path.join(att_dir, base)
            if os.path.exists(dst):
                stem, ext = os.path.splitext(base)
                i = 1
                while os.path.exists(dst):
                    dst = os.path.join(att_dir, f"{stem}_{i}{ext}")
                    i += 1
            try:
                shutil.copy2(src, dst)
                new_paths.append(dst)
                log.info("Вложение скопировано: %s → %s", src, dst)
            except Exception as exc:
                log.exception(
                    "Ошибка копирования вложения %s: %s", src, exc
                )

        meta["attachments"] = new_paths
        log.info("Вложений скопировано: %d/%d",
                len(new_paths), len(src_paths))

    @staticmethod
    def _save_session_metadata(
        meta: Dict[str, Any], session_dir: str,
    ) -> None:
        try:
            os.makedirs(session_dir, exist_ok=True)
            path = os.path.join(session_dir, "session.json")
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2, ensure_ascii=False)
            os.replace(tmp, path)
            log.info("Метаданные сохранены: %s", path)
        except Exception as exc:
            log.error("Не удалось сохранить метаданные: %s", exc)

    @staticmethod
    def _read_session_meta(session_dir: str) -> Dict[str, Any]:
        path = os.path.join(session_dir, "session.json")
        if not os.path.isfile(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            log.warning("Не удалось прочитать %s: %s", path, exc)
            return {}

    def _new_session_dir(self) -> str:
        temp = self.config_manager.config["storage"].get(
            "temp_path", "/tmp/screen-recorder"
        )
        sessions_root = os.path.join(temp, "sessions")
        session_dir = os.path.join(
            sessions_root,
            datetime.now().strftime("%Y-%m-%d_%H-%M-%S"),
        )
        base = session_dir
        i = 1
        while os.path.exists(session_dir):
            session_dir = f"{base}_{i}"
            i += 1
        os.makedirs(session_dir, exist_ok=True)
        log.debug("Создана папка сессии: %s", session_dir)
        return session_dir

    # ------------------------------------------------------------------
    # Загрузить видео
    # ------------------------------------------------------------------
    @Slot()
    def _open_upload_video(self) -> None:
        log.info("Запрос на загрузку внешнего видео")

        file_path, _ = QFileDialog.getOpenFileName(
            None,
            "Выберите видеофайл",
            os.path.expanduser("~"),
            "Video files (*.mp4 *.mkv *.mov *.avi *.webm *.flv "
            "*.wmv);;All files (*)",
        )
        if not file_path:
            return
        file_path = safe_local_path(file_path)

        if not os.path.isfile(file_path):
            QMessageBox.warning(
                None, "Загрузить видео",
                f"Файл не найден:\n{file_path}",
            )
            return

        initial = {
            "name": os.path.splitext(os.path.basename(file_path))[0],
            "include_name_in_prompt": True,
            "include_project_in_prompt": True,
            "include_comment_in_prompt": True,
            "include_tags_in_prompt": True,
        }

        meta = self._ask_metadata(
            title="Метаданные загружаемого видео",
            initial=initial,
        )
        if meta is None:
            return

        session_dir = self._new_session_dir()

        target_video = os.path.join(session_dir, "video.mp4")
        try:
            src_abs = os.path.abspath(file_path)
            dst_abs = os.path.abspath(target_video)
            if src_abs == dst_abs:
                log.info("Файл уже в папке сессии")
            else:
                shutil.copy2(src_abs, dst_abs)
        except Exception as exc:
            log.exception("Ошибка копирования файла: %s", exc)
            QMessageBox.critical(
                None, "Загрузить видео",
                f"Не удалось скопировать файл:\n{exc}",
            )
            return

        if not os.path.exists(target_video):
            QMessageBox.critical(
                None, "Загрузить видео",
                "Файл не был скопирован",
            )
            return

        meta["date"] = datetime.now().strftime("%Y-%m-%d")
        meta["session_dir"] = session_dir
        meta["video_path"] = target_video
        meta["source"] = "upload"
        meta["source_file"] = src_abs
        meta.pop("task_id", None)

        meta.setdefault("relations", [])

        self._copy_attachments_to_session(meta, session_dir)
        self._save_session_metadata(meta, session_dir)

        try:
            task_id = self.task_queue.add_task(
                {"video_path": target_video, "metadata": meta}
            )
            log.info("Задача %s добавлена в очередь", task_id)
            self._notify(
                "Загрузить видео",
                f"Файл добавлен в очередь: {meta.get('name', '')}",
            )
        except Exception as exc:
            log.exception(
                "Не удалось добавить задачу в очередь: %s", exc
            )
            QMessageBox.critical(
                None, "Загрузить видео",
                f"Не удалось добавить в очередь:\n{exc}",
            )

    @Slot()
    def _create_document_record(self) -> None:
        log.info("Запрос на создание записи-документа")

        initial: Dict[str, Any] = {
            "include_name_in_prompt": True,
            "include_project_in_prompt": True,
            "include_comment_in_prompt": True,
            "include_tags_in_prompt": True,
            "skip_transcription": False,
            "generate_summary": False,
            "generate_deepseek_prompt": True,
            "document_save_transcript": True,
            "document_to_deepseek": True,
        }

        meta = self._ask_metadata(
            title="Создание записи из документов",
            initial=initial,
            allow_document_only=True,
        )
        if meta is None:
            log.info(
                "Пользователь отменил создание записи-документа"
            )
            return

        documents = list(meta.get("documents") or [])
        if not documents:
            attachments = list(meta.get("attachments") or [])
            if attachments:
                log.info(
                    "Документы не выбраны, но есть вложения (%d) — "
                    "используем их как источник текста",
                    len(attachments),
                )
                documents = attachments
                meta["documents"] = documents
                meta["document_only"] = True

        if not documents:
            QMessageBox.warning(
                None, "Записи",
                "Не выбрано ни одного документа.\n\n"
                "Откройте вкладку «Документ» и прикрепите хотя бы "
                "один файл (или добавьте вложение на вкладке "
                "«Вложения»).",
            )
            return

        session_dir = self._new_session_dir()

        att_dir = os.path.join(session_dir, "attachments")
        os.makedirs(att_dir, exist_ok=True)

        max_chars = int(
            self.app_cfg.get("attachment_name_max_chars", 50)
        )

        copied: List[str] = []
        for src in documents:
            if not src or not os.path.isfile(src):
                log.warning("Документ не найден: %s", src)
                continue
            base = sanitize_filename(
                os.path.basename(src), max_chars=max_chars
            )
            dst = os.path.join(att_dir, base)
            if os.path.exists(dst):
                stem, ext = os.path.splitext(base)
                i = 1
                while os.path.exists(dst):
                    dst = os.path.join(
                        att_dir, f"{stem}_{i}{ext}"
                    )
                    i += 1
            try:
                shutil.copy2(src, dst)
                copied.append(dst)
            except Exception as exc:
                log.exception(
                    "Не удалось скопировать документ %s: %s",
                    src, exc,
                )

        if not copied:
            QMessageBox.critical(
                None, "Записи",
                "Не удалось скопировать ни одного документа.",
            )
            return

        meta["attachments"] = copied
        meta["documents"] = copied
        meta["session_dir"] = session_dir
        meta["video_path"] = ""
        meta["source"] = "document"
        meta["document_only"] = True
        meta.pop("task_id", None)
        meta.setdefault("relations", [])

        meta.setdefault("skip_transcription", False)
        meta.setdefault("generate_summary", False)
        meta.setdefault("generate_deepseek_prompt", True)
        meta.setdefault("document_save_transcript", True)
        meta.setdefault("document_to_deepseek", True)

        if not (meta.get("date") or "").strip():
            meta["date"] = datetime.now().strftime("%Y-%m-%d")
        if not (meta.get("time") or "").strip():
            meta["time"] = datetime.now().strftime("%H:%M:%S")

        self._save_session_metadata(meta, session_dir)

        need_processing = bool(
            meta.get("generate_deepseek_prompt")
            or meta.get("generate_summary")
        )

        task_id = ""
        if need_processing:
            try:
                task_payload = {
                    "video_path": "",
                    "metadata": dict(meta),
                }
                task_id = self.task_queue.add_task(task_payload)
                log.info(
                    "Задача-документ добавлена в очередь: %s "
                    "(документов: %d, deepseek=%s, summary=%s)",
                    task_id, len(copied),
                    meta.get("generate_deepseek_prompt"),
                    meta.get("generate_summary"),
                )
            except Exception as exc:
                log.exception(
                    "Не удалось поставить задачу-документ в "
                    "очередь: %s", exc,
                )
                QMessageBox.critical(
                    None, "Записи",
                    f"Не удалось поставить в очередь:\n{exc}",
                )
                return

        if task_id:
            self._notify(
                "Запись-документ",
                f"Запись «{meta.get('name', '')}» создана "
                f"({len(copied)} док.) и поставлена в очередь",
            )
        else:
            self._notify(
                "Запись-документ",
                f"Запись «{meta.get('name', '')}» создана "
                f"({len(copied)} док.) без обработки",
            )

        try:
            QDesktopServices.openUrl(
                QUrl.fromLocalFile(session_dir)
            )
        except Exception as exc:
            log.warning(
                "Не удалось открыть папку сессии: %s", exc
            )

    # ------------------------------------------------------------------
    # Импорт
    # ------------------------------------------------------------------
    @Slot()
    def _open_import(self) -> None:
        log.info("Запрос на импорт материалов")
        projects = self.config_manager.get_project_names()
        tags = self.config_manager.get_tags()

        dlg = ImportWindow(
            projects=projects,
            sessions_root=self._sessions_root(),
            parent=None,
            tags=tags,
            on_save_tag=self._add_tag_to_library,
            get_tags=self.config_manager.get_tags,
        )
        if dlg.exec() != dlg.DialogCode.Accepted:
            return

        data = dlg.result_data
        self._perform_import(data)

    def _perform_import(self, data: Dict[str, Any]) -> None:
        date_dt: datetime = data["date"]
        video_src = data.get("video_path") or ""
        transcript_src = data.get("transcript_path") or ""
        protocol_src = data.get("protocol_path") or ""
        name = data.get("name") or f"Импорт {date_dt:%Y-%m-%d}"
        project = data.get("project") or "Default"
        comment = data.get("comment") or ""
        enqueue = bool(data.get("enqueue"))
        open_folder = bool(data.get("open_folder", True))

        temp = self.config_manager.config["storage"].get(
            "temp_path", "/tmp/screen-recorder"
        )
        sessions_root = os.path.join(temp, "sessions")
        os.makedirs(sessions_root, exist_ok=True)

        base_name = date_dt.strftime("%Y-%m-%d_%H-%M-%S")
        session_dir = os.path.join(sessions_root, base_name)
        i = 1
        while os.path.exists(session_dir):
            session_dir = os.path.join(
                sessions_root, f"{base_name}_{i}"
            )
            i += 1
        os.makedirs(session_dir, exist_ok=True)

        video_dst = ""
        if video_src and os.path.isfile(video_src):
            ext = os.path.splitext(video_src)[1].lower() or ".mp4"
            video_dst = os.path.join(session_dir, f"video{ext}")
            try:
                shutil.copy2(video_src, video_dst)
            except Exception as exc:
                log.exception(
                    "Импорт: ошибка копирования видео: %s", exc
                )
                video_dst = ""

        transcript_dst = ""
        if transcript_src and os.path.isfile(transcript_src):
            ext = os.path.splitext(transcript_src)[1].lower()
            if ext == ".txt":
                transcript_dst = os.path.join(
                    session_dir, "video.txt"
                )
                try:
                    shutil.copy2(transcript_src, transcript_dst)
                except Exception as exc:
                    log.exception(
                        "Импорт: ошибка копирования стенограммы: %s",
                        exc,
                    )
                    transcript_dst = ""
            else:
                text = self._read_imported_text(transcript_src)
                if text:
                    transcript_dst = os.path.join(
                        session_dir, "video.txt"
                    )
                    try:
                        with open(
                            transcript_dst, "w", encoding="utf-8"
                        ) as f:
                            f.write(text)
                    except Exception as exc:
                        log.exception(
                            "Импорт: ошибка записи стенограммы: %s",
                            exc,
                        )
                        transcript_dst = ""

        protocol_dst = ""
        if protocol_src and os.path.isfile(protocol_src):
            ext = (
                os.path.splitext(protocol_src)[1].lower() or ".txt"
            )
            protocol_dst = os.path.join(
                session_dir, f"manual_protocol{ext}"
            )
            try:
                shutil.copy2(protocol_src, protocol_dst)
            except Exception as exc:
                log.exception(
                    "Импорт: ошибка копирования протокола: %s", exc
                )
                protocol_dst = ""

        sum_cfg = self.config_manager.get_summarizer_settings()
        default_generate_summary = bool(sum_cfg.get("enabled", False))

        meta: Dict[str, Any] = {
            "project": project,
            "name": name,
            "description": name,
            "date": date_dt.strftime("%Y-%m-%d"),
            "time": date_dt.strftime("%H:%M:%S"),
            "comment": comment,
            "tags": list(data.get("tags") or []),
            "sync_ready": False,
            "skip_transcription": True,
            "source": "import",
            "source_files": {
                "video": video_src,
                "transcript": transcript_src,
                "protocol": protocol_src,
            },
            "video_path": video_dst,
            "session_dir": session_dir,
            "name_template": "",
            "name_abbr": "",
            "prompt": self.config_manager.get_default_prompt(),
            "prompt_name": "",
            "prompt_edited": False,
            "generate_summary": default_generate_summary,
            "is_scrum": False,
            "generate_deepseek_prompt": True,
            "include_name_in_prompt": True,
            "include_project_in_prompt": True,
            "include_comment_in_prompt": True,
            "include_tags_in_prompt": True,
            "previous_protocol_path": protocol_dst,
            "manual_protocol_path": protocol_dst,
            "attachments": [],
            "send_attachments_to_transcribe": False,
            "send_attachments_to_deepseek": False,
            "tfs_project": "",
            "tfs_template_name": "",
            "tfs_save_attachment": False,
            "relations": [],
        }

        self._save_session_metadata(meta, session_dir)

        task_id = ""
        if enqueue and video_dst:
            try:
                task_payload = {
                    "video_path": video_dst,
                    "metadata": dict(meta),
                }
                task_id = self.task_queue.add_task(task_payload)
            except Exception as exc:
                log.exception(
                    "Импорт: не удалось добавить в очередь: %s", exc
                )

        files_summary = []
        if video_dst:
            files_summary.append("видео")
        if transcript_dst:
            files_summary.append("стенограмма")
        if protocol_dst:
            files_summary.append("протокол")
        files_str = ", ".join(files_summary) or "без файлов"

        if task_id:
            self._notify(
                "Импорт",
                f"Запись «{name}» импортирована ({files_str}), "
                f"поставлена в очередь",
            )
        else:
            self._notify(
                "Импорт",
                f"Запись «{name}» импортирована ({files_str})",
            )

        if open_folder:
            try:
                QDesktopServices.openUrl(
                    QUrl.fromLocalFile(session_dir)
                )
            except Exception as exc:
                log.warning(
                    "Импорт: не удалось открыть папку: %s", exc
                )

    @staticmethod
    def _read_imported_text(path: str) -> str:
        from .file_readers import read_any_text
        return read_any_text(path)

    # ------------------------------------------------------------------
    # Обработчики задач
    # ------------------------------------------------------------------
    def _on_task_started(self, task_id: str) -> None:
        self._set_state(AppState.PROCESSING)

    def _on_task_progress(self, task_id: str, progress: int,
                          stage: str) -> None:
        # Overlay-панели больше нет — прогресс виден в
        # окне «Очередь задач» и в логах.
        log.debug(
            "Прогресс задачи %s: %d%% (%s)",
            task_id, progress, stage,
        )

    # ------------------------------------------------------------------
    # НОВОЕ: локальная транскрибация — прогресс и вопрос
    # ------------------------------------------------------------------
    @Slot(str, str, int)
    def _on_local_transcribe_progress(
        self, task_id: str, message: str, percent: int,
    ) -> None:
        """
        Принимает сообщение о прогрессе локальной транскрибации
        и записывает его в очередь задач.
        """
        log.info(
            "[%s] local_progress: %s (%d%%)",
            task_id, message, percent,
        )
        try:
            self.task_queue.set_task_live_log(
                task_id, message, percent,
            )
        except Exception as exc:
            log.debug(
                "Не удалось записать live_log для %s: %s",
                task_id, exc,
            )

    async def _ask_local_transcribe_async(
        self, reason: str,
    ) -> bool:
        """
        Асинхронный callback: спрашивает у пользователя,
        нужно ли выполнить транскрибацию локально.

        ВАЖНО: вызывается из фонового потока (asyncio-loop
        processor-а). Прямой вызов Qt-виджетов из чужого потока
        НЕБЕЗОПАСЕН, а QTimer.singleShot из чужого потока Qt
        молча игнорирует (именно поэтому окно не появлялось).

        Используем QMetaObject.invokeMethod с
        BlockingQueuedConnection — это гарантированно выполнит
        слот в главном Qt-потоке и дождётся его завершения.
        """
        import threading
        from PySide6.QtCore import (
            QMetaObject, QObject, Qt as _Qt, Slot as _Slot,
        )
        from PySide6.QtWidgets import QApplication

        log.info(
            "_ask_local_transcribe_async: ВЫЗВАН "
            "(reason=%d символов, поток=%s)",
            len(reason or ""),
            threading.current_thread().name,
        )

        app = QApplication.instance()
        if app is None:
            log.error(
                "_ask_local_transcribe_async: нет QApplication — "
                "невозможно показать диалог. Возвращаю False."
            )
            return False

        in_main_thread = (
            threading.current_thread() is app.thread()
        )

        # Если вдруг уже в главном потоке — вызываем диалог
        # напрямую (invokeMethod с BlockingQueuedConnection
        # в этом случае дал бы дедлок).
        if in_main_thread:
            log.info(
                "_ask_local_transcribe_async: уже в главном "
                "потоке — показываю диалог напрямую"
            )
            return self._show_local_transcribe_dialog(reason)

        log.info(
            "_ask_local_transcribe_async: перебрасываю диалог "
            "в главный поток через BlockingQueuedConnection…"
        )

        result_holder = {"answer": False}
        outer_self = self

        class _DialogInvoker(QObject):
            @_Slot()
            def show_dialog(self) -> None:
                log.info(
                    "_DialogInvoker.show_dialog: ВХОД "
                    "(главный Qt-поток)"
                )
                try:
                    result_holder["answer"] = (
                        outer_self._show_local_transcribe_dialog(
                            reason
                        )
                    )
                    log.info(
                        "_DialogInvoker.show_dialog: "
                        "получили ответ=%s",
                        result_holder["answer"],
                    )
                except Exception as exc:
                    log.exception(
                        "_DialogInvoker.show_dialog: ошибка: %s",
                        exc,
                    )
                    result_holder["answer"] = False

        invoker = _DialogInvoker()
        invoker.moveToThread(app.thread())

        log.info(
            "_ask_local_transcribe_async: invokeMethod "
            "(BlockingQueuedConnection)…"
        )
        QMetaObject.invokeMethod(
            invoker,
            "show_dialog",
            _Qt.ConnectionType.BlockingQueuedConnection,
        )
        log.info(
            "_ask_local_transcribe_async: invokeMethod "
            "вернулся, ответ=%s",
            result_holder["answer"],
        )

        log.info(
            "Пользователь %s локальную транскрибацию",
            "выбрал" if result_holder["answer"] else "отклонил",
        )
        return bool(result_holder["answer"])

    def _show_local_transcribe_dialog(
        self, reason: str,
    ) -> bool:
        """
        Синхронно показывает модальный диалог выбора локальной
        транскрибации.

        ДОЛЖЕН вызываться ТОЛЬКО из главного Qt-потока.

        Returns:
            True  — «Выполнить локально».
            False — «Отменить задачу».
        """
        from PySide6.QtWidgets import QMessageBox

        log.info(
            "_show_local_transcribe_dialog: ВХОД "
            "(reason=%d символов)",
            len(reason or ""),
        )

        try:
            box = QMessageBox()
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle(
                "Сервер транскрибации недоступен"
            )
            box.setTextFormat(Qt.TextFormat.PlainText)
            box.setText(reason)

            box.setWindowModality(
                Qt.WindowModality.ApplicationModal
            )
            box.setWindowFlag(
                Qt.WindowType.WindowStaysOnTopHint, True
            )

            local_btn = box.addButton(
                "Выполнить локально",
                QMessageBox.ButtonRole.AcceptRole,
            )
            cancel_btn = box.addButton(
                "Отменить задачу",
                QMessageBox.ButtonRole.RejectRole,
            )
            box.setDefaultButton(cancel_btn)

            log.info(
                "_show_local_transcribe_dialog: QMessageBox "
                "создан, показываю и поднимаю…"
            )

            box.show()
            box.raise_()
            box.activateWindow()
            try:
                handle = box.windowHandle()
                if handle is not None:
                    handle.requestActivate()
            except Exception as exc:
                log.debug(
                    "_show_local_transcribe_dialog: "
                    "requestActivate упал: %s", exc,
                )

            log.info(
                "_show_local_transcribe_dialog: вхожу в exec() "
                "— ждём ответа пользователя…"
            )
            box.exec()
            clicked = box.clickedButton()
            log.info(
                "_show_local_transcribe_dialog: вышли из "
                "exec(), clickedButton=%r, локальная=%s",
                clicked, clicked is local_btn,
            )
            return clicked is local_btn
        except Exception as exc:
            log.exception(
                "_show_local_transcribe_dialog: ошибка: %s",
                exc,
            )
            return False

    def _on_task_completed(self, task_id: str, result: dict) -> None:
        self._set_state(AppState.IDLE)
        output_dir = result.get("output_dir", "")
        if output_dir:
            self._notify("Обработка",
                         f"Файлы сохранены в {output_dir}")
        else:
            self._notify("Обработка", "Задача завершена")

        if output_dir:
            self._auto_publish_after_processing(task_id, output_dir)

    def _on_task_failed(self, task_id: str, error: str) -> None:
        log.error("Задача %s провалена: %s", task_id, error)
        self._set_state(AppState.IDLE)
        self._notify(
            "Ошибка обработки", error[:100],
            QSystemTrayIcon.MessageIcon.Critical,
        )

    # ------------------------------------------------------------------
    # Автопубликация на сервер
    # ------------------------------------------------------------------
    def _auto_publish_after_processing(
        self, task_id: str, output_dir: str
    ) -> None:
        try:
            cfg = self.config_manager.get_sync_settings()
        except Exception as exc:
            log.warning("Не удалось прочитать sync-настройки: %s", exc)
            return

        if not cfg.get("enabled"):
            return
        if not cfg.get("auto_upload_after_processing"):
            return
        if not (cfg.get("base_url") and cfg.get("api_key")):
            log.debug("Автопубликация пропущена: нет base_url/api_key")
            return

        if not output_dir or not os.path.isdir(output_dir):
            log.warning(
                "Автопубликация: папка не найдена: %s", output_dir
            )
            return

        if self.app_cfg.get("sync_auto_publish_ready_only", True):
            meta = self._read_session_meta(output_dir)
            if not meta.get("sync_ready", False):
                log.info(
                    "Автопубликация пропущена: запись %s не помечена "
                    "как «готова к синхронизации» (sync_ready=false).",
                    task_id,
                )
                return

        log.info(
            "Автопубликация записи %s на сервер (%s)",
            task_id, output_dir,
        )

        manager = SyncManager(
            sessions_root=self._sessions_root(),
            sync_settings=cfg,
            config_manager=self.config_manager,
        )

        async def _publish():
            try:
                res = await manager.publish_session(output_dir)
                log.info(
                    "Автопубликация %s: action=%s, id=%s",
                    task_id, res.get("action"), res.get("id"),
                )
                self._notify(
                    "Синхронизация",
                    f"Запись опубликована на сервер "
                    f"({res.get('action')})",
                )
            except Exception as exc:
                log.exception(
                    "Автопубликация %s не удалась: %s", task_id, exc
                )
                self._notify(
                    "Синхронизация",
                    f"Не удалось опубликовать запись: {exc}"[:100],
                    QSystemTrayIcon.MessageIcon.Warning,
                )

        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(_publish(), self._loop)

    # ------------------------------------------------------------------
    # Настройки, очередь, записи, библиотека, синхронизация
    # ------------------------------------------------------------------
    def _open_settings(self) -> None:
        self.settings_window = SettingsWindow(self.config_manager)
        if self.settings_window.exec():
            self.config_manager.load()
            self.app_cfg = self.config_manager.get_app_settings()
            self.processor.config = self.config_manager.config
            self.processor.config_manager = self.config_manager

    def _open_queue(self) -> None:
        self.queue_window = QueueWindow(
            self.task_queue, self.processor
        )
        self.queue_window.show()

    def _open_sessions(self) -> None:
        self.sessions_window = SessionsWindow(
            self._sessions_root(),
            self.task_queue,
            self.processor,
            config_manager=self.config_manager,
        )
        self.sessions_window.import_requested.connect(
            self._open_import
        )
        self.sessions_window.show()

    def _open_library(self) -> None:
        try:
            self.library_window = LibraryWindow(
                sessions_root=self._sessions_root(),
                config_manager=self.config_manager,
                parent=None,
            )
            self.library_window.show()
        except Exception as exc:
            log.exception(
                "Не удалось открыть окно «Библиотека»: %s", exc
            )
            QMessageBox.critical(
                None, "Библиотека",
                f"Не удалось открыть окно поиска:\n{exc}",
            )

    def _open_sync(self) -> None:
        try:
            self.sync_window = SyncWindow(
                sessions_root=self._sessions_root(),
                config_manager=self.config_manager,
                parent=None,
            )
            self.sync_window.show()
        except Exception as exc:
            log.exception(
                "Не удалось открыть окно «Синхронизация»: %s", exc
            )
            QMessageBox.critical(
                None, "Синхронизация",
                f"Не удалось открыть окно синхронизации:\n{exc}",
            )

    def _open_tasks_window(self) -> None:
        try:
            self.tasks_window = TasksWindow(
                sessions_root=self._sessions_root(),
                config_manager=self.config_manager,
                parent=None,
            )
            self.tasks_window.show()
        except Exception as exc:
            log.exception(
                "Не удалось открыть окно «Поручения»: %s", exc
            )
            QMessageBox.critical(
                None, "Поручения",
                f"Не удалось открыть окно поручений:\n{exc}",
            )

    def _open_tfs(self) -> None:
        try:
            self.tfs_window = TfsWindow(
                config_manager=self.config_manager,
                parent=None,
            )
            self.tfs_window.show()
        except Exception as exc:
            log.exception(
                "Не удалось открыть окно «Задачи TFS»: %s", exc
            )
            QMessageBox.critical(
                None, "Задачи TFS",
                f"Не удалось открыть окно:\n{exc}",
            )

    # ------------------------------------------------------------------
    # Логи / уведомления
    # ------------------------------------------------------------------
    def _on_log_to_gui(self, message: str, level: str) -> None:
        # Overlay-панели больше нет — логи только в файл и консоль.
        pass

    def _notify(
        self,
        title: str,
        message: str,
        icon: QSystemTrayIcon.MessageIcon = (
            QSystemTrayIcon.MessageIcon.Information
        ),
    ) -> None:
        self.tray_manager.show_notification(title, message, icon)

    # ------------------------------------------------------------------
    # Завершение
    # ------------------------------------------------------------------
    def _quit(self) -> None:
        """
        Корректное завершение приложения.

        Порядок важен:
          1. Сохраняем очередь.
          2. Закрываем все открытые окна — чтобы Qt-объекты,
             на которые держат ссылки корутины, не удалились
             «на ходу».
          3. Отменяем фоновые задачи (sync-puller, processor).
          4. Даём loop-у обработать отмену.
          5. Останавливаем processor.
          6. Останавливаем loop.
          7. Останавливаем Qt.

        Нарушение этого порядка (например, остановка loop до
        отмены задач) приводило к segfault при выходе.
        """
        log.info("Завершение работы приложения")
        self._set_state(AppState.QUITTING)

        # 1. Сохраняем очередь
        try:
            self.task_queue.save_queue()
        except Exception as exc:
            log.warning("Не удалось сохранить очередь: %s", exc)

        if self._loop is None:
            log.info("Приложение остановлено (loop=None)")
            self.app.quit()
            return

        # 2. Закрываем все окна, которые могут держать ссылки
        #    на объекты, используемые в loop-потоке.
        for win_attr in (
            "sessions_window",
            "library_window",
            "sync_window",
            "tasks_window",
            "tfs_window",
            "queue_window",
            "settings_window",
        ):
            win = getattr(self, win_attr, None)
            if win is None:
                continue
            try:
                win.close()
                log.debug("Окно %s закрыто", win_attr)
            except Exception as exc:
                log.warning(
                    "Ошибка закрытия %s: %s", win_attr, exc
                )
            setattr(self, win_attr, None)

        # 3. Отменяем sync-puller и ждём
        if self._sync_pull_task is not None:
            log.info("Отмена фонового sync-puller…")
            try:
                self._loop.call_soon_threadsafe(
                    self._sync_pull_task.cancel
                )
                import time as _t
                _t.sleep(0.3)
            except Exception as exc:
                log.warning(
                    "Ошибка отмены sync-puller: %s", exc
                )

        # 4. Отменяем processing task
        if self._processing_task is not None:
            log.info("Отмена воркера обработки…")
            try:
                self._loop.call_soon_threadsafe(
                    self._processing_task.cancel
                )
                import time as _t
                _t.sleep(0.3)
            except Exception as exc:
                log.warning(
                    "Ошибка отмены обработчика: %s", exc
                )

        # 5. Останавливаем processor
        try:
            self.processor.stop()
        except Exception as exc:
            log.warning(
                "Ошибка остановки processor: %s", exc
            )

        # 6. Останавливаем loop
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception as exc:
            log.warning(
                "Ошибка остановки loop: %s", exc
            )

        log.info("Приложение остановлено")
        self.app.quit()

    def startup(self) -> None:
        self.tray_manager.create_tray_icon()
        self._notify(
            "Screen Recorder",
            "Приложение запущено",
        )


def main() -> int:
    # ------------------------------------------------------------------
    # КРИТИЧНО: включаем faulthandler.
    #
    # При segfault Python не печатает traceback — падает «нативно».
    # faulthandler перехватывает SIGSEGV/SIGABRT и пишет C-стек в
    # файл. Это единственный способ понять, где именно упал Qt.
    # ------------------------------------------------------------------
    import faulthandler

    _faulthandler_dir = "/tmp/screen-recorder"
    try:
        os.makedirs(_faulthandler_dir, exist_ok=True)
        _faulthandler_path = os.path.join(
            _faulthandler_dir, "faulthandler.log"
        )
        _faulthandler_file = open(
            _faulthandler_path, "a", encoding="utf-8"
        )
        faulthandler.enable(file=_faulthandler_file, all_threads=True)
        print(
            f"[faulthandler] включён, пишет в {_faulthandler_path}",
            file=sys.stderr,
        )
    except Exception as exc:
        print(
            f"[faulthandler] не удалось включить: {exc}",
            file=sys.stderr,
        )
        # Пробуем без файла — пойдёт в stderr.
        try:
            faulthandler.enable(all_threads=True)
        except Exception:
            pass

    log_level = os.environ.get(
        "SCREEN_RECORDER_LOG_LEVEL", "DEBUG"
    )
    max_bytes = 10 * 1024 * 1024
    backup_count = 5
    try:
        _cfg = ConfigManager()
        _log_cfg = _cfg.get_log_settings()
        set_log_path(_log_cfg["log_path"])
        log_level = _log_cfg.get("level", log_level)
        max_bytes = int(_log_cfg["max_bytes_mb"]) * 1024 * 1024
        backup_count = int(_log_cfg["backup_count"])
    except Exception as exc:
        print(f"Предупреждение: не удалось прочитать конфиг ({exc})",
              file=sys.stderr)

    setup_logger(
        level=log_level,
        max_bytes=max_bytes,
        backup_count=backup_count,
    )
    log.info("=" * 60)
    log.info("Запуск Screen Recorder & Transcriber (без записи)")
    log.info("Python: %s", sys.version)
    log.info("=" * 60)

    if IS_LINUX and not os.environ.get("QT_QPA_PLATFORM"):
        os.environ["QT_QPA_PLATFORM"] = "xcb"
        log.info(
            "QT_QPA_PLATFORM принудительно установлен в 'xcb' "
            "(обход конфликта Qt/GTK3 на GNOME 40+)"
        )

    if not check_ffmpeg_installed():
        if IS_LINUX:
            log.critical("ffmpeg не установлен")
            print(
                "ОШИБКА: ffmpeg не установлен.\n"
                "Установите: sudo apt install ffmpeg"
            )
            return 1
        else:
            log.warning(
                "ffmpeg не найден в PATH — конвертация видео и "
                "автосжатие медиа будут недоступны"
            )
            print(
                "ПРЕДУПРЕЖДЕНИЕ: ffmpeg не найден в PATH.\n"
                "Конвертация видео и автосжатие медиа будут "
                "недоступны.\n"
                "Скачайте: https://ffmpeg.org/download.html"
            )
    else:
        log.info("ffmpeg найден: OK")

    app = QApplication(sys.argv)
    app.setApplicationName("Screen Recorder")
    app.setApplicationDisplayName("Screen Recorder")
    app.setOrganizationName("ScreenRecorder")
    app.setOrganizationDomain("screen-recorder.local")
    app.setDesktopFileName("screen-recorder")
    app.setQuitOnLastWindowClosed(False)

    _icon_candidates = [
        _resource("icons/app.png"),
        _resource("icons/app.svg"),
    ]
    _icon_set = False
    for _icon_path in _icon_candidates:
        if os.path.exists(_icon_path):
            app.setWindowIcon(QIcon(_icon_path))
            log.info("Иконка приложения установлена: %s", _icon_path)
            _icon_set = True
            break

    if not _icon_set:
        log.warning(
            "Иконка приложения не найдена ни в %s. "
            "В трее и доке будет системная заглушка.",
            " или ".join(_icon_candidates),
        )

    qss_path = _resource("styles.qss")
    if os.path.exists(qss_path):
        try:
            with open(qss_path, "r", encoding="utf-8") as f:
                app.setStyleSheet(f.read())
        except Exception as exc:
            log.warning("Не удалось загрузить стили: %s", exc)

    controller = ScreenRecorderApp(app)
    controller.startup()

    if IS_LINUX:
        signal.signal(
            signal.SIGINT, lambda *_: controller._quit()
        )
        timer = QTimer()
        timer.start(500)
        timer.timeout.connect(lambda: None)
    else:
        log.info(
            "SIGINT-хак отключён (не Linux): приложение "
            "останавливается через трей → «Выход»"
        )

    log.info("Вход в главный цикл приложения")
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())