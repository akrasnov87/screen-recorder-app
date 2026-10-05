"""Управление виртуальными машинами Yandex Cloud для транскрибации.

Задачи модуля:
  • Определить, доступен ли сервис транскрибации (HTTP-пинг).
  • Найти ВМ, отвечающую за транскрибацию, по имени из настроек.
  • Определить, должна ли ВМ работать сейчас по расписанию
    (schedule.cron) с учётом исключений (exceptions.txt).
  • Временно продлить расписание ВМ (например, на 30 минут),
    чтобы она гарантированно запустилась.
  • Дождаться, пока ВМ станет доступна по TCP/HTTP.
  • Восстановить исходное расписание после завершения обработки.

Формат schedule.cron (см. SCHEDULE.md):
    <дни_недели> <время_начала> <время_конца>
    Пример: 1-5 09:00 18:00

Формат exceptions.txt (см. EXCEPTIONS.md):
    <дата> <время> <действие>
    Пример: 2026-07-07 15:50 START

Изменения:
  • Модуль создан.
  • Добавлен метод restore_from_backup() — позволяет
    восстановить расписание из явно указанной резервной копии
    (используется, когда сессия управления ВМ создана в одном
    процессе — processor, а завершается в другом — main).
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from .logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Константы форматов
# ---------------------------------------------------------------------------
_SCHEDULE_LINE_RE = re.compile(
    r"^\s*([\d,\-*ALLal]+)\s+(\d{1,2}(?::\d{2})?)"
    r"\s+(\d{1,2}(?::\d{2})?)\s*(?:#.*)?$"
)
_EXCEPTION_LINE_RE = re.compile(
    r"^\s*(\S+)\s+(\S+)\s+(START|STOP)\s*(?:#.*)?$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Утилиты времени
# ---------------------------------------------------------------------------
def _time_to_minutes(hhmm: str) -> int:
    """'09:30' → 570. Если формат 'HH' — '09' → 540."""
    parts = hhmm.strip().split(":")
    h = int(parts[0])
    m = int(parts[1]) if len(parts) > 1 else 0
    return h * 60 + m


def _minutes_to_time(minutes: int) -> str:
    """570 → '09:30'. Оборачивает по модулю 24 часа."""
    minutes = minutes % (24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _now_minutes() -> int:
    now = datetime.now()
    return now.hour * 60 + now.minute


def _parse_days(spec: str) -> List[int]:
    """
    '1-5' → [1,2,3,4,5]; '1,3,5' → [1,3,5]; '*' или 'ALL' → [1..7].
    """
    spec = spec.strip().upper()
    if spec in ("*", "ALL"):
        return list(range(1, 8))

    result: List[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            try:
                start, end = int(a), int(b)
            except ValueError:
                continue
            if start <= end:
                result.extend(range(start, end + 1))
            else:
                # Оборачивание через воскресенье: 5-1 → [5,6,7,1]
                result.extend(range(start, 8))
                result.extend(range(1, end + 1))
        else:
            try:
                result.append(int(part))
            except ValueError:
                continue
    return result


def _iso_weekday_to_cron(iso_weekday: int) -> int:
    """ISO: Пн=1..Вс=7. Cron в этом проекте: Пн=1..Вс=7. Совпадает."""
    return iso_weekday


# ---------------------------------------------------------------------------
# Парсинг расписания
# ---------------------------------------------------------------------------
def parse_schedule_lines(
    content: str,
) -> List[Dict[str, Any]]:
    """
    Разбирает содержимое schedule.cron.

    Возвращает список правил:
        {"days": [1,2,3], "start_min": 540, "end_min": 1080,
         "raw": "1-5 09:00 18:00"}
    """
    rules: List[Dict[str, Any]] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _SCHEDULE_LINE_RE.match(stripped)
        if not m:
            log.debug("schedule: пропуск строки: %r", stripped)
            continue
        days_spec, start_s, end_s = m.groups()
        try:
            rules.append({
                "days": _parse_days(days_spec),
                "start_min": _time_to_minutes(start_s),
                "end_min": _time_to_minutes(end_s),
                "raw": stripped,
            })
        except Exception as exc:
            log.warning(
                "schedule: ошибка разбора %r: %s", stripped, exc
            )
    return rules


def is_vm_should_run_now(
    schedule_content: str,
    *,
    now: Optional[datetime] = None,
) -> bool:
    """
    Проверяет, должна ли ВМ работать сейчас по расписанию.

    Логика:
      • Для каждого правила проверяем, попадает ли текущий день
        недели в days.
      • Если да — проверяем, попадает ли текущее время в
        [start_min, end_min).
      • Если интервал пересекает полночь (start > end) — считаем
        его ночным: время >= start ИЛИ время < end.
      • Если хотя бы одно правило сработало — True.
      • Если ни одно — False.
    """
    now = now or datetime.now()
    weekday = _iso_weekday_to_cron(now.isoweekday())
    now_min = now.hour * 60 + now.minute

    rules = parse_schedule_lines(schedule_content)
    for rule in rules:
        if weekday not in rule["days"]:
            continue
        s = rule["start_min"]
        e = rule["end_min"]
        if s == e:
            # Интервал нулевой длины — игнорируем.
            continue
        if s < e:
            if s <= now_min < e:
                return True
        else:
            # Ночной интервал: например, 22:00-06:00
            if now_min >= s or now_min < e:
                return True
    return False


def is_vm_should_run_today(
    schedule_content: str,
    *,
    now: Optional[datetime] = None,
) -> bool:
    """Есть ли для сегодняшнего дня хоть одно правило в расписании."""
    now = now or datetime.now()
    weekday = _iso_weekday_to_cron(now.isoweekday())
    rules = parse_schedule_lines(schedule_content)
    return any(weekday in r["days"] for r in rules)


# ---------------------------------------------------------------------------
# Работа с файлами ВМ
# ---------------------------------------------------------------------------
def _find_vm_dir(root_path: str, vm_name: str) -> str:
    """Возвращает путь к папке ВМ по имени. Пусто — если не найдена."""
    if not root_path or not vm_name:
        return ""
    candidate = os.path.join(root_path, vm_name)
    if os.path.isdir(candidate):
        return candidate
    return ""


def _read_file(path: str) -> str:
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception as exc:
        log.warning("Не удалось прочитать %s: %s", path, exc)
        return ""


def _write_file_atomic(path: str, content: str) -> bool:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
        return True
    except Exception as exc:
        log.exception("Не удалось записать %s: %s", path, exc)
        return False


def _backup_file(path: str) -> str:
    """Создаёт .bak-копию файла. Возвращает путь к копии или ''."""
    if not path or not os.path.isfile(path):
        return ""
    backup = path + ".transcribe_bak"
    try:
        shutil.copy2(path, backup)
        log.info("Создана резервная копия: %s", backup)
        return backup
    except Exception as exc:
        log.warning("Не удалось создать резервную копию %s: %s", path, exc)
        return ""


def _restore_file(path: str, backup: str) -> bool:
    if not backup or not os.path.isfile(backup):
        return False
    try:
        shutil.copy2(backup, path)
        os.remove(backup)
        log.info("Восстановлен файл из резервной копии: %s", path)
        return True
    except Exception as exc:
        log.warning("Не удалось восстановить %s: %s", path, exc)
        return False


# ---------------------------------------------------------------------------
# Изменение расписания
# ---------------------------------------------------------------------------
def extend_schedule_for_now(
    schedule_content: str,
    extension_minutes: int = 30,
    *,
    now: Optional[datetime] = None,
) -> Tuple[str, str]:
    """
    Продлевает расписание так, чтобы ВМ гарантированно работала
    сейчас + extension_minutes.

    Возвращает (новое_содержимое, описание_изменения).

    Логика:
      1. Если для сегодняшнего дня уже есть правило, покрывающее
         текущее время + extension_minutes — ничего не меняем.
      2. Иначе добавляем новую строку:
         <сегодняшний_день> <сейчас> <сейчас + extension_minutes>
      3. Если новая строка пересекается с существующей для того же
         дня — расширяем существующую (меняем end_min).
      4. Если end_min переходит за полночь — добавляем два правила:
         одно на сегодня [now_min, 24*60), второе на завтра
         [0, end_min % (24*60)).
    """
    now = now or datetime.now()
    weekday = _iso_weekday_to_cron(now.isoweekday())
    now_min = now.hour * 60 + now.minute
    end_min = now_min + extension_minutes

    rules = parse_schedule_lines(schedule_content)

    # Ищем правило, которое покрывает [now_min, end_min)
    # для случая, когда end_min не переходит за полночь.
    if end_min <= 24 * 60:
        for rule in rules:
            if weekday not in rule["days"]:
                continue
            s, e = rule["start_min"], rule["end_min"]
            if s == e:
                continue
            if s < e:
                # Дневной интервал
                if s <= now_min and end_min <= e:
                    return schedule_content, (
                        f"Расписание уже покрывает текущее время "
                        f"(правило {rule['raw']})"
                    )
            else:
                # Ночной интервал (правило уже пересекает полночь)
                # Проверяем, что now_min и end_min оба попадают
                # в [s, 24*60) ∪ [0, e).
                def _in_night(x: int, s_: int, e_: int) -> bool:
                    return x >= s_ or x < e_

                if _in_night(now_min, s, e) and _in_night(end_min, s, e):
                    return schedule_content, (
                        f"Расписание уже покрывает текущее время "
                        f"(правило {rule['raw']})"
                    )
    else:
        # end_min переходит за полночь. Проверяем, что есть
        # правило, покрывающее и [now_min, 24*60), и [0, end_min%1440).
        end_next_day = end_min % (24 * 60)
        next_weekday = _iso_weekday_to_cron(
            (now + timedelta(days=1)).isoweekday()
        )

        covered_today = False
        covered_tomorrow = False
        for rule in rules:
            s, e = rule["start_min"], rule["end_min"]
            if s == e:
                continue
            if s < e:
                # Дневной: [s, e)
                if weekday in rule["days"]:
                    if s <= now_min < e:
                        covered_today = True
                    # Хвост до полуночи от today
                    if s <= now_min and e >= 24 * 60:
                        covered_today = True
                if next_weekday in rule["days"]:
                    if s <= 0 and e >= end_next_day:
                        covered_tomorrow = True
            else:
                # Ночной: [s, 24*60) ∪ [0, e)
                if weekday in rule["days"] and now_min >= s:
                    covered_today = True
                if next_weekday in rule["days"] and end_next_day < e:
                    covered_tomorrow = True

        if covered_today and covered_tomorrow:
            return schedule_content, (
                "Расписание уже покрывает текущее время и "
                "переход через полночь"
            )

    # Ищем правило для сегодняшнего дня, которое заканчивается
    # до end_min — его можно расширить (только если end_min
    # не переходит за полночь).
    if end_min <= 24 * 60:
        for rule in rules:
            if weekday not in rule["days"]:
                continue
            s, e = rule["start_min"], rule["end_min"]
            if s < e and e > now_min and e < end_min:
                old_line = rule["raw"]
                new_line = (
                    f"{_format_days(rule['days'])} "
                    f"{_minutes_to_time(s)} {_minutes_to_time(end_min)}"
                )
                new_content = schedule_content.replace(
                    old_line, new_line, 1
                )
                return new_content, (
                    f"Расширено правило «{old_line}» → «{new_line}»"
                )

    # Иначе добавляем новую строку (или две, если переход
    # через полночь).
    if end_min <= 24 * 60:
        new_line = (
            f"{weekday} {_minutes_to_time(now_min)} "
            f"{_minutes_to_time(end_min)}"
        )
        new_content = (
            schedule_content.rstrip() + "\n" + new_line + "\n"
        )
        return new_content, f"Добавлено правило «{new_line}»"

    # Переход через полночь: два правила.
    end_next_day = end_min % (24 * 60)
    next_weekday = _iso_weekday_to_cron(
        (now + timedelta(days=1)).isoweekday()
    )
    line_today = (
        f"{weekday} {_minutes_to_time(now_min)} 23:59"
    )
    line_tomorrow = (
        f"{next_weekday} 00:00 {_minutes_to_time(end_next_day)}"
    )
    new_content = (
        schedule_content.rstrip()
        + "\n" + line_today + "\n" + line_tomorrow + "\n"
    )
    return new_content, (
        f"Добавлены правила «{line_today}» и «{line_tomorrow}» "
        f"(переход через полночь)"
    )


def _format_days(days: List[int]) -> str:
    """[1,2,3,4,5] → '1-5'; [1,3,5] → '1,3,5'."""
    days = sorted(set(days))
    if not days:
        return "*"
    if days == list(range(1, 8)):
        return "*"

    # Ищем непрерывные диапазоны
    parts: List[str] = []
    start = days[0]
    prev = days[0]
    for d in days[1:]:
        if d == prev + 1:
            prev = d
        else:
            parts.append(
                f"{start}-{prev}" if start != prev else str(start)
            )
            start = prev = d
    parts.append(
        f"{start}-{prev}" if start != prev else str(start)
    )
    return ",".join(parts)


# ---------------------------------------------------------------------------
# Исключения (проверка приоритета)
# ---------------------------------------------------------------------------
def check_exception_now(
    exceptions_content: str,
    *,
    now: Optional[datetime] = None,
) -> Optional[str]:
    """
    Проверяет, есть ли активное исключение для текущего момента.

    Возвращает "START", "STOP" или None.
    Исключение имеет высший приоритет над расписанием.
    """
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    weekday = _iso_weekday_to_cron(now.isoweekday())
    now_min = now.hour * 60 + now.minute
    now_str = now.strftime("%H:%M")

    active: Optional[str] = None
    for line in exceptions_content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _EXCEPTION_LINE_RE.match(stripped)
        if not m:
            continue
        date_spec, time_spec, action = m.groups()
        action = action.upper()

        # --- Проверка даты ---
        if date_spec == "*":
            date_ok = True
        elif date_spec.upper().startswith("DAY"):
            # DAY1-5 или DAY3
            days_spec = date_spec[3:]
            days = _parse_days(days_spec)
            date_ok = weekday in days
        else:
            date_ok = (date_spec == today)

        if not date_ok:
            continue

        # --- Проверка времени ---
        if time_spec == "*":
            time_ok = True
        elif "-" in time_spec:
            try:
                s_s, e_s = time_spec.split("-", 1)
                s = _time_to_minutes(s_s)
                e = _time_to_minutes(e_s)
            except Exception:
                continue
            if s < e:
                time_ok = s <= now_min < e
            else:
                time_ok = now_min >= s or now_min < e
        else:
            time_ok = (time_spec == now_str)

        if time_ok:
            # Последнее подходящее правило выигрывает.
            active = action

    return active


# ---------------------------------------------------------------------------
# Менеджер ВМ
# ---------------------------------------------------------------------------
class YandexVMManager:
    """
    Управляет ВМ Yandex Cloud через локальные файлы расписания.

    НЕ работает с API Yandex Cloud напрямую — только с файлами
    schedule.cron / exceptions.txt в папке ВМ. Само включение ВМ
    выполняет внешний vm_manager.py (см. SCHEDULE.md), который
    вызывается по cron.

    Задача этого класса — временно продлить расписание, чтобы ВМ
    гарантированно запустилась, и дождаться её доступности по
    HTTP-пингу сервиса транскрибации.
    """

    def __init__(self, settings: Dict[str, Any]) -> None:
        self.root_path = str(
            settings.get("root_path", "") or ""
        ).strip()
        self.vm_name = str(
            settings.get("transcribe_vm_name", "") or ""
        ).strip()
        self.start_timeout = int(
            settings.get("transcribe_vm_start_timeout", 600)
        )
        self.check_interval = int(
            settings.get("transcribe_vm_check_interval", 10)
        )
        self.schedule_extension_minutes = int(
            settings.get("transcribe_vm_schedule_extension_minutes", 30)
        )

        # Резервные копии файлов для восстановления.
        self._schedule_backup: str = ""
        self._schedule_path: str = ""
        self._original_schedule: str = ""
        self._was_modified: bool = False

        log.debug(
            "YandexVMManager: root=%r, vm=%r, timeout=%d, "
            "interval=%d, extension=%d",
            self.root_path, self.vm_name,
            self.start_timeout, self.check_interval,
            self.schedule_extension_minutes,
        )

    # ------------------------------------------------------------------
    # Проверки
    # ------------------------------------------------------------------
    def is_configured(self) -> bool:
        """Настроен ли менеджер (есть root_path и vm_name)."""
        return bool(self.root_path and self.vm_name)

    def is_vm_dir_exists(self) -> bool:
        return bool(
            self.root_path
            and self.vm_name
            and os.path.isdir(
                os.path.join(self.root_path, self.vm_name)
            )
        )

    def _vm_dir(self) -> str:
        return _find_vm_dir(self.root_path, self.vm_name)

    def _schedule_file(self) -> str:
        vm_dir = self._vm_dir()
        if not vm_dir:
            return ""
        return os.path.join(vm_dir, "schedule.cron")

    def _exceptions_file(self) -> str:
        vm_dir = self._vm_dir()
        if not vm_dir:
            return ""
        return os.path.join(vm_dir, "exceptions.txt")

    # ------------------------------------------------------------------
    # Диагностика
    # ------------------------------------------------------------------
    def diagnose(self, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        """
        Возвращает словарь с диагностикой состояния ВМ:
            {
              "configured": bool,
              "vm_dir_exists": bool,
              "vm_dir": str,
              "schedule_exists": bool,
              "schedule_should_run": bool,
              "exception": "START" | "STOP" | None,
              "should_run_now": bool,   # с учётом исключений
              "reason": str,
            }
        """
        now = now or datetime.now()
        result: Dict[str, Any] = {
            "configured": self.is_configured(),
            "vm_dir_exists": False,
            "vm_dir": "",
            "schedule_exists": False,
            "schedule_should_run": False,
            "exception": None,
            "should_run_now": False,
            "reason": "",
        }

        if not self.is_configured():
            result["reason"] = (
                "ВМ для транскрибации не задана в настройках "
                "(yandex_vm.transcribe_vm_name)"
            )
            return result

        vm_dir = self._vm_dir()
        if not vm_dir:
            result["reason"] = (
                f"Папка ВМ «{self.vm_name}» не найдена в "
                f"{self.root_path}"
            )
            return result

        result["vm_dir_exists"] = True
        result["vm_dir"] = vm_dir

        schedule_path = os.path.join(vm_dir, "schedule.cron")
        schedule_content = _read_file(schedule_path)
        if schedule_content:
            result["schedule_exists"] = True

        result["schedule_should_run"] = is_vm_should_run_now(
            schedule_content, now=now
        )

        exceptions_path = os.path.join(vm_dir, "exceptions.txt")
        exceptions_content = _read_file(exceptions_path)
        exception = check_exception_now(exceptions_content, now=now)
        result["exception"] = exception

        if exception == "START":
            result["should_run_now"] = True
            result["reason"] = "Активно исключение START"
        elif exception == "STOP":
            result["should_run_now"] = False
            result["reason"] = "Активно исключение STOP"
        else:
            result["should_run_now"] = result["schedule_should_run"]
            if result["schedule_should_run"]:
                result["reason"] = "По расписанию ВМ должна работать"
            else:
                result["reason"] = "По расписанию ВМ должна быть выключена"

        return result

    # ------------------------------------------------------------------
    # Изменение расписания
    # ------------------------------------------------------------------
    def extend_schedule_for_now(self) -> Dict[str, Any]:
        """
        Продлевает расписание ВМ так, чтобы она работала сейчас.

        Возвращает:
            {
              "ok": bool,
              "modified": bool,
              "message": str,
              "schedule_path": str,
            }
        """
        if not self.is_configured():
            return {
                "ok": False,
                "modified": False,
                "message": "ВМ не настроена",
                "schedule_path": "",
            }

        schedule_path = self._schedule_file()
        if not schedule_path:
            return {
                "ok": False,
                "modified": False,
                "message": "Файл расписания не найден",
                "schedule_path": "",
            }

        original = _read_file(schedule_path)

        new_content, description = extend_schedule_for_now(
            original,
            extension_minutes=self.schedule_extension_minutes,
        )

        if new_content == original:
            log.info(
                "YandexVMManager: расписание не требует изменений: %s",
                description,
            )
            return {
                "ok": True,
                "modified": False,
                "message": description,
                "schedule_path": schedule_path,
            }

        # Создаём резервную копию
        backup = _backup_file(schedule_path)
        if not backup:
            return {
                "ok": False,
                "modified": False,
                "message": "Не удалось создать резервную копию",
                "schedule_path": schedule_path,
            }

        if not _write_file_atomic(schedule_path, new_content):
            return {
                "ok": False,
                "modified": False,
                "message": "Не удалось записать расписание",
                "schedule_path": schedule_path,
            }

        self._schedule_backup = backup
        self._schedule_path = schedule_path
        self._original_schedule = original
        self._was_modified = True

        log.info(
            "YandexVMManager: расписание продлено: %s (%s)",
            schedule_path, description,
        )
        return {
            "ok": True,
            "modified": True,
            "message": description,
            "schedule_path": schedule_path,
        }

    def restore_schedule(self) -> Dict[str, Any]:
        """
        Восстанавливает исходное расписание ВМ.

        Возвращает:
            {"ok": bool, "message": str}
        """
        if not self._was_modified:
            return {
                "ok": True,
                "message": "Расписание не изменялось — нечего восстанавливать",
            }

        if not self._schedule_backup or not os.path.isfile(
            self._schedule_backup
        ):
            return {
                "ok": False,
                "message": "Резервная копия расписания не найдена",
            }

        ok = _restore_file(self._schedule_path, self._schedule_backup)
        if ok:
            self._was_modified = False
            self._schedule_backup = ""
            self._original_schedule = ""
            return {
                "ok": True,
                "message": "Расписание восстановлено",
            }
        return {
            "ok": False,
            "message": "Не удалось восстановить расписание",
        }

    def restore_from_backup(
        self,
        schedule_path: str,
        backup_path: str,
    ) -> Dict[str, Any]:
        """
        Восстанавливает расписание из указанной резервной копии.

        Используется, когда сессия управления ВМ была создана в
        одном процессе (processor), а завершается в другом (main).
        Позволяет не хранить приватные поля менеджера между
        процессами/сессиями.

        Args:
            schedule_path: путь к schedule.cron.
            backup_path:   путь к .transcribe_bak.

        Returns:
            {"ok": bool, "message": str}
        """
        if not schedule_path or not os.path.isfile(schedule_path):
            return {
                "ok": False,
                "message": (
                    f"Файл расписания не найден: {schedule_path}"
                ),
            }
        if not backup_path or not os.path.isfile(backup_path):
            return {
                "ok": False,
                "message": (
                    f"Резервная копия не найдена: {backup_path}"
                ),
            }
        ok = _restore_file(schedule_path, backup_path)
        if ok:
            return {
                "ok": True,
                "message": "Расписание восстановлено",
            }
        return {
            "ok": False,
            "message": "Не удалось восстановить расписание",
        }

    def get_modified_schedule_info(
        self,
        schedule_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Информация о текущем расписании.

        Args:
            schedule_path: явный путь к schedule.cron. Если задан —
                используется он, а не self._schedule_path. Нужно,
                когда метод вызывается на новом объекте менеджера
                (например, из main.py), у которого нет сохранённых
                приватных полей.

        Returns:
            {
              "modified": bool,
              "schedule_path": str,
              "current_content": str,
              "next_stop_time": str,  # "HH:MM" или ""
            }
        """
        path = schedule_path or self._schedule_path
        result: Dict[str, Any] = {
            "modified": self._was_modified,
            "schedule_path": path,
            "current_content": "",
            "next_stop_time": "",
        }
        if not path or not os.path.isfile(path):
            return result

        content = _read_file(path)
        result["current_content"] = content

        # Ищем ближайшее время окончания для сегодняшнего дня.
        now = datetime.now()
        weekday = _iso_weekday_to_cron(now.isoweekday())
        now_min = now.hour * 60 + now.minute

        rules = parse_schedule_lines(content)
        end_candidates: List[int] = []
        for rule in rules:
            if weekday not in rule["days"]:
                continue
            s, e = rule["start_min"], rule["end_min"]
            if s < e and s <= now_min < e:
                end_candidates.append(e)
            elif s > e and (now_min >= s or now_min < e):
                end_candidates.append(e)

        if end_candidates:
            result["next_stop_time"] = _minutes_to_time(
                min(end_candidates)
            )
        return result


