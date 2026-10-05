"""Запись экрана и аудио через ffmpeg.

Изменения:
  • Таймауты и задержки ffmpeg читаются из config["app"]:
      ffmpeg_start_check_delay, ffmpeg_stop_timeout, ffmpeg_kill_timeout.
  • Удалён неиспользуемый метод get_monitors().
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from datetime import datetime
from typing import Dict, List, Optional

from PySide6.QtCore import QObject, Signal

from .logger import get_logger
from .platform_utils import (
    IS_LINUX,
    IS_WINDOWS,
    ffmpeg_binary_name,
    is_screen_recording_available,
    screen_recording_unavailable_reason,
)
from .utils import find_drm_card, get_system_monitors

log = get_logger(__name__)


class ScreenRecorder(QObject):
    """Управляет записью экрана через ffmpeg."""

    recording_started = Signal()
    recording_paused = Signal()
    recording_resumed = Signal()
    recording_stopped = Signal(str)
    recording_error = Signal(str)

    def __init__(self, config: Dict,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.config = config or {}
        self._process: Optional[subprocess.Popen] = None
        self._output_path: Optional[str] = None
        self._paused = False
        self._overlay_text = ""
        self._start_time: Optional[datetime] = None
        self._ffmpeg_log = None
        self._ffmpeg_log_path: Optional[str] = None

        app_cfg = self.config.get("app", {}) or {}
        self._start_check_delay = float(
            app_cfg.get("ffmpeg_start_check_delay", 0.3)
        )
        self._stop_timeout = float(
            app_cfg.get("ffmpeg_stop_timeout", 10)
        )
        self._kill_timeout = float(
            app_cfg.get("ffmpeg_kill_timeout", 5)
        )

        log.info(
            "ScreenRecorder инициализирован "
            "(start_delay=%.2fс, stop_timeout=%.0fс, kill_timeout=%.0fс)",
            self._start_check_delay, self._stop_timeout, self._kill_timeout,
        )

    def _build_output_path(self) -> str:
        temp = self.config.get("storage", {}).get(
            "temp_path", "/tmp/screen-recorder"
        )
        sessions = os.path.join(
            temp, "sessions", datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        )
        os.makedirs(sessions, exist_ok=True)
        path = os.path.join(sessions, "video.mp4")
        log.debug("Путь вывода: %s", path)
        return path

    def _build_ffmpeg_cmd(
        self, monitor: str, with_microphone: bool,
    ) -> List[str]:
        rec = self.config.get("recording", {})
        comp = self.config.get("compression", {})
        video_bitrate = comp.get("video_bitrate", 4000)
        audio_bitrate = comp.get("audio_bitrate", 192)

        session = os.environ.get("XDG_SESSION_TYPE", "x11").lower()
        log.info("Тип графической сессии: %s", session)

        if session == "wayland":
            card = find_drm_card()
            if not card:
                raise RuntimeError(
                    "Не найдено DRM-устройство /dev/dri/cardN"
                )

            crop_expr = ""
            for m in get_system_monitors():
                if m["display"] == monitor or m["name"] == monitor:
                    crop_expr = (
                        f"crop={m['width']}:{m['height']}:"
                        f"{m['x']}:{m['y']}"
                    )
                    log.info("kmsgrab: обрезка под %s", m["name"])
                    break

            if crop_expr:
                vf = (
                    f"{crop_expr},hwmap=derive_device=vaapi,"
                    f"scale_vaapi=format=nv12"
                )
            else:
                vf = "hwmap=derive_device=vaapi,scale_vaapi=format=nv12"

            cmd = [
                ffmpeg_binary_name(), "-y",
                "-f", "kmsgrab", "-device", card, "-i", "-",
            ]
            if with_microphone:
                cmd += ["-f", "pulse", "-i", "default"]

            cmd += ["-vf", vf]

            cmd += [
                "-c:v", "h264_vaapi",
                "-b:v", f"{video_bitrate}k",
            ]
            if with_microphone:
                cmd += ["-c:a", "aac", "-b:a", f"{audio_bitrate}k"]

            log.info("kmsgrab: card=%s, vf=%s", card, vf)

        else:
            mons = get_system_monitors()
            size = "1920x1080"
            for m in mons:
                if m["display"] == monitor or m["name"] == monitor:
                    size = f"{m['width']}x{m['height']}"
                    break

            cmd = [
                ffmpeg_binary_name(), "-y",
                "-f", "x11grab",
                "-video_size", size,
                "-framerate", "30",
                "-i", monitor,
            ]
            if with_microphone:
                cmd += ["-f", "pulse", "-i", "default"]

            if rec.get("show_watermark") and self._overlay_text:
                safe = (self._overlay_text
                        .replace(":", "\\:").replace("'", ""))
                drawtext = (
                    f"drawtext=text='{safe}':x=20:y=20:fontsize=24:"
                    f"fontcolor=white@0.8:box=1:boxcolor=black@0.5"
                )
                cmd += ["-vf", drawtext]

            cmd += [
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-pix_fmt", "yuv420p",
                "-b:v", f"{video_bitrate}k",
            ]
            if with_microphone:
                cmd += ["-c:a", "aac", "-b:a", f"{audio_bitrate}k"]

        cmd.append(self._output_path)
        log.debug("Полная команда ffmpeg: %s", " ".join(cmd))
        return cmd

    async def start_recording(
        self, monitor: str, with_microphone: bool,
    ) -> bool:
        # --- Проверка доступности записи на текущей платформе ---
        if not is_screen_recording_available():
            reason = screen_recording_unavailable_reason()
            log.warning(
                "Запись экрана недоступна на этой платформе: %s",
                reason,
            )
            self.recording_error.emit(reason)
            return False

        if self._process is not None:
            log.warning("Попытка начать запись, но запись уже идёт")
            return False
        try:
            log.info("Запуск записи: monitor=%s, mic=%s",
                     monitor, with_microphone)
            self._output_path = self._build_output_path()
            cmd = self._build_ffmpeg_cmd(monitor, with_microphone)

            self._ffmpeg_log_path = self._output_path.replace(
                ".mp4", ".ffmpeg.log"
            )
            self._ffmpeg_log = open(self._ffmpeg_log_path, "w")
            log.info("Лог ffmpeg: %s", self._ffmpeg_log_path)

            self._process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=self._ffmpeg_log,
                preexec_fn=os.setsid,
            )

            # Пауза из конфига (ffmpeg_start_check_delay)
            await asyncio.sleep(self._start_check_delay)
            if self._process.poll() is not None:
                rc = self._process.returncode
                log.error(
                    "ffmpeg упал сразу после старта (код %d). См. %s",
                    rc, self._ffmpeg_log_path,
                )
                try:
                    self._ffmpeg_log.close()
                    self._ffmpeg_log = None
                    with open(self._ffmpeg_log_path, "r",
                              encoding="utf-8") as f:
                        for line in f.readlines()[-20:]:
                            log.error("ffmpeg: %s", line.rstrip())
                except Exception:
                    pass
                self._process = None
                self._output_path = None
                self.recording_error.emit(f"ffmpeg exit code {rc}")
                return False

            self._paused = False
            self._start_time = datetime.now()
            log.info("Запись запущена (PID=%d)", self._process.pid)
            self.recording_started.emit()
            return True
        except Exception as exc:
            log.exception("Не удалось запустить запись: %s", exc)
            self.recording_error.emit(str(exc))
            self._process = None
            return False

    async def pause_recording(self) -> bool:
        if self._process is None or self._paused:
            log.debug("Пауза невозможна: процесс=%s, paused=%s",
                      self._process is not None, self._paused)
            return False
        try:
            log.info("Пауза записи (PID=%d)", self._process.pid)
            os.killpg(os.getpgid(self._process.pid), signal.SIGSTOP)
            self._paused = True
            self.recording_paused.emit()
            return True
        except Exception as exc:
            log.exception("Ошибка паузы: %s", exc)
            self.recording_error.emit(str(exc))
            return False

    async def resume_recording(self) -> bool:
        if self._process is None or not self._paused:
            log.debug("Возобновление невозможно: процесс=%s, paused=%s",
                      self._process is not None, self._paused)
            return False
        try:
            log.info("Возобновление записи (PID=%d)", self._process.pid)
            os.killpg(os.getpgid(self._process.pid), signal.SIGCONT)
            self._paused = False
            self.recording_resumed.emit()
            return True
        except Exception as exc:
            log.exception("Ошибка возобновления: %s", exc)
            self.recording_error.emit(str(exc))
            return False

    async def stop_recording(self) -> str:
        if self._process is None:
            log.debug("Остановка записи: процесс не запущен")
            return ""
        path = self._output_path or ""
        try:
            log.info("Остановка записи (PID=%d, файл=%s)",
                     self._process.pid, path)
            if self._paused:
                os.killpg(os.getpgid(self._process.pid), signal.SIGCONT)
                self._paused = False
            self._process.send_signal(signal.SIGINT)
            try:
                self._process.wait(timeout=self._stop_timeout)
                log.info("ffmpeg корректно завершён")
            except subprocess.TimeoutExpired:
                log.warning(
                    "ffmpeg не завершился за %.0fс, SIGKILL",
                    self._stop_timeout,
                )
                os.killpg(os.getpgid(self._process.pid), signal.SIGKILL)
                self._process.wait(timeout=self._kill_timeout)
        except Exception as exc:
            log.exception("Ошибка остановки записи: %s", exc)
            self.recording_error.emit(str(exc))
        finally:
            if getattr(self, "_ffmpeg_log", None):
                try:
                    self._ffmpeg_log.close()
                except Exception:
                    pass
                self._ffmpeg_log = None
            self._ffmpeg_log_path = None
            self._process = None
            self._output_path = None
            self._start_time = None
            if path and os.path.exists(path):
                size = os.path.getsize(path)
                log.info("Файл записи: %s (%.2f МБ)",
                         path, size / 1024 / 1024)
            else:
                log.warning("Файл записи не создан: %s", path)
            self.recording_stopped.emit(path)
        return path

    def set_overlay_text(self, text: str) -> None:
        self._overlay_text = text
        log.debug("Текст водяного знака: %s", text)

    def get_recording_status(self) -> Dict:
        return {
            "recording": self._process is not None,
            "paused": self._paused,
            "start_time": (self._start_time.isoformat()
                           if self._start_time else None),
            "output": self._output_path,
        }