"""Модальный диалог ожидания ответа от LLM.

Используется в окне «Скрам»: пользователь нажал кнопку
«Анализ поручений и TFS…» — открывается диалог с прогрессом.
После завершения запроса диалог закрывается и эмитит
finished_ok/failed с текстом — слушатель (ScrumWindow)
передаёт результат в AnalysisResultWidget.

Изменения:
  • Модуль создан.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton,
    QVBoxLayout, QWidget,
)

from ..litellm_client import LiteLLMClient, LiteLLMError
from ..logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Воркер
# ---------------------------------------------------------------------------
class _LlmWorker(QThread):
    """Выполняет запрос к LiteLLM в фоновом потоке."""

    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.25,
        max_tokens: int = 4000,
        connect_timeout: float = 15.0,
        read_timeout: float = 600.0,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._base_url = base_url
        self._api_key = api_key
        self._model = model
        self._system_prompt = system_prompt
        self._user_prompt = user_prompt
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout

    def run(self) -> None:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                result = loop.run_until_complete(self._run())
            finally:
                try:
                    loop.run_until_complete(loop.shutdown_asyncgens())
                finally:
                    loop.close()
            self.finished_ok.emit(result)
        except LiteLLMError as exc:
            log.error("LlmWorker: LiteLLMError: %s", exc)
            self.failed.emit(str(exc))
        except Exception as exc:
            log.exception("LlmWorker: ошибка: %s", exc)
            self.failed.emit(str(exc))

    async def _run(self) -> str:
        async with LiteLLMClient(
            base_url=self._base_url,
            api_key=self._api_key,
            model=self._model,
            connect_timeout=self._connect_timeout,
            read_timeout=self._read_timeout,
        ) as client:
            return await client.chat_completion(
                system_prompt=self._system_prompt,
                user_prompt=self._user_prompt,
                model=self._model,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
            )


# ---------------------------------------------------------------------------
# Диалог
# ---------------------------------------------------------------------------
class AnalysisWorkerDialog(QDialog):
    """
    Модальный диалог ожидания ответа от LLM.

    Эмитит сигналы после завершения запроса:
      • finished_ok(text) — успешный ответ;
      • failed(error)     — ошибка.

    Слушатель передаёт данные в AnalysisResultWidget и
    закрывает диалог.
    """

    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        system_prompt: str,
        user_prompt: str,
        prompt_name: str = "",
        temperature: float = 0.25,
        max_tokens: int = 4000,
        connect_timeout: float = 15.0,
        read_timeout: float = 600.0,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Выполнение анализа")
        self.setModal(True)
        self.setMinimumWidth(480)
        self.setMinimumHeight(160)

        self._base_url = base_url
        self._api_key = api_key
        self._model = model
        self._system_prompt = system_prompt
        self._user_prompt = user_prompt
        self._prompt_name = prompt_name
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout

        self._worker: Optional[_LlmWorker] = None
        self._cancelled = False

        self._build_ui()

        # Автозапуск — пользователь уже нажал кнопку.
        from PySide6.QtCore import QTimer
        QTimer.singleShot(50, self._start_request)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        info = QLabel(
            f"<b>Промпт:</b> {self._prompt_name or '—'}<br>"
            f"<span style='color:#666'>"
            f"Модель: <code>{self._model}</code>"
            f"</span>"
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        layout.addWidget(self.progress)

        self.status_label = QLabel(
            "Отправка запроса к LLM… Пожалуйста, подождите.\n\n"
            "Обычно ответ приходит за 5–60 секунд, но для больших "
            "списков может занять несколько минут."
        )
        self.status_label.setStyleSheet(
            "QLabel { color: #666; font-style: italic; }"
        )
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        btn_row = QHBoxLayout()
        btn_row.addStretch()

        self.cancel_btn = QPushButton("Отмена")
        self.cancel_btn.setToolTip(
            "Прервать ожидание ответа."
        )
        self.cancel_btn.clicked.connect(self._on_cancel)
        btn_row.addWidget(self.cancel_btn)

        layout.addLayout(btn_row)

    # ------------------------------------------------------------------
    # Запуск
    # ------------------------------------------------------------------
    def _start_request(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return

        self._worker = _LlmWorker(
            base_url=self._base_url,
            api_key=self._api_key,
            model=self._model,
            system_prompt=self._system_prompt,
            user_prompt=self._user_prompt,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            connect_timeout=self._connect_timeout,
            read_timeout=self._read_timeout,
            parent=self,
        )
        self._worker.finished_ok.connect(self._on_result)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._on_worker_done)
        self._worker.start()

        log.info(
            "AnalysisWorkerDialog: запрос отправлен "
            "(model=%s, system=%d симв., user=%d симв.)",
            self._model,
            len(self._system_prompt or ""),
            len(self._user_prompt or ""),
        )

    def _on_result(self, text: str) -> None:
        if self._cancelled:
            return
        log.info(
            "AnalysisWorkerDialog: получен ответ (%d символов)",
            len(text or ""),
        )
        self.finished_ok.emit(text or "")
        self.accept()

    def _on_failed(self, error: str) -> None:
        if self._cancelled:
            return
        log.error(
            "AnalysisWorkerDialog: ошибка запроса: %s", error
        )
        self.failed.emit(error or "Неизвестная ошибка")
        self.accept()

    def _on_worker_done(self) -> None:
        self._worker = None

    def _on_cancel(self) -> None:
        self._cancelled = True
        if self._worker is not None and self._worker.isRunning():
            log.info(
                "AnalysisWorkerDialog: пользователь прервал ожидание"
            )
            try:
                self._worker.requestInterruption()
            except Exception:
                pass
        self.reject()

    def closeEvent(self, event) -> None:
        if self._worker is not None and self._worker.isRunning():
            try:
                self._worker.requestInterruption()
            except Exception:
                pass
        super().closeEvent(event)