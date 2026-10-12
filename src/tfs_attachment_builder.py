"""Сборка TFS-выборки и сохранение её во вложения записи.

Модуль:
  • находит WIQL-шаблон по имени в config["tfs"]["wiql_templates"];
  • выполняет WIQL к TFS через TfsClient;
  • сохраняет результат в файл:
      – attachments/tfs_work_items.csv — для человека (Excel);
      – attachments/tfs_work_items.md  — для LLM (опционально).
  • Возвращает словарь с результатом.

Формат CSV:
  • разделитель «;» (стандарт русской локали Excel);
  • кодировка utf-8-sig (BOM) — Excel корректно открывает
    кириллицу;
  • колонки: ID, Тип, Заголовок, Статус, Исполнитель,
    Родитель, Оценка, Осталось, Выполнено, Итерация, Теги.

Изменения:
  • Поддержка шаблонных переменных WIQL ({{Имя}}):
      – функция принимает параметр variable_values: Dict[str, str];
      – переменные подставляются в WIQL через
        tfs_client.substitute_variables() перед выполнением;
      – если переменные не заданы — возвращает ошибку с перечнем
        недостающих переменных (без выполнения запроса).
"""
from __future__ import annotations

import csv
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from .logger import get_logger
from .tfs_client import (
    TfsClient,
    TfsError,
    extract_variables,
    substitute_variables,
    validate_variables,
)

log = get_logger(__name__)


# Имена файлов внутри attachments/.
CSV_FILENAME = "tfs_work_items.csv"
MD_FILENAME = "tfs_work_items.md"


# Заголовки и порядок колонок в CSV.
_CSV_COLUMNS: List[tuple] = [
    ("id",                 "ID"),
    ("work_item_type",     "Тип"),
    ("title",              "Заголовок"),
    ("state",              "Статус"),
    ("assigned_to",        "Исполнитель"),
    ("parent_id",          "Родитель"),
    ("original_estimate",  "Оценка"),
    ("remaining_work",     "Осталось"),
    ("completed_work",     "Выполнено"),
    ("iteration_path",     "Итерация"),
    ("tags",               "Теги"),
]


def _fmt_hours(value: Any) -> str:
    """Формат часов: целое без .0, иначе — с одним знаком."""
    if value is None or value == 0:
        return ""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if v == int(v):
        return str(int(v))
    return f"{v:g}"


