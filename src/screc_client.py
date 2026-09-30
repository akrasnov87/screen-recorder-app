"""Клиент к удалённому серверу синхронизации screc-server.

Реализует API из DOCS.md (v1.1.0):
  • GET  /health
  • GET  /api/v1/tree
  • GET  /api/v1/tree/{project}
  • GET  /api/v1/tree/{project}/{year}
  • GET  /api/v1/tree/{project}/{year}/{month}
  • POST /api/v1/records
  • GET  /api/v1/records/{id}
  • PATCH /api/v1/records/{id}
  • DELETE /api/v1/records/{id}                      ← ЗАБЛОКИРОВАНО
  • GET  /api/v1/records/{id}/video-url
  • PUT  /api/v1/records/{id}/video-url
  • GET  /api/v1/records/{id}/artifacts
  • GET  /api/v1/records/{id}/artifacts/{filename}
  • HEAD /api/v1/records/{id}/artifacts/{filename}
  • POST /api/v1/records/{id}/artifacts/check
  • POST /api/v1/records/{id}/artifacts
  • DELETE /api/v1/records/{id}/artifacts/{filename} ← ЗАБЛОКИРОВАНО
  • DELETE /api/v1/records/{id}/artifacts            ← не реализовано
  • GET  /api/v1/records/{id}/transcript
  • GET  /api/v1/records/{id}/summary
  • GET  /api/v1/records/_/search
  • GET  /api/v1/sync/changes
  • GET  /api/v1/sync/snapshot

Клиент полностью асинхронный (aiohttp), поддерживает потоковую
загрузку и скачивание артефактов.

Изменения:
  • Добавлены методы check_artifact() и check_artifacts_batch() —
    позволяют перед отправкой файла узнать, нужно ли его
    загружать (условная загрузка, см. §7.6 документации).
  • upload_artifact_path() теперь принимает параметр
    skip_if_hash_matches: если True — сначала спрашивает сервер
    через HEAD, и если файл уже есть с таким же хэшем — не
    отправляет его.

  • ДОБАВЛЕН ПРЕДОХРАНИТЕЛЬ: удаление данных на сервере
    запрещено политикой приложения. Флаг _DELETE_ALLOWED_ON_SERVER
    в начале файла. Пока он False — методы delete_record() и
    delete_artifact() не отправляют запрос, а сразу бросают
    ScrecError. Это защищает от случайного вызова в будущем.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import aiohttp

from .logger import get_logger
from .utils import sanitize_filename

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# ПРЕДОХРАНИТЕЛЬ: удаление данных на сервере запрещено.
#
# Если когда-нибудь потребуется разрешить удаление (например,
# отдельной утилитой администратора), установите True осознанно.
# Пока флаг False — методы delete_record() и delete_artifact()
# не отправляют HTTP-запрос, а сразу бросают ScrecError.
# ---------------------------------------------------------------------------
_DELETE_ALLOWED_ON_SERVER: bool = False


class ScrecError(RuntimeError):
    """Ошибка при обращении к серверу синхронизации."""

    def __init__(
        self,
        message: str,
        status: int = 0,
        body: str = "",
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def _sha256_file(path: str) -> str:
    """Вычисляет SHA-256 хэш файла."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception as exc:
        log.warning("Не удалось посчитать sha256 для %s: %s", path, exc)
        return ""


