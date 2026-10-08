"""Окно со списком всех записей (сессий).

Изменения:
  • _send_to_bitrix() использует config.default_chat_id.
  • Добавлена колонка «Теги» и поддержка редактирования тегов
    через карточку метаданных.
  • Интеграция с синхронизацией:
      – пункт меню «Файл → Синхронизация с сервером…»;
      – пункт меню «Файл → Синхронизировать выбранную запись…»;
      – кнопка «Синхронизировать…» на нижней панели;
      – контекстное меню с «Синхронизировать…»;
      – колонка «Синхр.»;
      – диалог SyncOneRecordDialog с выбором передавать ли медиа,
        ссылку file://, удалять ли локальные медиа после загрузки.
  • Добавлен встроенный медиаплеер (см. media_player.py).
  • Пути из QFileDialog нормализуются через safe_local_path().
  • Добавлен просмотр стенограммы (video.txt).
  • Добавлен просмотр протокола (.docx).
  • Добавлен флаг «готово к синхронизации» (sync_ready):
      – колонка «Синхр.» показывает «да» / «готово» / «черновик»;
      – пункт контекстного меню «Отметить как готово к синхронизации»
        / «Снять отметку»;
      – кнопка «Готово к синхронизации» на нижней панели;
      – метод _toggle_sync_ready.
  • Добавлена колонка «Поручения» с количеством поручений
    (из action_items.json).
  • Добавлена панель фильтров над таблицей:
      – фильтр по проекту (выпадающий список);
      – фильтр по периоду (дата с/по + чекбокс «Ограничить»);
      – текстовый поиск по названию, проекту, тегам, дате;
      – кнопка «Сбросить фильтры»;
      – счётчик «Показано: N из M».
  • Метод «Выборка» позволяет фильтровать записи
    по проектам.
  • Пропуск транскрибации при наличии video.txt: диалог
    с выбором «сохранить стенограмму / пересобрать».
"""
from __future__ import annotations

import asyncio
import html
import json
import os
import shutil
import traceback
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, QDate, QThread, QUrl, Signal
from PySide6.QtGui import (
    QGuiApplication, QAction, QDesktopServices, QKeySequence, QColor,
    QFont,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDateEdit, QDialog,
    QDialogButtonBox, QFileDialog, QFrame, QHBoxLayout, QHeaderView,
    QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMenu, QMenuBar, QMessageBox, QPlainTextEdit, QProgressDialog,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from ..daily_digest import (
    build_digest_prompt,
    collect_digest_entries,
    save_digest_prompt,
)
from ..file_readers import read_any_text, read_json_file
from ..logger import get_logger
from ..markdown_docx import markdown_to_docx
from ..markdown_to_bitrix import markdown_to_plain, markdown_to_plain_with_bb
from ..screc_client import ScrecClient, ScrecError
from ..sync_manager import (
    SyncManager,
    build_public_view_url,
    get_record_id,
    get_record_path,
    is_record_published,
)
from ..task_queue import TaskQueue
from ..utils import safe_local_path, sanitize_filename
from .docx_viewer import DocxViewerDialog
from .markdown_editor import MarkdownEditorDialog, MarkdownViewerDialog
from .media_player import (
    is_builtin_player_available, open_media, probe_media_support,
)
from .metadata_dialog import MetadataDialog
from .text_viewer import TextViewerDialog
from .tooltips import attach_tooltip
from ..platform_utils import (
    is_screen_recording_available,
    screen_recording_unavailable_reason,
)
from .tasks_editor_dialog import TasksEditorDialog
from ..action_items_prompt_builder import (
    regenerate_action_items_prompt,
)

log = get_logger(__name__)


# --- Статусы ---
STATUS_UPLOADED = "Сохранено"
STATUS_TRANSCRIBING = "Транскрибация"
STATUS_PROCESSED = "Обработан"
STATUS_ERROR = "Ошибка"
STATUS_UNKNOWN = "Неизвестно"


STATUS_COLORS = {
    STATUS_PROCESSED:    "#2E7D32",
    STATUS_UPLOADED:     "#37474F",
    STATUS_TRANSCRIBING: "#B8860B",
    STATUS_ERROR:        "#C62828",
    STATUS_UNKNOWN:      "#616161",
}


_VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".wmv")
_AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".aac", ".opus", ".ogg")


