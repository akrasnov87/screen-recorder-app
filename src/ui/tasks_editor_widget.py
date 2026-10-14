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
  • НОВОЕ: компактные строки таблицы.
  • КРИТИЧНО (фикс segfault):
      – Внутренний TasksEditorDialog создаётся с parent=None
        (было parent=self).
      – Установлен WA_DeleteOnClose=False.
      – _apply_column_filters защищён от реэнтрантности и
        мёртвых Qt-объектов (shiboken6.isValid).
      – Двойной клик по строке подключается через
        QTimer.singleShot(0, ...) — иначе use-after-free в
        Qt: Qt держит указатель на QTableWidgetItem, а внутри
        слота таблица перестраивается → segfault.
        Подтверждено стеком faulthandler.log:
          QTableWidget::itemDoubleClicked → segfault.
      – НОВОЕ: attach_double_click=False у TasksEditorDialog.
        Подписку на itemDoubleClicked делает ТОЛЬКО виджет —
        иначе получается ДВОЙНОЕ открытие редактора (два
        окна на один двойной клик).
  • НОВОЕ: параметр sort_by_assignee.
      Прокидывается во внутренний TasksEditorDialog. Если True —
      таблица поручений сортируется по колонке «Исполнитель»
      (А→Я, пустые в конце). Применяется всегда.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QLineEdit, QMenu,
    QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from ..logger import get_logger
from ..tasks_manager import STATUS_LABELS, STATUS_ORDER
from .tasks_editor_dialog import TasksEditorDialog
from .tooltips import attach_tooltip, make_info_icon

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Проверка валидности Qt-объектов
# ---------------------------------------------------------------------------
try:
    from shiboken6 import isValid as _qt_is_valid  # type: ignore
except ImportError:
    log.warning(
        "shiboken6 не установлен — проверка валидности "
        "Qt-объектов недоступна. Возможны segfault при "
        "обращении к удалённым виджетам."
    )

    def _qt_is_valid(obj: Any) -> bool:  # type: ignore
        return obj is not None


