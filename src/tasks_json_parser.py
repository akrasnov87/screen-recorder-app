"""Парсер JSON поручений из буфера обмена или файла.

Поддерживает несколько форматов входных данных:
  • полный объект action_items.json ({version, items: [...]});
  • объект только с items;
  • массив поручений ([{...}, {...}]);
  • одиночный объект-поручение;
  • русские синонимы полей (текст/исполнитель/срок/номер/...).

Возвращает нормализованный список поручений (dict с полями
tasks_manager) и метаинформацию о формате.

Изменения:
  • Добавлен синоним поля «number» (номер/№/num/no).
  • При разборе номера из JSON: если он отсутствует или
    невалиден — возвращается 0 (номер будет выдан
    автоматически при импорте/вставке).
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from .logger import get_logger
from .tasks_manager import (
    STATUS_LABELS,
    _normalize_item,
)

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Синонимы полей
# ---------------------------------------------------------------------------
_FIELD_ALIASES: Dict[str, List[str]] = {
    "text": [
        "text", "текст", "поручение", "задача", "описание",
        "title", "task", "action",
    ],
    "assignee": [
        "assignee", "исполнитель", "ответственный", "кому",
        "assignee_name", "owner", "responsible",
    ],
    "status": [
        "status", "статус", "состояние",
    ],
    "due_date": [
        "due_date", "срок", "дата", "дедлайн", "deadline",
        "due", "due_at",
    ],
    "comment": [
        "comment", "комментарий", "примечание", "note", "notes",
    ],
    "context": [
        "context", "контекст",
    ],
    "source": [
        "source", "источник",
    ],
    "id": [
        "id", "идентификатор",
    ],
    "number": [
        "number", "номер", "№", "num", "no", "n",
    ],
    "created_at": [
        "created_at", "создано", "created",
    ],
    "updated_at": [
        "updated_at", "обновлено", "updated",
    ],
}


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------
class ParseResult:
    """Результат разбора JSON."""

    def __init__(
        self,
        items: List[Dict[str, Any]],
        *,
        format_kind: str = "",
        session_name: str = "",
        warning: str = "",
    ) -> None:
        self.items = items
        self.format_kind = format_kind
        self.session_name = session_name
        self.warning = warning

    def __bool__(self) -> bool:
        return bool(self.items)

    def __len__(self) -> int:
        return len(self.items)


def parse_action_items_json(raw: str) -> ParseResult:
    """
    Парсит JSON-строку и возвращает нормализованные поручения.

    Никогда не бросает исключение — при ошибке возвращает
    пустой ParseResult с warning.
    """
    if raw is None:
        return ParseResult([], warning="Буфер обмена пуст")

    if not raw.strip():
        return ParseResult(
            [],
            warning="Буфер обмена пуст или содержит только пробелы",
        )

    # --- 1. Убираем BOM и нормализуем переводы строк ---
    text = _strip_bom(raw)
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # --- 2. Убираем markdown-обёртку ```json ... ``` ---
    text = _strip_code_fence(text)

    # --- 3. Обрезаем мусор до первого { или [ ---
    text = _trim_to_json_start(text)

    if not text:
        return ParseResult(
            [],
            warning=(
                "В буфере обмена нет JSON: не найдено ни одной "
                "открывающей скобки '{' или '['."
            ),
        )

    # --- 4. Пробуем распарсить JSON ---
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        fixed = _fix_common_json_errors(text)
        try:
            data = json.loads(fixed)
            log.info(
                "JSON распарсен после мягкой коррекции "
                "(изначальная ошибка: %s)", exc,
            )
        except json.JSONDecodeError as exc2:
            log.warning(
                "Не удалось разобрать JSON: %s\n"
                "После коррекции: %s", exc, exc2,
            )
            # Обрезаем текст для показа в UI.
            preview = text[:200]
            if len(text) > 200:
                preview += "…"
            return ParseResult(
                [],
                warning=(
                    f"Не удалось разобрать JSON: {exc.msg} "
                    f"(строка {exc.lineno}, позиция {exc.colno}).\n"
                    f"Начало текста: {preview!r}"
                ),
            )

    return _normalize_parsed(data)


# ---------------------------------------------------------------------------
# Обработка форматов
# ---------------------------------------------------------------------------
def _normalize_parsed(data: Any) -> ParseResult:
    """Приводит распарсенные данные к списку поручений."""
    # --- Вариант 1: полный объект с items ---
    if isinstance(data, dict) and "items" in data:
        items_raw = data.get("items")
        if not isinstance(items_raw, list):
            return ParseResult(
                [],
                warning="Поле 'items' не является массивом",
            )
        session_name = str(data.get("session_name") or "")
        return ParseResult(
            _normalize_items(items_raw),
            format_kind="object_with_items",
            session_name=session_name,
        )

    # --- Вариант 2: массив поручений ---
    if isinstance(data, list):
        return ParseResult(
            _normalize_items(data),
            format_kind="array",
        )

    # --- Вариант 3: одиночный объект-поручение ---
    if isinstance(data, dict):
        # Проверим, похож ли объект на поручение: есть хотя бы
        # одно из ключевых полей.
        keys_lower = {str(k).lower() for k in data.keys()}
        looks_like_item = any(
            k in keys_lower
            for k in ("text", "текст", "поручение", "задача",
                      "assignee", "исполнитель", "due_date", "срок",
                      "number", "номер")
        )
        if looks_like_item:
            return ParseResult(
                _normalize_items([data]),
                format_kind="single_item",
            )

    return ParseResult(
        [],
        warning=(
            "Неизвестный формат JSON. Ожидался один из:\n"
            "  • объект с полем items;\n"
            "  • массив поручений;\n"
            "  • одиночный объект-поручение."
        ),
    )


def _normalize_items(items_raw: List[Any]) -> List[Dict[str, Any]]:
    """Нормализует список сырых элементов."""
    result: List[Dict[str, Any]] = []
    for idx, raw in enumerate(items_raw):
        if not isinstance(raw, dict):
            log.warning(
                "Элемент #%d не является объектом — пропущен", idx,
            )
            continue

        normalized = _normalize_one(raw)
        if not normalized:
            continue
        result.append(normalized)

    return result


def _normalize_one(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Приводит один элемент к каноническому виду tasks_manager.

    Использует синонимы полей. Если поля нет — берёт дефолт.
    Возвращает None, если не удалось получить текст.
    """
    # Строим lower-case карту ключей: оригинал → значение.
    lower_map: Dict[str, Any] = {}
    for k, v in raw.items():
        lower_map[str(k).lower()] = v

    def get(field: str, default: Any = "") -> Any:
        for alias in _FIELD_ALIASES.get(field, []):
            if alias in lower_map:
                return lower_map[alias]
        return default

    text = str(get("text", "") or "").strip()
    if not text:
        # Если текста нет — элемент бесполезен.
        return None

    assignee = str(get("assignee", "") or "").strip()
    status_raw = str(get("status", "") or "").strip().lower()
    status = _normalize_status(status_raw)

    due_date = _normalize_due_date(get("due_date", ""))
    comment = str(get("comment", "") or "").strip()
    context = str(get("context", "") or "").strip()
    source = str(get("source", "") or "manual").strip()
    item_id = str(get("id", "") or "").strip()

    # --- Номер ---
    number_raw = get("number", 0)
    try:
        number = int(number_raw)
        if number < 0:
            number = 0
    except (TypeError, ValueError):
        number = 0

    # Передаём в _normalize_item из tasks_manager — там
    # проставляются created_at/updated_at, если их нет.
    normalized = _normalize_item({
        "id": item_id or uuid.uuid4().hex,
        "number": number,
        "text": text,
        "assignee": assignee,
        "status": status,
        "due_date": due_date,
        "comment": comment,
        "context": context,
        "source": source,
        "created_at": str(get("created_at", "") or ""),
        "updated_at": str(get("updated_at", "") or ""),
    })
    return normalized


