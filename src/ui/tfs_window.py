"""Окно «Задачи TFS» — просмотр списка work items.

Поддерживает два режима:
  • Плоский список (FROM WorkItems) — QTreeWidget в один уровень.
  • Дерево связей (FROM WorkItemLinks ... MODE (Recursive)) —
    иерархия «родитель → дети» на основе workItemRelations.

Возможности:
  • выполнение WIQL-запроса (редактируемого в окне);
  • выбор проекта (подгружается из TFS);
  • выбор сохранённого шаблона WIQL;
  • таблица/дерево задач с полями ID, Тип, Заголовок, Статус,
    Исполнитель, Оценка, Осталось;
  • открытие задачи в браузере TFS;
  • копирование ID и прямой ссылки на задачу;
  • сохранение текущего WIQL как шаблона;
  • сохранение/сброс изменённого WIQL.

Всё взаимодействие с TFS выполняется в фоновом QThread
(_TfsWorker), чтобы не блокировать UI.

Изменения:
  • QTableWidget заменён на QTreeWidget — единый виджет для
    плоского и иерархического отображения.
  • Автоматическое определение типа запроса (WorkItems vs
    WorkItemLinks) и вызов соответствующего метода клиента.
  • Для link-запросов строится дерево по workItemRelations.
  • Добавлены колонки «Оценка» (OriginalEstimate) и
    «Осталось» (RemainingWork).
  • Все прежние возможности сохранены.

  • ДОБАВЛЕНЫ ШАБЛОННЫЕ ПЕРЕМЕННЫЕ WIQL:
      – при нажатии «Выполнить», если WIQL содержит {{Переменные}},
        открывается диалог их ввода (TfsVariablesDialog);
      – введённые значения хранятся в памяти окна — при повторном
        «Выполнить» они подставляются автоматически (кнопка
        «Изменить переменные…» позволяет их поправить);
      – при выборе шаблона с переменными — значения также
        можно задать заранее.

  • НОВОЕ: плашка «Переменные в запросе» стала сворачиваемой.
    По умолчанию она свёрнута — показывает одну компактную
    строку с именами переменных. Клик по заголовку разворачивает
    её — видны все переменные с их значениями.
"""
from __future__ import annotations

import asyncio
import csv
import html as _html
import os
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import (
    QAction, QDesktopServices, QGuiApplication, QKeySequence,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QHBoxLayout, QHeaderView,
    QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
    QSplitter, QTreeWidget, QTreeWidgetItem, QTextBrowser,
    QVBoxLayout, QWidget,
)

from ..logger import get_logger
from ..tfs_client import (
    TfsClient,
    TfsError,
    DEFAULT_WORK_ITEM_FIELDS,
    extract_variables,
    substitute_variables,
    validate_variables,
)
from .tooltips import attach_tooltip, make_info_icon, with_info

log = get_logger(__name__)


def _html_escape(s: str) -> str:
    """Минимальное экранирование HTML."""
    import html
    return html.escape(str(s or ""))

# ---------------------------------------------------------------------------
# Диалог ввода значений шаблонных переменных
# ---------------------------------------------------------------------------
class TfsVariablesDialog(QDialog):
    """
    Диалог ввода значений для шаблонных переменных WIQL.

    Показывает по одному полю ввода на каждую переменную.
    Используется и в окне «Задачи TFS», и (потенциально) в
    других местах.

    Режимы:
      • allow_empty=False (по умолчанию) — все поля обязательны
        (для выполнения запроса).
      • allow_empty=True — пустые значения допускаются
        (для сохранения дефолтов шаблона).
    """

    def __init__(
        self,
        variables: List[str],
        *,
        initial: Optional[Dict[str, str]] = None,
        title: str = "Значения переменных WIQL",
        description: str = "",
        allow_empty: bool = False,
        empty_label: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(540)

        self._variables = list(variables or [])
        self._initial = dict(initial or {})
        self._inputs: Dict[str, QLineEdit] = {}
        self._result: Dict[str, str] = {}
        self._allow_empty = bool(allow_empty)
        self._empty_label = empty_label or ""

        self._build_ui(description)

    def _build_ui(self, description: str) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(8)

        info_row = QHBoxLayout()
        info_text = description or (
            "В WIQL-запросе есть шаблонные переменные. "
            "Укажите их значения — они будут подставлены перед "
            "выполнением запроса.\n\n"
            "Значения экранируются автоматически: одинарная "
            "кавычка «'» внутри значения не сломает запрос."
        )
        info = QLabel(info_text)
        info.setWordWrap(True)
        info.setStyleSheet("QLabel { color: #666; }")
        info_row.addWidget(info, 1)

        icon = make_info_icon("tfs_variables_dialog")
        if icon is not None:
            info_row.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)

        root.addLayout(info_row)

        # --- Форма ---
        form = QFormLayout()
        form.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight
            | Qt.AlignmentFlag.AlignVCenter
        )
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
        )

        for name in self._variables:
            line = QLineEdit()
            line.setPlaceholderText(f"Значение для «{name}»…")
            line.setText(self._initial.get(name, ""))
            if self._allow_empty:
                line.setToolTip(
                    f"Переменная шаблона WIQL: {name}\n\n"
                    f"Значение по умолчанию. Подставляется в "
                    f"запрос автоматически, когда шаблон "
                    f"используется без ручного ввода.\n\n"
                    f"Если оставить пустым — значение будет "
                    f"запрашиваться у пользователя."
                )
            else:
                line.setToolTip(
                    f"Переменная шаблона WIQL: {name}\n\n"
                    f"Подставляется в запрос вместо "
                    f"конструкции {{{{{name}}}}}.\n\n"
                    f"Спецсимволы (одинарная кавычка) "
                    f"экранируются автоматически."
                )
            self._inputs[name] = line
            form.addRow(f"{name}:", line)

        root.addLayout(form)

        # --- Плашка «пустые допустимы» ---
        if self._allow_empty and self._empty_label:
            hint = QLabel(
                f"<span style='color:#666; font-size:11px'>"
                f"{_html_escape(self._empty_label)}</span>"
            )
            hint.setWordWrap(True)
            root.addWidget(hint)

        # --- Кнопки ---
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.button(
            QDialogButtonBox.StandardButton.Ok
        ).setText("Сохранить" if self._allow_empty else "Выполнить")
        buttons.button(
            QDialogButtonBox.StandardButton.Cancel
        ).setText("Отмена")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        if self._inputs:
            first = next(iter(self._inputs.values()))
            first.setFocus()
            first.selectAll()

    def _on_accept(self) -> None:
        values: Dict[str, str] = {}
        missing: List[str] = []

        for name, field in self._inputs.items():
            value = field.text().strip()
            values[name] = value
            if not value and not self._allow_empty:
                missing.append(name)

        if missing:
            names = ", ".join(missing)
            QMessageBox.warning(
                self, "Значения переменных",
                f"Заполните все переменные.\n\n"
                f"Пустые: {names}",
            )
            return

        self._result = values
        self.accept()

    def result_values(
        self, *, include_empty: bool = False,
    ) -> Dict[str, str]:
        """
        Возвращает введённые значения.

        Args:
            include_empty: если False (по умолчанию) — пустые
                           значения не попадают в результат.
                           Если True — попадают (для сохранения
                           дефолтов, когда пустая строка = «нет
                           дефолта»).
        """
        if include_empty:
            return dict(self._result)
        return {
            k: v for k, v in self._result.items() if v
        }


