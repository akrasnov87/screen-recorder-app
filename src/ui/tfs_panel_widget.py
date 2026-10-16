"""Встраиваемая панель задач TFS для окна «Скрам».

Отличия от TfsWindow:
  • QWidget вместо QDialog;
  • нет собственной кнопки «Закрыть» и меню;
  • есть метод apply_session_defaults() для автозаполнения
    проекта/шаблона из метаданных записи;
  • размеры по умолчанию меньше — панель рассчитана на
    встраивание в нижнюю половину левой части окна;
  • в дереве задач показываются только ключевые колонки
    (см. _SCRUM_VISIBLE_COLUMNS), остальные скрыты.

Реализация: композиция — инкапсулирует TfsWindow, забирает из
него ключевые виджеты и переносит их в свой layout.

Изменения:
  • Модуль создан.
  • ДОБАВЛЕНО: настройка видимых колонок дерева — в панели
    «Скрама» показываем только ID, Заголовок, Исполнитель,
    Completed Work, Work Date (см. _configure_tree_columns).
    Колонка «Тип» теперь СКРЫТА (ранее показывалась) — она
    занимает место, а тип задачи обычно понятен из заголовка.
  • НОВОЕ: панель фильтров по колонкам дерева TFS.
  • ИСПРАВЛЕНО: variables_info_label заменён на variables_panel
    (плашка переменных WIQL теперь сворачиваемая — см.
    tfs_window.py).
  • НОВОЕ: уменьшены вертикальные отступы между элементами
    виджета — чтобы три блока (поручения / TFS / результат
    анализа) помещались в левой части окна «Скрам» без
    прокрутки. Высота WIQL-поля уменьшена до 40–60 px.
  • НОВОЕ: параметр sort_by_assignee.
    Прокидывается во внутренний TfsWindow. Если True — задачи
    сортируются по колонке «Исполнитель» (А→Я, пустые в конце).
    В дереве связей иерархия сохраняется: сортируются только
    узлы внутри каждого родителя.
  • НОВОЕ: сворачиваемая верхняя панель управления (проект +
    шаблон + WIQL + переменные + фильтры по колонкам). Одним
    кликом по заголовку «▶ Панель управления TFS» вся эта
    часть скрывается/показывается. Это освобождает заметную
    часть вертикального места под дерево задач — что особенно
    важно в левой нижней половине окна «Скрам». Состояние
    хранится в self._controls_expanded и инициализируется
    из параметра start_controls_collapsed (по умолчанию —
    свёрнуто).
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..logger import get_logger
from ..tfs_client import extract_variables
from .tfs_window import TfsWindow, TfsVariablesDialog
from .tooltips import attach_tooltip, make_info_icon

log = get_logger(__name__)


class TfsPanelWidget(QWidget):
    """
    Встраиваемая панель для просмотра задач TFS.

    Переиспользует всю логику TfsWindow, но предоставляет
    компактный UI без диалоговой обвязки.
    """

    # ------------------------------------------------------------------
    # Настройка колонок дерева
    # ------------------------------------------------------------------
    # В панели «Скрама» показываем только ключевые колонки —
    # чтобы таблица была компактной и не уезжала по горизонтали
    # в узкой нижней половине левой части окна.
    #
    # Оставляем: ID, Заголовок, Исполнитель,
    # Completed Work, Work Date.
    #
    # ИСТОРИЯ:
    #   • Раньше также показывалась колонка «Тип» — теперь
    #     она СКРЫТА: в компактном режиме она занимает место,
    #     а тип задачи обычно понятен из заголовка или
    #     из префикса (например, «Bug 1234: …»).
    #   • При необходимости тип можно посмотреть в окне
    #     «Задачи TFS» (трей → «Задачи TFS») — там он виден.
    #
    # ВАЖНО: названия должны точно совпадать с теми, что
    # установлены в TfsWindow._build_ui() (см. setHeaderLabels).
    _SCRUM_VISIBLE_COLUMNS: tuple = (
        "ID",
        "Заголовок",
        "Исполнитель",
        "Completed Work",
        "Work Date",
    )

    # Индекс колонки «Заголовок» — растягивается на всю ширину.
    _SCRUM_STRETCH_COLUMN: int = 2

    # Индексы колонок для фильтрации (в исходном TfsWindow).
    _COL_ID: int = 0
    _COL_TYPE: int = 1
    _COL_TITLE: int = 2
    _COL_ASSIGNEE: int = 4

    def __init__(
        self,
        *,
        config_manager,
        max_items: int = 200,
        sort_by_assignee: bool = False,
        start_controls_collapsed: bool = True,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)

        self._config_manager = config_manager
        self._max_items = int(max_items)

        # --- НОВОЕ: сортировка по исполнителю ---
        self._sort_by_assignee = bool(sort_by_assignee)

        # --- НОВОЕ: сворачиваемая панель управления ---
        # По умолчанию панель СВЁРНУТА — это освобождает
        # вертикальное место под дерево задач в окне «Скрам».
        self._controls_expanded: bool = not bool(
            start_controls_collapsed
        )

        # Поля фильтров (создаются в _build_ui).
        self._filter_id: Optional[QLineEdit] = None
        self._filter_type: Optional[QLineEdit] = None
        self._filter_title: Optional[QLineEdit] = None
        self._filter_assignee: Optional[QLineEdit] = None

        # Внутренний TfsWindow — используем как движок.
        # НЕ показываем.
        self._win = TfsWindow(
            config_manager=config_manager,
            sort_by_assignee=self._sort_by_assignee,
            parent=self,
        )

        self._build_ui()

        # Настраиваем колонки дерева — в панели «Скрама»
        # показываем только 5 нужных колонок, а не все 11.
        self._configure_tree_columns()

        # Применяем начальное состояние сворачивания панели.
        self._apply_controls_expanded_state()

        # Прячем внутреннее окно — оно не должно показываться.
        self._win.hide()
        self._win.setWindowFlags(Qt.WindowType.Widget)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        # Уменьшили вертикальные отступы.
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(1)

        # --- Заголовок (компактный) ---
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(4)
        header.addWidget(QLabel("<b>Задачи TFS</b>"))
        header.addStretch()
        icon = make_info_icon("scrum_tfs_section")
        if icon is not None:
            header.addWidget(icon)
        layout.addLayout(header)

        # ==============================================================
        # НОВОЕ: сворачиваемая панель управления.
        #
        # Кнопка-заголовок всегда видна (одна компактная строка).
        # Клик по ней сворачивает/разворачивает весь блок
        # управления: проект, шаблон, WIQL, переменные, фильтры.
        # ==============================================================
        self.panel_toggle_btn = QPushButton()
        self.panel_toggle_btn.setFlat(True)
        self.panel_toggle_btn.setCursor(
            Qt.CursorShape.PointingHandCursor
        )
        self.panel_toggle_btn.setFocusPolicy(
            Qt.FocusPolicy.NoFocus
        )
        self.panel_toggle_btn.setStyleSheet(
            "QPushButton {"
            "  text-align: left;"
            "  border: none;"
            "  background: transparent;"
            "  color: #1565C0;"
            "  font-weight: bold;"
            "  font-size: 10pt;"
            "  padding: 1px 0;"
            "}"
            "QPushButton:hover { color: #0d47a1; }"
        )
        self.panel_toggle_btn.clicked.connect(
            self._toggle_controls_panel
        )
        attach_tooltip(
            self.panel_toggle_btn,
            "scrum_tfs_panel_toggle",
        )
        layout.addWidget(self.panel_toggle_btn)

        # --- Контейнер сворачиваемой части ---
        self.controls_container = QWidget()
        controls_layout = QVBoxLayout(self.controls_container)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(1)

        # --- Панель управления: проект + шаблон + переменные ---
        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(4)

        controls.addWidget(QLabel("Проект:"))
        self._win.project_combo.setParent(self)
        attach_tooltip(self._win.project_combo, "scrum_tfs_project")
        controls.addWidget(self._win.project_combo, 1)

        controls.addSpacing(4)
        controls.addWidget(QLabel("Шаблон:"))
        self._win.template_combo.setParent(self)
        attach_tooltip(self._win.template_combo, "scrum_tfs_template")
        controls.addWidget(self._win.template_combo, 2)

        self.apply_btn = QPushButton("Применить")
        self.apply_btn.clicked.connect(self._win._on_apply_template)
        controls.addWidget(self.apply_btn)

        self.variables_btn = QPushButton("Переменные…")
        attach_tooltip(self.variables_btn, "scrum_tfs_variables")
        self.variables_btn.clicked.connect(self._win._on_edit_variables)
        controls.addWidget(self.variables_btn)

        self.run_btn = QPushButton("Выполнить")
        self.run_btn.setDefault(True)
        self.run_btn.clicked.connect(self._on_run)
        controls.addWidget(self.run_btn)

        controls_layout.addLayout(controls)

        # --- WIQL-превью (компактное) ---
        wiql_row = QHBoxLayout()
        wiql_row.setContentsMargins(0, 0, 0, 0)
        wiql_row.setSpacing(4)
        wiql_row.addWidget(QLabel("<b>WIQL:</b>"))

        # Забираем wiql_edit из TfsWindow.
        self._win.wiql_edit.setParent(self)
        # Уменьшили высоту WIQL-поля: было min=60, max=90.
        self._win.wiql_edit.setMinimumHeight(40)
        self._win.wiql_edit.setMaximumHeight(60)
        wiql_row.addWidget(self._win.wiql_edit, 1)
        controls_layout.addLayout(wiql_row)

        # --- Сворачиваемая плашка переменных WIQL ---
        # (из TfsWindow; теперь это variables_panel, а не
        # variables_info_label — плашка стала сворачиваемой).
        self._win.variables_panel.setParent(self)
        controls_layout.addWidget(self._win.variables_panel)

        # Прогресс (из TfsWindow).
        self._win.progress.setParent(self)
        controls_layout.addWidget(self._win.progress)

        # --- Панель фильтров по колонкам ---
        controls_layout.addWidget(self._build_column_filters())

        # Добавляем контейнер в общий layout панели.
        layout.addWidget(self.controls_container)

        # --- Дерево задач ---
        self._win.tree.setParent(self)
        layout.addWidget(self._win.tree, 1)

        # --- Статус (компактный) ---
        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(6)
        self._win.status_label.setParent(self)
        self._win.status_label.setStyleSheet(
            "QLabel { color: #666; font-size: 9pt; }"
        )
        self._win.count_label.setParent(self)
        self._win.count_label.setStyleSheet(
            "QLabel { color: #444; font-weight: bold; "
            "font-size: 9pt; }"
        )
        status_row.addWidget(self._win.status_label, 1)
        status_row.addWidget(self._win.count_label, 0)
        layout.addLayout(status_row)

        # --- Кнопки над деревом (компактные) ---
        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.setSpacing(4)

        self.open_btn = QPushButton("Открыть")
        self.open_btn.clicked.connect(self._win._open_in_browser)
        bottom.addWidget(self.open_btn)

        self.copy_id_btn = QPushButton("Копировать ID")
        self.copy_id_btn.clicked.connect(self._win._copy_id)
        bottom.addWidget(self.copy_id_btn)

        self.copy_url_btn = QPushButton("Копировать ссылку")
        self.copy_url_btn.clicked.connect(self._win._copy_url)
        bottom.addWidget(self.copy_url_btn)

        self.expand_all_btn = QPushButton("Развернуть")
        self.expand_all_btn.clicked.connect(
            lambda: self._win.tree.expandAll()
        )
        bottom.addWidget(self.expand_all_btn)

        self.collapse_all_btn = QPushButton("Свернуть")
        self.collapse_all_btn.clicked.connect(
            lambda: self._win.tree.collapseAll()
        )
        bottom.addWidget(self.collapse_all_btn)

        bottom.addStretch()
        layout.addLayout(bottom)

    # ------------------------------------------------------------------
    # Сворачивание панели управления
    # ------------------------------------------------------------------
    def _toggle_controls_panel(self) -> None:
        """
        Переключает состояние «панель управления развёрнута».

        Скрывает/показывает весь контейнер controls_container,
        содержащий: проект + шаблон + WIQL + переменные +
        фильтры по колонкам.
        """
        self._controls_expanded = not self._controls_expanded
        self._apply_controls_expanded_state()

        log.debug(
            "TfsPanelWidget: панель управления %s",
            "развёрнута" if self._controls_expanded
            else "свёрнута",
        )

    def _apply_controls_expanded_state(self) -> None:
        """
        Применяет текущее состояние к UI.

        Обновляет текст кнопки-заголовка (стрелка ▶ / ▼) и
        видимость контейнера controls_container.
        """
        if self._controls_expanded:
            arrow = "▼"
            hint = (
                "Клик — свернуть панель управления. "
                "Освобождает место под дерево задач."
            )
        else:
            arrow = "▶"
            hint = (
                "Клик — развернуть панель управления "
                "(проект, шаблон, WIQL, переменные, фильтры)."
            )

        self.panel_toggle_btn.setText(
            f"{arrow}  Панель управления TFS"
        )
        self.panel_toggle_btn.setToolTip(hint)

        # Показ/скрытие контейнера.
        self.controls_container.setVisible(self._controls_expanded)

    # ------------------------------------------------------------------
    # Панель фильтров по колонкам
    # ------------------------------------------------------------------
    def _build_column_filters(self) -> QWidget:
        """
        Панель фильтров по колонкам дерева задач TFS.

        Работает по уже загруженному дереву — не делает новых
        запросов к TFS. Фильтры применяются мгновенно.
        """
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
            "Очистить все фильтры по колонкам задач TFS."
        )
        attach_tooltip(
            self.filter_reset_btn, "scrum_tfs_filter_reset"
        )
        self.filter_reset_btn.clicked.connect(
            self._on_reset_column_filters
        )
        header.addWidget(self.filter_reset_btn)

        icon = make_info_icon("scrum_tfs_filter_intro")
        if icon is not None:
            header.addWidget(icon)
        outer.addLayout(header)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(3)

        # ID
        row.addWidget(QLabel("ID:"))
        self._filter_id = QLineEdit()
        self._filter_id.setPlaceholderText("1505 или 1500-1510")
        self._filter_id.setMaximumWidth(140)
        self._filter_id.setClearButtonEnabled(True)
        attach_tooltip(
            self._filter_id, "scrum_tfs_filter_id"
        )
        self._filter_id.textChanged.connect(
            self._on_filter_changed
        )
        row.addWidget(self._filter_id)

        # Тип
        row.addWidget(QLabel("Тип:"))
        self._filter_type = QLineEdit()
        self._filter_type.setPlaceholderText("Bug, Task…")
        self._filter_type.setMaximumWidth(120)
        self._filter_type.setClearButtonEnabled(True)
        attach_tooltip(
            self._filter_type, "scrum_tfs_filter_type"
        )
        self._filter_type.textChanged.connect(
            self._on_filter_changed
        )
        row.addWidget(self._filter_type)

        # Заголовок
        row.addWidget(QLabel("Заголовок:"))
        self._filter_title = QLineEdit()
        self._filter_title.setPlaceholderText("подстрока…")
        self._filter_title.setClearButtonEnabled(True)
        attach_tooltip(
            self._filter_title, "scrum_tfs_filter_title"
        )
        self._filter_title.textChanged.connect(
            self._on_filter_changed
        )
        row.addWidget(self._filter_title, 2)

        # Исполнитель
        row.addWidget(QLabel("Исполнитель:"))
        self._filter_assignee = QLineEdit()
        self._filter_assignee.setPlaceholderText("часть имени…")
        self._filter_assignee.setClearButtonEnabled(True)
        attach_tooltip(
            self._filter_assignee, "scrum_tfs_filter_assignee"
        )
        self._filter_assignee.textChanged.connect(
            self._on_filter_changed
        )
        row.addWidget(self._filter_assignee, 2)

        outer.addLayout(row)
        return box

    # ------------------------------------------------------------------
    # Логика фильтрации
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_id_filter(raw: str) -> Optional[set]:
        """
        Разбирает строку фильтра по ID.

        Поддерживает: «1505», «1500-1510», «1, 3, 5-7».
        """
        s = (raw or "").strip()
        if not s:
            return None

        result: set = set()
        for chunk in re.split(r"[,;]+", s):
            chunk = chunk.strip().replace(" ", "")
            if not chunk:
                continue

            m = re.match(r"^(\d+)\s*-\s*(\d+)$", chunk)
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

    def _matches_column_filters(
        self, item: QTreeWidgetItem
    ) -> bool:
        """Проверяет, удовлетворяет ли узел колоночным фильтрам."""
        # ID
        if self._filter_id is not None:
            ids = self._parse_id_filter(self._filter_id.text())
            if ids is not None:
                raw_id = item.text(self._COL_ID)
                try:
                    n = int(raw_id)
                except (TypeError, ValueError):
                    n = 0
                if n not in ids:
                    return False

        # Тип
        if self._filter_type is not None:
            q = self._filter_type.text().strip().lower()
            if q:
                if q not in item.text(self._COL_TYPE).lower():
                    return False

        # Заголовок
        if self._filter_title is not None:
            q = self._filter_title.text().strip().lower()
            if q:
                if q not in item.text(self._COL_TITLE).lower():
                    return False

        # Исполнитель
        if self._filter_assignee is not None:
            q = self._filter_assignee.text().strip().lower()
            if q:
                if q not in item.text(self._COL_ASSIGNEE).lower():
                    return False

        return True

    def _any_filter_active(self) -> bool:
        """Есть ли хотя бы один активный колоночный фильтр."""
        for f in (
            self._filter_id,
            self._filter_type,
            self._filter_title,
            self._filter_assignee,
        ):
            if f is not None and f.text().strip():
                return True
        return False

    def _apply_column_filters(self) -> None:
        """
        Применяет колоночные фильтры к дереву.

        Логика:
          • Обходим все узлы рекурсивно.
          • Для каждого узла проверяем, проходит ли он фильтр.
          • Узел виден, если:
              – сам проходит фильтр, ИЛИ
              – хотя бы один из его потомков проходит фильтр
                (иначе при фильтрации по имени родитель
                 «исчезнет», и потомки тоже пропадут из виду).
          • Узлы без активных фильтров всегда видны.

        Скрываем через setHidden(True) — данные и связи дерева
        остаются нетронутыми.
        """
        tree = self._win.tree
        active = self._any_filter_active()

        if not active:
            # Все видимы.
            def _show_all(it: QTreeWidgetItem) -> None:
                it.setHidden(False)
                for i in range(it.childCount()):
                    _show_all(it.child(i))

            for i in range(tree.topLevelItemCount()):
                _show_all(tree.topLevelItem(i))
            return

        def _walk(it: QTreeWidgetItem) -> bool:
            """
            Рекурсивно обходит поддерево и возвращает True,
            если узел сам или хотя бы один из потомков виден.
            """
            children_visible = False
            for i in range(it.childCount()):
                child = it.child(i)
                child_vis = _walk(child)
                if child_vis:
                    children_visible = True

            self_visible = self._matches_column_filters(it)
            visible = self_visible or children_visible

            it.setHidden(not visible)
            return visible

        for i in range(tree.topLevelItemCount()):
            _walk(tree.topLevelItem(i))

    def _on_filter_changed(self) -> None:
        """Реакция на изменение любого колоночного фильтра."""
        self._apply_column_filters()

    def _on_reset_column_filters(self) -> None:
        """Сбрасывает все колоночные фильтры."""
        for f in (
            self._filter_id,
            self._filter_type,
            self._filter_title,
            self._filter_assignee,
        ):
            if f is None:
                continue
            f.blockSignals(True)
            f.clear()
            f.blockSignals(False)

        self._apply_column_filters()
        log.debug("Колоночные фильтры TFS сброшены")

    # ------------------------------------------------------------------
    # Настройка колонок дерева
    # ------------------------------------------------------------------
    def _configure_tree_columns(self) -> None:
        """
        Скрывает лишние колонки в дереве задач.

        TfsWindow строит дерево с 11 колонками (ID, Тип,
        Заголовок, Статус, Исполнитель, Effort, Size, Оценка,
        Осталось, Completed Work, Work Date). В панели «Скрама»
        такое количество избыточно — нижняя половина левой
        части окна слишком узкая, приходится постоянно
        скроллить по горизонтали.

        Метод оставляет только колонки из
        _SCRUM_VISIBLE_COLUMNS, остальные скрывает через
        setColumnHidden().

        Также настраивает ширину колонок:
          • «Заголовок» — растягивается на всё свободное место;
          • остальные — по содержимому.

        Данные в скрытых колонках остаются доступны программно
        (по индексу), поэтому никакие кнопки, меню и обработчики
        не ломаются: они работают через _selected_item(),
        который читает UserRole, а не номер колонки.
        """
        try:
            tree = self._win.tree
        except AttributeError:
            log.warning(
                "TfsPanelWidget: не удалось получить tree "
                "из внутреннего TfsWindow"
            )
            return

        # Получаем названия всех колонок.
        try:
            headers = [
                tree.headerItem().text(i)
                for i in range(tree.columnCount())
            ]
        except Exception as exc:
            log.warning(
                "TfsPanelWidget: не удалось прочитать заголовки "
                "колонок tree: %s", exc,
            )
            return

        visible_set = set(self._SCRUM_VISIBLE_COLUMNS)

        hidden_indices: List[int] = []
        for idx, name in enumerate(headers):
            if name in visible_set:
                tree.setColumnHidden(idx, False)
            else:
                tree.setColumnHidden(idx, True)
                hidden_indices.append(idx)

        log.info(
            "TfsPanelWidget: колонки tree настроены — "
            "показаны: %s; скрыто: %d",
            ", ".join(self._SCRUM_VISIBLE_COLUMNS),
            len(hidden_indices),
        )

        # --- Настраиваем ширину колонок ---
        try:
            from PySide6.QtWidgets import QHeaderView

            header = tree.header()

            # Отключаем автоматическое растягивание последней
            # колонки: иначе Qt растянет последнюю видимую
            # («Work Date»), а «Заголовок» останется узким.
            header.setStretchLastSection(False)

            for idx, name in enumerate(headers):
                if name == "Заголовок":
                    header.setSectionResizeMode(
                        idx, QHeaderView.ResizeMode.Stretch
                    )
                elif name in (
                    "ID",
                    "Исполнитель",
                    "Completed Work",
                    "Work Date",
                ):
                    header.setSectionResizeMode(
                        idx,
                        QHeaderView.ResizeMode.ResizeToContents,
                    )
        except Exception as exc:
            log.warning(
                "TfsPanelWidget: не удалось настроить "
                "QHeaderView: %s", exc,
            )

    # ------------------------------------------------------------------
    # Действия
    # ------------------------------------------------------------------
    def _on_run(self) -> None:
        """Запускает WIQL-запрос с проверкой переменных."""
        # Проверяем переменные через внутренний метод.
        self._win._on_run()

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------
    def apply_session_defaults(
        self,
        *,
        project: str = "",
        template: str = "",
        variables: Optional[Dict[str, str]] = None,
    ) -> None:
        """
        Заполняет панель значениями из метаданных записи.

        Args:
            project:   имя проекта TFS (из session.json).
            template:  имя WIQL-шаблона.
            variables: значения переменных.
        """
        # --- Проект ---
        if project:
            idx = self._win.project_combo.findData(project)
            if idx >= 0:
                self._win.project_combo.setCurrentIndex(idx)
                log.debug(
                    "TFS-панель: применён проект %r из метаданных",
                    project,
                )
            else:
                # Пробуем по тексту (на случай, если данных ещё нет).
                idx = self._win.project_combo.findText(project)
                if idx >= 0:
                    self._win.project_combo.setCurrentIndex(idx)

        # --- Шаблон ---
        if template:
            idx = self._win.template_combo.findData(template)
            if idx >= 0:
                self._win.template_combo.setCurrentIndex(idx)
                # Применяем шаблон вручную (по аналогии
                # с нажатием «Применить»).
                self._win._on_apply_template()
                log.debug(
                    "TFS-панель: применён шаблон %r из метаданных",
                    template,
                )

        # --- Переменные ---
        if variables:
            for k, v in variables.items():
                if v is None:
                    continue
                self._win._variable_values[str(k)] = str(v)

            # Обновляем информационную плашку.
            wiql = self._win.wiql_edit.toPlainText()
            variables_in_wiql = extract_variables(wiql)
            if variables_in_wiql:
                self._win._update_variables_info(variables_in_wiql)

    def reload_settings(self) -> None:
        """Перечитывает настройки TFS (проекты, шаблоны)."""
        try:
            self._win._load_settings()
            self._win._refresh_templates()
            self._win._reload_projects()
        except Exception as exc:
            log.warning(
                "TfsPanelWidget.reload_settings: %s", exc
            )

    def closeEvent(self, event) -> None:
        # Останавливаем воркер TfsWindow, если он запущен.
        try:
            if (self._win._worker is not None
                    and self._win._worker.isRunning()):
                self._win._worker.requestInterruption()
        except Exception:
            pass
        super().closeEvent(event)