def _normalize_status(raw: str) -> str:
    """Приводит статус к каноническому виду."""
    if not raw:
        return "created"
    if raw in STATUS_LABELS:
        return raw

    # Русские синонимы.
    ru_map = {
        "создан": "created",
        "новая": "created",
        "новая задача": "created",
        "в работе": "in_progress",
        "работа": "in_progress",
        "inprogress": "in_progress",
        "in progress": "in_progress",
        "ожидание": "waiting",
        "wait": "waiting",
        "waiting": "waiting",
        "выполнен": "done",
        "выполнено": "done",
        "готово": "done",
        "закрыт": "done",
    }
    return ru_map.get(raw, "created")


_DATE_PATTERNS = [
    (re.compile(r"^(\d{4})-(\d{2})-(\d{2})$"), "ymd"),
    (re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ]"), "ymd_iso"),
    (re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})$"), "dmy"),
    (re.compile(r"^(\d{2})/(\d{2})/(\d{4})$"), "mdy"),
]


def _normalize_due_date(raw: Any) -> str:
    """Приводит дату к YYYY-MM-DD. Пустая строка при ошибке."""
    if raw is None:
        return ""
    if isinstance(raw, (int, float)):
        # Unix timestamp? Не поддерживаем — пропускаем.
        return ""
    s = str(raw).strip()
    if not s:
        return ""

    # ISO с временем — обрезаем.
    if "T" in s:
        s = s.split("T", 1)[0]
    if " " in s:
        s = s.split(" ", 1)[0]

    for pattern, kind in _DATE_PATTERNS:
        m = pattern.match(s)
        if not m:
            continue
        try:
            if kind == "ymd":
                y, mo, d = m.group(1), m.group(2), m.group(3)
            elif kind == "ymd_iso":
                y, mo, d = m.group(1), m.group(2), m.group(3)
            elif kind == "dmy":
                d, mo, y = m.group(1), m.group(2), m.group(3)
            else:  # mdy
                mo, d, y = m.group(1), m.group(2), m.group(3)
            return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
        except Exception:
            continue

    # Если совсем не получилось — попробуем через date.fromisoformat.
    try:
        return date.fromisoformat(s).isoformat()
    except Exception:
        pass

    log.debug("Не удалось распарсить дату: %r", raw)
    return ""


