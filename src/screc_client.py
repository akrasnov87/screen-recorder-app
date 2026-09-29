"""Клиент к удалённому серверу синхронизации screc-server.

Реализует API из DOCS.md (v1.0.0):
  • GET  /health
  • GET  /api/v1/tree
  • GET  /api/v1/tree/{project}
  • GET  /api/v1/tree/{project}/{year}
  • GET  /api/v1/tree/{project}/{year}/{month}
  • POST /api/v1/records
  • GET  /api/v1/records/{id}
  • PATCH /api/v1/records/{id}
  • DELETE /api/v1/records/{id}
  • GET  /api/v1/records/{id}/video-url
  • PUT  /api/v1/records/{id}/video-url
  • GET  /api/v1/records/{id}/artifacts
  • GET  /api/v1/records/{id}/artifacts/{filename}
  • POST /api/v1/records/{id}/artifacts
  • DELETE /api/v1/records/{id}/artifacts/{filename}
  • GET  /api/v1/records/{id}/transcript
  • GET  /api/v1/records/{id}/summary
  • GET  /api/v1/records/_/search
  • GET  /api/v1/sync/changes
  • GET  /api/v1/sync/snapshot

Клиент полностью асинхронный (aiohttp), поддерживает потоковую
загрузку и скачивание артефактов.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import aiohttp

from .logger import get_logger

log = get_logger(__name__)


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
            "ScrecClient создан: base_url=%s, connect=%.1f, read=%.1f",
            self.base_url, self._connect_timeout, self._read_timeout,
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
            artifact_dir: папка сессии, где лежат артефакты.

        Returns:
            {"id": ..., "revision": ..., "action": ...,
             "path": ..., "artifacts": [...]}
        """
        if self._session is None:
            raise ScrecError("aiohttp-сессия не открыта")

        url = f"{self.base_url}/api/v1/records"
        artifacts = artifacts or []

        log.info(
            "ScrecClient.publish_record: project=%r, folder=%r, "
            "artifacts=%d",
            payload.get("project"), payload.get("folder_name"),
            len(artifacts),
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
                form.add_field(
                    "files",
                    fh,
                    filename=os.path.basename(full_path),
                    content_type="application/octet-stream",
                )
                form.add_field("kinds", kind)

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
                    "action=%s, revision=%s",
                    data.get("id"), data.get("action"),
                    data.get("revision"),
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

    async def delete_record(
        self, record_id: str, hard: bool = False
    ) -> Dict[str, Any]:
        return await self._request_json(
            "DELETE",
            f"/api/v1/records/{record_id}",
            params={"hard": "true" if hard else "false"},
            context=f"delete_record({record_id})",
        )

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

    async def upload_artifact_path(
        self,
        record_id: str,
        kind: str,
        file_path: str,
        *,
        progress_cb: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        POST /api/v1/records/{id}/artifacts — загрузить артефакт.

        Файл читается с диска потоково через file-like объект.
        Для больших медиафайлов это критично.

        Args:
            record_id:   ID записи.
            kind:        "video" / "audio" / "attachment" / ...
            file_path:   путь к файлу на диске.
            progress_cb: необязательный колбэк
                         (bytes_sent, total_bytes).
                         total_bytes = -1, если размер неизвестен.
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

        try:
            with open(file_path, "rb") as fh:
                form = aiohttp.FormData()
                form.add_field(
                    "file",
                    fh,
                    filename=os.path.basename(file_path),
                    content_type="application/octet-stream",
                )
                form.add_field("kind", kind)

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
                        "record=%s",
                        os.path.basename(file_path), kind, record_id,
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

    async def delete_artifact(
        self, record_id: str, filename: str
    ) -> Dict[str, Any]:
        return await self._request_json(
            "DELETE",
            f"/api/v1/records/{record_id}/artifacts/{quote(filename)}",
            context=f"delete_artifact({filename})",
        )

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