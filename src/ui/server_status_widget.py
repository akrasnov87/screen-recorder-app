"""Виджет индикаторов состояния внешних серверов.

Используется в окне «Записи» (правый верхний угол меню-бара).
Показывает компактные цветные кружки для:
  • сервера транскрибации;
  • сервера синхронизации (хранилище);
  • LiteLLM;
  • Bitrix24;
  • TFS.

Каждый индикатор:
  • серый   — не настроен или недоступен;
  • жёлтый  — настроен, но не хватает параметров;
  • оранж.  — идёт проверка;
  • зелёный — доступен.

Проверка выполняется в фоне (QThread), чтобы не блокировать UI.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QMenu, QToolTip, QWidget,
)

from ..logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Внутренний воркер: проверяет все серверы параллельно
# ---------------------------------------------------------------------------
class _ServerCheckWorker(QThread):
    """Проверяет доступность всех серверов асинхронно."""

    finished = Signal(dict)

    def __init__(
        self,
        config_manager,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._config_manager = config_manager

    def run(self) -> None:
        try:
            result = asyncio.run(self._check_all())
        except Exception as exc:
            log.exception("ServerStatusWidget: ошибка проверки: %s", exc)
            result = {}
        self.finished.emit(result)

    async def _check_all(self) -> Dict[str, Dict[str, Any]]:
        """Возвращает словарь {key: {"ok": bool, "state": str, "hint": str}}."""
        results: Dict[str, Dict[str, Any]] = {}

        # Проверяем всё параллельно.
        tasks = {
            "transcribe": self._check_transcribe(),
            "storage": self._check_storage(),
            "litellm": self._check_litellm(),
            "bitrix": self._check_bitrix(),
            "tfs": self._check_tfs(),
        }
        for key, coro in tasks.items():
            try:
                results[key] = await coro
            except Exception as exc:
                results[key] = {
                    "state": "error",
                    "hint": f"Ошибка проверки: {exc}",
                }
        return results

    # ------------------------------------------------------------------
    # Отдельные проверки
    # ------------------------------------------------------------------
    async def _check_transcribe(self) -> Dict[str, Any]:
        try:
            cfg = self._config_manager.get_transcribe_settings()
        except Exception as exc:
            return {"state": "error", "hint": f"Настройки: {exc}"}

        url = str(cfg.get("url") or "").strip()
        key = str(cfg.get("access_key") or "")

        if not url:
            return {
                "state": "not_configured",
                "hint": "Сервер транскрибации не настроен "
                        "(Настройки → Транскрибация)",
            }

        # Проверяем доступность URL через login (или хотя бы /).
        try:
            import aiohttp
            timeout = aiohttp.ClientTimeout(
                total=5.0, connect=3.0, sock_read=5.0,
            )
            async with aiohttp.ClientSession(timeout=timeout) as s:
                # Быстрая проверка — попытка логина.
                form = aiohttp.FormData()
                form.add_field("access_key", key or "")
                async with s.post(
                    f"{url.rstrip('/')}/api/auth/login", data=form,
                ) as r:
                    if r.status < 400:
                        return {
                            "state": "ok",
                            "hint": f"✓ Сервер транскрибации: {url}",
                        }
                    return {
                        "state": "error",
                        "hint": f"Сервер транскрибации недоступен: "
                                f"HTTP {r.status} ({url})",
                    }
        except Exception as exc:
            return {
                "state": "error",
                "hint": f"Сервер транскрибации недоступен: {exc}",
            }

    async def _check_storage(self) -> Dict[str, Any]:
        try:
            cfg = self._config_manager.get_sync_settings()
        except Exception as exc:
            return {"state": "error", "hint": f"Настройки: {exc}"}

        url = str(cfg.get("base_url") or "").strip()
        key = str(cfg.get("api_key") or "")

        if not url:
            return {
                "state": "not_configured",
                "hint": "Сервер синхронизации не настроен "
                        "(Настройки → Синхронизация)",
            }
        if not key:
            return {
                "state": "warning",
                "hint": "Не задан API-ключ синхронизации",
            }

        try:
            import aiohttp
            timeout = aiohttp.ClientTimeout(
                total=5.0, connect=3.0, sock_read=5.0,
            )
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(f"{url.rstrip('/')}/health") as r:
                    if r.status >= 400:
                        return {
                            "state": "error",
                            "hint": f"Хранилище недоступно: "
                                    f"HTTP {r.status} ({url})",
                        }
                    data = await r.json()
                    if data.get("status") == "ok":
                        return {
                            "state": "ok",
                            "hint": f"✓ Хранилище доступно: {url}\n"
                                    f"Записей: {data.get('records_count', '?')}, "
                                    f"ревизия: {data.get('revision', '?')}",
                        }
                    return {
                        "state": "error",
                        "hint": f"Сервер вернул status={data.get('status')!r}",
                    }
        except Exception as exc:
            return {
                "state": "error",
                "hint": f"Хранилище недоступно: {exc}",
            }

    async def _check_litellm(self) -> Dict[str, Any]:
        try:
            cfg = self._config_manager.get_summarizer_settings()
        except Exception as exc:
            return {"state": "error", "hint": f"Настройки: {exc}"}

        if not cfg.get("enabled"):
            return {
                "state": "not_configured",
                "hint": "Суммаризация отключена "
                        "(Настройки → Суммаризация)",
            }

        provider = str(cfg.get("provider") or "server").lower()
        if provider != "litellm":
            return {
                "state": "not_configured",
                "hint": f"Провайдер суммаризации: {provider} "
                        f"(LiteLLM не используется)",
            }

        l = cfg.get("litellm") or {}
        url = str(l.get("base_url") or "").strip()
        if not url:
            return {
                "state": "warning",
                "hint": "Не задан base_url LiteLLM",
            }

        try:
            import aiohttp
            timeout = aiohttp.ClientTimeout(
                total=5.0, connect=3.0, sock_read=5.0,
            )
            async with aiohttp.ClientSession(timeout=timeout) as s:
                headers = {}
                if l.get("api_key"):
                    headers["Authorization"] = (
                        f"Bearer {l['api_key']}"
                    )
                async with s.get(
                    f"{url.rstrip('/')}/v1/models", headers=headers,
                ) as r:
                    if r.status < 400:
                        return {
                            "state": "ok",
                            "hint": f"✓ LiteLLM доступен: {url}",
                        }
                    if r.status in (404, 405):
                        # Сервер без /v1/models, но живой.
                        return {
                            "state": "ok",
                            "hint": f"✓ LiteLLM доступен: {url} "
                                    f"(без /v1/models)",
                        }
                    return {
                        "state": "error",
                        "hint": f"LiteLLM недоступен: "
                                f"HTTP {r.status} ({url})",
                    }
        except Exception as exc:
            return {
                "state": "error",
                "hint": f"LiteLLM недоступен: {exc}",
            }

    async def _check_bitrix(self) -> Dict[str, Any]:
        try:
            cfg = self._config_manager.get_bitrix_settings()
        except Exception as exc:
            return {"state": "error", "hint": f"Настройки: {exc}"}

        if not cfg.get("enabled"):
            return {
                "state": "not_configured",
                "hint": "Bitrix24 отключён",
            }

        webhook = str(cfg.get("webhook_url") or "").strip()
        if not webhook:
            return {
                "state": "warning",
                "hint": "Bitrix24 включён, но вебхук не задан",
            }

        try:
            from ..bitrix_client import Bitrix24Client
            async with Bitrix24Client(
                webhook_url=webhook,
                connect_timeout=5.0,
                read_timeout=5.0,
                max_message_chars=1000,
            ) as client:
                name = await client.ping()
                return {
                    "state": "ok",
                    "hint": f"✓ Bitrix24 доступен: {name}",
                }
        except Exception as exc:
            return {
                "state": "error",
                "hint": f"Bitrix24 недоступен: {exc}",
            }

    async def _check_tfs(self) -> Dict[str, Any]:
        try:
            cfg = self._config_manager.get_tfs_settings()
        except Exception as exc:
            return {"state": "error", "hint": f"Настройки: {exc}"}

        if not cfg.get("enabled"):
            return {
                "state": "not_configured",
                "hint": "TFS отключён",
            }

        url = str(cfg.get("base_url") or "").strip()
        pat = str(cfg.get("api_key") or "")
        if not url:
            return {
                "state": "warning",
                "hint": "TFS включён, но base_url не задан",
            }
        if not pat:
            return {
                "state": "warning",
                "hint": "TFS включён, но PAT не задан",
            }

        try:
            from ..tfs_client import TfsClient
            async with TfsClient(
                base_url=url,
                pat=pat,
                api_version=cfg.get("api_version", "7.0"),
                connect_timeout=5.0,
                read_timeout=5.0,
            ) as client:
                projects = await client.list_projects()
                return {
                    "state": "ok",
                    "hint": f"✓ TFS доступен: "
                            f"{len(projects)} проектов",
                }
        except Exception as exc:
            return {
                "state": "error",
                "hint": f"TFS недоступен: {exc}",
            }


# ---------------------------------------------------------------------------
# Виджет индикаторов
# ---------------------------------------------------------------------------
class ServerStatusWidget(QWidget):
    """
    Панель индикаторов состояния серверов.

    Размещается справа в меню-баре окна «Записи»:
        menu_bar.setCornerWidget(status_widget, Qt.TopRightCorner)
    """

    # Порядок отображения индикаторов.
    INDICATORS = (
        ("transcribe", "Транскрибация"),
        ("storage", "Хранилище"),
        ("litellm", "LiteLLM"),
        ("bitrix", "Bitrix24"),
        ("tfs", "TFS"),
    )

    # Цвета состояний.
    _COLORS = {
        "ok":              "#2E7D32",   # зелёный
        "warning":         "#F9A825",   # жёлтый
        "error":           "#C62828",   # красный
        "not_configured":  "#9E9E9E",   # серый
        "checking":        "#FF9800",   # оранжевый
    }

    _STATE_HINTS = {
        "ok":              "Доступен",
        "warning":         "Настроен частично",
        "error":           "Ошибка подключения",
        "not_configured":  "Не настроен",
        "checking":        "Проверка…",
    }

    def __init__(
        self,
        config_manager,
        parent: Optional[QWidget] = None,
        *,
        auto_check_interval_ms: int = 120_000,  # 2 минуты
    ) -> None:
        super().__init__(parent)
        self._config_manager = config_manager

        self._states: Dict[str, Dict[str, Any]] = {}
        for key, _ in self.INDICATORS:
            self._states[key] = {
                "state": "not_configured",
                "hint": "Ещё не проверялось",
            }

        self._labels: Dict[str, QLabel] = {}
        self._worker: Optional[_ServerCheckWorker] = None

        self._build_ui()

        # Первая проверка — сразу после создания.
        QTimer.singleShot(500, self.refresh)

        # Автопроверка раз в N секунд.
        if auto_check_interval_ms > 0:
            self._auto_timer = QTimer(self)
            self._auto_timer.setInterval(auto_check_interval_ms)
            self._auto_timer.timeout.connect(self.refresh)
            self._auto_timer.start()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 12, 0)
        layout.setSpacing(4)

        for key, label in self.INDICATORS:
            dot = QLabel("●")
            dot.setStyleSheet(
                f"QLabel {{ color: {self._COLORS['not_configured']};"
                f" font-size: 14px; }}"
            )
            dot.setCursor(Qt.CursorShape.WhatsThisCursor)
            dot.setToolTip(f"{label}: Ещё не проверялось")
            dot.installEventFilter(self)
            dot.setProperty("_server_key", key)
            dot.setProperty("_server_label", label)

            self._labels[key] = dot
            layout.addWidget(dot)

        layout.addStretch()

    def eventFilter(self, obj, event):
        """Показывает всплывающую подсказку по клику на кружок."""
        try:
            from PySide6.QtCore import QEvent
            if event.type() == QEvent.Type.MouseButtonPress:
                key = obj.property("_server_key")
                label = obj.property("_server_label")
                if key:
                    state = self._states.get(key, {})
                    hint = state.get("hint", "—")
                    QToolTip.showText(
                        obj.mapToGlobal(obj.rect().bottomLeft()),
                        f"<b>{label}</b><br>{hint}",
                        obj,
                    )
        except Exception:
            pass
        return super().eventFilter(obj, event)

    # ------------------------------------------------------------------
    # Обновление
    # ------------------------------------------------------------------
    def refresh(self) -> None:
        """Запускает фоновую проверку всех серверов."""
        if self._worker is not None and self._worker.isRunning():
            log.debug(
                "ServerStatusWidget: проверка уже выполняется"
            )
            return

        # Визуально — «проверка» (оранжевый).
        for key, _ in self.INDICATORS:
            self._set_dot_state(key, "checking", "Проверка…")

        self._worker = _ServerCheckWorker(
            self._config_manager, parent=self
        )
        self._worker.finished.connect(self._on_checked)
        self._worker.start()

    def _on_checked(self, results: Dict[str, Dict[str, Any]]) -> None:
        """Применяет результаты фоновой проверки."""
        for key, _ in self.INDICATORS:
            r = results.get(key) or {}
            state = str(r.get("state") or "not_configured")
            hint = str(r.get("hint") or "—")
            self._set_dot_state(key, state, hint)

        log.debug(
            "ServerStatusWidget: обновлены состояния: %s",
            {k: v.get("state")
             for k, v in (results or {}).items()},
        )

    def _set_dot_state(
        self, key: str, state: str, hint: str,
    ) -> None:
        """Обновляет цвет и подсказку для одного индикатора."""
        self._states[key] = {"state": state, "hint": hint}

        label_widget = self._labels.get(key)
        if label_widget is None:
            return

        color = self._COLORS.get(state, self._COLORS["not_configured"])
        label_widget.setStyleSheet(
            f"QLabel {{ color: {color}; font-size: 14px; }}"
        )

        key_label = ""
        for k, lbl in self.INDICATORS:
            if k == key:
                key_label = lbl
                break

        state_label = self._STATE_HINTS.get(state, state)
        label_widget.setToolTip(
            f"{key_label}: {state_label}\n{hint}"
        )
    
    def closeEvent(self, event) -> None:
        """Останавливает воркер при закрытии."""
        try:
            if (self._worker is not None
                    and self._worker.isRunning()):
                self._worker.requestInterruption()
                self._worker.wait(2000)
        except Exception as exc:
            log.debug(
                "ServerStatusWidget.closeEvent: %s", exc,
            )
        super().closeEvent(event)