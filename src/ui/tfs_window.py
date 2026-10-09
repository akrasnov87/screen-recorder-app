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
  • Автоматическое разворачивание первого уровня дерева.
  • Все прежние возможности (копирование ID/ссылки, контекстное
    меню, сохранение шаблонов) сохранены.
"""
from __future__ import annotations

import asyncio
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
    QAbstractItemView, QComboBox, QDialog, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QMenu,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QSplitter, QTreeWidget, QTreeWidgetItem, QTextBrowser,
    QVBoxLayout, QWidget,
)

from ..logger import get_logger
from ..tfs_client import (
    TfsClient,
    TfsError,
    DEFAULT_WORK_ITEM_FIELDS,
)
from .tooltips import attach_tooltip, make_info_icon, with_info

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Фоновый воркер
# ---------------------------------------------------------------------------
class _TfsWorker(QThread):
    """
    Универсальный воркер для операций с TFS.

    Принимает корутину-фабрику, которая получает progress-callback
    и возвращает результат. Все исключения TfsError транслируются
    в сигнал failed.
    """

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

        self.setWindowTitle("Задачи TFS")
        self.setMinimumSize(1400, 800)
        self.setModal(False)

        self._load_settings()   # ← сначала загружаем настройки
        self._build_ui()        # ← потом строим UI
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
            "Правый клик — контекстное меню."
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

        # Предварительно ставим проект из настроек — до загрузки
        # полного списка с сервера.
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

        self.run_btn = QPushButton("Выполнить")
        self.run_btn.setDefault(True)
        self.run_btn.setToolTip(
            "Выполнить текущий WIQL-запрос (Ctrl+Enter)"
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

        # --- Дерево задач (используется и для плоского списка) ---
        self.tree = QTreeWidget()
        self.tree.setColumnCount(7)
        self.tree.setHeaderLabels([
            "ID", "Тип", "Заголовок", "Статус",
            "Исполнитель", "Оценка", "Осталось",
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
        )   # ID
        hv.setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )   # Тип
        hv.setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )   # Заголовок (растягивается)
        hv.setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents
        )   # Статус
        hv.setSectionResizeMode(
            4, QHeaderView.ResizeMode.ResizeToContents
        )   # Исполнитель
        hv.setSectionResizeMode(
            5, QHeaderView.ResizeMode.ResizeToContents
        )   # Оценка
        hv.setSectionResizeMode(
            6, QHeaderView.ResizeMode.ResizeToContents
        )   # Осталось

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
    # Определение типа запроса
    # ------------------------------------------------------------------
    def _on_wiql_text_changed(self) -> None:
        """Обновляет подпись с типом запроса."""
        wiql = self.wiql_edit.toPlainText()
        if TfsClient.is_link_query(wiql):
            self.query_kind_label.setText(
                "Режим: дерево связей"
            )
        else:
            self.query_kind_label.setText(
                "Режим: плоский список"
            )

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
        wiql = self.wiql_edit.toPlainText().strip()
        if not wiql:
            QMessageBox.information(
                self, "Задачи TFS",
                "Поле WIQL пустое — нечего сохранять.",
            )
            return

        name, ok = QInputDialog.getText(
            self, "Сохранить шаблон WIQL",
            "Название шаблона (например, «Мои активные задачи»):",
        )
        if not ok or not (name or "").strip():
            return
        name = name.strip()

        try:
            self.config_manager.add_tfs_wiql_template(name, wiql)
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

        QMessageBox.information(
            self, "Задачи TFS",
            f"Шаблон «{name}» сохранён.",
        )
        log.info("Шаблон WIQL сохранён: %r", name)

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

        project = self.project_combo.currentData() or ""
        max_items = int(self._cfg.get("max_items") or 0)

        cfg = self._cfg
        base_url = cfg["base_url"]
        api_key = cfg["api_key"]
        api_version = cfg.get("api_version", "7.0")
        connect_timeout = float(cfg.get("connect_timeout", 15))
        read_timeout = float(cfg.get("read_timeout", 60))

        self._current_query_text = wiql

        is_link = TfsClient.is_link_query(wiql)

        log.info(
            "Запуск WIQL: type=%s, project=%r, max_items=%d, "
            "%d символов",
            "link" if is_link else "flat",
            project or "<root>", max_items, len(wiql),
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
                        wiql, project
                    )
                    return {
                        "kind": "link",
                        "relations": result.get("relations") or [],
                        "items": result.get("items") or [],
                    }
                else:
                    progress("Выполнение WIQL…")
                    items = await client.get_work_items_by_wiql(
                        wiql, project,
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
            # Приоритеты:
            #   1) текущий выбор пользователя (если был);
            #   2) проект по умолчанию из настроек;
            #   3) первый проект из списка.
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
                # Ставим первый реальный проект
                # (индекс 0 — это "— без проекта —").
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
        ):
            w.setEnabled(not busy)

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
        """
        Строит дерево на основе workItemRelations.

        Логика:
          • source == null  → корневой элемент;
          • source != null  → ребёнок элемента source;
          • target всегда — сама задача.
        """
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

        # Защита от дублей в списке детей.
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
                # В дереве нет данных — рисуем заглушку.
                placeholder = QTreeWidgetItem([
                    str(item_id), "", "(нет данных)",
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

        # Если корней не нашлось (все задачи — дети),
        # показываем все узлы верхнего уровня.
        if not root_items:
            for item_id in children_map.keys():
                item = _create(item_id)
                if item is not None:
                    root_items.append(item)

        self.tree.addTopLevelItems(root_items)

        # Автоматически раскрываем первый уровень.
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
        remaining = it.get("remaining_work") or 0
        rem_str = f"{remaining:g}" if remaining else "—"

        estimate = it.get("original_estimate") or 0
        est_str = f"{estimate:g}" if estimate else "—"

        item = QTreeWidgetItem([
            str(it.get("id") or ""),
            it.get("work_item_type") or "—",
            it.get("title") or "—",
            it.get("state") or "—",
            it.get("assigned_to") or "—",
            est_str,
            rem_str,
        ])
        item.setData(
            0, Qt.ItemDataRole.UserRole, int(it.get("id") or 0)
        )

        # Цвет статуса.
        state = (it.get("state") or "").lower()
        if "done" in state or "closed" in state \
                or "resolved" in state:
            item.setForeground(3, Qt.GlobalColor.darkGreen)
        elif "active" in state or "in progress" in state:
            item.setForeground(3, Qt.GlobalColor.darkYellow)
        elif "new" in state or "proposed" in state:
            item.setForeground(3, Qt.GlobalColor.darkBlue)

        # Приоритет — в тултип.
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
            ("Rating", _esc(it.get("rating"))),                       # ← НОВОЕ
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
            ("Effort", self._fmt_hours(it.get("effort"))),
            (
                "Target Date",
                _esc(self._short_date(it.get("target_date"))),
            ),
            (
                "Родитель",
                str(it.get("parent_id") or "—"),
            ),
            (
                "Создано",
                _esc(self._short_date(it.get("created_date"))),
            ),
            (
                "Изменено",
                _esc(self._short_date(it.get("changed_date"))),
            ),
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
        """
        '2026-10-09T12:34:56.789Z' → '2026-10-09 12:34'.

        Возвращает '—' для пустого значения.
        """
        s = (raw or "").strip()
        if not s:
            return "—"
        try:
            # Убираем миллисекунды и Z.
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
        """
        Формирует прямую ссылку на задачу в веб-интерфейсе TFS.

        Для on-premise: {base_url}/{project}/_workitems/edit/{id}
        Если проект неизвестен — {base_url}/_workitems/edit/{id}
        (TFS перенаправит).
        """
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