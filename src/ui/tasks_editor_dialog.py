"""Редактор поручений одной записи.

Открывается из окна «Записи» (меню «Поручения»). Умеет:
  • показывать список поручений с фильтром по статусу/исполнителю;
  • создавать, редактировать, удалять элементы;
  • импортировать/экспортировать JSON;
  • сохранять изменения в action_items.json внутри папки сессии.

Изменения:
  • Добавлена колонка «№» — сквозной числовой номер поручения.
  • При создании/дублировании номера выдаются из общего
    счётчика (action_items_counter.json в корне sessions/).
  • _selected_item ищет элемент по ID в колонке «ID» (последней).
  • НОВОЕ: добавлено поле «№ поручения» для поиска по номеру.
    Поддерживаются: одно число, список через запятую, диапазон.
"""
from __future__ import annotations

import html
import os
import re
from datetime import date
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import (
    QColor, QDesktopServices, QKeySequence, QShortcut,
)
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QApplication, QAbstractItemView, QComboBox, QCheckBox, QDateEdit,
    QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)
from ..tasks_json_parser import (
    ParseResult,
    parse_action_items_json,
)
from ..file_readers import read_json_file
from ..logger import get_logger
from ..tasks_manager import (
    ACTION_ITEMS_FILE,
    STATUS_COLORS,
    STATUS_LABELS,
    STATUS_ORDER,
    days_until_due,
    delete_item,
    due_date_priority,
    export_items,
    import_items,
    is_overdue,
    load_action_items,
    make_item,
    save_action_items,
    update_item,
)
from ..utils import safe_local_path
from .tooltips import attach_tooltip, make_info_icon, with_info

import uuid
from datetime import datetime

def _new_uuid_hex() -> str:
    return uuid.uuid4().hex

