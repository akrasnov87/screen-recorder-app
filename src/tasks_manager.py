"""Управление поручениями (action items) записей.

Поручения хранятся в отдельном файле `action_items.json` внутри
папки сессии. Структура файла:

    {
      "version": 1,
      "session_id": "2026-10-05_08-30-00",   # имя папки сессии
      "session_name": "ПРОТОКОЛ СОВЕЩАНИЯ...",
      "updated_at": "2026-10-05T09:15:00",
      "items": [
        {
          "id": "uuid4-hex",
          "text": "Подготовить макет",
          "assignee": "Иванов И.И.",
          "status": "created",         # created|in_progress|waiting|done
          "due_date": "2026-10-10",
          "created_at": "2026-10-05T09:00:00",
          "updated_at": "2026-10-05T09:15:00",
          "context": "ПРОТОКОЛ СОВЕЩАНИЯ ЕЖД-2026-10-05",
          "source": "manual",          # manual|import|auto
          "comment": ""
        },
        ...
      ]
    }

Статусы:
  • created     — создан (по умолчанию)
  • in_progress — в работе
  • waiting     — ожидание
  • done        — выполнен
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from .file_readers import read_json_file
from .logger import get_logger

log = get_logger(__name__)


# Имя файла поручений внутри папки сессии.
ACTION_ITEMS_FILE = "action_items.json"

# Текущая версия формата.
ACTION_ITEMS_VERSION = 1

# Возможные статусы и их человекочитаемые названия.
STATUS_LABELS: Dict[str, str] = {
    "created":     "Создан",
    "in_progress": "В работе",
    "waiting":     "Ожидание",
    "done":        "Выполнен",
}

STATUS_ORDER: List[str] = [
    "created", "in_progress", "waiting", "done",
]

# Цвета для UI (HEX).
STATUS_COLORS: Dict[str, str] = {
    "created":     "#1565C0",  # синий
    "in_progress": "#EF6C00",  # оранжевый
    "waiting":     "#7B1FA2",  # фиолетовый
    "done":        "#2E7D32",  # зелёный
}


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------
def _iso_now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


def _path_for(session_dir: str) -> str:
    return os.path.join(session_dir, ACTION_ITEMS_FILE)


# ---------------------------------------------------------------------------
# Чтение / запись
# ---------------------------------------------------------------------------
def load_action_items(session_dir: str) -> Dict[str, Any]:
    """
    Загружает action_items.json из папки сессии.

    Если файла нет — возвращает пустую структуру, привязанную
    к этой сессии.
    """
    path = _path_for(session_dir)
    data = read_json_file(path)

    if not isinstance(data, dict):
        data = {}

    items = data.get("items")
    if not isinstance(items, list):
        items = []

    # Нормализуем элементы — на случай ручной правки JSON.
    normalized: List[Dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        normalized.append(_normalize_item(raw))

    result: Dict[str, Any] = {
        "version": int(data.get("version", ACTION_ITEMS_VERSION)),
        "session_id": str(
            data.get("session_id")
            or os.path.basename(session_dir.rstrip("/"))
        ),
        "session_name": str(data.get("session_name") or ""),
        "updated_at": str(data.get("updated_at") or ""),
        "items": normalized,
    }
    return result


def save_action_items(
    session_dir: str, data: Dict[str, Any],
) -> bool:
    """
    Атомарно сохраняет action_items.json.

    Возвращает True при успехе.
    """
    try:
        os.makedirs(session_dir, exist_ok=True)

        payload = {
            "version": ACTION_ITEMS_VERSION,
            "session_id": str(data.get("session_id") or ""),
            "session_name": str(data.get("session_name") or ""),
            "updated_at": _iso_now(),
            "items": [
                _normalize_item(it)
                for it in (data.get("items") or [])
                if isinstance(it, dict)
            ],
        }

        path = _path_for(session_dir)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)

        log.info(
            "Поручения сохранены: %s (%d шт.)",
            path, len(payload["items"]),
        )
        return True
    except Exception as exc:
        log.exception(
            "Не удалось сохранить поручения в %s: %s",
            session_dir, exc,
        )
        return False


# ---------------------------------------------------------------------------
# Нормализация / создание элемента
# ---------------------------------------------------------------------------
def _normalize_item(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Приводит один элемент к каноническому виду."""
    status = str(raw.get("status") or "created").strip().lower()
    if status not in STATUS_LABELS:
        status = "created"

    item_id = str(raw.get("id") or "").strip() or _new_id()

    return {
        "id": item_id,
        "text": str(raw.get("text") or "").strip(),
        "assignee": str(raw.get("assignee") or "").strip(),
        "status": status,
        "due_date": str(raw.get("due_date") or "").strip(),
        "created_at": str(raw.get("created_at") or _iso_now()),
        "updated_at": str(raw.get("updated_at") or _iso_now()),
        "context": str(raw.get("context") or "").strip(),
        "source": str(raw.get("source") or "manual").strip(),
        "comment": str(raw.get("comment") or "").strip(),
    }


