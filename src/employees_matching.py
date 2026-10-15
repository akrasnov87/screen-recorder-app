"""Сопоставление исполнителей поручений с сотрудниками справочника.

Модуль не зависит от Qt — чистые функции для сравнения ФИО.
Используется и в диалоге «Отправка поручений сотрудникам»
(send_to_employees_dialog.py), и в виджете сопоставления
(ui/employees_matching_widget.py).

Основная задача — fuzzy-сопоставление строк вида:
    «Иванов Иван Иванович» ↔ «Иванов И.И.» ↔ «Иванов И.»

Алгоритм:
  1. Нормализация (нижний регистр, ё→е, удаление знаков).
  2. Извлечение фамилии и инициалов.
  3. Комбинированная оценка: полное сходство строк +
     совпадение фамилии с инициалами.
"""
from __future__ import annotations

from difflib import SequenceMatcher
from typing import Tuple


# Пороги уверенности.
MATCH_CONFIDENT = 0.75   # «OK» — можно отправлять
MATCH_UNCERTAIN = 0.55   # «проверить» — жёлтая подсветка
# Ниже MATCH_UNCERTAIN — сопоставление не предлагается.


def normalize_name(raw: str) -> str:
    """
    Нормализует ФИО для сравнения:
      • нижний регистр;
      • ё → е;
      • убираем точки, запятые, дефисы, лишние пробелы;
      • переводим в единый разделитель пробел.
    """
    if not raw:
        return ""
    s = raw.lower().strip()
    s = s.replace("ё", "е")
    for ch in ".,;:!?—–-–_()[]{}\"'":
        s = s.replace(ch, " ")
    s = " ".join(s.split())
    return s


def extract_initials(name: str) -> Tuple[str, str]:
    """
    Извлекает (фамилия, инициалы).

    Пример: «Иванов Иван Иванович» → («иванов», «ии»)
            «Иванов И.И.»         → («иванов», «ии»)
            «Иванов И.»           → («иванов», «и»)
            «Иванов»              → («иванов», «»)
    """
    parts = normalize_name(name).split()
    if not parts:
        return "", ""

    surname = parts[0]
    initials = ""
    for p in parts[1:]:
        if p:
            initials += p[0]

    return surname, initials


def similarity(a: str, b: str) -> float:
    """Сходство двух строк (0..1)."""
    a_n = normalize_name(a)
    b_n = normalize_name(b)
    if not a_n or not b_n:
        return 0.0
    return SequenceMatcher(None, a_n, b_n).ratio()


def match_score(assignee: str, employee_name: str) -> float:
    """
    Итоговая оценка соответствия «исполнитель ↔ сотрудник».

    Учитывает:
      • полное сходство нормализованных строк;
      • совпадение фамилии + инициалов (более надёжно,
        чем полное сравнение, если порядок слов разный).
    """
    base = similarity(assignee, employee_name)

    a_sur, a_init = extract_initials(assignee)
    e_sur, e_init = extract_initials(employee_name)

    ini_score = 0.0
    if a_sur and e_sur:
        sur_sim = SequenceMatcher(None, a_sur, e_sur).ratio()
        if sur_sim >= 0.85:
            # Фамилии совпадают.
            if a_init and e_init:
                # Сравниваем инициалы посимвольно.
                common = sum(
                    1 for x, y in zip(a_init, e_init) if x == y
                )
                max_len = max(len(a_init), len(e_init))
                ini_score = sur_sim * (0.4 + 0.6 * common / max_len)
            else:
                ini_score = sur_sim * 0.7

    return max(base, ini_score)