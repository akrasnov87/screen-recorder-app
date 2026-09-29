"""
TranscribeClient — клиент к серверу AI Видео Транскрибатора.

Соответствует API из server/main.py:
  • POST /api/auth/login          — form-data access_key → JSON {"token": ...}
  • POST /api/process-upload      — multipart file + form-поля → JSON {"task_id": ...}
  • GET  /api/task-status/{id}    — Header X-Session-Token → JSON со статусом
  • POST /api/auth/verify         — проверка токена

Поле формы summary_prompt поддерживается сервером — передаётся
в Summarizer.summarize() как custom_prompt.

Особенности:
  • Файл передаётся в aiohttp потоково (file-like object),
    без чтения в память целиком — важно для больших аудио/видео.
  • Поддерживается отмена через asyncio.Event: если во время
    polling-а ожидания результата кто-то выставит event — метод
    прерывает цикл и возвращает {"status": "cancelled"}.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Dict, Optional

import aiohttp

from .logger import get_logger

log = get_logger(__name__)


class TranscribeError(RuntimeError):
    """Ошибка при обращении к серверу транскрибации."""
    def __init__(self, message: str, status: int = 0, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class _FileStreamWrapper:
    """
    Обёртка над обычным файловым объектом для aiohttp.FormData.

    aiohttp умеет работать с file-like объектами, у которых есть
    методы read() и seek() (или итерация). Мы передаём сюда
    синхронный open(..., "rb") и сообщаем aiohttp нужный filename
    и content_type через FormData.add_field.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fp = None
        self._size = 0

    def __enter__(self):
        self._fp = open(self._path, "rb")
        try:
            self._size = self._path.stat().st_size
        except OSError:
            self._size = 0
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._fp is not None:
            try:
                self._fp.close()
            except Exception:
                pass
            self._fp = None

    @property
    def file(self):
        return self._fp