# ---------------------------------------------------------------------------
# Проверка доступности сервиса транскрибации
# ---------------------------------------------------------------------------
async def ping_transcribe_service(
    base_url: str,
    *,
    timeout: float = 5.0,
) -> bool:
    """
    Быстрый HTTP-пинг сервиса транскрибации.

    Пытается достучаться до /health или /api/auth/verify.
    Возвращает True, если сервис ответил (любой HTTP < 500).
    """
    if not base_url:
        return False

    base_url = base_url.rstrip("/")
    candidates = [
        f"{base_url}/health",
        f"{base_url}/api/auth/verify",
        base_url,
    ]

    timeout_obj = aiohttp.ClientTimeout(total=timeout)
    try:
        async with aiohttp.ClientSession(
            timeout=timeout_obj
        ) as session:
            for url in candidates:
                try:
                    async with session.get(url) as resp:
                        # Любой ответ < 500 считаем «сервис жив».
                        if resp.status < 500:
                            log.debug(
                                "ping_transcribe: %s → HTTP %d",
                                url, resp.status,
                            )
                            return True
                except Exception as exc:
                    log.debug(
                        "ping_transcribe: %s не ответил: %s", url, exc
                    )
                    continue
    except Exception as exc:
        log.debug("ping_transcribe: ошибка сессии: %s", exc)

    return False


