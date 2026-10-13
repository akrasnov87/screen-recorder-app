"""Виджет-обёртка вокруг редактора поручений.

Используется внутри окна «Скрам». В отличие от TasksEditorDialog,
это QWidget, а не QDialog, и не имеет кнопок «Сохранить/Отмена» —
все изменения применяются сразу (как в TasksEditorDialog при
нажатии «Сохранить»).

Реализация: композиция — создаём QDialog-объект без показа,
извлекаем из него ключевые виджеты и встраиваем их.

Изменения:
  • Модуль создан.
  • ДОБАВЛЕНО: настройка видимых колонок таблицы поручений —
    в панели «Скрама» скрываем колонки «Срок» и «ID», чтобы
    таблица была компактной. Колонка «Текст» растягивается
    на всю оставшуюся ширину.
  • НОВОЕ: панель фильтров по колонкам поручений (только в
    компактном режиме «Скрама»).
  • УБРАНО: панель кнопок «Создать / Редактировать / Удалить /
    Дублировать» — она дублировала действия редактора и
    загромождала верхнюю часть панели.
    Все CRUD-операции доступны через:
      – контекстное меню таблицы (правый клик);
      – двойной клик по строке (открывает редактирование).
  • НОВОЕ: компактные строки таблицы — уменьшены отступы
    в ячейках и вертикальная высота строки, чтобы визуально
    совпадать с деревом задач TFS.
  • НОВОЕ: уменьшены вертикальные отступы между элементами
    виджета — чтобы три блока (поручения / TFS / результат
    анализа) помещались в левой части окна «Скрам» без
    прокрутки.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QLineEdit, QMenu,
    QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from ..logger import get_logger
from ..tasks_manager import STATUS_LABELS, STATUS_ORDER
from .tasks_editor_dialog import TasksEditorDialog
from .tooltips import attach_tooltip, make_info_icon

log = get_logger(__name__)


class TasksEditorWidget(QWidget):
    """
    Встроенный редактор поручений записи.

    Обёртка над TasksEditorDialog: переиспользует всю его логику,
    но предоставляет интерфейс QWidget для встраивания.

    Автосохранение: при любых изменениях данные пишутся в
    action_items.json сразу (см. TasksEditorDialog._save).
    """

    # Сигнал: список поручений изменился (для обновления других
    # блоков при необходимости).
    items_changed = Signal()

    # ------------------------------------------------------------------
    # Настройка колонок таблицы
    # ------------------------------------------------------------------
    # В панели «Скрама» таблица поручений узкая (нижняя половина
    # левой части окна). Колонки «Срок» и «ID» в ней избыточны:
    #   • «Срок» дублируется в тексте поручения и в редакторе;
    #   • «ID» — служебная, нужна только для выбора элемента.
    #
    # Скрываем их через setColumnHidden — данные остаются
    # доступны программно, индексы НЕ сдвигаются, поэтому
    # TasksEditorDialog._selected_item() (читает id из колонки 6)
    # продолжает работать без изменений.
    _SCRUM_HIDDEN_COLUMNS: tuple = (
        4,   # Срок
        6,   # ID (уже скрыта в TasksEditorDialog — дублируем)
    )

    # Индекс колонки «Текст» — она растягивается на всю ширину.
    _SCRUM_STRETCH_COLUMN: int = 2

    # Высота строки таблицы (в пикселях). Используется, чтобы
    # строки поручений выглядели так же компактно, как в дереве
    # задач TFS. Значение подобрано эмпирически под системный
    # шрифт и совпадает со стандартной высотой QTreeWidgetItem.
    _ROW_HEIGHT_PX: int = 22

    def __init__(
        self,
        *,
        session_dir: str,
        session_name: str = "",
        employees: Optional[List[Dict[str, str]]] = None,
        compact: bool = False,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)

        self._session_dir = session_dir
        self._session_name = session_name
        self._employees = list(employees or [])
        self._compact = bool(compact)

        # Создаём внутренний TasksEditorDialog, но НЕ показываем его.
        # Мы позаимствуем из него логику и данные, а UI строим сами
        # (переиспользуя его виджеты).
        self._dlg = TasksEditorDialog(
            session_dir=session_dir,
            session_name=session_name,
            employees=self._employees,
            parent=self,
        )

        # Колоночные фильтры (создаём только в компактном режиме).
        self._filter_number: Optional[QLineEdit] = None
        self._filter_status: Optional[QComboBox] = None
        self._filter_text: Optional[QLineEdit] = None
        self._filter_assignee: Optional[QLineEdit] = None
        self._filter_comment: Optional[QLineEdit] = None

        self._build_ui()

        # Перехватываем закрытие диалога — не даём ему закрыться.
        # (Мы используем его только как «движок».)
        self._dlg.reject = self._noop_reject  # type: ignore
        self._dlg.accept = self._noop_accept  # type: ignore

    def _noop_reject(self) -> None:
        """Не даём внутреннему диалогу закрыться по reject."""
        pass

    def _noop_accept(self) -> None:
        """Не даём внутреннему диалогу закрыться по accept."""
        pass

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        # Уменьшили вертикальные отступы: было (4, 4, 4, 4) spacing 4.
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(1)

        # --- Заголовок (компактный) ---
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(4)
        header.addWidget(QLabel("<b>Поручения записи</b>"))
        header.addStretch()
        icon = make_info_icon("scrum_tasks_section")
        if icon is not None:
            header.addWidget(icon)
        layout.addLayout(header)

        # --- Панель фильтров по колонкам (только в «Скраме») ---
        if self._compact:
            layout.addWidget(self._build_column_filters())

        # --- Таблица из внутреннего диалога ---
        # Забираем table-виджет из _dlg и встраиваем.
        self._dlg.table.setParent(self)
        layout.addWidget(self._dlg.table, 1)

        # --- Счётчик (компактный) ---
        self._dlg.stats_label.setParent(self)
        self._dlg.stats_label.setStyleSheet(
            "QLabel { color: #666; font-size: 9pt; padding: 0; }"
        )
        layout.addWidget(self._dlg.stats_label)

        # --- Настраиваем видимость и ширину колонок таблицы ---
        self._configure_table_columns()

        # --- Контекстное меню таблицы (замена панели кнопок) ---
        self._install_table_context_menu()

    # ------------------------------------------------------------------
    # Контекстное меню таблицы
    # ------------------------------------------------------------------
    def _install_table_context_menu(self) -> None:
        """
        Навешивает контекстное меню на таблицу поручений.

        Заменяет удалённую панель кнопок — все CRUD-операции
        доступны через правый клик по строке:
          • Создать поручение;
          • Редактировать (то же — двойной клик);
          • Дублировать;
          • Удалить.
        """
        table = self._dlg.table
        table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        table.customContextMenuRequested.connect(
            self._show_table_context_menu
        )

        # Двойной клик по строке открывает редактирование.
        table.itemDoubleClicked.connect(
            lambda _item: self._on_edit()
        )

    def _show_table_context_menu(self, pos) -> None:
        """Показывает контекстное меню для таблицы поручений."""
        table = self._dlg.table

        menu = QMenu(self)

        # --- Создать ---
        act_add = menu.addAction("Создать поручение")
        act_add.triggered.connect(self._on_add)

        # --- Редактировать / Дублировать / Удалить —
        #     только если есть выделение.
        has_selection = table.currentRow() >= 0
        if has_selection:
            menu.addSeparator()

            act_edit = menu.addAction("Редактировать")
            act_edit.triggered.connect(self._on_edit)

            act_dup = menu.addAction("Дублировать")
            act_dup.triggered.connect(self._on_duplicate)

            menu.addSeparator()

            act_del = menu.addAction("Удалить")
            act_del.triggered.connect(self._on_delete)

        menu.exec(table.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------------
    # Панель фильтров по колонкам
    # ------------------------------------------------------------------
    def _build_column_filters(self) -> QWidget:
        """
        Панель фильтров по колонкам таблицы поручений.

        Каждое поле соответствует одной колонке:
          • №           — точное число, диапазон (40-50) или
                          список (1,3,5);
          • Статус      — выпадающий список;
          • Текст       — подстрока в тексте поручения;
          • Исполнитель — подстрока в имени исполнителя;
          • Комментарий — подстрока в комментарии.

        Все фильтры комбинируются по логике AND и применяются
        мгновенно, без перезагрузки данных.
        """
        box = QWidget()
        outer = QVBoxLayout(box)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # --- Заголовок строки ---
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(4)
        header.addWidget(QLabel("<b>Фильтр по колонкам:</b>"))
        header.addStretch()

        self.filter_reset_btn = QPushButton("Сбросить")
        self.filter_reset_btn.setToolTip(
            "Очистить все фильтры по колонкам поручений."
        )
        attach_tooltip(
            self.filter_reset_btn, "scrum_tasks_filter_reset"
        )
        self.filter_reset_btn.clicked.connect(
            self._on_reset_column_filters
        )
        header.addWidget(self.filter_reset_btn)

        icon = make_info_icon("scrum_tasks_filter_intro")
        if icon is not None:
            header.addWidget(icon)
        outer.addLayout(header)

        # --- Строка полей ---
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(3)

        # №
        row.addWidget(QLabel("№:"))
        self._filter_number = QLineEdit()
        self._filter_number.setPlaceholderText("42 или 40-50")
        self._filter_number.setMaximumWidth(110)
        self._filter_number.setClearButtonEnabled(True)
        self._filter_number.setToolTip(
            "Фильтр по номеру поручения.\n\n"
            "Поддерживается:\n"
            "  • одно число: 42;\n"
            "  • несколько через запятую: 1, 3, 5;\n"
            "  • диапазон: 40-50;\n"
            "  • смешанное: 1, 5-7, 10.\n\n"
            "Пробелы игнорируются. Пустое поле — фильтр выключен."
        )
        attach_tooltip(
            self._filter_number, "scrum_tasks_filter_number"
        )
        self._filter_number.textChanged.connect(
            self._on_filter_changed
        )
        row.addWidget(self._filter_number)

        # Статус
        row.addWidget(QLabel("Статус:"))
        self._filter_status = QComboBox()
        self._filter_status.addItem("— все —", "")
        for s in STATUS_ORDER:
            self._filter_status.addItem(STATUS_LABELS[s], s)
        self._filter_status.setMinimumWidth(110)
        self._filter_status.setMaximumWidth(150)
        attach_tooltip(
            self._filter_status, "scrum_tasks_filter_status"
        )
        self._filter_status.currentIndexChanged.connect(
            self._on_filter_changed
        )
        row.addWidget(self._filter_status)

        # Текст
        row.addWidget(QLabel("Текст:"))
        self._filter_text = QLineEdit()
        self._filter_text.setPlaceholderText("подстрока…")
        self._filter_text.setClearButtonEnabled(True)
        self._filter_text.setMinimumWidth(120)
        attach_tooltip(
            self._filter_text, "scrum_tasks_filter_text"
        )
        self._filter_text.textChanged.connect(
            self._on_filter_changed
        )
        row.addWidget(self._filter_text, 2)

        # Исполнитель
        row.addWidget(QLabel("Исполнитель:"))
        self._filter_assignee = QLineEdit()
        self._filter_assignee.setPlaceholderText("часть имени…")
        self._filter_assignee.setClearButtonEnabled(True)
        self._filter_assignee.setMinimumWidth(120)
        attach_tooltip(
            self._filter_assignee, "scrum_tasks_filter_assignee"
        )
        self._filter_assignee.textChanged.connect(
            self._on_filter_changed
        )
        row.addWidget(self._filter_assignee, 2)

        # Комментарий
        row.addWidget(QLabel("Комм.:"))
        self._filter_comment = QLineEdit()
        self._filter_comment.setPlaceholderText("подстрока…")
        self._filter_comment.setClearButtonEnabled(True)
        self._filter_comment.setMinimumWidth(100)
        attach_tooltip(
            self._filter_comment, "scrum_tasks_filter_comment"
        )
        self._filter_comment.textChanged.connect(
            self._on_filter_changed
        )
        row.addWidget(self._filter_comment, 1)

        outer.addLayout(row)
        return box

    # ------------------------------------------------------------------
    # Логика фильтрации
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_number_filter(raw: str) -> Optional[set]:
        """
        Разбирает строку фильтра по номеру.

        Поддерживает:
          • пусто → None (фильтр выключен);
          • «42» → {42};
          • «1,3,5» → {1, 3, 5};
          • «40-50» → {40..50};
          • смешанное: «1, 5-7, 10» → {1, 5, 6, 7, 10}.
        """
        import re as _re

        s = (raw or "").strip()
        if not s:
            return None

        result: set = set()
        for chunk in _re.split(r"[,;]+", s):
            chunk = chunk.strip().replace(" ", "")
            if not chunk:
                continue

            m = _re.match(r"^(\d+)\s*-\s*(\d+)$", chunk)
            if m:
                try:
                    a = int(m.group(1))
                    b = int(m.group(2))
                except ValueError:
                    continue
                if a > b:
                    a, b = b, a
                if b - a > 10000:
                    b = a + 10000
                result.update(range(a, b + 1))
                continue

            if chunk.isdigit():
                try:
                    result.add(int(chunk))
                except ValueError:
                    continue

        return result if result else None

    def _matches_column_filters(self, item: Dict[str, Any]) -> bool:
        """
        Проверяет, удовлетворяет ли поручение всем колоночным
        фильтрам. Логика — AND.
        """
        # №
        if self._filter_number is not None:
            numbers = self._parse_number_filter(
                self._filter_number.text()
            )
            if numbers is not None:
                try:
                    n = int(item.get("number") or 0)
                except (TypeError, ValueError):
                    n = 0
                if n not in numbers:
                    return False

        # Статус
        if self._filter_status is not None:
            status_f = self._filter_status.currentData() or ""
            if status_f:
                if (item.get("status") or "created") != status_f:
                    return False

        # Текст
        if self._filter_text is not None:
            q = self._filter_text.text().strip().lower()
            if q:
                text = (item.get("text") or "").lower()
                if q not in text:
                    return False

        # Исполнитель
        if self._filter_assignee is not None:
            q = self._filter_assignee.text().strip().lower()
            if q:
                assignee = (item.get("assignee") or "").lower()
                if q not in assignee:
                    return False

        # Комментарий
        if self._filter_comment is not None:
            q = self._filter_comment.text().strip().lower()
            if q:
                comment = (item.get("comment") or "").lower()
                if q not in comment:
                    return False

        return True

    def _apply_column_filters(self) -> None:
        """
        Применяет колоночные фильтры к таблице.

        Скрывает строки, которые не проходят фильтр, через
        setRowHidden. Не трогает данные во внутреннем диалоге —
        только видимость.
        """
        table = self._dlg.table
        # Данные берём из того же источника, что и таблица —
        # отфильтрованный список задач из _dlg._filtered_items().
        # Он уже учитывает свои (диалоговые) фильтры.
        try:
            items_in_dlg = self._dlg._filtered_items()
        except Exception as exc:
            log.warning(
                "Не удалось получить отфильтрованные элементы: %s",
                exc,
            )
            items_in_dlg = []

        # Строим индекс по id элемента → dict.
        by_id: Dict[str, Dict[str, Any]] = {}
        for it in items_in_dlg:
            key = str(it.get("id") or "")
            if key:
                by_id[key] = it

        hidden = 0
        total = table.rowCount()
        for row in range(total):
            id_item = table.item(row, 6)  # колонка ID
            item_id = id_item.text() if id_item else ""
            it = by_id.get(item_id)

            if it is None:
                # Нет данных — показываем (не наша забота).
                table.setRowHidden(row, False)
                continue

            visible = self._matches_column_filters(it)
            table.setRowHidden(row, not visible)
            if not visible:
                hidden += 1

        log.debug(
            "Колоночные фильтры поручений: скрыто %d из %d",
            hidden, total,
        )

    def _on_filter_changed(self) -> None:
        """Реакция на изменение любого колоночного фильтра."""
        self._apply_column_filters()

    def _on_reset_column_filters(self) -> None:
        """Сбрасывает все колоночные фильтры."""
        if self._filter_number is not None:
            self._filter_number.blockSignals(True)
            self._filter_number.clear()
            self._filter_number.blockSignals(False)

        if self._filter_status is not None:
            self._filter_status.blockSignals(True)
            self._filter_status.setCurrentIndex(0)
            self._filter_status.blockSignals(False)

        if self._filter_text is not None:
            self._filter_text.blockSignals(True)
            self._filter_text.clear()
            self._filter_text.blockSignals(False)

        if self._filter_assignee is not None:
            self._filter_assignee.blockSignals(True)
            self._filter_assignee.clear()
            self._filter_assignee.blockSignals(False)

        if self._filter_comment is not None:
            self._filter_comment.blockSignals(True)
            self._filter_comment.clear()
            self._filter_comment.blockSignals(False)

        self._apply_column_filters()
        log.debug("Колоночные фильтры поручений сброшены")

    # ------------------------------------------------------------------
    # Настройка колонок таблицы
    # ------------------------------------------------------------------
    def _configure_table_columns(self) -> None:
        """
        Скрывает лишние колонки и настраивает ширину.

        Что делает:
          1. Скрывает колонки из _SCRUM_HIDDEN_COLUMNS
             («Срок», «ID»).
          2. Отключает stretchLastSection — иначе Qt растянул
             бы последнюю видимую колонку («Комментарий»),
             а «Текст» остался бы узким.
          3. Включает ResizeMode.Stretch для колонки «Текст» —
             она займёт всю оставшуюся ширину.
          4. Для остальных видимых колонок — ResizeToContents.
          5. Уменьшает вертикальные отступы в ячейках и высоту
             строк — чтобы строки выглядели компактно, как в
             дереве задач TFS.

        Данные в скрытых колонках остаются доступны программно
        (по индексу). Индексы НЕ сдвигаются, поэтому
        TasksEditorDialog._selected_item() продолжает читать
        ID из колонки 6 — код не ломается.
        """
        try:
            table = self._dlg.table
        except AttributeError:
            log.warning(
                "TasksEditorWidget: не удалось получить table "
                "из внутреннего TasksEditorDialog"
            )
            return

        # --- 1. Скрываем колонки ---
        for col in self._SCRUM_HIDDEN_COLUMNS:
            try:
                table.setColumnHidden(col, True)
            except Exception as exc:
                log.warning(
                    "TasksEditorWidget: не удалось скрыть "
                    "колонку %d: %s", col, exc,
                )

        # --- 2. Настраиваем ширину ---
        try:
            from PySide6.QtWidgets import QHeaderView

            header = table.horizontalHeader()

            # Отключаем автоматическое растягивание последней
            # колонки. Иначе Qt растянет последнюю видимую
            # («Комментарий»), а «Текст» останется узким.
            header.setStretchLastSection(False)

            total_cols = table.columnCount()
            for idx in range(total_cols):
                if idx in self._SCRUM_HIDDEN_COLUMNS:
                    continue
                if idx == self._SCRUM_STRETCH_COLUMN:
                    # «Текст» — растягивается на всё свободное.
                    header.setSectionResizeMode(
                        idx, QHeaderView.ResizeMode.Stretch
                    )
                else:
                    # Остальные — по содержимому, не тянутся.
                    header.setSectionResizeMode(
                        idx,
                        QHeaderView.ResizeMode.ResizeToContents,
                    )
        except Exception as exc:
            log.warning(
                "TasksEditorWidget: не удалось настроить "
                "QHeaderView: %s", exc,
            )

        # --- 3. Компактные строки: уменьшаем высоту и padding ---
        try:
            vheader = table.verticalHeader()
            # Включаем фиксированную высоту строк — иначе Qt
            # растянет строки на всю высоту viewport, если строк
            # мало.
            vheader.setSectionResizeMode(
                vheader.ResizeMode.Fixed
            )
            vheader.setDefaultSectionSize(self._ROW_HEIGHT_PX)
            vheader.setMinimumSectionSize(self._ROW_HEIGHT_PX)
            # Скрываем номера строк слева — в компактной панели
            # они не нужны и дополнительно занимают место.
            vheader.setVisible(False)
        except Exception as exc:
            log.warning(
                "TasksEditorWidget: не удалось настроить "
                "вертикальный заголовок: %s", exc,
            )

        # QSS для компактных отступов в ячейках.
        compact_qss = (
            "QTableView::item {"
            "  padding: 1px 6px;"
            "  margin: 0;"
            "  border: none;"
            "}"
            "QTableView {"
            "  gridline-color: palette(mid);"
            "}"
            "QHeaderView::section {"
            "  padding: 2px 6px;"
            "  margin: 0;"
            "  border: none;"
            "  border-right: 1px solid palette(mid);"
            "  border-bottom: 1px solid palette(mid);"
            "}"
        )
        try:
            existing = table.styleSheet() or ""
            table.setStyleSheet(existing + compact_qss)
        except Exception as exc:
            log.warning(
                "TasksEditorWidget: не удалось применить QSS: %s",
                exc,
            )

        log.info(
            "TasksEditorWidget: колонки таблицы настроены — "
            "скрыты: %s; растягивается: колонка %d (Текст); "
            "высота строки: %d px",
            ", ".join(str(c) for c in self._SCRUM_HIDDEN_COLUMNS),
            self._SCRUM_STRETCH_COLUMN,
            self._ROW_HEIGHT_PX,
        )

    # ------------------------------------------------------------------
    # Действия
    # ------------------------------------------------------------------
    def _on_add(self) -> None:
        self._dlg._add_item()
        self._apply_column_filters()
        self.items_changed.emit()

    def _on_edit(self) -> None:
        self._dlg._edit_selected()
        self._apply_column_filters()
        self.items_changed.emit()

    def _on_delete(self) -> None:
        self._dlg._delete_selected()
        self._apply_column_filters()
        self.items_changed.emit()

    def _on_duplicate(self) -> None:
        self._dlg._duplicate_selected()
        self._apply_column_filters()
        self.items_changed.emit()

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------
    def reload(self) -> None:
        """Перечитывает action_items.json и обновляет таблицу."""
        try:
            self._dlg._reload()
            self._apply_column_filters()
        except Exception as exc:
            log.warning(
                "TasksEditorWidget.reload: %s", exc
            )