class TranscribeClient:
    """Асинхронный клиент для сервиса транскрибации."""

    def __init__(
        self,
        base_url: str,
        session_token: Optional[str] = None,
        connect_timeout: float = 15.0,
        read_timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session_token = session_token
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._session: Optional[aiohttp.ClientSession] = None
        log.debug(
            "TranscribeClient создан: base_url=%s, connect=%.1f, read=%.1f",
            self.base_url, connect_timeout, read_timeout,
        )

    async def __aenter__(self) -> "TranscribeClient":
        timeout = aiohttp.ClientTimeout(
            total=None,
            connect=self._connect_timeout,
            sock_connect=self._connect_timeout,
            sock_read=self._read_timeout,
        )
        self._session = aiohttp.ClientSession(timeout=timeout)
        log.debug("Открыта aiohttp-сессия (connect=%.1f, read=%.1f)",
                  self._connect_timeout, self._read_timeout)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            log.debug("Закрыта aiohttp-сессия TranscribeClient")

    # ------------------------------------------------------------------
    # Вспомогательные
    # ------------------------------------------------------------------
    @staticmethod
    async def _read_body(resp: aiohttp.ClientResponse) -> str:
        try:
            text = await resp.text()
        except Exception:
            return ""
        text = text.strip()
        return (text[:500] + "…") if len(text) > 500 else text

    async def _raise_for_status(
        self,
        resp: aiohttp.ClientResponse,
        url: str,
        context: str,
    ) -> None:
        if resp.status < 400:
            return
        body = await self._read_body(resp)
        msg = f"{context}: HTTP {resp.status} от {url}"
        if body:
            msg += f" — {body}"
        log.error(msg)
        raise TranscribeError(msg, status=resp.status, body=body)

    def _auth_headers(self) -> Dict[str, str]:
        return (
            {"X-Session-Token": self.session_token}
            if self.session_token else {}
        )

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------
    async def login(self, access_key: str) -> Dict[str, Any]:
        """
        Логин: POST /api/auth/login с form-data access_key.
        Сервер возвращает {"success": true, "token": "...", "expires_in": ...}.
        """
        if self._session is None:
            raise TranscribeError("aiohttp-сессия не открыта")

        url = f"{self.base_url}/api/auth/login"
        log.info("Авторизация на сервере транскрибации: %s", url)

        form = aiohttp.FormData()
        form.add_field("access_key", access_key or "")

        try:
            async with self._session.post(url, data=form) as resp:
                log.debug("Ответ авторизации: HTTP %d", resp.status)
                await self._raise_for_status(resp, url, "Авторизация")

                try:
                    data = await resp.json()
                except Exception as exc:
                    body = await self._read_body(resp)
                    msg = (f"Авторизация: не удалось разобрать JSON "
                           f"(HTTP {resp.status}): {exc}. Тело: {body}")
                    log.error(msg)
                    raise TranscribeError(msg, status=resp.status, body=body)

                token = data.get("token")
                if not token:
                    body = str(data)[:500]
                    msg = f"Авторизация: сервер не вернул token. Ответ: {body}"
                    log.error(msg)
                    raise TranscribeError(msg, status=resp.status, body=body)

                self.session_token = token
                log.info("Авторизация успешна, token получен")
                return data
        except TranscribeError:
            raise
        except Exception as exc:
            log.exception("Ошибка авторизации: %s", exc)
            raise

    async def process_video(
        self,
        file_path: Path,
        summary_language: str = "ru",
        transcription_language: str = "auto",
        wait: bool = True,
        poll_interval: float = 2.0,
        summary_prompt: str = "",
        max_wait: float = 7200.0,
        simple_format: bool = False,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> Dict[str, Any]:
        """
        Загрузка файла: POST /api/process-upload.

        Файл передаётся потоково через file-like object — не читается
        в память целиком. Это критично для длинных записей: mp3 на
        2 часа ≈ 180 МБ, и держать такой объём в RAM накладно.

        Параметр cancel_event позволяет прервать ожидание результата:
        если он выставлен во время polling-а, метод вернёт
        {"status": "cancelled"} и не будет ждать завершения задачи
        на сервере.
        """
        if self._session is None:
            raise TranscribeError("aiohttp-сессия не открыта")

        url = f"{self.base_url}/api/process-upload"
        headers = self._auth_headers()

        size_mb = file_path.stat().st_size / 1024 / 1024 if file_path.exists() else 0
        log.info("Загрузка файла: %s (%.2f МБ) → %s",
                 file_path.name, size_mb, url)
        log.debug("Параметры: summary_language=%s, transcription_language=%s, "
                  "simple_format=%s, wait=%s, max_wait=%.0f, prompt=%d символов",
                  summary_language, transcription_language, simple_format,
                  wait, max_wait, len(summary_prompt or ""))

        # Проверяем отмену до старта
        if cancel_event is not None and cancel_event.is_set():
            log.info("Транскрибация отменена до загрузки файла")
            return {"status": "cancelled", "task_id": ""}

        # Открываем файл и передаём в aiohttp как file-like.
        # Формат: (filename, fileobj, content_type)
        #
        # ВАЖНО: aiohttp.FormData.add_field с file-like объектом
        # стримит данные, а не грузит их в память.
        try:
            with _FileStreamWrapper(file_path) as wrapper:
                form = aiohttp.FormData()
                form.add_field(
                    "file",
                    wrapper.file,
                    filename=file_path.name,
                    content_type="application/octet-stream",
                )
                form.add_field("summary_language", summary_language)
                form.add_field("transcription_language", transcription_language)
                form.add_field(
                    "simple_format", "true" if simple_format else "false"
                )
                if summary_prompt:
                    form.add_field("summary_prompt", summary_prompt)
                    log.info("Промпт отправлен на сервер (%d символов)",
                             len(summary_prompt))

                try:
                    async with self._session.post(
                        url, data=form, headers=headers
                    ) as resp:
                        log.debug("Ответ сервера: HTTP %d", resp.status)
                        await self._raise_for_status(resp, url, "Загрузка файла")

                        try:
                            data = await resp.json()
                        except Exception as exc:
                            body = await self._read_body(resp)
                            msg = (f"Загрузка файла: не удалось разобрать JSON "
                                   f"(HTTP {resp.status}): {exc}. Тело: {body}")
                            log.error(msg)
                            raise TranscribeError(
                                msg, status=resp.status, body=body
                            )

                        task_id = data.get("task_id")
                        log.info("Задача транскрибации создана: task_id=%s",
                                 task_id)

                        if wait and not task_id:
                            body = str(data)[:500]
                            msg = f"Сервер не вернул task_id. Ответ: {body}"
                            log.error(msg)
                            raise TranscribeError(
                                msg, status=resp.status, body=body
                            )

                        if not wait or not task_id:
                            log.debug(
                                "Ожидание результата не требуется, "
                                "возвращаем ответ"
                            )
                            return data

                        return await self._poll(
                            task_id, poll_interval, max_wait, cancel_event
                        )
                except TranscribeError:
                    raise
                except Exception as exc:
                    log.exception("Ошибка загрузки файла на транскрибацию: %s",
                                  exc)
                    raise
        except FileNotFoundError:
            msg = f"Файл не найден: {file_path}"
            log.error(msg)
            raise TranscribeError(msg)
        except PermissionError as exc:
            msg = f"Нет доступа к файлу {file_path}: {exc}"
            log.error(msg)
            raise TranscribeError(msg)

    async def _poll(
        self,
        task_id: str,
        interval: float,
        max_wait: float = 7200.0,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> Dict[str, Any]:
        """
        Опрос статуса: GET /api/task-status/{task_id} с X-Session-Token.

        Возвращает финальный JSON со status в ("completed", "error",
        "cancelled"). Если cancel_event выставлен — возвращает
        {"status": "cancelled"} немедленно, не дожидаясь завершения
        задачи на сервере.
        """
        url = f"{self.base_url}/api/task-status/{task_id}"
        headers = self._auth_headers()
        log.debug("Ожидание результата %s (интервал %.1f с, лимит %.0f с)",
                  task_id, interval, max_wait)

        t_start = time.monotonic()
        attempt = 0
        last_status = None

        while True:
            # --- Проверка отмены ---
            if cancel_event is not None and cancel_event.is_set():
                log.info(
                    "Транскрибация %s отменена пользователем "
                    "(poll #%d, прошло %.1f с)",
                    task_id, attempt, time.monotonic() - t_start,
                )
                return {
                    "status": "cancelled",
                    "error": "cancelled by user",
                    "task_id": task_id,
                }

            attempt += 1
            elapsed = time.monotonic() - t_start

            if elapsed > max_wait:
                msg = (f"Транскрибация {task_id}: превышен лимит ожидания "
                       f"({max_wait:.0f} с)")
                log.error(msg)
                return {"status": "error", "error": msg, "task_id": task_id}

            try:
                async with self._session.get(url, headers=headers) as resp:
                    if resp.status == 404:
                        body = await self._read_body(resp)
                        log.warning("Опрос %s: HTTP 404 — %s", task_id, body)
                    elif resp.status >= 400:
                        body = await self._read_body(resp)
                        log.warning("Опрос %s: HTTP %d — %s",
                                    task_id, resp.status, body)
                    else:
                        data = await resp.json()
                        status = data.get("status")
                        progress = data.get("progress", 0)
                        message = data.get("message", "")
                        if status != last_status:
                            log.info(
                                "Транскрибация %s: status=%s, progress=%s, "
                                "message=%r",
                                task_id, status, progress, message,
                            )
                            last_status = status
                        else:
                            log.debug(
                                "Poll #%d: status=%s, progress=%s (%.1f с)",
                                attempt, status, progress, elapsed,
                            )

                        if status in ("completed", "error"):
                            log.info("Транскрибация завершена: status=%s "
                                     "(за %d опросов, %.1f с)",
                                     status, attempt, elapsed)
                            return data
            except Exception as exc:
                log.warning("Ошибка при опросе статуса (попытка %d): %s",
                            attempt, exc)

            # --- Ожидание с возможностью отмены ---
            # asyncio.sleep не прерывается внешним событием,
            # поэтому ждём через wait_for на cancel_event.wait(),
            # ограничивая время интервалом.
            if cancel_event is not None:
                try:
                    await asyncio.wait_for(
                        cancel_event.wait(), timeout=interval
                    )
                    # event выставлен — выходим на следующей итерации
                    continue
                except asyncio.TimeoutError:
                    # интервал истёк, event не выставлен — идём на новый poll
                    continue
            else:
                await asyncio.sleep(interval)

    async def verify(self) -> bool:
        """POST /api/auth/verify — проверка валидности токена."""
        if self._session is None or not self.session_token:
            return False
        url = f"{self.base_url}/api/auth/verify"
        form = aiohttp.FormData()
        form.add_field("session_token", self.session_token)
        try:
            async with self._session.post(url, data=form) as resp:
                if resp.status >= 400:
                    return False
                data = await resp.json()
                return bool(data.get("valid"))
        except Exception as exc:
            log.warning("Проверка сессии провалена: %s", exc)
            return False