# ---------------------------------------------------------------------------
# Обработка частых ошибок JSON
# ---------------------------------------------------------------------------
def _strip_code_fence(text: str) -> str:
    """Убирает ```json ... ``` обёртку, если она есть."""
    t = text.strip()
    if t.startswith("```"):
        # Убираем первую строку.
        lines = t.split("\n")
        lines = lines[1:]  # удаляем ```
        # Убираем последнюю ```, если есть.
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return t


def _fix_common_json_errors(text: str) -> str:
    """
    Мягкая коррекция типичных ошибок JSON:
      • NBSP → пробел;
      • // комментарии;
      • /* ... */ комментарии;
      • одинарные кавычки → двойные;
      • висячие запятые;
      • NaN/Infinity → null.
    """
    s = text

    # NBSP → пробел.
    s = s.replace("\u00a0", " ")

    # /* ... */ комментарии (многострочные).
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.DOTALL)

    # // комментарии до конца строки.
    s = re.sub(r"//[^\n]*", "", s)

    # Одинарные кавычки → двойные.
    def _replace_quotes(match: re.Match) -> str:
        inner = match.group(1).replace('"', '\\"')
        return f'"{inner}"'

    s = re.sub(r"'([^'\\]*(?:\\.[^'\\]*)*)'", _replace_quotes, s)

    # NaN/Infinity → null (Python их понимает, но JSON — нет;
    # на всякий случай приводим к null).
    s = re.sub(r"\bNaN\b", "null", s)
    s = re.sub(r"\b-?Infinity\b", "null", s)

    # Висячие запятые перед закрывающей скобкой.
    s = re.sub(r",(\s*[}\]])", r"\1", s)

    return s

def _strip_bom(text: str) -> str:
    """Убирает BOM (UTF-8/UTF-16) и ведущие пробельные символы."""
    if not text:
        return text
    # UTF-8 BOM: '\ufeff'
    # UTF-16 BOM: '\ufffe'
    # Некоторые приложения добавляют zero-width no-break space.
    for ch in ("\ufeff", "\ufffe", "\u200b"):
        if text.startswith(ch):
            text = text[len(ch):]
    return text.lstrip("\n\r\t ")


def _trim_to_json_start(text: str) -> str:
    """
    Обрезает текст до первой '{' или '['.

    Полезно, когда в буфер попал мусор в начале
    («Пример: {…}» или «Вот JSON: […]»).
    """
    if not text:
        return ""

    for ch in ("{", "["):
        idx = text.find(ch)
        if idx == -1:
            continue
        # Если символов до скобки немного (< 40) — обрезаем.
        # Если больше — скорее всего это не JSON, оставляем как есть,
        # чтобы показать пользователю исходный текст.
        if idx <= 40:
            return text[idx:]
        # Для длинного мусора — всё равно пробуем отрезать,
        # чтобы хоть что-то попробовать распарсить.
        return text[idx:]

    return ""