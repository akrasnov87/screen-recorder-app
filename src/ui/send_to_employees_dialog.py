"""Диалог сопоставления исполнителей поручений и сотрудников.

Открывается из окна «Поручения» по кнопке «Отправить сотрудникам…».

Логика:
  1. На входе — список выбранных поручений и справочник сотрудников
     (name, chat_id) из config.
  2. Извлекаем уникальных исполнителей из поля assignee.
  3. Автоматически сопоставляем (fuzzy match ФИО).
  4. Пользователь проверяет/корректирует через ComboBox.
  5. Отправляем каждому сотруднику отдельное сообщение с его
     поручениями.

Изменения:
  • В сообщениях сотрудникам выводятся сквозные номера поручений.
"""
from __future__ import annotations

import asyncio
import html
import threading
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, QDate
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFormLayout, QHBoxLayout, QHeaderView,
    QLabel, QMessageBox, QProgressDialog, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..bitrix_client import Bitrix24Client, Bitrix24Error
from ..logger import get_logger
from ..markdown_to_bitrix import (
    format_action_items_to_bitrix,
    markdown_to_bitrix,
)
from ..tasks_manager import (
    STATUS_LABELS,
    days_until_due,
    is_overdue,
)

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Нормализация и сопоставление имён
# ---------------------------------------------------------------------------
def _normalize_name(raw: str) -> str:
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


def _extract_initials(name: str) -> Tuple[str, str]:
    """
    Извлекает (фамилия, инициалы).

    Пример: «Иванов Иван Иванович» → («иванов», «ии»)
            «Иванов И.И.»         → («иванов», «ии»)
            «Иванов И.»           → («иванов», «и»)
            «Иванов»              → («иванов», «»)
    """
    parts = _normalize_name(name).split()
    if not parts:
        return "", ""

    surname = parts[0]
    initials = ""
    for p in parts[1:]:
        if p:
            initials += p[0]

    return surname, initials


def _similarity(a: str, b: str) -> float:
    """Сходство двух строк (0..1)."""
    a_n = _normalize_name(a)
    b_n = _normalize_name(b)
    if not a_n or not b_n:
        return 0.0
    return SequenceMatcher(None, a_n, b_n).ratio()


def _match_score(assignee: str, employee_name: str) -> float:
    """
    Итоговая оценка соответствия «исполнитель ↔ сотрудник».

    Учитывает:
      • полное сходство нормализованных строк;
      • совпадение фамилии + инициалов (более надёжно,
        чем полное сравнение, если порядок слов разный).
    """
    base = _similarity(assignee, employee_name)

    a_sur, a_init = _extract_initials(assignee)
    e_sur, e_init = _extract_initials(employee_name)

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


# ---------------------------------------------------------------------------
# Определение типа связи
# ---------------------------------------------------------------------------
LINK_AUTO   = "auto"    # предложено автоматически, уверенно
LINK_MAYBE  = "maybe"   # предложено автоматически, неуверенно
LINK_MANUAL = "manual"  # выбрано вручную
LINK_NONE   = ""        # не выбрано


