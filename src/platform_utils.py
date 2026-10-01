"""Кроссплатформенные утилиты.

ВАЖНО: этот модуль НЕ должен импортировать .logger или другие
внутренние модули с побочными эффектами — иначе получится
циклический импорт (logger → platform_utils → logger).

Содержит:
  • определение ОС;
  • проверки доступности функций (запись экрана);
  • сообщения о недоступности функций;
  • кроссплатформенные пути (temp, config, log).
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
# Доступность функций
# ---------------------------------------------------------------------------
def is_screen_recording_available() -> bool:
    """
    Доступна ли запись экрана на текущей платформе.

    На Linux — да (x11grab / kmsgrab).
    На Windows — нет (нужен отдельный порт recorder.py).
    На macOS — нет (не поддерживается).
    """
    return IS_LINUX


def screen_recording_unavailable_reason() -> str:
    """
    Возвращает текст причины, почему запись экрана недоступна.

    Пустая строка, если запись доступна.
    """
    if IS_LINUX:
        return ""
    if IS_WINDOWS:
        return (
            "Запись экрана на Windows не поддерживается в текущей "
            "версии приложения.\n\n"
            "Это связано с тем, что запись реализована через ffmpeg "
            "с источниками x11grab / kmsgrab, которые доступны только "
            "на Linux.\n\n"
            "Что можно делать на Windows:\n"
            "  • транскрибировать готовые видео и аудио "
            "(«Загрузить видео» / «Импорт»);\n"
            "  • работать с готовыми стенограммами и протоколами;\n"
            "  • синхронизировать записи с сервером;\n"
            "  • отправлять протоколы и summary в Bitrix24;\n"
            "  • искать по сохранённым записям (Библиотека).\n\n"
            "Запись экрана появится в следующих версиях "
            "после порта recorder.py на Windows (ddagrab/gdigrab)."
        )
    return (
        f"Запись экрана не поддерживается на платформе «{OS_NAME}»."
    )


def is_hotkey_recording_available() -> bool:
    """Доступны ли глобальные хоткеи старта/остановки записи."""
    return IS_LINUX


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