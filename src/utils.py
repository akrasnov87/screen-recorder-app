"""Вспомогательные функции приложения.

Изменения:
  • Удалены неиспользуемые функции: get_system_info(),
    format_file_size(), get_free_space(), validate_path(),
    parse_date_from_filename().
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import uuid
from datetime import datetime
from typing import Dict, List

from .logger import get_logger
from urllib.parse import unquote

log = get_logger(__name__)


def check_ffmpeg_installed() -> bool:
    """Проверяет наличие ffmpeg в PATH."""
    path = shutil.which("ffmpeg")
    found = path is not None
    log.debug("Проверка ffmpeg: %s (%s)", found, path or "не найден")
    return found


def get_system_monitors() -> List[Dict[str, str]]:
    """Возвращает список мониторов через xrandr (X11)."""
    log.debug("Запрос списка мониторов через xrandr --listmonitors")
    monitors: List[Dict[str, str]] = []
    try:
        out = subprocess.check_output(
            ["xrandr", "--listmonitors"], text=True
        )
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 4:
                index = parts[0].rstrip(":")
                name = parts[-1]
                geometry = parts[2]
                m = re.match(
                    r"(\d+)/\d+x(\d+)/\d+\+(\d+)\+(\d+)", geometry
                )
                if m:
                    w, h, x, y = m.groups()
                    monitors.append({
                        "index": index,
                        "name": name,
                        "width": w,
                        "height": h,
                        "x": x,
                        "y": y,
                        "display": f":0.0+{x},{y}",
                    })
                    log.debug("Найден монитор: %s %sx%s @ (%s,%s) → %s",
                              name, w, h, x, y, f":0.0+{x},{y}")
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        log.warning(
            "xrandr недоступен (%s), используется монитор по умолчанию",
            exc,
        )

    if not monitors:
        log.warning("Список мониторов пуст, добавляем fallback 1920x1080")
        monitors.append({
            "index": "0",
            "name": "default",
            "width": "1920",
            "height": "1080",
            "x": "0",
            "y": "0",
            "display": ":0.0+0,0",
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

    kmsgrab по умолчанию ищет card0, но в некоторых системах
    основной DRM-узел может быть card1 (например, при наличии
    нескольких GPU или виртуального дисплея).
    """
    import glob
    cards = sorted(glob.glob("/dev/dri/card[0-9]*"))
    if not cards:
        log.warning("Не найдено ни одного /dev/dri/cardN")
        return None
    for c in cards:
        if os.access(c, os.R_OK):
            log.debug("Найден доступный DRM-узел: %s", c)
            return c
    log.warning("DRM-узлы найдены, но ни один не доступен: %s", cards)
    return cards[0]
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