"""Точка входа приложения Screen Recorder & Transcriber.

Изменения:
  • _build_default_meta() и _ask_metadata() учитывают
    config.default_project — имя проекта по умолчанию для
    новых записей.
  • Добавлена поддержка тегов: передача справочника в
    MetadataDialog, сохранение новых тегов в config.
  • Добавлена интеграция с удалённым сервером синхронизации:
      – окно «Синхронизация» (SyncWindow);
      – автопубликация записи после успешной обработки;
      – фоновый воркер дельта-синхронизации.
  • Пути из QFileDialog нормализуются через safe_local_path().
  • Автопубликация проверяет флаг sync_ready (если включена
    настройка sync_auto_publish_ready_only). Это защищает от
    публикации черновиков и от гонки publish/pull.
  • Управление ВМ Yandex: при недоступности сервиса
    транскрибации приложение может временно включить ВМ,
    дождаться запуска и продолжить обработку. После
    обработки пользователю предлагается отключить ВМ
    (восстановить исходное расписание).
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
    from src.hotkeys import GlobalHotkeyManager
    from src.ui.import_window import ImportWindow
    from src.ui.library_window import LibraryWindow
    from src.ui.metadata_dialog import MetadataDialog
    from src.ui.overlay_panel import OverlayPanel
    from src.ui.queue_window import QueueWindow
    from src.ui.sessions_window import SessionsWindow
    from src.ui.settings_window import SettingsWindow
    from src.ui.sync_window import SyncWindow
    from src.ui.tray_manager import TrayManager
    from src.ui.yandex_vm_window import YandexVMDialog
    from src.ui.tasks_window import TasksWindow
    from src.logger import (
        get_logger,
        register_gui_handler,
        set_log_path,
        setup_logger,
    )
    from src.processor import VideoProcessor
    from src.recorder import ScreenRecorder
    from src.sync_manager import SyncManager, is_record_published
    from src.task_queue import TaskQueue
    from src.yandex_vm_manager import YandexVMManager
    from src.platform_utils import (
        IS_LINUX,
        IS_WINDOWS,
        is_screen_recording_available,
        is_hotkey_recording_available,
        screen_recording_unavailable_reason,
    )
    from src.utils import (
        check_ffmpeg_installed,
        get_system_monitors,
        safe_local_path,
        sanitize_filename,
        is_recording_supported,
        recording_unavailable_message,
    )
else:
    from .config_manager import ConfigManager
    from .hotkeys import GlobalHotkeyManager
    from .ui.import_window import ImportWindow
    from .ui.library_window import LibraryWindow
    from .ui.metadata_dialog import MetadataDialog
    from .ui.overlay_panel import OverlayPanel
    from .ui.queue_window import QueueWindow
    from .ui.sessions_window import SessionsWindow
    from .ui.settings_window import SettingsWindow
    from .ui.sync_window import SyncWindow
    from .ui.tray_manager import TrayManager
    from .ui.yandex_vm_window import YandexVMDialog
    from .ui.tasks_window import TasksWindow
    from .logger import (
        get_logger,
        register_gui_handler,
        set_log_path,
        setup_logger,
    )
    from .processor import VideoProcessor
    from .recorder import ScreenRecorder
    from .sync_manager import SyncManager, is_record_published
    from .task_queue import TaskQueue
    from .yandex_vm_manager import YandexVMManager
    from src.platform_utils import (
        IS_LINUX,
        IS_WINDOWS,
        is_screen_recording_available,
        is_hotkey_recording_available,
        screen_recording_unavailable_reason,
    )
    from src.utils import (
        check_ffmpeg_installed,
        get_system_monitors,
        safe_local_path,
        sanitize_filename,
        is_recording_supported,
        recording_unavailable_message,
    )

from PySide6.QtCore import QObject, QTimer, QUrl, Slot
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QMessageBox, QSystemTrayIcon,
)

log = get_logger(__name__)


class AppState(Enum):
    """Явные состояния приложения."""
    IDLE = "idle"
    RECORDING = "recording"
    PAUSED = "paused"
    PROCESSING = "processing"
    QUITTING = "quitting"


def _resource(name: str) -> str:
    base = os.path.join(os.path.dirname(__file__), "..", "resources")
    return os.path.join(base, name)


class ScreenRecorderApp(QObject):
    """Главный контроллер приложения."""

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

        self.recorder = ScreenRecorder(self.config_manager.config)
        self.processor = VideoProcessor(
            self.config_manager.config,
            self.task_queue,
            is_recording_cb=self._is_recording,
            config_manager=self.config_manager,
        )
        self.tray_manager = TrayManager(self.config_manager.config, self)
        self.overlay_panel = OverlayPanel(self.config_manager.config)
        self.settings_window: Optional[SettingsWindow] = None
        self.queue_window: Optional[QueueWindow] = None
        self.sessions_window: Optional[SessionsWindow] = None
        self.library_window: Optional[LibraryWindow] = None
        self.sync_window: Optional[SyncWindow] = None
        self.tasks_window: Optional[TasksWindow] = None

        self.hotkey_manager = GlobalHotkeyManager(
            self.recorder,
            self.config_manager,
            start_cb=self._hotkey_start_recording,
            stop_cb=self._hotkey_stop_recording,
        )

        self._state: AppState = AppState.IDLE

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._processing_task: Optional[asyncio.Task] = None
        self._sync_pull_task: Optional[asyncio.Task] = None

        self._current_session_meta: Dict[str, Any] = {}

        self._connect_signals()
        self._start_async_loop()
        self._maybe_start_sync_puller()
        log.info("ScreenRecorderApp инициализирован (локальный режим)")

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

    def _is_recording(self) -> bool:
        """Возвращает True, если идёт запись (в т.ч. на паузе)."""
        try:
            return bool(
                self.recorder.get_recording_status().get(
                    "recording", False
                )
            )
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Инициализация
    # ------------------------------------------------------------------
    def _connect_signals(self) -> None:
        log.debug("Подключение сигналов")
        self.tray_manager.toggle_recording_requested.connect(
            self._toggle_recording
        )

        self.tray_manager.open_yandex_vm_requested.connect(
            self._open_yandex_vm
        )

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
        self.tray_manager.import_requested.connect(self._open_import)
        self.tray_manager.upload_video_requested.connect(
            self._open_upload_video
        )
        self.tray_manager.quit_requested.connect(self._quit)

        self.recorder.recording_started.connect(
            self._on_recording_started
        )
        self.recorder.recording_paused.connect(
            self._on_recording_paused
        )
        self.recorder.recording_resumed.connect(
            self._on_recording_resumed
        )
        self.recorder.recording_stopped.connect(
            self._on_recording_stopped
        )
        self.recorder.recording_error.connect(
            lambda msg: self._notify(
                "Ошибка записи", msg,
                QSystemTrayIcon.MessageIcon.Critical,
            )
        )

        self.processor.task_started.connect(self._on_task_started)
        self.processor.task_progress.connect(self._on_task_progress)
        self.processor.task_completed.connect(self._on_task_completed)
        self.processor.task_failed.connect(self._on_task_failed)

        self.overlay_panel.start_requested.connect(
            self._start_recording
        )
        self.overlay_panel.pause_requested.connect(self._toggle_pause)
        self.overlay_panel.stop_requested.connect(self._stop_recording)

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

        self.hotkey_manager.set_loop(self._loop)
        log.info("asyncio-loop запущен в фоновом потоке")

    # ------------------------------------------------------------------
    # Фоновый воркер синхронизации
    # ------------------------------------------------------------------
    def _maybe_start_sync_puller(self) -> None:
        """Запускает фоновый дельта-синк, если он включён в настройках."""
        try:
            cfg = self.config_manager.get_sync_settings()
        except Exception as exc:
            log.warning("Не удалось прочитать sync-настройки: %s", exc)
            return

        if not (cfg.get("enabled") and cfg.get("auto_pull_enabled")):
            log.debug("Фоновый sync-puller не запускается (выключен)")
            return
        if not (cfg.get("base_url") and cfg.get("api_key")):
            log.debug("Фоновый sync-puller не запускается (нет base_url/api_key)")
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
    def _rec_cfg(self) -> Dict[str, Any]:
        return self.config_manager.config.get("recording", {})

    def _show_overlay(self) -> bool:
        return bool(self._rec_cfg().get("show_overlay_panel", True))

    def _show_start_notification(self) -> bool:
        return bool(
            self._rec_cfg().get("show_start_notification", True)
        )

    def _sessions_root(self) -> str:
        return self.config_manager.get_sessions_root()

    # ------------------------------------------------------------------
    # Метаданные
    # ------------------------------------------------------------------
    def _resolve_default_project(self) -> str:
        """
        Возвращает проект для новых записей.

        Приоритеты:
          1) config.default_project, если он есть в списке проектов;
          2) первый проект из get_project_names();
          3) "Default".
        """
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

        log.debug(
            "Открытие диалога метаданных: title=%r, projects=%d, "
            "prompts=%d, name_templates=%d, tags=%d, "
            "initial_keys=%s, default_project=%r",
            title, len(projects), len(prompts), len(name_templates),
            len(tags), list(init.keys()), init.get("project"),
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
            log.info("Диалог метаданных завершён: Продолжить")
            return result
        log.info("Диалог метаданных завершён: Пропустить")
        return None

    def _build_default_meta(self) -> Dict[str, Any]:
        """
        Формирует метаданные по умолчанию.

        По умолчанию sync_ready=False — запись считается
        черновиком, пока пользователь явно не поставит галочку.

        По умолчанию skip_transcription=True — транскрибация
        выполняется как обычно.
        """
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
            # --- Дата и время записи ---
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
            # --- Флаг готовности к синхронизации ---
            "sync_ready": False,
            # --- НОВОЕ: выполнять ли транскрибацию ---
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
        }
        log.debug(
            "Сформированы метаданные по умолчанию: project=%r, "
            "date=%s, time=%s, sync_ready=False, "
            "do_transcribe=True, "
            "ctx: name=%s project=%s comment=%s tags=%s",
            default_project,
            meta["date"], meta["time"],
            meta["include_name_in_prompt"],
            meta["include_project_in_prompt"],
            meta["include_comment_in_prompt"],
            meta["include_tags_in_prompt"],
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
        """Сохраняет новый тег в справочник config["tags"]."""
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
        """Читает session.json. Возвращает {} при ошибке."""
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
    # Запись
    # ------------------------------------------------------------------
    @Slot()
    def _start_recording(self) -> None:
        log.info("Запрос на старт записи (UI)")

        # --- Проверка доступности записи ---
        if not is_screen_recording_available():
            reason = screen_recording_unavailable_reason()
            log.warning(
                "Запись недоступна на платформе %s",
                "Windows" if IS_WINDOWS else "не Linux",
            )
            QMessageBox.warning(
                None,
                "Запись экрана недоступна",
                reason,
            )
            return

        rec = self._rec_cfg()

        if rec.get("show_metadata_on_start", True):
            meta = self._ask_metadata(title="Параметры записи")
            if meta is None:
                log.info("Пользователь отменил ввод метаданных")
                return
        else:
            meta = self._build_default_meta()

        self._current_session_meta = meta

        monitors = get_system_monitors()
        idx = int(rec.get("monitor", 0))
        display = (monitors[idx]["display"]
                   if 0 <= idx < len(monitors) else ":0.0+0,0")

        self.recorder.set_overlay_text(
            f"{meta.get('name', 'ScreenRecorder')} "
            f"{datetime.now():%Y-%m-%d %H:%M}"
        )

        async def _run():
            await self.recorder.start_recording(
                display, rec.get("with_microphone", True)
            )

        asyncio.run_coroutine_threadsafe(_run(), self._loop)

    def _toggle_recording(self) -> None:
        if not is_screen_recording_available():
            reason = screen_recording_unavailable_reason()
            QMessageBox.warning(
                None,
                "Запись экрана недоступна",
                reason,
            )
            return

        status = self.recorder.get_recording_status()
        if status["recording"]:
            self._stop_recording()
        else:
            self._start_recording()

    def _toggle_pause(self) -> None:
        status = self.recorder.get_recording_status()
        if not status["recording"]:
            return
        if status["paused"]:
            asyncio.run_coroutine_threadsafe(
                self.recorder.resume_recording(), self._loop
            )
        else:
            asyncio.run_coroutine_threadsafe(
                self.recorder.pause_recording(), self._loop
            )

    @Slot()
    def _stop_recording(self) -> None:
        log.info("Запрос на остановку записи (UI)")
        asyncio.run_coroutine_threadsafe(
            self.recorder.stop_recording(), self._loop
        )

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
        meta["monitor"] = (
            self.config_manager.config["recording"].get("monitor", 0)
        )
        meta["session_dir"] = session_dir
        meta["video_path"] = target_video
        meta["source"] = "upload"
        meta["source_file"] = src_abs
        meta.pop("task_id", None)

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
            # --- Флаг готовности к синхронизации ---
            "sync_ready": False,
            # --- НОВОЕ: выполнять ли транскрибацию ---
            "skip_transcription": True,
            "source": "import",
            "source_files": {
                "video": video_src,
                "transcript": transcript_src,
                "protocol": protocol_src,
            },
            "video_path": video_dst,
            "session_dir": session_dir,
            "monitor": (
                self.config_manager.config["recording"]
                .get("monitor", 0)
            ),
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
    # Хоткеи
    # ------------------------------------------------------------------
    def _hotkey_start_recording(self) -> None:
        if not is_hotkey_recording_available():
            log.debug(
                "Хоткей старта записи недоступен на этой платформе"
            )
            return
        status = self.recorder.get_recording_status()
        if status["recording"]:
            if status["paused"]:
                QTimer.singleShot(
                    0, lambda: asyncio.run_coroutine_threadsafe(
                        self.recorder.resume_recording(), self._loop
                    )
                )
            else:
                QTimer.singleShot(
                    0, lambda: asyncio.run_coroutine_threadsafe(
                        self.recorder.pause_recording(), self._loop
                    )
                )
        else:
            QTimer.singleShot(0, self._start_recording)

    def _hotkey_stop_recording(self) -> None:
        if not is_hotkey_recording_available():
            log.debug(
                "Хоткей остановки записи недоступен на этой платформе"
            )
            return
        QTimer.singleShot(0, self._stop_recording)

    # ------------------------------------------------------------------
    # Обработчики записи
    # ------------------------------------------------------------------
    def _on_recording_started(self) -> None:
        log.info("Событие: запись началась")
        self._set_state(AppState.RECORDING)
        self.tray_manager.set_recording_state("recording")
        if self._show_overlay():
            self.overlay_panel.reset()
            self.overlay_panel.show_panel()
            self.overlay_panel.update_status("Запись...")
        if self._show_start_notification():
            self._notify("Запись", "Запись началась")

    def _on_recording_paused(self) -> None:
        log.info("Событие: запись на паузе")
        self._set_state(AppState.PAUSED)
        self.tray_manager.set_recording_state("paused")
        if self._show_overlay():
            self.overlay_panel.update_status("Пауза")
        self._notify("Запись", "Запись приостановлена")

    def _on_recording_resumed(self) -> None:
        log.info("Событие: запись возобновлена")
        self._set_state(AppState.RECORDING)
        self.tray_manager.set_recording_state("recording")
        if self._show_overlay():
            self.overlay_panel.update_status("Запись...")

    def _on_recording_stopped(self, path: str) -> None:
        log.info("Событие: запись остановлена, файл=%s", path)
        self._set_state(AppState.IDLE)
        if self._show_overlay():
            self.overlay_panel.reset()
            self.overlay_panel.update_status("Обработка...")

        if not path or not os.path.exists(path):
            log.error("Файл записи не найден: %s", path)
            self._notify(
                "Ошибка", "Файл записи не найден",
                QSystemTrayIcon.MessageIcon.Critical,
            )
            self.tray_manager.set_recording_state("idle")
            if self._show_overlay():
                self.overlay_panel.hide_panel()
            return

        rec = self._rec_cfg()
        if rec.get("show_metadata_on_stop", True):
            initial = dict(self._current_session_meta)
            meta = self._ask_metadata(
                title="Уточните данные записи",
                initial=initial,
            )
            if meta is None:
                meta = initial or self._build_default_meta()
        else:
            meta = (dict(self._current_session_meta)
                    or self._build_default_meta())

        # --- Дата и время: не перезаписываем то, что уже задано ---
        now = datetime.now()

        # Если пользователь отказался от ввода метаданных
        # (meta получен из _build_default_meta или из initial
        # без явных date/time) — ставим текущие дату и время.
        if not (meta.get("date") or "").strip():
            meta["date"] = now.strftime("%Y-%m-%d")
        if not (meta.get("time") or "").strip():
            meta["time"] = now.strftime("%H:%M:%S")

        meta["monitor"] = (
            self.config_manager.config["recording"].get("monitor", 0)
        )
        session_dir = os.path.dirname(path)
        meta["session_dir"] = session_dir
        meta["video_path"] = path
        meta["source"] = "record"
        meta.pop("task_id", None)

        self._copy_attachments_to_session(meta, session_dir)
        self._save_session_metadata(meta, session_dir)

        task_id = self.task_queue.add_task(
            {"video_path": path, "metadata": meta}
        )
        log.info("Задача %s добавлена в очередь", task_id)

        self._current_session_meta = {}

    # ------------------------------------------------------------------
    # Обработка задач
    # ------------------------------------------------------------------
    def _on_task_started(self, task_id: str) -> None:
        if self._is_recording():
            return
        self._set_state(AppState.PROCESSING)
        self.tray_manager.set_recording_state("processing")

    def _on_task_progress(self, task_id: str, progress: int,
                          stage: str) -> None:
        if self._show_overlay():
            self.overlay_panel.update_progress(progress, stage)

    def _on_task_completed(self, task_id: str, result: dict) -> None:
        if not self._is_recording():
            self._set_state(AppState.IDLE)
            self.tray_manager.set_recording_state("idle")
        if self._show_overlay():
            self.overlay_panel.update_progress(100, "Готово")
        output_dir = result.get("output_dir", "")
        if output_dir:
            self._notify("Обработка",
                         f"Файлы сохранены в {output_dir}")
        else:
            self._notify("Обработка", "Задача завершена")
        if self._show_overlay() and not self._is_recording():
            hide_after = int(
                self.app_cfg.get("overlay_hide_after_task_ms", 3000)
            )
            QTimer.singleShot(
                hide_after, self.overlay_panel.hide_panel
            )

        if output_dir:
            self._auto_publish_after_processing(task_id, output_dir)

        # --- Завершение VM-сессии (отключение ВМ) ---
        vm_info = result.get("vm_session")
        if vm_info and vm_info.get("schedule_modified"):
            self._finish_vm_session(vm_info)

    def _finish_vm_session(self, vm_info: dict) -> None:
        """
        Спрашивает пользователя, отключить ли ВМ после обработки.

        Восстанавливает расписание из резервной копии, если
        пользователь согласен. Иначе — предупреждает, до какого
        времени ВМ продолжит работу.
        """
        vm_name = vm_info.get("vm_name", "")
        schedule_path = vm_info.get("schedule_path", "")

        if not schedule_path:
            log.warning(
                "_finish_vm_session: не передан schedule_path — "
                "нечего восстанавливать"
            )
            return

        try:
            vm_settings = self.config_manager.get_yandex_vm_settings()
        except Exception as exc:
            log.warning(
                "Не удалось прочитать настройки ВМ: %s", exc
            )
            return

        vm_manager = YandexVMManager(vm_settings)
        backup_path = schedule_path + ".transcribe_bak"

        info = vm_manager.get_modified_schedule_info(
            schedule_path=schedule_path
        )
        next_stop = info.get("next_stop_time", "")

        if next_stop:
            msg = (
                f"<b>Транскрибация завершена.</b><br><br>"
                f"ВМ «{vm_name}» была временно включена для "
                f"транскрибации.<br><br>"
                f"<b>Отключить ВМ сейчас?</b><br>"
                f"Если да — расписание будет восстановлено, "
                f"и ВМ выключится при следующем запуске "
                f"vm_manager.py.<br><br>"
                f"Если нет — ВМ будет работать до "
                f"<b>{next_stop}</b>."
            )
        else:
            msg = (
                f"<b>Транскрибация завершена.</b><br><br>"
                f"ВМ «{vm_name}» была временно включена. "
                f"Отключить её сейчас?"
            )

        reply = QMessageBox.question(
            None, "Управление ВМ Yandex",
            msg,
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )

        if reply == QMessageBox.StandardButton.Yes:
            restore = vm_manager.restore_from_backup(
                schedule_path=schedule_path,
                backup_path=backup_path,
            )
            if restore["ok"]:
                self._notify(
                    "ВМ Yandex",
                    f"Расписание ВМ «{vm_name}» восстановлено",
                )
            else:
                QMessageBox.warning(
                    None, "ВМ Yandex",
                    f"Не удалось восстановить расписание:\n"
                    f"{restore['message']}",
                )
        else:
            if next_stop:
                QMessageBox.information(
                    None, "ВМ Yandex",
                    f"ВМ «{vm_name}» продолжит работу до "
                    f"{next_stop}.\n\n"
                    f"Отключить её позже можно через окно "
                    f"«ВМ Yandex» (трей) — вручную поправить "
                    f"schedule.cron.",
                )
            else:
                QMessageBox.information(
                    None, "ВМ Yandex",
                    f"ВМ «{vm_name}» продолжит работу по "
                    f"изменённому расписанию.",
                )

    def _on_task_failed(self, task_id: str, error: str) -> None:
        log.error("Задача %s провалена: %s", task_id, error)
        if not self._is_recording():
            self._set_state(AppState.IDLE)
            self.tray_manager.set_recording_state("idle")
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
        """
        Публикует запись на сервер, если это включено в настройках.

        Если включена настройка sync_auto_publish_ready_only —
        публикация выполняется только для записей с флагом
        sync_ready=true (защита от гонки publish/pull).
        """
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

        # --- Проверка флага готовности ---
        if self.app_cfg.get("sync_auto_publish_ready_only", True):
            meta = self._read_session_meta(output_dir)
            if not meta.get("sync_ready", False):
                log.info(
                    "Автопубликация пропущена: запись %s не помечена "
                    "как «готова к синхронизации» (sync_ready=false). "
                    "Поставьте галочку в карточке метаданных или в "
                    "окне «Записи».",
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
            self.recorder.config = self.config_manager.config
            self.processor.config = self.config_manager.config
            self.processor.config_manager = self.config_manager
            self.hotkey_manager.update_hotkeys(
                self.config_manager.config
            )

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
        """Открывает сводное окно «Поручения»."""
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

    def _open_yandex_vm(self) -> None:
        """Открывает окно «ВМ Yandex»."""
        yandex_cfg = self.config_manager.get_yandex_vm_settings()
        root_path = yandex_cfg.get("root_path", "")

        dlg = YandexVMDialog(
            root_path=root_path,
            config_manager=self.config_manager,
            parent=None,
        )
        dlg.exec()

    # ------------------------------------------------------------------
    # Логи / уведомления
    # ------------------------------------------------------------------
    def _on_log_to_gui(self, message: str, level: str) -> None:
        if self._show_overlay():
            self.overlay_panel.add_log(f"[{level}] {message}")

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
        log.info("Завершение работы приложения")
        self._set_state(AppState.QUITTING)

        self.task_queue.save_queue()
        self.hotkey_manager.unregister_hotkeys()

        if self._loop is not None:
            if self._sync_pull_task is not None:
                try:
                    self._loop.call_soon_threadsafe(
                        self._sync_pull_task.cancel
                    )
                except Exception:
                    pass

            if self._processing_task is not None:
                try:
                    self._loop.call_soon_threadsafe(
                        self._processing_task.cancel
                    )
                except Exception:
                    pass

            try:
                self.processor.stop()
            except Exception:
                pass

            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass

        log.info("Приложение остановлено")
        self.app.quit()

    def startup(self) -> None:
        self.tray_manager.create_tray_icon()
        self.hotkey_manager.register_hotkeys()

        if is_screen_recording_available():
            self._notify(
                "Screen Recorder",
                "Приложение запущено (локальный режим)",
            )
        else:
            self._notify(
                "Screen Recorder",
                "Запись экрана недоступна на этой платформе. "
                "Доступны транскрибация, синхронизация и другие "
                "функции.",
            )

def main() -> int:
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
    log.info("Запуск Screen Recorder & Transcriber (локальный режим)")
    log.info("Python: %s", sys.version)
    log.info("=" * 60)

    # --- ФИКС: принудительно используем xcb (X11) вместо Wayland ---
    # Причина: Qt пытается загрузить плагин libqgtk3.so, который
    # обращается к устаревшему ключу 'antialiasing' в
    # gnome-settings-daemon. На GNOME 40+ этого ключа нет, и GLib
    # аварийно завершает процесс.
    # См. https://bugs.launchpad.net/ubuntu/+source/gtk+3.0/+bug/1922464
    if IS_LINUX and not os.environ.get("QT_QPA_PLATFORM"):
        os.environ["QT_QPA_PLATFORM"] = "xcb"
        log.info(
            "QT_QPA_PLATFORM принудительно установлен в 'xcb' "
            "(обход конфликта Qt/GTK3 на GNOME 40+)"
        )
    # --- КОНЕЦ ФИКСА ---

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
        # На Linux этот хак нужен, чтобы Ctrl+C в консоли
        # корректно останавливал Qt-приложение.
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