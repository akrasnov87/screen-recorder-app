"""Вспомогательные функции приложения.

Изменения:
  • Удалены неиспользуемые функции: get_system_info(),
    format_file_size(), get_free_space(), validate_path(),
    parse_date_from_filename().
  • Добавлена safe_local_path() — нормализует путь от QFileDialog
    (декодирует percent-encoding, убирает \0).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import uuid
from datetime import datetime
from typing import Dict, List
from urllib.parse import unquote

from .logger import get_logger
from .platform_utils import (
    IS_LINUX,
    IS_WINDOWS,
    ffmpeg_binary_name,
    ffprobe_binary_name,
    is_screen_recording_available,
    screen_recording_unavailable_reason,
)

log = get_logger(__name__)


def check_ffmpeg_installed() -> bool:
    """Проверяет наличие ffmpeg в PATH (кроссплатформенно)."""
    binary = ffmpeg_binary_name()
    path = shutil.which(binary)
    found = path is not None
    log.debug(
        "Проверка %s: %s (%s)",
        binary, found, path or "не найден",
    )
    return found


def get_system_monitors() -> List[Dict[str, str]]:
    """
    Возвращает список мониторов.

    Кроссплатформенно через Qt (QGuiApplication.screens()).
    На Linux формат `display` совместим с x11grab
    (":0.0+X,Y"); на Windows `display` — имя монитора.

    Если Qt ещё не инициализирован — возвращает fallback
    1920x1080 (для ранних вызовов из config_manager).
    """
    from PySide6.QtGui import QGuiApplication

    monitors: List[Dict[str, str]] = []

    app = QGuiApplication.instance()
    if app is None:
        log.warning(
            "QGuiApplication не инициализирован — "
            "используется fallback-монитор 1920x1080"
        )
        return [{
            "index": "0",
            "name": "default",
            "width": "1920",
            "height": "1080",
            "x": "0",
            "y": "0",
            "display": ":0.0+0,0" if IS_LINUX else "default",
        }]

    try:
        screens = app.screens()
    except Exception as exc:
        log.warning(
            "Не удалось получить список экранов: %s", exc
        )
        screens = []

    for i, screen in enumerate(screens):
        try:
            geo = screen.geometry()
        except Exception:
            continue

        name = screen.name() or f"screen{i}"

        # На Linux оставляем формат x11grab для совместимости
        # с recorder.py. На Windows/macOS — просто имя экрана.
        if IS_LINUX:
            display = f":0.0+{geo.x()},{geo.y()}"
        else:
            display = name

        monitors.append({
            "index": str(i),
            "name": name,
            "width": str(geo.width()),
            "height": str(geo.height()),
            "x": str(geo.x()),
            "y": str(geo.y()),
            "display": display,
        })
        log.debug(
            "Найден монитор: %s %sx%s @ (%s,%s) → %s",
            name, geo.width(), geo.height(),
            geo.x(), geo.y(), display,
        )

    if not monitors:
        log.warning(
            "Список мониторов пуст, fallback 1920x1080"
        )
        monitors.append({
            "index": "0",
            "name": "default",
            "width": "1920",
            "height": "1080",
            "x": "0",
            "y": "0",
            "display": ":0.0+0,0" if IS_LINUX else "default",
        })

    log.info("Итого мониторов: %d", len(monitors))
    return monitors


def generate_task_id() -> str:
    """Генерирует уникальный ID задачи."""
    task_id = (
        f"{datetime.now():%Y-%m-%d_%H-%M-%S}_{uuid.uuid4().hex[:6]}"
    )
    log.debug("Сгенерирован task_id: %s", task_id)
    return task_id


def find_drm_card() -> str | None:
    """
    Возвращает путь к первому доступному /dev/dri/cardN.

    Только для Linux (kmsgrab). На других ОС — None.
    """
    if not IS_LINUX:
        log.debug(
            "find_drm_card: не Linux (%s) — возвращаем None",
            "Windows" if IS_WINDOWS else "другая ОС",
        )
        return None

    import glob
    cards = sorted(glob.glob("/dev/dri/card[0-9]*"))
    if not cards:
        log.warning("Не найдено ни одного /dev/dri/cardN")
        return None
    for c in cards:
        if os.access(c, os.R_OK):
            log.debug("Найден доступный DRM-узел: %s", c)
            return c
    log.warning(
        "DRM-узлы найдены, но ни один не доступен: %s", cards
    )
    return cards[0]


# ---------------------------------------------------------------------------
# Нормализация путей от QFileDialog
# ---------------------------------------------------------------------------
def safe_local_path(path: str) -> str:
    """
    Нормализует путь, полученный от QFileDialog.

    На некоторых платформах (особенно под Wayland/GTK) Qt возвращает
    путь с percent-encoding: например, вместо
    "/home/user/КСУО_Мобил.xlsx" приходит
    "/home/user/%D0%9A%D0%A1%D0%A3%D0%9E_%D0%9C%D0%BE%D0%B1%D0%B8%D0%BB.xlsx".

    Функция декодирует percent-encoding (до 2 раз — на случай
    двойного кодирования) и убирает нулевые байты.

    Args:
        path: путь от QFileDialog.

    Returns:
        Нормализованный путь. Если path пустой — возвращается как есть.
    """
    if not path:
        return path

    decoded = path
    for _ in range(2):
        try:
            new_val = unquote(decoded, errors="replace")
        except Exception:
            break
        if new_val == decoded:
            break
        decoded = new_val

    # Убираем нулевые байты — защита от «битых» имён.
    decoded = decoded.replace("\x00", "")

    if decoded != path:
        log.debug(
            "safe_local_path: %r → %r", path, decoded
        )
    return decoded


# ---------------------------------------------------------------------------
# Санитизация имён файлов
# ---------------------------------------------------------------------------

# Символы, недопустимые в именах файлов на большинстве ФС.
_FILENAME_BAD_CHARS = '<>:"/\\|?*\n\r\t'

# Значение по умолчанию для лимита имени вложения (символы).
# Должно совпадать с config["app"]["attachment_name_max_chars"].
DEFAULT_ATTACHMENT_NAME_MAX_CHARS = 50


def sanitize_filename(
    name: str,
    max_chars: int = DEFAULT_ATTACHMENT_NAME_MAX_CHARS,
) -> str:
    """
    Приводит имя файла к безопасному виду:
      • убирает путь (берёт только basename);
      • декодирует URL-encoding (%D0%9A → К), если он есть;
      • удаляет недопустимые символы;
      • сохраняет кириллицу, точки и дефисы;
      • обрезает имя по СИМВОЛАМ, сохраняя расширение.

    Кириллица в UTF-8 занимает 2 байта на символ, поэтому
    лимит в 50 символов даёт ~100 байт — с большим запасом
    до лимита файловой системы (255 байт).

    Args:
        name:      исходное имя (может содержать путь).
        max_chars: максимум символов в basename (без учёта
                   расширения оно тоже входит в лимит).

    Returns:
        Безопасное имя файла. Если ничего не осталось — "file".
    """
    if not name:
        return "file"

    base = os.path.basename(name).strip()
    if not base:
        return "file"

    # --- Декодируем URL-encoding (до 3 раз — бывает двойное) ---
    decoded = base
    for _ in range(3):
        try:
            new_val = unquote(decoded, errors="strict")
        except Exception:
            break
        if new_val == decoded:
            break
        decoded = new_val
    base = decoded

    # --- Удаляем недопустимые символы ---
    cleaned = "".join(
        ("_" if ch in _FILENAME_BAD_CHARS else ch)
        for ch in base
    )

    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")

    cleaned = cleaned.strip(" .")
    if not cleaned:
        return "file"

    # --- Обрезка по СИМВОЛАМ с сохранением расширения ---
    limit = max(5, int(max_chars))

    if len(cleaned) <= limit:
        return cleaned

    stem, ext = os.path.splitext(cleaned)

    # Если расширение слишком длинное — игнорируем его.
    if len(ext) > 10:
        stem = cleaned
        ext = ""

    # Резервируем место под расширение.
    stem_limit = max(1, limit - len(ext))
    if len(stem) > stem_limit:
        stem = stem[:stem_limit]

    result = (stem + ext).strip(" .")
    if not result:
        result = "file"

    log.debug(
        "sanitize_filename: %r → %r (%d → %d символов)",
        name, result, len(cleaned), len(result),
    )
    return result

# ---------------------------------------------------------------------------
# Доступность записи экрана
# ---------------------------------------------------------------------------
def is_recording_supported() -> bool:
    """Доступна ли запись экрана на текущей платформе."""
    return is_screen_recording_available()


def recording_unavailable_message() -> str:
    """Возвращает сообщение о недоступности записи."""
    return screen_recording_unavailable_reason()