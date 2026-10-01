"""Оркестратор: управление ВМ Yandex при недоступности транскрибации.

Логика:
  1. Проверить доступность сервиса транскрибации.
  2. Если недоступен — проверить настройки ВМ.
  3. Если ВМ не задана — вернуть решение «пропустить транскрибацию».
  4. Если ВМ задана — проверить её расписание:
     • если ВМ должна работать сейчас — ждать её запуска;
     • если ВМ должна быть выключена — продлить расписание
       и ждать запуска.
  5. Вернуть решение «транскрибация возможна» + отпечаток
     (было ли изменено расписание).
  6. После обработки — восстановить расписание, если пользователь
     согласен.

Изменения:
  • Модуль создан.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from .logger import get_logger
from .yandex_vm_manager import (
    YandexVMManager,
    ping_transcribe_service,
    wait_for_transcribe_service,
)

log = get_logger(__name__)


@dataclass
class TranscribeVMSession:
    """Сессия управления ВМ для одной обработки."""
    vm_manager: Optional[YandexVMManager] = None
    schedule_modified: bool = False
    schedule_backup: str = ""
    schedule_path: str = ""
    original_schedule: str = ""
    started_at: Optional[datetime] = None
    schedule_was_modified_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schedule_modified": self.schedule_modified,
            "schedule_path": self.schedule_path,
            "schedule_backup": self.schedule_backup,
        }


@dataclass
class PrepareResult:
    """Результат подготовки к транскрибации."""
    can_transcribe: bool = False
    skipped_reason: str = ""
    vm_session: Optional[TranscribeVMSession] = None
    vm_diagnose: Dict[str, Any] = field(default_factory=dict)
    error: str = ""


class TranscribeVMController:
    """
    Управляет ВМ Yandex при недоступности сервиса транскрибации.

    Использование:
        controller = TranscribeVMController(config_manager)

        # Перед обработкой:
        result = await controller.prepare_for_transcribe(
            progress_cb=...,
            cancel_event=...,
        )
        if not result.can_transcribe:
            # Пропускаем транскрибацию
            ...

        # После обработки:
        await controller.finish_session(
            vm_session=result.vm_session,
            on_ask_user=...,
        )
    """

    def __init__(self, config_manager) -> None:
        self.config_manager = config_manager

    # ------------------------------------------------------------------
    # Настройки
    # ------------------------------------------------------------------
    def _get_vm_settings(self) -> Dict[str, Any]:
        try:
            return self.config_manager.get_yandex_vm_settings()
        except Exception as exc:
            log.warning(
                "Не удалось прочитать настройки ВМ: %s", exc
            )
            return {}

    def _get_transcribe_url(self) -> str:
        try:
            return (
                self.config_manager.get_transcribe_settings()
                .get("url", "")
                or ""
            ).strip()
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Основной сценарий
    # ------------------------------------------------------------------
    async def prepare_for_transcribe(
        self,
        *,
        progress_cb: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> PrepareResult:
        """
        Готовит ВМ к транскрибации (если нужно).

        Возвращает PrepareResult с решением:
          • can_transcribe=True — можно продолжать обработку;
          • can_transcribe=False — транскрибацию нужно пропустить.
        """
        result = PrepareResult()

        transcribe_url = self._get_transcribe_url()
        if not transcribe_url:
            result.skipped_reason = (
                "URL сервера транскрибации не задан"
            )
            log.info(
                "TranscribeVMController: URL транскрибации не задан — "
                "транскрибация будет пропущена"
            )
            return result

        if progress_cb:
            progress_cb("Проверка доступности сервиса транскрибации…")

        # --- 1. Проверяем доступность сервиса ---
        service_ok = await ping_transcribe_service(
            transcribe_url, timeout=5.0
        )

        if service_ok:
            log.info(
                "TranscribeVMController: сервис транскрибации доступен"
            )
            result.can_transcribe = True
            return result

        log.warning(
            "TranscribeVMController: сервис транскрибации недоступен — "
            "проверяем ВМ"
        )

        # --- 2. Проверяем настройки ВМ ---
        vm_settings = self._get_vm_settings()
        vm_name = str(
            vm_settings.get("transcribe_vm_name", "") or ""
        ).strip()

        if not vm_name:
            result.skipped_reason = (
                "Сервис транскрибации недоступен, ВМ для "
                "транскрибации не задана в настройках. "
                "Транскрибация будет пропущена."
            )
            log.warning(
                "TranscribeVMController: ВМ не задана — "
                "транскрибация пропускается"
            )
            return result

        # --- 3. Проверяем, что папка ВМ существует ---
        vm_manager = YandexVMManager(vm_settings)
        if not vm_manager.is_vm_dir_exists():
            result.error = (
                f"Папка ВМ «{vm_name}» не найдена в "
                f"{vm_settings.get('root_path', '')}"
            )
            result.skipped_reason = (
                f"{result.error}. Транскрибация будет пропущена."
            )
            log.error(result.error)
            return result

        # --- 4. Диагностика ВМ ---
        if progress_cb:
            progress_cb(
                f"Сервис недоступен. Проверка расписания ВМ "
                f"«{vm_name}»…"
            )

        diagnose = vm_manager.diagnose()
        result.vm_diagnose = diagnose

        log.info(
            "TranscribeVMController: ВМ «%s» — should_run_now=%s, "
            "schedule_should_run=%s, exception=%s, reason=%r",
            vm_name, diagnose["should_run_now"],
            diagnose["schedule_should_run"],
            diagnose["exception"], diagnose["reason"],
        )

        # --- 5. Если ВМ должна работать — ждём её запуска ---
        if diagnose["should_run_now"]:
            if progress_cb:
                progress_cb(
                    f"По расписанию ВМ «{vm_name}» должна работать. "
                    f"Ожидание запуска…"
                )
            log.info(
                "TranscribeVMController: ВМ должна работать — "
                "ждём запуска"
            )
        else:
            # --- 6. ВМ должна быть выключена — продлеваем расписание ---
            if progress_cb:
                progress_cb(
                    f"По расписанию ВМ «{vm_name}» выключена. "
                    f"Продление расписания…"
                )

            extend_result = vm_manager.extend_schedule_for_now()
            if not extend_result["ok"]:
                result.error = (
                    f"Не удалось продлить расписание ВМ: "
                    f"{extend_result['message']}"
                )
                result.skipped_reason = (
                    f"{result.error}. Транскрибация будет пропущена."
                )
                log.error(result.error)
                return result

            log.info(
                "TranscribeVMController: расписание продлено — %s",
                extend_result["message"],
            )

            if progress_cb:
                progress_cb(
                    f"Расписание продлено. Ожидание запуска ВМ…"
                )

        # --- 7. Ждём, пока сервис станет доступен ---
        # Создаём сессию только если расписание было изменено —
        # иначе нечего восстанавливать после обработки.
        session: Optional[TranscribeVMSession] = None
        if vm_manager._was_modified:
            session = TranscribeVMSession(
                vm_manager=vm_manager,
                schedule_modified=True,
                schedule_backup=vm_manager._schedule_backup,
                schedule_path=vm_manager._schedule_path,
                original_schedule=vm_manager._original_schedule,
                started_at=datetime.now(),
                schedule_was_modified_at=datetime.now(),
            )
            result.vm_session = session

        timeout = int(
            vm_settings.get("transcribe_vm_start_timeout", 600)
        )
        interval = int(
            vm_settings.get("transcribe_vm_check_interval", 10)
        )

        def _wait_progress(elapsed: float, max_wait: float) -> None:
            if progress_cb:
                progress_cb(
                    f"Ожидание запуска ВМ «{vm_name}»… "
                    f"({int(elapsed)} / {int(max_wait)} с)"
                )

        ok = await wait_for_transcribe_service(
            transcribe_url,
            max_wait=float(timeout),
            check_interval=float(interval),
            progress_cb=_wait_progress,
            cancel_event=cancel_event,
        )

        if not ok:
            result.error = (
                f"ВМ «{vm_name}» не запустилась за {timeout} с"
            )
            result.skipped_reason = (
                f"{result.error}. Транскрибация будет пропущена."
            )
            log.error(result.error)
            # Восстанавливаем расписание, если меняли
            if session is not None and session.schedule_modified and vm_manager:
                vm_manager.restore_schedule()
            return result

        result.can_transcribe = True
        log.info(
            "TranscribeVMController: ВМ запущена, можно продолжать"
        )
        return result

    # ------------------------------------------------------------------
    # Завершение
    # ------------------------------------------------------------------
    async def finish_session(
        self,
        vm_session: Optional[TranscribeVMSession],
        *,
        on_ask_user: Optional[Callable[[Dict[str, Any]], bool]] = None,
    ) -> Dict[str, Any]:
        """
        Завершает сессию: спрашивает пользователя, отключить ли ВМ,
        и восстанавливает расписание при согласии.

        Args:
            vm_session:  сессия из prepare_for_transcribe.
            on_ask_user: колбэк, принимающий словарь с информацией
                         о ВМ и возвращающий True, если пользователь
                         согласен отключить ВМ (восстановить
                         расписание).

        Returns:
            {
              "restored": bool,
              "message": str,
              "next_stop_time": str,
            }
        """
        result = {
            "restored": False,
            "message": "",
            "next_stop_time": "",
        }

        if vm_session is None or vm_session.vm_manager is None:
            return result

        vm_manager = vm_session.vm_manager
        vm_name = vm_manager.vm_name

        # Получаем информацию о текущем (изменённом) расписании
        info = vm_manager.get_modified_schedule_info()
        next_stop_time = info.get("next_stop_time", "")
        result["next_stop_time"] = next_stop_time

        # Если расписание не менялось — нечего восстанавливать
        if not session_schedule_modified(vm_session):
            result["message"] = (
                f"Расписание ВМ «{vm_name}» не менялось"
            )
            return result

        # Спрашиваем пользователя
        ask_data = {
            "vm_name": vm_name,
            "next_stop_time": next_stop_time,
            "schedule_path": vm_session.schedule_path,
        }

        user_wants_stop = True
        if on_ask_user is not None:
            try:
                user_wants_stop = bool(on_ask_user(ask_data))
            except Exception as exc:
                log.exception(
                    "Ошибка в on_ask_user: %s", exc
                )
                user_wants_stop = False

        if user_wants_stop:
            restore = vm_manager.restore_schedule()
            result["restored"] = restore["ok"]
            result["message"] = restore["message"]
            log.info(
                "TranscribeVMController: расписание восстановлено "
                "(пользователь согласился отключить ВМ)"
            )
        else:
            if next_stop_time:
                result["message"] = (
                    f"ВМ «{vm_name}» продолжит работу до "
                    f"{next_stop_time}"
                )
            else:
                result["message"] = (
                    f"ВМ «{vm_name}» продолжит работу по "
                    f"изменённому расписанию"
                )
            log.info(
                "TranscribeVMController: расписание НЕ восстановлено "
                "(пользователь отказался)"
            )

        return result


def session_schedule_modified(session: TranscribeVMSession) -> bool:
    """Проверяет, было ли изменено расписание в сессии."""
    if session is None:
        return False
    if not session.schedule_modified:
        return False
    return bool(session.schedule_backup)