def _format_cell(value: Any, key: str = "") -> str:
    """Приводит значение к строке для CSV/MD."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(x) for x in value)
    if key in ("original_estimate", "remaining_work",
               "completed_work"):
        return _fmt_hours(value)
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value).strip()


def _save_csv(
    items: List[Dict[str, Any]], path: str,
) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(
            f, delimiter=";", quoting=csv.QUOTE_MINIMAL
        )
        writer.writerow([label for _, label in _CSV_COLUMNS])

        for it in items:
            row = [
                _format_cell(it.get(key), key)
                for key, _ in _CSV_COLUMNS
            ]
            writer.writerow(row)


def _save_markdown(
    items: List[Dict[str, Any]],
    path: str,
    *,
    project: str = "",
    template: str = "",
) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    lines: List[str] = []
    lines.append("# Задачи TFS")
    lines.append("")
    if project:
        lines.append(f"**Проект:** {project}")
    if template:
        lines.append(f"**Шаблон WIQL:** {template}")
    lines.append(
        f"**Сформировано:** {datetime.now():%Y-%m-%d %H:%M:%S}"
    )
    lines.append(f"**Задач:** {len(items)}")
    lines.append("")

    headers = [label for _, label in _CSV_COLUMNS]
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("| " + " | ".join(["---"] * len(headers)) + " |")

    for it in items:
        cells = [
            _format_cell(it.get(key), key).replace("|", "/")
            for key, _ in _CSV_COLUMNS
        ]
        lines.append("| " + " | ".join(cells) + " |")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def find_template_wiql(
    templates: List[Dict[str, str]], name: str,
) -> str:
    """Ищет WIQL-шаблон по имени. Возвращает "" если не найден."""
    target = (name or "").strip()
    if not target:
        return ""
    for t in templates or []:
        if (t.get("name") or "").strip() == target:
            return (t.get("wiql") or "").strip()
    return ""


def fetch_and_save_tfs_work_items(
    *,
    session_dir: str,
    tfs_settings: Dict[str, Any],
    tfs_project: str,
    template_name: str,
    templates: List[Dict[str, Any]],
    fmt: str = "csv",
    max_items: int = 0,
    variable_values: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Выполняет WIQL-шаблон и сохраняет результат в attachments/.

    Значения шаблонных переменных определяются так:
      1. Сначала берутся переданные variable_values (то, что
         пользователь ввёл в UI или что сохранено в session.json).
      2. Затем — значения по умолчанию из шаблона
         (templates[i]["variables"]).
      3. Если переменная не получила значения ни там, ни там —
         возвращается ошибка с перечнем недостающих (запрос
         НЕ выполняется).

    Args:
        session_dir:      папка сессии.
        tfs_settings:     dict из ConfigManager.get_tfs_settings().
        tfs_project:      имя проекта TFS (может быть пустым).
        template_name:    имя WIQL-шаблона (обязательно).
        templates:        список шаблонов. Каждый элемент —
                          {"name": str, "wiql": str,
                           "variables": {имя: значение}}.
        fmt:              "csv" | "md" | "both".
        max_items:        максимум задач (0 — из tfs_settings["max_items"]).
        variable_values:  значения переменных {имя: значение}.
                          Приоритетнее дефолтов шаблона.

    Returns:
        {
          "ok": bool,
          "files": [<пути к сохранённым файлам>],
          "count": int,
          "kind": "flat" | "link" | "",
          "error": str,
          "variables": [<имена переменных шаблона>],
        }
    """
    import asyncio

    result: Dict[str, Any] = {
        "ok": False,
        "files": [],
        "count": 0,
        "kind": "",
        "error": "",
        "variables": [],
    }

    if not session_dir or not os.path.isdir(session_dir):
        result["error"] = f"Папка сессии не найдена: {session_dir}"
        return result

    if not (tfs_settings.get("enabled")
            and tfs_settings.get("base_url")
            and tfs_settings.get("api_key")):
        result["error"] = "TFS не настроен"
        return result

    # --- Находим шаблон ---
    template_obj: Optional[Dict[str, Any]] = None
    target_name = (template_name or "").strip()
    for tpl in templates or []:
        if (tpl.get("name") or "").strip() == target_name:
            template_obj = tpl
            break

    if template_obj is None:
        result["error"] = (
            f"WIQL-шаблон {template_name!r} не найден в настройках"
        )
        return result

    wiql = (template_obj.get("wiql") or "").strip()
    if not wiql:
        result["error"] = (
            f"У шаблона {template_name!r} пустой WIQL"
        )
        return result

    # --- Дефолты из шаблона ---
    template_defaults: Dict[str, str] = {}
    raw_defaults = template_obj.get("variables")
    if isinstance(raw_defaults, dict):
        for k, v in raw_defaults.items():
            k_str = str(k or "").strip()
            if not k_str:
                continue
            template_defaults[k_str] = str(v if v is not None else "")

    # --- Работа с переменными ---
    required_vars = extract_variables(wiql)
    result["variables"] = required_vars

    # Значения от пользователя (приоритетнее).
    user_values: Dict[str, str] = {}
    if isinstance(variable_values, dict):
        for k, v in variable_values.items():
            k_str = str(k or "").strip()
            if not k_str:
                continue
            v_str = str(v if v is not None else "").strip()
            if v_str:
                user_values[k_str] = v_str

    # Итоговые значения: user_values поверх дефолтов.
    final_values: Dict[str, str] = {}
    for name in required_vars:
        if name in user_values:
            final_values[name] = user_values[name]
        elif name in template_defaults and template_defaults[name]:
            final_values[name] = template_defaults[name]

    if required_vars:
        # Проверяем, что для всех есть значение.
        missing = [
            n for n in required_vars
            if not final_values.get(n, "").strip()
        ]
        if missing:
            result["error"] = (
                f"В шаблоне {template_name!r} есть переменные, "
                f"для которых не заданы значения (ни в записи, "
                f"ни по умолчанию в шаблоне): "
                f"{', '.join(missing)}"
            )
            return result

        wiql = substitute_variables(wiql, final_values)

        log.info(
            "TFS-выборка: в WIQL подставлены переменные %s "
            "(из пользователя: %d, из дефолтов: %d)",
            ", ".join(required_vars),
            len(user_values),
            sum(
                1 for n in required_vars
                if n not in user_values
                and n in template_defaults
            ),
        )

    if max_items <= 0:
        max_items = int(tfs_settings.get("max_items", 200))

    async def _fetch():
        async with TfsClient(
            base_url=tfs_settings["base_url"],
            pat=tfs_settings["api_key"],
            api_version=tfs_settings.get("api_version", "7.0"),
            connect_timeout=float(
                tfs_settings.get("connect_timeout", 15)
            ),
            read_timeout=float(
                tfs_settings.get("read_timeout", 60)
            ),
        ) as client:
            return await client.get_work_items_universal(
                wiql, tfs_project, max_items=max_items,
            )

    try:
        data = asyncio.run(_fetch())
    except TfsError as exc:
        result["error"] = f"Ошибка TFS: {exc}"
        log.error(
            "TFS-выборка (%s / %s): %s",
            tfs_project, template_name, exc,
        )
        return result
    except Exception as exc:
        result["error"] = f"Неожиданная ошибка: {exc}"
        log.exception(
            "TFS-выборка (%s / %s): неожиданная ошибка: %s",
            tfs_project, template_name, exc,
        )
        return result

    items = data.get("items") or []
    result["count"] = len(items)
    result["kind"] = data.get("kind") or ""

    att_dir = os.path.join(session_dir, "attachments")
    os.makedirs(att_dir, exist_ok=True)

    fmt = (fmt or "csv").lower()
    if fmt not in ("csv", "md", "both"):
        fmt = "csv"

    saved: List[str] = []

    if fmt in ("csv", "both"):
        csv_path = os.path.join(att_dir, CSV_FILENAME)
        try:
            _save_csv(items, csv_path)
            saved.append(csv_path)
        except Exception as exc:
            log.exception(
                "Не удалось сохранить CSV %s: %s", csv_path, exc
            )

    if fmt in ("md", "both"):
        md_path = os.path.join(att_dir, MD_FILENAME)
        try:
            _save_markdown(
                items, md_path,
                project=tfs_project,
                template=template_name,
            )
            saved.append(md_path)
        except Exception as exc:
            log.exception(
                "Не удалось сохранить MD %s: %s", md_path, exc
            )

    if not saved:
        result["error"] = "Не удалось сохранить ни одного файла"
        return result

    result["ok"] = True
    result["files"] = saved

    log.info(
        "TFS-выборка сохранена: kind=%s, задач=%d, файлов=%d "
        "(project=%r, template=%r, переменных=%d)",
        result["kind"], result["count"], len(saved),
        tfs_project or "—", template_name,
        len(required_vars),
    )
    return result