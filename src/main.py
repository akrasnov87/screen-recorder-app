"""Точка входа приложения Screen Recorder & Transcriber (локальный режим)."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from src.config_manager import ConfigManager
    from src.hotkeys import GlobalHotkeyManager
    from src.logger import (
        get_logger,
        register_gui_handler,
        set_log_path,
        setup_logger,
    )
    from src.metadata_dialog import MetadataDialog
    from src.overlay_panel import OverlayPanel
    from src.processor import VideoProcessor
    from src.queue_window import QueueWindow
    from src.recorder import ScreenRecorder
    from src.sessions_window import SessionsWindow
    from src.settings_window import SettingsWindow
    from src.task_queue import TaskQueue
    from src.tray_manager import TrayManager
    from src.utils import check_ffmpeg_installed, get_system_monitors
else:
    from .config_manager import ConfigManager
    from .hotkeys import GlobalHotkeyManager
    from .logger import (
        get_logger,
        register_gui_handler,
        set_log_path,
        setup_logger,
    )
    from .metadata_dialog import MetadataDialog
    from .overlay_panel import OverlayPanel
    from .processor import VideoProcessor
    from .queue_window import QueueWindow
    from .recorder import ScreenRecorder
    from .sessions_window import SessionsWindow
    from .settings_window import SettingsWindow
    from .task_queue import TaskQueue
    from .tray_manager import TrayManager
    from .utils import check_ffmpeg_installed, get_system_monitors

from PySide6.QtCore import QObject, QTimer, Slot
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QMessageBox, QSystemTrayIcon,
)

log = get_logger(__name__)


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
        temp_path = self.config_manager.config["storage"]["temp_path"]
        log.info("Временная папка: %s", temp_path)

        self.task_queue = TaskQueue(os.path.join(temp_path, "queue.json"))
        log.debug("Очередь: %s", self.task_queue.get_queue_status())

        self.recorder = ScreenRecorder(self.config_manager.config)
        self.processor = VideoProcessor(
            self.config_manager.config,
            self.task_queue,
            is_recording_cb=self._is_recording,
        )
        self.tray_manager = TrayManager(self)
        self.overlay_panel = OverlayPanel()
        self.settings_window: Optional[SettingsWindow] = None
        self.queue_window: Optional[QueueWindow] = None
        self.sessions_window: Optional[SessionsWindow] = None

        self.hotkey_manager = GlobalHotkeyManager(
            self.recorder,
            self.config_manager,
            start_cb=self._hotkey_start_recording,
            stop_cb=self._hotkey_stop_recording,
        )

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._processing_task: Optional[asyncio.Task] = None
        self._pump: Optional[QTimer] = None

        self._current_session_meta: Dict[str, Any] = {}

        self._connect_signals()
        self._start_async_loop()
        log.info("ScreenRecorderApp инициализирован (локальный режим)")

    # ------------------------------------------------------------------
    # Инициализация
    # ------------------------------------------------------------------
    def _connect_signals(self) -> None:
        log.debug("Подключение сигналов")
        self.tray_manager.toggle_recording_requested.connect(self._toggle_recording)
        self.tray_manager.open_settings_requested.connect(self._open_settings)
        self.tray_manager.open_queue_requested.connect(self._open_queue)
        self.tray_manager.open_sessions_requested.connect(self._open_sessions)
        self.tray_manager.upload_video_requested.connect(self._open_upload_video)
        self.tray_manager.quit_requested.connect(self._quit)

        self.recorder.recording_started.connect(self._on_recording_started)
        self.recorder.recording_paused.connect(self._on_recording_paused)
        self.recorder.recording_resumed.connect(self._on_recording_resumed)
        self.recorder.recording_stopped.connect(self._on_recording_stopped)
        self.recorder.recording_error.connect(
            lambda msg: self._notify(
                "Ошибка записи", msg, QSystemTrayIcon.MessageIcon.Critical
            )
        )

        self.processor.task_started.connect(self._on_task_started)
        self.processor.task_progress.connect(self._on_task_progress)
        self.processor.task_completed.connect(self._on_task_completed)
        self.processor.task_failed.connect(self._on_task_failed)

        self.overlay_panel.start_requested.connect(self._start_recording)
        self.overlay_panel.pause_requested.connect(self._toggle_pause)
        self.overlay_panel.stop_requested.connect(self._stop_recording)

        register_gui_handler(self._on_log_to_gui)

    def _start_async_loop(self) -> None:
        log.debug("Запуск asyncio-loop")
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._processing_task = self._loop.create_task(self.processor.run_forever())
        self.hotkey_manager.set_loop(self._loop)

        self._pump = QTimer(self)
        self._pump.setInterval(20)
        self._pump.timeout.connect(self._pump_loop)
        self._pump.start()
        log.info("asyncio-loop запущен, воркер обработки стартовал")

    def _pump_loop(self) -> None:
        if self._loop is None:
            return
        try:
            self._loop.stop()
            self._loop.run_forever()
        except RuntimeError:
            pass

    # ------------------------------------------------------------------
    # Геттеры
    # ------------------------------------------------------------------
    def _rec_cfg(self) -> Dict[str, Any]:
        return self.config_manager.config.get("recording", {})

    def _show_overlay(self) -> bool:
        return bool(self._rec_cfg().get("show_overlay_panel", True))

    def _show_start_notification(self) -> bool:
        return bool(self._rec_cfg().get("show_start_notification", True))

    def _is_recording(self) -> bool:
        try:
            return bool(self.recorder.get_recording_status().get("recording", False))
        except Exception:
            return False

    def _sessions_root(self) -> str:
        return os.path.join(
            self.config_manager.config["storage"].get(
                "temp_path", "/tmp/screen-recorder"
            ),
            "sessions",
        )

    # ------------------------------------------------------------------
    # Метаданные
    # ------------------------------------------------------------------
    def _ask_metadata(
        self,
        title: str = "Метаданные записи",
        initial: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        cfg = self.config_manager.config
        projects = list(cfg.get("projects", []) or [])
        prompts = self.config_manager.get_prompts()
        default_prompt = self.config_manager.get_default_prompt()
        name_templates = self.config_manager.get_name_templates()

        init = dict(initial or {})
        project = init.get("project")
        if project and project not in projects:
            projects = [project] + projects

        log.debug(
            "Открытие диалога метаданных: title=%r, projects=%d, "
            "prompts=%d, name_templates=%d, initial_keys=%s",
            title, len(projects), len(prompts), len(name_templates),
            list(init.keys()),
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
        cfg = self.config_manager.config
        projects = cfg.get("projects", []) or []
        default_project = projects[0] if projects else "Default"
        now_str = f"{datetime.now():%Y-%m-%d %H-%M}"
        meta = {
            "project": default_project,
            "name": f"Запись {now_str}",
            "description": f"Запись {now_str}",
            "name_template": "",
            "name_abbr": "",
            "comment": "",
            "prompt": self.config_manager.get_default_prompt(),
            "prompt_name": "",
            "prompt_edited": False,
            "is_scrum": False,
            "generate_deepseek_prompt": False,
            "include_name_in_prompt": False,
            "include_project_in_prompt": False,
            "include_comment_in_prompt": False,
            "previous_protocol_path": "",
            "attachments": [],
            "send_attachments_to_transcribe": False,
            "send_attachments_to_deepseek": False,
        }
        log.debug("Сформированы метаданные по умолчанию: %s", meta)
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
            log.info(
                "Промпт «%s» %s в библиотеку (текст %d символов, "
                "всего промптов: %d)",
                name,
                "обновлён" if replaced else "добавлен",
                len(text),
                len(prompts),
            )
            self._notify("Промпты", f"Промпт «{name}» сохранён в библиотеку")
        except Exception as exc:
            log.exception("Ошибка сохранения промпта: %s", exc)

    def _add_name_template(self, label: str, template: str) -> None:
        """Сохраняет шаблон названия записи в конфиг."""
        try:
            label = (label or "").strip()
            template = (template or "").strip()
            if not template:
                log.warning("Шаблон имени пустой, сохранение отменено")
                return
            self.config_manager.add_name_template(label, template)
            log.info("Шаблон имени сохранён: label=%r, template=%r",
                     label, template)
            self._notify("Шаблоны имён",
                         f"Шаблон «{label or template}» сохранён")
        except Exception as exc:
            log.exception("Ошибка сохранения шаблона имени: %s", exc)

    @staticmethod
    def _copy_attachments_to_session(
        meta: Dict[str, Any], session_dir: str
    ) -> None:
        src_paths = list(meta.get("attachments", []) or [])
        if not src_paths:
            return

        att_dir = os.path.join(session_dir, "attachments")
        os.makedirs(att_dir, exist_ok=True)

        log.info("Копирование вложений: %d файлов → %s",
                 len(src_paths), att_dir)

        new_paths: List[str] = []
        for src in src_paths:
            if not src or not os.path.exists(src):
                log.warning("Вложение не найдено, пропуск: %s", src)
                continue
            base = os.path.basename(src)
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
                log.exception("Ошибка копирования вложения %s: %s", src, exc)

        meta["attachments"] = new_paths
        log.info("Вложений успешно скопировано: %d/%d",
                 len(new_paths), len(src_paths))

    @staticmethod
    def _save_session_metadata(meta: Dict[str, Any], session_dir: str) -> None:
        try:
            os.makedirs(session_dir, exist_ok=True)
            path = os.path.join(session_dir, "session.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2, ensure_ascii=False)
            log.info("Метаданные сохранены: %s", path)
        except Exception as exc:
            log.error("Не удалось сохранить метаданные: %s", exc)

    def _new_session_dir(self) -> str:
        temp = self.config_manager.config["storage"].get(
            "temp_path", "/tmp/screen-recorder"
        )
        sessions_root = os.path.join(temp, "sessions")
        session_dir = os.path.join(
            sessions_root, datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
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
        rec = self._rec_cfg()

        if rec.get("show_metadata_on_start", True):
            meta = self._ask_metadata(title="Параметры записи")
            if meta is None:
                log.info("Пользователь отменил ввод метаданных")
                return
        else:
            meta = self._build_default_meta()

        self._current_session_meta = meta
        log.info(
            "Метаданные для записи получены: project=%s, name=%s, "
            "template=%r, abbr=%r, is_scrum=%s, generate_deepseek=%s, "
            "prompt=%d символов, attachments=%d, "
            "send_to_transcribe=%s, send_to_deepseek=%s",
            meta.get("project"), meta.get("name"),
            meta.get("name_template", ""), meta.get("name_abbr", ""),
            meta.get("is_scrum"), meta.get("generate_deepseek_prompt"),
            len(meta.get("prompt", "") or ""),
            len(meta.get("attachments", []) or []),
            meta.get("send_attachments_to_transcribe"),
            meta.get("send_attachments_to_deepseek"),
        )

        monitors = get_system_monitors()
        idx = int(rec.get("monitor", 0))
        display = monitors[idx]["display"] if 0 <= idx < len(monitors) else ":0.0+0,0"
        log.info("Старт записи: monitor_idx=%d, display=%s, mic=%s",
                 idx, display, rec.get("with_microphone", True))

        self.recorder.set_overlay_text(
            f"{meta.get('name', 'ScreenRecorder')} {datetime.now():%Y-%m-%d %H:%M}"
        )

        async def _run():
            await self.recorder.start_recording(
                display, rec.get("with_microphone", True)
            )

        asyncio.run_coroutine_threadsafe(_run(), self._loop)

    def _toggle_recording(self) -> None:
        status = self.recorder.get_recording_status()
        log.debug("Переключение записи: status=%s", status)
        if status["recording"]:
            self._stop_recording()
        else:
            self._start_recording()

    def _toggle_pause(self) -> None:
        status = self.recorder.get_recording_status()
        if not status["recording"]:
            return
        if status["paused"]:
            log.info("Возобновление записи (UI)")
            asyncio.run_coroutine_threadsafe(
                self.recorder.resume_recording(), self._loop
            )
        else:
            log.info("Пауза записи (UI)")
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
            "Video files (*.mp4 *.mkv *.mov *.avi *.webm *.flv *.wmv);;All files (*)",
        )
        if not file_path:
            log.info("Загрузка видео отменена пользователем")
            return

        if not os.path.isfile(file_path):
            QMessageBox.warning(None, "Загрузить видео",
                                f"Файл не найден:\n{file_path}")
            return

        src_size_mb = os.path.getsize(file_path) / 1024 / 1024
        log.info("Выбран файл для загрузки: %s (%.2f МБ)",
                 file_path, src_size_mb)

        initial = {"name": os.path.splitext(os.path.basename(file_path))[0]}
        meta = self._ask_metadata(
            title="Метаданные загружаемого видео",
            initial=initial,
        )
        if meta is None:
            log.info("Загрузка видео отменена на этапе метаданных")
            return

        session_dir = self._new_session_dir()
        log.info("Папка сессии: %s", session_dir)

        target_video = os.path.join(session_dir, "video.mp4")
        try:
            src_abs = os.path.abspath(file_path)
            dst_abs = os.path.abspath(target_video)
            if src_abs == dst_abs:
                log.info("Файл уже в папке сессии, копирование не нужно")
            else:
                log.info("Копирование %s → %s", src_abs, dst_abs)
                shutil.copy2(src_abs, dst_abs)
        except Exception as exc:
            log.exception("Ошибка копирования файла: %s", exc)
            QMessageBox.critical(None, "Загрузить видео",
                                 f"Не удалось скопировать файл:\n{exc}")
            return

        if not os.path.exists(target_video):
            log.error("Файл после копирования не найден: %s", target_video)
            QMessageBox.critical(None, "Загрузить видео",
                                 "Файл не был скопирован")
            return

        size_mb = os.path.getsize(target_video) / 1024 / 1024
        log.info("Файл скопирован (%.2f МБ), сессия: %s", size_mb, session_dir)

        meta["date"] = datetime.now().strftime("%Y-%m-%d")
        meta["monitor"] = self.config_manager.config["recording"].get("monitor", 0)
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
            log.info("Задача %s добавлена в очередь (загрузка)", task_id)
            self._notify("Загрузить видео",
                         f"Файл добавлен в очередь: {meta.get('name', '')}")
        except Exception as exc:
            log.exception("Не удалось добавить задачу в очередь: %s", exc)
            QMessageBox.critical(None, "Загрузить видео",
                                 f"Не удалось добавить в очередь:\n{exc}")

    # ------------------------------------------------------------------
    # Хоткеи
    # ------------------------------------------------------------------
    def _hotkey_start_recording(self) -> None:
        status = self.recorder.get_recording_status()
        log.debug("Хоткей start/пауза: status=%s", status)
        if status["recording"]:
            if status["paused"]:
                QTimer.singleShot(0, lambda: asyncio.run_coroutine_threadsafe(
                    self.recorder.resume_recording(), self._loop))
            else:
                QTimer.singleShot(0, lambda: asyncio.run_coroutine_threadsafe(
                    self.recorder.pause_recording(), self._loop))
        else:
            QTimer.singleShot(0, self._start_recording)

    def _hotkey_stop_recording(self) -> None:
        log.debug("Хоткей stop")
        QTimer.singleShot(0, self._stop_recording)

    # ------------------------------------------------------------------
    # Обработчики записи
    # ------------------------------------------------------------------
    def _on_recording_started(self) -> None:
        log.info("Событие: запись началась")
        self.tray_manager.set_recording_state("recording")
        if self._show_overlay():
            self.overlay_panel.reset()
            self.overlay_panel.show_panel()
            self.overlay_panel.update_status("Запись...")
        if self._show_start_notification():
            self._notify("Запись", "Запись началась")

    def _on_recording_paused(self) -> None:
        log.info("Событие: запись на паузе")
        self.tray_manager.set_recording_state("paused")
        if self._show_overlay():
            self.overlay_panel.update_status("Пауза")
        self._notify("Запись", "Запись приостановлена")

    def _on_recording_resumed(self) -> None:
        log.info("Событие: запись возобновлена")
        self.tray_manager.set_recording_state("recording")
        if self._show_overlay():
            self.overlay_panel.update_status("Запись...")

    def _on_recording_stopped(self, path: str) -> None:
        log.info("Событие: запись остановлена, файл=%s", path)
        if self._show_overlay():
            self.overlay_panel.reset()
            self.overlay_panel.update_status("Обработка...")

        if not path or not os.path.exists(path):
            log.error("Файл записи не найден: %s", path)
            self._notify("Ошибка", "Файл записи не найден",
                         QSystemTrayIcon.MessageIcon.Critical)
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
                log.info("Метаданные при остановке не подтверждены — "
                         "используем начальные")
        else:
            meta = dict(self._current_session_meta) or self._build_default_meta()

        meta["date"] = datetime.now().strftime("%Y-%m-%d")
        meta["monitor"] = self.config_manager.config["recording"].get("monitor", 0)
        session_dir = os.path.dirname(path)
        meta["session_dir"] = session_dir
        meta["video_path"] = path
        meta["source"] = "record"
        meta.pop("task_id", None)

        self._copy_attachments_to_session(meta, session_dir)
        self._save_session_metadata(meta, session_dir)

        size_mb = os.path.getsize(path) / 1024 / 1024 if os.path.exists(path) else 0
        log.info(
            "Сессия сохранена: dir=%s, video=%s (%.2f МБ), "
            "name=%s, is_scrum=%s, generate_deepseek=%s, attachments=%d",
            session_dir, os.path.basename(path), size_mb,
            meta.get("name"), meta.get("is_scrum", False),
            meta.get("generate_deepseek_prompt", False),
            len(meta.get("attachments", []) or []),
        )

        task_id = self.task_queue.add_task({"video_path": path, "metadata": meta})
        log.info("Задача %s добавлена в очередь", task_id)

        self._current_session_meta = {}

    # ------------------------------------------------------------------
    # Обработка задач
    # ------------------------------------------------------------------
    def _on_task_started(self, task_id: str) -> None:
        if self._is_recording():
            log.debug("Запись идёт — трей не меняем на processing (task=%s)",
                      task_id)
            return
        log.info("Задача %s: трей → processing", task_id)
        self.tray_manager.set_recording_state("processing")

    def _on_task_progress(self, task_id: str, progress: int, stage: str) -> None:
        log.debug("Прогресс %s: %d%% (%s)", task_id, progress, stage)
        if self._show_overlay():
            self.overlay_panel.update_progress(progress, stage)

    def _on_task_completed(self, task_id: str, result: dict) -> None:
        log.info("Задача %s завершена: %s", task_id, list(result.keys()))
        if not self._is_recording():
            self.tray_manager.set_recording_state("idle")
        if self._show_overlay():
            self.overlay_panel.update_progress(100, "Готово")
        output_dir = result.get("output_dir", "")
        if output_dir:
            self._notify("Обработка", f"Файлы сохранены в {output_dir}")
        else:
            self._notify("Обработка", "Задача завершена")
        if self._show_overlay() and not self._is_recording():
            QTimer.singleShot(3000, self.overlay_panel.hide_panel)

    def _on_task_failed(self, task_id: str, error: str) -> None:
        log.error("Задача %s провалена: %s", task_id, error)
        if not self._is_recording():
            self.tray_manager.set_recording_state("idle")
        self._notify("Ошибка обработки", error[:100],
                     QSystemTrayIcon.MessageIcon.Critical)
        if self._show_overlay():
            self.overlay_panel.add_log(f"ERROR: {error}")

    # ------------------------------------------------------------------
    # Настройки, очередь, записи
    # ------------------------------------------------------------------
    def _open_settings(self) -> None:
        log.info("Открытие окна настроек")
        self.settings_window = SettingsWindow(self.config_manager)
        if self.settings_window.exec():
            log.info("Настройки сохранены, перезагрузка конфигурации")
            self.config_manager.load()
            self.recorder.config = self.config_manager.config
            self.processor.config = self.config_manager.config
            self.hotkey_manager.update_hotkeys(self.config_manager.config)
            log.info("Конфигурация перечитана, горячие клавиши обновлены")
        else:
            log.debug("Окно настроек закрыто без сохранения")

    def _open_queue(self) -> None:
        log.info("Открытие окна очереди задач")
        self.queue_window = QueueWindow(self.task_queue, self.processor)
        self.queue_window.show()

    def _open_sessions(self) -> None:
        log.info("Открытие окна списка записей")
        self.sessions_window = SessionsWindow(
            self._sessions_root(),
            self.task_queue,
            self.processor,
            config_manager=self.config_manager,
        )
        self.sessions_window.show()

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
        icon: QSystemTrayIcon.MessageIcon = QSystemTrayIcon.MessageIcon.Information,
    ) -> None:
        self.tray_manager.show_notification(title, message, icon)

    # ------------------------------------------------------------------
    # Завершение
    # ------------------------------------------------------------------
    def _quit(self) -> None:
        log.info("Завершение работы приложения")
        try:
            status = self.task_queue.get_queue_status()
            log.info("Состояние очереди на момент выхода: %s", status)
        except Exception as exc:
            log.warning("Не удалось получить статус очереди: %s", exc)

        self.task_queue.save_queue()
        self.hotkey_manager.unregister_hotkeys()
        if self._processing_task:
            self._processing_task.cancel()
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        log.info("Приложение остановлено")
        self.app.quit()

    def startup(self) -> None:
        log.info("Запуск приложения")
        self.tray_manager.create_tray_icon()
        self.hotkey_manager.register_hotkeys()
        self._notify("Screen Recorder", "Приложение запущено (локальный режим)")


def main() -> int:
    log_level = os.environ.get("SCREEN_RECORDER_LOG_LEVEL", "DEBUG")
    try:
        _cfg = ConfigManager()
        _log_cfg = _cfg.get_log_settings()
        set_log_path(_log_cfg["log_path"])
        log_level = _log_cfg.get("level", log_level)
    except Exception as exc:
        print(f"Предупреждение: не удалось прочитать конфиг ({exc})",
              file=sys.stderr)

    setup_logger(level=log_level)
    log.info("=" * 60)
    log.info("Запуск Screen Recorder & Transcriber (локальный режим)")
    log.info("Python: %s", sys.version)
    log.info("=" * 60)

    if not check_ffmpeg_installed():
        log.critical("ffmpeg не установлен")
        print("ОШИБКА: ffmpeg не установлен. Установите: sudo apt install ffmpeg")
        return 1

    log.info("ffmpeg найден: OK")

    app = QApplication(sys.argv)
    app.setApplicationName("Screen Recorder")
    app.setQuitOnLastWindowClosed(False)

    qss_path = _resource("styles.qss")
    if os.path.exists(qss_path):
        try:
            with open(qss_path, "r", encoding="utf-8") as f:
                app.setStyleSheet(f.read())
            log.info("Стили загружены: %s", qss_path)
        except Exception as exc:
            log.warning("Не удалось загрузить стили: %s", exc)

    controller = ScreenRecorderApp(app)
    controller.startup()

    signal.signal(signal.SIGINT, lambda *_: controller._quit())
    timer = QTimer()
    timer.start(500)
    timer.timeout.connect(lambda: None)

    log.info("Вход в главный цикл приложения")
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())