def make_item(
    text: str,
    *,
    assignee: str = "",
    status: str = "created",
    due_date: str = "",
    context: str = "",
    source: str = "manual",
    comment: str = "",
) -> Dict[str, Any]:
    """Создаёт новый элемент поручения."""
    now = _iso_now()
    return _normalize_item({
        "id": _new_id(),
        "text": text,
        "assignee": assignee,
        "status": status,
        "due_date": due_date,
        "created_at": now,
        "updated_at": now,
        "context": context,
        "source": source,
        "comment": comment,
    })


# ---------------------------------------------------------------------------
# CRUD-операции над элементами
# ---------------------------------------------------------------------------
def add_item(
    session_dir: str,
    item: Dict[str, Any],
    *,
    session_name: str = "",
) -> Dict[str, Any]:
    """Добавляет поручение и сохраняет файл. Возвращает добавленный элемент."""
    data = load_action_items(session_dir)
    if session_name:
        data["session_name"] = session_name

    item = _normalize_item(item)
    item["updated_at"] = _iso_now()
    data["items"].append(item)
    save_action_items(session_dir, data)
    return item


def update_item(
    session_dir: str,
    item_id: str,
    patch: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Обновляет поручение по id. Возвращает обновлённый элемент."""
    data = load_action_items(session_dir)
    updated: Optional[Dict[str, Any]] = None

    for i, item in enumerate(data["items"]):
        if item.get("id") != item_id:
            continue
        merged = dict(item)
        merged.update(patch)
        merged["id"] = item_id  # id менять нельзя
        merged["updated_at"] = _iso_now()
        data["items"][i] = _normalize_item(merged)
        updated = data["items"][i]
        break

    if updated is None:
        log.warning(
            "Поручение %s не найдено в %s", item_id, session_dir
        )
        return None

    save_action_items(session_dir, data)
    return updated


def delete_item(session_dir: str, item_id: str) -> bool:
    """Удаляет поручение по id. Возвращает True при успехе."""
    data = load_action_items(session_dir)
    before = len(data["items"])
    data["items"] = [
        it for it in data["items"] if it.get("id") != item_id
    ]
    if len(data["items"]) == before:
        return False
    save_action_items(session_dir, data)
    return True


# ---------------------------------------------------------------------------
# Импорт / экспорт
# ---------------------------------------------------------------------------
def export_items(
    session_dir: str, target_path: str,
) -> bool:
    """
    Сохраняет копию action_items.json в произвольное место.
    """
    data = load_action_items(session_dir)
    try:
        with open(target_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        log.info("Поручения экспортированы: %s", target_path)
        return True
    except Exception as exc:
        log.exception(
            "Не удалось экспортировать поручения в %s: %s",
            target_path, exc,
        )
        return False


def import_items(
    session_dir: str, source_path: str,
    *,
    merge: bool = True,
    session_name: str = "",
) -> int:
    """
    Импортирует поручения из JSON-файла.

    Args:
        session_dir:  папка сессии, в которую импортируем.
        source_path:  путь к JSON-файлу.
        merge:        True — добавить к существующим;
                      False — заменить полностью.
        session_name: обновить имя сессии.

    Returns:
        Количество добавленных элементов.
    """
    data = read_json_file(source_path)
    if not isinstance(data, dict):
        log.warning(
            "Импорт поручений: %s не является JSON-объектом",
            source_path,
        )
        return 0

    incoming = data.get("items")
    if not isinstance(incoming, list):
        log.warning(
            "Импорт поручений: нет массива items в %s", source_path
        )
        return 0

    current = load_action_items(session_dir)
    if session_name:
        current["session_name"] = session_name

    existing_ids = {it.get("id") for it in current["items"]}
    added = 0

    if not merge:
        current["items"] = []

    for raw in incoming:
        if not isinstance(raw, dict):
            continue
        item = _normalize_item(raw)
        # Если id уже есть — генерируем новый, чтобы не перетирать.
        if item["id"] in existing_ids:
            item["id"] = _new_id()
        item["source"] = "import"
        item["updated_at"] = _iso_now()
        current["items"].append(item)
        existing_ids.add(item["id"])
        added += 1

    save_action_items(session_dir, current)
    log.info(
        "Импортировано поручений: %d (в %s)", added, session_dir
    )
    return added


# ---------------------------------------------------------------------------
# Сбор поручений по всем сессиям
# ---------------------------------------------------------------------------
def scan_all_action_items(sessions_root: str) -> List[Dict[str, Any]]:
    """
    Собирает поручения из всех сессий в корневой папке.

    Возвращает список словарей с дополнительными полями:
      • session_dir
      • session_id
      • session_name
    """
    result: List[Dict[str, Any]] = []

    if not os.path.isdir(sessions_root):
        return result

    try:
        entries = sorted(os.listdir(sessions_root))
    except OSError as exc:
        log.error(
            "Не удалось прочитать %s: %s", sessions_root, exc
        )
        return result

    for name in entries:
        session_dir = os.path.join(sessions_root, name)
        if not os.path.isdir(session_dir):
            continue

        path = _path_for(session_dir)
        if not os.path.isfile(path):
            continue

        data = load_action_items(session_dir)
        session_name = data.get("session_name") or name
        session_id = data.get("session_id") or name

        for item in data["items"]:
            enriched = dict(item)
            enriched["session_dir"] = session_dir
            enriched["session_id"] = session_id
            enriched["session_name"] = session_name
            result.append(enriched)

    log.debug(
        "scan_all_action_items: найдено %d поручений в %s",
        len(result), sessions_root,
    )
    return result

# ---------------------------------------------------------------------------
# Просроченные поручения
# ---------------------------------------------------------------------------
from datetime import date as _date


def _parse_due_date(raw: str) -> Optional[_date]:
    """
    Парсит дату срока в формате YYYY-MM-DD.

    Возвращает datetime.date или None, если строка пустая
    или не соответствует формату.
    """
    s = (raw or "").strip()
    if not s:
        return None
    try:
        y, m, d = s.split("-")
        return _date(int(y), int(m), int(d))
    except Exception:
        return None


def is_overdue(item: Dict[str, Any]) -> bool:
    """
    Просрочено ли поручение.

    Условия просрочки (все должны выполниться):
      • задан due_date;
      • due_date < сегодня;
      • статус не 'done'.

    Поручение со сроком «сегодня» НЕ считается просроченным —
    оно действительно до конца дня.
    """
    if (item.get("status") or "created") == "done":
        return False

    due = _parse_due_date(item.get("due_date") or "")
    if due is None:
        return False

    return due < _date.today()


def due_date_priority(item: Dict[str, Any]) -> int:
    """
    Приоритет сортировки по сроку:
      0 — просрочено
      1 — срок сегодня
      2 — срок в будущем
      3 — срока нет
      4 — выполнено
    """
    if (item.get("status") or "created") == "done":
        return 4

    due = _parse_due_date(item.get("due_date") or "")
    if due is None:
        return 3

    today = _date.today()
    if due < today:
        return 0
    if due == today:
        return 1
    return 2


def days_until_due(item: Dict[str, Any]) -> Optional[int]:
    """
    Сколько дней до срока (может быть отрицательным).

    None — если срока нет.
    """
    due = _parse_due_date(item.get("due_date") or "")
    if due is None:
        return None
    return (due - _date.today()).days