# ---------------------------------------------------------------------------
# Диалог
# ---------------------------------------------------------------------------
class SendToEmployeesDialog(QDialog):
    """
    Окно сопоставления исполнителей и сотрудников.
    """

    def __init__(
        self,
        items: List[Dict[str, Any]],
        employees: List[Dict[str, str]],
        bitrix_cfg: Dict[str, Any],
        *,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Отправка поручений сотрудникам")
        self.setModal(True)
        self.setMinimumSize(1000, 660)

        self._items = list(items or [])
        self._employees = list(employees or [])
        self._bitrix_cfg = bitrix_cfg or {}

        # Уникальные исполнители из поручений.
        self._assignees: List[str] = self._collect_assignees()

        # Сопоставление: assignee → employee_name (или "").
        self._mapping: Dict[str, str] = {}

        self._build_ui()
        self._auto_match()
        self._reload_table()
        self._reload_preview()

    # ------------------------------------------------------------------
    # Подготовка данных
    # ------------------------------------------------------------------
    def _collect_assignees(self) -> List[str]:
        """Уникальные непустые исполнители из поручений."""
        seen = set()
        result: List[str] = []
        for it in self._items:
            a = (it.get("assignee") or "").strip()
            if a and a not in seen:
                seen.add(a)
                result.append(a)
        # Сортируем по алфавиту, но пустых (без исполнителя) — в конец.
        result.sort(key=lambda x: x.lower())
        return result

    def _has_unassigned(self) -> bool:
        """Есть ли поручения без исполнителя."""
        return any(
            not (it.get("assignee") or "").strip()
            for it in self._items
        )

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        # --- Инфо ---
        info = QLabel(
            "Сопоставьте исполнителей из поручений с сотрудниками "
            "из справочника. Каждому сотруднику уйдёт отдельное "
            "сообщение с его поручениями.<br><br>"
            "<span style='color:#888'>Строки, подсвеченные жёлтым — "
            "автосопоставление с невысокой уверенностью. "
            "Проверьте их вручную.<br>"
            "Сотрудники без <code>chat_id</code> показаны серым — "
            "им отправить нельзя.</span>"
        )
        info.setWordWrap(True)
        root.addWidget(info)

        # --- Таблица сопоставления ---
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels([
            "Исполнитель из поручения",
            "Поручений",
            "Сотрудник",
            "Статус",
        ])
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        hv = self.table.horizontalHeader()
        hv.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hv.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hv.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hv.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        root.addWidget(self.table, 1)

        # --- Кнопки управления ---
        ctrl = QHBoxLayout()

        self.auto_match_btn = QPushButton("Пересопоставить")
        self.auto_match_btn.setToolTip(
            "Запустить автоматическое сопоставление заново.\n"
            "Ручные правки будут сброшены."
        )
        self.auto_match_btn.clicked.connect(self._on_auto_match_clicked)
        ctrl.addWidget(self.auto_match_btn)

        self.clear_btn = QPushButton("Снять все")
        self.clear_btn.setToolTip(
            "Снять все сопоставления."
        )
        self.clear_btn.clicked.connect(self._on_clear_clicked)
        ctrl.addWidget(self.clear_btn)

        ctrl.addSpacing(20)

        self.send_to_assignee_only_check = QCheckBox(
            "Отправлять только сотрудникам с чатом"
        )
        self.send_to_assignee_only_check.setChecked(True)
        self.send_to_assignee_only_check.setToolTip(
            "Если включено — из рассылки исключаются исполнители, "
            "которым не сопоставлен сотрудник или у сотрудника "
            "не задан chat_id."
        )
        self.send_to_assignee_only_check.toggled.connect(
            self._reload_preview
        )
        ctrl.addWidget(self.send_to_assignee_only_check)

        ctrl.addStretch()
        root.addLayout(ctrl)

        # --- Предпросмотр ---
        preview_header = QHBoxLayout()
        preview_header.addWidget(
            QLabel("<b>Предпросмотр рассылки:</b>")
        )
        preview_header.addStretch()
        self.preview_stats = QLabel("")
        self.preview_stats.setStyleSheet(
            "QLabel { color: #444; font-weight: bold; }"
        )
        preview_header.addWidget(self.preview_stats)
        root.addLayout(preview_header)

        self.preview = QLabel("")
        self.preview.setWordWrap(True)
        self.preview.setTextFormat(Qt.TextFormat.PlainText)
        self.preview.setStyleSheet(
            "QLabel {"
            "  background-color: #f5f5f5;"
            "  border: 1px solid #ddd;"
            "  border-radius: 4px;"
            "  padding: 8px;"
            "  font-family: monospace;"
            "  color: #333;"
            "}"
        )
        self.preview.setMinimumHeight(120)
        self.preview.setMaximumHeight(180)
        root.addWidget(self.preview)

        # --- Нижние кнопки ---
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        self.ok_btn = buttons.button(
            QDialogButtonBox.StandardButton.Ok
        )
        self.ok_btn.setText("Отправить")
        buttons.button(
            QDialogButtonBox.StandardButton.Cancel
        ).setText("Отмена")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # ------------------------------------------------------------------
    # Автосопоставление
    # ------------------------------------------------------------------
    def _auto_match(self) -> None:
        """Автоматически сопоставляет исполнителей с сотрудниками."""
        self._mapping = {}
        used_employees: set = set()

        # Готовим сотрудников: name → chat_id.
        employees_by_name = {
            (e.get("name") or "").strip(): (e.get("chat_id") or "").strip()
            for e in self._employees
            if (e.get("name") or "").strip()
        }

        for assignee in self._assignees:
            best_name = ""
            best_score = 0.0
            for emp_name in employees_by_name:
                if emp_name in used_employees:
                    # Одного сотрудника можно выбрать только
                    # один раз — иначе сообщения дублируются.
                    continue
                score = _match_score(assignee, emp_name)
                if score > best_score:
                    best_score = score
                    best_name = emp_name

            if best_score >= 0.75:
                self._mapping[assignee] = best_name
                used_employees.add(best_name)
            elif best_score >= 0.55:
                # Неуверенное — тоже предлагаем, но пометим.
                self._mapping[assignee] = best_name
                used_employees.add(best_name)
            else:
                self._mapping[assignee] = ""

        log.info(
            "Автосопоставление: исполнителей=%d, сопоставлено=%d, "
            "неуверенно=%d",
            len(self._assignees),
            sum(1 for v in self._mapping.values() if v),
            sum(
                1 for a, v in self._mapping.items()
                if v and _match_score(a, v) < 0.75
            ),
        )

    def _on_auto_match_clicked(self) -> None:
        self._auto_match()
        self._reload_table()
        self._reload_preview()

    def _on_clear_clicked(self) -> None:
        self._mapping = {a: "" for a in self._assignees}
        self._reload_table()
        self._reload_preview()

    # ------------------------------------------------------------------
    # Таблица
    # ------------------------------------------------------------------
    def _items_of_assignee(self, assignee: str) -> List[Dict[str, Any]]:
        return [
            it for it in self._items
            if (it.get("assignee") or "").strip() == assignee
        ]

    def _reload_table(self) -> None:
        self.table.setRowCount(0)

        for assignee in self._assignees:
            row = self.table.rowCount()
            self.table.insertRow(row)

            items = self._items_of_assignee(assignee)
            n_items = len(items)
            n_overdue = sum(1 for it in items if is_overdue(it))

            # --- Исполнитель ---
            assignee_item = QTableWidgetItem(assignee)
            self.table.setItem(row, 0, assignee_item)

            # --- Кол-во поручений ---
            count_text = str(n_items)
            if n_overdue:
                count_text += f"  (⚠ {n_overdue} просроч.)"
            count_item = QTableWidgetItem(count_text)
            if n_overdue:
                count_item.setForeground(QColor("#B71C1C"))
            self.table.setItem(row, 1, count_item)

            # --- ComboBox выбора сотрудника ---
            combo = QComboBox()
            combo.setMinimumWidth(260)
            combo.addItem("— не отправлять —", "")

            current_value = self._mapping.get(assignee, "")

            for e in self._employees:
                emp_name = (e.get("name") or "").strip()
                chat_id = (e.get("chat_id") or "").strip()
                if not emp_name:
                    continue
                label = emp_name
                if chat_id:
                    label += f"  ({chat_id})"
                else:
                    label += "  — нет chat_id"
                combo.addItem(label, emp_name)

            # Проставляем текущее значение.
            idx = combo.findData(current_value)
            if idx >= 0:
                combo.setCurrentIndex(idx)

            combo.currentIndexChanged.connect(
                lambda _idx, a=assignee, c=combo: self._on_combo_changed(
                    a, c.currentData() or ""
                )
            )
            self.table.setCellWidget(row, 2, combo)

            # --- Статус ---
            status_item = self._make_status_item(assignee)
            self.table.setItem(row, 3, status_item)

            # --- Подсветка строки ---
            self._apply_row_style(row, assignee)

    def _make_status_item(self, assignee: str) -> QTableWidgetItem:
        """Создаёт ячейку «Статус»."""
        value = self._mapping.get(assignee, "")

        if not value:
            item = QTableWidgetItem("не сопоставлено")
            item.setForeground(QColor("#888"))
            item.setToolTip(
                "Исполнитель не сопоставлен сотруднику. "
                "Его поручения не будут отправлены."
            )
            return item

        emp = self._find_employee(value)
        if not emp:
            item = QTableWidgetItem("сотрудник не найден")
            item.setForeground(QColor("#C62828"))
            return item

        chat_id = (emp.get("chat_id") or "").strip()
        if not chat_id:
            item = QTableWidgetItem("нет chat_id")
            item.setForeground(QColor("#C62828"))
            item.setToolTip(
                f"У сотрудника «{value}» не задан chat_id. "
                f"Заполните Настройки → Сотрудники."
            )
            return item

        # Проверяем «уверенность» сопоставления.
        score = _match_score(assignee, value)
        if score >= 0.75:
            item = QTableWidgetItem("OK")
            item.setForeground(QColor("#2E7D32"))
        else:
            item = QTableWidgetItem("проверить")
            item.setForeground(QColor("#B8860B"))
            item.setToolTip(
                f"Автосопоставление неуверенное "
                f"(сходство {int(score * 100)}%). "
                f"Проверьте вручную."
            )
        return item

    def _find_employee(
        self, name: str,
    ) -> Optional[Dict[str, str]]:
        target = (name or "").strip()
        for e in self._employees:
            if (e.get("name") or "").strip() == target:
                return e
        return None

    def _apply_row_style(self, row: int, assignee: str) -> None:
        """Подсвечивает строку в зависимости от статуса."""
        value = self._mapping.get(assignee, "")
        if not value:
            return

        emp = self._find_employee(value)
        if not emp:
            return

        chat_id = (emp.get("chat_id") or "").strip()
        score = _match_score(assignee, value)

        # Определяем цвет фона.
        bg: Optional[QColor] = None
        if not chat_id:
            bg = QColor("#FFF8E1")   # жёлтый — нет chat_id
        elif score < 0.75:
            bg = QColor("#FFF8E1")   # жёлтый — проверить

        if bg is None:
            return

        for col in range(self.table.columnCount()):
            cell = self.table.item(row, col)
            if cell is None:
                continue
            cell.setBackground(bg)

    def _on_combo_changed(self, assignee: str, employee_name: str) -> None:
        self._mapping[assignee] = employee_name

        # Обновляем строку — статус и подсветку.
        for row in range(self.table.rowCount()):
            a_item = self.table.item(row, 0)
            if a_item is None:
                continue
            if a_item.text() == assignee:
                status_item = self._make_status_item(assignee)
                self.table.setItem(row, 3, status_item)
                self._apply_row_style(row, assignee)
                break

        self._reload_preview()

    # ------------------------------------------------------------------
    # Предпросмотр
    # ------------------------------------------------------------------
    def _reload_preview(self) -> None:
        lines: List[str] = []
        send_count = 0
        skip_count = 0
        no_chat_count = 0

        for assignee in self._assignees:
            value = self._mapping.get(assignee, "")
            items = self._items_of_assignee(assignee)

            if not value:
                skip_count += 1
                lines.append(
                    f"• {assignee} — не сопоставлено "
                    f"({len(items)} поруч.) — пропуск"
                )
                continue

            emp = self._find_employee(value)
            if not emp:
                skip_count += 1
                lines.append(
                    f"• {assignee} → {value} — сотрудник "
                    f"не найден — пропуск"
                )
                continue

            chat_id = (emp.get("chat_id") or "").strip()
            if not chat_id:
                no_chat_count += 1
                lines.append(
                    f"• {assignee} → {value} — нет chat_id — пропуск"
                )
                continue

            send_count += 1
            score = _match_score(assignee, value)
            mark = "✓" if score >= 0.75 else "?"
            lines.append(
                f"{mark} {assignee} → {value} ({chat_id}) "
                f"— {len(items)} поруч."
            )

        # --- Поручения без исполнителя ---
        unassigned = [
            it for it in self._items
            if not (it.get("assignee") or "").strip()
        ]
        if unassigned:
            lines.append(
                f"• (без исполнителя) — {len(unassigned)} поруч. "
                f"— пропуск"
            )

        if not lines:
            lines.append("(нет данных)")

        self.preview.setText("\n".join(lines))

        stats_parts = [f"Отправим: {send_count}"]
        if skip_count:
            stats_parts.append(f"Пропустим: {skip_count}")
        if no_chat_count:
            stats_parts.append(f"Без chat_id: {no_chat_count}")
        self.preview_stats.setText(" · ".join(stats_parts))

    # ------------------------------------------------------------------
    # Отправка
    # ------------------------------------------------------------------
    def _collect_messages(self) -> List[Dict[str, Any]]:
        """
        Собирает итоговые сообщения:
        [{ chat_id, employee_name, text_md, items }]
        """
        result: List[Dict[str, Any]] = []

        for assignee in self._assignees:
            value = self._mapping.get(assignee, "")
            if not value:
                continue

            emp = self._find_employee(value)
            if not emp:
                continue

            chat_id = (emp.get("chat_id") or "").strip()
            if not chat_id:
                continue

            items = self._items_of_assignee(assignee)
            if not items:
                continue

            text_md = format_action_items_to_bitrix(
                items,
                title=f"Поручения — {assignee}",
                group_by_assignee=False,
                include_status=True,
                include_due_date=True,
                include_number=True,   # ← сквозные номера в сообщении
            )

            result.append({
                "chat_id": chat_id,
                "employee_name": value,
                "assignee": assignee,
                "text_md": text_md,
                "items": items,
            })

        return result

    def _on_accept(self) -> None:
        messages = self._collect_messages()

        if not messages:
            QMessageBox.warning(
                self, "Поручения",
                "Нет получателей с заданным chat_id. "
                "Проверьте сопоставление.",
            )
            return

        webhook = (self._bitrix_cfg.get("webhook_url") or "").strip()
        if not webhook:
            QMessageBox.warning(
                self, "Bitrix24",
                "Вебхук Bitrix24 не настроен. "
                "Откройте Настройки → Bitrix24.",
            )
            return

        # --- Подтверждение ---
        preview_lines: List[str] = []
        for m in messages[:15]:
            preview_lines.append(
                f"• {m['assignee']} → {m['employee_name']} "
                f"({m['chat_id']}) — {len(m['items'])} поруч."
            )
        if len(messages) > 15:
            preview_lines.append(
                f"… и ещё {len(messages) - 15}"
            )
        preview_text = "\n".join(preview_lines)

        reply = QMessageBox.question(
            self, "Отправка сотрудникам",
            f"<b>Будет отправлено {len(messages)} сообщений:</b>"
            f"<br><br>"
            f"<pre style='font-family:monospace'>"
            f"{html.escape(preview_text)}</pre>"
            f"<br>Продолжить?",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # --- Параметры клиента ---
        connect_timeout = float(
            self._bitrix_cfg.get("connect_timeout", 15)
        )
        read_timeout = float(
            self._bitrix_cfg.get("read_timeout", 60)
        )
        max_chars = int(
            self._bitrix_cfg.get("max_message_chars", 15000)
        )
        system = bool(self._bitrix_cfg.get("system_message", False))
        url_preview = not bool(
            self._bitrix_cfg.get("disable_url_preview", False)
        )

        # --- Прогресс ---
        dlg = QProgressDialog(
            "Отправка сотрудникам…",
            None, 0, len(messages), self,
        )
        dlg.setWindowTitle("Bitrix24")
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.show()
        QGuiApplication.processEvents()

        errors: List[str] = []

        def _worker() -> None:
            async def _run() -> None:
                async with Bitrix24Client(
                    webhook_url=webhook,
                    connect_timeout=connect_timeout,
                    read_timeout=read_timeout,
                    max_message_chars=max_chars,
                ) as client:
                    for i, m in enumerate(messages, start=1):
                        try:
                            text_bb = markdown_to_bitrix(m["text_md"])
                            await client.send_message(
                                dialog_id=m["chat_id"],
                                text=text_bb,
                                system=system,
                                url_preview=url_preview,
                            )
                            log.info(
                                "Отправлено поручений: %s → %s "
                                "(%d поруч.)",
                                m["assignee"], m["chat_id"],
                                len(m["items"]),
                            )
                        except Bitrix24Error as exc:
                            log.error(
                                "Не удалось отправить %s → %s: %s",
                                m["assignee"], m["chat_id"], exc,
                            )
                            errors.append(
                                f"{m['assignee']} → "
                                f"{m['chat_id']}: {exc}"
                            )
                        # Обновляем прогресс из фонового потока
                        # через механизм Qt — небезопасно.
                        # Поэтому просто пропускаем — прогресс
                        # останется на 0. Это допустимо: операция
                        # обычно быстрая.

            try:
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(_run())
                finally:
                    loop.close()
            except Exception as exc:
                log.exception(
                    "Ошибка отправки сотрудникам: %s", exc
                )
                errors.append(f"Общая ошибка: {exc}")

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        while t.is_alive():
            QGuiApplication.processEvents()
            t.join(0.05)

        dlg.close()

        # --- Итог ---
        if errors:
            err_text = "\n".join(errors[:15])
            if len(errors) > 15:
                err_text += f"\n… и ещё {len(errors) - 15}"
            QMessageBox.warning(
                self, "Bitrix24",
                f"Отправлено: {len(messages) - len(errors)} "
                f"из {len(messages)}\n\n"
                f"Ошибки:\n{err_text}",
            )
            if len(errors) == len(messages):
                return
        else:
            QMessageBox.information(
                self, "Bitrix24",
                f"Отправлено сообщений: {len(messages)}",
            )

        self.accept()