def _iso_now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Диалог редактирования одного поручения
# ---------------------------------------------------------------------------
class _ItemEditDialog(QDialog):
    """Модальный диалог создания/редактирования одного поручения."""

    def __init__(
        self,
        item: Optional[Dict[str, Any]] = None,
        *,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(
            "Поручение" if item is None else "Редактирование поручения"
        )
        self.setModal(True)
        self.setMinimumWidth(560)

        self._initial = item or {}

        form = QFormLayout(self)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.text_input = QPlainTextEdit()
        self.text_input.setPlaceholderText(
            "Что нужно сделать (например: «Подготовить макет "
            "по проекту X»)."
        )
        self.text_input.setFixedHeight(80)
        form.addRow("Текст поручения:", self.text_input)

        self.assignee_input = QLineEdit()
        self.assignee_input.setPlaceholderText(
            "ФИО или ник исполнителя (например: Иванов И.И.)"
        )
        form.addRow("Исполнитель:", self.assignee_input)

        self.status_combo = QComboBox()
        for s in STATUS_ORDER:
            self.status_combo.addItem(STATUS_LABELS[s], s)
        form.addRow("Статус:", self.status_combo)

        self.due_date_check = QDateEdit()
        self.due_date_check.setCalendarPopup(True)
        self.due_date_check.setDisplayFormat("yyyy-MM-dd")
        self.due_date_check.setSpecialValueText("—")
        self.due_date_check.setDate(QDate.currentDate())
        form.addRow("Срок:", self.due_date_check)

        self.due_date_enabled = QPushButton("Убрать срок")
        self.due_date_enabled.setCheckable(False)
        self.due_date_enabled.clicked.connect(
            lambda: self.due_date_check.setDate(QDate.currentDate())
        )

        self.comment_input = QPlainTextEdit()
        self.comment_input.setPlaceholderText(
            "Комментарий (необязательно)."
        )
        self.comment_input.setFixedHeight(60)
        form.addRow("Комментарий:", self.comment_input)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.button(
            QDialogButtonBox.StandardButton.Ok
        ).setText("Сохранить")
        buttons.button(
            QDialogButtonBox.StandardButton.Cancel
        ).setText("Отмена")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

        self._apply_initial()

    def _apply_initial(self) -> None:
        it = self._initial
        self.text_input.setPlainText(it.get("text") or "")
        self.assignee_input.setText(it.get("assignee") or "")
        status = it.get("status") or "created"
        idx = self.status_combo.findData(status)
        if idx >= 0:
            self.status_combo.setCurrentIndex(idx)
        due = (it.get("due_date") or "").strip()
        if due:
            try:
                y, m, d = due.split("-")
                self.due_date_check.setDate(
                    QDate(int(y), int(m), int(d))
                )
            except Exception:
                pass
        self.comment_input.setPlainText(it.get("comment") or "")

    def _on_accept(self) -> None:
        if not self.text_input.toPlainText().strip():
            QMessageBox.warning(
                self, "Поручение",
                "Текст поручения не может быть пустым.",
            )
            return
        self.accept()

    def result_data(self) -> Dict[str, Any]:
        qd = self.due_date_check.date()
        return {
            "text": self.text_input.toPlainText().strip(),
            "assignee": self.assignee_input.text().strip(),
            "status": self.status_combo.currentData() or "created",
            "due_date": f"{qd.year():04d}-{qd.month():02d}-{qd.day():02d}",
            "comment": self.comment_input.toPlainText().strip(),
        }


# ---------------------------------------------------------------------------
# Основной редактор
# ---------------------------------------------------------------------------
class TasksEditorDialog(QDialog):
    """
    Редактор поручений записи.

    Работает с файлом action_items.json внутри session_dir.
    """

    def __init__(
        self,
        session_dir: str,
        *,
        session_name: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(
            f"Поручения — "
            f"{session_name or os.path.basename(session_dir)}"
        )
        self.setModal(True)
        self.setMinimumSize(1120, 640)

        self._session_dir = session_dir
        self._session_name = session_name
        self._data: Dict[str, Any] = {}

        self._build_ui()
        self._reload()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        # --- Инфо-плашка ---
        info_row = QHBoxLayout()
        info = QLabel(
            "Поручения сохраняются в файл "
            "<code>action_items.json</code> внутри папки записи "
            "и передаются на сервер синхронизации вместе с "
            "остальными артефактами.<br><br>"
            "Каждому поручению присваивается сквозной числовой "
            "номер (№) — он отображается в таблицах и "
            "передаётся в чат Bitrix24."
        )
        info.setWordWrap(True)
        info.setStyleSheet("QLabel { color: #666; }")
        info_row.addWidget(info, 1)
        icon = make_info_icon("tasks_intro")
        if icon is not None:
            info_row.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(info_row)

        # --- Фильтры ---
        filters = QHBoxLayout()
        filters.addWidget(QLabel("Статус:"))

        self.status_filter = QComboBox()
        self.status_filter.addItem("— все —", "")
        for s in STATUS_ORDER:
            self.status_filter.addItem(STATUS_LABELS[s], s)
        self.status_filter.currentIndexChanged.connect(
            self._reload_table
        )
        filters.addWidget(self.status_filter)

        filters.addSpacing(12)
        filters.addWidget(QLabel("Исполнитель:"))
        self.assignee_filter = QComboBox()
        self.assignee_filter.addItem("— все —", "")
        self.assignee_filter.setMinimumWidth(200)
        self.assignee_filter.currentIndexChanged.connect(
            self._reload_table
        )
        filters.addWidget(self.assignee_filter)

        # --- НОВОЕ: поиск по номеру поручения ---
        filters.addSpacing(12)
        filters.addWidget(QLabel("№ поручения:"))
        self.number_search_input = QLineEdit()
        self.number_search_input.setPlaceholderText(
            "42, 43 или 40-50"
        )
        self.number_search_input.setMaximumWidth(160)
        self.number_search_input.setClearButtonEnabled(True)
        self.number_search_input.setToolTip(
            "Поиск по номеру поручения.\n\n"
            "Поддерживается:\n"
            "  • одно число: 42;\n"
            "  • несколько через запятую: 42, 43, 44;\n"
            "  • диапазон: 40-50;\n"
            "  • смешанное: 1, 5-7, 10.\n\n"
            "Пробелы игнорируются. Пустое поле — фильтр выключен."
        )
        self.number_search_input.textChanged.connect(
            self._reload_table
        )
        filters.addWidget(self.number_search_input)

        filters.addSpacing(12)
        filters.addWidget(QLabel("Поиск:"))
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(
            "Подстрока в тексте поручения…"
        )
        self.search_input.textChanged.connect(self._reload_table)
        filters.addWidget(self.search_input, 1)

        root.addLayout(filters)

        # --- Дополнительные фильтры ---
        filters2 = QHBoxLayout()

        self.overdue_only_check = QCheckBox(
            "Только просроченные"
        )
        self.overdue_only_check.setToolTip(
            "Показать только поручения, у которых срок уже истёк "
            "(due_date < сегодня) и статус не «Выполнен»."
        )
        self.overdue_only_check.toggled.connect(self._reload_table)
        filters2.addWidget(self.overdue_only_check)

        filters2.addSpacing(12)

        self.due_filter_enabled = QCheckBox("Срок с:")
        self.due_filter_enabled.setToolTip(
            "Включить фильтр по диапазону дат срока."
        )
        filters2.addWidget(self.due_filter_enabled)

        self.due_from = QDateEdit()
        self.due_from.setCalendarPopup(True)
        self.due_from.setDisplayFormat("yyyy-MM-dd")
        self.due_from.setDate(QDate.currentDate().addMonths(-1))
        self.due_from.dateChanged.connect(self._reload_table)
        filters2.addWidget(self.due_from)

        filters2.addWidget(QLabel("по:"))

        self.due_to = QDateEdit()
        self.due_to.setCalendarPopup(True)
        self.due_to.setDisplayFormat("yyyy-MM-dd")
        self.due_to.setDate(QDate.currentDate().addMonths(1))
        self.due_to.dateChanged.connect(self._reload_table)
        filters2.addWidget(self.due_to)

        self.due_filter_enabled.toggled.connect(
            self._on_due_filter_toggled
        )

        filters2.addSpacing(12)

        self.sort_by_due_check = QCheckBox(
            "Сортировать по сроку"
        )
        self.sort_by_due_check.setToolTip(
            "Просроченные — сверху, затем «сегодня», затем "
            "будущие, затем без срока."
        )
        self.sort_by_due_check.setChecked(True)
        self.sort_by_due_check.toggled.connect(self._reload_table)
        filters2.addWidget(self.sort_by_due_check)

        filters2.addStretch()

        root.addLayout(filters2)

        # --- Таблица ---
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels([
            "№", "Статус", "Текст", "Исполнитель",
            "Срок", "Комментарий", "ID",
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
        self.table.itemDoubleClicked.connect(
            lambda _it: self._edit_selected()
        )
        hv = self.table.horizontalHeader()
        hv.setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        hv.setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        hv.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hv.setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents
        )
        hv.setSectionResizeMode(
            4, QHeaderView.ResizeMode.ResizeToContents
        )
        hv.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        hv.setSectionResizeMode(
            6, QHeaderView.ResizeMode.ResizeToContents
        )
        root.addWidget(self.table, 1)

        # --- Кнопки действий ---
        actions = QHBoxLayout()

        self.add_btn = QPushButton("Создать")
        self.add_btn.clicked.connect(self._add_item)
        actions.addWidget(self.add_btn)

        self.edit_btn = QPushButton("Редактировать")
        self.edit_btn.clicked.connect(self._edit_selected)
        actions.addWidget(self.edit_btn)

        self.delete_btn = QPushButton("Удалить")
        self.delete_btn.clicked.connect(self._delete_selected)
        actions.addWidget(self.delete_btn)

        self.duplicate_btn = QPushButton("Дублировать")
        self.duplicate_btn.setToolTip(
            "Создать копию выбранного поручения (с новым номером)."
        )
        self.duplicate_btn.clicked.connect(self._duplicate_selected)
        actions.addWidget(self.duplicate_btn)

        actions.addSpacing(20)

        self.import_btn = QPushButton("Импорт JSON…")
        self.import_btn.setToolTip(
            "Загрузить поручения из внешнего JSON-файла "
            "(добавит к существующим)."
        )
        self.import_btn.clicked.connect(self._import_json)
        actions.addWidget(self.import_btn)

        self.export_btn = QPushButton("Экспорт JSON…")
        self.export_btn.setToolTip(
            "Сохранить все поручения в отдельный JSON-файл."
        )
        self.export_btn.clicked.connect(self._export_json)
        actions.addWidget(self.export_btn)

        self.paste_btn = QPushButton("Вставить JSON…")
        self.paste_btn.setToolTip(
            "Вставить поручения из буфера обмена.\n\n"
            "Поддерживаются:\n"
            "  • полный JSON action_items.json;\n"
            "  • массив поручений;\n"
            "  • один объект-поручение;\n"
            "  • русские синонимы полей (текст/исполнитель/"
            "срок/статус/номер).\n\n"
            "Горячая клавиша: Ctrl+Shift+V"
        )
        self.paste_btn.clicked.connect(self._paste_json)
        actions.addWidget(self.paste_btn)

        actions.addStretch()

        self.stats_label = QLabel("")
        self.stats_label.setStyleSheet(
            "QLabel { color: #444; font-weight: bold; }"
        )
        actions.addWidget(self.stats_label)

        root.addLayout(actions)

        # --- Нижние кнопки ---
        bottom = QHBoxLayout()
        bottom.addStretch()

        self.open_folder_btn = QPushButton("Открыть папку записи")
        self.open_folder_btn.clicked.connect(self._open_folder)
        bottom.addWidget(self.open_folder_btn)

        self.save_close_btn = QPushButton("Сохранить и закрыть")
        self.save_close_btn.clicked.connect(self.accept)
        bottom.addWidget(self.save_close_btn)

        self.cancel_btn = QPushButton("Отмена")
        self.cancel_btn.clicked.connect(self.reject)
        bottom.addWidget(self.cancel_btn)

        root.addLayout(bottom)

        # --- Горячие клавиши ---
        QShortcut(
            QKeySequence("Ctrl+Shift+V"), self
        ).activated.connect(self._paste_json)

        # --- Первичная инициализация состояния фильтров ---
        self._on_due_filter_toggled(False)

    def _on_due_filter_toggled(self, enabled: bool) -> None:
        if not hasattr(self, "due_from"):
            return
        self.due_from.setEnabled(enabled)
        if hasattr(self, "due_to"):
            self.due_to.setEnabled(enabled)
        if hasattr(self, "sort_by_due_check"):
            self._reload_table()

    # ------------------------------------------------------------------
    # Разбор строки поиска по номеру
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_number_query(raw: str) -> Optional[set]:
        """
        Разбирает строку поиска по номеру поручения.

        Поддерживает:
          • пусто → None (фильтр выключен);
          • «42» → {42};
          • «42, 43, 44» → {42, 43, 44};
          • «40-50» → {40, 41, ..., 50};
          • смешанное: «1, 5-7, 10» → {1, 5, 6, 7, 10}.

        Невалидные куски игнорируются.

        Returns:
            set[int] или None.
        """
        s = (raw or "").strip()
        if not s:
            return None

        result: set = set()

        for chunk in re.split(r"[,;]+", s):
            chunk = chunk.strip().replace(" ", "")
            if not chunk:
                continue

            # Диапазон вида «40-50».
            m = re.match(r"^(\d+)\s*-\s*(\d+)$", chunk)
            if m:
                try:
                    a = int(m.group(1))
                    b = int(m.group(2))
                except ValueError:
                    continue
                if a > b:
                    a, b = b, a
                # Защита от огромных диапазонов.
                if b - a > 10000:
                    b = a + 10000
                result.update(range(a, b + 1))
                continue

            # Одиночное число.
            if chunk.isdigit():
                try:
                    result.add(int(chunk))
                except ValueError:
                    continue

        return result if result else None

    # ------------------------------------------------------------------
    # Данные
    # ------------------------------------------------------------------
    def _reload(self) -> None:
        """Перечитывает файл поручений с диска и обновляет UI."""
        self._data = load_action_items(self._session_dir)
        if self._session_name:
            self._data["session_name"] = self._session_name
        self._rebuild_assignees()
        self._reload_table()

    def _rebuild_assignees(self) -> None:
        """Перестраивает список исполнителей в фильтре."""
        current = self.assignee_filter.currentData() or ""
        names = sorted({
            (it.get("assignee") or "").strip()
            for it in self._data.get("items", [])
            if (it.get("assignee") or "").strip()
        })

        self.assignee_filter.blockSignals(True)
        self.assignee_filter.clear()
        self.assignee_filter.addItem("— все —", "")
        for n in names:
            self.assignee_filter.addItem(n, n)
        idx = self.assignee_filter.findData(current)
        if idx >= 0:
            self.assignee_filter.setCurrentIndex(idx)
        self.assignee_filter.blockSignals(False)

    def _filtered_items(self) -> List[Dict[str, Any]]:
        # Защита от преждевременных вызовов из _build_ui.
        if not hasattr(self, "sort_by_due_check"):
            return []

        status_filter = self.status_filter.currentData() or ""
        assignee_filter = self.assignee_filter.currentData() or ""
        query = (self.search_input.text() or "").strip().lower()

        # --- Поиск по номеру ---
        number_query_raw = (
            self.number_search_input.text().strip()
            if hasattr(self, "number_search_input") else ""
        )
        numbers_filter = self._parse_number_query(number_query_raw)

        overdue_only = self.overdue_only_check.isChecked()
        due_enabled = self.due_filter_enabled.isChecked()
        due_from = self.due_from.date()
        due_to = self.due_to.date()

        result: List[Dict[str, Any]] = []
        for it in self._data.get("items", []):
            if status_filter and it.get("status") != status_filter:
                continue
            if assignee_filter and it.get("assignee") != assignee_filter:
                continue

            # --- Фильтр по номеру ---
            if numbers_filter is not None:
                try:
                    n = int(it.get("number") or 0)
                except (TypeError, ValueError):
                    n = 0
                if n not in numbers_filter:
                    continue

            if query:
                haystack = (
                    (it.get("text") or "") + " " +
                    (it.get("comment") or "") + " " +
                    (it.get("assignee") or "") + " " +
                    str(it.get("number") or "")
                ).lower()
                if query not in haystack:
                    continue

            # --- Только просроченные ---
            if overdue_only and not is_overdue(it):
                continue

            # --- Фильтр по диапазону дат срока ---
            if due_enabled:
                raw_due = (it.get("due_date") or "").strip()
                if not raw_due:
                    continue
                try:
                    y, m, d = raw_due.split("-")
                    item_due = QDate(int(y), int(m), int(d))
                except Exception:
                    continue
                if item_due < due_from or item_due > due_to:
                    continue

            result.append(it)

        # --- Сортировка ---
        if self.sort_by_due_check.isChecked():
            result.sort(key=lambda x: (
                due_date_priority(x),
                (x.get("due_date") or "9999-99-99"),
                (x.get("assignee") or ""),
            ))

        return result

    def _reload_table(self) -> None:
        items = self._filtered_items()
        self.table.setRowCount(0)

        # Цвета для просроченных строк.
        overdue_bg = QColor("#FFEBEE")
        overdue_fg = QColor("#B71C1C")
        today_bg = QColor("#FFF8E1")
        today_fg = QColor("#B8860B")

        today = QDate.currentDate()

        for it in items:
            row = self.table.rowCount()
            self.table.insertRow(row)

            status = it.get("status") or "created"
            label = STATUS_LABELS.get(status, status)
            status_color = STATUS_COLORS.get(status, "#000000")

            overdue = is_overdue(it)

            # Дата срока.
            raw_due = (it.get("due_date") or "").strip()
            due_is_today = False
            if raw_due:
                try:
                    y, m, d = raw_due.split("-")
                    due_qdate = QDate(int(y), int(m), int(d))
                    due_is_today = (due_qdate == today)
                except Exception:
                    due_is_today = False

            # --- № ---
            number = it.get("number") or 0
            try:
                number = int(number)
            except (TypeError, ValueError):
                number = 0
            number_text = str(number) if number > 0 else "—"
            number_item = QTableWidgetItem(number_text)
            number_item.setForeground(QColor("#444"))
            number_item.setTextAlignment(
                Qt.AlignmentFlag.AlignRight
                | Qt.AlignmentFlag.AlignVCenter
            )
            self.table.setItem(row, 0, number_item)

            # --- Статус ---
            status_item = QTableWidgetItem(label)
            status_item.setForeground(QColor(status_color))
            self.table.setItem(row, 1, status_item)

            # --- Текст ---
            text_item = QTableWidgetItem(it.get("text") or "")
            self.table.setItem(row, 2, text_item)

            # --- Исполнитель ---
            assignee_item = QTableWidgetItem(
                it.get("assignee") or "—"
            )
            self.table.setItem(row, 3, assignee_item)

            # --- Срок ---
            due_text = raw_due or "—"
            if overdue:
                days = days_until_due(it) or 0
                if days == 0:
                    due_text = f"{raw_due}  (сегодня)"
                elif days > 0:
                    due_text = f"{raw_due}  (+{days} дн.)"
                else:
                    due_text = (
                        f"{raw_due}  (просрочено на "
                        f"{abs(days)} дн.)"
                    )
            elif due_is_today:
                due_text = f"{raw_due}  (сегодня)"

            due_item = QTableWidgetItem(due_text)
            if overdue:
                due_item.setForeground(overdue_fg)
                due_item.setToolTip(
                    f"Просрочено на "
                    f"{abs(days_until_due(it) or 0)} дн."
                )
            elif due_is_today:
                due_item.setForeground(today_fg)
            self.table.setItem(row, 4, due_item)

            # --- Комментарий ---
            self.table.setItem(
                row, 5, QTableWidgetItem(it.get("comment") or "")
            )

            # --- ID ---
            id_item = QTableWidgetItem(it.get("id") or "")
            id_item.setForeground(QColor("#888"))
            self.table.setItem(row, 6, id_item)

            # --- Подсветка строки ---
            if overdue:
                for col in range(self.table.columnCount()):
                    cell = self.table.item(row, col)
                    if cell is None:
                        continue
                    cell.setBackground(overdue_bg)
                    if col != 1:
                        cell.setForeground(overdue_fg)
                # Дополнительный визуальный маркер — «⚠»
                first_cell = self.table.item(row, 2)
                if first_cell is not None:
                    first_cell.setText(
                        "⚠  " + (first_cell.text() or "")
                    )
            elif due_is_today and status != "done":
                for col in range(self.table.columnCount()):
                    cell = self.table.item(row, col)
                    if cell is None:
                        continue
                    cell.setBackground(today_bg)

        # --- Итоги ---
        all_items = self._data.get("items", [])
        overdue_count = sum(
            1 for x in all_items if is_overdue(x)
        )
        total = len(all_items)

        stats = (
            f"Всего: {total} · показано: {len(items)}"
        )
        if overdue_count:
            stats += f" · просрочено: {overdue_count}"
        self.stats_label.setText(stats)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    def _selected_item(self) -> Optional[Dict[str, Any]]:
        row = self.table.currentRow()
        if row < 0:
            return None
        id_item = self.table.item(row, 6)
        if id_item is None:
            return None
        item_id = id_item.text()
        for it in self._data.get("items", []):
            if it.get("id") == item_id:
                return it
        return None

    def _add_item(self) -> None:
        dlg = _ItemEditDialog(parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        patch = dlg.result_data()
        item = make_item(
            patch["text"],
            session_dir=self._session_dir,   # ← сквозной номер
            assignee=patch["assignee"],
            status=patch["status"],
            due_date=patch["due_date"],
            context=self._data.get("session_name") or "",
            source="manual",
            comment=patch["comment"],
        )
        self._data.setdefault("items", []).append(item)
        self._save()
        self._rebuild_assignees()
        self._reload_table()
        log.info(
            "Создано поручение №%s: %s",
            item.get("number"), (item.get("text") or "")[:60],
        )

    def _edit_selected(self) -> None:
        item = self._selected_item()
        if not item:
            QMessageBox.information(
                self, "Поручения", "Выберите поручение."
            )
            return

        dlg = _ItemEditDialog(item=item, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        patch = dlg.result_data()
        updated = update_item(
            self._session_dir, item["id"], patch
        )
        if updated is None:
            QMessageBox.warning(
                self, "Поручения",
                "Не удалось сохранить изменения.",
            )
            return
        self._rebuild_assignees()
        self._reload_table()

    def _delete_selected(self) -> None:
        item = self._selected_item()
        if not item:
            QMessageBox.information(
                self, "Поручения", "Выберите поручение."
            )
            return
        number = item.get("number") or 0
        num_prefix = f"№{number} " if number else ""
        reply = QMessageBox.question(
            self, "Удаление",
            f"Удалить поручение {num_prefix}?\n\n"
            f"«{(item.get('text') or '')[:120]}»",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        delete_item(self._session_dir, item["id"])
        self._reload()

    def _duplicate_selected(self) -> None:
        item = self._selected_item()
        if not item:
            return
        copy = dict(item)
        copy.pop("id", None)
        copy.pop("number", None)  # копия получит новый номер
        copy["text"] = (copy.get("text") or "") + " (копия)"
        new_item = make_item(
            copy["text"],
            session_dir=self._session_dir,
            assignee=copy.get("assignee") or "",
            status=copy.get("status") or "created",
            due_date=copy.get("due_date") or "",
            context=copy.get("context") or "",
            source="manual",
            comment=copy.get("comment") or "",
        )
        self._data.setdefault("items", []).append(new_item)
        self._save()
        self._reload_table()

    # ------------------------------------------------------------------
    # Импорт / экспорт
    # ------------------------------------------------------------------
    def _import_json(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Импорт поручений", os.path.expanduser("~"),
            "JSON (*.json);;Все файлы (*)",
        )
        if not path:
            return
        path = safe_local_path(path)

        reply = QMessageBox.question(
            self, "Импорт поручений",
            "Как импортировать?\n\n"
            "• <b>Да</b> — добавить к существующим.\n"
            "• <b>Нет</b> — заменить полностью.",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes,
        )
        if reply == QMessageBox.StandardButton.Cancel:
            return

        added = import_items(
            self._session_dir, path,
            merge=(reply == QMessageBox.StandardButton.Yes),
            session_name=self._data.get("session_name") or "",
        )
        QMessageBox.information(
            self, "Импорт поручений",
            f"Импортировано: {added}",
        )
        self._reload()

    def _export_json(self) -> None:
        default_path = os.path.join(
            os.path.expanduser("~"),
            "action_items_export.json",
        )
        path, _ = QFileDialog.getSaveFileName(
            self, "Экспорт поручений",
            default_path,
            "JSON (*.json);;;Все файлы (*)",
        )
        if not path:
            return
        path = safe_local_path(path)
        if not path.lower().endswith(".json"):
            path += ".json"

        if export_items(self._session_dir, path):
            QMessageBox.information(
                self, "Экспорт поручений",
                f"Сохранено:\n{path}",
            )
        else:
            QMessageBox.critical(
                self, "Экспорт поручений",
                "Не удалось сохранить файл.",
            )

    def _paste_json(self) -> None:
        """Вставляет поручения из буфера обмена."""
        try:
            clipboard = QApplication.clipboard()
            raw = clipboard.text()
        except Exception as exc:
            log.exception(
                "Не удалось прочитать буфер обмена: %s", exc
            )
            QMessageBox.critical(
                self, "Вставка JSON",
                f"Не удалось прочитать буфер обмена:\n{exc}",
            )
            return

        if not raw or not raw.strip():
            QMessageBox.information(
                self, "Вставка JSON",
                "Буфер обмена пуст.",
            )
            return

        result = parse_action_items_json(raw)

        if not result:
            self._show_paste_error(raw, result.warning)
            return

        self._show_paste_preview(result)

    def _show_paste_error(self, raw: str, warning: str) -> None:
        """Показывает подробную ошибку парсинга JSON."""
        preview = raw.strip()
        if len(preview) > 300:
            preview = preview[:300] + "…"

        head = preview[:60]
        head_codes = " ".join(f"{ord(c):04x}" for c in head)

        QMessageBox.warning(
            self, "Вставка JSON — ошибка",
            f"<b>Не удалось разобрать JSON из буфера обмена.</b>"
            f"<br><br>"
            f"<b>Причина:</b><br>"
            f"<span style='color:#B8860B'>"
            f"{html.escape(warning or 'Неизвестная ошибка.')}"
            f"</span>"
            f"<br><br>"
            f"<b>Начало буфера (первые 300 символов):</b>"
            f"<pre style='font-family:monospace; font-size:9pt; "
            f"background:#f5f5f5; padding:6px; "
            f"border:1px solid #ddd;'>"
            f"{html.escape(preview)}"
            f"</pre>"
            f"<b>Коды первых 60 символов (hex):</b>"
            f"<pre style='font-family:monospace; font-size:8pt; "
            f"background:#f5f5f5; padding:6px; "
            f"border:1px solid #ddd;'>"
            f"{html.escape(head_codes)}"
            f"</pre>"
            f"<b>Что можно сделать:</b><br>"
            f"• Скопируйте JSON из <i>текстового</i> редактора "
            f"(не из браузера — он может добавить HTML/RTF).<br>"
            f"• Убедитесь, что текст начинается с <code>{{</code> "
            f"или <code>[</code>.<br>"
            f"• Проверьте, что нет «умных кавычек» "
            f"(<code>“ ” ‘ ’</code>) — нужны обычные "
            f"<code>\"</code>.<br>"
            f"• При вставке из чата уберите лишний текст "
            f"(приветствия, подписи).",
        )

    def _show_paste_preview(self, result: ParseResult) -> None:
        """Показывает диалог предпросмотра перед импортом."""
        items = result.items
        format_labels = {
            "object_with_items": "полный объект action_items.json",
            "array": "массив поручений",
            "single_item": "одиночное поручение",
        }
        fmt = format_labels.get(
            result.format_kind, result.format_kind or "неизвестный",
        )

        # --- Превью первых элементов ---
        preview_lines: List[str] = []
        for i, it in enumerate(items[:10], start=1):
            text = (it.get("text") or "").strip()
            if len(text) > 80:
                text = text[:77] + "…"
            assignee = (it.get("assignee") or "").strip() or "—"
            status = STATUS_LABELS.get(
                it.get("status") or "created", "—"
            )
            due = (it.get("due_date") or "").strip() or "—"
            number = it.get("number") or 0
            num_prefix = f"№{number} " if number else ""
            preview_lines.append(
                f"{i}. [{status}] {num_prefix}{assignee} · до {due}\n"
                f"   {text}"
            )
        if len(items) > 10:
            preview_lines.append(
                f"… и ещё {len(items) - 10}"
            )
        preview_text = "\n".join(preview_lines) or "(нет элементов)"

        # --- Диалог ---
        dlg = QDialog(self)
        dlg.setWindowTitle("Вставка JSON — предпросмотр")
        dlg.setModal(True)
        dlg.setMinimumSize(720, 560)

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        info = QLabel(
            f"<b>Формат:</b> {html.escape(fmt)}<br>"
            f"<b>Поручений:</b> {len(items)}"
            + (
                f"<br><b>Имя сессии:</b> "
                f"{html.escape(result.session_name)}"
                if result.session_name else ""
            )
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        if result.warning:
            warn = QLabel(
                f"<span style='color:#B8860B'>"
                f"⚠ {html.escape(result.warning)}</span>"
            )
            warn.setWordWrap(True)
            layout.addWidget(warn)

        layout.addWidget(QLabel("<b>Предпросмотр:</b>"))

        preview_label = QLabel(preview_text)
        preview_label.setWordWrap(True)
        preview_label.setTextFormat(Qt.TextFormat.PlainText)
        preview_label.setStyleSheet(
            "QLabel {"
            "  background-color: #f5f5f5;"
            "  border: 1px solid #ddd;"
            "  border-radius: 4px;"
            "  padding: 8px;"
            "  font-family: monospace;"
            "  color: #333;"
            "}"
        )
        preview_label.setMinimumHeight(200)
        layout.addWidget(preview_label, 1)

        # --- Режим импорта ---
        mode_label = QLabel(
            "<b>Что делать с текущими поручениями?</b>"
        )
        layout.addWidget(mode_label)

        merge_check = QCheckBox(
            "Добавить к существующим (иначе — заменить все)"
        )
        merge_check.setChecked(True)
        merge_check.setToolTip(
            "• Включено — новые поручения добавятся к тем, "
            "что уже есть в записи.\n"
            "• Выключено — все текущие поручения будут удалены, "
            "останутся только вставленные."
        )
        layout.addWidget(merge_check)

        # --- Кнопки ---
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=dlg,
        )
        buttons.button(
            QDialogButtonBox.StandardButton.Ok
        ).setText("Вставить")
        buttons.button(
            QDialogButtonBox.StandardButton.Cancel
        ).setText("Отмена")
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        merge = merge_check.isChecked()
        self._apply_pasted_items(items, merge=merge)

    def _apply_pasted_items(
        self,
        items: List[Dict[str, Any]],
        *,
        merge: bool,
    ) -> None:
        """Применяет вставленные поручения к текущим данным."""
        existing = self._data.get("items", []) or []

        if merge:
            existing_ids = {it.get("id") for it in existing}
            added = 0
            for it in items:
                # Если id уже есть — генерируем новый, чтобы
                # не перетирать существующее.
                if it.get("id") in existing_ids:
                    it["id"] = _new_uuid_hex()
                # Если у элемента нет номера — выдаём новый.
                if not it.get("number"):
                    from ..tasks_manager import (
                        get_next_item_number,
                    )
                    it["number"] = get_next_item_number(
                        self._session_dir
                    )
                it["source"] = "import"
                it["updated_at"] = _iso_now()
                existing.append(it)
                existing_ids.add(it["id"])
                added += 1
            log.info(
                "Вставка JSON: добавлено %d (всего %d)",
                added, len(existing),
            )
        else:
            # При полной замене тоже выдаём номера, если их нет.
            for it in items:
                if not it.get("number"):
                    from ..tasks_manager import (
                        get_next_item_number,
                    )
                    it["number"] = get_next_item_number(
                        self._session_dir
                    )
            existing = list(items)
            log.info(
                "Вставка JSON: заменено всё, элементов=%d",
                len(existing),
            )

        self._data["items"] = existing
        self._save()
        self._rebuild_assignees()
        self._reload_table()

        QMessageBox.information(
            self, "Вставка JSON",
            f"Применено поручений: {len(items)}\n"
            f"Всего в записи: {len(existing)}",
        )

    # ------------------------------------------------------------------
    # Служебное
    # ------------------------------------------------------------------
    def _save(self) -> None:
        self._data["session_name"] = (
            self._data.get("session_name") or self._session_name
        )
        save_action_items(self._session_dir, self._data)

    def _open_folder(self) -> None:
        if not os.path.isdir(self._session_dir):
            QMessageBox.warning(
                self, "Поручения",
                f"Папка не найдена:\n{self._session_dir}",
            )
            return
        QDesktopServices.openUrl(
            QUrl.fromLocalFile(self._session_dir)
        )