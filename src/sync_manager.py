"""Логика синхронизации записей с удалённым сервером screc-server.

Задачи модуля:
  • Собрать RecordPayload из локального session.json.
  • Опубликовать запись на сервер (POST /api/v1/records) — метаданные
    + текстовые артефакты.
  • Опционально загрузить видео и аудио как артефакты
    (kind=video / kind=audio).
  • Скачать запись с сервера и разложить по локальным папкам,
    включая видео/аудио, если они есть на сервере.
  • Дельта-синхронизация через /sync/changes.
  • Синхронизация справочников проектов и тегов через фиктивные
    записи в проекте "_config".

Ключевые особенности:
  • Медиа передаётся как обычные артефакты — не требует
    доработок сервера.
  • Ограничение размера — max_artifact_mb (не больше
    SCREC_MAX_ARTIFACT_MB на сервере).
  • Умная стратегия скачивания по sha256; force_overwrite
    позволяет перезаписать всё.
  • Артефакты, которых нет на сервере, удаляются локально,
    НО видео/аудио не удаляются, если они не загружались
    на сервер (send_media_to_server=False).

Изменения:
  • Перед загрузкой медиа-файлов выполняется пакетная проверка
    через POST /records/{id}/artifacts/check. Файлы, у которых
    хэш совпадает с серверным, НЕ отправляются по сети (экономия
    трафика).
  • Управляется настройкой sync_use_hash_check (включена по
    умолчанию).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .file_readers import read_json_file
from .logger import get_logger
from .screc_client import ScrecClient, ScrecError

log = get_logger(__name__)


# Имя файла локального состояния синхронизации.
_SYNC_STATE_FILE = "sync_state.json"

# Файл-маркер, что запись уже опубликована на сервере.
_SYNC_MARKER_FILE = ".sync_published.json"

# Проект для хранения конфигов на сервере (не настоящий проект).
_CONFIG_PROJECT = "_config"
_CONFIG_FOLDER_PROJECTS = "projects"
_CONFIG_FOLDER_TAGS = "tags"


# Соответствие локальных файлов и kind на сервере (текстовые артефакты).
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
    """
    Возвращает словарь {"video": "/path/video.mp4",
                       "audio": "/path/video.mp3"}
    с найденными в папке сессии медиафайлами.

    Если файла нет — ключ отсутствует.
    """
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
) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]]]:
    """
    Возвращает два списка:
      • small_artifacts — (kind, filename) для отправки в POST /records
        (метаданные + текстовые артефакты + вложения);
      • media_artifacts — (kind, filename) для отдельной загрузки
        через POST /records/{id}/artifacts (видео, аудио).

    Медиа-артефакты отделены потому, что их может быть много/они
    большие, и удобнее грузить их по одному после создания записи.

    Args:
        include_media:    если True — в media_artifacts попадут
                          найденные видео/аудио.
        max_artifact_mb:  ограничение на размер каждого медиафайла.
    """
    small: List[Tuple[str, str]] = []
    media: List[Tuple[str, str]] = []
    seen_kinds: Dict[str, bool] = {}

    # --- Текстовые артефакты ---
    for kind, filename in _ARTIFACT_KINDS:
        if seen_kinds.get(kind):
            continue
        full = os.path.join(session_dir, filename)
        if os.path.isfile(full):
            small.append((kind, filename))
            seen_kinds[kind] = True

    # --- Вложения ---
    att_dir = os.path.join(session_dir, "attachments")
    if os.path.isdir(att_dir):
        try:
            for name in sorted(os.listdir(att_dir)):
                full = os.path.join(att_dir, name)
                if not os.path.isfile(full):
                    continue

                # Клиент санитизирует имя перед отправкой
                # (см. utils.sanitize_filename), но если исходное
                # имя заведомо огромное — предупредим пользователя,
                # чтобы он понимал, что на сервере файл будет
                # под другим именем.
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

    # --- Медиа ---
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
            if max_bytes > 0 and size > max_bytes:
                log.warning(
                    "Медиафайл %s (%s) превышает лимит %d МБ — "
                    "не будет отправлен на сервер",
                    os.path.basename(path), kind, max_artifact_mb,
                )
                continue
            media.append((kind, os.path.basename(path)))

    return small, media


# ---------------------------------------------------------------------------
# Менеджер синхронизации
# ---------------------------------------------------------------------------
class SyncManager:
    """Управляет синхронизацией записей и конфигов с сервером."""

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
            "force_overwrite=%s, use_hash_check=%s",
            self.settings.get("base_url"),
            self.settings.get("send_media_to_server"),
            self.settings.get("auto_upload_after_processing"),
            self.settings.get("auto_pull_enabled"),
            self.settings.get("delete_local_on_server_delete"),
            self.settings.get("force_overwrite_on_download"),
            self.settings.get("use_hash_check"),
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
        """Нужно ли использовать условную загрузку (HEAD/check)."""
        return bool(self.settings.get("use_hash_check", True))

    # ------------------------------------------------------------------
    # Локальное состояние (last_synced_revision)
    # ------------------------------------------------------------------
    def load_state(self) -> Dict[str, Any]:
        if not os.path.isfile(self._state_path):
            return {"last_synced_revision": 0}
        try:
            with open(self._state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {"last_synced_revision": 0}
            data.setdefault("last_synced_revision", 0)
            return data
        except Exception as exc:
            log.warning(
                "Не удалось прочитать sync_state: %s", exc
            )
            return {"last_synced_revision": 0}

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

        Этапы:
          1. POST /records — метаданные + текстовые артефакты
             (с sha256 для каждого файла).
          2. Если send_media_to_server:
             a. POST /records/{id}/artifacts/check — пакетная
                проверка хэшей всех медиафайлов.
             b. Отправка только тех файлов, для которых сервер
                ответил skip=false.

        Returns:
            {"action": ..., "record_id": ..., "revision": ...,
             "path": ..., "media_uploaded": [...],
             "media_skipped": [...]}
        """
        if not self.is_configured():
            raise ScrecError(
                "Синхронизация не настроена: укажите base_url и api_key "
                "в Настройки → Синхронизация."
            )

        if not os.path.isdir(session_dir):
            raise ScrecError(f"Папка сессии не найдена: {session_dir}")

        session_json = os.path.join(session_dir, "session.json")
        meta = read_json_file(session_json) or {}

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

        # --- Хэши для текстовых артефактов ---
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
            "folder=%r, small_artifacts=%d, media_artifacts=%d "
            "(include_media=%s, max=%d МБ, hashes=%d)",
            os.path.basename(session_dir),
            payload.get("project"), payload.get("folder_name"),
            len(small_artifacts), len(media_artifacts),
            include_media, max_artifact_mb,
            len(small_artifact_hashes),
        )

        if progress_cb:
            names = ", ".join(a[0] for a in small_artifacts) or "—"
            media_names = ", ".join(a[0] for a in media_artifacts) or "—"
            progress_cb(
                f"Отправка метаданных: {payload['folder_name']} "
                f"(текстовых: {len(small_artifacts)}: {names}; "
                f"медиа: {len(media_artifacts)}: {media_names})"
            )

        media_uploaded: List[Dict[str, Any]] = []
        media_skipped: List[Dict[str, Any]] = []

        async with self._make_client() as client:
            # --- Шаг 1: запись с текстовыми артефактами ---
            result = await client.publish_record(
                payload=payload,
                artifacts=small_artifacts,
                artifact_hashes=small_artifact_hashes,
                artifact_dir=session_dir,
            )

            record_id = result.get("id") or ""

            # --- Шаг 2: медиа-артефакты ---
            if record_id and media_artifacts:
                # Собираем локальные хэши для медиа
                media_items: List[Dict[str, Any]] = []
                for kind, filename in media_artifacts:
                    full_path = os.path.join(session_dir, filename)
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

                # --- Пакетная проверка хэшей ---
                # Отправляем один маленький JSON и получаем ответ,
                # какие файлы надо загружать, а какие уже есть.
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

                # --- Загрузка только тех файлов, которые нужны ---
                for idx, item in enumerate(media_items, start=1):
                    kind = item["kind"]
                    filename = item["filename"]
                    full_path = item["path"]
                    size_bytes = item["size_bytes"]
                    sha = item["sha256"]
                    size_mb = size_bytes / 1024 / 1024

                    # Файл уже есть на сервере с таким же хэшем —
                    # не отправляем содержимое.
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

        # --- Сохраняем состояние ---
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
                "media_uploaded": [
                    m["filename"] for m in media_uploaded
                    if not m.get("skipped")
                ],
            })
            _write_sync_state(session_dir, state)
            log.info(
                "SyncManager: запись опубликована, record_id=%s "
                "(revision=%s, action=%s, media_uploaded=%d)",
                record_id, result.get("revision"),
                result.get("action"), len(media_uploaded),
            )

        # --- Опциональное удаление локальных медиа ---
        if (self.settings.get("delete_local_media_after_media_upload")
                and media_uploaded):
            self._maybe_delete_uploaded_media(
                session_dir,
                [m for m in media_uploaded if not m.get("skipped")],
            )

        # --- Опциональное удаление локальных медиа (старое поведение) ---
        if self.settings.get("allow_delete_local_media_after_upload"):
            self._maybe_delete_local_media(session_dir, meta)

        if progress_cb:
            progress_cb(
                f"Готово: {result.get('action', 'ok')} "
                f"(id={record_id or '—'}, "
                f"медиа: {len(media_uploaded)} загружено, "
                f"{len(media_skipped)} пропущено)"
            )

        return {
            "action": result.get("action", "create"),
            "record_id": record_id,
            "revision": result.get("revision", 0),
            "path": result.get("path", ""),
            "media_uploaded": media_uploaded,
            "media_skipped": media_skipped,
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
        """
        Синхронизирует ОДНУ запись с явными параметрами,
        переопределяя настройки из config на время операции.

        Args:
            session_dir:              папка сессии.
            include_media:            передавать ли видео/аудио
                                      (если None — из настроек).
            send_video_link:          передавать ли ссылку file://
                                      (если None — из настроек).
            delete_media_after_upload:
                                      удалять ли локальные медиа
                                      после успешной загрузки
                                      (если None — из настроек).
            progress_cb:              колбэк для отчёта.

        Returns:
            Тот же словарь, что и publish_session, плюс поля:
              – "include_media": фактически применённое значение;
              – "send_video_link": фактически применённое значение;
              – "delete_media_after_upload": применённое значение.
        """
        # Копируем настройки и переопределяем параметры.
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
            # Возвращаем настройки как было.
            self.settings = old_settings

    def _maybe_delete_uploaded_media(
        self,
        session_dir: str,
        uploaded: List[Dict[str, Any]],
    ) -> None:
        """Удаляет локальные медиафайлы, которые успешно ушли на сервер."""
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
        """Старое поведение: удалить video.* после публикации."""
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
        """
        Скачивает с сервера только медиа-артефакты (video/audio)
        для конкретной записи.

        Используется в окне «Записи», когда пользователь хочет
        посмотреть/послушать запись, но локально медиафайла нет,
        а запись уже опубликована на сервере (есть record_id).

        Returns:
            {
              "downloaded": [{"kind": ..., "filename": ...,
                              "local_path": ...}, ...],
              "missing": [{"kind": ..., "filename": ...}, ...],
              "session_dir": ...,
              "record_id": ...,
            }
            Если медиа на сервере нет — returned["downloaded"] пуст,
            returned["missing"] содержит список ожидаемых kind'ов,
            которые на сервере отсутствуют.
        """
        if not self.is_configured():
            raise ScrecError(
                "Синхронизация не настроена: укажите base_url и api_key "
                "в Настройки → Синхронизация."
            )
        if not os.path.isdir(session_dir):
            raise ScrecError(f"Папка сессии не найдена: {session_dir}")

        record_id = get_record_id(session_dir)
        if not record_id:
            raise ScrecError(
                "Запись не опубликована на сервере — нечего скачивать. "
                "Опубликуйте её через «Файл → Синхронизировать "
                "выбранную запись…» или включите передачу медиа "
                "на сервер в настройках."
            )

        if progress_cb:
            progress_cb(
                f"Запрос метаданных записи {record_id} с сервера"
            )

        downloaded: List[Dict[str, Any]] = []
        missing: List[Dict[str, Any]] = []

        async with self._make_client() as client:
            record = await client.get_record(record_id)
            artifacts = record.get("artifacts") or []

            # Ищем медиа-артефакты на сервере.
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

                # Локальный путь: video.<ext> в папке сессии.
                target_rel = os.path.basename(filename)
                target_path = os.path.join(session_dir, target_rel)

                size_mb = int(art.get("size") or 0) / 1024 / 1024
                if progress_cb:
                    progress_cb(
                        f"Скачивание {kind} [{idx}/{len(media_items)}]: "
                        f"{filename} ({size_mb:.1f} МБ)…"
                    )

                # Проверяем, не скачано ли уже.
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
                        "download_media_only: %s скачан в %s (%.1f МБ)",
                        filename, target_path, size_mb,
                    )
                except ScrecError as exc:
                    log.error(
                        "download_media_only: не удалось скачать %s: %s",
                        filename, exc,
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
        Скачивает запись с сервера и раскладывает по локальной
        папке sessions/<folder_name>.

        Медиа-артефакты (kind=video / kind=audio) сохраняются как
        video.<ext> в папке сессии.

        Args:
            download_media:  если False — медиа-артефакты не
                             скачиваются, только текстовые.
            force_overwrite: если None — берётся из настроек.
        """
        if not self.is_configured():
            raise ScrecError(
                "Синхронизация не настроена: укажите base_url и api_key."
            )

        if force_overwrite is None:
            force_overwrite = bool(
                self.settings.get("force_overwrite_on_download", False)
            )

        if progress_cb:
            progress_cb(f"Получение записи {record_id} с сервера")

        renamed_from: Optional[str] = None

        async with self._make_client() as client:
            record = await client.get_record(record_id)

            if session_dir is None:
                path = str(record.get("path") or "").strip()
                folder = os.path.basename(path) if path else record_id
                session_dir = os.path.join(self.sessions_root, folder)

            # --- Переименование папки при смене path ---
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

                # --- Медиа ---
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
                    if not target_rel.startswith("video."):
                        # На всякий случай — если имя не video.*,
                        # оставляем как есть.
                        pass
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

                # --- Текстовые артефакты ---
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

            # --- Удаление локальных текстовых артефактов, которых
            #     нет на сервере (медиа не трогаем, если сервер
            #     их не хранит) ---
            self._prune_local_artifacts(
                session_dir, server_targets, server_media_kinds
            )

            # --- video_url ---
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

            # --- Локальный маркер ---
            state = _read_sync_state(session_dir)
            state.update({
                "record_id": record_id,
                "path": record.get("path", ""),
                "revision": record.get("revision", 0),
                "downloaded_at": _iso_now(),
            })
            _write_sync_state(session_dir, state)

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
        return os.path.join("attachments", base)

    def _prune_local_artifacts(
        self,
        session_dir: str,
        server_targets: Dict[str, Dict[str, Any]],
        server_media_kinds: set,
    ) -> None:
        """
        Удаляет локальные текстовые артефакты, которых нет на сервере.

        Медиа: если сервер НЕ хранит видео/аудио (server_media_kinds
        пусто по соответствующему kind), локальные media НЕ
        удаляем. Если сервер хранит — удаляем те, что не совпадают
        с серверным списком (но это обрабатывается выше, в
        download_record: если сервер отдал kind=video, то файл
        сохранится под серверным именем; если сервер отдал другой
        ext — старый video.<ext> мог остаться).

        Для простоты: не удаляем media совсем — это безопасно и
        не приводит к потере данных.
        """
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

        log.info(
            "SyncManager: применяю изменение action=%s id=%s path=%s",
            action, record_id, path,
        )

        if action == "delete":
            await self._handle_delete(record_id, path)
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

        if action in ("artifact_upload", "artifact_delete",
                      "artifact_soft_delete", "artifact_delete_all"):
            # Перезагружаем запись и приводим локальные файлы в
            # соответствие серверному состоянию.
            session_dir = self._find_local_session(record_id, path)
            if session_dir:
                await self.download_record(
                    record_id, session_dir=session_dir
                )
            else:
                await self.download_record(record_id)
            return

        log.debug("Неизвестный action=%r — пропускаю", action)

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
        if not os.path.isdir(self.sessions_root):
            return ""

        try:
            entries = os.listdir(self.sessions_root)
        except OSError:
            return ""

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
                    return full
            except Exception:
                continue

        if path:
            folder = os.path.basename(path.rstrip("/"))
            candidate = os.path.join(self.sessions_root, folder)
            if os.path.isdir(candidate):
                return candidate

        return ""

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
            "summary_bb": json.dumps(data, ensure_ascii=False),
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
                        raw = await client.get_summary(rid)
                        data = json.loads(raw)
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
                    except Exception as exc:
                        msg = f"Ошибка применения проектов: {exc}"
                        log.warning(msg)
                        errors.append(msg)

                elif folder == _CONFIG_FOLDER_TAGS:
                    try:
                        raw = await client.get_summary(rid)
                        data = json.loads(raw)
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