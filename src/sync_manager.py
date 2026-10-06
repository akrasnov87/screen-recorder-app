"""Логика синхронизации записей с удалённым сервером screc-server.

Задачи модуля:
  • Собрать RecordPayload из локального session.json.
  • Опубликовать запись на сервер (POST /api/v1/records).
  • Опционально загрузить видео и аудио как артефакты.
  • Скачать запись с сервера и разложить по локальным папкам.
  • Дельта-синхронизация через /sync/changes.
  • Синхронизация справочников проектов и тегов.

Ключевые особенности:
  • Медиа передаётся как обычные артефакты.
  • Ограничение размера — max_artifact_mb.
  • Умная стратегия скачивания по sha256.
  • Артефакты, которых нет на сервере, НЕ удаляются, если
    запись помечена как черновик (sync_ready=false).
  • Локальный мьютекс на session_dir — publish и download для
    одной папки не выполняются одновременно.

Изменения:
  • Перед загрузкой медиа-файлов выполняется пакетная проверка
    через POST /records/{id}/artifacts/check.
  • Добавлено автосжатие медиа.
  • (КРИТИЧНО 2) _apply_change для artifact_upload не
    перезагружает всю запись.
  • (КРИТИЧНО 3) Индекс record_id → session_dir в sync_state.json.
  • (КРИТИЧНО 4) JSON-конфиги в summary_bb с префиксом-маркером.
  • (КРИТИЧНО 5) Логирование skipped/uploaded артефактов.
  • (НОВОЕ) Флаг sync_ready:
      – build_payload_from_meta передаёт sync_ready на сервер;
      – publish_session сохраняет sync_ready в sync_state.json;
      – _prune_local_artifacts пропускается для черновиков;
      – _apply_change игнорирует изменения для черновиков;
      – локальный мьютекс на session_dir (asyncio.Lock).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import (
    Any, Callable, Dict, Iterable, List, Optional, Tuple,
)
from urllib.parse import quote

from .file_readers import read_json_file
from .logger import get_logger
from .screc_client import ScrecClient, ScrecError
from .video_compressor import (
    CompressionError,
    DEFAULT_AUDIO_BITRATE_KBPS,
    DEFAULT_MIN_VIDEO_BITRATE_KBPS,
    DEFAULT_PRESET,
    compress_video_to_target_size,
)

log = get_logger(__name__)


# Имя файла локального состояния синхронизации.
_SYNC_STATE_FILE = "sync_state.json"

# Файл-маркер, что запись уже опубликована на сервере.
_SYNC_MARKER_FILE = ".sync_published.json"

# Проект для хранения конфигов на сервере (не настоящий проект).
_CONFIG_PROJECT = "_config"
_CONFIG_FOLDER_PROJECTS = "projects"
_CONFIG_FOLDER_TAGS = "tags"

# Префикс-маркер для JSON-конфигов в summary_bb.
# Публичное имя — используется снаружи (screc_client.py).
CONFIG_JSON_PREFIX = "§CONFIG_JSON§\n"
# Обратная совместимость со старым именем.
_CONFIG_JSON_PREFIX = CONFIG_JSON_PREFIX


# Соответствие локальных файлов и kind на сервере.
# Соответствие локальных файлов и kind на сервере.
_ARTIFACT_KINDS: List[Tuple[str, str]] = [
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
    # --- Поручения (action items) ---
    ("action_items", "action_items.json"),
]


# Расширения для видео и аудио.
_VIDEO_EXTS: Tuple[str, ...] = (
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".wmv",
)
_AUDIO_EXTS: Tuple[str, ...] = (
    ".mp3", ".wav", ".m4a", ".aac", ".opus", ".ogg",
)


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------
def _ensure_dirs(path: str) -> None:
    if path:
        os.makedirs(path, exist_ok=True)


def _iso_now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _file_url(path: str) -> str:
    if not path:
        return ""
    abs_path = os.path.abspath(path)
    return "file://" + abs_path


def _parse_date(date_str: str) -> Tuple[str, str, str]:
    if not date_str:
        return "", "", ""
    s = date_str.strip()
    for sep in ("T", " "):
        if sep in s:
            s = s.split(sep, 1)[0]
    parts = s.split("-")
    if len(parts) >= 3:
        y, m, d = parts[0], parts[1], parts[2]
        if y.isdigit() and m.isdigit() and d.isdigit():
            return y, m, d
    return "", "", ""


def _session_folder_name(session_dir: str, meta: Dict[str, Any]) -> str:
    name = (meta.get("name") or "").strip()
    if name:
        return name
    return os.path.basename(session_dir.rstrip("/")) or "record"


def _sha256_file(path: str) -> str:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception as exc:
        log.warning("Не удалось посчитать sha256 для %s: %s", path, exc)
        return ""


def _read_sync_state(session_dir: str) -> Dict[str, Any]:
    path = os.path.join(session_dir, _SYNC_MARKER_FILE)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        log.warning("Не удалось прочитать %s: %s", path, exc)
        return {}


def _write_sync_state(session_dir: str, data: Dict[str, Any]) -> None:
    _ensure_dirs(session_dir)
    path = os.path.join(session_dir, _SYNC_MARKER_FILE)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as exc:
        log.error("Не удалось записать %s: %s", path, exc)


def is_record_published(session_dir: str) -> bool:
    state = _read_sync_state(session_dir)
    return bool(state.get("record_id"))


def get_record_id(session_dir: str) -> str:
    state = _read_sync_state(session_dir)
    return str(state.get("record_id") or "")


def _is_sync_ready(session_dir: str) -> bool:
    """
    Читает флаг sync_ready из session.json.

    Если файла нет или флаг не выставлен — запись считается
    черновиком (sync_ready=False).
    """
    meta = read_json_file(
        os.path.join(session_dir, "session.json")
    ) or {}
    return bool(meta.get("sync_ready", False))


# ---------------------------------------------------------------------------
# Сборка payload
# ---------------------------------------------------------------------------
def build_payload_from_meta(
    session_dir: str,
    meta: Dict[str, Any],
    *,
    send_video_link: bool = True,
) -> Dict[str, Any]:
    date_str = (meta.get("date") or "").strip()
    year, month, _day = _parse_date(date_str)
    if not year or not month:
        base = os.path.basename(session_dir.rstrip("/"))
        parts = base.split("_")
        if parts and len(parts[0]) >= 7 and parts[0][4] == "-":
            y2, m2, _ = _parse_date(parts[0])
            year = year or y2
            month = month or m2
    if not year:
        year = datetime.now().strftime("%Y")
    if not month:
        month = datetime.now().strftime("%m")

    project = (meta.get("project") or "").strip() or "Default"
    folder_name = _session_folder_name(session_dir, meta)

    payload: Dict[str, Any] = {
        "project": project,
        "year": str(year),
        "month": str(month).zfill(2),
        "folder_name": folder_name,
    }

    optional_str = (
        "name", "description", "comment", "source",
        "date", "time",
        "summary_bb", "prompt", "prompt_name",
        "name_template", "name_abbr",
    )
    for key in optional_str:
        v = meta.get(key)
        if v is not None and str(v).strip() != "":
            payload[key] = v if isinstance(v, str) else str(v)

    if "source" not in payload:
        payload["source"] = "record"

    time_val = (meta.get("time") or "").strip()
    if time_val:
        payload["time"] = time_val

    for key in (
        "is_scrum",
        "prompt_edited",
        "generate_summary",
        "generate_deepseek_prompt",
        "include_name_in_prompt",
        "include_project_in_prompt",
        "include_comment_in_prompt",
        "include_tags_in_prompt",
    ):
        if key in meta:
            payload[key] = bool(meta[key])

    # --- Флаг готовности к синхронизации ---
    if "sync_ready" in meta:
        payload["sync_ready"] = bool(meta["sync_ready"])

    raw_tags = meta.get("tags")
    if isinstance(raw_tags, list):
        tags: List[str] = []
        for t in raw_tags:
            if isinstance(t, str) and t.strip():
                tags.append(t.strip())
            elif isinstance(t, dict):
                name = str(t.get("name") or "").strip()
                if name:
                    tags.append(name)
        if tags:
            payload["tags"] = tags

    if send_video_link:
        video_path = (meta.get("video_path") or "").strip()
        if not video_path:
            for ext in (".mp4", ".mkv", ".mov", ".avi", ".webm",
                        ".flv", ".wmv", ".mp3", ".wav", ".m4a",
                        ".aac", ".opus", ".ogg"):
                candidate = os.path.join(session_dir, f"video{ext}")
                if os.path.isfile(candidate):
                    video_path = candidate
                    break

        if video_path and os.path.isfile(video_path):
            payload["video_url"] = _file_url(video_path)
            try:
                payload["video_size"] = os.path.getsize(video_path)
            except OSError:
                pass

    return payload


def _find_media_in_session(session_dir: str) -> Dict[str, str]:
    """Возвращает {"video": path, "audio": path} для найденных медиа."""
    result: Dict[str, str] = {}

    for ext in _VIDEO_EXTS:
        candidate = os.path.join(session_dir, f"video{ext}")
        if os.path.isfile(candidate):
            result["video"] = candidate
            break

    for ext in _AUDIO_EXTS:
        candidate = os.path.join(session_dir, f"video{ext}")
        if os.path.isfile(candidate):
            result["audio"] = candidate
            break

    return result


def collect_artifacts(
    session_dir: str,
    *,
    include_media: bool = False,
    max_artifact_mb: int = 50,
) -> Tuple[
    List[Tuple[str, str]],
    List[Dict[str, Any]],
]:
    """Возвращает (small_artifacts, media_artifacts)."""
    small: List[Tuple[str, str]] = []
    media: List[Dict[str, Any]] = []
    seen_kinds: Dict[str, bool] = {}

    for kind, filename in _ARTIFACT_KINDS:
        if seen_kinds.get(kind):
            continue
        full = os.path.join(session_dir, filename)
        if os.path.isfile(full):
            small.append((kind, filename))
            seen_kinds[kind] = True

    att_dir = os.path.join(session_dir, "attachments")
    if os.path.isdir(att_dir):
        try:
            for name in sorted(os.listdir(att_dir)):
                full = os.path.join(att_dir, name)
                if not os.path.isfile(full):
                    continue

                name_bytes = len(name.encode("utf-8"))
                if name_bytes > 200:
                    log.warning(
                        "Вложение %r: имя длинное (%d байт UTF-8) — "
                        "будет автоматически сокращено перед "
                        "отправкой на сервер.",
                        name, name_bytes,
                    )

                small.append(
                    ("attachment", os.path.join("attachments", name))
                )
        except OSError as exc:
            log.warning("Не удалось прочитать %s: %s", att_dir, exc)

    if include_media:
        max_bytes = max_artifact_mb * 1024 * 1024
        found = _find_media_in_session(session_dir)

        for kind in ("video", "audio"):
            path = found.get(kind)
            if not path:
                continue
            try:
                size = os.path.getsize(path)
            except OSError:
                continue

            needs_compression = (
                max_bytes > 0 and size > max_bytes
            )

            media.append({
                "kind": kind,
                "filename": os.path.basename(path),
                "path": path,
                "size_bytes": size,
                "needs_compression": needs_compression,
                "target_bytes": max_bytes if needs_compression else 0,
            })

    return small, media

# ---------------------------------------------------------------------------
# Публичные ссылки на сервер (для браузера, /view/**)
# ---------------------------------------------------------------------------
# Допустимые блоки для /view/...?blocks=
# Должны совпадать с DOCS.md §6.7.
PUBLIC_VIEW_BLOCKS: Tuple[str, ...] = (
    "video",
    "audio",
    "transcript",
    "protocol",
    "summary",
)


def get_record_path(session_dir: str) -> str:
    """
    Читает относительный путь записи на сервере из
    .sync_published.json.

    Путь имеет вид "project/year/month/folder_name" и
    используется для построения публичной ссылки /view/...

    Возвращает "" если запись ещё не публиковалась или
    path отсутствует (старые версии state-файла).
    """
    state = _read_sync_state(session_dir)
    return str(state.get("path") or "").strip().strip("/")


def split_record_path(path: str) -> Tuple[str, str, str, str]:
    """
    Разбивает путь "project/year/month/folder_name" на
    4 компонента.

    Учитывает, что folder_name может содержать пробелы и
    кириллицу, но не содержит "/".

    Returns:
        (project, year, month, folder_name)
        Все компоненты — строки. При ошибке разбора
        возвращается ("", "", "", "").
    """
    if not path:
        return "", "", "", ""

    parts = path.strip("/").split("/", 3)
    if len(parts) < 4:
        log.warning(
            "split_record_path: некорректный path=%r "
            "(нужно 4 сегмента)", path,
        )
        return "", "", "", ""

    return parts[0], parts[1], parts[2], parts[3]


def build_public_view_url(
    base_url: str,
    record_path: str,
    record_id: str,
    *,
    blocks: Optional[Iterable[str]] = None,
) -> str:
    """
    Собирает публичную ссылку на HTML-страницу записи на сервере.

    Args:
        base_url:    базовый URL сервера (например,
                     "http://localhost:8000").
        record_path: путь "project/year/month/folder_name"
                     (см. get_record_path).
        record_id:   UUID записи на сервере.
        blocks:      список блоков для отображения
                     ("video", "audio", "transcript",
                     "protocol", "summary"). None или пусто —
                     не добавлять параметр (сервер покажет all).

    Returns:
        Готовая ссылка или "" если чего-то не хватает.
    """
    if not base_url or not record_path or not record_id:
        return ""

    project, year, month, folder_name = split_record_path(record_path)
    if not (project and year and month and folder_name):
        return ""

    base = base_url.rstrip("/")
    url = (
        f"{base}/view/"
        f"{quote(project, safe='')}/"
        f"{quote(year, safe='')}/"
        f"{quote(month, safe='')}/"
        f"{quote(folder_name, safe='')}"
        f"?id={quote(record_id, safe='')}"
    )

    if blocks:
        valid = [
            b.strip().lower() for b in blocks
            if b and b.strip().lower() in PUBLIC_VIEW_BLOCKS
        ]
        if valid:
            url += "&blocks=" + ",".join(valid)

    return url


def build_public_artifact_url(
    base_url: str,
    record_path: str,
    record_id: str,
    filename: str,
    *,
    download: bool = False,
) -> str:
    """
    Собирает публичную ссылку на конкретный артефакт записи.

    Args:
        base_url:    базовый URL сервера.
        record_path: путь "project/year/month/folder_name".
        record_id:   UUID записи.
        filename:    имя файла-артефакта (basename, как в
                     _meta.json).
        download:    если True — добавить &download=true.

    Returns:
        Готовая ссылка или "".
    """
    if not (base_url and record_path and record_id and filename):
        return ""

    project, year, month, folder_name = split_record_path(record_path)
    if not (project and year and month and folder_name):
        return ""

    base = base_url.rstrip("/")
    url = (
        f"{base}/view/"
        f"{quote(project, safe='')}/"
        f"{quote(year, safe='')}/"
        f"{quote(month, safe='')}/"
        f"{quote(folder_name, safe='')}"
        f"/artifact/{quote(os.path.basename(filename), safe='')}"
        f"?id={quote(record_id, safe='')}"
    )
    if download:
        url += "&download=true"
    return url


def build_public_url_for_session(
    session_dir: str,
    sync_settings: Dict[str, Any],
    *,
    blocks: Optional[Iterable[str]] = None,
) -> str:
    """
    Удобная обёртка: собирает публичную ссылку для сессии,
    читая base_url из sync_settings, path и record_id — из
    .sync_published.json.

    Returns:
        Готовая ссылка или "" (если запись не опубликована
        или нет base_url).
    """
    base_url = str(
        (sync_settings or {}).get("base_url") or ""
    ).strip()
    if not base_url:
        return ""

    record_id = get_record_id(session_dir)
    if not record_id:
        return ""

    record_path = get_record_path(session_dir)
    if not record_path:
        return ""

    return build_public_view_url(
        base_url, record_path, record_id, blocks=blocks,
    )

# ---------------------------------------------------------------------------
# Менеджер синхронизации
# ---------------------------------------------------------------------------
class SyncManager:
    """
    Управляет синхронизацией записей и конфигов с сервером.

    Локальный мьютекс на session_dir (class-level asyncio.Lock)
    защищает от гонки publish/download для одной папки.
    """

    # --- Локальные мьютексы на session_dir (publish vs download) ---
    # Формат: {abspath(session_dir): (loop, asyncio.Lock)}
    #
    # ВАЖНО: asyncio.Lock привязывается к event loop при первом
    # использовании. У нас операции синхронизации выполняются
    # в разных loop'ах (фоновый loop приложения + отдельные loop'ы
    # в QThread-воркерах), поэтому храним пару (loop, lock) и
    # пересоздаём лок при смене loop'а.
    # Для защиты самого словаря используем threading.Lock —
    # asyncio.Lock здесь нельзя, он тоже привязывается к loop.
    _session_locks: Dict[str, tuple] = {}
    _session_locks_guard = threading.Lock()

    @classmethod
    async def _get_session_lock(
        cls, session_dir: str
    ) -> asyncio.Lock:
        """
        Возвращает asyncio.Lock для конкретной папки сессии.

        Если лок уже создан в текущем event loop — возвращаем
        его. Если loop сменился (например, publish был в фоновом
        loop приложения, а download запущен из QThread-воркера) —
        создаём новый лок.
        """
        key = os.path.abspath(session_dir)
        current_loop = asyncio.get_running_loop()

        with cls._session_locks_guard:
            entry = cls._session_locks.get(key)
            if entry is not None:
                stored_loop, stored_lock = entry
                if stored_loop is current_loop:
                    return stored_lock

            lock = asyncio.Lock()
            cls._session_locks[key] = (current_loop, lock)
            return lock

    def __init__(
        self,
        sessions_root: str,
        sync_settings: Dict[str, Any],
        *,
        config_manager=None,
    ) -> None:
        self.sessions_root = sessions_root
        self.settings = sync_settings or {}
        self.config_manager = config_manager

        self._state_path = os.path.join(
            sessions_root, _SYNC_STATE_FILE
        )

        log.debug(
            "SyncManager создан: base_url=%r, send_media=%s, "
            "auto_upload=%s, auto_pull=%s, delete_local_on_delete=%s, "
            "force_overwrite=%s, use_hash_check=%s, "
            "compress_media=%s",
            self.settings.get("base_url"),
            self.settings.get("send_media_to_server"),
            self.settings.get("auto_upload_after_processing"),
            self.settings.get("auto_pull_enabled"),
            self.settings.get("delete_local_on_server_delete"),
            self.settings.get("force_overwrite_on_download"),
            self.settings.get("use_hash_check"),
            self.settings.get("compress_media_if_too_large"),
        )

    # ------------------------------------------------------------------
    # Клиент
    # ------------------------------------------------------------------
    def _make_client(self) -> ScrecClient:
        return ScrecClient(
            base_url=self.settings.get("base_url", ""),
            api_key=self.settings.get("api_key", ""),
            connect_timeout=float(
                self.settings.get("connect_timeout", 15)
            ),
            read_timeout=float(
                self.settings.get("read_timeout", 120)
            ),
        )

    def is_configured(self) -> bool:
        return bool(
            self.settings.get("base_url")
            and self.settings.get("api_key")
        )

    def _use_hash_check(self) -> bool:
        return bool(self.settings.get("use_hash_check", True))

    def _use_compression(self) -> bool:
        return bool(
            self.settings.get("compress_media_if_too_large", True)
        )

    # ------------------------------------------------------------------
    # Локальное состояние
    # ------------------------------------------------------------------
    def load_state(self) -> Dict[str, Any]:
        if not os.path.isfile(self._state_path):
            return {
                "last_synced_revision": 0,
                "record_index": {},
            }
        try:
            with open(self._state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {
                    "last_synced_revision": 0,
                    "record_index": {},
                }
            data.setdefault("last_synced_revision", 0)
            data.setdefault("record_index", {})
            if not isinstance(data.get("record_index"), dict):
                data["record_index"] = {}
            return data
        except Exception as exc:
            log.warning(
                "Не удалось прочитать sync_state: %s", exc
            )
            return {
                "last_synced_revision": 0,
                "record_index": {},
            }

    def save_state(self, state: Dict[str, Any]) -> None:
        _ensure_dirs(self.sessions_root)
        tmp = self._state_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self._state_path)
        except Exception as exc:
            log.error("Не удалось сохранить sync_state: %s", exc)

    def get_last_revision(self) -> int:
        state = self.load_state()
        try:
            return int(state.get("last_synced_revision", 0))
        except (TypeError, ValueError):
            return 0

    def set_last_revision(self, revision: int) -> None:
        state = self.load_state()
        state["last_synced_revision"] = int(revision)
        state["updated_at"] = _iso_now()
        self.save_state(state)

    # ------------------------------------------------------------------
    # Индекс record_id → session_dir
    # ------------------------------------------------------------------
    def _update_record_index(
        self, record_id: str, session_dir: str
    ) -> None:
        if not record_id or not session_dir:
            return
        try:
            state = self.load_state()
            index = state.setdefault("record_index", {})
            index[str(record_id)] = os.path.abspath(session_dir)
            state["updated_at"] = _iso_now()
            self.save_state(state)
        except Exception as exc:
            log.warning(
                "Не удалось обновить индекс record_id → session_dir: %s",
                exc,
            )

    def _remove_from_record_index(self, record_id: str) -> None:
        if not record_id:
            return
        try:
            state = self.load_state()
            index = state.setdefault("record_index", {})
            if record_id in index:
                del index[record_id]
                state["updated_at"] = _iso_now()
                self.save_state(state)
        except Exception as exc:
            log.warning(
                "Не удалось удалить %s из индекса: %s",
                record_id, exc,
            )

    def _lookup_in_record_index(self, record_id: str) -> str:
        if not record_id:
            return ""
        try:
            state = self.load_state()
            index = state.get("record_index") or {}
            path = index.get(record_id) or ""
            if path and os.path.isdir(path):
                return path
            if path:
                log.debug(
                    "Индекс указывает на несуществующую папку: %s "
                    "(record_id=%s) — удаляем из индекса",
                    path, record_id,
                )
                self._remove_from_record_index(record_id)
        except Exception as exc:
            log.warning(
                "Не удалось прочитать индекс по %s: %s", record_id, exc
            )
        return ""

    # ------------------------------------------------------------------
    # Сжатие медиа
    # ------------------------------------------------------------------
    async def _compress_media_if_needed(
        self,
        item: Dict[str, Any],
        *,
        progress_cb: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> bool:
        if not item.get("needs_compression"):
            return True

        path = item.get("path") or ""
        target_bytes = int(item.get("target_bytes") or 0)
        kind = item.get("kind") or ""

        if not path or not os.path.isfile(path):
            log.warning("Сжатие: файл не найден: %s", path)
            return False

        if target_bytes <= 0:
            log.warning(
                "Сжатие: некорректный целевой размер %d для %s",
                target_bytes, path,
            )
            return False

        min_video_kbps = int(
            self.settings.get(
                "compression_min_video_bitrate_kbps",
                DEFAULT_MIN_VIDEO_BITRATE_KBPS,
            )
        )
        audio_kbps = int(
            self.settings.get(
                "compression_audio_bitrate_kbps",
                DEFAULT_AUDIO_BITRATE_KBPS,
            )
        )
        preset = str(
            self.settings.get("compression_preset", DEFAULT_PRESET)
        ).strip() or DEFAULT_PRESET

        if progress_cb:
            try:
                progress_cb(
                    f"Сжатие {kind}: "
                    f"{os.path.basename(path)} "
                    f"({item.get('size_bytes', 0) // (1024 * 1024)} МБ → "
                    f"{target_bytes // (1024 * 1024)} МБ)…"
                )
            except Exception:
                pass

        try:
            await compress_video_to_target_size(
                src_path=path,
                target_bytes=target_bytes,
                min_video_bitrate_kbps=min_video_kbps,
                audio_bitrate_kbps=audio_kbps,
                preset=preset,
                progress_cb=progress_cb,
                cancel_event=cancel_event,
            )
        except CompressionError as exc:
            log.error(
                "Сжатие %s (%s) не удалось: %s",
                path, kind, exc,
            )
            return False
        except Exception as exc:
            log.exception(
                "Неожиданная ошибка при сжатии %s: %s",
                path, exc,
            )
            return False

        try:
            new_size = os.path.getsize(path)
            item["size_bytes"] = new_size
            item["needs_compression"] = False
            log.info(
                "Сжатие %s завершено: новый размер %d МБ",
                path, new_size // (1024 * 1024),
            )
        except OSError:
            pass

        return True

    # ------------------------------------------------------------------
    # Публикация (upload)
    # ------------------------------------------------------------------
    async def publish_session(
        self,
        session_dir: str,
        *,
        progress_cb: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        """
        Публикует одну запись на сервер.

        Защищено локальным мьютексом на session_dir — параллельный
        download_record для этой же папки будет ждать.
        """
        lock = await self._get_session_lock(session_dir)
        async with lock:
            return await self._publish_session_impl(
                session_dir, progress_cb=progress_cb
            )

    async def _publish_session_impl(
        self,
        session_dir: str,
        *,
        progress_cb: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        if not self.is_configured():
            raise ScrecError(
                "Синхронизация не настроена: укажите base_url и api_key "
                "в Настройки → Синхронизация."
            )

        if not os.path.isdir(session_dir):
            raise ScrecError(f"Папка сессии не найдена: {session_dir}")

        session_json = os.path.join(session_dir, "session.json")
        meta = read_json_file(session_json) or {}

        # --- Проверка флага sync_ready ---
        if not meta.get("sync_ready", False):
            log.warning(
                "publish_session: запись %s не помечена как «готова "
                "к синхронизации» (sync_ready=false). Публикация "
                "выполняется принудительно (вызов из UI), но "
                "рекомендуется сначала поставить галочку.",
                os.path.basename(session_dir),
            )

        if progress_cb:
            progress_cb(f"Сбор метаданных: {os.path.basename(session_dir)}")

        payload = build_payload_from_meta(
            session_dir, meta,
            send_video_link=bool(
                self.settings.get("send_video_link", True)
            ),
        )

        include_media = bool(
            self.settings.get("send_media_to_server", False)
        )
        max_artifact_mb = int(
            self.settings.get("max_artifact_mb", 50)
        )

        small_artifacts, media_artifacts = collect_artifacts(
            session_dir,
            include_media=include_media,
            max_artifact_mb=max_artifact_mb,
        )

        small_artifact_hashes: Dict[str, str] = {}
        if small_artifacts:
            for kind, filename in small_artifacts:
                full_path = os.path.join(session_dir, filename)
                if os.path.isfile(full_path):
                    sha = _sha256_file(full_path)
                    if sha:
                        small_artifact_hashes[os.path.basename(filename)] = sha

        log.info(
            "SyncManager.publish_session: session=%s, project=%r, "
            "folder=%r, sync_ready=%s, small_artifacts=%d, "
            "media_artifacts=%d (include_media=%s, max=%d МБ, "
            "hashes=%d, compress=%s)",
            os.path.basename(session_dir),
            payload.get("project"), payload.get("folder_name"),
            meta.get("sync_ready", False),
            len(small_artifacts), len(media_artifacts),
            include_media, max_artifact_mb,
            len(small_artifact_hashes),
            self._use_compression(),
        )

        if progress_cb:
            names = ", ".join(a[0] for a in small_artifacts) or "—"
            media_names = ", ".join(
                a["kind"] for a in media_artifacts
            ) or "—"
            progress_cb(
                f"Отправка метаданных: {payload['folder_name']} "
                f"(текстовых: {len(small_artifacts)}: {names}; "
                f"медиа: {len(media_artifacts)}: {media_names})"
            )

        media_uploaded: List[Dict[str, Any]] = []
        media_skipped: List[Dict[str, Any]] = []
        media_compressed: List[Dict[str, Any]] = []

        # Сохраняем исходные размеры ДО сжатия — иначе
        # int(size_mb * 1024 * 1024) даст потерю точности.
        original_sizes: Dict[str, int] = {}
        for item in media_artifacts:
            if item.get("needs_compression"):
                original_sizes[item["filename"]] = int(
                    item.get("size_bytes") or 0
                )

        if include_media and media_artifacts and self._use_compression():
            for idx, item in enumerate(media_artifacts, start=1):
                if not item.get("needs_compression"):
                    continue

                kind = item["kind"]
                filename = item["filename"]
                size_mb = item["size_bytes"] / 1024 / 1024
                target_mb = item["target_bytes"] / 1024 / 1024

                if progress_cb:
                    progress_cb(
                        f"Сжатие {kind} [{idx}/{len(media_artifacts)}]: "
                        f"{filename} ({size_mb:.1f} МБ → "
                        f"{target_mb:.1f} МБ)…"
                    )

                log.info(
                    "Автосжатие медиа %s (%s): %.1f МБ → %.1f МБ",
                    filename, kind, size_mb, target_mb,
                )

                ok = await self._compress_media_if_needed(
                    item,
                    progress_cb=progress_cb,
                )
                if ok:
                    media_compressed.append({
                        "kind": kind,
                        "filename": filename,
                        "original_size_bytes": original_sizes.get(
                            filename, 0
                        ),
                        "new_size_bytes": item.get("size_bytes", 0),
                    })
                else:
                    log.warning(
                        "Сжатие %s (%s) не удалось — файл будет "
                        "пропущен", filename, kind,
                    )
                    media_skipped.append({
                        "kind": kind,
                        "filename": filename,
                        "reason": "compression_failed",
                    })

        async with self._make_client() as client:
            result = await client.publish_record(
                payload=payload,
                artifacts=small_artifacts,
                artifact_hashes=small_artifact_hashes,
                artifact_dir=session_dir,
            )

            record_id = result.get("id") or ""

            skipped_small = result.get("skipped_artifacts") or []
            uploaded_small = result.get("uploaded_artifacts") or []
            if skipped_small or uploaded_small:
                log.info(
                    "publish_session: текстовые артефакты — "
                    "uploaded=%d, skipped=%d",
                    len(uploaded_small), len(skipped_small),
                )
                for item in skipped_small:
                    if isinstance(item, dict):
                        fname = item.get("filename") or "?"
                        reason = item.get("reason") or "—"
                        log.info(
                            "publish_session: текстовый артефакт "
                            "пропущен сервером: %s (reason=%s)",
                            fname, reason,
                        )

            if record_id and media_artifacts:
                media_items: List[Dict[str, Any]] = []
                for item in media_artifacts:
                    kind = item["kind"]
                    filename = item["filename"]
                    full_path = item["path"]

                    if any(
                        s.get("filename") == filename
                        and s.get("reason") == "compression_failed"
                        for s in media_skipped
                    ):
                        continue

                    if not os.path.isfile(full_path):
                        log.warning(
                            "Медиафайл исчез: %s", full_path
                        )
                        media_skipped.append({
                            "kind": kind,
                            "filename": filename,
                            "reason": "not found",
                        })
                        continue

                    try:
                        size_bytes = os.path.getsize(full_path)
                    except OSError:
                        size_bytes = 0
                    sha = _sha256_file(full_path)
                    media_items.append({
                        "kind": kind,
                        "filename": filename,
                        "path": full_path,
                        "size_bytes": size_bytes,
                        "sha256": sha,
                    })

                skip_flags: Dict[str, bool] = {}
                if self._use_hash_check() and media_items:
                    if progress_cb:
                        progress_cb(
                            f"Проверка хэшей медиа: "
                            f"{len(media_items)} файлов"
                        )
                    try:
                        check_payload = [
                            {
                                "filename": it["filename"],
                                "sha256": it["sha256"],
                            }
                            for it in media_items
                            if it["sha256"]
                        ]
                        if check_payload:
                            check_resp = (
                                await client.check_artifacts_batch(
                                    record_id, check_payload
                                )
                            )
                            for item in check_resp.get("results") or []:
                                fname = item.get("filename") or ""
                                if fname:
                                    skip_flags[fname] = bool(
                                        item.get("skip")
                                    )
                            skipped = sum(
                                1 for v in skip_flags.values() if v
                            )
                            log.info(
                                "Пакетная проверка медиа: "
                                "проверено=%d, skip=%d, upload=%d",
                                len(skip_flags), skipped,
                                len(skip_flags) - skipped,
                            )
                    except ScrecError as exc:
                        log.warning(
                            "Пакетная проверка медиа не удалась (%s) — "
                            "отправим все файлы как обычно", exc,
                        )

                for idx, item in enumerate(media_items, start=1):
                    kind = item["kind"]
                    filename = item["filename"]
                    full_path = item["path"]
                    size_bytes = item["size_bytes"]
                    sha = item["sha256"]
                    size_mb = size_bytes / 1024 / 1024

                    if skip_flags.get(filename):
                        log.info(
                            "SyncManager: медиа %s (%s) пропущено — "
                            "хэш совпал (экономия %.1f МБ трафика)",
                            filename, kind, size_mb,
                        )
                        media_uploaded.append({
                            "kind": kind,
                            "filename": filename,
                            "size_bytes": size_bytes,
                            "skipped": True,
                            "reason": "sha256_match",
                        })
                        continue

                    if progress_cb:
                        progress_cb(
                            f"Загрузка {kind} "
                            f"[{idx}/{len(media_items)}]: "
                            f"{filename} ({size_mb:.1f} МБ)…"
                        )

                    try:
                        res = await client.upload_artifact_path(
                            record_id=record_id,
                            kind=kind,
                            file_path=full_path,
                            skip_if_hash_matches=self._use_hash_check(),
                        )
                        if res.get("skipped"):
                            log.info(
                                "SyncManager: медиа %s (%s) пропущено "
                                "сервером (reason: %s)",
                                filename, kind, res.get("reason"),
                            )
                            media_uploaded.append({
                                "kind": kind,
                                "filename": filename,
                                "size_bytes": size_bytes,
                                "skipped": True,
                                "reason": res.get("reason", ""),
                            })
                        else:
                            log.info(
                                "SyncManager: медиа %s (%s, %.1f МБ) "
                                "загружено в record %s",
                                filename, kind, size_mb, record_id,
                            )
                            media_uploaded.append({
                                "kind": kind,
                                "filename": filename,
                                "size_bytes": size_bytes,
                                "skipped": False,
                            })
                    except ScrecError as exc:
                        log.error(
                            "SyncManager: не удалось загрузить %s: %s",
                            filename, exc,
                        )
                        media_skipped.append({
                            "kind": kind,
                            "filename": filename,
                            "reason": str(exc),
                        })

        if record_id:
            state = _read_sync_state(session_dir)
            state.update({
                "record_id": record_id,
                "path": result.get("path", ""),
                "revision": result.get("revision", 0),
                "action": result.get("action", "create"),
                "published_at": _iso_now(),
                "folder_name": payload.get("folder_name", ""),
                "project": payload.get("project", ""),
                "sync_ready": bool(meta.get("sync_ready", False)),
                "media_uploaded": [
                    m["filename"] for m in media_uploaded
                    if not m.get("skipped")
                ],
            })
            _write_sync_state(session_dir, state)

            self._update_record_index(record_id, session_dir)

            log.info(
                "SyncManager: запись опубликована, record_id=%s "
                "(revision=%s, action=%s, sync_ready=%s, "
                "media_uploaded=%d, media_compressed=%d)",
                record_id, result.get("revision"),
                result.get("action"),
                meta.get("sync_ready", False),
                len(media_uploaded),
                len(media_compressed),
            )

        if (self.settings.get("delete_local_media_after_media_upload")
                and media_uploaded):
            self._maybe_delete_uploaded_media(
                session_dir,
                [m for m in media_uploaded if not m.get("skipped")],
            )

        if self.settings.get("allow_delete_local_media_after_upload"):
            self._maybe_delete_local_media(session_dir, meta)

        if progress_cb:
            progress_cb(
                f"Готово: {result.get('action', 'ok')} "
                f"(id={record_id or '—'}, "
                f"медиа: {len(media_uploaded)} загружено, "
                f"{len(media_skipped)} пропущено, "
                f"{len(media_compressed)} сжато)"
            )

        return {
            "action": result.get("action", "create"),
            "record_id": record_id,
            "revision": result.get("revision", 0),
            "path": result.get("path", ""),
            "media_uploaded": media_uploaded,
            "media_skipped": media_skipped,
            "media_compressed": media_compressed,
            "skipped_artifacts": result.get("skipped_artifacts", []),
            "uploaded_artifacts": result.get("uploaded_artifacts", []),
        }

    # ------------------------------------------------------------------
    # Публикация одной записи с явными параметрами
    # ------------------------------------------------------------------
    async def sync_one(
        self,
        session_dir: str,
        *,
        include_media: Optional[bool] = None,
        send_video_link: Optional[bool] = None,
        delete_media_after_upload: Optional[bool] = None,
        progress_cb: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        old_settings = dict(self.settings)

        try:
            if include_media is not None:
                self.settings["send_media_to_server"] = bool(include_media)
            if send_video_link is not None:
                self.settings["send_video_link"] = bool(send_video_link)
            if delete_media_after_upload is not None:
                self.settings["delete_local_media_after_media_upload"] = bool(
                    delete_media_after_upload
                )

            log.info(
                "SyncManager.sync_one: %s, include_media=%s, "
                "send_video_link=%s, delete_media=%s",
                os.path.basename(session_dir),
                self.settings.get("send_media_to_server"),
                self.settings.get("send_video_link"),
                self.settings.get("delete_local_media_after_media_upload"),
            )

            result = await self.publish_session(
                session_dir, progress_cb=progress_cb
            )
            result["include_media"] = bool(
                self.settings.get("send_media_to_server", False)
            )
            result["send_video_link"] = bool(
                self.settings.get("send_video_link", True)
            )
            result["delete_media_after_upload"] = bool(
                self.settings.get(
                    "delete_local_media_after_media_upload", False
                )
            )
            return result
        finally:
            self.settings = old_settings

    def _maybe_delete_uploaded_media(
        self,
        session_dir: str,
        uploaded: List[Dict[str, Any]],
    ) -> None:
        for item in uploaded:
            filename = item.get("filename") or ""
            if not filename:
                continue
            full = os.path.join(session_dir, filename)
            if not os.path.isfile(full):
                continue
            try:
                size = os.path.getsize(full)
                os.remove(full)
                log.warning(
                    "SyncManager: локальное медиа (%s, %.1f МБ) "
                    "удалено после загрузки на сервер",
                    filename, size / 1024 / 1024,
                )
            except Exception as exc:
                log.warning(
                    "Не удалось удалить %s: %s", full, exc
                )

    def _maybe_delete_local_media(
        self, session_dir: str, meta: Dict[str, Any]
    ) -> None:
        video_path = (meta.get("video_path") or "").strip()
        if not video_path or not os.path.isfile(video_path):
            return
        try:
            size = os.path.getsize(video_path)
            os.remove(video_path)
            log.warning(
                "SyncManager: локальный медиафайл удалён после "
                "публикации (%.1f МБ): %s",
                size / 1024 / 1024, video_path,
            )
        except Exception as exc:
            log.warning(
                "SyncManager: не удалось удалить %s: %s",
                video_path, exc,
            )

    # ------------------------------------------------------------------
    # Скачивание только медиа
    # ------------------------------------------------------------------
    async def download_media_only(
        self,
        session_dir: str,
        *,
        progress_cb: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        if not self.is_configured():
            raise ScrecError(
                "Синхронизация не настроена: укажите base_url и api_key."
            )
        if not os.path.isdir(session_dir):
            raise ScrecError(f"Папка сессии не найдена: {session_dir}")

        record_id = get_record_id(session_dir)
        if not record_id:
            raise ScrecError(
                "Запись не опубликована на сервере — нечего скачивать."
            )

        if progress_cb:
            progress_cb(
                f"Запрос метаданных записи {record_id} с сервера"
            )

        downloaded: List[Dict[str, Any]] = []
        missing: List[Dict[str, Any]] = []

        lock = await self._get_session_lock(session_dir)
        async with lock:
            async with self._make_client() as client:
                record = await client.get_record(record_id)
                artifacts = record.get("artifacts") or []

                media_items: List[Dict[str, Any]] = []
                for art in artifacts:
                    kind = str(art.get("kind") or "")
                    if kind in ("video", "audio"):
                        media_items.append(art)

                if not media_items:
                    log.info(
                        "download_media_only: на сервере нет медиа для "
                        "record=%s (артефактов всего: %d)",
                        record_id, len(artifacts),
                    )
                    for kind in ("video", "audio"):
                        missing.append({
                            "kind": kind,
                            "filename": f"video.{kind}",
                        })
                    return {
                        "downloaded": [],
                        "missing": missing,
                        "session_dir": session_dir,
                        "record_id": record_id,
                    }

                for idx, art in enumerate(media_items, start=1):
                    kind = str(art.get("kind") or "")
                    filename = str(art.get("filename") or "")
                    if not filename:
                        continue

                    target_rel = os.path.basename(filename)
                    target_path = os.path.join(session_dir, target_rel)

                    size_mb = int(art.get("size") or 0) / 1024 / 1024
                    if progress_cb:
                        progress_cb(
                            f"Скачивание {kind} "
                            f"[{idx}/{len(media_items)}]: "
                            f"{filename} ({size_mb:.1f} МБ)…"
                        )

                    if self._should_skip_download(
                        target_path, art, force_overwrite=False
                    ):
                        log.info(
                            "download_media_only: %s уже актуален "
                            "(совпадает по sha256/size)", target_path,
                        )
                        downloaded.append({
                            "kind": kind,
                            "filename": filename,
                            "local_path": target_path,
                            "skipped": True,
                        })
                        continue

                    try:
                        await client.download_artifact(
                            record_id, filename, target_path,
                            progress_cb=None,
                        )
                        downloaded.append({
                            "kind": kind,
                            "filename": filename,
                            "local_path": target_path,
                            "skipped": False,
                        })
                        log.info(
                            "download_media_only: %s скачан в %s "
                            "(%.1f МБ)", filename, target_path, size_mb,
                        )
                    except ScrecError as exc:
                        log.error(
                            "download_media_only: не удалось скачать "
                            "%s: %s", filename, exc,
                        )
                        missing.append({
                            "kind": kind,
                            "filename": filename,
                            "reason": str(exc),
                        })

        if progress_cb:
            progress_cb(
                f"Готово: скачано {len(downloaded)} медиафайлов"
            )

        return {
            "downloaded": downloaded,
            "missing": missing,
            "session_dir": session_dir,
            "record_id": record_id,
        }

    # ------------------------------------------------------------------
    # Скачивание (download)
    # ------------------------------------------------------------------
    async def download_record(
        self,
        record_id: str,
        *,
        session_dir: Optional[str] = None,
        progress_cb: Optional[Callable[[str], None]] = None,
        force_overwrite: Optional[bool] = None,
        download_media: bool = True,
    ) -> Dict[str, Any]:
        """
        Скачивает запись с сервера и раскладывает по локальной папке.

        Если известна локальная session_dir — защищено мьютексом
        (не пересекается с publish_session).
        """
        if not self.is_configured():
            raise ScrecError(
                "Синхронизация не настроена: укажите base_url и api_key."
            )

        if force_overwrite is None:
            force_overwrite = bool(
                self.settings.get("force_overwrite_on_download", False)
            )

        # --- Определяем session_dir заранее, чтобы взять мьютекс ---
        if session_dir is None:
            session_dir = self._find_local_session(record_id, "")

        if session_dir:
            lock = await self._get_session_lock(session_dir)
            async with lock:
                return await self._download_record_impl(
                    record_id,
                    session_dir=session_dir,
                    progress_cb=progress_cb,
                    force_overwrite=force_overwrite,
                    download_media=download_media,
                )

        return await self._download_record_impl(
            record_id,
            session_dir=None,
            progress_cb=progress_cb,
            force_overwrite=force_overwrite,
            download_media=download_media,
        )

    async def _download_record_impl(
        self,
        record_id: str,
        *,
        session_dir: Optional[str],
        progress_cb: Optional[Callable[[str], None]],
        force_overwrite: bool,
        download_media: bool,
    ) -> Dict[str, Any]:
        if progress_cb:
            progress_cb(f"Получение записи {record_id} с сервера")

        renamed_from: Optional[str] = None

        async with self._make_client() as client:
            record = await client.get_record(record_id)

            if session_dir is None:
                path = str(record.get("path") or "").strip()
                folder = os.path.basename(path) if path else record_id
                session_dir = os.path.join(self.sessions_root, folder)

            existing_dir = self._find_local_session(
                record_id, record.get("path") or ""
            )
            if (existing_dir
                    and os.path.abspath(existing_dir)
                    != os.path.abspath(session_dir)):
                renamed_from = existing_dir
                if progress_cb:
                    progress_cb(
                        f"Переименование папки: "
                        f"{os.path.basename(existing_dir)} → "
                        f"{os.path.basename(session_dir)}"
                    )
                try:
                    if os.path.exists(session_dir):
                        session_dir = existing_dir
                        renamed_from = None
                    else:
                        os.rename(existing_dir, session_dir)
                        log.info(
                            "SyncManager: папка переименована: %s → %s",
                            existing_dir, session_dir,
                        )
                except Exception as exc:
                    log.warning(
                        "Не удалось переименовать %s → %s: %s",
                        existing_dir, session_dir, exc,
                    )
                    session_dir = existing_dir
                    renamed_from = None

            _ensure_dirs(session_dir)

            self._save_remote_meta(session_dir, record)
            self._sync_summary_md(session_dir, record)

            downloaded: List[Dict[str, Any]] = []
            items = (record.get("artifacts") or [])
            server_targets: Dict[str, Dict[str, Any]] = {}
            server_media_kinds: set = set()

            for art in items:
                kind = str(art.get("kind") or "attachment")
                filename = str(art.get("filename") or "")
                if not filename:
                    continue

                if kind in ("video", "audio"):
                    if not download_media:
                        log.info(
                            "Медиа-артефакт %s (%s) пропущен "
                            "(download_media=False)",
                            filename, kind,
                        )
                        continue
                    server_media_kinds.add(kind)
                    target_rel = os.path.basename(filename)
                    target_path = os.path.join(session_dir, target_rel)

                    if self._should_skip_download(
                        target_path, art, force_overwrite
                    ):
                        log.debug(
                            "Медиа %s не изменился — пропускаем",
                            filename,
                        )
                        downloaded.append({
                            "kind": kind,
                            "filename": filename,
                            "local_path": target_path,
                            "skipped": True,
                        })
                        continue

                    if progress_cb:
                        size_mb = int(art.get("size") or 0) / 1024 / 1024
                        progress_cb(
                            f"Скачивание {kind}: {filename} "
                            f"({size_mb:.1f} МБ)"
                        )

                    try:
                        await client.download_artifact(
                            record_id, filename, target_path
                        )
                        downloaded.append({
                            "kind": kind,
                            "filename": filename,
                            "local_path": target_path,
                        })
                    except ScrecError as exc:
                        log.error(
                            "Не удалось скачать медиа %s/%s: %s",
                            record_id, filename, exc,
                        )
                    continue

                target_rel = self._artifact_target_relpath(
                    kind, filename
                )
                target_path = os.path.join(session_dir, target_rel)
                server_targets[target_rel] = art

                if self._should_skip_download(
                    target_path, art, force_overwrite
                ):
                    log.debug(
                        "Артефакт %s не изменился — пропускаем", filename
                    )
                    downloaded.append({
                        "kind": kind,
                        "filename": filename,
                        "local_path": target_path,
                        "skipped": True,
                    })
                    continue

                if progress_cb:
                    progress_cb(f"Скачивание: {filename}")

                try:
                    await client.download_artifact(
                        record_id, filename, target_path
                    )
                    downloaded.append({
                        "kind": kind,
                        "filename": filename,
                        "local_path": target_path,
                    })
                except ScrecError as exc:
                    log.error(
                        "Не удалось скачать артефакт %s/%s: %s",
                        record_id, filename, exc,
                    )

            self._prune_local_artifacts(
                session_dir, server_targets, server_media_kinds
            )

            video = record.get("video") or {}
            if video.get("url"):
                meta = read_json_file(
                    os.path.join(session_dir, "session.json")
                ) or {}
                meta["video_url"] = video.get("url")
                meta["video_size"] = video.get("size", 0)
                meta["video_mime"] = video.get("mime", "")
                meta["video_duration"] = video.get("duration", 0)
                self._write_json(
                    os.path.join(session_dir, "session.json"), meta
                )

            state = _read_sync_state(session_dir)
            state.update({
                "record_id": record_id,
                "path": record.get("path", ""),
                "revision": record.get("revision", 0),
                "downloaded_at": _iso_now(),
            })
            _write_sync_state(session_dir, state)

            self._update_record_index(record_id, session_dir)

        if progress_cb:
            progress_cb(
                f"Готово: {record_id} → {session_dir} "
                f"({len(downloaded)} артефактов)"
            )

        return {
            "session_dir": session_dir,
            "record_id": record_id,
            "artifacts": downloaded,
            "renamed_from": renamed_from,
        }

    # ------------------------------------------------------------------
    # Скачивание одного артефакта
    # ------------------------------------------------------------------
    async def download_single_artifact(
        self,
        record_id: str,
        session_dir: str,
        kind: str,
        filename: str,
        *,
        progress_cb: Optional[Callable[[str], None]] = None,
    ) -> Optional[str]:
        if kind in ("video", "audio"):
            target_rel = os.path.basename(filename)
        else:
            target_rel = self._artifact_target_relpath(kind, filename)
        target_path = os.path.join(session_dir, target_rel)

        lock = await self._get_session_lock(session_dir)
        async with lock:
            async with self._make_client() as client:
                try:
                    info = await client.get_artifact_info(
                        record_id, filename
                    )
                except ScrecError as exc:
                    log.warning(
                        "download_single_artifact: HEAD не удался "
                        "(%s) — скачиваем как обычно", exc,
                    )
                    info = {"exists": True}

                if not info.get("exists"):
                    log.info(
                        "download_single_artifact: артефакт %s/%s "
                        "не существует на сервере — пропускаем",
                        record_id, filename,
                    )
                    return None

                server_sha = str(info.get("sha256") or "")
                if server_sha and os.path.isfile(target_path):
                    local_sha = _sha256_file(target_path)
                    if local_sha and local_sha == server_sha:
                        log.info(
                            "download_single_artifact: %s уже актуален "
                            "(sha256 совпал)", target_path,
                        )
                        return target_path

                if progress_cb:
                    size_mb = int(info.get("size") or 0) / 1024 / 1024
                    progress_cb(
                        f"Скачивание {kind}: {filename} "
                        f"({size_mb:.1f} МБ)"
                    )

                try:
                    await client.download_artifact(
                        record_id, filename, target_path,
                        progress_cb=None,
                    )
                    log.info(
                        "download_single_artifact: %s → %s",
                        filename, target_path,
                    )
                    return target_path
                except ScrecError as exc:
                    log.error(
                        "download_single_artifact: не удалось скачать "
                        "%s/%s: %s", record_id, filename, exc,
                    )
                    return None

    # ------------------------------------------------------------------
    # Вспомогательные методы скачивания
    # ------------------------------------------------------------------
    @staticmethod
    def _should_skip_download(
        local_path: str,
        server_art: Dict[str, Any],
        force_overwrite: bool,
    ) -> bool:
        if not os.path.isfile(local_path):
            return False
        if force_overwrite:
            return False

        server_sha = str(server_art.get("sha256") or "").strip()
        if server_sha:
            local_sha = _sha256_file(local_path)
            if local_sha and local_sha == server_sha:
                return True
            return False

        server_size = int(server_art.get("size") or 0)
        if server_size > 0:
            try:
                local_size = os.path.getsize(local_path)
            except OSError:
                return False
            if local_size == server_size:
                return True

        return False

    @staticmethod
    def _artifact_target_relpath(kind: str, filename: str) -> str:
        base = os.path.basename(filename)
        ext = os.path.splitext(base)[1]

        if kind == "transcript":
            return "video.txt"
        if kind == "summary":
            return "video_summary.md"
        if kind == "protocol":
            return f"protocol{ext or '.txt'}"
        if kind == "manual_protocol":
            return f"manual_protocol{ext or '.txt'}"
        if kind == "deepseek_prompt":
            return f"deepseek_prompt{ext or '.txt'}"
        if kind == "action_items":
            return "action_items.json"
        return os.path.join("attachments", base)

    def _prune_local_artifacts(
        self,
        session_dir: str,
        server_targets: Dict[str, Dict[str, Any]],
        server_media_kinds: set,
    ) -> None:
        """
        Удаляет локальные артефакты, которых нет на сервере.

        ВАЖНО: если запись не помечена как «готова к синхронизации»
        (sync_ready=false) — prune пропускается. Это защищает
        черновик от удаления локальных файлов, которые ещё не
        публиковались.
        """
        if not _is_sync_ready(session_dir):
            log.info(
                "SyncManager: prune пропущен — запись %s не помечена "
                "как «готова к синхронизации» (sync_ready=false)",
                os.path.basename(session_dir),
            )
            return

        candidates: List[str] = []
        for _kind, fname in _ARTIFACT_KINDS:
            candidates.append(fname)

        for rel in candidates:
            local_path = os.path.join(session_dir, rel)
            if not os.path.isfile(local_path):
                continue
            if rel in server_targets:
                continue
            try:
                os.remove(local_path)
                log.info(
                    "SyncManager: удалён локальный артефакт "
                    "(нет на сервере): %s", rel,
                )
            except Exception as exc:
                log.warning(
                    "Не удалось удалить %s: %s", local_path, exc
                )

        att_dir = os.path.join(session_dir, "attachments")
        if os.path.isdir(att_dir):
            try:
                for name in os.listdir(att_dir):
                    rel = os.path.join("attachments", name)
                    if rel in server_targets:
                        continue
                    full = os.path.join(att_dir, name)
                    if not os.path.isfile(full):
                        continue
                    try:
                        os.remove(full)
                        log.info(
                            "SyncManager: удалено вложение "
                            "(нет на сервере): %s", rel,
                        )
                    except Exception as exc:
                        log.warning(
                            "Не удалось удалить вложение %s: %s",
                            full, exc,
                        )
            except OSError as exc:
                log.warning(
                    "Не удалось прочитать %s: %s", att_dir, exc
                )

    def _sync_summary_md(
        self, session_dir: str, record: Dict[str, Any]
    ) -> None:
        summary_bb = str(record.get("summary_bb") or "").strip()
        if not summary_bb:
            return

        for art in record.get("artifacts") or []:
            if str(art.get("kind") or "") == "summary":
                return

        target_path = os.path.join(session_dir, "video_summary.md")
        try:
            with open(target_path, "w", encoding="utf-8") as f:
                f.write(summary_bb)
            log.debug(
                "SyncManager: video_summary.md обновлён из summary_bb "
                "(%d символов)", len(summary_bb),
            )
        except Exception as exc:
            log.warning(
                "Не удалось записать %s: %s", target_path, exc
            )

    @staticmethod
    def _write_json(path: str, data: Dict[str, Any]) -> None:
        _ensure_dirs(os.path.dirname(path) or ".")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)

    @staticmethod
    def _save_remote_meta(
        session_dir: str, record: Dict[str, Any]
    ) -> None:
        session_json = os.path.join(session_dir, "session.json")
        existing = read_json_file(session_json) or {}

        new_meta: Dict[str, Any] = dict(existing)

        server_fields = (
            "project", "name", "description", "comment",
            "source", "is_scrum", "date", "time",
            "tags", "summary_bb", "prompt", "prompt_name",
            "prompt_edited", "name_template", "name_abbr",
            "generate_summary", "generate_deepseek_prompt",
            "include_name_in_prompt", "include_project_in_prompt",
            "include_comment_in_prompt", "include_tags_in_prompt",
            # --- Флаг готовности к синхронизации ---
            "sync_ready",
        )
        for key in server_fields:
            if key in record:
                new_meta[key] = record[key]

        new_meta["server_id"] = record.get("id", "")
        new_meta["server_revision"] = record.get("revision", 0)
        new_meta["server_path"] = record.get("path", "")
        new_meta["server_updated_at"] = record.get("updated_at", "")

        SyncManager._write_json(session_json, new_meta)

    # ------------------------------------------------------------------
    # Дельта-синхронизация
    # ------------------------------------------------------------------
    async def pull_changes(
        self,
        *,
        progress_cb: Optional[Callable[[str], None]] = None,
        max_pages: int = 50,
    ) -> Dict[str, Any]:
        if not self.is_configured():
            raise ScrecError(
                "Синхронизация не настроена: укажите base_url и api_key."
            )

        since = self.get_last_revision()
        if progress_cb:
            progress_cb(f"Запрос изменений с revision>{since}")

        page_size = int(self.settings.get("sync_page_size", 200))
        applied = 0
        errors: List[str] = []
        new_revision = since

        async with self._make_client() as client:
            for page_idx in range(max_pages):
                try:
                    resp = await client.get_changes(
                        since_revision=new_revision,
                        limit=page_size,
                    )
                except ScrecError as exc:
                    msg = f"Не удалось получить изменения: {exc}"
                    log.error(msg)
                    errors.append(msg)
                    break

                changes = resp.get("changes") or []
                server_rev = int(
                    resp.get("server_revision", new_revision)
                )

                if progress_cb:
                    progress_cb(
                        f"Страница {page_idx + 1}: "
                        f"{len(changes)} изменений"
                    )

                for ch in changes:
                    try:
                        await self._apply_change(ch, client)
                        applied += 1
                    except Exception as exc:
                        msg = (
                            f"Ошибка применения изменения "
                            f"rev={ch.get('rev')}: {exc}"
                        )
                        log.exception(msg)
                        errors.append(msg)

                new_revision = server_rev

                if not resp.get("has_more"):
                    break

        self.set_last_revision(new_revision)

        if progress_cb:
            progress_cb(
                f"Применено изменений: {applied}, "
                f"новая ревизия: {new_revision}"
            )

        return {
            "applied": applied,
            "last_revision": new_revision,
            "errors": errors,
        }

    async def _apply_change(
        self, change: Dict[str, Any], client: ScrecClient
    ) -> None:
        """
        Применяет одно изменение с сервера.

        ВАЖНО: если локальная запись не помечена как
        «готова к синхронизации» (sync_ready=false), изменение
        игнорируется. Это защищает черновик от перезаписи
        серверной версией.
        """
        action = change.get("action")
        record_id = change.get("id") or ""
        path = change.get("path") or ""

        if not record_id:
            return

        if path.startswith(f"{_CONFIG_PROJECT}/") or \
                path.startswith("_config/"):
            log.debug(
                "SyncManager: пропускаю служебную запись конфига: %s",
                path,
            )
            return

        # --- Проверка флага sync_ready ---
        session_dir = self._find_local_session(record_id, path)
        if session_dir:
            meta = read_json_file(
                os.path.join(session_dir, "session.json")
            ) or {}
            if not meta.get("sync_ready", False):
                log.info(
                    "apply_change: пропускаю %s для record_id=%s — "
                    "запись не помечена как «готова к синхронизации»",
                    action, record_id,
                )
                return

        log.info(
            "SyncManager: применяю изменение action=%s id=%s path=%s",
            action, record_id, path,
        )

        if action == "delete":
            await self._handle_delete(record_id, path)
            return

        if action in ("artifact_upload", "artifact_delete",
                      "artifact_soft_delete"):
            artifact_filename = (
                change.get("filename")
                or change.get("artifact_filename")
                or change.get("artifact")
                or ""
            )

            session_dir = self._find_local_session(record_id, path)

            if not session_dir:
                log.info(
                    "apply_change: локальной папки нет для "
                    "record_id=%s — скачиваю запись целиком",
                    record_id,
                )
                await self.download_record(record_id)
                return

            if action in ("artifact_delete", "artifact_soft_delete"):
                if artifact_filename:
                    self._delete_local_artifact(
                        session_dir, artifact_filename
                    )
                return

            if artifact_filename:
                kind = str(change.get("kind") or "")
                log.info(
                    "apply_change: точечное скачивание артефакта "
                    "%s (kind=%s) для record_id=%s",
                    artifact_filename, kind or "?", record_id,
                )
                await self.download_single_artifact(
                    record_id, session_dir, kind, artifact_filename,
                    progress_cb=None,
                )

                try:
                    record = await client.get_record(record_id)
                    self._save_remote_meta(session_dir, record)
                except ScrecError as exc:
                    log.debug(
                        "apply_change: не удалось обновить метаданные "
                        "после точечного скачивания: %s", exc,
                    )
            else:
                log.info(
                    "apply_change: artifact_upload без filename — "
                    "обновляю только метаданные записи"
                )
                try:
                    record = await client.get_record(record_id)
                    self._save_remote_meta(session_dir, record)
                except ScrecError as exc:
                    log.warning(
                        "apply_change: не удалось получить запись %s: %s",
                        record_id, exc,
                    )
            return

        if action in ("create", "update", "update_links"):
            session_dir = self._find_local_session(record_id, path)
            if session_dir:
                await self.download_record(
                    record_id, session_dir=session_dir
                )
            else:
                await self.download_record(record_id)
            return

        if action == "artifact_delete_all":
            session_dir = self._find_local_session(record_id, path)
            if session_dir:
                await self.download_record(
                    record_id, session_dir=session_dir
                )
            else:
                await self.download_record(record_id)
            return

        log.debug("Неизвестный action=%r — пропускаю", action)

    def _delete_local_artifact(
        self, session_dir: str, filename: str
    ) -> None:
        base = os.path.basename(filename)
        candidates = [
            os.path.join(session_dir, base),
            os.path.join(session_dir, "attachments", base),
        ]
        for p in candidates:
            if os.path.isfile(p):
                try:
                    os.remove(p)
                    log.info(
                        "apply_change: локальный артефакт удалён "
                        "(удалён на сервере): %s", p,
                    )
                except Exception as exc:
                    log.warning(
                        "Не удалось удалить %s: %s", p, exc
                    )

    async def _handle_delete(
        self, record_id: str, path: str
    ) -> None:
        session_dir = self._find_local_session(record_id, path)
        if not session_dir:
            log.debug(
                "Удаление на сервере: локальная папка не найдена "
                "(record_id=%s)", record_id,
            )
            return

        delete_local = bool(
            self.settings.get("delete_local_on_server_delete", False)
        )

        if delete_local:
            try:
                shutil.rmtree(session_dir)
                log.warning(
                    "SyncManager: локальная папка удалена "
                    "(запись удалена на сервере): %s", session_dir,
                )
            except Exception as exc:
                log.error(
                    "Не удалось удалить %s: %s", session_dir, exc
                )
            finally:
                self._remove_from_record_index(record_id)
        else:
            state = _read_sync_state(session_dir)
            state["deleted_on_server"] = True
            state["deleted_at"] = _iso_now()
            _write_sync_state(session_dir, state)
            log.info(
                "SyncManager: запись помечена как удалённая на "
                "сервере (папка оставлена): %s", session_dir,
            )

    def _find_local_session(
        self, record_id: str, path: str
    ) -> str:
        """
        Ищет локальную папку сессии по record_id.

        Порядок:
          1. Индекс record_id → session_dir в sync_state.json.
          2. Fallback: обход всех папок sessions/.
          3. Fallback: поиск по basename(path) из change.
        """
        indexed = self._lookup_in_record_index(record_id)
        if indexed:
            return indexed

        if not os.path.isdir(self.sessions_root):
            return ""

        found = ""
        try:
            entries = os.listdir(self.sessions_root)
        except OSError:
            entries = []

        for name in entries:
            full = os.path.join(self.sessions_root, name)
            if not os.path.isdir(full):
                continue
            marker = os.path.join(full, _SYNC_MARKER_FILE)
            if not os.path.isfile(marker):
                continue
            try:
                with open(marker, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if str(data.get("record_id") or "") == record_id:
                    found = full
                    break
            except Exception:
                continue

        if not found and path:
            folder = os.path.basename(path.rstrip("/"))
            candidate = os.path.join(self.sessions_root, folder)
            if os.path.isdir(candidate):
                found = candidate

        if found:
            self._update_record_index(record_id, found)
        return found

    # ------------------------------------------------------------------
    # Синхронизация "всё"
    # ------------------------------------------------------------------
    async def sync_all(
        self,
        *,
        project: str = "",
        progress_cb: Optional[Callable[[int, int, str], None]] = None,
    ) -> Dict[str, Any]:
        sessions = self._list_local_sessions(project=project)
        total = len(sessions)

        ok = 0
        errors: List[Dict[str, str]] = []

        for i, session_dir in enumerate(sessions, start=1):
            name = os.path.basename(session_dir)
            if progress_cb:
                progress_cb(i, total, f"Публикация {name}")
            try:
                await self.publish_session(session_dir)
                ok += 1
            except ScrecError as exc:
                errors.append({"session": name, "error": str(exc)})
                log.error(
                    "sync_all: ошибка публикации %s: %s", name, exc
                )

        if progress_cb:
            progress_cb(total, total, "Готово")

        return {
            "total": total,
            "ok": ok,
            "errors": errors,
        }

    def _list_local_sessions(self, project: str = "") -> List[str]:
        if not os.path.isdir(self.sessions_root):
            return []
        result: List[str] = []
        try:
            entries = sorted(os.listdir(self.sessions_root))
        except OSError:
            return []

        for name in entries:
            full = os.path.join(self.sessions_root, name)
            if not os.path.isdir(full):
                continue
            if not os.path.isfile(
                os.path.join(full, "session.json")
            ):
                continue
            if project:
                meta = read_json_file(
                    os.path.join(full, "session.json")
                ) or {}
                if (meta.get("project") or "").strip() != project:
                    continue
            result.append(full)
        return result

    # ------------------------------------------------------------------
    # Синхронизация справочников проектов и тегов
    # ------------------------------------------------------------------
    async def push_configs(
        self,
        *,
        progress_cb: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        if not self.is_configured():
            raise ScrecError("Синхронизация не настроена.")
        if self.config_manager is None:
            raise ScrecError(
                "push_configs: не передан config_manager."
            )

        if progress_cb:
            progress_cb("Публикация справочников на сервер…")

        projects = self.config_manager.get_projects()
        tags = self.config_manager.get_tags()

        results: Dict[str, Any] = {"projects": None, "tags": None}

        async with self._make_client() as client:
            results["projects"] = await self._push_config_item(
                client, "projects", projects, progress_cb
            )
            results["tags"] = await self._push_config_item(
                client, "tags", tags, progress_cb
            )

        if progress_cb:
            progress_cb("Справочники опубликованы")

        return results

    async def _push_config_item(
        self,
        client: ScrecClient,
        folder_name: str,
        data: Any,
        progress_cb: Optional[Callable[[str], None]],
    ) -> Dict[str, Any]:
        payload = {
            "project": _CONFIG_PROJECT,
            "year": "0000",
            "month": "00",
            "folder_name": folder_name,
            "name": folder_name,
            "source": "config",
            "sync_ready": True,
            "summary_bb": (
                CONFIG_JSON_PREFIX
                + json.dumps(data, ensure_ascii=False)
            ),
        }
        if progress_cb:
            progress_cb(
                f"Отправка config/{folder_name} "
                f"({len(data) if hasattr(data, '__len__') else 0} элем.)"
            )
        result = await client.publish_record(payload=payload)
        log.info(
            "SyncManager.push_configs: %s опубликован (id=%s)",
            folder_name, result.get("id"),
        )
        return result

    async def pull_configs(
        self,
        *,
        progress_cb: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        if not self.is_configured():
            raise ScrecError("Синхронизация не настроена.")
        if self.config_manager is None:
            raise ScrecError(
                "pull_configs: не передан config_manager."
            )

        if progress_cb:
            progress_cb("Загрузка справочников с сервера…")

        applied: Dict[str, bool] = {"projects": False, "tags": False}
        errors: List[str] = []

        async with self._make_client() as client:
            try:
                items = await client.get_month_records(
                    _CONFIG_PROJECT, "0000", "00"
                )
            except ScrecError as exc:
                msg = f"Не удалось получить список config-записей: {exc}"
                log.warning(msg)
                errors.append(msg)
                return {
                    "projects": False, "tags": False, "errors": errors,
                }

            for item in items or []:
                folder = str(item.get("folder_name") or "")
                rid = str(item.get("id") or "")
                if not rid:
                    continue

                if folder == _CONFIG_FOLDER_PROJECTS:
                    try:
                        data = await client.get_config_json(rid)
                        if isinstance(data, list):
                            self.config_manager.set_projects(data)
                            applied["projects"] = True
                            log.info(
                                "SyncManager.pull_configs: применены "
                                "проекты (%d шт.)", len(data),
                            )
                            if progress_cb:
                                progress_cb(
                                    f"Применены проекты: {len(data)} шт."
                                )
                        else:
                            log.warning(
                                "pull_configs: projects — "
                                "ожидался list, получен %s",
                                type(data).__name__,
                            )
                    except Exception as exc:
                        msg = f"Ошибка применения проектов: {exc}"
                        log.warning(msg)
                        errors.append(msg)

                elif folder == _CONFIG_FOLDER_TAGS:
                    try:
                        data = await client.get_config_json(rid)
                        if isinstance(data, list):
                            self.config_manager.set_tags(data)
                            applied["tags"] = True
                            log.info(
                                "SyncManager.pull_configs: применены "
                                "теги (%d шт.)", len(data),
                            )
                            if progress_cb:
                                progress_cb(
                                    f"Применены теги: {len(data)} шт."
                                )
                        else:
                            log.warning(
                                "pull_configs: tags — "
                                "ожидался list, получен %s",
                                type(data).__name__,
                            )
                    except Exception as exc:
                        msg = f"Ошибка применения тегов: {exc}"
                        log.warning(msg)
                        errors.append(msg)

        if progress_cb:
            progress_cb(
                f"Справочники: проекты={'да' if applied['projects'] else 'нет'}, "
                f"теги={'да' if applied['tags'] else 'нет'}"
            )

        return {
            "projects": applied["projects"],
            "tags": applied["tags"],
            "errors": errors,
        }

    async def sync_configs(
        self,
        *,
        direction: str = "both",
        progress_cb: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        if direction not in ("push", "pull", "both"):
            raise ScrecError(
                f"sync_configs: неизвестное направление {direction!r}"
            )

        result: Dict[str, Any] = {
            "push": None, "pull": None, "errors": [],
        }

        if direction in ("push", "both"):
            try:
                result["push"] = await self.push_configs(
                    progress_cb=progress_cb
                )
            except ScrecError as exc:
                log.error("sync_configs.push: %s", exc)
                result["errors"].append(f"push: {exc}")

        if direction in ("pull", "both"):
            try:
                result["pull"] = await self.pull_configs(
                    progress_cb=progress_cb
                )
            except ScrecError as exc:
                log.error("sync_configs.pull: %s", exc)
                result["errors"].append(f"pull: {exc}")

        return result