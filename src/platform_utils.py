"""Кроссплатформенные утилиты.

ВАЖНО: этот модуль НЕ должен импортировать .logger или другие
внутренние модули с побочными эффектами — иначе получится
циклический импорт (logger → platform_utils → logger).

Содержит:
  • определение ОС;
  • кроссплатформенные пути (temp, config, log);
  • имена бинарников ffmpeg/ffprobe.

Изменения:
  • УДАЛЕНО: функции is_screen_recording_available(),
    screen_recording_unavailable_reason(),
    is_hotkey_recording_available() — механизм записи экрана
    удалён из приложения.
"""
from __future__ import annotations

import os
import sys
import tempfile


# ---------------------------------------------------------------------------
# Определение ОС
# ---------------------------------------------------------------------------
IS_WINDOWS: bool = sys.platform.startswith("win")
IS_LINUX: bool = sys.platform.startswith("linux")
IS_MACOS: bool = sys.platform == "darwin"

# Человекочитаемое имя ОС для сообщений.
OS_NAME: str = (
    "Windows" if IS_WINDOWS
    else "Linux" if IS_LINUX
    else "macOS" if IS_MACOS
    else sys.platform
)


# ---------------------------------------------------------------------------
# Пути
# ---------------------------------------------------------------------------
def default_temp_dir() -> str:
    """
    Кроссплатформенная временная папка приложения.

    Linux:   /tmp/screen-recorder
    Windows: C:\\Users\\<user>\\AppData\\Local\\Temp\\screen-recorder
    macOS:   /var/folders/.../T/screen-recorder
    """
    base = tempfile.gettempdir()
    return os.path.join(base, "screen-recorder")


def default_log_path() -> str:
    """Кроссплатформенный путь к файлу лога."""
    return os.path.join(default_temp_dir(), "app.log")


def default_config_dir() -> str:
    """
    Кроссплатформенная папка конфига.

    Linux/macOS: ~/.config/screen-recorder
    Windows:     %APPDATA%\\screen-recorder
    """
    if IS_WINDOWS:
        appdata = os.environ.get("APPDATA")
        if appdata:
            return os.path.join(appdata, "screen-recorder")
        return os.path.join(
            os.path.expanduser("~"), ".config", "screen-recorder"
        )
    return os.path.join(
        os.path.expanduser("~"), ".config", "screen-recorder"
    )


def default_config_path() -> str:
    """Кроссплатформенный путь к config.json."""
    return os.path.join(default_config_dir(), "config.json")


# ---------------------------------------------------------------------------
# FFmpeg
# ---------------------------------------------------------------------------
def ffmpeg_binary_name() -> str:
    """Имя исполняемого файла ffmpeg для текущей ОС."""
    return "ffmpeg.exe" if IS_WINDOWS else "ffmpeg"


def ffprobe_binary_name() -> str:
    """Имя исполняемого файла ffprobe для текущей ОС."""
    return "ffprobe.exe" if IS_WINDOWS else "ffprobe"