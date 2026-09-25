"""Глобальные горячие клавиши через pynput."""
from __future__ import annotations

import asyncio
from typing import Callable, Dict, FrozenSet, Optional

from pynput import keyboard

from .config_manager import ConfigManager
from .logger import get_logger
from .utils import get_system_monitors

log = get_logger(__name__)

_MODIFIER_ALIASES = {
    "ctrl": keyboard.Key.ctrl,
    "control": keyboard.Key.ctrl,
    "shift": keyboard.Key.shift,
    "alt": keyboard.Key.alt,
    "cmd": keyboard.Key.cmd,
    "super": keyboard.Key.cmd,
    "meta": keyboard.Key.cmd,
    "win": keyboard.Key.cmd,
}


def _parse_hotkey(combo: str) -> FrozenSet:
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    keys = set()
    for p in parts:
        if p in _MODIFIER_ALIASES:
            keys.add(_MODIFIER_ALIASES[p])
        else:
            keys.add(p)
    log.debug("Горячая клавиша '%s' → %s", combo, keys)
    return frozenset(keys)


def _normalize_key(key) -> object:
    if hasattr(key, "char") and key.char:
        return key.char.lower()
    if key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
        return keyboard.Key.ctrl
    if key in (keyboard.Key.shift_l, keyboard.Key.shift_r):
        return keyboard.Key.shift
    if key in (keyboard.Key.alt_l, keyboard.Key.alt_r, keyboard.Key.alt_gr):
        return keyboard.Key.alt
    if key in (keyboard.Key.cmd_l, keyboard.Key.cmd_r):
        return keyboard.Key.cmd
    return key


class GlobalHotkeyManager:
    """Менеджер глобальных горячих клавиш."""

    def __init__(
        self,
        recorder,
        config: ConfigManager,
        start_cb: Optional[Callable[[], None]] = None,
        stop_cb: Optional[Callable[[], None]] = None,
    ) -> None:
        """
        Args:
            recorder: ScreenRecorder — для проверки статуса записи.
            config: ConfigManager — для чтения настроек хоткеев.
            start_cb: колбэк старт/пауза/возобновление. Если задан —
                используется вместо прямой работы с recorder.
            stop_cb: колбэк остановки. Если задан — используется вместо
                прямой работы с recorder.
        """
        self.recorder = recorder
        self.config = config
        self._listener: Optional[keyboard.Listener] = None
        self._pressed: set = set()
        self._hotkeys: Dict[FrozenSet, Callable[[], None]] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Колбэки для делегирования в GUI-поток (main.py)
        self._start_cb = start_cb
        self._stop_cb = stop_cb

        log.info("GlobalHotkeyManager инициализирован "
                 "(callbacks: start=%s, stop=%s)",
                 start_cb is not None, stop_cb is not None)

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        log.debug("Установлен asyncio-loop для горячих клавиш")

    def register_hotkeys(self) -> None:
        rec = self.config.config.get("recording", {})
        self._hotkeys = {
            _parse_hotkey(rec.get("hotkey_start", "Ctrl+Shift+R")): self.on_start_stop,
            _parse_hotkey(rec.get("hotkey_stop", "Ctrl+Shift+S")): self.on_stop,
        }
        self._listener = keyboard.Listener(
            on_press=self._on_press, on_release=self._on_release
        )
        self._listener.daemon = True
        self._listener.start()
        log.info("Горячие клавиши зарегистрированы: start=%s, stop=%s",
                 rec.get("hotkey_start"), rec.get("hotkey_stop"))

    def unregister_hotkeys(self) -> None:
        if self._listener:
            try:
                self._listener.stop()
                log.info("Слушатель горячих клавиш остановлен")
            except Exception as exc:
                log.warning("Ошибка остановки слушателя: %s", exc)
            self._listener = None
        self._pressed.clear()

    def update_hotkeys(self, new_config: dict) -> None:
        log.info("Обновление горячих клавиш")
        self.unregister_hotkeys()
        self.register_hotkeys()

    # ------------------------------------------------------------------
    # Действия
    # ------------------------------------------------------------------
    def on_start_stop(self) -> None:
        """
        Ctrl+Shift+R:
          • если идёт запись и не на паузе — пауза,
          • если на паузе — возобновление,
          • если записи нет — старт (через колбэк, чтобы учесть настройки).
        """
        status = self.recorder.get_recording_status()

        if status["recording"]:
            # Пауза/возобновление — работаем напрямую с recorder, это не
            # связано с настройками отображения.
            loop = self._loop
            if loop is None:
                log.warning("Hotkey start/stop: loop не установлен")
                return
            if status["paused"]:
                log.info("Hotkey: возобновление записи")
                asyncio.run_coroutine_threadsafe(
                    self.recorder.resume_recording(), loop
                )
            else:
                log.info("Hotkey: пауза записи")
                asyncio.run_coroutine_threadsafe(
                    self.recorder.pause_recording(), loop
                )
            return

        # Запись не идёт — старт.
        if self._start_cb is not None:
            log.info("Hotkey: старт записи через callback (учитывает настройки)")
            try:
                self._start_cb()
            except Exception as exc:
                log.exception("Ошибка в start_cb: %s", exc)
            return

        # Fallback (обратная совместимость): прямая работа с recorder
        log.info("Hotkey: старт записи напрямую (без callback)")
        self._fallback_start()

    def on_stop(self) -> None:
        """Ctrl+Shift+S — остановка записи."""
        status = self.recorder.get_recording_status()
        if not status["recording"]:
            log.debug("Hotkey stop: запись не идёт")
            return

        if self._stop_cb is not None:
            log.info("Hotkey: остановка через callback (учитывает настройки)")
            try:
                self._stop_cb()
            except Exception as exc:
                log.exception("Ошибка в stop_cb: %s", exc)
            return

        # Fallback (обратная совместимость): прямая работа с recorder
        log.info("Hotkey: остановка напрямую (без callback)")
        loop = self._loop
        if loop is None:
            log.warning("Hotkey stop: loop не установлен")
            return
        asyncio.run_coroutine_threadsafe(self.recorder.stop_recording(), loop)

    # ------------------------------------------------------------------
    # Внутреннее
    # ------------------------------------------------------------------
    def _fallback_start(self) -> None:
        loop = self._loop
        if loop is None:
            log.warning("Hotkey start: loop не установлен")
            return
        rec = self.config.config.get("recording", {})
        monitors = get_system_monitors()
        idx = int(rec.get("monitor", 0))
        display = monitors[idx]["display"] if 0 <= idx < len(monitors) else ":0.0+0,0"
        with_mic = rec.get("with_microphone", True)
        asyncio.run_coroutine_threadsafe(
            self.recorder.start_recording(display, with_mic), loop
        )

    def _on_press(self, key) -> None:
        norm = _normalize_key(key)
        self._pressed.add(norm)
        pressed = frozenset(self._pressed)
        for combo, cb in self._hotkeys.items():
            if combo and combo.issubset(pressed):
                try:
                    cb()
                except Exception as exc:
                    log.exception("Ошибка обработки хоткея: %s", exc)

    def _on_release(self, key) -> None:
        norm = _normalize_key(key)
        self._pressed.discard(norm)