# ---------------------------------------------------------------------------
# Фоновый воркер
# ---------------------------------------------------------------------------
class _TfsWorker(QThread):
    """Универсальный воркер для операций с TFS."""

    finished_ok = Signal(object)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, coro_factory, parent=None) -> None:
        super().__init__(parent)
        self._coro_factory = coro_factory

    def run(self) -> None:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                result = loop.run_until_complete(
                    self._coro_factory(self._emit_progress)
                )
            finally:
                try:
                    loop.run_until_complete(loop.shutdown_asyncgens())
                finally:
                    loop.close()
            self.finished_ok.emit(result)
        except TfsError as exc:
            log.error("TfsWorker: TfsError: %s", exc)
            self.failed.emit(str(exc))
        except Exception as exc:
            log.exception("TfsWorker: ошибка: %s", exc)
            self.failed.emit(f"{exc}\n\n{traceback.format_exc()}")

    def _emit_progress(self, message: str) -> None:
        self.progress.emit(message)


# ---------------------------------------------------------------------------
# Окно
# ---------------------------------------------------------------------------
class TfsWindow(QDialog):
    """Окно просмотра списка задач TFS (список или дерево)."""

    def __init__(
        self,
        config_manager,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.config_manager = config_manager

        self._worker: Optional[_TfsWorker] = None
        self._items: List[Dict[str, Any]] = []
        self._items_by_id: Dict[int, Dict[str, Any]] = {}
        self._projects: List[str] = []
        self._templates: List[Dict[str, str]] = []
        self._current_query_text: str = ""

        # --- Значения шаблонных переменных ---
        # Храним между вызовами «Выполнить», чтобы пользователь
        # не вводил их повторно.
        self._variable_values: Dict[str, str] = {}

        # --- Состояние сворачиваемой плашки переменных ---
        # По умолчанию свёрнута (одна компактная строка).
        self._variables_panel_expanded: bool = False

        self.setWindowTitle("Задачи TFS")
        self.setMinimumSize(1400, 800)
        self.setModal(False)

        self._load_settings()
        self._build_ui()
        self._refresh_templates()

        # Автоматически загружаем проекты, если интеграция настроена.
        if self._cfg.get("enabled") and self._cfg.get("base_url") \
                and self._cfg.get("api_key"):
            self._reload_projects()
        else:
            self.status_label.setText(
                "<span style='color:#c62828'>"
                "Интеграция с TFS не настроена. "
                "Откройте Настройки → TFS.</span>"
            )

        log.info(
            "TfsWindow открыто: enabled=%s, base_url=%r",
            self._cfg.get("enabled"),
            self._cfg.get("base_url") or "—",
        )

    # ------------------------------------------------------------------
    # Загрузка настроек
    # ------------------------------------------------------------------
    def _load_settings(self) -> None:
        try:
            self._cfg = self.config_manager.get_tfs_settings()
        except Exception as exc:
            log.warning(
                "Не удалось прочитать настройки TFS: %s", exc
            )
            self._cfg = {
                "enabled": False,
                "base_url": "",
                "api_key": "",
                "api_version": "7.0",
                "connect_timeout": 15,
                "read_timeout": 60,
                "default_wiql": "",
                "default_project": "",
                "max_items": 200,
                "cache_ttl_sec": 60,
                "wiql_templates": [],
            }

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
            "Просмотр задач TFS / Azure DevOps Server по WIQL-запросу. "
            "Поддерживаются как плоские запросы (FROM WorkItems), "
            "так и запросы по связям (FROM WorkItemLinks) — во втором "
            "случае список отображается деревом. "
            "Двойной клик по строке — открыть задачу в браузере TFS. "
            "Правый клик — контекстное меню.<br><br>"
            "<span style='color:#666'><b>Шаблонные переменные.</b> "
            "В WIQL можно использовать переменные вида "
            "<code>{{Имя}}</code>. Перед выполнением запроса "
            "приложение попросит указать их значения. "
            "Для литерала <code>{{</code> используйте "
            "<code>{{{</code> (тройная скобка).</span>"
        )
        info.setWordWrap(True)
        info.setStyleSheet("QLabel { color: #666; }")
        info_row.addWidget(info, 1)
        icon = make_info_icon("tfs_window_intro")
        if icon is not None:
            info_row.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(info_row)

        # --- Панель фильтров ---
        filters_box = QWidget()
        filters_box.setStyleSheet(
            "QWidget { background-color: palette(window); }"
        )
        filters_layout = QVBoxLayout(filters_box)
        filters_layout.setContentsMargins(0, 0, 0, 0)
        filters_layout.setSpacing(4)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Проект:"))

        self.project_combo = QComboBox()
        self.project_combo.setMinimumWidth(220)
        self.project_combo.addItem("— без проекта —", "")
        attach_tooltip(self.project_combo, "tfs_window_project")
        row1.addWidget(self.project_combo)

        default_project = str(
            self._cfg.get("default_project") or ""
        ).strip()
        if default_project:
            self.project_combo.addItem(
                default_project, default_project
            )
            self.project_combo.setCurrentIndex(1)

        row1.addSpacing(8)
        row1.addWidget(QLabel("Шаблон WIQL:"))

        self.template_combo = QComboBox()
        self.template_combo.setMinimumWidth(260)
        self.template_combo.addItem("— не выбрано —", "")
        attach_tooltip(self.template_combo, "tfs_window_template")
        row1.addWidget(self.template_combo)

        self.apply_template_btn = QPushButton("Применить")
        self.apply_template_btn.setToolTip(
            "Подставить текст выбранного шаблона в поле запроса"
        )
        self.apply_template_btn.clicked.connect(
            self._on_apply_template
        )
        row1.addWidget(self.apply_template_btn)

        row1.addStretch()

        # --- Кнопка «Переменные…» ---
        self.variables_btn = QPushButton("Переменные…")
        self.variables_btn.setToolTip(
            "Открыть диалог значений шаблонных переменных.\n\n"
            "Кнопка активна только если текущий WIQL содержит "
            "переменные вида {{Имя}}.\n\n"
            "Значения подставляются в запрос перед выполнением."
        )
        self.variables_btn.clicked.connect(
            self._on_edit_variables
        )
        self.variables_btn.setEnabled(False)
        row1.addWidget(self.variables_btn)

        self.run_btn = QPushButton("Выполнить")
        self.run_btn.setDefault(True)
        self.run_btn.setToolTip(
            "Выполнить текущий WIQL-запрос (Ctrl+Enter).\n\n"
            "Если запрос содержит переменные {{…}} — приложение "
            "сначала попросит их значения."
        )
        self.run_btn.clicked.connect(self._on_run)
        row1.addWidget(self.run_btn)

        filters_layout.addLayout(row1)

        wiql_header = QHBoxLayout()
        wiql_header.addWidget(QLabel("<b>WIQL-запрос:</b>"))
        wiql_header.addStretch()

        self.query_kind_label = QLabel("")
        self.query_kind_label.setStyleSheet(
            "QLabel { color: #666; font-style: italic; }"
        )
        wiql_header.addWidget(self.query_kind_label)

        wiql_header.addSpacing(12)

        self.reset_wiql_btn = QPushButton("Сбросить")
        self.reset_wiql_btn.setToolTip(
            "Вернуть WIQL из Настройки → TFS"
        )
        self.reset_wiql_btn.clicked.connect(self._on_reset_wiql)
        wiql_header.addWidget(self.reset_wiql_btn)

        self.save_template_btn = QPushButton("Сохранить шаблон…")
        self.save_template_btn.setToolTip(
            "Сохранить текущий WIQL как именованный шаблон"
        )
        self.save_template_btn.clicked.connect(
            self._on_save_template
        )
        wiql_header.addWidget(self.save_template_btn)

        filters_layout.addLayout(wiql_header)

        self.wiql_edit = QPlainTextEdit()
        self.wiql_edit.setPlaceholderText(
            "SELECT [System.Id] FROM WorkItems\n"
            "WHERE [System.AssignedTo] = @Me\n"
            "AND [System.State] <> 'Closed'\n"
            "ORDER BY [System.ChangedDate] DESC"
        )
        self.wiql_edit.setMinimumHeight(140)
        attach_tooltip(self.wiql_edit, "tfs_window_wiql")
        self.wiql_edit.textChanged.connect(
            self._on_wiql_text_changed
        )
        filters_layout.addWidget(self.wiql_edit)

        # --- Сворачиваемая плашка «Переменные в запросе» ---
        # Плашка занимает много места, если переменных несколько.
        # Делаем её сворачиваемой: по клику на заголовок
        # показываем/скрываем подробный список переменных.
        self.variables_panel = QWidget()
        self.variables_panel.setVisible(False)
        self.variables_panel.setStyleSheet(
            "QWidget#variablesPanel {"
            "  background-color: #FFF8E1;"
            "  border: 1px solid #FFE082;"
            "  border-radius: 4px;"
            "}"
        )
        self.variables_panel.setObjectName("variablesPanel")

        vp_layout = QVBoxLayout(self.variables_panel)
        vp_layout.setContentsMargins(8, 4, 8, 4)
        vp_layout.setSpacing(2)

        # Заголовок-переключатель (кликабельный).
        self.variables_header_btn = QPushButton()
        self.variables_header_btn.setFlat(True)
        self.variables_header_btn.setCursor(
            Qt.CursorShape.PointingHandCursor
        )
        self.variables_header_btn.setFocusPolicy(
            Qt.FocusPolicy.NoFocus
        )
        self.variables_header_btn.setStyleSheet(
            "QPushButton {"
            "  text-align: left;"
            "  border: none;"
            "  background: transparent;"
            "  color: #B8860B;"
            "  font-weight: bold;"
            "  padding: 2px 0;"
            "}"
            "QPushButton:hover { color: #8B6914; }"
        )
        self.variables_header_btn.clicked.connect(
            self._toggle_variables_panel
        )
        attach_tooltip(
            self.variables_header_btn,
            "scrum_tfs_variables_panel",
        )
        vp_layout.addWidget(self.variables_header_btn)

        # Разворачиваемое содержимое.
        self.variables_body_label = QLabel("")
        self.variables_body_label.setWordWrap(True)
        self.variables_body_label.setTextFormat(
            Qt.TextFormat.RichText
        )
        self.variables_body_label.setStyleSheet(
            "QLabel { color: #6B5B0B; padding: 0; }"
        )
        self.variables_body_label.setVisible(False)
        vp_layout.addWidget(self.variables_body_label)

        filters_layout.addWidget(self.variables_panel)

        # Обратная совместимость: старое имя атрибута
        # (используется в tfs_panel_widget.py).
        self.variables_info_label = self.variables_panel

        root.addWidget(filters_box)

        # --- Прогресс ---
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        # --- Сплиттер: дерево + превью ---
        splitter = QSplitter(Qt.Orientation.Horizontal)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(2)

        left_header = QHBoxLayout()
        left_header.addWidget(QLabel("<b>Задачи</b>"))
        left_header.addStretch()

        self.expand_all_btn = QPushButton("Развернуть всё")
        self.expand_all_btn.setToolTip(
            "Развернуть все уровни дерева"
        )
        self.expand_all_btn.clicked.connect(
            lambda: self.tree.expandAll()
        )
        left_header.addWidget(self.expand_all_btn)

        self.collapse_all_btn = QPushButton("Свернуть всё")
        self.collapse_all_btn.setToolTip(
            "Свернуть все уровни дерева"
        )
        self.collapse_all_btn.clicked.connect(
            lambda: self.tree.collapseAll()
        )
        left_header.addWidget(self.collapse_all_btn)

        table_icon = make_info_icon("tfs_window_table")
        if table_icon is not None:
            left_header.addWidget(table_icon)
        left_layout.addLayout(left_header)

        # --- Дерево задач ---
        self.tree = QTreeWidget()
        self.tree.setColumnCount(11)
        self.tree.setHeaderLabels([
            "ID", "Тип", "Заголовок", "Статус",
            "Исполнитель", "Effort", "Size",
            "Оценка", "Осталось", "Completed Work", "Work Date",
        ])
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.tree.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.tree.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.tree.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.tree.customContextMenuRequested.connect(
            self._show_context_menu
        )
        self.tree.itemSelectionChanged.connect(
            self._on_selection_changed
        )
        self.tree.itemDoubleClicked.connect(
            lambda _it, _col: self._open_in_browser()
        )

        hv = self.tree.header()
        hv.setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        hv.setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        hv.setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        hv.setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents
        )
        hv.setSectionResizeMode(
            4, QHeaderView.ResizeMode.ResizeToContents
        )
        hv.setSectionResizeMode(
            5, QHeaderView.ResizeMode.ResizeToContents
        )
        hv.setSectionResizeMode(
            6, QHeaderView.ResizeMode.ResizeToContents
        )
        hv.setSectionResizeMode(
            7, QHeaderView.ResizeMode.ResizeToContents
        )
        hv.setSectionResizeMode(
            8, QHeaderView.ResizeMode.ResizeToContents
        )
        hv.setSectionResizeMode(
            9, QHeaderView.ResizeMode.ResizeToContents
        )
        hv.setSectionResizeMode(
            10, QHeaderView.ResizeMode.ResizeToContents
        )

        left_layout.addWidget(self.tree, 1)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(2)

        right_header = QHBoxLayout()
        right_header.addWidget(QLabel("<b>Детали задачи</b>"))
        right_header.addStretch()
        right_layout.addLayout(right_header)

        self.details_view = QTextBrowser()
        self.details_view.setOpenExternalLinks(True)
        right_layout.addWidget(self.details_view, 1)

        splitter.addWidget(right)
        splitter.setSizes([980, 420])
        root.addWidget(splitter, 1)

        # --- Статусная строка ---
        status_row = QHBoxLayout()

        self.status_label = QLabel("")
        self.status_label.setStyleSheet(
            "QLabel { color: #666; }"
        )
        self.status_label.setWordWrap(True)
        status_row.addWidget(self.status_label, 1)

        self.count_label = QLabel("")
        self.count_label.setStyleSheet(
            "QLabel { color: #444; font-weight: bold; }"
        )
        status_row.addWidget(self.count_label, 0)

        root.addLayout(status_row)

        # --- Кнопки ---
        bottom = QHBoxLayout()

        self.open_btn = QPushButton("Открыть в браузере")
        self.open_btn.setToolTip(
            "Открыть выбранную задачу в браузере TFS"
        )
        self.open_btn.clicked.connect(self._open_in_browser)
        bottom.addWidget(self.open_btn)

        self.copy_id_btn = QPushButton("Скопировать ID")
        self.copy_id_btn.setToolTip(
            "Скопировать ID выбранной задачи в буфер обмена"
        )
        self.copy_id_btn.clicked.connect(self._copy_id)
        bottom.addWidget(self.copy_id_btn)

        self.copy_url_btn = QPushButton("Скопировать ссылку")
        self.copy_url_btn.setToolTip(
            "Скопировать прямую ссылку на задачу в буфер обмена"
        )
        self.copy_url_btn.clicked.connect(self._copy_url)
        bottom.addWidget(self.copy_url_btn)

        self.export_csv_btn = QPushButton("Экспорт в CSV…")
        self.export_csv_btn.setToolTip(
            "Сохранить все задачи из текущего результата "
            "в CSV-файл (открывается в Excel)."
        )
        self.export_csv_btn.clicked.connect(self._export_csv)
        bottom.addWidget(self.export_csv_btn)

        bottom.addStretch()

        self.refresh_btn = QPushButton("Обновить")
        self.refresh_btn.setToolTip(
            "Повторно выполнить текущий WIQL"
        )
        self.refresh_btn.clicked.connect(self._on_run)
        bottom.addWidget(self.refresh_btn)

        self.close_btn = QPushButton("Закрыть")
        self.close_btn.clicked.connect(self.close)
        bottom.addWidget(self.close_btn)

        root.addLayout(bottom)

        # --- Горячие клавиши ---
        from PySide6.QtGui import QShortcut
        QShortcut(
            QKeySequence("Ctrl+Return"), self
        ).activated.connect(self._on_run)
        QShortcut(
            QKeySequence("Ctrl+Enter"), self
        ).activated.connect(self._on_run)
        QShortcut(
            QKeySequence("F5"), self
        ).activated.connect(self._on_run)
        QShortcut(
            QKeySequence("Escape"), self
        ).activated.connect(self.close)

    # ------------------------------------------------------------------
    # Определение типа запроса и переменных
    # ------------------------------------------------------------------
    def _on_wiql_text_changed(self) -> None:
        """Обновляет подписи про тип запроса и переменные."""
        wiql = self.wiql_edit.toPlainText()

        # --- Тип запроса ---
        if TfsClient.is_link_query(wiql):
            self.query_kind_label.setText(
                "Режим: дерево связей"
            )
        else:
            self.query_kind_label.setText(
                "Режим: плоский список"
            )

        # --- Переменные ---
        variables = extract_variables(wiql)
        self._update_variables_info(variables)

    def _update_variables_info(self, variables: List[str]) -> None:
        """
        Обновляет сворачиваемую плашку «Переменные в запросе»
        и кнопку «Переменные…».

        Плашка состоит из:
          • заголовка-переключателя (кликабельный), в котором
            кратко перечислены имена переменных;
          • сворачиваемого тела с полным списком и значениями.

        По умолчанию плашка свёрнута — видно только одну
        компактную строку. Клик по заголовку разворачивает её.
        """
        if not hasattr(self, "variables_panel"):
            return

        if not variables:
            self.variables_panel.setVisible(False)
            self.variables_header_btn.setText("")
            self.variables_body_label.setText("")
            self.variables_body_label.setVisible(False)
            self._variables_panel_expanded = False
            if hasattr(self, "variables_btn"):
                self.variables_btn.setEnabled(False)
            return

        # --- Заголовок-переключатель ---
        # Краткий список имён переменных (до 3 — полностью,
        # иначе «первые 2 … +N»).
        marker = "▼" if self._variables_panel_expanded else "▶"

        if len(variables) <= 3:
            names_preview = ", ".join(variables)
        else:
            names_preview = (
                ", ".join(variables[:2])
                + f" … ещё {len(variables) - 2}"
            )

        self.variables_header_btn.setText(
            f"{marker}  В запросе используются переменные "
            f"({len(variables)}): {names_preview}"
        )
        self.variables_header_btn.setToolTip(
            "Клик — развернуть или свернуть список переменных "
            "с их значениями."
        )

        # --- Тело (разворачиваемая часть) ---
        body_lines: List[str] = []
        for name in variables:
            value = self._variable_values.get(name, "")
            if value:
                short = (
                    value if len(value) <= 60
                    else value[:57] + "…"
                )
                body_lines.append(
                    f"&nbsp;&nbsp;• <b>{name}</b> = "
                    f"<code>{_html.escape(short)}</code>"
                )
            else:
                body_lines.append(
                    f"&nbsp;&nbsp;• <b>{name}</b> — "
                    f"<i>значение не задано</i>"
                )

        body_lines.append(
            "<br><span style='font-size:11px'>"
            "Значения запрашиваются перед выполнением запроса. "
            "Кнопка «Переменные…» позволяет задать их заранее."
            "</span>"
        )

        self.variables_body_label.setText("<br>".join(body_lines))
        self.variables_body_label.setVisible(
            self._variables_panel_expanded
        )

        self.variables_panel.setVisible(True)

        if hasattr(self, "variables_btn"):
            self.variables_btn.setEnabled(True)

    def _toggle_variables_panel(self) -> None:
        """
        Разворачивает/сворачивает тело плашки переменных.

        Состояние хранится в self._variables_panel_expanded
        и сохраняется при обновлении содержимого — если
        пользователь развернул плашку, она останется
        развёрнутой до конца сессии.
        """
        self._variables_panel_expanded = (
            not self._variables_panel_expanded
        )

        # Обновляем стрелку в заголовке.
        text = self.variables_header_btn.text()
        if text:
            if self._variables_panel_expanded:
                text = text.replace("▶", "▼", 1)
            else:
                text = text.replace("▼", "▶", 1)
            self.variables_header_btn.setText(text)

        self.variables_body_label.setVisible(
            self._variables_panel_expanded
        )

        log.debug(
            "Плашка переменных WIQL: %s",
            "развёрнута" if self._variables_panel_expanded
            else "свёрнута",
        )

    def _on_edit_variables(self) -> None:
        """Открывает диалог ввода значений переменных."""
        wiql = self.wiql_edit.toPlainText().strip()
        if not wiql:
            QMessageBox.information(
                self, "Задачи TFS",
                "Введите WIQL-запрос.",
            )
            return

        variables = extract_variables(wiql)
        if not variables:
            QMessageBox.information(
                self, "Задачи TFS",
                "В текущем WIQL-запросе нет переменных.\n\n"
                "Синтаксис переменной: {{ИмяПеременной}}",
            )
            return

        dlg = TfsVariablesDialog(
            variables,
            initial=self._variable_values,
            title="Значения переменных WIQL",
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        self._variable_values.update(dlg.result_values())
        self._update_variables_info(variables)

        log.info(
            "TfsWindow: значения переменных обновлены: %s",
            ", ".join(variables),
        )

    def _ensure_variables_values(self) -> Optional[Dict[str, str]]:
        """
        Гарантирует, что у всех переменных есть значения.

        Возвращает dict со значениями или None, если пользователь
        отменил ввод. Если переменных нет — возвращает {}.
        """
        wiql = self.wiql_edit.toPlainText()
        variables = extract_variables(wiql)
        if not variables:
            return {}

        ok, missing = validate_variables(
            wiql, self._variable_values
        )
        if ok:
            return dict(self._variable_values)

        # Запрашиваем недостающие (и заодно правим имеющиеся).
        dlg = TfsVariablesDialog(
            variables,
            initial=self._variable_values,
            title="Значения переменных WIQL",
            description=(
                "В запросе есть переменные, которые нужно "
                "заполнить перед выполнением. "
                f"<br><br>Без значения остались: "
                f"<b>{', '.join(missing)}</b>."
            ),
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            log.info(
                "TfsWindow: ввод переменных отменён пользователем"
            )
            return None

        self._variable_values.update(dlg.result_values())
        self._update_variables_info(variables)
        return dict(self._variable_values)

    # ------------------------------------------------------------------
    # Настройки / шаблоны
    # ------------------------------------------------------------------
    def _set_wiql(self, text: str) -> None:
        self.wiql_edit.blockSignals(True)
        self.wiql_edit.setPlainText(text or "")
        self.wiql_edit.blockSignals(False)
        self._on_wiql_text_changed()

    def _refresh_templates(self) -> None:
        try:
            self._templates = (
                self.config_manager.get_tfs_wiql_templates()
            )
        except Exception as exc:
            log.warning(
                "Не удалось прочитать шаблоны WIQL: %s", exc
            )
            self._templates = []

        self.template_combo.blockSignals(True)
        self.template_combo.clear()
        self.template_combo.addItem("— не выбрано —", "")
        for tpl in self._templates:
            self.template_combo.addItem(
                tpl.get("name") or "", tpl.get("wiql") or ""
            )
        self.template_combo.blockSignals(False)

        log.debug(
            "Шаблоны WIQL обновлены: %d", len(self._templates)
        )

    def _on_apply_template(self) -> None:
        idx = self.template_combo.currentIndex()
        if idx <= 0:
            QMessageBox.information(
                self, "Задачи TFS",
                "Выберите шаблон в выпадающем списке.",
            )
            return
        wiql = self.template_combo.itemData(idx) or ""
        if not wiql:
            return
        self._set_wiql(wiql)
        log.info(
            "Применён шаблон WIQL: %r (%d символов)",
            self.template_combo.currentText(), len(wiql),
        )

    def _on_reset_wiql(self) -> None:
        self._set_wiql(self._cfg.get("default_wiql") or "")
        log.info("WIQL сброшен к значению по умолчанию")

    def _on_save_template(self) -> None:
        """
        Сохраняет текущий WIQL как шаблон.

        Если WIQL содержит шаблонные переменные — открывается
        диалог для указания значений по умолчанию. Эти значения
        будут автоматически подставляться, когда шаблон
        используется без ручного ввода (например, при
        автоматической обработке записи).
        """
        wiql = self.wiql_edit.toPlainText().strip()
        if not wiql:
            QMessageBox.information(
                self, "Задачи TFS",
                "Поле WIQL пустое — нечего сохранять.",
            )
            return

        # --- Значения по умолчанию для переменных ---
        variables = extract_variables(wiql)

        # Если шаблон с таким WIQL уже есть — подтянем
        # его текущие дефолты.
        existing_defaults: Dict[str, str] = {}

        # Сначала имя.
        name, ok = QInputDialog.getText(
            self, "Сохранить шаблон WIQL",
            "Название шаблона (например, «Мои активные задачи»):",
        )
        if not ok or not (name or "").strip():
            return
        name = name.strip()

        # Если в конфиге уже есть шаблон с таким именем —
        # берём его дефолты как основу.
        for tpl in self._templates:
            if (tpl.get("name") or "").strip() == name:
                raw_vars = tpl.get("variables") or {}
                if isinstance(raw_vars, dict):
                    existing_defaults = {
                        str(k): str(v)
                        for k, v in raw_vars.items()
                    }
                break

        defaults: Dict[str, str] = {}

        if variables:
            # Диалог с полями значений по умолчанию.
            dlg = TfsVariablesDialog(
                variables,
                initial=existing_defaults,
                title="Значения по умолчанию",
                description=(
                    "Укажите значения по умолчанию для "
                    "переменных шаблона.<br><br>"
                    "Они будут автоматически подставляться, "
                    "когда шаблон используется без ручного "
                    "ввода (например, при автоматической "
                    "обработке записи).<br><br>"
                    "<b>Можно оставить пустыми</b> — тогда при "
                    "использовании шаблона значения будут "
                    "запрашиваться у пользователя."
                ),
                allow_empty=True,
                empty_label="По умолчанию не задано",
                parent=self,
            )
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            defaults = dlg.result_values(include_empty=True)

        try:
            self.config_manager.add_tfs_wiql_template(
                name, wiql, variables=defaults,
            )
        except Exception as exc:
            log.exception(
                "Не удалось сохранить шаблон WIQL: %s", exc
            )
            QMessageBox.critical(
                self, "Задачи TFS",
                f"Не удалось сохранить шаблон:\n{exc}",
            )
            return

        self._refresh_templates()
        idx = self.template_combo.findText(name)
        if idx >= 0:
            self.template_combo.setCurrentIndex(idx)

        # --- Итог ---
        saved_defaults_count = sum(
            1 for v in defaults.values() if v
        )
        note = ""
        if variables:
            if saved_defaults_count == len(variables):
                note = (
                    f"\n\nЗначения по умолчанию заданы для всех "
                    f"переменных ({saved_defaults_count})."
                )
            elif saved_defaults_count > 0:
                note = (
                    f"\n\nЗначения по умолчанию заданы для "
                    f"{saved_defaults_count} из {len(variables)} "
                    f"переменных. Остальные будут "
                    f"запрашиваться при выполнении."
                )
            else:
                note = (
                    f"\n\nЗначения по умолчанию не заданы — "
                    f"все {len(variables)} переменных будут "
                    f"запрашиваться при использовании шаблона."
                )

        QMessageBox.information(
            self, "Задачи TFS",
            f"Шаблон «{name}» сохранён.{note}",
        )
        log.info(
            "Шаблон WIQL сохранён: %r, переменных=%d, "
            "дефолтов=%d",
            name, len(variables), saved_defaults_count,
        )

    # ------------------------------------------------------------------
    # Выполнение запроса
    # ------------------------------------------------------------------
    def _on_run(self) -> None:
        if not (self._cfg.get("enabled")
                and self._cfg.get("base_url")
                and self._cfg.get("api_key")):
            QMessageBox.warning(
                self, "Задачи TFS",
                "Интеграция с TFS не настроена.\n\n"
                "Откройте Настройки → TFS и заполните base_url "
                "и PAT.",
            )
            return

        wiql = self.wiql_edit.toPlainText().strip()
        if not wiql:
            QMessageBox.information(
                self, "Задачи TFS",
                "Введите WIQL-запрос.",
            )
            return

        # --- Переменные ---
        variables_values = self._ensure_variables_values()
        if variables_values is None:
            # Пользователь отменил ввод.
            return

        # Подставляем переменные.
        final_wiql = substitute_variables(wiql, variables_values)
        if final_wiql != wiql:
            log.info(
                "TfsWindow: WIQL после подстановки переменных "
                "(%d шт.): %d символов",
                len(variables_values), len(final_wiql),
            )

        project = self.project_combo.currentData() or ""
        max_items = int(self._cfg.get("max_items") or 0)

        cfg = self._cfg
        base_url = cfg["base_url"]
        api_key = cfg["api_key"]
        api_version = cfg.get("api_version", "7.0")
        connect_timeout = float(cfg.get("connect_timeout", 15))
        read_timeout = float(cfg.get("read_timeout", 60))

        self._current_query_text = final_wiql

        is_link = TfsClient.is_link_query(final_wiql)

        log.info(
            "Запуск WIQL: type=%s, project=%r, max_items=%d, "
            "%d символов (переменных: %d)",
            "link" if is_link else "flat",
            project or "<root>", max_items, len(final_wiql),
            len(variables_values),
        )

        async def _factory(progress):
            async with TfsClient(
                base_url=base_url,
                pat=api_key,
                api_version=api_version,
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
            ) as client:
                if is_link:
                    progress("Выполнение link-запроса…")
                    result = await client.get_work_items_by_wiql_links(
                        final_wiql, project
                    )
                    return {
                        "kind": "link",
                        "relations": result.get("relations") or [],
                        "items": result.get("items") or [],
                    }
                else:
                    progress("Выполнение WIQL…")
                    items = await client.get_work_items_by_wiql(
                        final_wiql, project,
                        max_items=max_items,
                    )
                    return {
                        "kind": "flat",
                        "relations": None,
                        "items": items,
                    }

        self._start_worker(
            _factory, on_ok=self._on_query_finished
        )

    def _reload_projects(self) -> None:
        """Загружает список проектов с сервера для выпадающего списка."""
        if not (self._cfg.get("enabled")
                and self._cfg.get("base_url")
                and self._cfg.get("api_key")):
            return

        cfg = self._cfg
        base_url = cfg["base_url"]
        api_key = cfg["api_key"]
        api_version = cfg.get("api_version", "7.0")
        connect_timeout = float(cfg.get("connect_timeout", 15))
        read_timeout = float(cfg.get("read_timeout", 60))

        async def _factory(progress):
            async with TfsClient(
                base_url=base_url,
                pat=api_key,
                api_version=api_version,
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
            ) as client:
                progress("Загрузка списка проектов…")
                return await client.list_projects()

        def _on_ok(projects: List[Dict[str, Any]]) -> None:
            self._projects = [
                str(p.get("name") or "")
                for p in projects
                if p.get("name")
            ]
            self.project_combo.blockSignals(True)
            current = self.project_combo.currentData() or ""
            self.project_combo.clear()
            self.project_combo.addItem("— без проекта —", "")
            for name in self._projects:
                self.project_combo.addItem(name, name)

            idx = -1
            if current:
                idx = self.project_combo.findData(current)

            if idx < 0:
                default_project = str(
                    self._cfg.get("default_project") or ""
                ).strip()
                if default_project:
                    idx = self.project_combo.findData(
                        default_project
                    )
                    if idx >= 0:
                        log.info(
                            "Проект по умолчанию из настроек "
                            "применён: %r",
                            default_project,
                        )
                    else:
                        log.warning(
                            "Проект по умолчанию %r не найден в "
                            "списке проектов TFS. Доступные: %s",
                            default_project,
                            ", ".join(self._projects) or "—",
                        )

            if idx < 0 and self._projects:
                idx = 1

            if idx >= 0:
                self.project_combo.setCurrentIndex(idx)

            self.project_combo.blockSignals(False)

            log.info(
                "Проекты TFS загружены: %d", len(self._projects)
            )

        self._start_worker(
            _factory, on_ok=_on_ok,
            silent_fail=True,
        )

    def _start_worker(
        self,
        coro_factory,
        *,
        on_ok,
        on_fail=None,
        silent_fail: bool = False,
    ) -> None:
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.information(
                self, "Задачи TFS",
                "Операция уже выполняется. Дождитесь завершения.",
            )
            return

        self._set_busy(True)
        self._worker = _TfsWorker(coro_factory, parent=self)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_ok.connect(on_ok)
        if on_fail is not None:
            self._worker.failed.connect(on_fail)
        elif silent_fail:
            self._worker.failed.connect(
                lambda msg: log.warning(
                    "TfsWorker (silent): %s", msg
                )
            )
        else:
            self._worker.failed.connect(self._on_worker_failed)
        self._worker.finished.connect(
            lambda: self._set_busy(False)
        )
        self._worker.start()

    def _set_busy(self, busy: bool) -> None:
        self.progress.setVisible(busy)
        for w in (
            self.run_btn, self.refresh_btn, self.reset_wiql_btn,
            self.save_template_btn, self.apply_template_btn,
            self.project_combo, self.template_combo,
            self.open_btn, self.copy_id_btn, self.copy_url_btn,
            self.expand_all_btn, self.collapse_all_btn,
            self.export_csv_btn,
        ):
            w.setEnabled(not busy)

        # Кнопка «Переменные…» активна только когда не busy
        # и есть переменные.
        if hasattr(self, "variables_btn"):
            has_vars = bool(
                extract_variables(self.wiql_edit.toPlainText())
            )
            self.variables_btn.setEnabled(not busy and has_vars)

    def _on_progress(self, message: str) -> None:
        self.status_label.setText(message)

    def _on_worker_failed(self, error: str) -> None:
        log.error("Операция TFS провалена: %s", error)
        self.status_label.setText(
            f"<span style='color:#c62828'>Ошибка: "
            f"{error[:200]}</span>"
        )
        QMessageBox.critical(
            self, "Задачи TFS",
            f"Ошибка выполнения запроса:\n\n{error}",
        )

    # ------------------------------------------------------------------
    # Результат запроса
    # ------------------------------------------------------------------
    def _on_query_finished(self, result: Dict[str, Any]) -> None:
        kind = result.get("kind") or "flat"
        items = result.get("items") or []
        relations = result.get("relations")

        self._items = list(items)
        self._items_by_id = {
            int(it["id"]): it for it in self._items if it.get("id")
        }

        if kind == "link" and relations:
            self._build_tree_from_relations(relations)
        else:
            self._build_flat_tree()

        self.count_label.setText(
            f"Найдено задач: {len(self._items)}"
        )
        self.status_label.setText(
            f"Запрос выполнен: {datetime.now():%H:%M:%S}"
        )
        log.info(
            "WIQL выполнен (%s): получено %d задач",
            kind, len(self._items),
        )

    def _build_flat_tree(self) -> None:
        """Плоский список — один уровень дерева."""
        self.tree.clear()
        for it in self._items:
            item = self._make_tree_item(it)
            self.tree.addTopLevelItem(item)

        if self._items:
            self.tree.setCurrentItem(self.tree.topLevelItem(0))

    def _build_tree_from_relations(
        self,
        relations: List[Dict[str, Any]],
    ) -> None:
        """Строит дерево на основе workItemRelations."""
        self.tree.clear()

        roots: List[int] = []
        children_map: Dict[int, List[int]] = {}

        for rel in relations:
            src = rel.get("source")
            tgt = rel.get("target")

            if not isinstance(tgt, dict) or not tgt.get("id"):
                continue
            try:
                tgt_id = int(tgt["id"])
            except (TypeError, ValueError):
                continue

            if not isinstance(src, dict) or not src.get("id"):
                if tgt_id not in roots:
                    roots.append(tgt_id)
            else:
                try:
                    src_id = int(src["id"])
                except (TypeError, ValueError):
                    continue
                children_map.setdefault(src_id, []).append(tgt_id)

        for k in list(children_map.keys()):
            seen = set()
            unique: List[int] = []
            for c in children_map[k]:
                if c not in seen:
                    unique.append(c)
                    seen.add(c)
            children_map[k] = unique

        visited: set = set()

        def _create(item_id: int) -> Optional[QTreeWidgetItem]:
            if item_id in visited:
                log.debug(
                    "Дерево: цикл или дубль (id=%s) — пропуск",
                    item_id,
                )
                return None
            visited.add(item_id)

            data = self._items_by_id.get(item_id)
            if data is None:
                placeholder = QTreeWidgetItem([
                    str(item_id), "", "(нет данных)",
                    "", "", "", "",
                    "", "", "", "",
                ])
                placeholder.setData(
                    0, Qt.ItemDataRole.UserRole, item_id
                )
                placeholder.setForeground(
                    0, Qt.GlobalColor.gray
                )
                node: QTreeWidgetItem = placeholder
            else:
                node = self._make_tree_item(data)

            for child_id in children_map.get(item_id, []):
                child = _create(child_id)
                if child is not None:
                    node.addChild(child)

            return node

        root_items: List[QTreeWidgetItem] = []
        for rid in roots:
            item = _create(rid)
            if item is not None:
                root_items.append(item)

        if not root_items:
            for item_id in children_map.keys():
                item = _create(item_id)
                if item is not None:
                    root_items.append(item)

        self.tree.addTopLevelItems(root_items)
        self.tree.expandToDepth(0)

        if root_items:
            self.tree.setCurrentItem(root_items[0])

        log.info(
            "Дерево построено: связей=%d, узлов=%d, "
            "корней=%d, задач=%d",
            len(relations), len(visited),
            len(root_items), len(self._items),
        )

    def _make_tree_item(
        self, it: Dict[str, Any]
    ) -> QTreeWidgetItem:
        """Создаёт QTreeWidgetItem для одной задачи."""
        effort = it.get("effort") or 0
        effort_str = f"{effort:g}" if effort else "—"

        size = it.get("size") or 0
        size_str = f"{size:g}" if size else "—"

        estimate = it.get("original_estimate") or 0
        est_str = f"{estimate:g}" if estimate else "—"

        remaining = it.get("remaining_work") or 0
        rem_str = f"{remaining:g}" if remaining else "—"

        completed = it.get("completed_work") or 0
        completed_str = f"{completed:g}" if completed else "—"

        fields = it.get("fields") or {}
        wd_raw = str(
            fields.get("IServ.VSTS.Scheduling.WorkDate") or ""
        )
        wd_str = self._short_date(wd_raw) if wd_raw else "—"

        item = QTreeWidgetItem([
            str(it.get("id") or ""),
            it.get("work_item_type") or "—",
            it.get("title") or "—",
            it.get("state") or "—",
            it.get("assigned_to") or "—",
            effort_str,
            size_str,
            est_str,
            rem_str,
            completed_str,
            wd_str,
        ])
        item.setData(
            0, Qt.ItemDataRole.UserRole, int(it.get("id") or 0)
        )

        state = (it.get("state") or "").lower()
        if "done" in state or "closed" in state \
                or "resolved" in state:
            item.setForeground(3, Qt.GlobalColor.darkGreen)
        elif "active" in state or "in progress" in state:
            item.setForeground(3, Qt.GlobalColor.darkYellow)
        elif "new" in state or "proposed" in state:
            item.setForeground(3, Qt.GlobalColor.darkBlue)

        priority = it.get("priority") or 0
        if priority:
            item.setToolTip(0, f"Приоритет: {priority}")

        return item

    # ------------------------------------------------------------------
    # Выбор и действия
    # ------------------------------------------------------------------
    def _selected_item(self) -> Optional[Dict[str, Any]]:
        node = self.tree.currentItem()
        if node is None:
            return None
        try:
            item_id = int(
                node.data(0, Qt.ItemDataRole.UserRole)
            )
        except (TypeError, ValueError):
            return None
        return self._items_by_id.get(item_id)

    def _on_selection_changed(self) -> None:
        it = self._selected_item()
        if not it:
            self.details_view.clear()
            return
        self.details_view.setHtml(
            self._build_details_html(it)
        )

    def _build_details_html(
        self, it: Dict[str, Any]
    ) -> str:
        def _esc(v: Any) -> str:
            return _html.escape(str(v or ""))

        title = _esc(it.get("title") or "(без названия)")
        item_id = int(it.get("id") or 0)
        url = self._build_item_url(item_id)

        rows = [
            ("ID", str(item_id)),
            ("Тип", _esc(it.get("work_item_type"))),
            ("Статус", _esc(it.get("state"))),
            ("Исполнитель", _esc(it.get("assigned_to"))),
            ("Приоритет", str(it.get("priority") or "—")),
            ("Rating", _esc(it.get("rating"))),
            ("Effort", self._fmt_hours(it.get("effort"))),
            ("Size", self._fmt_hours(it.get("size"))),
            (
                "Оценка (Original Estimate)",
                self._fmt_hours(it.get("original_estimate")),
            ),
            (
                "Осталось (Remaining Work)",
                self._fmt_hours(it.get("remaining_work")),
            ),
            (
                "Выполнено (Completed Work)",
                self._fmt_hours(it.get("completed_work")),
            ),
            (
                "Story Points",
                self._fmt_hours(it.get("story_points")),
            ),
            (
                "Target Date",
                _esc(self._short_date(it.get("target_date"))),
            ),
            ("Work Date", _esc(self._short_date(
                (it.get("fields") or {}).get(
                    "IServ.VSTS.Scheduling.WorkDate"
                )
            ))),
            ("Родитель", str(it.get("parent_id") or "—")),
            ("Создано", _esc(self._short_date(it.get("created_date")))),
            ("Изменено", _esc(self._short_date(it.get("changed_date")))),
            ("Область", _esc(it.get("area_path"))),
            ("Итерация", _esc(it.get("iteration_path"))),
            ("Теги", _esc(it.get("tags"))),
        ]

        html_parts: List[str] = [
            "<div style='font-family:sans-serif; font-size:11pt;'>",
            f"<h3 style='margin:0 0 6px 0'>#{item_id} — {title}</h3>",
        ]
        if url:
            html_parts.append(
                f"<p><a href='{_html.escape(url)}'>"
                f"Открыть в браузере TFS</a></p>"
            )

        html_parts.append(
            "<table cellpadding='4' cellspacing='0' "
            "style='border-collapse:collapse;'>"
        )
        for k, v in rows:
            if not v or v == "—":
                continue
            html_parts.append(
                "<tr>"
                f"<td style='color:#666; padding-right:8px;'>"
                f"<b>{_html.escape(k)}:</b></td>"
                f"<td>{v}</td>"
                "</tr>"
            )
        html_parts.append("</table>")
        html_parts.append("</div>")
        return "".join(html_parts)

    @staticmethod
    def _fmt_hours(value: Any) -> str:
        """Форматирует число часов (без .0 для целых)."""
        if value is None or value == 0:
            return "—"
        try:
            v = float(value)
        except (TypeError, ValueError):
            return str(value)
        if v == int(v):
            return str(int(v))
        return f"{v:g}"

    @staticmethod
    def _short_date(raw: str) -> str:
        """'2026-10-09T12:34:56.789Z' → '2026-10-09 12:34'."""
        s = (raw or "").strip()
        if not s:
            return "—"
        try:
            if "." in s:
                s = s.split(".", 1)[0]
            if s.endswith("Z"):
                s = s[:-1]
            if "T" in s:
                s = s.replace("T", " ")
            return s[:16]
        except Exception:
            return raw

    def _build_item_url(self, item_id: int) -> str:
        """Формирует прямую ссылку на задачу в веб-интерфейсе TFS."""
        if not item_id:
            return ""
        base = (self._cfg.get("base_url") or "").rstrip("/")
        if not base:
            return ""
        return f"{base}/_workitems/edit/{int(item_id)}"

    def _open_in_browser(self) -> None:
        it = self._selected_item()
        if not it:
            QMessageBox.information(
                self, "Задачи TFS", "Выберите задачу."
            )
            return
        url = self._build_item_url(int(it.get("id") or 0))
        if not url:
            QMessageBox.warning(
                self, "Задачи TFS",
                "Не удалось сформировать ссылку: не задан base_url.",
            )
            return
        log.info("Открытие задачи %s в браузере: %s",
                 it.get("id"), url)
        QDesktopServices.openUrl(QUrl(url))

    def _copy_id(self) -> None:
        it = self._selected_item()
        if not it:
            return
        text = str(it.get("id") or "")
        QGuiApplication.clipboard().setText(text)
        self.status_label.setText(
            f"ID скопирован: {text}"
        )

    def _copy_url(self) -> None:
        it = self._selected_item()
        if not it:
            return
        url = self._build_item_url(int(it.get("id") or 0))
        if not url:
            return
        QGuiApplication.clipboard().setText(url)
        self.status_label.setText("Ссылка скопирована")

    def _export_csv(self) -> None:
        """Экспорт текущего результата (self._items) в CSV."""
        if not self._items:
            QMessageBox.information(
                self, "Экспорт в CSV",
                "Нет данных для экспорта. Сначала выполните "
                "WIQL-запрос.",
            )
            return

        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        default_name = f"tfs_tasks_{stamp}.csv"
        default_path = os.path.join(
            os.path.expanduser("~"), default_name
        )

        target, _ = QFileDialog.getSaveFileName(
            self,
            "Сохранить результат в CSV",
            default_path,
            "CSV-файлы (*.csv);;Все файлы (*)",
        )
        if not target:
            return

        if not target.lower().endswith(".csv"):
            target += ".csv"

        columns: List[tuple] = [
            ("id",                 "ID"),
            ("work_item_type",     "Тип"),
            ("title",              "Заголовок"),
            ("state",              "Статус"),
            ("assigned_to",        "Исполнитель"),
            ("rating",             "Rating"),
            ("priority",           "Приоритет"),
            ("severity",           "Важность"),
            ("effort",             "Effort"),
            ("size",               "Size"),
            ("original_estimate",  "Оценка (Original Estimate)"),
            ("remaining_work",     "Осталось (Remaining Work)"),
            ("completed_work",     "Выполнено (Completed Work)"),
            ("story_points",       "Story Points"),
            ("target_date",        "Target Date"),
            ("parent_id",          "Родитель"),
            ("iteration_path",     "Итерация"),
            ("area_path",          "Область"),
            ("tags",               "Теги"),
            ("created_date",       "Создано"),
            ("changed_date",       "Изменено"),
            ("url",                "URL"),
        ]

        extra_fields: List[tuple] = [
            ("IServ.VSTS.Scheduling.WorkDate", "Work Date"),
        ]

        try:
            with open(
                target, "w",
                encoding="utf-8-sig",
                newline="",
            ) as f:
                writer = csv.writer(
                    f,
                    delimiter=";",
                    quoting=csv.QUOTE_MINIMAL,
                )

                header = [label for _, label in columns]
                header += [label for _, label in extra_fields]
                writer.writerow(header)

                items_sorted = sorted(
                    self._items,
                    key=lambda x: int(x.get("id") or 0),
                )

                for it in items_sorted:
                    row: List[Any] = []
                    for key, _label in columns:
                        v = it.get(key)
                        row.append(_csv_cell(v, key))

                    fields = it.get("fields") or {}
                    for ref_name, _label in extra_fields:
                        v = fields.get(ref_name)
                        row.append(_csv_cell(v, "date"))

                    writer.writerow(row)

            log.info(
                "Экспорт CSV: %s (%d задач)",
                target, len(self._items),
            )
            QMessageBox.information(
                self, "Экспорт в CSV",
                f"Сохранено задач: {len(self._items)}\n\n"
                f"Файл: {target}",
            )

            reply = QMessageBox.question(
                self, "Экспорт в CSV",
                "Открыть папку с файлом?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply == QMessageBox.StandardButton.Yes:
                folder = os.path.dirname(target) or "."
                QDesktopServices.openUrl(
                    QUrl.fromLocalFile(folder)
                )

        except Exception as exc:
            log.exception("Ошибка экспорта CSV: %s", exc)
            QMessageBox.critical(
                self, "Экспорт в CSV",
                f"Не удалось сохранить файл:\n{exc}",
            )

    def _show_context_menu(self, pos) -> None:
        if not self._selected_item():
            return
        menu = QMenu(self)
        menu.addAction(
            "Открыть в браузере", self._open_in_browser
        )
        menu.addAction("Скопировать ID", self._copy_id)
        menu.addAction(
            "Скопировать ссылку", self._copy_url
        )
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------------
    # Закрытие
    # ------------------------------------------------------------------
    def closeEvent(self, event) -> None:
        if self._worker is not None and self._worker.isRunning():
            reply = QMessageBox.question(
                self, "Задачи TFS",
                "Запрос ещё выполняется. Закрыть окно?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        event.accept()


def _csv_cell(value: Any, key: str = "") -> str:
    """Приводит значение Work Item к строке для CSV."""
    if value is None:
        return ""

    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(x) for x in value)

    if key.endswith("_date") or key == "date":
        s = str(value).strip()
        if not s:
            return ""
        if "." in s:
            s = s.split(".", 1)[0]
        if s.endswith("Z"):
            s = s[:-1]
        if "T" in s:
            s = s.replace("T", " ")
        return s[:16]

    if isinstance(value, float):
        if value == int(value):
            return str(int(value))
        return f"{value:g}"
    if isinstance(value, int):
        return str(value)

    return str(value)