# ---------------------------------------------------------------------------
# Поток сканирования
# ---------------------------------------------------------------------------
class SessionsScanThread(QThread):
    """Фоновое сканирование папки sessions/."""

    finished_ok = Signal(list)
    failed = Signal(str)

    def __init__(self, sessions_root: str,
                 tasks_index: Dict[str, Dict[str, Any]],
                 parent=None) -> None:
        super().__init__(parent)
        self._sessions_root = sessions_root
        self._tasks_index = tasks_index

    def run(self) -> None:
        try:
            rows = self._collect(
                self._sessions_root, self._tasks_index
            )
            self.finished_ok.emit(rows)
        except Exception as exc:
            log.exception("Ошибка сканирования сессий: %s", exc)
            self.failed.emit(str(exc))

    @staticmethod
    def _find_first_video(session_dir: str) -> str:
        for ext in _VIDEO_EXTS:
            p = os.path.join(session_dir, f"video{ext}")
            if os.path.exists(p):
                return p
        return ""

    @staticmethod
    def _find_first_audio(session_dir: str) -> str:
        for ext in _AUDIO_EXTS:
            p = os.path.join(session_dir, f"video{ext}")
            if os.path.exists(p):
                return p
        return ""

    @staticmethod
    def _find_prompt(session_dir: str) -> str:
        for fname in ("deepseek_prompt.docx", "deepseek_prompt.md",
                      "deepseek_prompt.txt"):
            p = os.path.join(session_dir, fname)
            if os.path.exists(p):
                return p
        return ""

    @staticmethod
    def _extract_tags(meta: Dict[str, Any]) -> List[str]:
        raw = meta.get("tags")
        if not raw:
            return []
        result: List[str] = []
        seen = set()
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, str):
                    name = item.strip()
                elif isinstance(item, dict):
                    name = str(item.get("name") or "").strip()
                else:
                    continue
                if name and name not in seen:
                    result.append(name)
                    seen.add(name)
        return result

    def _collect(
        self,
        sessions_root: str,
        tasks_index: Dict[str, Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        if not os.path.isdir(sessions_root):
            return rows

        try:
            entries = sorted(os.listdir(sessions_root))
        except OSError as exc:
            log.error(
                "Не удалось прочитать %s: %s", sessions_root, exc
            )
            return rows

        for name in entries:
            session_dir = os.path.join(sessions_root, name)
            if not os.path.isdir(session_dir):
                continue

            meta_path = os.path.join(session_dir, "session.json")
            meta = read_json_file(meta_path) or {}

            video_path = self._find_first_video(session_dir)
            has_video = bool(video_path)
            transcript_path = os.path.join(session_dir, "video.txt")
            has_transcript = os.path.exists(transcript_path)
            audio_path = self._find_first_audio(session_dir)
            has_audio = bool(audio_path)

            task = (
                tasks_index.get(os.path.abspath(video_path))
                if video_path else None
            )
            task_id = (task or {}).get("task_id", "")

            if task is not None:
                st = task.get("status", "")
                if st == "completed":
                    status = STATUS_PROCESSED
                elif st == "error":
                    status = STATUS_ERROR
                elif st in ("pending", "converting", "transcribing",
                            "summarizing"):
                    status = STATUS_TRANSCRIBING
                else:
                    status = STATUS_UNKNOWN
            else:
                if has_transcript or has_audio:
                    status = STATUS_PROCESSED
                elif has_video:
                    status = STATUS_UPLOADED
                else:
                    status = STATUS_UNKNOWN

            date_str = meta.get("date") or ""
            time_str = meta.get("time") or ""
            try:
                parts = name.split("_")
                if len(parts) >= 2:
                    date_str = date_str or parts[0]
                    time_str = time_str or parts[1].replace("-", ":")
            except Exception:
                pass
            if not date_str:
                try:
                    ts = os.path.getmtime(session_dir)
                    date_str = datetime.fromtimestamp(ts).strftime(
                        "%Y-%m-%d"
                    )
                    time_str = datetime.fromtimestamp(ts).strftime(
                        "%H:%M:%S"
                    )
                except Exception:
                    pass

            prompt_path = self._find_prompt(session_dir)

            attachments = list(meta.get("attachments", []) or [])
            attachments_dir = os.path.join(session_dir, "attachments")
            if not attachments and os.path.isdir(attachments_dir):
                try:
                    attachments = [
                        os.path.join(attachments_dir, f)
                        for f in sorted(
                            os.listdir(attachments_dir)
                        )
                        if os.path.isfile(
                            os.path.join(attachments_dir, f)
                        )
                    ]
                except OSError:
                    attachments = []

            manual_protocol_path = (
                meta.get("manual_protocol_path") or ""
            )
            if manual_protocol_path and not os.path.exists(
                manual_protocol_path
            ):
                manual_protocol_path = ""

            summary_bb = str(meta.get("summary_bb") or "")
            tags = self._extract_tags(meta)

            published = is_record_published(session_dir)
            record_id = get_record_id(session_dir)

            # --- Флаг готовности к синхронизации ---
            sync_ready = bool(meta.get("sync_ready", False))

            # --- Поручения: разбивка по статусам из action_items.json ---
            action_items_total = 0
            action_items_by_status: Dict[str, int] = {}
            try:
                action_items_path = os.path.join(
                    session_dir, "action_items.json"
                )
                if os.path.isfile(action_items_path):
                    ai_data = read_json_file(action_items_path) or {}
                    items = ai_data.get("items")
                    if isinstance(items, list):
                        action_items_total = len(items)
                        for it in items:
                            if not isinstance(it, dict):
                                continue
                            st = str(it.get("status") or "created").strip().lower()
                            if st not in ("created", "in_progress",
                                        "waiting", "done", "cancelled"):
                                st = "created"
                            action_items_by_status[st] = (
                                action_items_by_status.get(st, 0) + 1
                            )
            except Exception as exc:
                log.warning(
                    "Не удалось прочитать поручения из %s: %s",
                    session_dir, exc,
                )

            rows.append({
                "action_items_total": action_items_total,
                "action_items_by_status": action_items_by_status,
                "action_items_done": action_items_by_status.get("done", 0),
                "action_items_cancelled": action_items_by_status.get("cancelled", 0),
                "dir": session_dir,
                "name": meta.get("name") or name,
                "project": meta.get("project") or "",
                "status": status,
                "source": meta.get("source") or "record",
                "is_scrum": bool(meta.get("is_scrum", False)),
                "datetime": f"{date_str} {time_str}".strip(),
                "video_path": video_path,
                "audio_path": audio_path,
                "has_video": has_video,
                "has_audio": has_audio,
                "transcript_path": (
                    transcript_path if has_transcript else ""
                ),
                "has_transcript": has_transcript,
                "task_id": task_id,
                "prompt_path": prompt_path,
                "attachments": attachments,
                "manual_protocol_path": manual_protocol_path,
                "summary_bb": summary_bb,
                "tags": tags,
                "published": published,
                "record_id": record_id,
                # --- Флаг готовности к синхронизации ---
                "sync_ready": sync_ready,
            })

        rows.sort(key=lambda r: r["datetime"], reverse=True)
        return rows


# ---------------------------------------------------------------------------
# Фоновый воркер для синхронизации одной записи
# ---------------------------------------------------------------------------
class _OneSyncWorker(QThread):
    """Синхронизирует одну запись с сервером."""

    finished_ok = Signal(dict)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(
        self,
        manager: SyncManager,
        session_dir: str,
        include_media: bool,
        send_video_link: bool,
        delete_media_after_upload: bool,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._manager = manager
        self._session_dir = session_dir
        self._include_media = include_media
        self._send_video_link = send_video_link
        self._delete_after = delete_media_after_upload

    def run(self) -> None:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                result = loop.run_until_complete(
                    self._manager.sync_one(
                        self._session_dir,
                        include_media=self._include_media,
                        send_video_link=self._send_video_link,
                        delete_media_after_upload=self._delete_after,
                        progress_cb=self._emit_progress,
                    )
                )
            finally:
                try:
                    loop.run_until_complete(loop.shutdown_asyncgens())
                finally:
                    loop.close()
            self.finished_ok.emit(result)
        except ScrecError as exc:
            log.error("OneSyncWorker: ScrecError: %s", exc)
            self.failed.emit(str(exc))
        except Exception as exc:
            log.exception("OneSyncWorker: ошибка: %s", exc)
            self.failed.emit(f"{exc}\n\n{traceback.format_exc()}")

    def _emit_progress(self, message: str) -> None:
        self.progress.emit(message)


# ---------------------------------------------------------------------------
# Фоновый воркер для скачивания медиа с сервера
# ---------------------------------------------------------------------------
class _MediaDownloadWorker(QThread):
    """Скачивает только медиа-артефакты записи с сервера."""

    finished_ok = Signal(dict)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(
        self,
        manager: SyncManager,
        session_dir: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._manager = manager
        self._session_dir = session_dir

    def run(self) -> None:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                result = loop.run_until_complete(
                    self._manager.download_media_only(
                        self._session_dir,
                        progress_cb=self._emit_progress,
                    )
                )
            finally:
                try:
                    loop.run_until_complete(loop.shutdown_asyncgens())
                finally:
                    loop.close()
            self.finished_ok.emit(result)
        except ScrecError as exc:
            log.error("MediaDownloadWorker: ScrecError: %s", exc)
            self.failed.emit(str(exc))
        except Exception as exc:
            log.exception("MediaDownloadWorker: ошибка: %s", exc)
            self.failed.emit(f"{exc}\n\n{traceback.format_exc()}")

    def _emit_progress(self, message: str) -> None:
        self.progress.emit(message)


# ---------------------------------------------------------------------------
# Диалог синхронизации одной записи
# ---------------------------------------------------------------------------
class SyncOneRecordDialog(QDialog):
    """
    Диалог настройки публикации одной записи на сервер.
    """

    def __init__(
        self,
        row: Dict[str, Any],
        sync_settings: Dict[str, Any],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._row = row
        self._sync_settings = sync_settings or {}

        self.setWindowTitle("Синхронизация записи")
        self.setModal(True)
        self.setMinimumWidth(620)

        self._result_data: Dict[str, Any] = {}

        self._build_ui()
        self._populate()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        name = self._row.get("name") or "(без названия)"
        project = self._row.get("project") or "—"
        dt = self._row.get("datetime") or "—"
        published = bool(self._row.get("published"))
        record_id = self._row.get("record_id") or ""
        sync_ready = bool(self._row.get("sync_ready"))

        status_html = (
            f"<span style='color:#2E7D32'><b>Опубликована</b> "
            f"(record_id: {html.escape(record_id[:8])}…)</span>"
            if published else
            "<span style='color:#B8860B'><b>Ещё не опубликована</b></span>"
        )

        ready_html = (
            "<span style='color:#2E7D32'>готово</span>"
            if sync_ready else
            "<span style='color:#B8860B'>черновик (sync_ready=false)"
            "</span>"
        )

        header = QLabel(
            f"<b>{html.escape(name)}</b><br>"
            f"<span style='color:#666'>Проект:</span> "
            f"{html.escape(project)} &nbsp;|&nbsp; "
            f"<span style='color:#666'>Дата:</span> "
            f"{html.escape(dt)}<br>"
            f"<span style='color:#666'>Статус:</span> {status_html}<br>"
            f"<span style='color:#666'>Готовность:</span> {ready_html}"
        )
        header.setWordWrap(True)
        root.addWidget(header)

        if not sync_ready:
            warn = QLabel(
                "<span style='color:#B8860B'><b>Запись ещё не "
                "помечена как «готова к синхронизации».</b> "
                "Публикация возможна, но фоновый pull будет "
                "игнорировать эту запись, пока вы не поставите "
                "галочку.</span>"
            )
            warn.setWordWrap(True)
            root.addWidget(warn)

        files_label = QLabel("<b>Локальные файлы:</b>")
        root.addWidget(files_label)

        self.files_view = QPlainTextEdit()
        self.files_view.setReadOnly(True)
        self.files_view.setMaximumHeight(140)
        root.addWidget(self.files_view)

        self.send_media_check = QCheckBox(
            "Передавать медиа (видео и аудио) на сервер"
        )
        self.send_media_check.setToolTip(
            "Если включено — видео и аудио уйдут на сервер "
            "как артефакты (kind=video / kind=audio).\n\n"
            "Если выключено — на сервер уйдут только метаданные "
            "и текстовые артефакты (стенограмма, протокол, "
            "summary, промпт, вложения)."
        )
        self.send_media_check.setChecked(
            bool(self._sync_settings.get("send_media_to_server", False))
        )
        root.addWidget(self.send_media_check)

        self.send_video_link_check = QCheckBox(
            "Передавать ссылку на видео (file://…)"
        )
        self.send_video_link_check.setToolTip(
            "Сохраняет в метаданных записи на сервере ссылку "
            "file:///путь/к/video.mp4. На другом устройстве "
            "ссылка не откроется, но она документирует, где "
            "изначально лежал файл."
        )
        self.send_video_link_check.setChecked(
            bool(self._sync_settings.get("send_video_link", True))
        )
        root.addWidget(self.send_video_link_check)

        self.delete_after_check = QCheckBox(
            "Удалить локальные медиа после успешной загрузки "
            "на сервер"
        )
        self.delete_after_check.setToolTip(
            "Работает только если включена передача медиа.\n\n"
            "После успешной загрузки видео/аудио на сервер "
            "локальная копия удаляется. Файл можно будет "
            "скачать обратно через окно «Синхронизация»."
        )
        self.delete_after_check.setChecked(
            bool(
                self._sync_settings.get(
                    "delete_local_media_after_media_upload", False
                )
            )
        )
        root.addWidget(self.delete_after_check)

        self.send_media_check.toggled.connect(
            self.delete_after_check.setEnabled
        )
        self.delete_after_check.setEnabled(
            self.send_media_check.isChecked()
        )

        max_mb = int(
            self._sync_settings.get("max_artifact_mb", 50)
        )
        limit_label = QLabel(
            f"<span style='color:#666'>Максимальный размер "
            f"одного артефакта: <b>{max_mb} МБ</b>. Файлы больше "
            f"лимита будут пропущены.</span>"
        )
        limit_label.setWordWrap(True)
        root.addWidget(limit_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.button(
            QDialogButtonBox.StandardButton.Ok
        ).setText("Синхронизировать")
        buttons.button(
            QDialogButtonBox.StandardButton.Cancel
        ).setText("Отмена")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _populate(self) -> None:
        lines: List[str] = []
        session_dir = self._row.get("dir") or ""

        for kind, exts in (
            ("video", (".mp4", ".mkv", ".mov", ".avi", ".webm",
                       ".flv", ".wmv")),
            ("audio", (".mp3", ".wav", ".m4a", ".aac",
                       ".opus", ".ogg")),
        ):
            for ext in exts:
                p = os.path.join(session_dir, f"video{ext}")
                if os.path.isfile(p):
                    try:
                        size = os.path.getsize(p) / 1024 / 1024
                    except OSError:
                        size = 0
                    lines.append(
                        f"  [{kind}]  video{ext}  —  {size:.2f} МБ"
                    )
                    break

        text_files = [
            ("transcript", "video.txt"),
            ("summary", "video_summary.md"),
            ("summary", "summary.md"),
            ("protocol", "protocol.docx"),
            ("protocol", "protocol.md"),
            ("protocol", "protocol.txt"),
            ("manual_protocol", "manual_protocol.docx"),
            ("manual_protocol", "manual_protocol.md"),
            ("manual_protocol", "manual_protocol.txt"),
            ("manual_protocol", "manual_protocol.pdf"),
            ("deepseek_prompt", "deepseek_prompt.docx"),
            ("deepseek_prompt", "deepseek_prompt.md"),
            ("deepseek_prompt", "deepseek_prompt.txt"),
            ("action_items", "action_items.json"),
        ]
        seen_kinds: set = set()
        for kind, fname in text_files:
            if kind in seen_kinds:
                continue
            p = os.path.join(session_dir, fname)
            if os.path.isfile(p):
                try:
                    size = os.path.getsize(p) / 1024
                except OSError:
                    size = 0
                lines.append(
                    f"  [{kind}]  {fname}  —  {size:.1f} КБ"
                )
                seen_kinds.add(kind)

        att_dir = os.path.join(session_dir, "attachments")
        if os.path.isdir(att_dir):
            try:
                for name in sorted(os.listdir(att_dir)):
                    p = os.path.join(att_dir, name)
                    if os.path.isfile(p):
                        try:
                            size = os.path.getsize(p) / 1024
                        except OSError:
                            size = 0
                        lines.append(
                            f"  [attachment]  {name}  —  "
                            f"{size:.1f} КБ"
                        )
            except OSError:
                pass

        if not lines:
            lines = ["  (нет локальных файлов)"]

        self.files_view.setPlainText("\n".join(lines))

    def _on_accept(self) -> None:
        self._result_data = {
            "include_media": bool(self.send_media_check.isChecked()),
            "send_video_link": bool(
                self.send_video_link_check.isChecked()
            ),
            "delete_media_after_upload": bool(
                self.delete_after_check.isChecked()
            ),
        }
        self.accept()

    def result_data(self) -> Dict[str, Any]:
        return dict(self._result_data)


# ---------------------------------------------------------------------------
# Диалог выбора периода для «Свода за день»
# ---------------------------------------------------------------------------
class DailyDigestDialog(QDialog):
    """
    Диалог формирования свода за период.

    Позволяет выбрать:
      • период (с / по);
      • проекты (один, несколько или все);
      • промпт из библиотеки (с возможностью редактирования);
      • включать ли краткое описание (summary) каждой записи;
      • формат сохранения (docx / md / txt).

    После подтверждения:
      1. Отбирает записи за период и по выбранным проектам.
      2. Собирает промпт через daily_digest.
      3. Сохраняет файл в папку «Загрузки».
    """

    def __init__(
        self,
        sessions_root: str,
        config_manager,
        *,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._sessions_root = sessions_root
        self._config_manager = config_manager

        self.setWindowTitle("Выборка")
        self.setModal(True)
        self.setMinimumSize(860, 780)

        self._result_path: str = ""
        self._result_count: int = 0

        # Список проектов, выбранных пользователем (пусто = все).
        self._selected_projects: List[str] = []

        self._build_ui()
        self._load_projects()
        self._apply_initial_prompt()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # --- Инфо ---
        info = QLabel(
            "Формируется единый промпт из протоколов всех записей "
            "за выбранный период. Промпт можно отредактировать "
            "и сохранить в папку «Загрузки»."
        )
        info.setWordWrap(True)
        info.setStyleSheet("QLabel { color: #666; }")
        root.addWidget(info)

        # --- Период ---
        period_row = QHBoxLayout()
        period_row.addWidget(QLabel("Период:"))

        self.date_from = QDateEdit()
        self.date_from.setCalendarPopup(True)
        self.date_from.setDisplayFormat("yyyy-MM-dd")
        self.date_from.setDate(QDate.currentDate())
        period_row.addWidget(self.date_from)

        period_row.addWidget(QLabel("по:"))

        self.date_to = QDateEdit()
        self.date_to.setCalendarPopup(True)
        self.date_to.setDisplayFormat("yyyy-MM-dd")
        self.date_to.setDate(QDate.currentDate())
        period_row.addWidget(self.date_to)

        self.single_day_check = QCheckBox("Один день")
        self.single_day_check.setChecked(True)
        self.single_day_check.setToolTip(
            "Если включено — дата «по» совпадает с датой «с», "
            "формируется свод за один день."
        )
        self.single_day_check.toggled.connect(
            self._on_single_day_toggled
        )
        period_row.addWidget(self.single_day_check)

        period_row.addSpacing(12)

        self.include_summary_check = QCheckBox(
            "Включать краткое описание (summary)"
        )
        self.include_summary_check.setChecked(False)
        self.include_summary_check.setToolTip(
            "Если включено — в промпт также попадут краткие "
            "описания записей (summary_bb из session.json)."
        )
        period_row.addWidget(self.include_summary_check)

        period_row.addSpacing(12)
        period_row.addWidget(QLabel("Формат:"))
        self.fmt_combo = QComboBox()
        self.fmt_combo.addItem("DOCX", "docx")
        self.fmt_combo.addItem("Markdown", "md")
        self.fmt_combo.addItem("TXT", "txt")
        period_row.addWidget(self.fmt_combo)

        period_row.addStretch()
        root.addLayout(period_row)

        # --- Блок выбора проектов ---
        root.addWidget(self._build_projects_section())

        # --- Промпт ---
        prompt_header = QHBoxLayout()
        prompt_header.addWidget(QLabel("<b>Промпт</b>"))
        prompt_header.addSpacing(12)
        prompt_header.addWidget(QLabel("Из библиотеки:"))
        self.prompt_combo = QComboBox()
        self.prompt_combo.setMinimumWidth(280)
        prompt_header.addWidget(self.prompt_combo, 1)
        prompt_header.addSpacing(8)

        self.reset_prompt_btn = QPushButton("Сбросить правку")
        self.reset_prompt_btn.setToolTip(
            "Вернуть текст промпта к значению из библиотеки."
        )
        self.reset_prompt_btn.clicked.connect(
            self._reset_prompt_from_library
        )
        prompt_header.addWidget(self.reset_prompt_btn)

        root.addLayout(prompt_header)

        self.prompt_edit = QPlainTextEdit()
        self.prompt_edit.setPlaceholderText(
            "Введите инструкцию для ИИ. Например:\n"
            "«Составь сводку по дню: ключевые решения, риски, "
            "поручения. Укажи ответственных и сроки.»"
        )
        self.prompt_edit.setMinimumHeight(260)
        root.addWidget(self.prompt_edit, 1)

        # --- Статус ---
        self.status_label = QLabel("")
        self.status_label.setStyleSheet(
            "QLabel { color: #666; }"
        )
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        # --- Кнопки ---
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.button(
            QDialogButtonBox.StandardButton.Ok
        ).setText("Сформировать и сохранить")
        buttons.button(
            QDialogButtonBox.StandardButton.Cancel
        ).setText("Отмена")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # ------------------------------------------------------------------
    # Блок выбора проектов
    # ------------------------------------------------------------------
    def _build_projects_section(self) -> QWidget:
        """
        Блок выбора проектов: список с чекбоксами, кнопки
        «Выбрать все» / «Снять все» и счётчик выбранных.
        """
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(4)

        header = QHBoxLayout()
        header.addWidget(QLabel("<b>Проекты</b>"))
        header.addSpacing(8)
        hint = QLabel(
            "<span style='color:#666'>Если ничего не отмечено — "
            "свод строится по всем проектам за выбранный "
            "период.</span>"
        )
        hint.setWordWrap(True)
        header.addWidget(hint, 1)
        layout.addLayout(header)

        self.projects_list = QListWidget()
        self.projects_list.setMinimumHeight(120)
        self.projects_list.setMaximumHeight(180)
        self.projects_list.setSelectionMode(
            QListWidget.SelectionMode.NoSelection
        )
        self.projects_list.setAlternatingRowColors(True)
        self.projects_list.itemChanged.connect(
            self._on_project_item_changed
        )
        layout.addWidget(self.projects_list)

        btns = QHBoxLayout()

        self.projects_all_btn = QPushButton("Выбрать все")
        self.projects_all_btn.clicked.connect(
            lambda: self._set_all_projects(True)
        )
        btns.addWidget(self.projects_all_btn)

        self.projects_none_btn = QPushButton("Снять все")
        self.projects_none_btn.clicked.connect(
            lambda: self._set_all_projects(False)
        )
        btns.addWidget(self.projects_none_btn)

        btns.addStretch()

        self.projects_count_label = QLabel("Проектов не выбрано")
        self.projects_count_label.setStyleSheet(
            "QLabel { color: #444; font-weight: bold; }"
        )
        btns.addWidget(self.projects_count_label)

        layout.addLayout(btns)
        return box

    def _load_projects(self) -> None:
        """
        Заполняет список проектов уникальными значениями из
        всех записей в sessions_root.
        """
        projects_set: set = set()
        if os.path.isdir(self._sessions_root):
            try:
                entries = sorted(
                    os.listdir(self._sessions_root)
                )
            except OSError:
                entries = []

            for name in entries:
                session_dir = os.path.join(
                    self._sessions_root, name
                )
                if not os.path.isdir(session_dir):
                    continue
                meta = read_json_file(
                    os.path.join(session_dir, "session.json")
                ) or {}
                p = (meta.get("project") or "").strip()
                if p:
                    projects_set.add(p)

        self.projects_list.blockSignals(True)
        self.projects_list.clear()
        for p in sorted(projects_set):
            item = QListWidgetItem(p)
            item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsUserCheckable
            )
            item.setCheckState(Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, p)
            self.projects_list.addItem(item)
        self.projects_list.blockSignals(False)

        self._selected_projects = []
        self._update_projects_count()

    def _on_project_item_changed(
        self, item: QListWidgetItem,
    ) -> None:
        name = item.data(Qt.ItemDataRole.UserRole) or ""
        if not name:
            return
        if item.checkState() == Qt.CheckState.Checked:
            if name not in self._selected_projects:
                self._selected_projects.append(name)
        else:
            self._selected_projects = [
                p for p in self._selected_projects if p != name
            ]
        self._update_projects_count()

    def _set_all_projects(self, value: bool) -> None:
        self.projects_list.blockSignals(True)
        for i in range(self.projects_list.count()):
            item = self.projects_list.item(i)
            item.setCheckState(
                Qt.CheckState.Checked if value
                else Qt.CheckState.Unchecked
            )
        self.projects_list.blockSignals(False)

        if value:
            self._selected_projects = [
                self.projects_list.item(i).data(
                    Qt.ItemDataRole.UserRole
                )
                for i in range(self.projects_list.count())
            ]
        else:
            self._selected_projects = []

        self._update_projects_count()

    def _update_projects_count(self) -> None:
        if not hasattr(self, "projects_count_label"):
            return
        n = len(self._selected_projects)
        total = self.projects_list.count()

        if n == 0:
            self.projects_count_label.setText(
                f"Все проекты ({total})"
            )
        elif n == total:
            self.projects_count_label.setText(
                f"Выбраны все ({n})"
            )
        else:
            self.projects_count_label.setText(
                f"Выбрано: {n} из {total}"
            )

    # ------------------------------------------------------------------
    # Промпт
    # ------------------------------------------------------------------
    def _apply_initial_prompt(self) -> None:
        """Заполняет выпадающий список промптов и ставит дефолт."""
        if self._config_manager is None:
            return

        try:
            prompts = self._config_manager.get_prompts()
            default_prompt = self._config_manager.get_default_prompt()
        except Exception as exc:
            log.warning("Не удалось прочитать промпты: %s", exc)
            prompts, default_prompt = [], ""

        self.prompt_combo.blockSignals(True)
        self.prompt_combo.clear()
        self.prompt_combo.addItem("— не выбрано —", "")

        current_idx = 0
        for i, p in enumerate(prompts, start=1):
            self.prompt_combo.addItem(p["name"], p["text"])
            if p.get("text", "").strip() == default_prompt.strip():
                current_idx = i

        self.prompt_combo.setCurrentIndex(current_idx)
        self.prompt_combo.blockSignals(False)
        self.prompt_combo.currentIndexChanged.connect(
            self._on_prompt_selected
        )

        self._on_prompt_selected(current_idx)

    def _on_prompt_selected(self, index: int) -> None:
        if index < 0:
            return
        text = self.prompt_combo.itemData(index) or ""
        self.prompt_edit.setPlainText(text)

    def _reset_prompt_from_library(self) -> None:
        index = self.prompt_combo.currentIndex()
        self._on_prompt_selected(index)

    # ------------------------------------------------------------------
    # Период
    # ------------------------------------------------------------------
    def _on_single_day_toggled(self, checked: bool) -> None:
        if checked:
            self.date_to.setDate(self.date_from.date())
        self.date_to.setEnabled(not checked)

    # ------------------------------------------------------------------
    # Отбор сессий за период
    # ------------------------------------------------------------------
    def _collect_sessions_for_period(self) -> List[str]:
        """
        Возвращает список папок сессий за выбранный период
        с учётом выбранных проектов.
        """
        qd_from = self.date_from.date()
        qd_to = (
            qd_from if self.single_day_check.isChecked()
            else self.date_to.date()
        )
        if qd_to < qd_from:
            qd_from, qd_to = qd_to, qd_from

        d_from = date(qd_from.year(), qd_from.month(), qd_from.day())
        d_to = date(qd_to.year(), qd_to.month(), qd_to.day())

        if not os.path.isdir(self._sessions_root):
            return []

        # --- Фильтр по проектам ---
        selected_projects_set = set(self._selected_projects)

        result: List[str] = []
        try:
            entries = sorted(os.listdir(self._sessions_root))
        except OSError as exc:
            log.error(
                "Не удалось прочитать %s: %s",
                self._sessions_root, exc,
            )
            return []

        for name in entries:
            session_dir = os.path.join(self._sessions_root, name)
            if not os.path.isdir(session_dir):
                continue
            if not os.path.isfile(
                os.path.join(session_dir, "session.json")
            ):
                continue

            meta = read_json_file(
                os.path.join(session_dir, "session.json")
            ) or {}

            # --- Фильтр по проектам ---
            if selected_projects_set:
                project = (meta.get("project") or "").strip()
                if project not in selected_projects_set:
                    continue

            date_str = str(meta.get("date") or "").strip()
            if not date_str:
                base = name.split("_")[0]
                if len(base) == 10:
                    date_str = base

            if not date_str:
                continue

            try:
                y, m, d = date_str.split("-")
                sd = date(int(y), int(m), int(d))
            except Exception:
                continue

            if d_from <= sd <= d_to:
                result.append(session_dir)

        return result

    # ------------------------------------------------------------------
    # Подтверждение
    # ------------------------------------------------------------------
    def _on_accept(self) -> None:
        instruction = self.prompt_edit.toPlainText().strip()
        if not instruction:
            QMessageBox.warning(
                self, "Выборка",
                "Введите текст промпта (инструкцию для ИИ).",
            )
            return

        session_dirs = self._collect_sessions_for_period()
        if not session_dirs:
            projects_note = ""
            if self._selected_projects:
                projects_note = (
                    "\n\nВыбранные проекты: "
                    + ", ".join(sorted(self._selected_projects))
                )
            QMessageBox.information(
                self, "Выборка",
                "За выбранный период не найдено ни одной записи."
                + projects_note,
            )
            return

        # --- Сбор данных ---
        QGuiApplication.setOverrideCursor(
            Qt.CursorShape.WaitCursor
        )
        try:
            entries = collect_digest_entries(session_dirs)
        finally:
            QGuiApplication.restoreOverrideCursor()

        if not entries:
            QMessageBox.warning(
                self, "Выборка",
                "Не удалось прочитать данные записей за период.",
            )
            return

        # --- Метка периода ---
        if self.single_day_check.isChecked():
            period_label = entries[0]["date"]
        else:
            dates = [e["date"] for e in entries if e.get("date")]
            if dates:
                period_label = f"{min(dates)} … {max(dates)}"
            else:
                period_label = "—"

        # --- Сборка промпта ---
        prompt_text = build_digest_prompt(
            entries,
            user_instruction=instruction,
            period_label=period_label,
            include_summary=self.include_summary_check.isChecked(),
        )

        if not prompt_text.strip():
            QMessageBox.warning(
                self, "Выборка",
                "Промпт получился пустым.",
            )
            return

        # --- Папка «Загрузки» ---
        downloads = self._downloads_dir()
        if not os.path.isdir(downloads):
            QMessageBox.warning(
                self, "Выборка",
                f"Папка «Загрузки» не найдена:\n{downloads}",
            )
            return

        fmt = self.fmt_combo.currentData() or "docx"
        base_name = self._make_base_name(period_label)

        path = save_digest_prompt(
            prompt_text,
            downloads,
            fmt=fmt,
            base_name=base_name,
        )

        if not path:
            QMessageBox.critical(
                self, "Выборка",
                "Не удалось сохранить файл промпта.",
            )
            return

        self._result_path = path
        self._result_count = len(entries)

        log.info(
            "Выборка: %d записей, период=%s, проекты=%s, файл=%s",
            len(entries), period_label,
            self._selected_projects or "все",
            path,
        )

        # --- Итог ---
        protocol_count = sum(
            1 for e in entries if e.get("protocol_text")
        )

        if self._selected_projects:
            if len(self._selected_projects) <= 5:
                projects_line = (
                    "<b>Проекты:</b> "
                    + ", ".join(
                        html.escape(p)
                        for p in sorted(self._selected_projects)
                    )
                    + "<br>"
                )
            else:
                projects_line = (
                    f"<b>Проекты:</b> выбрано "
                    f"{len(self._selected_projects)}<br>"
                )
        else:
            projects_line = "<b>Проекты:</b> все<br>"

        reply = QMessageBox.question(
            self, "Выборка",
            f"<b>Свод сформирован.</b><br><br>"
            f"{projects_line}"
            f"Записей за период: <b>{len(entries)}</b><br>"
            f"Протоколов прочитано: <b>{protocol_count}</b><br>"
            f"Файл сохранён:<br><code>{path}</code><br><br>"
            f"Открыть папку «Загрузки»?",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply == QMessageBox.StandardButton.Yes:
            QDesktopServices.openUrl(
                QUrl.fromLocalFile(downloads)
            )

        self.accept()

    @staticmethod
    def _make_base_name(period_label: str) -> str:
        """Формирует безопасное имя файла по метке периода."""
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        safe = period_label.replace("…", "").replace(" ", "_")
        safe = "".join(
            c for c in safe if c.isalnum() or c in "-_."
        )
        safe = safe.strip("._-") or "period"
        return f"daily_digest_{safe}_{stamp}"

    @staticmethod
    def _downloads_dir() -> str:
        """Возвращает путь к папке «Загрузки»."""
        try:
            import subprocess
            out = subprocess.check_output(
                ["xdg-user-dir", "DOWNLOAD"],
                text=True, timeout=2,
            ).strip()
            if out and os.path.isdir(out):
                return out
        except Exception:
            pass

        home = os.path.expanduser("~")
        for candidate in ("Загрузки", "Downloads"):
            p = os.path.join(home, candidate)
            if os.path.isdir(p):
                return p
        return home

    # ------------------------------------------------------------------
    # Результат
    # ------------------------------------------------------------------
    def result_path(self) -> str:
        return self._result_path

    def result_count(self) -> int:
        return self._result_count


# ---------------------------------------------------------------------------
# Диалог публичной ссылки на запись
# ---------------------------------------------------------------------------
class PublicLinkDialog(QDialog):
    """
    Диалог с публичной ссылкой на запись.

    Позволяет выбрать блоки (video, audio, transcript, protocol,
    summary), увидеть итоговую ссылку, скопировать её или
    открыть в браузере.
    """

    def __init__(
        self,
        record_name: str,
        record_path: str,
        record_id: str,
        base_url: str,
        *,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Ссылка на запись на сервере")
        self.setModal(True)
        self.setMinimumWidth(760)

        self._record_name = record_name or ""
        self._record_path = record_path or ""
        self._record_id = record_id or ""
        self._base_url = (base_url or "").rstrip("/")

        self._block_checkboxes: Dict[str, QCheckBox] = {}

        self._build_ui()
        self._refresh()

    def _build_ui(self) -> None:
        from PySide6.QtWidgets import QDialogButtonBox

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # --- Инфо ---
        info = QLabel(
            f"<b>{html.escape(self._record_name)}</b><br>"
            f"<span style='color:#666'>Путь на сервере:</span> "
            f"<code>{html.escape(self._record_path)}</code><br>"
            f"<span style='color:#666'>Record ID:</span> "
            f"<code>{html.escape(self._record_id)}</code>"
        )
        info.setWordWrap(True)
        root.addWidget(info)

        hint = QLabel(
            "<span style='color:#666'>Ссылка ведёт на публичную "
            "HTML-страницу записи на сервере. Открывается в "
            "браузере без авторизации — можно отправить коллеге. "
            "Выберите, какие блоки показывать.</span>"
        )
        hint.setWordWrap(True)
        root.addWidget(hint)

        # --- Блоки ---
        blocks_header = QLabel("<b>Блоки на странице</b>")
        root.addWidget(blocks_header)

        blocks_row = QHBoxLayout()
        block_labels = {
            "video": "Видео",
            "audio": "Аудио",
            "transcript": "Стенограмма",
            "protocol": "Протокол",
            "summary": "Summary",
        }
        for key, label in block_labels.items():
            cb = QCheckBox(label)
            cb.setChecked(True)
            cb.toggled.connect(self._refresh)
            blocks_row.addWidget(cb)
            self._block_checkboxes[key] = cb

        blocks_row.addStretch()

        all_btn = QPushButton("Все")
        all_btn.clicked.connect(lambda: self._set_all(True))
        blocks_row.addWidget(all_btn)

        none_btn = QPushButton("Ничего")
        none_btn.clicked.connect(lambda: self._set_all(False))
        blocks_row.addWidget(none_btn)

        root.addLayout(blocks_row)

        # --- Ссылка ---
        link_label = QLabel("<b>Итоговая ссылка</b>")
        root.addWidget(link_label)

        link_row = QHBoxLayout()

        self.link_input = QLineEdit()
        self.link_input.setReadOnly(True)
        link_row.addWidget(self.link_input, 1)

        copy_btn = QPushButton("Скопировать")
        copy_btn.clicked.connect(self._copy)
        link_row.addWidget(copy_btn)

        open_btn = QPushButton("Открыть в браузере")
        open_btn.clicked.connect(self._open)
        link_row.addWidget(open_btn)

        root.addLayout(link_row)

        # --- Кнопки ---
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Close,
            parent=self,
        )
        buttons.button(
            QDialogButtonBox.StandardButton.Close
        ).setText("Закрыть")
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        root.addWidget(buttons)

    def _set_all(self, value: bool) -> None:
        for cb in self._block_checkboxes.values():
            cb.setChecked(value)

    def _selected_blocks(self) -> List[str]:
        return [
            key for key, cb in self._block_checkboxes.items()
            if cb.isChecked()
        ]

    def _refresh(self) -> None:
        blocks = self._selected_blocks()
        # Если все включены — не добавляем параметр (сервер
        # покажет all).
        if set(blocks) == set(self._block_checkboxes.keys()):
            blocks_arg = None
        else:
            blocks_arg = blocks or None

        url = build_public_view_url(
            self._base_url,
            self._record_path,
            self._record_id,
            blocks=blocks_arg,
        )
        self.link_input.setText(url)

    def _copy(self) -> None:
        link = self.link_input.text().strip()
        if not link:
            return
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(link)
        log.info("Ссылка скопирована: %s", link)

    def _open(self) -> None:
        link = self.link_input.text().strip()
        if not link:
            return
        QDesktopServices.openUrl(QUrl(link))
        log.info("Открытие ссылки в браузере: %s", link)


# ---------------------------------------------------------------------------
# Основное окно
# ---------------------------------------------------------------------------
class SessionsWindow(QDialog):
    """Список всех записей в папке sessions/."""

    import_requested = Signal()

    def __init__(
        self,
        sessions_root: str,
        task_queue: TaskQueue,
        processor,
        config_manager=None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.sessions_root = sessions_root
        self.task_queue = task_queue
        self.processor = processor
        self.config_manager = config_manager
        self.setWindowTitle("Записи")
        self.setMinimumSize(1600, 800)
        self.setModal(False)
        self._rows: List[Dict[str, Any]] = []
        # Полный список записей (до фильтрации).
        self._all_rows: List[Dict[str, Any]] = []
        self._thread: Optional[SessionsScanThread] = None
        self._sync_worker: Optional[_OneSyncWorker] = None
        self._sync_progress_dlg: Optional[QProgressDialog] = None
        self._media_worker: Optional[_MediaDownloadWorker] = None
        self._media_progress_dlg: Optional[QProgressDialog] = None
        self._pending_media_request: Optional[Dict[str, Any]] = None

        self._app_cfg: Dict[str, Any] = {}
        if config_manager is not None:
            try:
                self._app_cfg = config_manager.get_app_settings()
            except Exception as exc:
                log.warning(
                    "Не удалось прочитать app-настройки: %s", exc
                )

        self._build_ui()
        self._build_menu_bar()
        self.refresh()

        log.debug(
            "SessionsWindow: встроенный плеер %s",
            "доступен" if is_builtin_player_available()
            else f"недоступен ({probe_media_support()})",
        )

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 4, 8, 8)
        root.setSpacing(6)

        header = QHBoxLayout()
        self.summary_label = QLabel("")
        header.addWidget(self.summary_label)
        header.addSpacing(20)
        self.selection_label = QLabel("")
        self.selection_label.setStyleSheet("QLabel { color: #666; }")
        header.addWidget(self.selection_label)
        header.addStretch()

        self.refresh_indicator = QLabel("")
        self.refresh_indicator.setStyleSheet(
            "QLabel { color: #4a90d9; font-style: italic; }"
        )
        header.addWidget(self.refresh_indicator)
        root.addLayout(header)

        # --- Панель фильтров ---
        root.addWidget(self._build_filters_bar())

        self.table = QTableWidget(0, 13)
        self.table.setHorizontalHeaderLabels([
            "Дата и время", "Название", "Проект", "Теги",
            "Статус", "Источник", "Скрам", "Вложения",
            "Поручения", "Summary", "Синхр.", "Task ID",
            "Папка",
        ])
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        hv = self.table.horizontalHeader()
        hv.setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        # Название — тянется на всё свободное место.
        hv.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hv.setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        hv.setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents
        )
        hv.setSectionResizeMode(
            4, QHeaderView.ResizeMode.ResizeToContents
        )
        hv.setSectionResizeMode(
            5, QHeaderView.ResizeMode.ResizeToContents
        )
        hv.setSectionResizeMode(
            6, QHeaderView.ResizeMode.ResizeToContents
        )
        hv.setSectionResizeMode(
            7, QHeaderView.ResizeMode.ResizeToContents
        )
        # Поручения — по ширине текста.
        hv.setSectionResizeMode(
            8, QHeaderView.ResizeMode.ResizeToContents
        )
        # Summary — по ширине текста.
        hv.setSectionResizeMode(
            9, QHeaderView.ResizeMode.ResizeToContents
        )
        hv.setSectionResizeMode(
            10, QHeaderView.ResizeMode.ResizeToContents
        )
        hv.setSectionResizeMode(
            11, QHeaderView.ResizeMode.ResizeToContents
        )
        hv.setSectionResizeMode(
            12, QHeaderView.ResizeMode.ResizeToContents
        )
        # Скрываем колонку «Папка» (индекс 12).
        self.table.setColumnHidden(12, True)
        self.table.itemSelectionChanged.connect(
            self._on_selection_changed
        )
        self.table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.table.customContextMenuRequested.connect(
            self._show_context_menu
        )
        root.addWidget(self.table, 1)

        root.addWidget(self._build_legend())

        # ------------------------------------------------------------------
        # Нижняя панель — ДВА РЯДА кнопок.
        # ------------------------------------------------------------------
        bottom_content = QHBoxLayout()
        bottom_content.setSpacing(6)

        self.play_video_btn = QPushButton("Смотреть видео")
        self.play_video_btn.setToolTip(
            "Открыть видео во встроенном плеере.\n\n"
            "Если файла нет локально, но он есть на сервере — "
            "будет предложено скачать его."
        )
        self.play_video_btn.clicked.connect(self._open_video)
        bottom_content.addWidget(self.play_video_btn)

        self.play_audio_btn = QPushButton("Прослушать аудио")
        self.play_audio_btn.setToolTip(
            "Открыть аудио во встроенном плеере.\n\n"
            "Если файла нет локально, но он есть на сервере — "
            "будет предложено скачать его."
        )
        self.play_audio_btn.clicked.connect(self._open_audio)
        bottom_content.addWidget(self.play_audio_btn)

        self.open_transcript_btn = QPushButton("Стенограмма")
        attach_tooltip(
            self.open_transcript_btn, "sess_transcript_button"
        )
        self.open_transcript_btn.clicked.connect(
            self._open_transcript
        )
        bottom_content.addWidget(self.open_transcript_btn)

        self.open_protocol_btn = QPushButton("Протокол")
        attach_tooltip(
            self.open_protocol_btn, "sess_protocol_button"
        )
        self.open_protocol_btn.clicked.connect(
            self._view_manual_protocol
        )
        bottom_content.addWidget(self.open_protocol_btn)

        bottom_content.addStretch()
        root.addLayout(bottom_content)

        # --- Ряд 2: сервер и сервисные действия ---
        bottom_actions = QHBoxLayout()
        bottom_actions.setSpacing(6)

        self.sync_ready_btn = QPushButton("Готово к синхронизации")
        attach_tooltip(
            self.sync_ready_btn, "sess_sync_ready_button"
        )
        self.sync_ready_btn.clicked.connect(self._toggle_sync_ready)
        bottom_actions.addWidget(self.sync_ready_btn)

        self.sync_btn = QPushButton("Синхронизировать…")
        self.sync_btn.setToolTip(
            "Опубликовать выбранную запись на сервер "
            "синхронизации.\n\n"
            "Откроется диалог, где можно выбрать:\n"
            "  • передавать ли медиа (видео и аудио);\n"
            "  • передавать ли ссылку file://;\n"
            "  • удалять ли локальные медиа после загрузки.\n\n"
            "Горячая клавиша: Ctrl+Shift+S"
        )
        self.sync_btn.clicked.connect(self._sync_one_record)
        bottom_actions.addWidget(self.sync_btn)

        # --- Публичная ссылка на сервер ---
        self.public_link_btn = QPushButton("Открыть в браузере")
        self.public_link_btn.setToolTip(
            "Открыть публичную HTML-страницу записи на сервере "
            "синхронизации (без авторизации).\n\n"
            "Работает, только если запись опубликована и задан "
            "base_url (Настройки → Синхронизация).\n\n"
            "Горячая клавиша: Ctrl+Shift+U"
        )
        self.public_link_btn.clicked.connect(
            self._open_public_link_dialog
        )
        self.public_link_btn.setEnabled(False)
        bottom_actions.addWidget(self.public_link_btn)

        self.copy_public_link_btn = QPushButton("Скопировать ссылку")
        self.copy_public_link_btn.setToolTip(
            "Скопировать в буфер обмена публичную ссылку на запись."
        )
        self.copy_public_link_btn.clicked.connect(
            self._copy_public_link
        )
        self.copy_public_link_btn.setEnabled(False)
        bottom_actions.addWidget(self.copy_public_link_btn)

        self.regen_action_prompt_btn = QPushButton(
            "Обновить промпт поручений"
        )
        self.regen_action_prompt_btn.setToolTip(
            "Пересобрать action_items_prompt.<ext> на основе "
            "текущего протокола записи.\n\n"
            "Полезно после ручной правки протокола — не нужно "
            "перезапускать всю обработку."
        )
        self.regen_action_prompt_btn.clicked.connect(
            self._regenerate_action_prompt
        )
        bottom_actions.addWidget(self.regen_action_prompt_btn)

        bottom_actions.addStretch()

        self.refresh_btn = QPushButton("Обновить")
        self.refresh_btn.clicked.connect(self.refresh)
        bottom_actions.addWidget(self.refresh_btn)

        self.daily_digest_btn = QPushButton("Выборка")
        self.daily_digest_btn.setToolTip(
            "Сформировать единый промпт из протоколов всех "
            "записей за выбранный период.\n\n"
            "Результат сохраняется в папку «Загрузки» в формате "
            "DOCX / Markdown / TXT."
        )
        self.daily_digest_btn.clicked.connect(
            self._open_daily_digest
        )
        bottom_actions.addWidget(self.daily_digest_btn)

        self.tasks_btn = QPushButton("Поручения")
        self.tasks_btn.setToolTip(
            "Открыть редактор поручений выбранной записи.\n\n"
            "Поручения хранятся в action_items.json и передаются "
            "на сервер синхронизации."
        )
        self.tasks_btn.clicked.connect(self._open_tasks_editor)
        bottom_actions.addWidget(self.tasks_btn)

        self.close_btn = QPushButton("Закрыть")
        self.close_btn.clicked.connect(self.close)
        bottom_actions.addWidget(self.close_btn)

        root.addLayout(bottom_actions)

    def _build_filters_bar(self) -> QWidget:
        """
        Панель фильтров над таблицей: проект, период,
        текстовый поиск. Все фильтры применяются к уже
        загруженному списку записей (self._all_rows) без
        повторного сканирования.
        """
        box = QFrame()
        box.setFrameShape(QFrame.Shape.StyledPanel)
        box.setStyleSheet(
            "QFrame { background-color: palette(window); "
            "border: 1px solid palette(mid); border-radius: 6px; }"
        )
        row = QHBoxLayout(box)
        row.setContentsMargins(10, 6, 10, 6)
        row.setSpacing(8)

        # --- Проект ---
        row.addWidget(QLabel("Проект:"))
        self.filter_project_combo = QComboBox()
        self.filter_project_combo.setMinimumWidth(180)
        self.filter_project_combo.addItem("— все проекты —", "")
        self.filter_project_combo.currentIndexChanged.connect(
            self._apply_filters
        )
        self.filter_project_combo.setToolTip(
            "Показать записи только выбранного проекта."
        )
        row.addWidget(self.filter_project_combo)

        row.addSpacing(12)

        # --- Период: с ---
        row.addWidget(QLabel("Период с:"))
        self.filter_date_from = QDateEdit()
        self.filter_date_from.setCalendarPopup(True)
        self.filter_date_from.setDisplayFormat("yyyy-MM-dd")
        self.filter_date_from.setDate(
            QDate.currentDate().addMonths(-1)
        )
        self.filter_date_from.dateChanged.connect(self._apply_filters)
        self.filter_date_from.setToolTip(
            "Начало периода (включительно)."
        )
        row.addWidget(self.filter_date_from)

        # --- Период: по ---
        row.addWidget(QLabel("по:"))
        self.filter_date_to = QDateEdit()
        self.filter_date_to.setCalendarPopup(True)
        self.filter_date_to.setDisplayFormat("yyyy-MM-dd")
        self.filter_date_to.setDate(QDate.currentDate())
        self.filter_date_to.dateChanged.connect(self._apply_filters)
        self.filter_date_to.setToolTip(
            "Конец периода (включительно)."
        )
        row.addWidget(self.filter_date_to)

        # --- Чекбокс «Ограничить по датам» ---
        self.filter_date_enabled = QCheckBox("Ограничить по датам")
        self.filter_date_enabled.setChecked(False)
        self.filter_date_enabled.setToolTip(
            "Если снять галочку — период игнорируется, "
            "показываются все записи."
        )
        self.filter_date_enabled.toggled.connect(
            self._on_date_filter_toggled
        )
        row.addWidget(self.filter_date_enabled)

        # --- Кнопка «Сбросить фильтры» ---
        self.filter_reset_btn = QPushButton("Сбросить фильтры")
        self.filter_reset_btn.setToolTip(
            "Убрать все фильтры (проект, даты, поиск)."
        )
        self.filter_reset_btn.clicked.connect(self._reset_filters)
        row.addWidget(self.filter_reset_btn)

        row.addStretch()

        # --- Текстовый поиск ---
        row.addWidget(QLabel("Поиск:"))
        self.filter_search_input = QLineEdit()
        self.filter_search_input.setPlaceholderText(
            "Подстрока в названии, тегах, проекте…"
        )
        self.filter_search_input.setClearButtonEnabled(True)
        self.filter_search_input.setMinimumWidth(220)
        self.filter_search_input.textChanged.connect(
            self._apply_filters
        )
        self.filter_search_input.setToolTip(
            "Поиск по подстроке без учёта регистра в названии, "
            "тегах, проекте, дате, имени папки."
        )
        row.addWidget(self.filter_search_input)

        # --- Счётчик показанных записей ---
        self.filter_count_label = QLabel("")
        self.filter_count_label.setStyleSheet(
            "QLabel { color: #444; font-weight: bold; }"
        )
        self.filter_count_label.setMinimumWidth(140)
        self.filter_count_label.setAlignment(
            Qt.AlignmentFlag.AlignRight
            | Qt.AlignmentFlag.AlignVCenter
        )
        row.addWidget(self.filter_count_label)

        # --- Начальное состояние ---
        # Устанавливаем enabled-состояние для полей дат напрямую,
        # не вызывая _apply_filters (таблица ещё не создана —
        # _build_filters_bar вызывается до _build_table).
        self.filter_date_from.setEnabled(
            self.filter_date_enabled.isChecked()
        )
        self.filter_date_to.setEnabled(
            self.filter_date_enabled.isChecked()
        )

        return box

    def _on_date_filter_toggled(self, enabled: bool) -> None:
        self.filter_date_from.setEnabled(enabled)
        self.filter_date_to.setEnabled(enabled)
        self._apply_filters()

    def _reset_filters(self) -> None:
        self.filter_project_combo.blockSignals(True)
        self.filter_project_combo.setCurrentIndex(0)
        self.filter_project_combo.blockSignals(False)

        self.filter_date_enabled.blockSignals(True)
        self.filter_date_enabled.setChecked(False)
        self.filter_date_enabled.blockSignals(False)

        self.filter_date_from.setDate(
            QDate.currentDate().addMonths(-1)
        )
        self.filter_date_to.setDate(QDate.currentDate())

        self.filter_search_input.clear()

        self._apply_filters()
        log.info("Записи: фильтры сброшены")

    def _refresh_project_filter(self) -> None:
        """
        Заполняет выпадающий список проектов уникальными
        значениями из self._all_rows.
        """
        if not hasattr(self, "filter_project_combo"):
            return

        current = self.filter_project_combo.currentData() or ""

        projects = sorted({
            (r.get("project") or "").strip()
            for r in self._all_rows
            if (r.get("project") or "").strip()
        })

        self.filter_project_combo.blockSignals(True)
        self.filter_project_combo.clear()
        self.filter_project_combo.addItem("— все проекты —", "")
        for p in projects:
            self.filter_project_combo.addItem(p, p)
        idx = self.filter_project_combo.findData(current)
        if idx >= 0:
            self.filter_project_combo.setCurrentIndex(idx)
        self.filter_project_combo.blockSignals(False)

    def _apply_filters(self) -> None:
        """
        Применяет фильтры (проект, период, поиск) к
        self._all_rows и обновляет self._rows + таблицу.
        """
        if not hasattr(self, "filter_project_combo"):
            return
        # Таблица ещё не создана — фильтры применим позже,
        # при первом заполнении через _on_scan_finished.
        if not hasattr(self, "table"):
            return

        project_filter = (
            self.filter_project_combo.currentData() or ""
        )
        date_enabled = self.filter_date_enabled.isChecked()
        search_query = (
            self.filter_search_input.text().strip().lower()
        )

        d_from = None
        d_to = None
        if date_enabled:
            qd_from = self.filter_date_from.date()
            qd_to = self.filter_date_to.date()
            if qd_to < qd_from:
                qd_from, qd_to = qd_to, qd_from
            d_from = date(
                qd_from.year(), qd_from.month(), qd_from.day()
            )
            d_to = date(
                qd_to.year(), qd_to.month(), qd_to.day()
            )

        filtered: List[Dict[str, Any]] = []

        for r in self._all_rows:
            # --- Фильтр по проекту ---
            if project_filter:
                if (r.get("project") or "") != project_filter:
                    continue

            # --- Фильтр по периоду ---
            if date_enabled and d_from and d_to:
                r_date_str = (r.get("datetime") or "").strip()
                if not r_date_str:
                    continue
                date_part = r_date_str.split(" ", 1)[0]
                try:
                    y, m, d = date_part.split("-")
                    r_date = date(int(y), int(m), int(d))
                except Exception:
                    continue
                if not (d_from <= r_date <= d_to):
                    continue

            # --- Текстовый поиск ---
            if search_query:
                haystack_parts = [
                    str(r.get("name") or ""),
                    str(r.get("project") or ""),
                    str(r.get("datetime") or ""),
                    " ".join(r.get("tags") or []),
                    os.path.basename(r.get("dir") or ""),
                ]
                haystack = " ".join(haystack_parts).lower()
                if search_query not in haystack:
                    continue

            filtered.append(r)

        self._rows = filtered
        self._render_rows()
        self._update_summary()

        if hasattr(self, "filter_count_label"):
            total = len(self._all_rows)
            shown = len(filtered)
            if shown == total:
                self.filter_count_label.setText(
                    f"Показано: {shown}"
                )
            else:
                self.filter_count_label.setText(
                    f"Показано: {shown} из {total}"
                )

    def _build_menu_bar(self) -> None:
        bar = QMenuBar(self)
        _MENU_QSS = (
            "QMenuBar {"
            "  background-color: palette(window);"
            "  color: palette(window-text);"
            "}"
            "QMenuBar::item {"
            "  background: transparent;"
            "  color: palette(window-text);"
            "  padding: 4px 10px;"
            "}"
            "QMenuBar::item:selected {"
            "  background-color: #2D7FF9;"
            "  color: #FFFFFF;"
            "}"
            "QMenuBar::item:pressed {"
            "  background-color: #1E5FBF;"
            "  color: #FFFFFF;"
            "}"
            "QMenu {"
            "  background-color: palette(window);"
            "  color: palette(window-text);"
            "  border: 1px solid palette(mid);"
            "}"
            "QMenu::item {"
            "  background: transparent;"
            "  color: palette(window-text);"
            "  padding: 5px 24px 5px 24px;"
            "}"
            "QMenu::item:selected {"
            "  background-color: #2D7FF9;"
            "  color: #FFFFFF;"
            "}"
            "QMenu::item:disabled {"
            "  color: palette(mid);"
            "}"
            "QMenu::separator {"
            "  height: 1px;"
            "  background-color: palette(mid);"
            "  margin: 4px 8px;"
            "}"
        )
        bar.setStyleSheet(_MENU_QSS)
        self.setStyleSheet(self.styleSheet() + _MENU_QSS)

        layout: QVBoxLayout = self.layout()
        layout.insertWidget(0, bar)

        m_file = bar.addMenu("Файл")

        act_import = QAction("Импорт материалов…", self)
        act_import.setShortcut(QKeySequence("Ctrl+I"))
        act_import.triggered.connect(self._on_import_requested)
        m_file.addAction(act_import)

        m_file.addSeparator()

        act_sync = QAction("Синхронизация с сервером…", self)
        act_sync.setShortcut(QKeySequence("Ctrl+Shift+Y"))
        act_sync.setToolTip(
            "Открыть окно синхронизации: публикация, скачивание, "
            "дельта-синхронизация"
        )
        act_sync.triggered.connect(self._open_sync_window)
        m_file.addAction(act_sync)

        act_sync_record = QAction(
            "Синхронизировать выбранную запись…", self
        )
        act_sync_record.setShortcut(QKeySequence("Ctrl+Shift+S"))
        act_sync_record.setToolTip(
            "Опубликовать выбранную запись на сервер "
            "с выбором параметров (медиа, ссылка, удаление)"
        )
        act_sync_record.triggered.connect(self._sync_one_record)
        m_file.addAction(act_sync_record)

        act_sync_pull = QAction(
            "Подтянуть изменения с сервера", self
        )
        act_sync_pull.setToolTip(
            "Запросить /sync/changes и применить изменения "
            "локально"
        )
        act_sync_pull.triggered.connect(self._pull_changes)
        m_file.addAction(act_sync_pull)

        m_file.addSeparator()

        act_public_link = QAction(
            "Открыть ссылку на сервере…", self
        )
        act_public_link.setShortcut(QKeySequence("Ctrl+Shift+U"))
        act_public_link.setToolTip(
            "Открыть публичную HTML-страницу записи на сервере "
            "синхронизации"
        )
        act_public_link.triggered.connect(
            self._open_public_link_dialog
        )
        m_file.addAction(act_public_link)

        act_copy_public_link = QAction(
            "Скопировать ссылку на сервер", self
        )
        act_copy_public_link.setToolTip(
            "Скопировать публичную ссылку на запись в буфер обмена"
        )
        act_copy_public_link.triggered.connect(
            self._copy_public_link
        )
        m_file.addAction(act_copy_public_link)

        m_file.addSeparator()

        act_open_folder = QAction("Открыть папку записи", self)
        act_open_folder.setShortcut(QKeySequence("Ctrl+Shift+E"))
        act_open_folder.triggered.connect(self._open_folder)
        m_file.addAction(act_open_folder)

        act_open_video = QAction("Смотреть видео", self)
        act_open_video.setShortcut(QKeySequence("Ctrl+Shift+V"))
        act_open_video.setToolTip(
            "Открыть видео во встроенном плеере.\n"
            "Если файла нет локально, но он есть на сервере — "
            "будет предложено скачать его."
        )
        act_open_video.triggered.connect(self._open_video)
        m_file.addAction(act_open_video)

        act_open_audio = QAction("Прослушать аудио", self)
        act_open_audio.setShortcut(QKeySequence("Ctrl+Shift+A"))
        act_open_audio.setToolTip(
            "Открыть аудио во встроенном плеере.\n"
            "Если файла нет локально, но он есть на сервере — "
            "будет предложено скачать его."
        )
        act_open_audio.triggered.connect(self._open_audio)
        m_file.addAction(act_open_audio)

        act_open_transcript = QAction(
            "Просмотреть стенограмму", self
        )
        act_open_transcript.setShortcut(QKeySequence("Ctrl+Shift+T"))
        attach_tooltip(
            act_open_transcript, "sess_transcript_action"
        )
        act_open_transcript.triggered.connect(self._open_transcript)
        m_file.addAction(act_open_transcript)

        act_view_protocol = QAction("Просмотреть протокол", self)
        act_view_protocol.setShortcut(QKeySequence("Ctrl+Shift+R"))
        act_view_protocol.setToolTip(
            "Открыть прикреплённый протокол (.docx) во встроенном "
            "просмотрщике с поиском.\n\n"
            "Поиск файла выполняется в корне папки записи и в "
            "подпапке attachments."
        )
        act_view_protocol.triggered.connect(
            self._view_manual_protocol
        )
        m_file.addAction(act_view_protocol)

        m_file.addSeparator()

        act_delete = QAction("Удалить запись…", self)
        act_delete.setShortcut(QKeySequence("Ctrl+Delete"))
        act_delete.triggered.connect(self._delete_session)
        m_file.addAction(act_delete)

        m_file.addSeparator()

        act_refresh = QAction("Обновить список", self)
        act_refresh.setShortcut(QKeySequence("F5"))
        act_refresh.triggered.connect(self.refresh)
        m_file.addAction(act_refresh)

        act_close = QAction("Закрыть окно", self)
        act_close.setShortcut(QKeySequence("Ctrl+W"))
        act_close.triggered.connect(self.close)
        m_file.addAction(act_close)

        m_meta = bar.addMenu("Метаданные")

        act_edit_meta = QAction(
            "Изменить метаданные и перезапустить…", self
        )
        act_edit_meta.setShortcut(QKeySequence("Ctrl+E"))
        act_edit_meta.triggered.connect(
            self._edit_metadata_and_restart
        )
        m_meta.addAction(act_edit_meta)

        act_edit_tags = QAction("Изменить теги…", self)
        act_edit_tags.setShortcut(QKeySequence("Ctrl+T"))
        act_edit_tags.triggered.connect(self._edit_tags)
        m_meta.addAction(act_edit_tags)

        m_meta.addSeparator()

        act_toggle_sync_ready = QAction(
            "Переключить «готово к синхронизации»", self
        )
        act_toggle_sync_ready.setShortcut(QKeySequence("Ctrl+Shift+G"))
        act_toggle_sync_ready.triggered.connect(
            self._toggle_sync_ready
        )
        m_meta.addAction(act_toggle_sync_ready)

        m_meta.addSeparator()

        act_restart = QAction("Перезапустить обработку", self)
        act_restart.setShortcut(QKeySequence("Ctrl+R"))
        act_restart.triggered.connect(self._restart_processing)
        m_meta.addAction(act_restart)

        act_enqueue = QAction("Поставить в очередь", self)
        act_enqueue.triggered.connect(self._enqueue_current)
        m_meta.addAction(act_enqueue)

        m_meta.addSeparator()

        act_status_uploaded = QAction(
            "Пометить: «Сохранено»", self
        )
        act_status_uploaded.triggered.connect(
            lambda: self._change_status(STATUS_UPLOADED)
        )
        m_meta.addAction(act_status_uploaded)

        act_status_processed = QAction(
            "Пометить: «Обработан»", self
        )
        act_status_processed.triggered.connect(
            lambda: self._change_status(STATUS_PROCESSED)
        )
        m_meta.addAction(act_status_processed)

        act_status_error = QAction("Пометить: «Ошибка»", self)
        act_status_error.triggered.connect(
            lambda: self._change_status(STATUS_ERROR)
        )
        m_meta.addAction(act_status_error)

        m_protocol = bar.addMenu("Протокол")

        act_edit_protocol_md = QAction(
            "Создать/редактировать протокол (Markdown)…", self
        )
        act_edit_protocol_md.setShortcut(QKeySequence("Ctrl+M"))
        act_edit_protocol_md.triggered.connect(
            self._edit_manual_protocol_md
        )
        m_protocol.addAction(act_edit_protocol_md)

        act_export_protocol_docx = QAction(
            "Экспорт протокола в DOCX…", self
        )
        act_export_protocol_docx.setShortcut(
            QKeySequence("Ctrl+Shift+M")
        )
        act_export_protocol_docx.triggered.connect(
            self._export_protocol_docx
        )
        m_protocol.addAction(act_export_protocol_docx)

        m_protocol.addSeparator()

        act_view_protocol_menu = QAction(
            "Просмотреть прикреплённый протокол", self
        )
        act_view_protocol_menu.setToolTip(
            "Открыть протокол (.docx) во встроенном просмотрщике "
            "с поиском.\n\n"
            "Поиск файла выполняется в корне папки записи и в "
            "подпапке attachments."
        )
        act_view_protocol_menu.triggered.connect(
            self._view_manual_protocol
        )
        m_protocol.addAction(act_view_protocol_menu)

        act_attach_protocol = QAction(
            "Прикрепить файл протокола…", self
        )
        act_attach_protocol.triggered.connect(
            self._attach_manual_protocol
        )
        m_protocol.addAction(act_attach_protocol)

        act_open_protocol = QAction(
            "Открыть прикреплённый протокол (внешне)", self
        )
        act_open_protocol.triggered.connect(
            self._open_manual_protocol
        )
        m_protocol.addAction(act_open_protocol)

        m_summary = bar.addMenu("Summary")

        act_edit_summary = QAction(
            "Изменить summary (Markdown)…", self
        )
        act_edit_summary.setShortcut(QKeySequence("Ctrl+P"))
        act_edit_summary.triggered.connect(self._edit_summary_bb)
        m_summary.addAction(act_edit_summary)

        act_view_summary = QAction("Просмотр summary", self)
        act_view_summary.setShortcut(QKeySequence("Ctrl+Shift+P"))
        act_view_summary.triggered.connect(self._view_summary)
        m_summary.addAction(act_view_summary)

        act_export_summary = QAction("Экспорт summary…", self)
        act_export_summary.triggered.connect(self._export_summary)
        m_summary.addAction(act_export_summary)

        m_deepseek = bar.addMenu("DeepSeek")

        act_open_prompt = QAction("Открыть промпт DeepSeek", self)
        act_open_prompt.setShortcut(QKeySequence("Ctrl+D"))
        act_open_prompt.triggered.connect(self._open_deepseek_prompt)
        m_deepseek.addAction(act_open_prompt)

        act_export_prompt = QAction("Экспорт промпта…", self)
        act_export_prompt.setShortcut(QKeySequence("Ctrl+Shift+D"))
        act_export_prompt.triggered.connect(self._export_prompt)
        m_deepseek.addAction(act_export_prompt)

        m_deepseek.addSeparator()

        act_save_downloads = QAction(
            "Сохранить промпт в Загрузки", self
        )
        act_save_downloads.setShortcut(QKeySequence("Ctrl+Alt+D"))
        act_save_downloads.triggered.connect(
            self._save_prompt_to_downloads
        )
        m_deepseek.addAction(act_save_downloads)

        m_bitrix = bar.addMenu("Bitrix24")

        m_tasks = bar.addMenu("Поручения")

        act_tasks_editor = QAction(
            "Редактор поручений текущей записи…", self
        )
        act_tasks_editor.setShortcut(QKeySequence("Ctrl+Shift+K"))
        act_tasks_editor.setToolTip(
            "Открыть редактор поручений (action_items.json) для "
            "выбранной записи.\n\n"
            "В редакторе можно создавать, редактировать, удалять "
            "поручения, импортировать и экспортировать JSON."
        )
        act_tasks_editor.triggered.connect(self._open_tasks_editor)
        m_tasks.addAction(act_tasks_editor)

        act_view_tasks = QAction("Сводное окно «Поручения»…", self)
        act_view_tasks.setToolTip(
            "Открыть сводную таблицу всех поручений по всем записям.\n"
            "Горячая клавиша: Ctrl+Shift+L"
        )
        act_view_tasks.setShortcut(QKeySequence("Ctrl+Shift+L"))
        act_view_tasks.triggered.connect(self._open_tasks_window)
        m_tasks.addAction(act_view_tasks)

        act_send = QAction("Отправить в чат…", self)
        act_send.setShortcut(QKeySequence("Ctrl+B"))
        act_send.triggered.connect(self._send_to_bitrix)
        m_bitrix.addAction(act_send)

        act_send_protocol = QAction("Отправить протокол…", self)
        act_send_protocol.triggered.connect(
            lambda: self._send_to_bitrix(default="protocol")
        )
        m_bitrix.addAction(act_send_protocol)

        act_send_summary = QAction("Отправить summary…", self)
        act_send_summary.triggered.connect(
            lambda: self._send_to_bitrix(default="summary")
        )
        m_bitrix.addAction(act_send_summary)

        m_attach = bar.addMenu("Вложения")

        act_open_attachments = QAction(
            "Открыть папку вложений", self
        )
        act_open_attachments.triggered.connect(
            self._open_attachments
        )
        m_attach.addAction(act_open_attachments)

        act_add_attachment = QAction("Добавить вложение…", self)
        act_add_attachment.triggered.connect(
            self._add_attachment_to_session
        )
        m_attach.addAction(act_add_attachment)

        m_utils = bar.addMenu("Утилиты")

        act_stats = QAction(
            "Показать размер видеофайлов…", self
        )
        act_stats.triggered.connect(self._show_video_stats)
        m_utils.addAction(act_stats)

        m_utils.addSeparator()

        act_del_video = QAction(
            "Удалить видеофайлы (оставить только аудио)…", self
        )
        act_del_video.triggered.connect(self._delete_video_files)
        m_utils.addAction(act_del_video)

        m_help = bar.addMenu("Справка")

        act_shortcuts = QAction("Горячие клавиши…", self)
        act_shortcuts.triggered.connect(self._show_shortcuts)
        m_help.addAction(act_shortcuts)

        act_player_info = QAction("Статус встроенного плеера…", self)
        act_player_info.setToolTip(
            "Показать информацию о доступности встроенного плеера "
            "(Qt Multimedia)"
        )
        act_player_info.triggered.connect(self._show_player_info)
        m_help.addAction(act_player_info)

    def _on_import_requested(self) -> None:
        self.import_requested.emit()

    def _show_player_info(self) -> None:
        status = probe_media_support()
        if is_builtin_player_available():
            QMessageBox.information(
                self, "Встроенный плеер", status,
            )
        else:
            QMessageBox.warning(
                self, "Встроенный плеер",
                f"{status}\n\n"
                "Медиафайлы будут открываться системным "
                "приложением (xdg-open). Для работы встроенного "
                "плеера установите пакет PySide6-Addons и "
                "необходимые GStreamer-плагины:\n\n"
                "sudo apt install python3-pyside6.qmultimedia "
                "gstreamer1.0-plugins-good gstreamer1.0-plugins-bad "
                "gstreamer1.0-libav",
            )

    def _open_daily_digest(self) -> None:
        """Открывает диалог формирования «Свода за день»."""
        try:
            dlg = DailyDigestDialog(
                sessions_root=self.sessions_root,
                config_manager=self.config_manager,
                parent=self,
            )
            dlg.exec()
        except Exception as exc:
            log.exception(
                "Не удалось открыть диалог «Выборка»: %s", exc
            )
            QMessageBox.critical(
                self, "Выборка",
                f"Ошибка открытия диалога:\n{exc}",
            )

    def _regenerate_action_prompt(self) -> None:
        """Пересобирает action_items_prompt для выбранной записи."""
        r = self._selected_row()
        if not r:
            QMessageBox.warning(
                self, "Промпт поручений", "Выберите запись"
            )
            return

        session_dir = r.get("dir") or ""
        if not session_dir or not os.path.isdir(session_dir):
            QMessageBox.warning(
                self, "Промпт поручений",
                f"Папка записи не найдена:\n{session_dir}",
            )
            return

        # --- Формат и шаблон из конфига ---
        fmt = "docx"
        template = ""
        if self.config_manager is not None:
            try:
                scrum = (
                    self.config_manager.config
                    .get("scrum", {}) or {}
                )
                fmt = str(
                    scrum.get("export_format") or "docx"
                ).lower()
                template = str(
                    scrum.get("action_items_prompt_template")
                    or ""
                )
            except Exception as exc:
                log.warning(
                    "Не удалось прочитать настройки скрама: %s",
                    exc,
                )

        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("Обновление промпта поручений")
        msg_box.setIcon(QMessageBox.Icon.Question)
        msg_box.setTextFormat(Qt.TextFormat.RichText)
        msg_box.setText(
            f"Пересобрать <code>action_items_prompt</code> "
            f"на основе протокола записи:<br><br>"
            f"<b>{html.escape(r.get('name') or '')}</b><br>"
            f"<code>{html.escape(session_dir)}</code><br><br>"
            f"Существующий файл будет перезаписан.<br><br>"
            f"Продолжить?"
        )
        msg_box.setStandardButtons(
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No
        )
        msg_box.setDefaultButton(QMessageBox.StandardButton.Yes)

        reply = msg_box.exec()
        if reply != QMessageBox.StandardButton.Yes:
            return

        QGuiApplication.setOverrideCursor(
            Qt.CursorShape.WaitCursor
        )
        try:
            res = regenerate_action_items_prompt(
                session_dir=session_dir,
                fmt=fmt,
                template=template,
                session_name=r.get("name") or "",
                session_date=r.get("datetime") or "",
            )
        finally:
            QGuiApplication.restoreOverrideCursor()

        if res.get("ok"):
            QMessageBox.information(
                self, "Промпт поручений",
                f"Файл обновлён:\n{res['path']}\n\n"
                f"Источник протокола:\n"
                f"{res.get('protocol_path') or '—'}\n"
                f"Символов: {res.get('protocol_chars', 0)}",
            )
        else:
            QMessageBox.warning(
                self, "Промпт поручений",
                f"Не удалось сформировать файл:\n\n"
                f"{res.get('warning') or 'Неизвестная ошибка'}",
            )

    # ------------------------------------------------------------------
    # Легенда
    # ------------------------------------------------------------------
    def _build_legend(self) -> QWidget:
        box = QFrame()
        box.setFrameShape(QFrame.Shape.StyledPanel)
        box.setStyleSheet(
            "QFrame { background-color: palette(window); "
            "border: 1px solid palette(mid); border-radius: 6px; }"
        )
        layout = QHBoxLayout(box)
        layout.setContentsMargins(10, 4, 10, 4)
        layout.setSpacing(14)

        layout.addWidget(QLabel("<b>Статусы:</b>"))
        items = [
            (STATUS_UPLOADED, "видео сохранено"),
            (STATUS_TRANSCRIBING, "задача в очереди"),
            (STATUS_PROCESSED, "все шаги завершены"),
            (STATUS_ERROR, "ошибка обработки"),
            (STATUS_UNKNOWN, "состояние неизвестно"),
        ]
        for status, hint in items:
            layout.addWidget(self._make_legend_item(status, hint))
        layout.addStretch()
        return box

    @staticmethod
    def _make_legend_item(status: str, hint: str) -> QWidget:
        w = QWidget()
        w.setToolTip(hint)
        row = QHBoxLayout(w)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        swatch = QLabel()
        swatch.setFixedSize(12, 12)
        swatch.setStyleSheet(
            f"background-color: "
            f"{STATUS_COLORS.get(status, '#616161')}; "
            f"border-radius: 2px;"
        )
        row.addWidget(swatch)
        text = QLabel(status)
        row.addWidget(text)
        hint_lbl = QLabel("?")
        hint_lbl.setToolTip(hint)
        hint_lbl.setStyleSheet(
            "color: gray; font-weight: bold; padding-left: 2px;"
        )
        row.addWidget(hint_lbl)
        return w

    def _on_selection_changed(self) -> None:
        r = self._selected_row()
        if not r:
            self.selection_label.setText("")
            self.sync_btn.setEnabled(False)
            self.sync_ready_btn.setEnabled(False)
            self.play_video_btn.setEnabled(False)
            self.play_audio_btn.setEnabled(False)
            self.open_transcript_btn.setEnabled(False)
            self.open_protocol_btn.setEnabled(False)
            if hasattr(self, "public_link_btn"):
                self.public_link_btn.setEnabled(False)
            if hasattr(self, "copy_public_link_btn"):
                self.copy_public_link_btn.setEnabled(False)
            return

        parts = [f"<b>{r['name']}</b>"]
        if r.get("project"):
            parts.append(f"проект: {r['project']}")
        if r.get("tags"):
            parts.append(
                f"теги: {', '.join(r['tags'])}"
            )
        parts.append(f"статус: {r['status']}")
        if r.get("sync_ready"):
            parts.append("<span style='color:#2E7D32'>готово к синхр.</span>")
        if r.get("manual_protocol_path"):
            parts.append("протокол: прикреплён")
        if (r.get("summary_bb") or "").strip():
            parts.append("summary: есть")
        ai_total = int(r.get("action_items_total") or 0)
        ai_done = int(r.get("action_items_done") or 0)
        if ai_total > 0:
            parts.append(
                f"<span style='color:#1565C0'>"
                f"поручений: {ai_done}/{ai_total}</span>"
            )
        if r.get("has_transcript"):
            parts.append("стенограмма: есть")
        if r.get("published"):
            parts.append(
                f"на сервере: {r.get('record_id', '')[:8]}"
            )
        self.selection_label.setText(" | ".join(parts))

        self.sync_btn.setEnabled(bool(r.get("dir")))
        self.sync_ready_btn.setEnabled(bool(r.get("dir")))

        if r.get("sync_ready"):
            self.sync_ready_btn.setText("Снять готовность")
        else:
            self.sync_ready_btn.setText("Готово к синхронизации")

        has_local_video = bool(r.get("has_video"))
        has_local_audio = bool(r.get("has_audio"))
        is_published = bool(r.get("published"))

        if hasattr(self, "public_link_btn"):
            self.public_link_btn.setEnabled(is_published)
        if hasattr(self, "copy_public_link_btn"):
            self.copy_public_link_btn.setEnabled(is_published)

        self.play_video_btn.setEnabled(
            has_local_video or is_published
        )
        self.play_audio_btn.setEnabled(
            has_local_audio or is_published
        )

        self.open_transcript_btn.setEnabled(
            bool(r.get("has_transcript"))
        )

        protocol_path = self._find_manual_protocol_path(r)
        self.open_protocol_btn.setEnabled(bool(protocol_path))

    # ------------------------------------------------------------------
    # Обновление
    # ------------------------------------------------------------------
    def _build_tasks_index(self) -> Dict[str, Dict[str, Any]]:
        tasks_index: Dict[str, Dict[str, Any]] = {}
        try:
            for t in self.task_queue.get_queue():
                vp = t.get("video_path", "")
                if vp:
                    tasks_index[os.path.abspath(vp)] = t
        except Exception as exc:
            log.warning("Не удалось получить очередь: %s", exc)
        return tasks_index

    def refresh(self) -> None:
        if self._thread is not None and self._thread.isRunning():
            return

        self.refresh_indicator.setText("Сканирование…")
        self.refresh_btn.setEnabled(False)

        tasks_index = self._build_tasks_index()
        self._thread = SessionsScanThread(
            self.sessions_root, tasks_index, parent=self
        )
        self._thread.finished_ok.connect(self._on_scan_finished)
        self._thread.failed.connect(self._on_scan_failed)
        self._thread.finished.connect(self._on_scan_thread_done)
        self._thread.start()

    def _on_scan_finished(self, rows: list) -> None:
        # Сохраняем полный список — фильтры работают по нему.
        self._all_rows = list(rows or [])

        # Обновляем список проектов в фильтре.
        self._refresh_project_filter()

        # Применяем фильтры.
        self._apply_filters()

    def _on_scan_failed(self, error: str) -> None:
        log.error("Сканирование сессий провалено: %s", error)
        QMessageBox.warning(
            self, "Записи", f"Ошибка сканирования:\n{error}"
        )

    def _on_scan_thread_done(self) -> None:
        self.refresh_indicator.setText("")
        self.refresh_btn.setEnabled(True)
        self._thread = None

    def _update_summary(self) -> None:
        rows = self._rows
        total = len(rows)
        processed = sum(
            1 for r in rows if r["status"] == STATUS_PROCESSED
        )
        uploaded = sum(
            1 for r in rows if r["status"] == STATUS_UPLOADED
        )
        in_progress = sum(
            1 for r in rows if r["status"] == STATUS_TRANSCRIBING
        )
        errors = sum(
            1 for r in rows if r["status"] == STATUS_ERROR
        )
        published = sum(1 for r in rows if r.get("published"))
        ready_count = sum(
            1 for r in rows if r.get("sync_ready") and not r.get("published")
        )

        # Показываем итоги по отфильтрованному списку.
        all_total = len(self._all_rows)
        if all_total != total:
            total_text = f"Всего: {total} из {all_total}"
        else:
            total_text = f"Всего: {total}"

        self.summary_label.setText(
            f"{total_text} | Обработан: {processed} | "
            f"Сохранено: {uploaded} | В обработке: {in_progress} | "
            f"Ошибок: {errors} | На сервере: {published} | "
            f"Готовы к синхр.: {ready_count}"
        )
        self._on_selection_changed()

    def _render_rows(self) -> None:
        self.table.setRowCount(0)
        for r in self._rows:
            row = self.table.rowCount()
            self.table.insertRow(row)

            self.table.setItem(
                row, 0, QTableWidgetItem(r["datetime"])
            )
            self.table.setItem(
                row, 1, QTableWidgetItem(r["name"])
            )
            self.table.setItem(
                row, 2, QTableWidgetItem(r["project"])
            )

            tags = r.get("tags") or []
            tags_text = ", ".join(tags) if tags else "—"
            tags_item = QTableWidgetItem(tags_text)
            if tags:
                tags_item.setToolTip(
                    "Теги записи: " + ", ".join(tags)
                )

                # Цвет ячейки берём из первого тега,
                # у которого задан color в справочнике.
                tag_color = self._resolve_first_tag_color(tags)
                if tag_color is not None:
                    qcolor = QColor(tag_color)
                    if qcolor.isValid():
                        tags_item.setBackground(qcolor)
                        tags_item.setForeground(
                            QColor("#000000")
                        )

            self.table.setItem(row, 3, tags_item)

            status_item = QTableWidgetItem(r["status"])
            if r["status"] == STATUS_ERROR:
                status_item.setForeground(Qt.GlobalColor.red)
            elif r["status"] == STATUS_PROCESSED:
                status_item.setForeground(Qt.GlobalColor.darkGreen)
            elif r["status"] == STATUS_TRANSCRIBING:
                status_item.setForeground(Qt.GlobalColor.darkYellow)
            self.table.setItem(row, 4, status_item)

            src_map = {
                "record": "Запись",
                "upload": "Загрузка",
                "import": "Импорт",
            }
            src = src_map.get(r["source"], r["source"] or "—")
            self.table.setItem(row, 5, QTableWidgetItem(src))
            self.table.setItem(
                row, 6,
                QTableWidgetItem("да" if r["is_scrum"] else "—"),
            )

            att_count = len(r.get("attachments", []) or [])
            self.table.setItem(
                row, 7,
                QTableWidgetItem(
                    str(att_count) if att_count else "—"
                ),
            )

            # --- Поручения: «выполнено/всего» + разбивка по статусам ---
            ai_total = int(r.get("action_items_total") or 0)
            ai_by_status: Dict[str, int] = (
                r.get("action_items_by_status") or {}
            )

            if ai_total > 0:
                ai_done = int(ai_by_status.get("done", 0))
                ai_cancelled = int(ai_by_status.get("cancelled", 0))

                ai_item = QTableWidgetItem(f"{ai_done}/{ai_total}")
                # Подсветка: если все выполнены — зелёный, если есть
                # отменённые — учитываем их отдельно.
                if ai_done == ai_total:
                    ai_item.setForeground(Qt.GlobalColor.darkGreen)
                elif ai_done + ai_cancelled == ai_total:
                    # Все закрыты (выполнены или отменены).
                    ai_item.setForeground(Qt.GlobalColor.darkGray)
                elif ai_done > 0:
                    ai_item.setForeground(Qt.GlobalColor.darkBlue)
                else:
                    ai_item.setForeground(Qt.GlobalColor.gray)

                ai_item.setTextAlignment(
                    Qt.AlignmentFlag.AlignRight
                    | Qt.AlignmentFlag.AlignVCenter
                )

                # --- Подробный tooltip с разбивкой по статусам ---
                status_order = (
                    ("created",     "Создан"),
                    ("in_progress", "В работе"),
                    ("waiting",     "Ожидание"),
                    ("done",        "Выполнен"),
                    ("cancelled",   "Отмена"),
                )
                tooltip_lines = [
                    f"Поручений к записи: <b>{ai_total}</b>",
                    f"Выполнено: <b>{ai_done}</b>",
                    "",
                    "Разбивка по статусам:",
                ]
                for key, label in status_order:
                    cnt = int(ai_by_status.get(key, 0))
                    if cnt > 0:
                        tooltip_lines.append(f"  • {label}: {cnt}")
                tooltip_lines.append("")
                tooltip_lines.append(
                    "Открыть редактор: «Поручения» → "
                    "«Редактор поручений текущей записи…» "
                    "(Ctrl+Shift+K)."
                )

                # QTableWidgetItem.setToolTip по умолчанию трактует
                # строку как plain text; для <br>/<b> нужно явно
                # обернуть в HTML.
                ai_item.setToolTip(
                    "<html><body style='white-space:pre-wrap;'>"
                    + "<br>".join(tooltip_lines)
                    + "</body></html>"
                )
            else:
                ai_item = QTableWidgetItem("—")
                ai_item.setForeground(Qt.GlobalColor.gray)
                ai_item.setToolTip(
                    "У этой записи нет поручений "
                    "(action_items.json отсутствует или пуст)."
                )

            self.table.setItem(row, 8, ai_item)

            # --- Summary: только наличие (да / —) ---
            summary_bb = (r.get("summary_bb") or "").strip()
            if summary_bb:
                summary_item = QTableWidgetItem("да")
                summary_item.setForeground(
                    Qt.GlobalColor.darkGreen
                )
                short = summary_bb.replace("\n", " ")
                if len(short) > 200:
                    short = short[:197] + "…"
                summary_item.setToolTip(
                    "Краткое описание записи "
                    "(первые 200 символов):\n\n" + short
                )
            else:
                summary_item = QTableWidgetItem("—")
                summary_item.setForeground(Qt.GlobalColor.gray)
                summary_item.setToolTip(
                    "У этой записи нет краткого описания."
                )
            self.table.setItem(row, 9, summary_item)

            # --- Колонка «Синхр.» ---
            published = bool(r.get("published"))
            sync_ready = bool(r.get("sync_ready"))

            # Варианты отображения:
            #   published + sync_ready → "да"    (зелёный, жирный)
            #   published, нет sync_ready → "да?" (зелёный, не жирный,
            #                                     с подсказкой-предупреждением)
            #   не published + sync_ready → "готово" (жёлтый, жирный)
            #   не published, нет sync_ready → "черновик" (серый)
            #
            # ВАЖНО: жирность задаём ТОЛЬКО явно, создавая новый QFont
            # с нуля. Нельзя наследовать шрифт от QTableWidgetItem —
            # у вновь созданного элемента он невалидный, а при
            # копировании через QFont(item.font()) можно случайно
            # подхватить bold от предыдущей ячейки.

            if published and sync_ready:
                sync_text = "да"
                sync_item = QTableWidgetItem(sync_text)
                sync_item.setForeground(Qt.GlobalColor.darkGreen)
                sync_item.setToolTip(
                    f"Опубликовано и помечено как «готово "
                    f"к синхронизации».\n\n"
                    f"Record ID: {r.get('record_id', '')}"
                )
                #bold_font = QFont()
                #bold_font.setBold(True)
                #sync_item.setFont(bold_font)

            elif published and not sync_ready:
                # Опубликовано, но признака готовности нет.
                sync_text = "да?"
                sync_item = QTableWidgetItem(sync_text)
                sync_item.setForeground(Qt.GlobalColor.darkYellow)
                sync_item.setToolTip(
                    f"Опубликовано, но <b>признак «готово "
                    f"к синхронизации» не выставлен</b> "
                    f"(sync_ready=false).\n\n"
                    f"Запись считается черновиком:\n"
                    f"  • автопубликация повторно не запускается;\n"
                    f"  • фоновый pull игнорирует эту запись;\n"
                    f"  • локальные артефакты не удаляются.\n\n"
                    f"Поставьте галочку «Готово к синхронизации» "
                    f"в карточке метаданных или в окне «Записи», "
                    f"чтобы разрешить фоновую синхронизацию.\n\n"
                    f"Record ID: {r.get('record_id', '')}"
                )
                # Внимание: НЕ жирный — чтобы визуально
                # отличалось от «полностью готовых».

            elif not published and sync_ready:
                sync_text = "готово"
                sync_item = QTableWidgetItem(sync_text)
                sync_item.setForeground(Qt.GlobalColor.darkYellow)
                sync_item.setToolTip(
                    "Запись помечена как «готова к синхронизации». "
                    "Можно публиковать на сервер, фоновый pull "
                    "будет её учитывать."
                )
                #bold_font = QFont()
                #bold_font.setBold(True)
                #sync_item.setFont(bold_font)

            else:
                # Не опубликовано, нет признака готовности.
                sync_text = "черновик"
                sync_item = QTableWidgetItem(sync_text)
                sync_item.setForeground(Qt.GlobalColor.gray)
                sync_item.setToolTip(
                    "Запись — черновик (sync_ready=false). "
                    "Автопубликация не запускается, фоновый "
                    "pull игнорирует, локальные артефакты "
                    "не удаляются."
                )
                # Не жирный.

            self.table.setItem(row, 10, sync_item)

            self.table.setItem(
                row, 11,
                QTableWidgetItem(r["task_id"] or "—"),
            )
            self.table.setItem(row, 12, QTableWidgetItem(r["dir"]))

    # ------------------------------------------------------------------
    # Готовность к синхронизации
    # ------------------------------------------------------------------
    def _toggle_sync_ready(self) -> None:
        """
        Переключает флаг sync_ready в session.json для выбранной
        записи. Показывается диалог подтверждения.
        """
        r = self._selected_row()
        if not r:
            QMessageBox.warning(self, "Записи", "Выберите запись")
            return

        session_json = os.path.join(r["dir"], "session.json")
        meta = read_json_file(session_json) or {}
        current = bool(meta.get("sync_ready", False))
        new_value = not current

        if new_value:
            reply = QMessageBox.question(
                self, "Готово к синхронизации",
                f"Отметить запись «{r['name']}» как готовую "
                f"к синхронизации?\n\n"
                f"После этого:\n"
                f"  • автопубликация может отправить запись "
                f"на сервер;\n"
                f"  • фоновый pull может перезаписывать локальные "
                f"файлы серверной версией;\n"
                f"  • локальные артефакты, которых нет на сервере, "
                f"могут быть удалены.\n\n"
                f"Убедитесь, что протокол, summary и вложения "
                f"на месте.",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        meta["sync_ready"] = new_value
        meta["sync_ready_at"] = (
            datetime.now().isoformat() if new_value else ""
        )

        if not self._write_json(session_json, meta):
            QMessageBox.critical(
                self, "Синхронизация",
                "Не удалось сохранить session.json",
            )
            return

        log.info(
            "Запись «%s»: sync_ready = %s",
            r["name"], new_value,
        )

        self.refresh()

        # --- Если запись опубликована — уведомляем сервер ---
        if r.get("published") and r.get("record_id"):
            self._patch_sync_ready_on_server(
                r["record_id"], new_value,
            )

        # --- Авто-публикация при установке «Готово» ---
        if new_value:
            r["sync_ready"] = True
            self._maybe_publish_after_sync_ready(r, was_ready=current)
        else:
            self._notify_sync_ready(r, False)

    def _notify_sync_ready(
        self, r: Dict[str, Any], ready: bool,
    ) -> None:
        """Показывает уведомление после смены флага."""
        if ready:
            QMessageBox.information(
                self, "Синхронизация",
                f"Запись «{r['name']}» помечена как «готова "
                f"к синхронизации».\n\n"
                f"Теперь её можно публиковать на сервер "
                f"(вручную или автоматически) и разрешить "
                f"фоновую синхронизацию.",
            )
        else:
            QMessageBox.information(
                self, "Синхронизация",
                f"Запись «{r['name']}» снова стала черновиком.\n\n"
                f"Автопубликация не запускается, фоновый pull "
                f"не перезаписывает локальные файлы, локальные "
                f"артефакты не удаляются.",
            )

    def _patch_sync_ready_on_server(
        self, record_id: str, sync_ready: bool,
    ) -> None:
        """
        Уведомляет сервер о смене флага sync_ready.

        Выполняется в фоне, не блокирует UI. Если запись
        не опубликована — метод не вызывается.
        """
        if self.config_manager is None:
            return

        try:
            cfg = self.config_manager.get_sync_settings()
        except Exception as exc:
            log.warning(
                "Не удалось прочитать sync-настройки: %s", exc
            )
            return

        if not (cfg.get("enabled") and cfg.get("base_url")
                and cfg.get("api_key")):
            return

        base_url = cfg["base_url"]
        api_key = cfg["api_key"]

        async def _patch() -> None:
            try:
                async with ScrecClient(
                    base_url=base_url,
                    api_key=api_key,
                    connect_timeout=float(
                        cfg.get("connect_timeout", 15)
                    ),
                    read_timeout=float(
                        cfg.get("read_timeout", 120)
                    ),
                ) as client:
                    await client.patch_record(
                        record_id,
                        {"sync_ready": bool(sync_ready)},
                    )
                log.info(
                    "Сервер уведомлён: record=%s, sync_ready=%s",
                    record_id, sync_ready,
                )
            except ScrecError as exc:
                log.warning(
                    "Не удалось уведомить сервер о sync_ready "
                    "(record=%s): %s", record_id, exc,
                )

        import threading

        def _runner() -> None:
            try:
                asyncio.run(_patch())
            except Exception as exc:
                log.warning(
                    "Ошибка в _patch_sync_ready_on_server: %s", exc
                )

        t = threading.Thread(target=_runner, daemon=True)
        t.start()

    # ------------------------------------------------------------------
    # Просмотр / прослушивание медиа
    # ------------------------------------------------------------------
    def _open_video(self) -> None:
        self._open_media_kind("video")

    def _open_audio(self) -> None:
        self._open_media_kind("audio")

    def _open_media_kind(self, kind: str) -> None:
        r = self._selected_row()
        if not r:
            QMessageBox.warning(self, "Записи", "Выберите запись")
            return

        local_path = ""
        if kind == "video":
            local_path = r.get("video_path") or ""
        elif kind == "audio":
            local_path = r.get("audio_path") or ""

        if local_path and os.path.isfile(local_path):
            self._play_media(local_path, r, kind)
            return

        if not r.get("published") or not r.get("record_id"):
            QMessageBox.information(
                self, "Записи",
                f"Локальный файл {'видео' if kind == 'video' else 'аудио'}"
                f" не найден, и запись не опубликована на сервере.\n\n"
                f"Опубликуйте запись через «Файл → Синхронизировать "
                f"выбранную запись…» — и, если включена передача медиа "
                f"на сервер, файл можно будет скачать.",
            )
            return

        auto_download = bool(
            self._app_cfg.get("media_auto_download_from_server", True)
        )

        if not auto_download:
            reply = QMessageBox.question(
                self, "Записи",
                f"Локального файла нет, но запись опубликована "
                f"на сервере.\n\n"
                f"Скачать {'видео' if kind == 'video' else 'аудио'} "
                f"с сервера?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        else:
            log.info(
                "Записи: авто-скачивание медиа (%s) для %s",
                kind, r["name"],
            )

        self._download_and_play_media(r, kind)

    def _download_and_play_media(
        self, row: Dict[str, Any], kind: str,
    ) -> None:
        if self.config_manager is None:
            QMessageBox.warning(
                self, "Записи",
                "Нет доступа к настройкам (ConfigManager).",
            )
            return

        if self._media_worker is not None and self._media_worker.isRunning():
            QMessageBox.information(
                self, "Записи",
                "Скачивание медиа уже выполняется. "
                "Дождитесь завершения.",
            )
            return

        cfg = self.config_manager.get_sync_settings()
        if not (cfg.get("enabled") and cfg.get("base_url")
                and cfg.get("api_key")):
            QMessageBox.warning(
                self, "Записи",
                "Синхронизация не настроена.\n\n"
                "Откройте Настройки → Синхронизация.",
            )
            return

        manager = SyncManager(
            sessions_root=self.sessions_root,
            sync_settings=cfg,
            config_manager=self.config_manager,
        )

        self._pending_media_request = {
            "row": row,
            "kind": kind,
        }

        self._media_progress_dlg = QProgressDialog(
            f"Скачивание медиа: {row['name']}…",
            "Отмена",
            0, 0, self,
        )
        self._media_progress_dlg.setWindowTitle("Скачивание медиа")
        self._media_progress_dlg.setWindowModality(
            Qt.WindowModality.WindowModal
        )
        self._media_progress_dlg.setMinimumDuration(0)
        self._media_progress_dlg.setCancelButton(None)
        self._media_progress_dlg.show()

        self._media_worker = _MediaDownloadWorker(
            manager=manager,
            session_dir=row["dir"],
            parent=self,
        )
        self._media_worker.progress.connect(self._on_media_download_progress)
        self._media_worker.finished_ok.connect(
            self._on_media_download_finished
        )
        self._media_worker.failed.connect(self._on_media_download_failed)
        self._media_worker.finished.connect(
            self._on_media_worker_done
        )
        self._media_worker.start()

    def _on_media_download_progress(self, message: str) -> None:
        if self._media_progress_dlg is not None:
            self._media_progress_dlg.setLabelText(message)

    def _on_media_download_finished(self, result: Dict[str, Any]) -> None:
        if self._media_progress_dlg is not None:
            self._media_progress_dlg.close()
            self._media_progress_dlg = None

        pending = self._pending_media_request
        self._pending_media_request = None

        if not pending:
            return

        row = pending["row"]
        kind = pending["kind"]

        downloaded = result.get("downloaded") or []
        missing = result.get("missing") or []

        target_path = ""
        for item in downloaded:
            item_kind = str(item.get("kind") or "")
            if item_kind == kind and not item.get("skipped"):
                target_path = str(item.get("local_path") or "")
                break
            if item_kind == kind and item.get("skipped"):
                target_path = str(item.get("local_path") or "")

        if not target_path:
            if kind == "video":
                for ext in _VIDEO_EXTS:
                    candidate = os.path.join(
                        row["dir"], f"video{ext}"
                    )
                    if os.path.isfile(candidate):
                        target_path = candidate
                        break
            elif kind == "audio":
                for ext in _AUDIO_EXTS:
                    candidate = os.path.join(
                        row["dir"], f"video{ext}"
                    )
                    if os.path.isfile(candidate):
                        target_path = candidate
                        break

        if not target_path or not os.path.isfile(target_path):
            missing_kind = next(
                (m for m in missing if str(m.get("kind")) == kind),
                None,
            )
            if missing_kind is not None:
                reason = missing_kind.get("reason") or ""
                QMessageBox.warning(
                    self, "Записи",
                    f"Не удалось получить медиа с сервера.\n\n"
                    f"Возможные причины:\n"
                    f"  • медиа не было передано на сервер "
                    f"(включите «Передавать видео и аудио на сервер» "
                    f"в Настройки → Синхронизация);\n"
                    f"  • файл удалён с сервера.\n\n"
                    + (f"Дополнительно: {reason}" if reason else ""),
                )
            else:
                QMessageBox.warning(
                    self, "Записи",
                    "Не удалось найти скачанный медиафайл.\n\n"
                    "Возможно, на сервере нет видео/аудио для этой "
                    "записи — включите передачу медиа в "
                    "Настройки → Синхронизация и синхронизируйте "
                    "запись заново.",
                )
            self.refresh()
            return

        self._play_media(target_path, row, kind)
        self.refresh()

    def _on_media_download_failed(self, error: str) -> None:
        if self._media_progress_dlg is not None:
            self._media_progress_dlg.close()
            self._media_progress_dlg = None
        self._pending_media_request = None

        log.error("Скачивание медиа не удалось: %s", error)
        QMessageBox.critical(
            self, "Записи",
            f"Не удалось скачать медиа с сервера:\n\n{error}",
        )

    def _on_media_worker_done(self) -> None:
        self._media_worker = None

    def _play_media(
        self,
        file_path: str,
        row: Dict[str, Any],
        kind: str,
    ) -> None:
        if not file_path or not os.path.isfile(file_path):
            QMessageBox.warning(
                self, "Записи",
                f"Файл не найден:\n{file_path or '(путь не задан)'}",
            )
            return

        prefer_builtin = bool(
            self._app_cfg.get("media_prefer_builtin_player", True)
        )
        win_w = int(
            self._app_cfg.get("media_player_window_width", 960)
        )
        win_h = int(
            self._app_cfg.get("media_player_window_height", 640)
        )

        title = f"{row.get('name') or 'Запись'} — "
        title += "видео" if kind == "video" else "аудио"

        log.info(
            "Воспроизведение медиа (%s): %s (builtin=%s)",
            kind, file_path, prefer_builtin,
        )

        used_builtin = open_media(
            file_path,
            title=title,
            prefer_builtin=prefer_builtin,
            window_width=win_w,
            window_height=win_h,
            parent=self,
        )

        if not used_builtin and prefer_builtin:
            QMessageBox.information(
                self, "Записи",
                "Встроенный плеер недоступен — файл открыт "
                "системным приложением.\n\n"
                "Установите PySide6-Addons и GStreamer-плагины "
                "для встроенного воспроизведения.",
            )

    # ------------------------------------------------------------------
    # Просмотр стенограммы
    # ------------------------------------------------------------------
    def _open_transcript(self) -> None:
        r = self._selected_row()
        if not r:
            QMessageBox.warning(self, "Записи", "Выберите запись")
            return

        session_dir = r.get("dir") or ""
        transcript_path = ""

        root_path = os.path.join(session_dir, "video.txt")
        if os.path.isfile(root_path):
            transcript_path = root_path

        if not transcript_path:
            attachments_path = os.path.join(
                session_dir, "attachments", "video.txt"
            )
            if os.path.isfile(attachments_path):
                transcript_path = attachments_path

        if not transcript_path:
            QMessageBox.information(
                self, "Записи",
                "У этой записи нет стенограммы (video.txt) ни в "
                "корне папки, ни в папке attachments.\n\n"
                "Стенограмма создаётся при обработке, если задан "
                "URL сервера транскрибации "
                "(Настройки → Транскрибация).",
            )
            return

        try:
            size_bytes = os.path.getsize(transcript_path)
        except OSError:
            size_bytes = 0

        if size_bytes > 20 * 1024 * 1024:
            reply = QMessageBox.question(
                self, "Стенограмма",
                f"Файл стенограммы очень большой: "
                f"{size_bytes / 1024 / 1024:.1f} МБ.\n\n"
                f"Открытие может занять время и замедлить "
                f"интерфейс.\n\n"
                f"Открыть всё равно?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        try:
            with open(
                transcript_path, "r",
                encoding="utf-8", errors="replace",
            ) as f:
                text = f.read()
        except Exception as exc:
            log.exception(
                "Не удалось прочитать стенограмму %s: %s",
                transcript_path, exc,
            )
            QMessageBox.critical(
                self, "Записи",
                f"Не удалось прочитать стенограмму:\n{exc}",
            )
            return

        source_label = (
            f"{os.path.basename(transcript_path)} "
            f"({size_bytes / 1024:.1f} КБ)"
        )

        dlg = TextViewerDialog(
            text=text,
            title=f"Стенограмма — {r['name']}",
            parent=self,
            source_label=source_label,
            default_save_name="video.txt",
        )
        dlg.exec()

        log.info(
            "Записи: просмотр стенограммы для «%s» (%d символов, файл=%s)",
            r["name"], len(text), transcript_path,
        )

    # ------------------------------------------------------------------
    # Просмотр протокола
    # ------------------------------------------------------------------
    _PROTOCOL_CANDIDATES = (
        "manual_protocol.docx",
        "manual_protocol.md",
        "manual_protocol.txt",
        "manual_protocol.pdf",
        "protocol.docx",
        "protocol.md",
        "protocol.txt",
        "protocol.pdf",
    )

    @classmethod
    def _find_manual_protocol_path(cls, r: Dict[str, Any]) -> str:
        session_dir = r.get("dir") or ""
        if not session_dir:
            return ""

        meta_path = r.get("manual_protocol_path") or ""
        if meta_path and os.path.isfile(meta_path):
            return meta_path

        for name in cls._PROTOCOL_CANDIDATES:
            candidate = os.path.join(session_dir, name)
            if os.path.isfile(candidate):
                return candidate

        att_dir = os.path.join(session_dir, "attachments")
        if os.path.isdir(att_dir):
            for name in cls._PROTOCOL_CANDIDATES:
                candidate = os.path.join(att_dir, name)
                if os.path.isfile(candidate):
                    return candidate

        return ""

    def _resolve_first_tag_color(
        self, tag_names: List[str],
    ) -> Optional[str]:
        if not tag_names or self.config_manager is None:
            return None

        try:
            tag_colors = {
                t["name"]: (t.get("color") or "").strip()
                for t in self.config_manager.get_tags()
            }
        except Exception as exc:
            log.warning(
                "Не удалось прочитать цвета тегов: %s", exc
            )
            return None

        for name in tag_names:
            color = tag_colors.get(name) or ""
            if color:
                return color

        return None

    def _view_manual_protocol(self) -> None:
        r = self._selected_row()
        if not r:
            QMessageBox.warning(self, "Записи", "Выберите запись")
            return

        protocol_path = self._find_manual_protocol_path(r)
        if not protocol_path:
            QMessageBox.information(
                self, "Записи",
                "У этой записи нет протокола (.docx) ни по пути из "
                "session.json, ни в корне папки, ни в папке "
                "attachments.\n\n"
                "Протокол можно:\n"
                "  • сформировать автоматически при обработке "
                "(если включена суммаризация);\n"
                "  • создать вручную: «Протокол → "
                "Создать/редактировать протокол (Markdown)…»;\n"
                "  • прикрепить готовый файл: «Протокол → "
                "Прикрепить файл протокола…».",
            )
            return

        try:
            size_bytes = os.path.getsize(protocol_path)
        except OSError:
            size_bytes = 0

        log.info(
            "Записи: просмотр протокола для «%s» (%s, %.1f КБ)",
            r["name"], protocol_path, size_bytes / 1024,
        )

        dlg = DocxViewerDialog(
            docx_path=protocol_path,
            title=f"Протокол — {r['name']}",
            parent=self,
        )
        dlg.exec()

    # ------------------------------------------------------------------
    # Синхронизация одной записи
    # ------------------------------------------------------------------
    def _sync_one_record(self) -> None:
        r = self._selected_row()
        if not r:
            QMessageBox.warning(self, "Записи", "Выберите запись")
            return
        if self.config_manager is None:
            QMessageBox.warning(
                self, "Синхронизация",
                "Нет доступа к настройкам (ConfigManager)."
            )
            return

        cfg = self.config_manager.get_sync_settings()
        if not (cfg.get("enabled") and cfg.get("base_url")
                and cfg.get("api_key")):
            QMessageBox.warning(
                self, "Синхронизация",
                "Синхронизация не настроена.\n\n"
                "Откройте Настройки → Синхронизация, "
                "укажите Base URL и API-ключ.",
            )
            return

        dlg = SyncOneRecordDialog(
            row=r,
            sync_settings=cfg,
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        opts = dlg.result_data()
        manager = SyncManager(
            sessions_root=self.sessions_root,
            sync_settings=cfg,
            config_manager=self.config_manager,
        )

        self._sync_progress_dlg = QProgressDialog(
            f"Синхронизация: {r['name']}…",
            "Отмена",
            0, 0, self,
        )
        self._sync_progress_dlg.setWindowTitle("Синхронизация")
        self._sync_progress_dlg.setWindowModality(
            Qt.WindowModality.WindowModal
        )
        self._sync_progress_dlg.setMinimumDuration(0)
        self._sync_progress_dlg.setCancelButton(None)
        self._sync_progress_dlg.show()

        self._sync_worker = _OneSyncWorker(
            manager=manager,
            session_dir=r["dir"],
            include_media=bool(opts.get("include_media")),
            send_video_link=bool(opts.get("send_video_link")),
            delete_media_after_upload=bool(
                opts.get("delete_media_after_upload")
            ),
            parent=self,
        )
        self._sync_worker.progress.connect(self._on_sync_progress)
        self._sync_worker.finished_ok.connect(self._on_sync_finished)
        self._sync_worker.failed.connect(self._on_sync_failed)
        self._sync_worker.finished.connect(
            self._on_sync_worker_done
        )
        self._sync_worker.start()

    # ------------------------------------------------------------------
    # Авто-публикация при установке «Готово к синхронизации»
    # ------------------------------------------------------------------
    def _publish_after_ready(self, r: Dict[str, Any]) -> None:
        if not self._app_cfg.get("sync_publish_on_ready", True):
            log.info(
                "Авто-публикация при отметке «Готово» выключена "
                "в настройках — запись %s останется локальной "
                "до ручной синхронизации", r["name"],
            )
            return

        if self.config_manager is None:
            return

        try:
            cfg = self.config_manager.get_sync_settings()
        except Exception as exc:
            log.warning(
                "Не удалось прочитать sync-настройки: %s", exc
            )
            return

        if not (cfg.get("enabled") and cfg.get("base_url")
                and cfg.get("api_key")):
            log.info(
                "Авто-публикация пропущена: синхронизация не "
                "настроена (запись %s)", r["name"],
            )
            return

        if (self._sync_worker is not None
                and self._sync_worker.isRunning()):
            log.info(
                "Авто-публикация отложена: уже выполняется "
                "синхронизация другой записи"
            )
            self._notify_ready_no_publish(r, reason="busy")
            return

        manager = SyncManager(
            sessions_root=self.sessions_root,
            sync_settings=cfg,
            config_manager=self.config_manager,
        )

        include_media = bool(
            cfg.get("send_media_to_server", False)
        )
        send_video_link = bool(cfg.get("send_video_link", True))
        delete_after = bool(
            cfg.get(
                "delete_local_media_after_media_upload", False
            )
        )

        log.info(
            "Авто-публикация записи «%s» после отметки «Готово» "
            "(медиа=%s, ссылка=%s, удалять после загрузки=%s)",
            r["name"], include_media, send_video_link, delete_after,
        )

        self._sync_progress_dlg = QProgressDialog(
            f"Авто-публикация: {r['name']}…",
            "Отмена",
            0, 0, self,
        )
        self._sync_progress_dlg.setWindowTitle(
            "Авто-публикация на сервер"
        )
        self._sync_progress_dlg.setWindowModality(
            Qt.WindowModality.WindowModal
        )
        self._sync_progress_dlg.setMinimumDuration(0)
        self._sync_progress_dlg.setCancelButton(None)
        self._sync_progress_dlg.show()

        self._sync_worker = _OneSyncWorker(
            manager=manager,
            session_dir=r["dir"],
            include_media=include_media,
            send_video_link=send_video_link,
            delete_media_after_upload=delete_after,
            parent=self,
        )
        self._sync_worker.progress.connect(self._on_sync_progress)
        self._sync_worker.finished_ok.connect(
            self._on_sync_finished
        )
        self._sync_worker.failed.connect(self._on_sync_failed)
        self._sync_worker.finished.connect(
            self._on_sync_worker_done
        )
        self._sync_worker.start()

    def _maybe_publish_after_sync_ready(
        self,
        row: Dict[str, Any],
        was_ready: bool,
    ) -> None:
        if was_ready or not row.get("sync_ready"):
            return
        if not self._app_cfg.get("sync_publish_on_ready", True):
            return
        self._publish_after_ready(row)

    def _notify_ready_no_publish(
        self, r: Dict[str, Any], reason: str = "",
    ) -> None:
        if reason == "busy":
            QMessageBox.information(
                self, "Готово к синхронизации",
                f"Запись «{r['name']}» помечена как «готова к "
                f"синхронизации».\n\n"
                f"Авто-публикация не запущена: уже выполняется "
                f"синхронизация другой записи. Опубликуйте эту "
                f"запись вручную через «Синхронизировать…».",
            )
        else:
            QMessageBox.information(
                self, "Готово к синхронизации",
                f"Запись «{r['name']}» помечена как «готова к "
                f"синхронизации».\n\n"
                f"Авто-публикация не запущена. Опубликуйте запись "
                f"через «Синхронизировать…» (Ctrl+Shift+S).",
            )

    def _on_sync_progress(self, message: str) -> None:
        if self._sync_progress_dlg is not None:
            self._sync_progress_dlg.setLabelText(message)

    def _on_sync_finished(self, result: Dict[str, Any]) -> None:
        if self._sync_progress_dlg is not None:
            self._sync_progress_dlg.close()
            self._sync_progress_dlg = None

        action = result.get("action") or "—"
        record_id = result.get("record_id") or "—"
        revision = result.get("revision") or 0
        media_up = result.get("media_uploaded") or []
        media_skip = result.get("media_skipped") or []

        lines: List[str] = []
        lines.append(f"<b>Запись успешно синхронизирована.</b><br><br>")
        lines.append(f"Действие: <b>{action}</b><br>")
        lines.append(f"Record ID: <code>{record_id}</code><br>")
        lines.append(f"Ревизия: {revision}<br>")
        lines.append(
            f"Передача медиа: "
            f"{'да' if result.get('include_media') else 'нет'}<br>"
        )
        lines.append(
            f"Ссылка file://: "
            f"{'да' if result.get('send_video_link') else 'нет'}<br>"
        )
        if result.get("include_media"):
            lines.append(f"<br>Медиа загружено: {len(media_up)}<br>")
            for m in media_up:
                size_mb = int(m.get("size_bytes") or 0) / 1024 / 1024
                lines.append(
                    f"&nbsp;&nbsp;• {m['filename']} "
                    f"({m['kind']}, {size_mb:.1f} МБ)<br>"
                )
            if media_skip:
                lines.append(
                    f"<br><span style='color:#c62828'>"
                    f"Медиа пропущено: {len(media_skip)}</span><br>"
                )
                for m in media_skip:
                    reason = m.get("reason") or "—"
                    lines.append(
                        f"&nbsp;&nbsp;• {m['filename']} — "
                        f"{reason[:80]}<br>"
                    )

        QMessageBox.information(
            self, "Синхронизация", "".join(lines),
        )

        self.refresh()

    def _on_sync_failed(self, error: str) -> None:
        if self._sync_progress_dlg is not None:
            self._sync_progress_dlg.close()
            self._sync_progress_dlg = None
        log.error("Синхронизация одной записи не удалась: %s", error)
        QMessageBox.critical(
            self, "Синхронизация",
            f"Не удалось синхронизировать запись:\n\n{error}",
        )

    def _on_sync_worker_done(self) -> None:
        self._sync_worker = None

    def _open_sync_window(self) -> None:
        try:
            from .sync_window import SyncWindow
            dlg = SyncWindow(
                sessions_root=self.sessions_root,
                config_manager=self.config_manager,
                parent=self,
            )
            dlg.exec()
            self.refresh()
        except Exception as exc:
            log.exception(
                "Не удалось открыть окно синхронизации: %s", exc
            )
            QMessageBox.critical(
                self, "Синхронизация",
                f"Ошибка открытия окна:\n{exc}",
            )

    # ------------------------------------------------------------------
    # Публичная ссылка на сервер
    # ------------------------------------------------------------------
    def _get_public_link(self, r: Dict[str, Any]) -> str:
        if self.config_manager is None:
            return ""

        try:
            sync_cfg = self.config_manager.get_sync_settings()
        except Exception as exc:
            log.warning(
                "Не удалось прочитать sync-настройки: %s", exc
            )
            return ""

        base_url = str(sync_cfg.get("base_url") or "").strip()
        if not base_url:
            return ""

        session_dir = r.get("dir") or ""
        if not session_dir:
            return ""

        record_id = get_record_id(session_dir)
        if not record_id:
            return ""

        record_path = get_record_path(session_dir)
        if not record_path:
            return ""

        return build_public_view_url(
            base_url, record_path, record_id, blocks=None,
        )

    def _open_public_link_dialog(self) -> None:
        r = self._selected_row()
        if not r:
            QMessageBox.warning(self, "Записи", "Выберите запись")
            return

        if self.config_manager is None:
            QMessageBox.warning(
                self, "Ссылка", "Нет доступа к настройкам."
            )
            return

        try:
            sync_cfg = self.config_manager.get_sync_settings()
        except Exception as exc:
            log.warning(
                "Не удалось прочитать sync-настройки: %s", exc
            )
            QMessageBox.warning(
                self, "Ссылка на сервер",
                f"Не удалось прочитать настройки синхронизации:\n{exc}",
            )
            return

        base_url = str(sync_cfg.get("base_url") or "").strip()
        if not base_url:
            QMessageBox.information(
                self, "Ссылка на сервер",
                "Публичные ссылки недоступны: не задан base_url "
                "в Настройки → Синхронизация.",
            )
            return

        session_dir = r.get("dir") or ""
        record_id = get_record_id(session_dir)
        if not record_id:
            QMessageBox.information(
                self, "Ссылка на сервер",
                "Запись ещё не опубликована на сервере.\n\n"
                "Опубликуйте её через «Синхронизировать…» "
                "(Ctrl+Shift+S), затем повторите.",
            )
            return

        record_path = get_record_path(session_dir)
        if not record_path:
            QMessageBox.information(
                self, "Ссылка на сервер",
                "В .sync_published.json нет поля path — "
                "возможно, запись публиковалась старой версией "
                "клиента. Опубликуйте её заново.",
            )
            return

        dlg = PublicLinkDialog(
            record_name=r.get("name") or "",
            record_path=record_path,
            record_id=record_id,
            base_url=base_url,
            parent=self,
        )
        dlg.exec()

    def _copy_public_link(self) -> None:
        r = self._selected_row()
        if not r:
            QMessageBox.warning(self, "Записи", "Выберите запись")
            return

        link = self._get_public_link(r)
        if not link:
            QMessageBox.information(
                self, "Ссылка на сервер",
                "Ссылка недоступна:\n"
                "  • запись не опубликована на сервере;\n"
                "  • не задан base_url в Настройки → Синхронизация.",
            )
            return

        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(link)
        log.info("Публичная ссылка скопирована: %s", link)
        QMessageBox.information(
            self, "Ссылка на сервер",
            f"Ссылка скопирована в буфер обмена:\n\n{link}",
        )

    def _pull_changes(self) -> None:
        if self.config_manager is None:
            QMessageBox.warning(
                self, "Синхронизация", "Нет ConfigManager"
            )
            return

        cfg = self.config_manager.get_sync_settings()
        if not (cfg.get("enabled") and cfg.get("base_url")
                and cfg.get("api_key")):
            QMessageBox.warning(
                self, "Синхронизация",
                "Синхронизация не настроена.\n\n"
                "Настройки → Синхронизация.",
            )
            return

        manager = SyncManager(
            sessions_root=self.sessions_root,
            sync_settings=cfg,
            config_manager=self.config_manager,
        )

        try:
            result = asyncio.run(manager.pull_changes())
        except Exception as exc:
            log.exception("Ошибка pull changes: %s", exc)
            QMessageBox.critical(
                self, "Синхронизация",
                f"Не удалось подтянуть изменения:\n{exc}",
            )
            return

        QMessageBox.information(
            self, "Синхронизация",
            f"Применено изменений: {result['applied']}\n"
            f"Новая ревизия: {result['last_revision']}\n"
            f"Ошибок: {len(result['errors'])}",
        )
        self.refresh()

    # ------------------------------------------------------------------
    # Bitrix24
    # ------------------------------------------------------------------
    def _send_to_bitrix(self, default: str = "") -> None:
        r = self._selected_row()
        if not r:
            QMessageBox.warning(self, "Записи", "Выберите запись")
            return

        if self.config_manager is None:
            QMessageBox.warning(
                self, "Bitrix24",
                "Нет доступа к настройкам — ConfigManager "
                "не передан.",
            )
            return

        bitrix_cfg = self.config_manager.get_bitrix_settings()
        if not bitrix_cfg.get("enabled"):
            QMessageBox.information(
                self, "Bitrix24",
                "Интеграция с Bitrix24 отключена.\n\n"
                "Включите её в Настройки → Bitrix24.",
            )
            return

        chat_id = self.config_manager.get_default_chat_id()
        project = r.get("project") or ""

        if chat_id:
            log.info(
                "Bitrix24: используется чат по умолчанию из настроек "
                "(%s) вместо чата проекта %r",
                chat_id, project or "—",
            )
        else:
            chat_id = self.config_manager.get_project_chat_id(project)
            if chat_id:
                log.info(
                    "Bitrix24: используется чат проекта «%s» (%s)",
                    project, chat_id,
                )

        protocol_path = self._find_manual_protocol_path(r)
        protocol_label = (
            os.path.basename(protocol_path) if protocol_path else ""
        )

        log.info(
            "Bitrix24: отправка записи «%s» — протокол=%r, "
            "summary=%d символов, session_dir=%s",
            r.get("name"),
            protocol_path or "не найден",
            len(r.get("summary_bb") or ""),
            r.get("dir") or "—",
        )

        session_info = {
            "name": r.get("name") or "",
            "project": project,
            "date": r.get("datetime") or "",
            "protocol_path": protocol_path,
            "protocol_label": protocol_label,
            "summary_bb": r.get("summary_bb") or "",
            "comment": "",
            "session_dir": r.get("dir") or "",
            "tags": list(r.get("tags") or []),
        }

        try:
            meta = read_json_file(
                os.path.join(r["dir"], "session.json")
            ) or {}
            session_info["comment"] = meta.get("comment") or ""
        except Exception:
            pass

        from .send_to_bitrix_dialog import SendToBitrixDialog

        projects = self.config_manager.get_projects()
        employees = self.config_manager.get_employees()

        try:
            sync_settings = self.config_manager.get_sync_settings()
        except Exception as exc:
            log.warning(
                "Не удалось прочитать sync-настройки: %s", exc
            )
            sync_settings = {}

        dlg = SendToBitrixDialog(
            session_info=session_info,
            chat_id=chat_id,
            bitrix_cfg=bitrix_cfg,
            projects=projects,
            employees=employees,
            sync_settings=sync_settings,
            default_send=default,
            parent=self,
        )
        dlg.exec()

    # ------------------------------------------------------------------
    # Быстрое редактирование тегов
    # ------------------------------------------------------------------
    def _edit_tags(self) -> None:
        r = self._selected_row()
        if not r:
            QMessageBox.warning(self, "Записи", "Выберите запись")
            return

        if self.config_manager is None:
            QMessageBox.warning(
                self, "Теги",
                "Нет доступа к настройкам.",
            )
            return

        session_json = os.path.join(r["dir"], "session.json")
        meta = read_json_file(session_json) or {}

        projects = self.config_manager.get_project_names()
        prompts = self.config_manager.get_prompts()
        default_prompt = self.config_manager.get_default_prompt()
        name_templates = self.config_manager.get_name_templates()
        tags = self.config_manager.get_tags()

        project = meta.get("project")
        if project and project not in projects:
            projects = [project] + projects

        dlg = MetadataDialog(
            projects=projects,
            prompts=prompts,
            title=f"Теги записи — {r['name']}",
            initial=meta,
            default_prompt=default_prompt,
            sessions_root=self.sessions_root,
            on_save_prompt=self._on_save_prompt_to_config,
            get_prompts=self.config_manager.get_prompts,
            name_templates=name_templates,
            on_save_name_template=self._on_save_name_template,
            get_name_templates=self.config_manager.get_name_templates,
            tags=tags,
            on_save_tag=self._on_save_tag_to_config,
            get_tags=self.config_manager.get_tags,
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        new_meta = dlg.result_data
        meta["tags"] = list(new_meta.get("tags", []))
        meta["sync_ready"] = bool(new_meta.get("sync_ready", False))
        if not self._write_json(session_json, meta):
            QMessageBox.critical(
                self, "Теги",
                "Не удалось сохранить session.json",
            )
            return

        log.info(
            "Теги записи «%s» обновлены: %s (sync_ready=%s)",
            r["name"], meta["tags"], meta["sync_ready"],
        )
        self.refresh()

    def _on_save_tag_to_config(self, name: str, color: str = "") -> None:
        try:
            name = (name or "").strip()
            if not name:
                return
            self.config_manager.add_tag(name, color)
        except Exception as exc:
            log.exception("Ошибка сохранения тега: %s", exc)
            raise

    # ------------------------------------------------------------------
    # Протокол
    # ------------------------------------------------------------------
    def _edit_manual_protocol_md(self) -> None:
        r = self._selected_row()
        if not r:
            QMessageBox.warning(self, "Записи", "Выберите запись")
            return

        md_path = os.path.join(r["dir"], "manual_protocol.md")
        initial_text = ""

        if os.path.exists(md_path):
            initial_text = read_any_text(md_path)
        else:
            existing = r.get("manual_protocol_path") or ""
            if existing and os.path.exists(existing):
                initial_text = self._read_protocol_as_text(existing)

        default_docx = os.path.join(
            r["dir"], "manual_protocol.docx"
        )

        dlg = MarkdownEditorDialog(
            text=initial_text,
            title=f"Протокол (Markdown) — {r['name']}",
            parent=self,
            default_docx_path=default_docx,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        new_text = dlg.result_text()

        try:
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(new_text)
        except Exception as exc:
            log.exception(
                "Не удалось записать %s: %s", md_path, exc
            )
            QMessageBox.critical(
                self, "Протокол",
                f"Не удалось сохранить протокол:\n{exc}",
            )
            return

        session_json = os.path.join(r["dir"], "session.json")
        meta = read_json_file(session_json) or {}
        meta["manual_protocol_path"] = md_path
        if not self._write_json(session_json, meta):
            QMessageBox.critical(
                self, "Протокол",
                "Файл протокола записан, но не удалось обновить "
                "session.json.",
            )
            return

        self.refresh()
        QMessageBox.information(
            self, "Протокол",
            f"Протокол сохранён и прикреплён к записи:\n"
            f"{md_path}\n\n"
            "Чтобы получить .docx для отправки в чат — "
            "используйте «Протокол → Экспорт протокола в DOCX…».",
        )

    def _export_protocol_docx(self) -> None:
        r = self._selected_row()
        if not r:
            QMessageBox.warning(self, "Записи", "Выберите запись")
            return

        src_path = r.get("manual_protocol_path") or ""
        md_path = os.path.join(r["dir"], "manual_protocol.md")

        if not src_path or not os.path.exists(src_path):
            if os.path.exists(md_path):
                src_path = md_path
            else:
                QMessageBox.information(
                    self, "Экспорт в DOCX",
                    "К записи не прикреплён протокол.",
                )
                return

        ext = os.path.splitext(src_path)[1].lower()

        if ext == ".docx":
            QMessageBox.information(
                self, "Экспорт в DOCX",
                f"Протокол уже в формате DOCX:\n{src_path}",
            )
            return

        md_text = self._read_protocol_as_text(src_path)
        if not md_text.strip():
            QMessageBox.warning(
                self, "Экспорт в DOCX",
                "Не удалось извлечь текст из исходного файла.",
            )
            return

        default_path = os.path.join(
            r["dir"], "manual_protocol.docx"
        )
        target, _ = QFileDialog.getSaveFileName(
            self,
            "Сохранить протокол как DOCX",
            default_path,
            "Документы Word (*.docx);;Все файлы (*)",
        )
        if not target:
            return
        target = safe_local_path(target)

        if not target.lower().endswith(".docx"):
            target += ".docx"

        title = r.get("name") or ""

        try:
            markdown_to_docx(md_text, target, title=title)
        except Exception as exc:
            log.exception("Ошибка конвертации в DOCX: %s", exc)
            QMessageBox.critical(
                self, "Экспорт в DOCX",
                f"Не удалось сохранить файл:\n{exc}",
            )
            return

        attach = False
        if os.path.abspath(target) == os.path.abspath(
            default_path
        ):
            session_json = os.path.join(
                r["dir"], "session.json"
            )
            meta = read_json_file(session_json) or {}
            meta["manual_protocol_path"] = target
            if self._write_json(session_json, meta):
                attach = True

        if attach:
            QMessageBox.information(
                self, "Экспорт в DOCX",
                f"Документ сохранён и прикреплён к записи:\n"
                f"{target}",
            )
            self.refresh()
        else:
            QMessageBox.information(
                self, "Экспорт в DOCX",
                f"Документ сохранён:\n{target}",
            )

    @staticmethod
    def _write_json(path: str, data: Dict[str, Any]) -> bool:
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, path)
            return True
        except Exception as exc:
            log.exception("Не удалось записать %s: %s", path, exc)
            return False

    @staticmethod
    def _read_protocol_as_text(path: str) -> str:
        return read_any_text(path)

    def _attach_manual_protocol(self) -> None:
        r = self._selected_row()
        if not r:
            QMessageBox.warning(self, "Записи", "Выберите запись")
            return

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Выберите файл протокола",
            os.path.expanduser("~"),
            "Документы (*.docx *.txt *.md *.pdf);;Все файлы (*)",
        )
        if not file_path:
            return
        file_path = safe_local_path(file_path)

        if not os.path.isfile(file_path):
            QMessageBox.warning(
                self, "Протокол", f"Файл не найден:\n{file_path}"
            )
            return

        ext = os.path.splitext(file_path)[1].lower() or ".bin"
        target = os.path.join(
            r["dir"], f"manual_protocol{ext}"
        )

        try:
            shutil.copy2(file_path, target)
        except Exception as exc:
            log.exception("Ошибка копирования протокола: %s", exc)
            QMessageBox.critical(
                self, "Протокол",
                f"Не удалось скопировать файл:\n{exc}",
            )
            return

        session_json = os.path.join(r["dir"], "session.json")
        meta = read_json_file(session_json) or {}
        meta["manual_protocol_path"] = target
        if not self._write_json(session_json, meta):
            QMessageBox.critical(
                self, "Протокол",
                "Не удалось обновить session.json",
            )
            return

        QMessageBox.information(
            self, "Протокол",
            f"Протокол прикреплён к записи:\n{target}",
        )
        self.refresh()

    def _open_manual_protocol(self) -> None:
        r = self._selected_row()
        if not r:
            QMessageBox.warning(self, "Записи", "Выберите запись")
            return
        path = self._find_manual_protocol_path(r)
        if not path:
            QMessageBox.information(
                self, "Протокол",
                "К этой записи не прикреплён ручной протокол.",
            )
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    def _edit_summary_bb(self) -> None:
        r = self._selected_row()
        if not r:
            QMessageBox.warning(self, "Записи", "Выберите запись")
            return

        threshold_chars = 0
        threshold_label = ""
        if self.config_manager is not None:
            try:
                bitrix_cfg = self.config_manager.get_bitrix_settings()
                threshold_chars = int(
                    bitrix_cfg.get("file_message_max_chars", 0) or 0
                )
                if threshold_chars > 0:
                    threshold_label = "Bitrix24"
            except Exception as exc:
                log.warning(
                    "Не удалось прочитать настройки Bitrix24 "
                    "для порога: %s", exc,
                )

        dlg = MarkdownEditorDialog(
            text=r.get("summary_bb") or "",
            title=f"Краткое описание — {r['name']}",
            parent=self,
            threshold_chars=threshold_chars,
            threshold_label=threshold_label,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        new_text = dlg.result_text()
        session_json = os.path.join(r["dir"], "session.json")
        meta = read_json_file(session_json) or {}
        meta["summary_bb"] = new_text
        if not self._write_json(session_json, meta):
            QMessageBox.critical(
                self, "Summary",
                "Не удалось сохранить summary в session.json",
            )
            return

        try:
            summary_md_path = os.path.join(
                r["dir"], "summary.md"
            )
            with open(summary_md_path, "w", encoding="utf-8") as f:
                f.write(new_text)
        except Exception as exc:
            log.warning("Не удалось сохранить summary.md: %s", exc)

        self.refresh()

    def _view_summary(self) -> None:
        r = self._selected_row()
        if not r:
            QMessageBox.warning(self, "Записи", "Выберите запись")
            return
        text = r.get("summary_bb") or ""
        if not text.strip():
            QMessageBox.information(
                self, "Summary",
                "У этой записи ещё нет краткого описания.",
            )
            return
        dlg = MarkdownViewerDialog(
            text=text,
            title=f"Просмотр summary — {r['name']}",
            parent=self,
        )
        dlg.exec()

    def _export_summary(self) -> None:
        r = self._selected_row()
        if not r:
            QMessageBox.warning(self, "Записи", "Выберите запись")
            return

        text_md = r.get("summary_bb") or ""
        if not text_md.strip():
            QMessageBox.information(
                self, "Экспорт summary",
                "У этой записи нет краткого описания.",
            )
            return

        formats = ["docx", "html", "md", "txt"]
        fmt, ok = QInputDialog.getItem(
            self, "Экспорт summary", "Формат файла:",
            formats, 0, False,
        )
        if not ok or not fmt:
            return

        default_path = os.path.join(
            r["dir"], f"summary_{self._safe_name(r['name'])}.{fmt}"
        )
        target_path, _ = QFileDialog.getSaveFileName(
            self,
            "Сохранить summary как",
            default_path,
            "Word (*.docx);;HTML (*.html);;Markdown (*.md);;"
            "Text (*.txt);;All files (*)",
        )
        if not target_path:
            return
        target_path = safe_local_path(target_path)

        try:
            if fmt == "docx":
                markdown_to_docx(
                    text_md, target_path,
                    title=r.get("name") or "",
                )
            elif fmt == "html":
                self._export_summary_html_md(
                    text_md, target_path
                )
            elif fmt == "md":
                with open(
                    target_path, "w", encoding="utf-8"
                ) as f:
                    f.write(text_md)
            else:
                with open(
                    target_path, "w", encoding="utf-8"
                ) as f:
                    f.write(markdown_to_plain(text_md))
            QMessageBox.information(
                self, "Экспорт summary",
                f"Файл сохранён:\n{target_path}",
            )
        except Exception as exc:
            log.exception("Ошибка экспорта summary: %s", exc)
            QMessageBox.critical(
                self, "Экспорт summary",
                f"Не удалось сохранить: {exc}",
            )

    @staticmethod
    def _export_summary_html_md(text_md: str, path: str) -> None:
        from PySide6.QtGui import QTextDocument
        doc = QTextDocument()
        doc.setMarkdown(text_md)
        html_body = doc.toHtml()
        with open(path, "w", encoding="utf-8") as f:
            f.write(html_body)

    @staticmethod
    def _safe_name(name: str) -> str:
        bad = '<>:"/\\|?*\n\r\t'
        cleaned = "".join(
            ("_" if c in bad else c) for c in (name or "summary")
        )
        cleaned = cleaned.strip() or "summary"
        return cleaned[:60]

    # ------------------------------------------------------------------
    # Быстрое сохранение промпта
    # ------------------------------------------------------------------
    @staticmethod
    def _downloads_dir() -> str:
        try:
            import subprocess
            out = subprocess.check_output(
                ["xdg-user-dir", "DOWNLOAD"],
                text=True, timeout=2,
            ).strip()
            if out and os.path.isdir(out):
                return out
        except Exception:
            pass

        home = os.path.expanduser("~")
        for candidate in ("Загрузки", "Downloads"):
            path = os.path.join(home, candidate)
            if os.path.isdir(path):
                return path
        return home

    def _save_prompt_to_downloads(self) -> None:
        r = self._selected_row()
        if not r:
            QMessageBox.warning(self, "Записи", "Выберите запись")
            return

        candidates = []
        for fname in ("deepseek_prompt.docx",
                      "deepseek_prompt.md",
                      "deepseek_prompt.txt"):
            p = os.path.join(r["dir"], fname)
            if os.path.exists(p):
                candidates.append(p)

        if not candidates:
            QMessageBox.information(
                self, "DeepSeek",
                "Для этой записи промпт не сформирован.\n\n"
                "Промпт создаётся при обработке.",
            )
            return

        downloads = self._downloads_dir()
        if not os.path.isdir(downloads):
            QMessageBox.warning(
                self, "DeepSeek",
                f"Папка «Загрузки» не найдена:\n{downloads}",
            )
            return

        safe_name = self._safe_name(r["name"]) or "record"
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

        saved: List[str] = []
        errors: List[str] = []

        for src in candidates:
            ext = os.path.splitext(src)[1]
            base = os.path.basename(src)
            stem = os.path.splitext(base)[0]
            dst_name = f"{stem}_{safe_name}_{stamp}{ext}"
            dst = os.path.join(downloads, dst_name)

            if os.path.exists(dst):
                i = 1
                while os.path.exists(dst):
                    dst = os.path.join(
                        downloads,
                        f"{stem}_{safe_name}_{stamp}_{i}{ext}",
                    )
                    i += 1

            try:
                shutil.copy2(src, dst)
                saved.append(dst)
            except Exception as exc:
                log.exception(
                    "Ошибка сохранения промпта %s: %s", src, exc
                )
                errors.append(f"{base}: {exc}")

        if saved and not errors:
            QMessageBox.information(
                self, "DeepSeek",
                f"Файлы промпта сохранены в «Загрузки»:\n\n"
                + "\n".join(saved),
            )
        elif saved and errors:
            QMessageBox.warning(
                self, "DeepSeek",
                "Часть файлов сохранена, часть — с ошибкой:\n\n"
                + "\n".join(saved)
                + "\n\nОшибки:\n"
                + "\n".join(errors),
            )
        else:
            QMessageBox.critical(
                self, "DeepSeek",
                "Не удалось сохранить ни одного файла:\n\n"
                + "\n".join(errors),
            )

    # ------------------------------------------------------------------
    # Утилиты: работа с видеофайлами
    # ------------------------------------------------------------------
    @staticmethod
    def _format_size(size_bytes: int) -> str:
        size = float(size_bytes)
        for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} ПБ"

    def _iter_session_dirs(self) -> List[str]:
        if not os.path.isdir(self.sessions_root):
            return []
        try:
            entries = sorted(os.listdir(self.sessions_root))
        except OSError as exc:
            log.error(
                "Не удалось прочитать %s: %s",
                self.sessions_root, exc,
            )
            return []

        result: List[str] = []
        for name in entries:
            d = os.path.join(self.sessions_root, name)
            if os.path.isdir(d):
                result.append(d)
        return result

    @staticmethod
    def _find_video_file(session_dir: str) -> str:
        for ext in _VIDEO_EXTS:
            p = os.path.join(session_dir, f"video{ext}")
            if os.path.exists(p):
                return p
        return ""

    @staticmethod
    def _find_audio_file(session_dir: str) -> str:
        for ext in _AUDIO_EXTS:
            p = os.path.join(session_dir, f"video{ext}")
            if os.path.exists(p):
                return p
        return ""

    def _collect_video_stats(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "sessions_total": 0,
            "sessions_with_video": 0,
            "sessions_without_audio": 0,
            "deletable": [],
            "total_size": 0,
            "keep_size": 0,
        }

        for session_dir in self._iter_session_dirs():
            result["sessions_total"] += 1

            video_path = self._find_video_file(session_dir)
            if not video_path:
                continue

            ext = os.path.splitext(video_path)[1].lower()
            if ext in _AUDIO_EXTS:
                continue

            result["sessions_with_video"] += 1

            try:
                size = os.path.getsize(video_path)
            except OSError:
                size = 0

            audio_path = self._find_audio_file(session_dir)
            if not audio_path:
                result["sessions_without_audio"] += 1
                result["keep_size"] += size
                continue

            result["deletable"].append({
                "session": session_dir,
                "name": os.path.basename(session_dir),
                "video": video_path,
                "video_size": size,
                "audio": audio_path,
            })
            result["total_size"] += size

        return result

    def _show_video_stats(self) -> None:
        stats = self._collect_video_stats()

        total = stats["sessions_total"]
        with_video = stats["sessions_with_video"]
        without_audio = stats["sessions_without_audio"]
        deletable = len(stats["deletable"])
        total_size = self._format_size(stats["total_size"])

        msg = (
            f"<b>Всего сессий:</b> {total}<br>"
            f"<b>Сессий с video.mp4:</b> {with_video}<br>"
            f"<b>Сессий без аудио (удалять нельзя):</b> "
            f"{without_audio}<br><br>"
            f"<b>Можно удалить video.mp4:</b> {deletable} шт.<br>"
            f"<b>Освободится:</b> {total_size}"
        )

        QMessageBox.information(
            self, "Размер видеофайлов", msg
        )

    def _delete_video_files(self) -> None:
        stats = self._collect_video_stats()
        deletable: List[Dict[str, Any]] = stats["deletable"]

        if not deletable:
            QMessageBox.information(
                self, "Удаление видеофайлов",
                "Нет сессий, где можно удалить video.mp4.",
            )
            return

        total_size_str = self._format_size(stats["total_size"])

        preview_lines: List[str] = []
        preview_limit = 15
        for i, item in enumerate(deletable):
            if i >= preview_limit:
                preview_lines.append(
                    f"… и ещё "
                    f"{len(deletable) - preview_limit} сессий"
                )
                break
            size_str = self._format_size(item["video_size"])
            preview_lines.append(f"• {item['name']} — {size_str}")

        preview = "\n".join(preview_lines)

        msg = (
            f"<b>Будет удалён video.mp4 из сессий:</b> "
            f"{len(deletable)} шт.<br>"
            f"<b>Освободится места:</b> {total_size_str}<br>"
            f"<br>"
            f"Аудиофайлы (video.mp3 и др.) <b>останутся</b>.<br>"
            f"<br>"
            f"<b>Сессии к удалению:</b><br>"
            f"<pre style='font-family:monospace'>{preview}</pre>"
            f"<br>"
            f"<b style='color:#c62828'>Действие необратимо.</b><br>"
            f"Продолжить?"
        )

        reply = QMessageBox.question(
            self,
            "Подтверждение удаления",
            msg,
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        progress = QProgressDialog(
            "Удаление видеофайлов…",
            "Отмена",
            0, len(deletable),
            self,
        )
        progress.setWindowTitle("Удаление видеофайлов")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)

        removed = 0
        freed_bytes = 0
        errors: List[str] = []

        for i, item in enumerate(deletable):
            if progress.wasCanceled():
                break

            video_path = item["video"]
            size = item["video_size"]

            audio_path = self._find_audio_file(item["session"])
            if not audio_path:
                errors.append(
                    f"{item['name']}: аудио исчезло, пропуск"
                )
                progress.setValue(i + 1)
                continue

            try:
                os.remove(video_path)
                removed += 1
                freed_bytes += size
            except Exception as exc:
                errors.append(f"{item['name']}: {exc}")

            progress.setValue(i + 1)

        progress.setValue(len(deletable))

        freed_str = self._format_size(freed_bytes)

        if errors:
            QMessageBox.warning(
                self, "Удаление видеофайлов",
                f"Удалено файлов: {removed} из {len(deletable)}\n"
                f"Освобождено: {freed_str}\n\n"
                f"Ошибки при удалении:\n"
                + "\n".join(errors[:20])
                + ("\n…" if len(errors) > 20 else ""),
            )
        else:
            QMessageBox.information(
                self, "Удаление видеофайлов",
                f"Удалено файлов: {removed}\n"
                f"Освобождено: {freed_str}",
            )

        self.refresh()

    # ------------------------------------------------------------------
    # Внутренние операции
    # ------------------------------------------------------------------
    def _remove_from_queue(self, task_id: str) -> bool:
        if not task_id:
            return False
        try:
            self.task_queue.remove_task(task_id)
            return True
        except Exception as exc:
            log.warning(
                "Не удалось удалить задачу %s: %s", task_id, exc
            )
            return False

    def _delete_processed_files(self, session_dir: str) -> None:
        """
        Удаляет результаты обработки при сбросе статуса
        в «Сохранено». Исходное видео и вложения не трогает.
        """
        for fname in (
            "video.mp3", "video.txt",
            "video.aac", "video.wav", "video.opus",
            "video.ogg", "video.m4a",
            "video_summary.md", "summary.md",
            "deepseek_prompt.docx", "deepseek_prompt.md",
            "deepseek_prompt.txt",
            "action_items.json",
            "action_items_prompt.docx",
            "action_items_prompt.md",
            "action_items_prompt.txt",
            "action_items_request.txt",
            "action_items_response_raw.txt",
        ):
            p = os.path.join(session_dir, fname)
            if os.path.exists(p):
                try:
                    os.remove(p)
                    log.info("Удалён артефакт: %s", p)
                except Exception as exc:
                    log.warning(
                        "Не удалось удалить %s: %s", p, exc
                    )

    def _remove_processed_artifacts(
        self,
        session_dir: str,
        *,
        keep_transcript: bool = False,
    ) -> None:
        """
        Удаляет результаты обработки.

        Args:
            keep_transcript: если True — файл video.txt НЕ удаляется
                             (транскрибация будет пропущена).
        """
        patterns = [
            "video.mp3", "video.aac", "video.wav", "video.opus",
            "video.ogg", "video.m4a",
            "video_summary.md",
            "deepseek_prompt.txt", "deepseek_prompt.md",
            "deepseek_prompt.docx",
            "protocol.docx", "protocol.md", "protocol.txt",
        ]
        if not keep_transcript:
            patterns.append("video.txt")

        for fname in patterns:
            p = os.path.join(session_dir, fname)
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception as exc:
                    log.warning(
                        "Не удалось удалить %s: %s", p, exc
                    )

    def _enqueue_session(self, r: Dict[str, Any]) -> Optional[str]:
        if not os.path.exists(r["video_path"]):
            log.error("Видео не найдено: %s", r["video_path"])
            return None
        meta = read_json_file(
            os.path.join(r["dir"], "session.json")
        ) or {}
        meta["video_path"] = r["video_path"]
        meta["session_dir"] = r["dir"]
        meta.pop("task_id", None)
        try:
            task_id = self.task_queue.add_task(
                {"video_path": r["video_path"], "metadata": meta}
            )
            # Сохраняем task_id в session.json.
            if task_id:
                meta["task_id"] = task_id
                self._write_json(
                    os.path.join(r["dir"], "session.json"),
                    meta,
                )
            return task_id
        except Exception as exc:
            log.exception("Не удалось добавить задачу: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Перезапуск / редактирование метаданных
    # ------------------------------------------------------------------
    def _restart_processing(self) -> None:
        r = self._selected_row()
        if not r:
            QMessageBox.warning(self, "Записи", "Выберите запись")
            return
        if not r["video_path"] or not os.path.exists(r["video_path"]):
            QMessageBox.warning(
                self, "Записи",
                f"Видео не найдено:\n{r['video_path']}",
            )
            return
        if QMessageBox.question(
            self, "Перезапустить обработку",
            f"Запись «{r['name']}» будет обработана заново.",
        ) != QMessageBox.StandardButton.Yes:
            return
        if r["task_id"]:
            self._remove_from_queue(r["task_id"])
        self._remove_processed_artifacts(r["dir"])
        task_id = self._enqueue_session(r)
        if task_id:
            QMessageBox.information(
                self, "Записи",
                f"Запись добавлена в очередь: {task_id}",
            )
            self.refresh()
        else:
            QMessageBox.critical(
                self, "Ошибка",
                "Не удалось добавить запись в очередь",
            )

    def _edit_metadata_and_restart(self) -> None:
        r = self._selected_row()
        if not r:
            QMessageBox.warning(self, "Записи", "Выберите запись")
            return

        if not r["video_path"] or not os.path.exists(
            r["video_path"]
        ):
            QMessageBox.warning(
                self, "Записи",
                f"Видео не найдено:\n"
                f"{r['video_path'] or '(путь не задан)'}",
            )
            return

        if self.config_manager is None:
            QMessageBox.warning(
                self, "Записи",
                "Нет доступа к настройкам.",
            )
            return

        session_json = os.path.join(r["dir"], "session.json")
        meta = read_json_file(session_json) or {}

        projects = self.config_manager.get_project_names()
        prompts = self.config_manager.get_prompts()
        default_prompt = self.config_manager.get_default_prompt()
        name_templates = self.config_manager.get_name_templates()
        tags = self.config_manager.get_tags()

        project = meta.get("project")
        if project and project not in projects:
            projects = [project] + projects

        dlg = MetadataDialog(
            projects=projects,
            prompts=prompts,
            title=f"Метаданные записи — {r['name']}",
            initial=meta,
            default_prompt=default_prompt,
            sessions_root=self.sessions_root,
            on_save_prompt=self._on_save_prompt_to_config,
            get_prompts=self.config_manager.get_prompts,
            name_templates=name_templates,
            on_save_name_template=self._on_save_name_template,
            get_name_templates=self.config_manager.get_name_templates,
            tags=tags,
            on_save_tag=self._on_save_tag_to_config,
            get_tags=self.config_manager.get_tags,
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        new_meta = dlg.result_data

        old_sync_ready = bool(meta.get("sync_ready", False))
        new_sync_ready = bool(new_meta.get("sync_ready", False))

        # ВАЖНО: date/time НЕ сохраняем принудительно —
        # пользователь мог изменить их в диалоге метаданных.
        # Сохраняем только те поля, которые не редактируются
        # в MetadataDialog.
        for keep_key in (
            "monitor",
            "summary_bb",
            "manual_protocol_path",
            "source", "source_files",
            "video_path", "session_dir",
        ):
            if keep_key not in new_meta and keep_key in meta:
                new_meta[keep_key] = meta[keep_key]

        new_meta.setdefault("video_path", r["video_path"])
        new_meta.setdefault("session_dir", r["dir"])
        new_meta.pop("task_id", None)

        reply = QMessageBox.question(
            self,
            "Перезапустить обработку",
            f"Запись «{r['name']}» будет обработана заново с "
            f"новыми параметрами.<br><br>Продолжить?",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        if not self._write_json(session_json, new_meta):
            QMessageBox.critical(
                self, "Записи",
                "Не удалось сохранить session.json",
            )
            return

        # --- Проверяем, есть ли уже стенограмма ---
        transcript_path = os.path.join(r["dir"], "video.txt")
        has_transcript = os.path.isfile(transcript_path)
        transcript_size = 0
        if has_transcript:
            try:
                transcript_size = os.path.getsize(transcript_path)
            except OSError:
                transcript_size = 0

        keep_transcript = False

        if has_transcript:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Question)
            box.setWindowTitle("Перезапуск обработки")
            box.setTextFormat(Qt.TextFormat.RichText)
            box.setText(
                f"<b>В папке записи уже есть стенограмма.</b>"
                f"<br><br>"
                f"Файл: <code>video.txt</code> "
                f"({transcript_size / 1024:.1f} КБ)<br><br>"
                f"<b>Сохранить стенограмму</b> — транскрибация "
                f"будет пропущена (быстро).<br>"
                f"<b>Пересобрать</b> — стенограмма будет удалена, "
                f"транскрибация запустится заново (долго)."
            )
            keep_btn = box.addButton(
                "Сохранить стенограмму",
                QMessageBox.ButtonRole.AcceptRole,
            )
            redo_btn = box.addButton(
                "Пересобрать стенограмму",
                QMessageBox.ButtonRole.DestructiveRole,
            )
            cancel_btn = box.addButton(
                "Отмена", QMessageBox.ButtonRole.RejectRole,
            )
            box.setDefaultButton(keep_btn)
            box.exec()

            clicked = box.clickedButton()
            if clicked is cancel_btn:
                log.info(
                    "Перезапуск отменён пользователем на этапе "
                    "выбора стенограммы"
                )
                return
            keep_transcript = (clicked is keep_btn)

            log.info(
                "Перезапуск «%s»: стенограмма %s",
                r["name"],
                "сохраняется" if keep_transcript else "пересобирается",
            )

        # --- Удаляем артефакты ---
        if keep_transcript:
            self._remove_processed_artifacts(
                r["dir"], keep_transcript=True,
            )
        else:
            self._remove_processed_artifacts(r["dir"])

        if r["task_id"]:
            self._remove_from_queue(r["task_id"])

        task_id = self._enqueue_session({
            "video_path": r["video_path"],
            "dir": r["dir"],
        })
        if not task_id:
            QMessageBox.critical(
                self, "Записи",
                "Метаданные сохранены, но не удалось поставить "
                "запись в очередь.",
            )
            return

        QMessageBox.information(
            self, "Записи",
            f"Запись поставлена в очередь на обработку: {task_id}",
        )
        self.refresh()

        # --- Авто-публикация, если флаг только что установлен ---
        if not old_sync_ready and new_sync_ready:
            for row in self._rows:
                if row["dir"] == r["dir"]:
                    row["sync_ready"] = True
                    self._maybe_publish_after_sync_ready(
                        row, was_ready=old_sync_ready,
                    )
                    break

    # ------------------------------------------------------------------
    # Помощники для MetadataDialog
    # ------------------------------------------------------------------
    def _on_save_prompt_to_config(self, name: str, text: str) -> None:
        try:
            name = (name or "").strip()
            text = (text or "").strip()
            if not name or not text:
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
        except Exception as exc:
            log.exception("Ошибка сохранения промпта: %s", exc)
            raise

    def _on_save_name_template(
        self, label: str, template: str,
    ) -> None:
        try:
            label = (label or "").strip()
            template = (template or "").strip()
            if not template:
                return
            self.config_manager.add_name_template(label, template)
        except Exception as exc:
            log.exception(
                "Ошибка сохранения шаблона имени: %s", exc
            )
            raise

    # ------------------------------------------------------------------
    # Изменение статуса
    # ------------------------------------------------------------------
    def _change_status(self, new_status: str) -> None:
        r = self._selected_row()
        if not r:
            QMessageBox.warning(self, "Записи", "Выберите запись")
            return
        confirm_msg = {
            STATUS_UPLOADED:
                f"Сбросить статус «{r['name']}» в «Сохранено»?",
            STATUS_PROCESSED:
                f"Пометить «{r['name']}» как «Обработан»?",
            STATUS_ERROR:
                f"Пометить «{r['name']}» как «Ошибка»?",
        }.get(new_status, f"Изменить статус на «{new_status}»?")
        if QMessageBox.question(
            self, "Изменить статус", confirm_msg
        ) != QMessageBox.StandardButton.Yes:
            return
        if r["task_id"]:
            self._remove_from_queue(r["task_id"])
        if new_status == STATUS_UPLOADED:
            self._delete_processed_files(r["dir"])
        if new_status == STATUS_PROCESSED:
            has_audio = os.path.exists(
                os.path.join(r["dir"], "video.mp3")
            )
            has_txt = os.path.exists(
                os.path.join(r["dir"], "video.txt")
            )
            if not (has_audio or has_txt):
                QMessageBox.warning(
                    self, "Записи",
                    "Не найдены video.mp3 или video.txt.",
                )
                self.refresh()
                return
        self.refresh()

    def _enqueue_current(self) -> None:
        r = self._selected_row()
        if not r:
            QMessageBox.warning(self, "Записи", "Выберите запись")
            return
        if r["task_id"]:
            QMessageBox.information(
                self, "Записи",
                f"Запись уже в очереди: {r['task_id']}",
            )
            return
        if not r["video_path"] or not os.path.exists(
            r["video_path"]
        ):
            QMessageBox.warning(
                self, "Записи",
                f"Видео не найдено:\n{r['video_path']}",
            )
            return
        task_id = self._enqueue_session(r)
        if task_id:
            QMessageBox.information(
                self, "Записи",
                f"Запись добавлена в очередь: {task_id}",
            )
            self.refresh()
        else:
            QMessageBox.critical(
                self, "Ошибка",
                "Не удалось добавить запись в очередь",
            )

    # ------------------------------------------------------------------
    # Контекстное меню
    # ------------------------------------------------------------------
    def _show_context_menu(self, pos) -> None:
        r = self._selected_row()
        if not r:
            return
        menu = QMenu(self)

        act_play_video = menu.addAction(
            "Смотреть видео", self._open_video
        )
        act_play_video.setEnabled(
            bool(r.get("has_video")) or bool(r.get("published"))
        )
        act_play_video.setToolTip(
            "Открыть видео во встроенном плеере.\n"
            "Если файла нет локально, но он есть на сервере — "
            "будет предложено скачать."
        )

        act_play_audio = menu.addAction(
            "Прослушать аудио", self._open_audio
        )
        act_play_audio.setEnabled(
            bool(r.get("has_audio")) or bool(r.get("published"))
        )
        act_play_audio.setToolTip(
            "Открыть аудио во встроенном плеере.\n"
            "Если файла нет локально, но он есть на сервере — "
            "будет предложено скачать."
        )

        act_open_transcript = menu.addAction(
            "Просмотреть стенограмму", self._open_transcript
        )
        act_open_transcript.setEnabled(
            bool(r.get("has_transcript")) or bool(r.get("published"))
        )
        act_open_transcript.setToolTip(
            "Открыть стенограмму (video.txt) во встроенном "
            "просмотрщике с поиском (Ctrl+F, F3 / Shift+F3).\n\n"
            "Поиск файла выполняется в корне папки записи и в "
            "подпапке attachments."
        )

        act_view_protocol = menu.addAction(
            "Просмотреть протокол", self._view_manual_protocol
        )
        protocol_path = self._find_manual_protocol_path(r)
        act_view_protocol.setEnabled(bool(protocol_path))
        act_view_protocol.setToolTip(
            "Открыть прикреплённый протокол (.docx) во встроенном "
            "просмотрщике с поиском (Ctrl+F, F3 / Shift+F3).\n\n"
            "Поиск файла выполняется в корне папки записи и в "
            "подпапке attachments."
        )

        menu.addSeparator()

        sync_ready_text = (
            "Снять отметку «готово к синхронизации»"
            if r.get("sync_ready")
            else "Отметить как «готово к синхронизации»"
        )
        act_sync_ready = menu.addAction(
            sync_ready_text, self._toggle_sync_ready
        )
        act_sync_ready.setToolTip(
            "Переключить флаг sync_ready.\n\n"
            "Пока флаг не выставлен — запись считается черновиком: "
            "автопубликация не запускается, фоновый pull не "
            "перезаписывает локальные файлы, локальные артефакты "
            "не удаляются."
        )

        act_sync = menu.addAction(
            "Синхронизировать…", self._sync_one_record
        )
        act_sync.setToolTip(
            "Опубликовать выбранную запись на сервер "
            "с выбором параметров (медиа, ссылка, удаление)"
        )

        is_published = bool(r.get("published"))
        act_public = menu.addAction(
            "Открыть ссылку на сервере…",
            self._open_public_link_dialog,
        )
        act_public.setEnabled(is_published)
        act_public.setToolTip(
            "Открыть публичную HTML-страницу записи на сервере"
        )
        act_copy_public = menu.addAction(
            "Скопировать ссылку на сервер",
            self._copy_public_link,
        )
        act_copy_public.setEnabled(is_published)
        act_copy_public.setToolTip(
            "Скопировать публичную ссылку на запись "
            "в буфер обмена"
        )

        menu.addSeparator()

        menu.addAction(
            "Редактировать метаданные и перезапустить…",
            self._edit_metadata_and_restart,
        )
        menu.addAction(
            "Изменить теги…", self._edit_tags
        )
        menu.addSeparator()
        menu.addAction(
            "Перезапустить обработку", self._restart_processing
        )
        menu.addAction(
            "Поставить в очередь", self._enqueue_current
        )
        menu.addSeparator()
        menu.addAction("Открыть папку записи", self._open_folder)
        menu.addSeparator()
        menu.addAction(
            "Изменить summary (Markdown)…", self._edit_summary_bb
        )
        menu.addAction(
            "Создать/редактировать протокол (Markdown)…",
            self._edit_manual_protocol_md,
        )
        menu.addAction(
            "Просмотреть протокол (.docx)…",
            self._view_manual_protocol,
        )

        menu.addSeparator()
        menu.addAction(
            "Редактор поручений", self._open_tasks_editor
        )

        menu.exec(self.table.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------------
    # DeepSeek-промпт
    # ------------------------------------------------------------------
    def _open_deepseek_prompt(self) -> None:
        r = self._selected_row()
        if not r:
            QMessageBox.warning(self, "Записи", "Выберите запись")
            return
        path = r.get("prompt_path", "")
        if not path or not os.path.exists(path):
            QMessageBox.information(
                self, "DeepSeek",
                "Для этой записи промпт не сформирован.",
            )
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _export_prompt(self) -> None:
        r = self._selected_row()
        if not r:
            QMessageBox.warning(self, "Записи", "Выберите запись")
            return
        path = r.get("prompt_path", "")
        if not path or not os.path.exists(path):
            QMessageBox.information(
                self, "DeepSeek",
                "Для этой записи промпт не сформирован.",
            )
            return

        preferred = "docx"
        if self.config_manager is not None:
            try:
                preferred = (
                    self.config_manager.get_scrum_settings()
                    .get("export_format", "docx")
                )
            except Exception:
                pass

        formats = ["docx", "md", "txt"]
        try:
            pref_idx = formats.index(preferred)
        except ValueError:
            pref_idx = 0

        fmt, ok = QInputDialog.getItem(
            self, "Экспорт промпта", "Формат файла:",
            formats, pref_idx, False,
        )
        if not ok or not fmt:
            return

        text = read_any_text(path)
        if not text.strip():
            QMessageBox.warning(
                self, "Экспорт", "Не удалось прочитать промпт.",
            )
            return

        base_name = os.path.splitext(os.path.basename(path))[0]
        default_path = os.path.join(
            r["dir"], f"{base_name}.{fmt}"
        )
        target_path, _ = QFileDialog.getSaveFileName(
            self,
            "Сохранить промпт как",
            default_path,
            "Word (*.docx);;Markdown (*.md);;Text (*.txt);;"
            "All files (*)",
        )
        if not target_path:
            return
        target_path = safe_local_path(target_path)

        try:
            if target_path.endswith(".docx"):
                from docx import Document
                doc = Document()
                for line in text.splitlines():
                    doc.add_paragraph(line)
                doc.save(target_path)
            else:
                with open(
                    target_path, "w", encoding="utf-8"
                ) as f:
                    f.write(text)
            QMessageBox.information(
                self, "Экспорт",
                f"Файл сохранён:\n{target_path}",
            )
        except Exception as exc:
            log.exception("Ошибка сохранения промпта: %s", exc)
            QMessageBox.critical(
                self, "Экспорт",
                f"Не удалось сохранить: {exc}",
            )

    # ------------------------------------------------------------------
    # Вложения
    # ------------------------------------------------------------------
    def _open_attachments(self) -> None:
        r = self._selected_row()
        if not r:
            QMessageBox.warning(self, "Записи", "Выберите запись")
            return
        att_dir = os.path.join(r["dir"], "attachments")
        if not os.path.isdir(att_dir):
            QMessageBox.information(
                self, "Вложения",
                "У этой записи нет вложений.",
            )
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(att_dir))

    def _add_attachment_to_session(self) -> None:
        r = self._selected_row()
        if not r:
            QMessageBox.warning(self, "Записи", "Выберите запись")
            return

        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Выберите файлы-вложения",
            os.path.expanduser("~"),
            "Документы (*.txt *.md *.docx *.pdf *.csv *.json);;"
            "Все файлы (*)",
        )
        if not files:
            return

        files = [safe_local_path(f) for f in files]

        att_dir = os.path.join(r["dir"], "attachments")
        os.makedirs(att_dir, exist_ok=True)

        session_json = os.path.join(r["dir"], "session.json")
        meta = read_json_file(session_json) or {}
        current = list(meta.get("attachments", []) or [])

        added = 0
        max_chars = int(
            self._app_cfg.get("attachment_name_max_chars", 50)
        )

        for src in files:
            if not os.path.exists(src):
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
                    dst = os.path.join(
                        att_dir, f"{stem}_{i}{ext}"
                    )
                    i += 1
            try:
                shutil.copy2(src, dst)
                current.append(dst)
                added += 1
            except Exception as exc:
                log.exception(
                    "Ошибка копирования вложения %s: %s", src, exc
                )

        if added:
            meta["attachments"] = current
            if not self._write_json(session_json, meta):
                QMessageBox.critical(
                    self, "Ошибка",
                    "Не удалось обновить session.json",
                )
                return
            QMessageBox.information(
                self, "Вложения",
                f"Добавлено файлов: {added}",
            )
            self.refresh()

    # ------------------------------------------------------------------
    # Обычные действия
    # ------------------------------------------------------------------
    def _selected_row(self) -> Optional[Dict[str, Any]]:
        i = self.table.currentRow()
        if i < 0 or i >= len(self._rows):
            return None
        return self._rows[i]

    def _open_folder(self) -> None:
        r = self._selected_row()
        if not r:
            QMessageBox.warning(self, "Записи", "Выберите запись")
            return
        folder = r["dir"]
        if not os.path.isdir(folder):
            QMessageBox.warning(
                self, "Записи",
                f"Папка не найдена:\n{folder}",
            )
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def _delete_session(self) -> None:
        r = self._selected_row()
        if not r:
            QMessageBox.warning(self, "Записи", "Выберите запись")
            return
        if QMessageBox.question(
            self, "Удалить запись",
            f"Удалить папку записи «{r['name']}» "
            f"со всем содержимым?\n\n"
            f"{r['dir']}\n\nДействие необратимо.",
        ) != QMessageBox.StandardButton.Yes:
            return
        if r["task_id"]:
            self._remove_from_queue(r["task_id"])
        try:
            shutil.rmtree(r["dir"])
            self.refresh()
        except Exception as exc:
            log.exception(
                "Не удалось удалить %s: %s", r["dir"], exc
            )
            QMessageBox.critical(
                self, "Ошибка",
                f"Не удалось удалить: {exc}",
            )

    def _open_tasks_window(self) -> None:
        """Открывает сводное окно «Поручения» по всем записям."""
        try:
            from .tasks_window import TasksWindow
        except ImportError as exc:
            log.exception(
                "Не удалось импортировать окно «Поручения»: %s",
                exc,
            )
            QMessageBox.critical(
                self, "Поручения",
                f"Модуль окна «Поручения» недоступен:\n{exc}",
            )
            return

        try:
            dlg = TasksWindow(
                sessions_root=self.sessions_root,
                config_manager=self.config_manager,
                parent=self,
            )
            dlg.show()
        except Exception as exc:
            log.exception(
                "Не удалось открыть окно «Поручения»: %s", exc,
            )
            QMessageBox.critical(
                self, "Поручения",
                f"Ошибка открытия окна:\n{exc}",
            )

    # ------------------------------------------------------------------
    # Поручения (action items)
    # ------------------------------------------------------------------
    def _open_tasks_editor(self) -> None:
        """Открывает редактор поручений для выбранной записи."""
        r = self._selected_row()
        if not r:
            QMessageBox.warning(self, "Поручения", "Выберите запись")
            return

        session_dir = r.get("dir") or ""
        if not session_dir or not os.path.isdir(session_dir):
            QMessageBox.warning(
                self, "Поручения",
                "Папка записи не найдена.",
            )
            return

        try:
            from .tasks_editor_dialog import TasksEditorDialog
        except ImportError as exc:
            log.exception(
                "Не удалось импортировать редактор поручений: %s",
                exc,
            )
            QMessageBox.critical(
                self, "Поручения",
                f"Модуль редактора поручений недоступен:\n{exc}",
            )
            return

        # --- Справочник сотрудников для выпадающего списка ---
        employees: List[Dict[str, str]] = []
        if self.config_manager is not None:
            try:
                employees = self.config_manager.get_employees()
            except Exception as exc:
                log.warning(
                    "Не удалось прочитать справочник сотрудников: %s",
                    exc,
                )

        dlg = TasksEditorDialog(
            session_dir=session_dir,
            session_name=r.get("name") or "",
            employees=employees,
            parent=self,
        )
        dlg.exec()

    # ------------------------------------------------------------------
    # Справка
    # ------------------------------------------------------------------
    def _show_shortcuts(self) -> None:
        QMessageBox.information(
            self,
            "Горячие клавиши",
            "Ctrl+I        — импорт материалов\n"
            "Ctrl+Shift+Y  — окно синхронизации\n"
            "Ctrl+Shift+S  — синхронизировать выбранную запись\n"
            "Ctrl+Shift+U  — открыть ссылку на сервере\n"
            "Ctrl+Shift+G  — переключить «готово к синхронизации»\n"
            "Ctrl+Shift+V  — смотреть видео\n"
            "Ctrl+Shift+A  — прослушать аудио\n"
            "Ctrl+Shift+T  — просмотреть стенограмму\n"
            "Ctrl+Shift+R  — просмотреть протокол (.docx)\n"
            "Ctrl+M        — создать/редактировать протокол\n"
            "Ctrl+Shift+M  — экспорт протокола в DOCX\n"
            "Ctrl+B        — отправить протокол/summary в Bitrix24\n"
            "Ctrl+E        — редактировать метаданные и перезапустить\n"
            "Ctrl+T        — изменить теги\n"
            "Ctrl+Shift+E  — открыть папку записи\n"
            "Ctrl+P        — изменить summary (Markdown)\n"
            "Ctrl+Shift+P  — просмотр summary\n"
            "Ctrl+D        — открыть промпт DeepSeek\n"
            "Ctrl+Shift+D  — экспорт промпта\n"
            "Ctrl+Alt+D    — сохранить промпт в «Загрузки»\n"
            "Ctrl+R        — перезапустить обработку\n"
            "F5            — обновить список\n"
            "Ctrl+Delete   — удалить запись\n"
            "Ctrl+Shift+K  — редактор поручений записи\n"
            "Ctrl+Shift+L  — сводное окно «Поручения»\n"
            "Ctrl+W        — закрыть окно",
        )