async def wait_for_transcribe_service(
    base_url: str,
    *,
    max_wait: float = 600.0,
    check_interval: float = 10.0,
    progress_cb=None,
    cancel_event: Optional[asyncio.Event] = None,
) -> bool:
    """
    Ждёт, пока сервис транскрибации станет доступен.

    Args:
        base_url:        URL сервиса.
        max_wait:        максимум секунд ожидания.
        check_interval:  пауза между проверками.
        progress_cb:     колбэк (elapsed_sec, max_wait).
        cancel_event:    если выставлен — прерываем ожидание.

    Returns:
        True, если сервис стал доступен; False — таймаут/отмена.
    """
    import time as _time

    t0 = _time.monotonic()
    attempt = 0

    while True:
        if cancel_event is not None and cancel_event.is_set():
            log.info("wait_for_transcribe: отменено пользователем")
            return False

        elapsed = _time.monotonic() - t0
        if elapsed > max_wait:
            log.warning(
                "wait_for_transcribe: таймаут %.0f с", max_wait
            )
            return False

        attempt += 1
        if progress_cb is not None:
            try:
                progress_cb(elapsed, max_wait)
            except Exception:
                pass

        ok = await ping_transcribe_service(base_url, timeout=5.0)
        if ok:
            log.info(
                "wait_for_transcribe: сервис доступен "
                "(попытка %d, %.1f с)",
                attempt, elapsed,
            )
            return True

        log.debug(
            "wait_for_transcribe: попытка %d, %.1f с — недоступен",
            attempt, elapsed,
        )

        # Прерываемое ожидание
        if cancel_event is not None:
            try:
                await asyncio.wait_for(
                    cancel_event.wait(), timeout=check_interval
                )
                continue
            except asyncio.TimeoutError:
                continue
        else:
            await asyncio.sleep(check_interval)