class TasksEditorWidget(QWidget):
    """
    Встроенный редактор поручений записи.

    Обёртка над TasksEditorDialog: переиспользует всю его логику,
    но предоставляет интерфейс QWidget для встраивания.
    """

    # Сигнал: список поручений изменился.
    items_changed = Signal()

    # ------------------------------------------------------------------
    # Настройка колонок таблицы
    # ------------------------------------------------------------------
    _SCRUM_HIDDEN_COLUMNS: tuple = (
        4,   # Срок
        6,   # ID (уже скрыта в TasksEditorDialog — дублируем)
    )

    _SCRUM_STRETCH_COLUMN: int = 2   # «Текст»

    _ROW_HEIGHT_PX: int = 22

    def __init__(
        self,
        *,
        session_dir: str,
        session_name: str = "",
        employees: Optional[List[Dict[str, str]]] = None,
        compact: bool = False,
        sort_by_assignee: bool = False,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)

        self._session_dir = session_dir
        self._session_name = session_name
        self._employees = list(employees or [])
        self._compact = bool(compact)

        # --- НОВОЕ: сортировка по исполнителю ---
        # Прокидывается во внутренний TasksEditorDialog и
        # применяется всегда (в т.ч. при обновлениях и фильтрах).
        self._sort_by_assignee = bool(sort_by_assignee)

        # Защита от реэнтрантности при применении колоночных
        # фильтров. Устанавливается в _apply_column_filters.
        self._applying_filters: bool = False

        # Создаём внутренний TasksEditorDialog, но НЕ показываем его.
        #
        # ВАЖНО: parent НЕ self, а None — иначе Qt может уничтожить
        # дочерний диалог вместе с виджетом и оставить висячие
        # указатели на его виджеты. Держим ссылку сами (self._dlg),
        # чтобы Python не собрал GC.
        #
        # attach_double_click=False — подписку на itemDoubleClicked
        # делает сам виджет (см. _install_table_context_menu).
        # Если бы подписка была и здесь, и в диалоге, двойной клик
        # открывал бы ДВА окна редактирования.
        self._dlg = TasksEditorDialog(
            session_dir=session_dir,
            session_name=session_name,
            employees=self._employees,
            attach_double_click=False,
            sort_by_assignee=self._sort_by_assignee,
            parent=None,
        )

        # --- КРИТИЧНО: не даём Qt удалить диалог при закрытии.
        self._dlg.setAttribute(
            Qt.WidgetAttribute.WA_DeleteOnClose, False
        )

        # Колоночные фильтры (создаём только в компактном режиме).
        self._filter_number: Optional[QLineEdit] = None
        self._filter_status: Optional[QComboBox] = None
        self._filter_text: Optional[QLineEdit] = None
        self._filter_assignee: Optional[QLineEdit] = None
        self._filter_comment: Optional[QLineEdit] = None

        self._build_ui()

        # --- Подписка на finished диалога: если что-то умудрится
        #     его закрыть — превентивно скрываем.
        self._dlg.finished.connect(self._on_dlg_finished)

    # ------------------------------------------------------------------
    # Защита от закрытия внутреннего диалога
    # ------------------------------------------------------------------
    def _on_dlg_finished(self, result: int) -> None:
        if self._dlg is None:
            return
        try:
            if not _qt_is_valid(self._dlg):
                return
            if self._dlg.isVisible():
                log.warning(
                    "TasksEditorWidget: внутренний диалог был "
                    "показан — скрываем (result=%s)", result,
                )
                self._dlg.hide()
        except Exception as exc:
            log.debug(
                "TasksEditorWidget: ошибка в _on_dlg_finished: %s",
                exc,
            )

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
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

        Единственное место, где подписываемся на itemDoubleClicked.
        Внутренний TasksEditorDialog создан с
        attach_double_click=False и на этот сигнал НЕ подписан —
        иначе было бы двойное открытие редактора.
        """
        table = self._dlg.table
        table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        table.customContextMenuRequested.connect(
            self._show_table_context_menu
        )

        # Двойной клик по строке открывает редактирование.
        #
        # ВАЖНО: НЕ вызываем self._on_edit() напрямую!
        # Сигнал itemDoubleClicked эмитится синхронно из C++
        # Qt внутри QAbstractItemView::mouseDoubleClickEvent.
        # В этот момент Qt держит указатель на QTableWidgetItem.
        # Если внутри слота начать перестройку таблицы
        # (_on_edit → _edit_selected → _reload → _reload_table →
        # setRowCount(0)), Qt-объект будет освобождён, и после
        # возврата из слота Qt снова обратится к нему — segfault
        # (подтверждено стеком faulthandler.log).
        #
        # QTimer.singleShot(0, ...) откладывает вызов до возврата
        # управления в event loop.
        table.itemDoubleClicked.connect(
            lambda _item: QTimer.singleShot(0, self._on_edit)
        )

    def _show_table_context_menu(self, pos) -> None:
        """Показывает контекстное меню для таблицы поручений."""
        if not _qt_is_valid(self._dlg) or not _qt_is_valid(
            self._dlg.table
        ):
            return

        table = self._dlg.table

        menu = QMenu(self)

        act_add = menu.addAction("Создать поручение")
        act_add.triggered.connect(self._on_add)

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
        """Панель фильтров по колонкам таблицы поручений."""
        box = QWidget()
        outer = QVBoxLayout(box)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

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
        """Разбирает строку фильтра по номеру."""
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
        """Проверяет, удовлетворяет ли поручение всем фильтрам."""
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

        if self._filter_status is not None:
            status_f = self._filter_status.currentData() or ""
            if status_f:
                if (item.get("status") or "created") != status_f:
                    return False

        if self._filter_text is not None:
            q = self._filter_text.text().strip().lower()
            if q:
                text = (item.get("text") or "").lower()
                if q not in text:
                    return False

        if self._filter_assignee is not None:
            q = self._filter_assignee.text().strip().lower()
            if q:
                assignee = (item.get("assignee") or "").lower()
                if q not in assignee:
                    return False

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

        Защищён от реэнтрантности и мёртвых Qt-объектов.
        """
        if self._applying_filters:
            log.debug(
                "Колоночные фильтры поручений: пропуск повторного "
                "входа (реэнтрантность)"
            )
            return
        self._applying_filters = True

        try:
            dlg = self._dlg
            if dlg is None:
                log.warning(
                    "Колоночные фильтры поручений: self._dlg = None"
                )
                return

            if not _qt_is_valid(dlg):
                log.warning(
                    "Колоночные фильтры поручений: "
                    "TasksEditorDialog удалён на C++ — фильтры "
                    "не применяются"
                )
                return

            try:
                table = dlg.table
            except Exception as exc:
                log.warning(
                    "Не удалось получить table из диалога: %s", exc
                )
                return

            if table is None:
                return
            if not _qt_is_valid(table):
                log.warning(
                    "Колоночные фильтры поручений: table удалён "
                    "на C++ — фильтры не применяются"
                )
                return

            try:
                items_in_dlg = dlg._filtered_items()
            except Exception as exc:
                log.warning(
                    "Не удалось получить отфильтрованные "
                    "элементы: %s", exc,
                )
                items_in_dlg = []

            by_id: Dict[str, Dict[str, Any]] = {}
            for it in items_in_dlg:
                key = str(it.get("id") or "")
                if key:
                    by_id[key] = it

            hidden = 0
            total = table.rowCount()
            for row in range(total):
                if row >= table.rowCount():
                    break

                id_item = table.item(row, 6)
                item_id = id_item.text() if id_item else ""
                it = by_id.get(item_id)

                if it is None:
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
        finally:
            self._applying_filters = False

    def _on_filter_changed(self) -> None:
        self._apply_column_filters()

    def _on_reset_column_filters(self) -> None:
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
        """Скрывает лишние колонки и настраивает ширину."""
        try:
            table = self._dlg.table
        except AttributeError:
            log.warning(
                "TasksEditorWidget: не удалось получить table "
                "из внутреннего TasksEditorDialog"
            )
            return

        for col in self._SCRUM_HIDDEN_COLUMNS:
            try:
                table.setColumnHidden(col, True)
            except Exception as exc:
                log.warning(
                    "TasksEditorWidget: не удалось скрыть "
                    "колонку %d: %s", col, exc,
                )

        try:
            from PySide6.QtWidgets import QHeaderView

            header = table.horizontalHeader()
            header.setStretchLastSection(False)

            total_cols = table.columnCount()
            for idx in range(total_cols):
                if idx in self._SCRUM_HIDDEN_COLUMNS:
                    continue
                if idx == self._SCRUM_STRETCH_COLUMN:
                    header.setSectionResizeMode(
                        idx, QHeaderView.ResizeMode.Stretch
                    )
                else:
                    header.setSectionResizeMode(
                        idx,
                        QHeaderView.ResizeMode.ResizeToContents,
                    )
        except Exception as exc:
            log.warning(
                "TasksEditorWidget: не удалось настроить "
                "QHeaderView: %s", exc,
            )

        try:
            vheader = table.verticalHeader()
            vheader.setSectionResizeMode(
                vheader.ResizeMode.Fixed
            )
            vheader.setDefaultSectionSize(self._ROW_HEIGHT_PX)
            vheader.setMinimumSectionSize(self._ROW_HEIGHT_PX)
            vheader.setVisible(False)
        except Exception as exc:
            log.warning(
                "TasksEditorWidget: не удалось настроить "
                "вертикальный заголовок: %s", exc,
            )

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
            "высота строки: %d px; sort_by_assignee=%s",
            ", ".join(str(c) for c in self._SCRUM_HIDDEN_COLUMNS),
            self._SCRUM_STRETCH_COLUMN,
            self._ROW_HEIGHT_PX,
            self._sort_by_assignee,
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
            if not _qt_is_valid(self._dlg):
                log.warning(
                    "TasksEditorWidget.reload: диалог удалён"
                )
                return
            self._dlg._reload()
            self._apply_column_filters()
        except Exception as exc:
            log.warning(
                "TasksEditorWidget.reload: %s", exc
            )