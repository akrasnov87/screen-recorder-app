"""
Минимальный асинхронный клиент к LiteLLM / любому OpenAI-совместимому API.

Использует endpoint `POST {base_url}/v1/chat/completions` (или
`{base_url}/chat/completions`, если base_url уже включает /v1).

Никаких зависимостей от openai SDK — только aiohttp.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

import aiohttp

from .logger import get_logger

log = get_logger(__name__)


class LiteLLMError(RuntimeError):
    """Ошибка при обращении к LiteLLM."""
    def __init__(self, message: str, status: int = 0, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def _join_chat_completions(base_url: str) -> str:
    """
    Склеивает base_url и путь до chat/completions.

    LiteLLM обычно живёт на `http://host:4000`, а его OpenAI-совместимый
    API — на `/v1/chat/completions`. Если пользователь уже указал /v1 —
    не удваиваем.
    """
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def _join_models(base_url: str) -> str:
    """То же, что _join_chat_completions, но для /models."""
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/models"
    return f"{base}/v1/models"


class LiteLLMClient:
    """Асинхронный клиент Chat Completions."""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        model: str = "gpt-4o-mini",
        connect_timeout: float = 15.0,
        read_timeout: float = 300.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or ""
        self.model = model
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._session: Optional[aiohttp.ClientSession] = None

        log.debug(
            "LiteLLMClient создан: base_url=%s, model=%s, "
            "connect=%.1f, read=%.1f",
            self.base_url, self.model, connect_timeout, read_timeout,
        )

    async def __aenter__(self) -> "LiteLLMClient":
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
    # Вспомогательные
    # ------------------------------------------------------------------
    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    async def _read_body(self, resp: aiohttp.ClientResponse) -> str:
        try:
            text = await resp.text()
        except Exception:
            return ""
        text = text.strip()
        return (text[:500] + "…") if len(text) > 500 else text

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------
    async def list_models(self) -> List[str]:
        """
        GET {base_url}/v1/models — быстрый способ проверить связность
        и валидность api_key. Не требует генерации.

        Возвращает список id моделей. Бросает LiteLLMError при HTTP >= 400.
        """
        if self._session is None:
            raise LiteLLMError("aiohttp-сессия не открыта")

        url = _join_models(self.base_url)
        log.info("LiteLLM list_models: url=%s", url)
        t0 = time.monotonic()
        try:
            async with self._session.get(url, headers=self._headers()) as resp:
                if resp.status >= 400:
                    body = await self._read_body(resp)
                    msg = f"LiteLLM models: HTTP {resp.status} от {url}"
                    if body:
                        msg += f" — {body}"
                    log.error(msg)
                    raise LiteLLMError(msg, status=resp.status, body=body)

                try:
                    data = await resp.json()
                except Exception as exc:
                    body = await self._read_body(resp)
                    msg = (f"LiteLLM models: не удалось разобрать JSON "
                           f"(HTTP {resp.status}): {exc}. Тело: {body}")
                    log.error(msg)
                    raise LiteLLMError(msg, status=resp.status, body=body)

                items = data.get("data") or []
                ids = [str(m.get("id", "")) for m in items if m.get("id")]
                elapsed = time.monotonic() - t0
                log.info("LiteLLM models: %d моделей получено (%.2f с)",
                         len(ids), elapsed)
                return ids
        except LiteLLMError:
            raise
        except asyncio.TimeoutError:
            msg = f"LiteLLM models: таймаут ({self._read_timeout:.0f} с)"
            log.error(msg)
            raise LiteLLMError(msg)
        except Exception as exc:
            log.exception("LiteLLM models: ошибка: %s", exc)
            raise LiteLLMError(str(exc))

    async def ping(self, timeout: float = 8.0) -> str:
        """
        Быстрая проверка связности и авторизации.

        1) Пытаемся GET /v1/models — если успех, возвращаем "models".
        2) Если /v1/models недоступен (404/405) — делаем chat-запрос
           с max_tokens=1 и одним словом "hi", ограничивая время.

        Возвращает строку с указанием, каким способом проверено:
          • "models" — быстрая проверка по /v1/models;
          • "chat"   — fallback через короткий chat completion.
        """
        if self._session is None:
            raise LiteLLMError("aiohttp-сессия не открыта")

        # --- Шаг 1: /v1/models ---
        try:
            await self.list_models()
            log.info("LiteLLM ping: /v1/models — OK")
            return "models"
        except LiteLLMError as exc:
            if exc.status in (404, 405):
                log.info("LiteLLM ping: /v1/models недоступен (%s), "
                         "пробуем chat", exc.status)
            else:
                # 401/403/5xx — уже осмысленная ошибка, не скрываем
                raise

        # --- Шаг 2: короткий chat ---
        # Временно урезаем read_timeout, чтобы пользователь не ждал
        # долгий ответ большой модели.
        old_read = self._read_timeout
        try:
            self._read_timeout = max(2.0, float(timeout))
            content = await self.chat_completion(
                system_prompt="",
                user_prompt="hi",
                max_tokens=1,
                temperature=0.0,
            )
            log.info("LiteLLM ping: chat — OK (ответ %d символов)",
                     len(content or ""))
            return "chat"
        finally:
            self._read_timeout = old_read

    async def chat_completion(
        self,
        system_prompt: str,
        user_prompt: str,
        model: Optional[str] = None,
        temperature: float = 0.25,
        max_tokens: int = 2200,
    ) -> str:
        """
        Один запрос Chat Completions. Возвращает `content` первого choice.
        Бросает LiteLLMError при HTTP >= 400 или некорректном ответе.
        """
        if self._session is None:
            raise LiteLLMError("aiohttp-сессия не открыта")

        url = _join_chat_completions(self.base_url)
        model_to_use = model or self.model
        messages: List[Dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})

        payload: Dict[str, Any] = {
            "model": model_to_use,
            "messages": messages,
            "temperature": float(temperature),
            "max_tokens": int(max_tokens),
            "stream": False,
        }

        log.info("LiteLLM chat: url=%s, model=%s, sys=%d симв, user=%d симв",
                 url, model_to_use, len(system_prompt or ""),
                 len(user_prompt or ""))

        t0 = time.monotonic()
        try:
            async with self._session.post(
                url, json=payload, headers=self._headers(),
            ) as resp:
                if resp.status >= 400:
                    body = await self._read_body(resp)
                    msg = f"LiteLLM: HTTP {resp.status} от {url}"
                    if body:
                        msg += f" — {body}"
                    log.error(msg)
                    raise LiteLLMError(msg, status=resp.status, body=body)

                try:
                    data = await resp.json()
                except Exception as exc:
                    body = await self._read_body(resp)
                    msg = (f"LiteLLM: не удалось разобрать JSON "
                           f"(HTTP {resp.status}): {exc}. Тело: {body}")
                    log.error(msg)
                    raise LiteLLMError(msg, status=resp.status, body=body)

                choices = data.get("choices") or []
                if not choices:
                    body = str(data)[:500]
                    msg = f"LiteLLM: пустой choices в ответе. Ответ: {body}"
                    log.error(msg)
                    raise LiteLLMError(msg, status=resp.status, body=body)

                content = (choices[0].get("message") or {}).get("content") or ""
                elapsed = time.monotonic() - t0
                if not content.strip():
                    log.warning("LiteLLM: пустой content (%.1f с)", elapsed)
                else:
                    log.info("LiteLLM ответ получен (%.1f с, %d символов)",
                             elapsed, len(content))
                return content
        except LiteLLMError:
            raise
        except asyncio.TimeoutError as exc:
            msg = f"LiteLLM: таймаут запроса ({self._read_timeout:.0f} с)"
            log.error(msg)
            raise LiteLLMError(msg)
        except Exception as exc:
            log.exception("LiteLLM: ошибка запроса: %s", exc)
            raise LiteLLMError(str(exc))