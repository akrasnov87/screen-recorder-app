"""Управление связями между записями.

Связи хранятся в session.json каждой записи в поле `relations`.

Формат:
    "relations": [
        {
            "target_session_id": "2026-10-01_10-30-00",
            "target_dir": "/abs/path/to/session",
            "target_name": "Название",
            "target_record_id": "770e8400-e29b-41d4-a716-...",
            "kind": "related"   # related | parent | child
        }
    ]

Поля:
  • target_session_id — имя папки сессии (локальный идентификатор);
  • target_dir        — абсолютный путь к папке (для быстрого доступа);
  • target_name       — название записи (кэш для UI);
  • target_record_id  — UUID записи на сервере синхронизации
                        (если опубликована). Нужен для стабильной
                        связи между устройствами: session_id
                        может отличаться, record_id — нет;
  • kind              — вид связи.

Виды связей:
  • related — связан (симметрично, по умолчанию)
  • parent  — эта запись является родителем для target
  • child   — эта запись является дочерней для target

Обратные связи вычисляются динамически:
  • parent (A→B) ↔ child (B→A)
  • related (A→B) ↔ related (B→A)

Изменения:
  • Модуль создан.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from .file_readers import read_json_file
from .logger import get_logger

log = get_logger(__name__)


RELATION_KINDS: Dict[str, str] = {
    "related": "Связан",
    "parent": "Родитель",
    "child": "Дочерний",
}

DEFAULT_RELATION_KIND = "related"


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------
def _inverse_kind(kind: str) -> str:
    """Обратный вид связи для двустороннего отображения."""
    if kind == "parent":
        return "child"
    if kind == "child":
        return "parent"
    return "related"


def _session_id_from_dir(session_dir: str) -> str:
    """session_id = имя папки сессии."""
    return os.path.basename(os.path.abspath(session_dir).rstrip("/"))


def _read_meta(session_dir: str) -> Dict[str, Any]:
    """Читает session.json конкретной сессии."""
    return read_json_file(
        os.path.join(session_dir, "session.json")
    ) or {}


def _read_record_id(session_dir: str) -> str:
    """
    Читает record_id записи (server_id или из .sync_published.json).

    Приоритет:
      1. session.json → server_id
      2. .sync_published.json → record_id
    """
    meta = _read_meta(session_dir)
    sid = str(meta.get("server_id") or "").strip()
    if sid:
        return sid

    marker = os.path.join(session_dir, ".sync_published.json")
    if os.path.isfile(marker):
        try:
            import json
            with open(marker, "r", encoding="utf-8") as f:
                data = json.load(f)
            return str(data.get("record_id") or "").strip()
        except Exception:
            pass
    return ""


# ---------------------------------------------------------------------------
# Нормализация
# ---------------------------------------------------------------------------
def normalize_relation(raw: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """
    Приводит одно звено к каноническому виду.

    Возвращает None, если звено пустое (нет ни target_session_id,
    ни target_dir, ни target_record_id).
    """
    if not isinstance(raw, dict):
        return None

    target_session_id = str(
        raw.get("target_session_id") or ""
    ).strip()
    target_dir = str(raw.get("target_dir") or "").strip()
    target_name = str(raw.get("target_name") or "").strip()
    target_record_id = str(
        raw.get("target_record_id") or ""
    ).strip()

    kind = str(
        raw.get("kind") or DEFAULT_RELATION_KIND
    ).strip().lower()
    if kind not in RELATION_KINDS:
        kind = DEFAULT_RELATION_KIND

    if not (target_session_id or target_dir or target_record_id):
        return None

    return {
        "target_session_id": target_session_id,
        "target_dir": target_dir,
        "target_name": target_name,
        "target_record_id": target_record_id,
        "kind": kind,
    }


# ---------------------------------------------------------------------------
# Чтение / запись
# ---------------------------------------------------------------------------
def load_relations(session_dir: str) -> List[Dict[str, str]]:
    """
    Читает relations из session.json.

    Возвращает нормализованный список (только валидные звенья).
    """
    meta = _read_meta(session_dir)
    raw = meta.get("relations")
    if not isinstance(raw, list):
        return []

    result: List[Dict[str, str]] = []
    seen: set = set()
    for item in raw:
        norm = normalize_relation(item)
        if norm is None:
            continue
        # Уникальность: комбинация (session_id | record_id, kind).
        key = (
            norm["target_session_id"] or norm["target_record_id"],
            norm["kind"],
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(norm)
    return result


def save_relations(
    session_dir: str, relations: List[Dict[str, str]],
) -> bool:
    """
    Записывает relations в session.json (атомарно).

    Также обновляет target_name / target_dir / target_record_id
    по актуальному session.json целевой записи, если они
    изменились.
    """
    session_json = os.path.join(session_dir, "session.json")
    meta = _read_meta(session_dir)

    normalized: List[Dict[str, str]] = []
    seen: set = set()

    for raw in relations or []:
        norm = normalize_relation(raw)
        if norm is None:
            continue

        # --- Обновляем данные цели по актуальному session.json ---
        target_dir = norm["target_dir"]
        if not target_dir and norm["target_session_id"]:
            # Ищем папку рядом с текущей.
            parent = os.path.dirname(session_dir)
            candidate = os.path.join(
                parent, norm["target_session_id"]
            )
            if os.path.isdir(candidate):
                target_dir = candidate
                norm["target_dir"] = candidate

        if target_dir and os.path.isdir(target_dir):
            target_meta = _read_meta(target_dir)
            fresh_name = str(target_meta.get("name") or "").strip()
            if fresh_name:
                norm["target_name"] = fresh_name
            if not norm["target_session_id"]:
                norm["target_session_id"] = _session_id_from_dir(
                    target_dir
                )
            # Свежий record_id (если цель опубликована).
            fresh_rid = _read_record_id(target_dir)
            if fresh_rid:
                norm["target_record_id"] = fresh_rid

        key = (
            norm["target_session_id"] or norm["target_record_id"],
            norm["kind"],
        )
        if key in seen:
            continue
        seen.add(key)
        normalized.append(norm)

    meta["relations"] = normalized

    try:
        import json
        tmp = session_json + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
        os.replace(tmp, session_json)
        log.info(
            "Связи сохранены: %s (%d шт.)",
            session_json, len(normalized),
        )
        return True
    except Exception as exc:
        log.exception(
            "Не удалось сохранить связи в %s: %s",
            session_json, exc,
        )
        return False


# ---------------------------------------------------------------------------
# Поиск связанных сессий
# ---------------------------------------------------------------------------
def _build_record_index(
    sessions_root: str,
) -> Dict[str, str]:
    """
    Строит индекс record_id → session_dir для всех сессий.

    Нужен для поиска по target_record_id (стабильный ID с сервера)
    и для обратного поиска.
    """
    index: Dict[str, str] = {}
    if not os.path.isdir(sessions_root):
        return index

    try:
        entries = os.listdir(sessions_root)
    except OSError:
        return index

    for name in entries:
        full = os.path.join(sessions_root, name)
        if not os.path.isdir(full):
            continue
        rid = _read_record_id(full)
        if rid:
            index[rid] = full
    return index


def _build_session_id_index(
    sessions_root: str,
) -> Dict[str, str]:
    """Индекс session_id (= имени папки) → session_dir."""
    index: Dict[str, str] = {}
    if not os.path.isdir(sessions_root):
        return index
    try:
        entries = os.listdir(sessions_root)
    except OSError:
        return index
    for name in entries:
        full = os.path.join(sessions_root, name)
        if os.path.isdir(full):
            index[name] = full
    return index


def resolve_target_dir(
    rel: Dict[str, str],
    *,
    sessions_root: str = "",
    session_id_index: Optional[Dict[str, str]] = None,
    record_id_index: Optional[Dict[str, str]] = None,
) -> str:
    """
    Возвращает актуальный путь к целевой записи.

    Порядок поиска:
      1. target_dir, если он существует.
      2. target_session_id через session_id_index.
      3. target_record_id через record_id_index.
      4. Рядом с текущей папкой (для случая, когда
         sessions_root не передан, но связь локальная).
    """
    # 1. Прямой путь
    tdir = rel.get("target_dir") or ""
    if tdir and os.path.isdir(tdir):
        return tdir

    # 2. По session_id
    sid = rel.get("target_session_id") or ""
    if sid and session_id_index:
        found = session_id_index.get(sid)
        if found and os.path.isdir(found):
            return found

    # 3. По record_id
    rid = rel.get("target_record_id") or ""
    if rid and record_id_index:
        found = record_id_index.get(rid)
        if found and os.path.isdir(found):
            return found

    return ""


def find_related_sessions(
    sessions_root: str,
    session_dir: str,
) -> List[Dict[str, Any]]:
    """
    Возвращает список записей, связанных с данной (двусторонне).

    Для каждой связанной записи возвращает:
        {
          "session_dir": str,
          "session_id": str,
          "record_id": str,
          "name": str,
          "project": str,
          "date": str,
          "kind": str,           # вид связи ОТ исходной записи
          "kind_reverse": str,   # вид связи ОТ связанной
          "outgoing": bool,      # True если связь исходит от исходной
        }

    Для входящих связей ищем сессии, у которых есть звено
    на текущую — по target_session_id или target_record_id.
    """
    result: List[Dict[str, Any]] = []
    seen_dirs: set = set()

    src_abs = os.path.abspath(session_dir)
    src_id = _session_id_from_dir(session_dir)
    src_rid = _read_record_id(session_dir)

    session_id_index = _build_session_id_index(sessions_root)
    record_id_index = _build_record_index(sessions_root)

    # ---------------------------------------------------------------
    # Исходящие связи
    # ---------------------------------------------------------------
    for rel in load_relations(session_dir):
        target_dir = resolve_target_dir(
            rel,
            sessions_root=sessions_root,
            session_id_index=session_id_index,
            record_id_index=record_id_index,
        )
        if not target_dir:
            continue

        target_abs = os.path.abspath(target_dir)
        if target_abs == src_abs or target_abs in seen_dirs:
            continue
        seen_dirs.add(target_abs)

        target_meta = _read_meta(target_dir)
        result.append({
            "session_dir": target_dir,
            "session_id": (
                rel["target_session_id"]
                or _session_id_from_dir(target_dir)
            ),
            "record_id": (
                rel["target_record_id"]
                or _read_record_id(target_dir)
            ),
            "name": (
                target_meta.get("name")
                or os.path.basename(target_dir)
            ),
            "project": target_meta.get("project") or "",
            "date": target_meta.get("date") or "",
            "kind": rel["kind"],
            "kind_reverse": _inverse_kind(rel["kind"]),
            "outgoing": True,
        })

    # ---------------------------------------------------------------
    # Входящие связи
    # ---------------------------------------------------------------
    if not os.path.isdir(sessions_root):
        return result

    try:
        entries = sorted(os.listdir(sessions_root))
    except OSError:
        return result

    for name in entries:
        other_dir = os.path.join(sessions_root, name)
        if not os.path.isdir(other_dir):
            continue
        other_abs = os.path.abspath(other_dir)
        if other_abs == src_abs or other_abs in seen_dirs:
            continue

        for rel in load_relations(other_dir):
            # Совпадение по session_id или record_id.
            matches = False
            if src_id and rel["target_session_id"] == src_id:
                matches = True
            elif (src_rid
                  and rel["target_record_id"] == src_rid):
                matches = True
            elif (rel["target_dir"]
                  and os.path.abspath(rel["target_dir"]) == src_abs):
                matches = True

            if not matches:
                continue

            other_meta = _read_meta(other_dir)
            result.append({
                "session_dir": other_dir,
                "session_id": _session_id_from_dir(other_dir),
                "record_id": _read_record_id(other_dir),
                "name": other_meta.get("name") or name,
                "project": other_meta.get("project") or "",
                "date": other_meta.get("date") or "",
                "kind": _inverse_kind(rel["kind"]),
                "kind_reverse": rel["kind"],
                "outgoing": False,
            })
            seen_dirs.add(other_abs)
            break

    log.debug(
        "find_related_sessions(%s): найдено %d",
        os.path.basename(session_dir), len(result),
    )
    return result


def list_all_sessions_except(
    sessions_root: str, exclude_dir: str,
) -> List[Dict[str, Any]]:
    """
    Возвращает список всех сессий, кроме указанной.

    Используется в диалоге выбора связанной записи.
    """
    result: List[Dict[str, Any]] = []
    if not os.path.isdir(sessions_root):
        return result

    exclude_abs = (
        os.path.abspath(exclude_dir) if exclude_dir else ""
    )

    try:
        entries = sorted(os.listdir(sessions_root))
    except OSError:
        return result

    for name in entries:
        full = os.path.join(sessions_root, name)
        if not os.path.isdir(full):
            continue
        if os.path.abspath(full) == exclude_abs:
            continue
        meta = _read_meta(full)
        result.append({
            "session_dir": full,
            "session_id": name,
            "record_id": _read_record_id(full),
            "name": meta.get("name") or name,
            "project": meta.get("project") or "",
            "date": meta.get("date") or "",
            "tags": meta.get("tags") or [],
        })

    result.sort(key=lambda r: r["date"], reverse=True)
    return result