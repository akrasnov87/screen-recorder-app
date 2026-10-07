"""Управление поручениями (action items) записей.

Поручения хранятся в отдельном файле `action_items.json` внутри
папки сессии. Структура файла:

    {
      "version": 2,
      "session_id": "2026-10-05_08-30-00",   # имя папки сессии
      "session_name": "ПРОТОКОЛ СОВЕЩАНИЯ...",
      "updated_at": "2026-10-05T09:15:00",
      "items": [
        {
          "id": "uuid4-hex",
          "number": 42,
          "text": "Подготовить макет",
          "assignee": "Иванов И.И.",
          ...
        }
      ]
    }

`number` — сквозной числовой номер поручения по всем записям.
Счётчик хранится в <sessions_root>/action_items_counter.json.

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

# Имя файла-счётчика сквозных номеров в корне сессий.
COUNTER_FILE = "action_items_counter.json"

# Текущая версия формата.
ACTION_ITEMS_VERSION = 2

# Возможные статусы и их человекочитаемые названия.
STATUS_LABELS: Dict[str, str] = {
    "created":     "Создан",
    "in_progress": "В работе",
    "waiting":     "Ожидание",
    "done":        "Выполнен",
    "cancelled":   "Отмена",
}

STATUS_ORDER: List[str] = [
    "created", "in_progress", "waiting", "done", "cancelled",
]

# Цвета для UI (HEX).
STATUS_COLORS: Dict[str, str] = {
    "created":     "#1565C0",  # синий
    "in_progress": "#EF6C00",  # оранжевый
    "waiting":     "#7B1FA2",  # фиолетовый
    "done":        "#2E7D32",  # зелёный
    "cancelled":   "#757575",  # серый
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


def _sessions_root_from_session_dir(session_dir: str) -> str:
    """
    Возвращает корень sessions/ по папке конкретной сессии.

    Папка сессии обычно лежит как <sessions_root>/<session_name>,
    поэтому родительская папка и есть sessions_root.
    """
    return os.path.dirname(os.path.abspath(session_dir.rstrip("/")))


def _counter_path(sessions_root: str) -> str:
    return os.path.join(sessions_root, COUNTER_FILE)


# ---------------------------------------------------------------------------
# Счётчик сквозных номеров
# ---------------------------------------------------------------------------
def _load_counter(sessions_root: str) -> int:
    """
    Читает текущее значение счётчика из <sessions_root>/action_items_counter.json.

    Если файла нет или он битый — возвращает 0.
    """
    path = _counter_path(sessions_root)
    data = read_json_file(path)
    if not isinstance(data, dict):
        return 0
    try:
        return max(0, int(data.get("last_number", 0)))
    except (TypeError, ValueError):
        return 0


def _save_counter(sessions_root: str, value: int) -> None:
    """Атомарно сохраняет значение счётчика."""
    try:
        os.makedirs(sessions_root, exist_ok=True)
        path = _counter_path(sessions_root)
        tmp = path + ".tmp"
        payload = {
            "last_number": int(value),
            "updated_at": _iso_now(),
        }
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as exc:
        log.error(
            "Не удалось сохранить счётчик поручений %s: %s",
            sessions_root, exc,
        )


def get_next_item_number(session_dir: str) -> int:
    """
    Возвращает следующий сквозной номер поручения и увеличивает
    счётчик в <sessions_root>/action_items_counter.json.

    Атомарность на уровне процесса: критической гонки нет,
    так как все операции выполняются в одном потоке
    (Qt GUI / процессор).
    """
    sessions_root = _sessions_root_from_session_dir(session_dir)
    current = _load_counter(sessions_root)
    next_number = current + 1
    _save_counter(sessions_root, next_number)
    log.debug(
        "Выдан номер поручения: %d (sessions_root=%s)",
        next_number, sessions_root,
    )
    return next_number


# ---------------------------------------------------------------------------
# Чтение / запись
# ---------------------------------------------------------------------------
def load_action_items(session_dir: str) -> Dict[str, Any]:
    """
    Загружает action_items.json из папки сессии.

    Если файла нет — возвращает пустую структуру, привязанную
    к этой сессии.

    ВАЖНО: при загрузке элементов, у которых нет поля `number`,
    присваивает им номер из счётчика (ленивая миграция).
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
    needs_migration = False
    for raw in items:
        if not isinstance(raw, dict):
            continue
        item = _normalize_item(raw)
        if not item.get("number"):
            needs_migration = True
        normalized.append(item)

    # Если есть элементы без номера — присваиваем и сохраняем.
    if needs_migration and normalized:
        for item in normalized:
            if not item.get("number"):
                item["number"] = get_next_item_number(session_dir)
        log.info(
            "Ленивая миграция номеров: %d элементов в %s",
            len(normalized), session_dir,
        )
        result = {
            "version": ACTION_ITEMS_VERSION,
            "session_id": str(
                data.get("session_id")
                or os.path.basename(session_dir.rstrip("/"))
            ),
            "session_name": str(data.get("session_name") or ""),
            "updated_at": _iso_now(),
            "items": normalized,
        }
        save_action_items(session_dir, result, skip_normalize=True)
        return result

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
    *,
    skip_normalize: bool = False,
) -> bool:
    """
    Атомарно сохраняет action_items.json.

    Возвращает True при успехе.

    Args:
        session_dir:    папка сессии.
        data:           словарь с items.
        skip_normalize: если True — элементы не проходят
                        _normalize_item повторно (используется
                        при ленивой миграции, чтобы не сбить
                        только что присвоенные номера).
    """
    try:
        os.makedirs(session_dir, exist_ok=True)

        if skip_normalize:
            items_out = list(data.get("items") or [])
        else:
            items_out = [
                _normalize_item(it)
                for it in (data.get("items") or [])
                if isinstance(it, dict)
            ]

        payload = {
            "version": ACTION_ITEMS_VERSION,
            "session_id": str(data.get("session_id") or ""),
            "session_name": str(data.get("session_name") or ""),
            "updated_at": _iso_now(),
            "items": items_out,
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

    # Номер: int или 0 (0 = «ещё не присвоен»).
    number_raw = raw.get("number")
    number = 0
    if number_raw is not None:
        try:
            number = int(number_raw)
        except (TypeError, ValueError):
            number = 0

    return {
        "id": item_id,
        "number": number,
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
    session_dir: str = "",
    assignee: str = "",
    status: str = "created",
    due_date: str = "",
    context: str = "",
    source: str = "manual",
    comment: str = "",
    number: int = 0,
) -> Dict[str, Any]:
    """
    Создаёт новый элемент поручения.

    Args:
        session_dir: если задан и number==0 — номер берётся
                     из сквозного счётчика.
        number:      если > 0 — используется как есть
                     (например, при импорте с сохранением номеров).
    """
    # Если номер не задан — берём из счётчика.
    if number <= 0 and session_dir:
        number = get_next_item_number(session_dir)

    now = _iso_now()
    return _normalize_item({
        "id": _new_id(),
        "number": number,
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
    """Добавляет поручение и сохраняет файл."""
    data = load_action_items(session_dir)
    if session_name:
        data["session_name"] = session_name

    item = _normalize_item(item)
    # Если у элемента нет номера — присваиваем.
    if not item.get("number"):
        item["number"] = get_next_item_number(session_dir)
    item["updated_at"] = _iso_now()
    data["items"].append(item)
    save_action_items(session_dir, data, skip_normalize=True)
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
        # number тоже менять нельзя через patch.
        merged["number"] = item.get("number") or 0
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
    """Сохраняет копию action_items.json в произвольное место."""
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
    preserve_numbers: bool = False,
) -> int:
    """
    Импортирует поручения из JSON-файла.

    Args:
        session_dir:      папка сессии, в которую импортируем.
        source_path:      путь к JSON-файлу.
        merge:            True — добавить к существующим;
                          False — заменить полностью.
        session_name:     обновить имя сессии.
        preserve_numbers: если True — сохранять номера из файла
                          (если они там есть). По умолчанию
                          номера выдаются заново, чтобы не
                          было коллизий со сквозной нумерацией.

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

        # Номер: либо сохраняем из файла, либо выдаём новый.
        if preserve_numbers and item.get("number"):
            pass  # оставляем как есть
        else:
            item["number"] = get_next_item_number(session_dir)

        item["source"] = "import"
        item["updated_at"] = _iso_now()
        current["items"].append(item)
        existing_ids.add(item["id"])
        added += 1

    save_action_items(session_dir, current, skip_normalize=True)
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
    st = (item.get("status") or "created")
    if st in ("done", "cancelled"):
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
    st = (item.get("status") or "created")
    if st in ("done", "cancelled"):
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