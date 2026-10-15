"""Виджет сопоставления исполнителей поручений с сотрудниками.

Переиспользуется:
  • в SendToEmployeesDialog (standalone-режим);
  • на вкладке «Сотрудникам» в SendToBitrixDialog
    (для скрам-записей).

Виджет НЕ отправляет сообщения. Он:
  • строит таблицу сопоставления исполнителей и сотрудников;
  • хранит self._mapping: assignee → employee_name;
  • возвращает через collect_messages() список готовых
    сообщений [{chat_id, employee_name, assignee, text_md, items}];
  • через validation_errors() сообщает о проблемах.

Логика сопоставления (fuzzy по ФИО) вынесена в
src/employees_matching.py — она общая для виджета и для
самостоятельного диалога SendToEmployeesDialog.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QHBoxLayout,
    QHeaderView, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..logger import get_logger
from ..markdown_to_bitrix import format_action_items_to_bitrix
from ..tasks_manager import (
    STATUS_LABELS,
    is_overdue,
)
from ..employees_matching import (
    MATCH_CONFIDENT,
    MATCH_UNCERTAIN,
    match_score,
)

log = get_logger(__name__)


class EmployeesMatchingWidget(QWidget):
    """
    Виджет сопоставления исполнителей поручений с сотрудниками.

    Работает с уже готовым списком поручений (items) и справочником
    сотрудников (employees). Ничего не читает с диска.
    """

    def __init__(
        self,
        *,
        items: List[Dict[str, Any]],
        employees: List[Dict[str, str]],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)

        self._items = list(items or [])
        self._employees = list(employees or [])

        # Уникальные непустые исполнители из поручений.
        self._assignees: List[str] = self._collect_assignees()

        # Сопоставление: assignee → employee_name (или "").
        self._mapping: Dict[str, str] = {}

        self._build_ui()
        self._auto_match()
        self._reload_table()
        self._reload_preview()

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------
    def collect_messages(self) -> List[Dict[str, Any]]:
        """
        Собирает итоговые сообщения для рассылки сотрудникам.

        Формат:
            [
              {
                "chat_id": str,
                "employee_name": str,
                "assignee": str,
                "text_md": str,
                "items": [...],
              }
            ]

        Возвращает только тех, у кого есть chat_id и хотя бы одно
        поручение. Все остальные (не сопоставленные, без chat_id)
        в список не попадают.
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
                include_number=True,
            )

            result.append({
                "chat_id": chat_id,
                "employee_name": value,
                "assignee": assignee,
                "text_md": text_md,
                "items": items,
            })

        return result

    def has_messages(self) -> bool:
        """Есть ли хотя бы одно готовое сообщение к отправке."""
        return bool(self.collect_messages())

    def validation_errors(self) -> List[str]:
        """
        Возвращает список предупреждений для диалога подтверждения.

        Пустой список — ошибок нет.
        """
        errors: List[str] = []

        if not self._items:
            errors.append("Нет поручений для отправки.")
            return errors

        if not self._employees:
            errors.append(
                "Справочник сотрудников пуст — не с кем сопоставлять."
            )
            return errors

        unmatched = [
            a for a in self._assignees
            if not self._mapping.get(a)
        ]
        if unmatched:
            errors.append(
                f"Без сопоставления: {len(unmatched)} "
                f"исполнителей ({', '.join(unmatched[:5])}"
                + ("…" if len(unmatched) > 5 else "")
                + ")."
            )

        no_chat = 0
        for assignee in self._assignees:
            value = self._mapping.get(assignee, "")
            if not value:
                continue
            emp = self._find_employee(value)
            if emp and not (emp.get("chat_id") or "").strip():
                no_chat += 1
        if no_chat:
            errors.append(
                f"У {no_chat} сопоставленных сотрудников не задан "
                f"chat_id — им отправить нельзя."
            )

        if not self.collect_messages():
            errors.append(
                "Не сформировано ни одного сообщения — "
                "нечего будет отправлять."
            )

        return errors

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
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
        hv.setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        hv.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hv.setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents
        )
        root.addWidget(self.table, 1)

        # --- Кнопки управления ---
        ctrl = QHBoxLayout()

        self.auto_match_btn = QPushButton("Пересопоставить")
        self.auto_match_btn.setToolTip(
            "Запустить автоматическое сопоставление заново.\n"
            "Ручные правки будут сброшены."
        )
        self.auto_match_btn.clicked.connect(
            self._on_auto_match_clicked
        )
        ctrl.addWidget(self.auto_match_btn)

        self.clear_btn = QPushButton("Снять все")
        self.clear_btn.setToolTip("Снять все сопоставления.")
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
        result.sort(key=lambda x: x.lower())
        return result

    # ------------------------------------------------------------------
    # Автосопоставление
    # ------------------------------------------------------------------
    def _auto_match(self) -> None:
        """Автоматически сопоставляет исполнителей с сотрудниками."""
        self._mapping = {}
        used_employees: set = set()

        employees_by_name = {
            (e.get("name") or "").strip(): (
                e.get("chat_id") or ""
            ).strip()
            for e in self._employees
            if (e.get("name") or "").strip()
        }

        for assignee in self._assignees:
            best_name = ""
            best_score = 0.0
            for emp_name in employees_by_name:
                if emp_name in used_employees:
                    # Одного сотрудника — только один раз.
                    continue
                score = match_score(assignee, emp_name)
                if score > best_score:
                    best_score = score
                    best_name = emp_name

            if best_score >= MATCH_UNCERTAIN:
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
                if v and match_score(a, v) < MATCH_CONFIDENT
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
    def _items_of_assignee(
        self, assignee: str
    ) -> List[Dict[str, Any]]:
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
            self.table.setItem(
                row, 0, QTableWidgetItem(assignee)
            )

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

            idx = combo.findData(current_value)
            if idx >= 0:
                combo.setCurrentIndex(idx)

            combo.currentIndexChanged.connect(
                lambda _idx, a=assignee, c=combo:
                    self._on_combo_changed(
                        a, c.currentData() or ""
                    )
            )
            self.table.setCellWidget(row, 2, combo)

            # --- Статус ---
            status_item = self._make_status_item(assignee)
            self.table.setItem(row, 3, status_item)

            # --- Подсветка строки ---
            self._apply_row_style(row, assignee)

    def _make_status_item(
        self, assignee: str
    ) -> QTableWidgetItem:
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

        score = match_score(assignee, value)
        if score >= MATCH_CONFIDENT:
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
        self, name: str
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
        score = match_score(assignee, value)

        bg: Optional[QColor] = None
        if not chat_id:
            bg = QColor("#FFF8E1")   # нет chat_id
        elif score < MATCH_CONFIDENT:
            bg = QColor("#FFF8E1")   # проверить

        if bg is None:
            return

        for col in range(self.table.columnCount()):
            cell = self.table.item(row, col)
            if cell is None:
                continue
            cell.setBackground(bg)

    def _on_combo_changed(
        self, assignee: str, employee_name: str
    ) -> None:
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
            score = match_score(assignee, value)
            mark = "✓" if score >= MATCH_CONFIDENT else "?"
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