class ScrecClient:
    """Асинхронный клиент к screc-server."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        connect_timeout: float = 15.0,
        read_timeout: float = 120.0,
    ) -> None:
        if not base_url:
            raise ScrecError("Не задан base_url для сервера синхронизации")
        if not api_key:
            raise ScrecError("Не задан api_key для сервера синхронизации")

        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._connect_timeout = float(connect_timeout)
        self._read_timeout = float(read_timeout)
        self._session: Optional[aiohttp.ClientSession] = None

        log.debug(
            "ScrecClient создан: base_url=%s, connect=%.1f, read=%.1f, "
            "delete_allowed=%s",
            self.base_url, self._connect_timeout, self._read_timeout,
            _DELETE_ALLOWED_ON_SERVER,
        )

    # ------------------------------------------------------------------
    # Контекстный менеджер
    # ------------------------------------------------------------------
    async def __aenter__(self) -> "ScrecClient":
        timeout = aiohttp.ClientTimeout(
            total=None,
            connect=self._connect_timeout,
            sock_connect=self._connect_timeout,
            sock_read=self._read_timeout,
        )
        self._session = aiohttp.ClientSession(timeout=timeout)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # Низкоуровневые помощники
    # ------------------------------------------------------------------
    def _headers(
        self, extra: Optional[Dict[str, str]] = None
    ) -> Dict[str, str]:
        h = {"X-API-Key": self.api_key}
        if extra:
            h.update(extra)
        return h

    async def _read_body(self, resp: aiohttp.ClientResponse) -> str:
        try:
            text = await resp.text()
        except Exception:
            return ""
        text = text.strip()
        return (text[:500] + "…") if len(text) > 500 else text

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        context: str = "запрос",
    ) -> Any:
        if self._session is None:
            raise ScrecError("aiohttp-сессия не открыта")

        url = f"{self.base_url}{path}"
        log.debug("ScrecClient: %s %s (params=%s)", method, url, params)

        try:
            async with self._session.request(
                method,
                url,
                headers=self._headers(
                    {"Content-Type": "application/json"}
                    if json_body is not None else None
                ),
                json=json_body,
                params=params,
            ) as resp:
                if resp.status >= 400:
                    body = await self._read_body(resp)
                    msg = f"ScrecClient: {context}: HTTP {resp.status} от {url}"
                    if body:
                        msg += f" — {body}"
                    log.error(msg)
                    raise ScrecError(msg, status=resp.status, body=body)

                ctype = (resp.headers.get("Content-Type") or "").lower()
                if "application/json" in ctype:
                    try:
                        return await resp.json()
                    except Exception as exc:
                        body = await self._read_body(resp)
                        msg = (
                            f"ScrecClient: {context}: не удалось "
                            f"разобрать JSON (HTTP {resp.status}): "
                            f"{exc}. Тело: {body}"
                        )
                        log.error(msg)
                        raise ScrecError(
                            msg, status=resp.status, body=body
                        )
                return await resp.text()
        except ScrecError:
            raise
        except aiohttp.ClientConnectorError as exc:
            msg = f"ScrecClient: ошибка подключения к {url}: {exc}"
            log.error(msg)
            raise ScrecError(msg)
        except aiohttp.ServerTimeoutError:
            msg = (
                f"ScrecClient: таймаут ({self._read_timeout:.0f} с) "
                f"при {context}"
            )
            log.error(msg)
            raise ScrecError(msg)
        except Exception as exc:
            log.exception("ScrecClient: ошибка %s: %s", context, exc)
            raise ScrecError(f"ScrecClient: {context}: {exc}")

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------
    async def health(self) -> Dict[str, Any]:
        """GET /health — проверка живости сервера (без ключа)."""
        if self._session is None:
            raise ScrecError("aiohttp-сессия не открыта")

        url = f"{self.base_url}/health"
        try:
            async with self._session.get(url) as resp:
                if resp.status >= 400:
                    body = await self._read_body(resp)
                    msg = f"Health: HTTP {resp.status} — {body}"
                    raise ScrecError(msg, status=resp.status, body=body)
                return await resp.json()
        except ScrecError:
            raise
        except Exception as exc:
            log.exception("Health: ошибка: %s", exc)
            raise ScrecError(f"Health: {exc}")

    async def ping(self) -> str:
        h = await self.health()
        if h.get("status") != "ok":
            raise ScrecError(f"Сервер вернул status={h.get('status')!r}")

        tree = await self.get_tree()
        return (
            f"ok: revision={h.get('revision', '?')}, "
            f"records={h.get('records_count', '?')}, "
            f"projects={len(tree)}"
        )

    # ------------------------------------------------------------------
    # Дерево
    # ------------------------------------------------------------------
    async def get_tree(self) -> List[Dict[str, Any]]:
        return await self._request_json(
            "GET", "/api/v1/tree", context="tree"
        )

    async def get_years(self, project: str) -> List[str]:
        return await self._request_json(
            "GET",
            f"/api/v1/tree/{quote(project)}",
            context=f"years({project})",
        )

    async def get_months(self, project: str, year: str) -> List[str]:
        return await self._request_json(
            "GET",
            f"/api/v1/tree/{quote(project)}/{year}",
            context=f"months({project}/{year})",
        )

    async def get_month_records(
        self, project: str, year: str, month: str
    ) -> List[Dict[str, Any]]:
        return await self._request_json(
            "GET",
            f"/api/v1/tree/{quote(project)}/{year}/{month}",
            context=f"month_records({project}/{year}/{month})",
        )

    # ------------------------------------------------------------------
    # Записи
    # ------------------------------------------------------------------
    async def publish_record(
        self,
        payload: Dict[str, Any],
        artifacts: Optional[List[Tuple[str, str]]] = None,
        artifact_hashes: Optional[Dict[str, str]] = None,
        *,
        artifact_dir: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        POST /api/v1/records — создать или обновить запись.

        Args:
            payload:      словарь RecordPayload.
            artifacts:    список пар (kind, filename). Файлы ищутся
                          в artifact_dir (или берутся как абсолютные
                          пути, если filename — полный путь).
            artifact_hashes: словарь {filename: sha256_hash}, где
                          filename — basename файла.
            artifact_dir: папка сессии, где лежат артефакты.

        Returns:
            {"id": ..., "revision": ..., "action": ...,
             "path": ..., "artifacts": [...],
             "skipped_artifacts": [...], "uploaded_artifacts": [...]}
        """
        if self._session is None:
            raise ScrecError("aiohttp-сессия не открыта")

        url = f"{self.base_url}/api/v1/records"
        artifacts = artifacts or []
        artifact_hashes = artifact_hashes or {}

        log.info(
            "ScrecClient.publish_record: project=%r, folder=%r, "
            "artifacts=%d, hashes=%d",
            payload.get("project"), payload.get("folder_name"),
            len(artifacts), len(artifact_hashes),
        )

        form = aiohttp.FormData()
        form.add_field(
            "payload",
            json.dumps(payload, ensure_ascii=False),
            content_type="application/json",
        )

        opened_files: List[Any] = []
        try:
            for kind, filename in artifacts:
                if artifact_dir and not os.path.isabs(filename):
                    full_path = os.path.join(artifact_dir, filename)
                else:
                    full_path = filename

                if not os.path.isfile(full_path):
                    log.warning(
                        "Артефакт не найден, пропускаем: %s", full_path
                    )
                    continue

                fh = open(full_path, "rb")
                opened_files.append(fh)

                raw_name = os.path.basename(full_path)
                safe_name = sanitize_filename(raw_name)
                if safe_name != raw_name:
                    log.info(
                        "publish_record: имя файла сокращено/очищено: "
                        "%r → %r", raw_name, safe_name,
                    )

                form.add_field(
                    "files",
                    fh,
                    filename=safe_name,
                    content_type="application/octet-stream",
                )

                sha = artifact_hashes.get(os.path.basename(full_path), "")
                form.add_field("sha256", sha)

            async with self._session.post(
                url, data=form, headers=self._headers()
            ) as resp:
                if resp.status >= 400:
                    body = await self._read_body(resp)
                    msg = (
                        f"ScrecClient.publish_record: HTTP "
                        f"{resp.status} — {body}"
                    )
                    log.error(msg)
                    raise ScrecError(
                        msg, status=resp.status, body=body
                    )
                data = await resp.json()
                log.info(
                    "ScrecClient.publish_record: OK id=%s, "
                    "action=%s, revision=%s, uploaded=%d, skipped=%d",
                    data.get("id"), data.get("action"),
                    data.get("revision"),
                    len(data.get("uploaded_artifacts", [])),
                    len(data.get("skipped_artifacts", [])),
                )
                return data
        except ScrecError:
            raise
        except Exception as exc:
            log.exception("ScrecClient.publish_record: ошибка: %s", exc)
            raise ScrecError(f"publish_record: {exc}")
        finally:
            for fh in opened_files:
                try:
                    fh.close()
                except Exception:
                    pass

    async def get_record(self, record_id: str) -> Dict[str, Any]:
        return await self._request_json(
            "GET",
            f"/api/v1/records/{record_id}",
            context=f"get_record({record_id})",
        )

    async def patch_record(
        self, record_id: str, patch: Dict[str, Any]
    ) -> Dict[str, Any]:
        return await self._request_json(
            "PATCH",
            f"/api/v1/records/{record_id}",
            json_body=patch,
            context=f"patch_record({record_id})",
        )

    # ------------------------------------------------------------------
    # УДАЛЕНИЕ — ЗАБЛОКИРОВАНО
    # ------------------------------------------------------------------
    async def delete_record(
        self, record_id: str, hard: bool = False
    ) -> Dict[str, Any]:
        """
        DELETE /api/v1/records/{id} — удалить запись.

        ⚠️  ВНИМАНИЕ: этот метод необратимо удаляет запись
        на сервере (а также её артефакты, если hard=True).

        В текущем приложении метод ЗАБЛОКИРОВАН флагом
        _DELETE_ALLOWED_ON_SERVER. Пока флаг False — вызов
        завершается ScrecError, и HTTP-запрос НЕ уходит.

        Чтобы разрешить удаление, установите
        _DELETE_ALLOWED_ON_SERVER = True в начале этого модуля —
        осознанно, например для отдельной утилиты администратора.
        """
        if not _DELETE_ALLOWED_ON_SERVER:
            msg = (
                "Удаление записей на сервере запрещено политикой "
                "приложения (см. _DELETE_ALLOWED_ON_SERVER в "
                "screc_client.py)."
            )
            log.warning(
                "delete_record(%s, hard=%s) заблокировано: %s",
                record_id, hard, msg,
            )
            raise ScrecError(msg)

        return await self._request_json(
            "DELETE",
            f"/api/v1/records/{record_id}",
            params={"hard": "true" if hard else "false"},
            context=f"delete_record({record_id})",
        )

    async def delete_artifact(
        self, record_id: str, filename: str
    ) -> Dict[str, Any]:
        """
        DELETE /api/v1/records/{id}/artifacts/{filename} —
        удалить один артефакт.

        ⚠️  ВНИМАНИЕ: необратимо удаляет файл на сервере.

        В текущем приложении метод ЗАБЛОКИРОВАН флагом
        _DELETE_ALLOWED_ON_SERVER. Пока флаг False — вызов
        завершается ScrecError, и HTTP-запрос НЕ уходит.
        """
        if not _DELETE_ALLOWED_ON_SERVER:
            msg = (
                "Удаление артефактов на сервере запрещено политикой "
                "приложения (см. _DELETE_ALLOWED_ON_SERVER в "
                "screc_client.py)."
            )
            log.warning(
                "delete_artifact(%s, %r) заблокировано: %s",
                record_id, filename, msg,
            )
            raise ScrecError(msg)

        return await self._request_json(
            "DELETE",
            f"/api/v1/records/{record_id}/artifacts/{quote(filename)}",
            context=f"delete_artifact({filename})",
        )

    # ------------------------------------------------------------------
    # Видео
    # ------------------------------------------------------------------
    async def get_video_url(self, record_id: str) -> Dict[str, Any]:
        return await self._request_json(
            "GET",
            f"/api/v1/records/{record_id}/video-url",
            context=f"get_video_url({record_id})",
        )

    async def set_video_url(
        self, record_id: str, video: Dict[str, Any]
    ) -> Dict[str, Any]:
        return await self._request_json(
            "PUT",
            f"/api/v1/records/{record_id}/video-url",
            json_body=video,
            context=f"set_video_url({record_id})",
        )

    # ------------------------------------------------------------------
    # Артефакты
    # ------------------------------------------------------------------
    async def list_artifacts(self, record_id: str) -> Dict[str, Any]:
        return await self._request_json(
            "GET",
            f"/api/v1/records/{record_id}/artifacts",
            context=f"list_artifacts({record_id})",
        )

    async def download_artifact(
        self,
        record_id: str,
        filename: str,
        target_path: str,
        *,
        inline: bool = False,
        progress_cb: Optional[Any] = None,
    ) -> str:
        """
        GET /api/v1/records/{id}/artifacts/{filename} — скачать
        артефакт в target_path потоково.

        Args:
            progress_cb: необязательный колбэк
                         (bytes_downloaded, total_bytes).
                         total_bytes = -1, если сервер не отдал
                         Content-Length.
        """
        if self._session is None:
            raise ScrecError("aiohttp-сессия не открыта")

        url = (
            f"{self.base_url}/api/v1/records/{record_id}/artifacts/"
            f"{quote(filename)}"
        )
        params = {} if inline else {"inline": "false"}

        os.makedirs(os.path.dirname(target_path) or ".", exist_ok=True)

        try:
            async with self._session.get(
                url, headers=self._headers(), params=params
            ) as resp:
                if resp.status >= 400:
                    body = await self._read_body(resp)
                    msg = (
                        f"download_artifact({filename}): HTTP "
                        f"{resp.status} — {body}"
                    )
                    log.error(msg)
                    raise ScrecError(
                        msg, status=resp.status, body=body
                    )

                try:
                    total = int(resp.headers.get("Content-Length") or 0)
                except Exception:
                    total = 0
                if total <= 0:
                    total = -1

                downloaded = 0
                with open(target_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(256 * 1024):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress_cb is not None:
                            try:
                                progress_cb(downloaded, total)
                            except Exception:
                                pass

            size = os.path.getsize(target_path)
            log.info(
                "ScrecClient: скачан артефакт %s → %s (%.1f КБ)",
                filename, target_path, size / 1024,
            )
            return target_path
        except ScrecError:
            raise
        except Exception as exc:
            log.exception(
                "download_artifact(%s): ошибка: %s", filename, exc
            )
            raise ScrecError(f"download_artifact({filename}): {exc}")

    # ------------------------------------------------------------------
    # Условная загрузка: проверка перед отправкой
    # ------------------------------------------------------------------
    async def check_artifact(
        self,
        record_id: str,
        filename: str,
        sha256: str,
    ) -> Dict[str, Any]:
        """
        HEAD /api/v1/records/{id}/artifacts/{filename} — спросить
        сервер, нужно ли загружать файл. Файл при этом НЕ
        отправляется.

        Args:
            record_id: ID записи.
            filename:  имя файла (basename).
            sha256:    SHA-256 клиента (hex-нижний регистр).

        Returns:
            {
              "skip": bool,             # True — файл есть, хэш совпал
              "exists": bool,           # файл есть на сервере
              "sha256": str,            # хэш существующего файла
              "size": int,
              "kind": str,
              "status_code": int,       # 200 или 404
            }
        """
        if self._session is None:
            raise ScrecError("aiohttp-сессия не открыта")

        url = (
            f"{self.base_url}/api/v1/records/{record_id}/artifacts/"
            f"{quote(filename)}"
        )

        headers = self._headers()
        if sha256:
            headers["X-Content-SHA256"] = sha256

        log.debug(
            "check_artifact: record=%s, filename=%r, sha=%s",
            record_id, filename, sha256[:12] if sha256 else "—",
        )

        try:
            async with self._session.head(
                url, headers=headers, allow_redirects=True,
            ) as resp:
                if resp.status >= 500:
                    body = await self._read_body(resp)
                    msg = (
                        f"check_artifact({filename}): HTTP "
                        f"{resp.status} — {body}"
                    )
                    log.error(msg)
                    raise ScrecError(
                        msg, status=resp.status, body=body
                    )

                info = {
                    "skip": False,
                    "exists": False,
                    "sha256": "",
                    "size": 0,
                    "kind": "",
                    "status_code": resp.status,
                }

                if resp.status == 200:
                    info["exists"] = True
                    info["skip"] = (
                        resp.headers.get("X-Artifact-Skip") == "true"
                    )
                    info["sha256"] = (
                        resp.headers.get("X-Artifact-SHA256") or ""
                    )
                    try:
                        info["size"] = int(
                            resp.headers.get("X-Artifact-Size") or 0
                        )
                    except (TypeError, ValueError):
                        info["size"] = 0
                    info["kind"] = (
                        resp.headers.get("X-Artifact-Kind") or ""
                    )

                log.debug(
                    "check_artifact: %s → exists=%s, skip=%s, "
                    "server_sha=%s",
                    filename, info["exists"], info["skip"],
                    info["sha256"][:12] if info["sha256"] else "—",
                )
                return info
        except ScrecError:
            raise
        except aiohttp.ClientConnectorError as exc:
            msg = f"check_artifact: ошибка подключения к {url}: {exc}"
            log.error(msg)
            raise ScrecError(msg)
        except aiohttp.ServerTimeoutError:
            msg = (
                f"check_artifact: таймаут ({self._read_timeout:.0f} с)"
            )
            log.error(msg)
            raise ScrecError(msg)
        except Exception as exc:
            log.exception("check_artifact: ошибка: %s", exc)
            raise ScrecError(f"check_artifact: {exc}")

    async def check_artifacts_batch(
        self,
        record_id: str,
        items: List[Dict[str, str]],
    ) -> Dict[str, Any]:
        """
        POST /api/v1/records/{id}/artifacts/check — пакетная
        проверка нескольких файлов одним запросом.

        Args:
            record_id: ID записи.
            items:     список [{"filename": str, "sha256": str}, ...]

        Returns:
            {
              "id": str,
              "results": [
                {"filename": str, "sha256": str, "skip": bool,
                 "reason": str, "size": int, "kind": str},
                ...
              ],
            }
        """
        if self._session is None:
            raise ScrecError("aiohttp-сессия не открыта")

        if not items:
            return {"id": record_id, "results": []}

        log.debug(
            "check_artifacts_batch: record=%s, items=%d",
            record_id, len(items),
        )

        result = await self._request_json(
            "POST",
            f"/api/v1/records/{record_id}/artifacts/check",
            json_body={"artifacts": items},
            context=f"check_artifacts_batch({record_id})",
        )

        results = result.get("results") or []
        skipped = sum(1 for r in results if r.get("skip"))
        log.info(
            "check_artifacts_batch: record=%s — проверено %d, "
            "skip=%d, upload=%d",
            record_id, len(results), skipped,
            len(results) - skipped,
        )
        return result

    async def upload_artifact_path(
        self,
        record_id: str,
        kind: str,
        file_path: str,
        *,
        progress_cb: Optional[Any] = None,
        skip_if_hash_matches: bool = True,
    ) -> Dict[str, Any]:
        """
        POST /api/v1/records/{id}/artifacts — загрузить артефакт.

        Если skip_if_hash_matches=True — сначала спрашивает сервер
        через HEAD, нужно ли загружать файл. Если файл уже есть с
        таким же хэшем — не отправляет содержимое (экономит трафик).

        Args:
            record_id:             ID записи.
            kind:                  "video" / "audio" / "attachment" / ...
            file_path:             путь к файлу на диске.
            progress_cb:           необязательный колбэк.
            skip_if_hash_matches:  если True — использовать HEAD-
                                   проверку перед загрузкой.
        """
        if self._session is None:
            raise ScrecError("aiohttp-сессия не открыта")

        if not os.path.isfile(file_path):
            raise ScrecError(f"Файл не найден: {file_path}")

        url = f"{self.base_url}/api/v1/records/{record_id}/artifacts"

        try:
            total = os.path.getsize(file_path)
        except OSError:
            total = -1

        sha = _sha256_file(file_path)
        filename = sanitize_filename(os.path.basename(file_path))

        # --- Условная загрузка: спрашиваем сервер ---
        if skip_if_hash_matches and sha:
            try:
                info = await self.check_artifact(
                    record_id, filename, sha
                )
                if info.get("skip"):
                    log.info(
                        "upload_artifact: %s (%s) пропущен — "
                        "сервер уже имеет файл с таким хэшем "
                        "(size=%d, server_sha=%s)",
                        filename, kind, info.get("size", 0),
                        info.get("sha256", "")[:12],
                    )
                    return {
                        "id": record_id,
                        "filename": filename,
                        "size": info.get("size", 0),
                        "sha256": info.get("sha256", sha),
                        "skipped": True,
                        "reason": "sha256_match",
                    }
            except ScrecError as exc:
                # Если HEAD-запрос упал (например, старый сервер
                # не поддерживает эндпоинт) — продолжаем обычную
                # загрузку.
                log.warning(
                    "upload_artifact: HEAD-проверка не удалась "
                    "(%s) — отправляем файл как обычно", exc,
                )

        # --- Отправка файла ---
        try:
            with open(file_path, "rb") as fh:
                form = aiohttp.FormData()
                form.add_field(
                    "file",
                    fh,
                    filename=filename,
                    content_type="application/octet-stream",
                )
                form.add_field("kind", kind)
                if sha:
                    form.add_field("sha256", sha)

                async with self._session.post(
                    url, data=form, headers=self._headers()
                ) as resp:
                    if resp.status >= 400:
                        body = await self._read_body(resp)
                        msg = (
                            f"upload_artifact({kind}/{file_path}): "
                            f"HTTP {resp.status} — {body}"
                        )
                        log.error(msg)
                        raise ScrecError(
                            msg, status=resp.status, body=body
                        )
                    data = await resp.json()
                    log.info(
                        "ScrecClient: загружен артефакт %s (%s) → "
                        "record=%s, skipped=%s, reason=%s",
                        filename, kind, record_id,
                        data.get("skipped"), data.get("reason"),
                    )
                    if progress_cb is not None:
                        try:
                            progress_cb(total if total > 0 else 0,
                                        total)
                        except Exception:
                            pass
                    return data
        except ScrecError:
            raise
        except Exception as exc:
            log.exception(
                "upload_artifact(%s): ошибка: %s", file_path, exc
            )
            raise ScrecError(f"upload_artifact({file_path}): {exc}")

    # ------------------------------------------------------------------
    # Транскрипт и summary
    # ------------------------------------------------------------------
    async def get_transcript(self, record_id: str) -> str:
        return await self._request_json(
            "GET",
            f"/api/v1/records/{record_id}/transcript",
            context=f"get_transcript({record_id})",
        )

    async def get_summary(self, record_id: str) -> str:
        return await self._request_json(
            "GET",
            f"/api/v1/records/{record_id}/summary",
            context=f"get_summary({record_id})",
        )

    # ------------------------------------------------------------------
    # Поиск
    # ------------------------------------------------------------------
    async def search(
        self,
        query: str,
        project: str = "",
        limit: int = 50,
    ) -> Dict[str, Any]:
        return await self._request_json(
            "GET",
            "/api/v1/records/_/search",
            params={"q": query, "project": project, "limit": limit},
            context=f"search({query!r})",
        )

    # ------------------------------------------------------------------
    # Синхронизация
    # ------------------------------------------------------------------
    async def get_changes(
        self, since_revision: int, limit: int = 200
    ) -> Dict[str, Any]:
        return await self._request_json(
            "GET",
            "/api/v1/sync/changes",
            params={"since_revision": since_revision, "limit": limit},
            context=f"changes(since={since_revision})",
        )

    async def get_snapshot(self) -> Dict[str, Any]:
        return await self._request_json(
            "GET",
            "/api/v1/sync/snapshot",
            context="snapshot",
        )