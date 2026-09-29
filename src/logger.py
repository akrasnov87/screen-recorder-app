"""Система логирования.

Изменения:
  • setup_logger() принимает max_bytes и backup_count — значения
    ротации теперь берутся из config["logging"].
  • Удалены неиспользуемые функции: log_to_gui(), rotate_logs(),
    unregister_gui_handler().
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from typing import Callable, List, Optional

# ---------------------------------------------------------------------------
# Глобальное состояние
# ---------------------------------------------------------------------------

_gui_handlers: List[Callable[[str, str], None]] = []
_root_logger: Optional[logging.Logger] = None
_DEFAULT_LOG_PATH = "/tmp/screen-recorder/app.log"
_active_log_path: str = _DEFAULT_LOG_PATH


def get_current_log_path() -> str:
    """Путь к файлу лога, куда пишет логгер."""
    return _active_log_path


def set_log_path(log_path: str) -> None:
    """
    Меняет путь лога. Использовать ДО setup_logger().

    Args:
        log_path: путь к файлу лога.
    """
    global _active_log_path
    _active_log_path = log_path


# ---------------------------------------------------------------------------
# Публичное API
# ---------------------------------------------------------------------------

def setup_logger(
    name: str = "src",
    log_path: Optional[str] = None,
    level: str = "DEBUG",
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
) -> logging.Logger:
    """
    Настраивает корневой логгер пакета.

    Args:
        name:         имя корневого логгера (обычно "src").
        log_path:     путь к файлу логов. Если None — берётся из
                      set_log_path() или дефолтный.
        level:        уровень логирования (DEBUG/INFO/WARNING/ERROR/CRITICAL).
        max_bytes:    размер файла лога перед ротацией.
        backup_count: сколько старых файлов хранить.

    Returns:
        Настроенный корневой логгер.
    """
    global _root_logger, _active_log_path

    if log_path is None:
        log_path = _active_log_path
    _active_log_path = log_path

    if _root_logger is not None:
        return _root_logger

    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.DEBUG))
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # --- Файл с ротацией ---
    fh = RotatingFileHandler(
        log_path,
        maxBytes=int(max_bytes),
        backupCount=int(backup_count),
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # --- Консоль ---
    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(logging.DEBUG)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # --- GUI-хук ---
    gui_handler = _GuiLogHandler()
    gui_handler.setLevel(logging.DEBUG)
    logger.addHandler(gui_handler)

    _root_logger = logger
    logger.debug(
        "Логгер инициализирован: %s (level=%s, max=%d MB, backups=%d)",
        log_path, level, max_bytes // (1024 * 1024), backup_count,
    )
    return logger


def get_logger(name: str) -> logging.Logger:
    """
    Возвращает дочерний логгер. Использовать во всех модулях:

        from .logger import get_logger
        log = get_logger(__name__)
    """
    return logging.getLogger(name)


def register_gui_handler(handler: Callable[[str, str], None]) -> None:
    """Регистрирует обработчик логов для GUI."""
    if handler not in _gui_handlers:
        _gui_handlers.append(handler)


# ---------------------------------------------------------------------------
# Внутренний обработчик для GUI
# ---------------------------------------------------------------------------

class _GuiLogHandler(logging.Handler):
    """Пересылает записи логов в GUI-обработчики."""

    def emit(self, record: logging.LogRecord) -> None:
        if not _gui_handlers:
            return
        try:
            msg = self.format(record)
            for handler in list(_gui_handlers):
                try:
                    handler(msg, record.levelname)
                except Exception:
                    pass
        except Exception:
            pass