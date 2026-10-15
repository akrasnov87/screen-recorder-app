"""Диалоговое окно для ввода метаданных записи с библиотекой промптов.

Изменения:
  • Интерфейс переделан на вкладки: «Основное», «Документ»,
    «Промпт», «Скрам и DeepSeek», «TFS», «Зависимости», «Вложения».
  • Добавлена вкладка «Документ»: позволяет прикрепить один или
    несколько документов (.docx / .pdf / .txt / .md / .csv /
    .json / .xlsx и т.п.) и создать запись без видео.
  • Блок «TFS-выборка» вынесен в отдельную вкладку «TFS».
  • Добавлена вкладка «Зависимости» — связи с другими записями.
  • Добавлен блок «Теги» (на вкладке «Основное»).
  • Добавлен блок «Синхронизация» на вкладке «Основное».
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFileDialog, QFormLayout, QFrame,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy,
    QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from ..file_readers import read_json_file
from ..logger import get_logger
from ..relations_manager import (
    DEFAULT_RELATION_KIND,
    RELATION_KINDS,
    list_all_sessions_except,
)
from ..tfs_client import extract_variables
from .tooltips import attach_tooltip, make_info_icon, with_info

log = get_logger(__name__)


# Расширения, которые можно прикреплять на вкладке «Документ».
_DOCUMENT_EXTS = (
    ".docx", ".doc", ".pdf", ".txt", ".md",
    ".csv", ".json", ".xlsx", ".xls",
    ".rtf", ".odt", ".pptx",
)


def format_name_template(
    template: str,
    name: str,
    abbr: str = "",
    now: Optional[datetime] = None,
) -> str:
    if not template:
        return (name or "").strip()
    now = now or datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H-%M")
    datetime_str = f"{date_str} {time_str}"

    result = template
    replacements = {
        "{name}": name or "",
        "{название}": name or "",
        "{abbr}": abbr or "",
        "{сокр}": abbr or "",
        "{date}": date_str,
        "{дата}": date_str,
        "{time}": time_str,
        "{время}": time_str,
        "{datetime}": datetime_str,
    }
    for key, value in replacements.items():
        result = result.replace(key, value)

    result = (
        result.replace("()", "")
        .replace("( )", "")
        .replace("()", "")
    )
    while "  " in result:
        result = result.replace("  ", " ")
    return result.strip()


# ===========================================================================
# Диалог выбора связанных записей
# ===========================================================================
class _RelationPickerDialog(QDialog):
    """
    Диалог выбора одной или нескольких записей для связи.

    Возвращает:
      • selected_sessions() — список выбранных dict'ов
        (session_dir, session_id, name, project, date);
      • selected_kind() — вид связи (related / parent / child).
    """

    def __init__(
        self,
        candidates: List[Dict[str, Any]],
        already_linked: Optional[set] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Выбор связанных записей")
        self.setModal(True)
        self.setMinimumSize(820, 620)

        self._candidates = list(candidates or [])
        self._already = set(already_linked or set())
        self._selected: List[Dict[str, Any]] = []
        self._check_items: List[Tuple[QTableWidgetItem, Dict[str, Any]]] = []

        self._build_ui()
        self._apply_filter()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        info = QLabel(
            "Отметьте одну или несколько записей. Уже связанные "
            "записи показаны серым и недоступны для выбора."
        )
        info.setWordWrap(True)
        info.setStyleSheet("QLabel { color: #666; }")
        root.addWidget(info)

        # --- Фильтры ---
        filters = QHBoxLayout()

        filters.addWidget(QLabel("Вид связи:"))
        self.kind_combo = QComboBox()
        for key, label in RELATION_KINDS.items():
            self.kind_combo.addItem(label, key)
        self.kind_combo.setCurrentIndex(
            list(RELATION_KINDS.keys()).index(DEFAULT_RELATION_KIND)
        )
        attach_tooltip(self.kind_combo, "meta_relations_kind")
        filters.addWidget(self.kind_combo)

        filters.addSpacing(12)
        filters.addWidget(QLabel("Поиск:"))
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(
            "Подстрока в названии, проекте, дате…"
        )
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self._apply_filter)
        filters.addWidget(self.search_input, 1)

        root.addLayout(filters)

        # --- Таблица ---
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels([
            "✓", "Дата", "Название", "Проект", "Папка",
        ])
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
        )
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
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
        hv.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        root.addWidget(self.table, 1)

        # --- Счётчик ---
        self.count_label = QLabel("Выбрано: 0")
        self.count_label.setStyleSheet(
            "QLabel { color: #444; font-weight: bold; }"
        )
        root.addWidget(self.count_label)

        # --- Кнопки ---
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.button(
            QDialogButtonBox.StandardButton.Ok
        ).setText("Добавить")
        buttons.button(
            QDialogButtonBox.StandardButton.Cancel
        ).setText("Отмена")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _apply_filter(self) -> None:
        query = (self.search_input.text() or "").strip().lower()

        self.table.setRowCount(0)
        self._check_items = []

        for c in self._candidates:
            haystack = " ".join([
                c.get("name") or "",
                c.get("project") or "",
                c.get("date") or "",
                c.get("session_id") or "",
            ]).lower()
            if query and query not in haystack:
                continue

            row = self.table.rowCount()
            self.table.insertRow(row)

            already = c["session_id"] in self._already
            check_item = QTableWidgetItem()
            check_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsUserCheckable
            )
            if already:
                check_item.setCheckState(Qt.CheckState.Checked)
                check_item.setFlags(Qt.ItemFlag.NoItemFlags)
            else:
                check_item.setCheckState(Qt.CheckState.Unchecked)
            check_item.stateChanged.connect(self._update_count)
            self.table.setItem(row, 0, check_item)

            self.table.setItem(
                row, 1, QTableWidgetItem(c.get("date") or "—")
            )
            self.table.setItem(
                row, 2,
                QTableWidgetItem(c.get("name") or "—"),
            )
            self.table.setItem(
                row, 3,
                QTableWidgetItem(c.get("project") or "—"),
            )
            folder_item = QTableWidgetItem(c["session_id"])
            folder_item.setToolTip(c["session_dir"])
            self.table.setItem(row, 4, folder_item)

            if already:
                for col in range(5):
                    cell = self.table.item(row, col)
                    if cell:
                        cell.setForeground(Qt.GlobalColor.gray)
                        cell.setToolTip("Уже связано")
            else:
                self._check_items.append((check_item, c))

        self._update_count()

    def _update_count(self) -> None:
        n = sum(
            1 for ci, _ in self._check_items
            if ci.checkState() == Qt.CheckState.Checked
        )
        self.count_label.setText(f"Выбрано: {n}")

    def _on_accept(self) -> None:
        self._selected = [
            c for ci, c in self._check_items
            if ci.checkState() == Qt.CheckState.Checked
        ]
        if not self._selected:
            QMessageBox.information(
                self, "Связи",
                "Не выбрано ни одной записи.",
            )
            return
        self.accept()

    def selected_sessions(self) -> List[Dict[str, Any]]:
        return list(self._selected)

    def selected_kind(self) -> str:
        return self.kind_combo.currentData() or DEFAULT_RELATION_KIND


# ===========================================================================
# Основной диалог метаданных
# ===========================================================================
class MetadataDialog(QDialog):
    """
    Окно ввода метаданных.

    Вкладки:
      • Основное
      • Документ (только в режиме allow_document_only)
      • Промпт
      • Скрам и DeepSeek
      • TFS
      • Зависимости
      • Вложения

    Атрибут result_data содержит поле document_only=True,
    если пользователь создаёт запись-документ (без видео).
    """

    def __init__(
        self,
        projects: List[str],
        prompts: List[Dict[str, str]],
        title: str = "Метаданные записи",
        initial: Optional[Dict[str, Any]] = None,
        default_prompt: str = "",
        sessions_root: str = "",
        on_save_prompt: Optional[Callable[[str, str], None]] = None,
        get_prompts: Optional[
            Callable[[], List[Dict[str, str]]]
        ] = None,
        name_templates: Optional[List[Dict[str, str]]] = None,
        on_save_name_template: Optional[
            Callable[[str, str], None]
        ] = None,
        get_name_templates: Optional[
            Callable[[], List[Dict[str, str]]]
        ] = None,
        tags: Optional[List[Dict[str, str]]] = None,
        on_save_tag: Optional[Callable[[str, str], None]] = None,
        get_tags: Optional[
            Callable[[], List[Dict[str, str]]]
        ] = None,
        categories: Optional[List[Dict[str, str]]] = None,
        tfs_settings: Optional[Dict[str, Any]] = None,
        tfs_templates: Optional[List[Dict[str, str]]] = None,
        on_request_tfs_projects: Optional[Callable[[], List[str]]] = None,
        default_tfs_save_attachment: bool = True,
        allow_document_only: bool = False,
        relations_max_in_dialog: int = 0,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(820)
        self.setMinimumHeight(620)

        self._projects = projects or []
        self._prompts = list(prompts or [])
        self._initial = initial or {}
        self._default_prompt = default_prompt or ""
        self._sessions_root = sessions_root
        self._on_save_prompt_cb = on_save_prompt
        self._get_prompts_cb = get_prompts

        self._name_templates: List[Dict[str, str]] = list(
            name_templates or []
        )
        self._on_save_name_template_cb = on_save_name_template
        self._get_name_templates_cb = get_name_templates

        self._tags: List[Dict[str, str]] = list(tags or [])
        self._on_save_tag_cb = on_save_tag
        self._get_tags_cb = get_tags
        self._selected_tags: List[str] = []

        # --- Категории ---
        self._categories: List[Dict[str, str]] = list(
            categories or []
        )

        self._tfs_settings: Dict[str, Any] = dict(tfs_settings or {})
        self._tfs_templates: List[Dict[str, str]] = list(
            tfs_templates or []
        )
        self._on_request_tfs_projects = on_request_tfs_projects
        self._default_tfs_save_attachment = bool(
            default_tfs_save_attachment
        )

        self._tfs_variable_values: Dict[str, str] = {}
        self._tfs_variable_inputs: Dict[str, QLineEdit] = {}

        # --- Документы ---
        self._allow_document_only = bool(allow_document_only)
        self._documents: List[str] = []

        # --- Связи ---
        self._relations: List[Dict[str, str]] = []
        self._relations_max_in_dialog = int(
            relations_max_in_dialog or 0
        )

        self.result_data: Dict[str, Any] = {}
        self._prompt_edited = False
        self._attachments: List[str] = []

        log.debug(
            "MetadataDialog: title=%r, allow_document_only=%s, "
            "projects=%d, categories=%d, prompts=%d, tags=%d, "
            "tfs_templates=%d, relations_max_in_dialog=%d",
            title, self._allow_document_only,
            len(self._projects), len(self._categories),
            len(self._prompts), len(self._tags),
            len(self._tfs_templates),
            self._relations_max_in_dialog,
        )

        self._build_ui()
        self._apply_initial()
        self._connect_signals()
        self._refresh_name_preview()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        self.tabs.addTab(self._build_main_tab(), "Основное")

        if self._allow_document_only:
            self.tabs.addTab(
                self._build_document_tab(), "Документ"
            )

        self.tabs.addTab(self._build_prompt_tab(), "Промпт")
        self.tabs.addTab(
            self._build_scrum_tab(), "Скрам и DeepSeek"
        )
        self.tabs.addTab(self._build_tfs_tab(), "TFS")
        # --- НОВОЕ: вкладка «Зависимости» ---
        self.tabs.addTab(
            self._build_relations_tab(), "Зависимости"
        )
        self.tabs.addTab(
            self._build_attachments_tab(), "Вложения"
        )

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.button(
            QDialogButtonBox.StandardButton.Ok
        ).setText("Продолжить")
        buttons.button(
            QDialogButtonBox.StandardButton.Cancel
        ).setText("Пропустить")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self._on_reject)
        root.addWidget(buttons)

    # ------------------------------------------------------------------
    # Вкладка «Основное» (без изменений)
    # ------------------------------------------------------------------
    def _build_main_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        info = QLabel(
            "Базовая информация о записи: проект, название, "
            "комментарий, теги и готовность к синхронизации."
        )
        info.setWordWrap(True)
        info.setStyleSheet("QLabel { color: #666; }")
        layout.addWidget(info)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        # --- Строка «Проект + Категория» ---
        project_category_row = QWidget()
        pc_layout = QHBoxLayout(project_category_row)
        pc_layout.setContentsMargins(0, 0, 0, 0)
        pc_layout.setSpacing(6)

        # Проект
        self.project_combo = QComboBox()
        self.project_combo.setEditable(True)
        self.project_combo.addItems(self._projects)
        self.project_combo.setCurrentIndex(-1)
        self.project_combo.setMinimumWidth(220)
        pc_layout.addWidget(
            with_info(self.project_combo, "meta_project"), 2
        )

        pc_layout.addWidget(QLabel("Категория:"), 0)

        # Категория
        self.category_combo = QComboBox()
        self.category_combo.setEditable(True)
        self.category_combo.addItem("— не указана —", "")
        for c in self._categories:
            name = (c.get("name") or "").strip()
            if name:
                self.category_combo.addItem(name, name)
        self.category_combo.setMinimumWidth(180)
        pc_layout.addWidget(
            with_info(self.category_combo, "meta_category"), 1
        )

        form.addRow("Проект / Категория:", project_category_row)

        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText(
            "Например: Совещание по проекту X"
        )

        self.name_combo = QComboBox()
        self.name_combo.setEditable(False)
        self.name_combo.setMinimumWidth(220)
        self._populate_name_templates()

        self.name_abbr_input = QLineEdit()
        self.name_abbr_input.setPlaceholderText("Сокр.")
        self.name_abbr_input.setMaximumWidth(100)

        name_row = QWidget()
        name_row_layout = QHBoxLayout(name_row)
        name_row_layout.setContentsMargins(0, 0, 0, 0)
        name_row_layout.setSpacing(6)
        name_row_layout.addWidget(self.name_input, 3)
        name_row_layout.addWidget(QLabel("Шаблон:"), 0)
        name_row_layout.addWidget(self.name_combo, 2)
        name_row_layout.addWidget(QLabel("Сокр.:"), 0)
        name_row_layout.addWidget(self.name_abbr_input, 0)
        name_icon = make_info_icon("meta_name")
        if name_icon is not None:
            name_row_layout.addWidget(name_icon, 0)

        form.addRow("Название:", name_row)

        self.name_preview_label = QLabel("")
        self.name_preview_label.setStyleSheet(
            "QLabel { color: #666; font-style: italic; }"
        )
        self.name_preview_label.setWordWrap(True)

        self.save_name_tpl_btn = QPushButton(
            "Сохранить как шаблон…"
        )
        self.save_name_tpl_btn.setToolTip(
            "Сохранить текущий текст как новый шаблон.\n"
            "Плейсхолдеры: {name} {abbr} {date} {time} {datetime}"
        )
        self.save_name_tpl_btn.clicked.connect(
            self._on_save_name_template
        )

        name_hint_row = QHBoxLayout()
        name_hint_row.addWidget(self.name_preview_label, 1)
        name_hint_row.addWidget(self.save_name_tpl_btn, 0)
        form.addRow("", self._wrap(name_hint_row))

        self.comment_input = QPlainTextEdit()
        self.comment_input.setPlaceholderText(
            "Свободный комментарий…"
        )
        self.comment_input.setFixedHeight(70)
        form.addRow(
            "Комментарий:",
            with_info(self.comment_input, "meta_comment"),
        )

        from PySide6.QtCore import QDate, QTime
        from PySide6.QtWidgets import QDateEdit, QTimeEdit

        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.date_edit.setDate(QDate.currentDate())
        self.date_edit.setToolTip(
            "Дата, к которой относится запись.\n\n"
            "По умолчанию — сегодняшняя дата. Измените, если "
            "запись сделана не сегодня."
        )

        self.time_edit = QTimeEdit()
        self.time_edit.setDisplayFormat("HH:mm:ss")
        self.time_edit.setTime(QTime.currentTime())
        self.time_edit.setToolTip(
            "Время, к которому относится запись.\n\n"
            "По умолчанию — текущее время."
        )

        self.date_time_now_btn = QPushButton("Сейчас")
        self.date_time_now_btn.setToolTip(
            "Установить текущие дату и время."
        )
        self.date_time_now_btn.clicked.connect(
            self._on_set_now_datetime
        )

        dt_row = QWidget()
        dt_layout = QHBoxLayout(dt_row)
        dt_layout.setContentsMargins(0, 0, 0, 0)
        dt_layout.setSpacing(6)
        dt_layout.addWidget(QLabel("Дата:"), 0)
        dt_layout.addWidget(self.date_edit, 1)
        dt_layout.addWidget(QLabel("Время:"), 0)
        dt_layout.addWidget(self.time_edit, 1)
        dt_layout.addWidget(self.date_time_now_btn, 0)
        dt_icon = make_info_icon("meta_datetime")
        if dt_icon is not None:
            dt_layout.addWidget(dt_icon, 0)

        form.addRow("Дата и время:", dt_row)

        layout.addLayout(form)

        layout.addWidget(self._build_tags_section())
        layout.addWidget(self._build_sync_ready_section())

        layout.addStretch()
        return w

    @staticmethod
    def _wrap(layout) -> QWidget:
        w = QWidget()
        w.setLayout(layout)
        return w

    def _on_set_now_datetime(self) -> None:
        from PySide6.QtCore import QDate, QTime

        if not hasattr(self, "date_edit") or not hasattr(
            self, "time_edit"
        ):
            log.warning(
                "MetadataDialog._on_set_now_datetime вызван до "
                "создания полей date_edit/time_edit"
            )
            return

        self.date_edit.setDate(QDate.currentDate())
        self.time_edit.setTime(QTime.currentTime())

    # ------------------------------------------------------------------
    # Вкладка «Документ» (без изменений)
    # ------------------------------------------------------------------
    def _build_document_tab(self) -> QWidget:
        """Вкладка «Документ» — прикрепить один или несколько
        документов вместо видео."""
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        info_row = QHBoxLayout()
        info = QLabel(
            "Создайте запись без видео — только из документов.<br><br>"
            "Что можно прикрепить:\n"
            "  • Word: <code>.docx</code>, <code>.doc</code>, "
            "<code>.odt</code>, <code>.rtf</code>;\n"
            "  • PDF: <code>.pdf</code>;\n"
            "  • текст: <code>.txt</code>, <code>.md</code>;\n"
            "  • таблицы: <code>.csv</code>, <code>.xlsx</code>, "
            "<code>.xls</code>;\n"
            "  • структурированное: <code>.json</code>;\n"
            "  • презентации: <code>.pptx</code>.<br><br>"
            "При создании записи файлы копируются в "
            "<code>attachments/</code>, а их текст используется "
            "как «стенограмма» — для формирования summary и "
            "DeepSeek-промпта."
        )
        info.setWordWrap(True)
        info.setStyleSheet("QLabel { color: #666; }")
        info_row.addWidget(info, 1)

        icon = make_info_icon("meta_document_tab_intro")
        if icon is not None:
            info_row.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)

        layout.addLayout(info_row)

        header = QHBoxLayout()
        header.addWidget(QLabel("<b>Прикреплённые документы</b>"))
        header.addStretch()
        hint_icon = make_info_icon("meta_documents")
        if hint_icon is not None:
            header.addWidget(hint_icon)
        layout.addLayout(header)

        hint = QLabel(
            "<span style='color:#666'>Отметьте один или несколько "
            "файлов. Порядок в списке сохраняется.</span>"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.documents_list = QListWidget()
        self.documents_list.setMinimumHeight(200)
        self.documents_list.setSelectionMode(
            QListWidget.SelectionMode.ExtendedSelection
        )
        self.documents_list.setAlternatingRowColors(True)
        attach_tooltip(self.documents_list, "meta_documents_list")
        layout.addWidget(self.documents_list, 1)

        doc_btns = QHBoxLayout()

        self.add_document_btn = QPushButton("Добавить файлы…")
        self.add_document_btn.setToolTip(
            "Выбрать один или несколько документов."
        )
        self.add_document_btn.clicked.connect(
            self._on_add_document
        )
        doc_btns.addWidget(self.add_document_btn)

        self.remove_document_btn = QPushButton("Удалить выбранные")
        self.remove_document_btn.setToolTip(
            "Убрать выбранные документы из записи."
        )
        self.remove_document_btn.clicked.connect(
            self._on_remove_document
        )
        doc_btns.addWidget(self.remove_document_btn)

        self.move_up_doc_btn = QPushButton("Вверх")
        self.move_up_doc_btn.clicked.connect(
            lambda: self._move_document(-1)
        )
        doc_btns.addWidget(self.move_up_doc_btn)

        self.move_down_doc_btn = QPushButton("Вниз")
        self.move_down_doc_btn.clicked.connect(
            lambda: self._move_document(1)
        )
        doc_btns.addWidget(self.move_down_doc_btn)

        doc_btns.addStretch()

        self.documents_count_label = QLabel("Документов: 0")
        self.documents_count_label.setStyleSheet(
            "QLabel { color: #444; font-weight: bold; }"
        )
        doc_btns.addWidget(self.documents_count_label)

        layout.addLayout(doc_btns)

        layout.addWidget(self._build_document_options_section())

        return w

    def _build_document_options_section(self) -> QWidget:
        """
        Блок «Что делать с документами» на вкладке «Документ».

        Значения по умолчанию подобраны под сценарий
        «запись-документ» (без видео):
          • Сохранить извлечённый текст как video.txt — ВКЛ.
            (это основа для последующих шагов: поиска,
            TFS-выборки, экспорта протоколов и т.д.)
          • Передать извлечённый текст в промпт DeepSeek
            как вложение — ВКЛ.
            (пользователь обычно хочет, чтобы содержимое
            документов попало в промпт DeepSeek.)
          • Формировать summary из документов — ВЫКЛ.
            (тяжёлая операция; пользователь включает её
            осознанно, если ему действительно нужна
            краткая выжимка.)
          • Формировать файл промпта для DeepSeek — ВЫКЛ.
            (это отдельный шаг обработки, не всем нужен;
            включается вручную.)
        """
        box = QFrame()
        box.setFrameShape(QFrame.Shape.StyledPanel)
        box.setStyleSheet(
            "QFrame {"
            "  background-color: #FAFAFA;"
            "  border: 1px solid #DDD;"
            "  border-radius: 4px;"
            "}"
        )
        layout = QVBoxLayout(box)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.addWidget(
            QLabel("<b>Что делать с документами</b>")
        )
        header.addStretch()
        icon = make_info_icon("meta_documents_actions")
        if icon is not None:
            header.addWidget(icon)
        layout.addLayout(header)

        hint = QLabel(
            "<span style='color:#666'>Текст документов будет "
            "извлечён и использован так же, как стенограмма."
            "</span>"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # --- Формировать summary из документов ---
        # По умолчанию ВЫКЛ: тяжёлая операция, включается
        # пользователем осознанно.
        self.doc_generate_summary_check = QCheckBox(
            "Сформировать summary из документов"
        )
        attach_tooltip(
            self.doc_generate_summary_check,
            "meta_doc_generate_summary",
        )
        self.doc_generate_summary_check.setChecked(False)
        layout.addWidget(self.doc_generate_summary_check)

        # --- Формировать файл промпта для DeepSeek ---
        # По умолчанию ВЫКЛ: отдельный шаг обработки,
        # не всем нужен.
        self.doc_generate_deepseek_check = QCheckBox(
            "Сформировать файл промпта для DeepSeek"
        )
        attach_tooltip(
            self.doc_generate_deepseek_check,
            "meta_doc_generate_deepseek",
        )
        self.doc_generate_deepseek_check.setChecked(False)
        layout.addWidget(self.doc_generate_deepseek_check)

        # --- Сохранить извлечённый текст как video.txt ---
        # По умолчанию ВКЛ: это базовая операция, дающая
        # возможность работать с документом так же, как
        # с обычной записью.
        self.doc_include_text_as_transcript_check = QCheckBox(
            "Сохранить извлечённый текст как «стенограмму» "
            "(video.txt)"
        )
        attach_tooltip(
            self.doc_include_text_as_transcript_check,
            "meta_doc_save_transcript",
        )
        self.doc_include_text_as_transcript_check.setChecked(True)
        layout.addWidget(
            self.doc_include_text_as_transcript_check
        )

        # --- Передать извлечённый текст в промпт DeepSeek ---
        # По умолчанию ВКЛ: пользователь, как правило, хочет,
        # чтобы содержимое документов попало в DeepSeek-промпт
        # (это и есть смысл «записи из документов»).
        self.doc_send_to_deepseek_check = QCheckBox(
            "Передать извлечённый текст в промпт DeepSeek "
            "как вложение"
        )
        attach_tooltip(
            self.doc_send_to_deepseek_check,
            "meta_doc_to_deepseek",
        )
        self.doc_send_to_deepseek_check.setChecked(True)
        layout.addWidget(self.doc_send_to_deepseek_check)

        return box

    # ------------------------------------------------------------------
    # Работа с документами (без изменений)
    # ------------------------------------------------------------------
    def _refresh_documents_list(self) -> None:
        if not hasattr(self, "documents_list"):
            return
        self.documents_list.clear()
        for p in self._documents:
            name = os.path.basename(p)
            it = QListWidgetItem(name)
            it.setToolTip(p)
            it.setData(Qt.ItemDataRole.UserRole, p)
            self.documents_list.addItem(it)

        if hasattr(self, "documents_count_label"):
            self.documents_count_label.setText(
                f"Документов: {len(self._documents)}"
            )

    def _on_add_document(self) -> None:
        patterns = " ".join(f"*{ext}" for ext in _DOCUMENT_EXTS)
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Выберите документы",
            os.path.expanduser("~"),
            f"Документы ({patterns});;Все файлы (*)",
        )
        if not files:
            return
        added = 0
        for f in files:
            if f and f not in self._documents:
                self._documents.append(f)
                added += 1
        if added:
            log.info(
                "Добавлено документов: %d (всего %d)",
                added, len(self._documents),
            )
            self._refresh_documents_list()

    def _on_remove_document(self) -> None:
        items = self.documents_list.selectedItems()
        if not items:
            return
        to_remove = {
            it.data(Qt.ItemDataRole.UserRole) for it in items
        }
        self._documents = [
            p for p in self._documents if p not in to_remove
        ]
        self._refresh_documents_list()

    def _move_document(self, delta: int) -> None:
        row = self.documents_list.currentRow()
        if row < 0:
            return
        new_row = row + delta
        if new_row < 0 or new_row >= len(self._documents):
            return
        self._documents[row], self._documents[new_row] = (
            self._documents[new_row], self._documents[row]
        )
        self._refresh_documents_list()
        self.documents_list.setCurrentRow(new_row)

    # ------------------------------------------------------------------
    # Блок тегов (без изменений)
    # ------------------------------------------------------------------
    def _build_tags_section(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(4)

        header = QHBoxLayout()
        header.addWidget(QLabel("<b>Теги</b>"))
        header.addStretch()
        icon = make_info_icon("meta_tags")
        if icon is not None:
            header.addWidget(icon)
        layout.addLayout(header)

        hint = QLabel(
            "<span style='color:#666'>Отметьте один или несколько "
            "тегов из справочника.</span>"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.tags_list = QListWidget()
        self.tags_list.setMinimumHeight(110)
        self.tags_list.setSelectionMode(
            QListWidget.SelectionMode.NoSelection
        )
        self.tags_list.setAlternatingRowColors(True)
        attach_tooltip(self.tags_list, "meta_tags_list")
        self._populate_tags_list()
        layout.addWidget(self.tags_list)

        btns = QHBoxLayout()
        self.add_tag_btn = QPushButton("Добавить новый тег…")
        self.add_tag_btn.setToolTip(
            "Добавить тег, которого нет в справочнике."
        )
        self.add_tag_btn.clicked.connect(self._on_add_new_tag)
        btns.addWidget(self.add_tag_btn)

        self.clear_tags_btn = QPushButton("Снять все")
        self.clear_tags_btn.clicked.connect(self._on_clear_tags)
        btns.addWidget(self.clear_tags_btn)

        btns.addStretch()

        self.tags_count_label = QLabel("")
        self.tags_count_label.setStyleSheet(
            "QLabel { color: #444; font-weight: bold; }"
        )
        btns.addWidget(self.tags_count_label)

        layout.addLayout(btns)

        self._update_tags_count()
        return box

    # ------------------------------------------------------------------
    # Блок «Готовность к синхронизации» (без изменений)
    # ------------------------------------------------------------------
    def _build_sync_ready_section(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(4)

        header = QHBoxLayout()
        header.addWidget(QLabel("<b>Синхронизация</b>"))
        header.addStretch()
        icon = make_info_icon("meta_sync_ready")
        if icon is not None:
            header.addWidget(icon)
        layout.addLayout(header)

        hint = QLabel(
            "<span style='color:#666'>Пока галочка не стоит — "
            "запись считается черновиком.</span>"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.sync_ready_check = QCheckBox(
            "Готово к синхронизации с сервером"
        )
        attach_tooltip(self.sync_ready_check, "meta_sync_ready")
        layout.addWidget(self.sync_ready_check)

        return box

    # ------------------------------------------------------------------
    # Теги (без изменений)
    # ------------------------------------------------------------------
    def _populate_tags_list(self) -> None:
        if not hasattr(self, "tags_list"):
            return
        self.tags_list.blockSignals(True)
        self.tags_list.clear()

        selected = set(self._selected_tags)

        for tag in self._tags:
            name = tag.get("name") or ""
            if not name:
                continue
            item = QListWidgetItem(name)
            item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsUserCheckable
            )
            item.setCheckState(
                Qt.CheckState.Checked if name in selected
                else Qt.CheckState.Unchecked
            )
            item.setData(Qt.ItemDataRole.UserRole, name)

            color = (tag.get("color") or "").strip()
            if color:
                try:
                    qcolor = QColor(color)
                    if qcolor.isValid():
                        item.setForeground(qcolor)
                        item.setBackground(
                            QColor(
                                qcolor.red(), qcolor.green(),
                                qcolor.blue(), 30,
                            )
                        )
                except Exception:
                    pass

            self.tags_list.addItem(item)

        self.tags_list.blockSignals(False)
        self._update_tags_count()

    def _refresh_tags_list(
        self, selected_names: Optional[List[str]] = None,
    ) -> None:
        if self._get_tags_cb is not None:
            try:
                fresh = self._get_tags_cb() or []
                if isinstance(fresh, list):
                    self._tags = fresh
            except Exception as exc:
                log.warning(
                    "Не удалось получить справочник тегов: %s", exc
                )

        if selected_names is not None:
            self._selected_tags = list(selected_names)

        self._populate_tags_list()

    def _update_tags_count(self) -> None:
        if not hasattr(self, "tags_count_label"):
            return
        n = len(self._selected_tags)
        if n == 0:
            self.tags_count_label.setText("Теги не выбраны")
        elif n == 1:
            self.tags_count_label.setText(
                f"Выбран: {self._selected_tags[0]}"
            )
        else:
            self.tags_count_label.setText(f"Выбрано: {n}")

    def _on_tag_item_changed(self, item: QListWidgetItem) -> None:
        name = item.data(Qt.ItemDataRole.UserRole) or ""
        if not name:
            return
        if item.checkState() == Qt.CheckState.Checked:
            if name not in self._selected_tags:
                self._selected_tags.append(name)
        else:
            self._selected_tags = [
                t for t in self._selected_tags if t != name
            ]
        self._update_tags_count()

    def _on_add_new_tag(self) -> None:
        name, ok = QInputDialog.getText(
            self,
            "Новый тег",
            "Имя тега (например, «важное», «риски», "
            "«для клиента»):",
        )
        if not ok:
            return
        name = (name or "").strip()
        if not name:
            QMessageBox.warning(
                self, "Тег", "Имя тега не может быть пустым."
            )
            return

        existing = [t.get("name") for t in self._tags]
        if name in existing:
            QMessageBox.information(
                self, "Тег",
                f"Тег «{name}» уже есть в справочнике.",
            )
        else:
            if self._on_save_tag_cb is not None:
                try:
                    self._on_save_tag_cb(name, "")
                except Exception as exc:
                    log.exception(
                        "Ошибка сохранения тега: %s", exc
                    )
                    QMessageBox.critical(
                        self, "Тег",
                        f"Не удалось сохранить тег:\n{exc}",
                    )
                    return
            else:
                self._tags.append({"name": name, "color": ""})

        if name not in self._selected_tags:
            self._selected_tags.append(name)

        self._refresh_tags_list(
            selected_names=list(self._selected_tags)
        )

    def _on_clear_tags(self) -> None:
        self._selected_tags = []
        self._populate_tags_list()

    # ==================================================================
    # Вкладка «Зависимости» (НОВАЯ)
    # ==================================================================
    def _build_relations_tab(self) -> QWidget:
        """
        Вкладка «Зависимости» — связи с другими записями.

        Позволяет выбрать одну или несколько записей, указать
        вид связи (related / parent / child) и сохранить их.
        """
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        info_row = QHBoxLayout()
        info = QLabel(
            "Свяжите эту запись с другими записями.<br><br>"
            "<b>Виды связи:</b><br>"
            "  • <b>Связан</b> — простая двусторонняя связь "
            "(по умолчанию);<br>"
            "  • <b>Родитель</b> — эта запись является "
            "родительской для указанной;<br>"
            "  • <b>Дочерний</b> — эта запись является "
            "дочерней для указанной.<br><br>"
            "Связанные записи можно открыть через кнопку "
            "<b>«Связи»</b> в окне «Записи».<br><br>"
            "<span style='color:#888'>Связи сохраняются в "
            "session.json и передаются на сервер синхронизации. "
            "При публикации вместе с локальным session_id "
            "сохраняется record_id с сервера — связь не "
            "потеряется при переименовании папок.</span>"
        )
        info.setWordWrap(True)
        info.setStyleSheet("QLabel { color: #666; }")
        info_row.addWidget(info, 1)
        icon = make_info_icon("meta_relations_intro")
        if icon is not None:
            info_row.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(info_row)

        header = QHBoxLayout()
        header.addWidget(QLabel("<b>Связанные записи</b>"))
        header.addStretch()
        layout.addLayout(header)

        # --- Таблица связей ---
        self.relations_table = QTableWidget(0, 4)
        self.relations_table.setHorizontalHeaderLabels([
            "Тип связи", "Название", "Проект", "Папка",
        ])
        self.relations_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.relations_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.relations_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        hv = self.relations_table.horizontalHeader()
        hv.setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        hv.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hv.setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        hv.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.relations_table.setMinimumHeight(220)
        self.relations_table.itemDoubleClicked.connect(
            lambda _it: self._on_edit_relation_kind()
        )
        layout.addWidget(self.relations_table)

        # --- Кнопки ---
        btns = QHBoxLayout()

        self.relation_add_btn = QPushButton("Добавить связь…")
        attach_tooltip(self.relation_add_btn, "meta_relations_add")
        self.relation_add_btn.clicked.connect(
            self._on_add_relation
        )
        btns.addWidget(self.relation_add_btn)

        self.relation_kind_btn = QPushButton("Изменить тип")
        attach_tooltip(self.relation_kind_btn, "meta_relations_kind")
        self.relation_kind_btn.clicked.connect(
            self._on_edit_relation_kind
        )
        btns.addWidget(self.relation_kind_btn)

        self.relation_remove_btn = QPushButton("Удалить")
        attach_tooltip(
            self.relation_remove_btn, "meta_relations_remove"
        )
        self.relation_remove_btn.clicked.connect(
            self._on_remove_relation
        )
        btns.addWidget(self.relation_remove_btn)

        btns.addStretch()

        self.relations_count_label = QLabel("Связей: 0")
        self.relations_count_label.setStyleSheet(
            "QLabel { color: #444; font-weight: bold; }"
        )
        btns.addWidget(self.relations_count_label)

        layout.addLayout(btns)
        layout.addStretch()

        return w

    # ------------------------------------------------------------------
    # Операции со связями
    # ------------------------------------------------------------------
    def _refresh_relations_table(self) -> None:
        if not hasattr(self, "relations_table"):
            return
        self.relations_table.setRowCount(0)

        for rel in self._relations:
            row = self.relations_table.rowCount()
            self.relations_table.insertRow(row)

            kind_label = RELATION_KINDS.get(
                rel["kind"], rel["kind"]
            )
            kind_item = QTableWidgetItem(kind_label)
            kind_item.setData(
                Qt.ItemDataRole.UserRole, rel["kind"]
            )
            self.relations_table.setItem(row, 0, kind_item)

            name = rel.get("target_name") or ""
            if not name:
                tdir = rel.get("target_dir") or ""
                if tdir and os.path.isdir(tdir):
                    meta = read_json_file(
                        os.path.join(tdir, "session.json")
                    ) or {}
                    name = str(meta.get("name") or "").strip()
                if not name:
                    name = (
                        rel.get("target_session_id")
                        or os.path.basename(tdir or "")
                        or rel.get("target_record_id")
                        or "(без названия)"
                    )
            name_item = QTableWidgetItem(name)
            name_item.setToolTip(name)
            self.relations_table.setItem(row, 1, name_item)

            project = ""
            tdir = rel.get("target_dir") or ""
            if tdir and os.path.isdir(tdir):
                meta = read_json_file(
                    os.path.join(tdir, "session.json")
                ) or {}
                project = str(meta.get("project") or "")
            self.relations_table.setItem(
                row, 2, QTableWidgetItem(project or "—")
            )

            folder_label = (
                rel.get("target_session_id")
                or rel.get("target_record_id")
                or "—"
            )
            folder_item = QTableWidgetItem(folder_label)
            folder_item.setToolTip(
                rel.get("target_dir") or "(папка не найдена)"
            )
            self.relations_table.setItem(row, 3, folder_item)

        self.relations_count_label.setText(
            f"Связей: {len(self._relations)}"
        )

    def _on_add_relation(self) -> None:
        if not self._sessions_root or not os.path.isdir(
            self._sessions_root
        ):
            QMessageBox.warning(
                self, "Связи",
                "Папка сессий не найдена — не из чего выбирать.",
            )
            return

        current_dir = str(
            self._initial.get("session_dir") or ""
        ).strip()

        candidates = list_all_sessions_except(
            self._sessions_root, current_dir
        )

        # --- Лимит из настроек ---
        limit = self._relations_max_in_dialog
        if limit > 0 and len(candidates) > limit:
            log.info(
                "Связи: список кандидатов урезан с %d до %d "
                "(app.relations_max_in_dialog)",
                len(candidates), limit,
            )
            candidates = candidates[:limit]

        if not candidates:
            QMessageBox.information(
                self, "Связи",
                "В архиве нет других записей для связи.",
            )
            return

        dlg = _RelationPickerDialog(
            candidates=candidates,
            already_linked={
                r.get("target_session_id") or ""
                for r in self._relations
            },
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        chosen = dlg.selected_sessions()
        if not chosen:
            return

        kind = dlg.selected_kind()
        for c in chosen:
            sid = c["session_id"]
            if any(
                r.get("target_session_id") == sid
                for r in self._relations
            ):
                continue
            self._relations.append({
                "target_session_id": sid,
                "target_dir": c["session_dir"],
                "target_name": c["name"],
                "target_record_id": c.get("record_id") or "",
                "kind": kind,
            })

        self._refresh_relations_table()
        log.info(
            "MetadataDialog: добавлено связей: %d (kind=%s)",
            len(chosen), kind,
        )

    def _on_edit_relation_kind(self) -> None:
        row = self.relations_table.currentRow()
        if row < 0 or row >= len(self._relations):
            QMessageBox.information(
                self, "Связи", "Выберите строку в таблице."
            )
            return

        rel = self._relations[row]
        current_kind = rel["kind"]

        items = list(RELATION_KINDS.values())
        keys = list(RELATION_KINDS.keys())
        try:
            current_idx = keys.index(current_kind)
        except ValueError:
            current_idx = 0

        new_label, ok = QInputDialog.getItem(
            self,
            "Вид связи",
            "Выберите вид связи:",
            items, current_idx, False,
        )
        if not ok or not new_label:
            return

        try:
            new_kind = keys[items.index(new_label)]
        except (ValueError, IndexError):
            return

        rel["kind"] = new_kind
        self._refresh_relations_table()

    def _on_remove_relation(self) -> None:
        row = self.relations_table.currentRow()
        if row < 0 or row >= len(self._relations):
            QMessageBox.information(
                self, "Связи", "Выберите строку в таблице."
            )
            return

        rel = self._relations[row]
        name = (
            rel.get("target_name")
            or rel.get("target_session_id")
            or rel.get("target_record_id")
            or ""
        )
        reply = QMessageBox.question(
            self, "Удалить связь",
            f"Удалить связь с записью «{name}»?",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        del self._relations[row]
        self._refresh_relations_table()

    # ==================================================================
    # Вкладка «Промпт» (без изменений)
    # ==================================================================
    def _build_prompt_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        info = QLabel(
            "Промпт — инструкция, которая используется при "
            "суммаризации и при формировании файла DeepSeek."
        )
        info.setWordWrap(True)
        info.setStyleSheet("QLabel { color: #666; }")
        layout.addWidget(info)

        lib_row = QHBoxLayout()
        lib_row.addWidget(QLabel("Библиотека:"))
        self.prompt_combo = QComboBox()
        self.prompt_combo.addItem("— не выбрано —", "")
        for p in self._prompts:
            self.prompt_combo.addItem(p["name"], p["text"])
        self.prompt_combo.setMinimumWidth(280)
        lib_row.addWidget(self.prompt_combo, 1)

        prompt_icon = make_info_icon("meta_prompt")
        if prompt_icon is not None:
            lib_row.addWidget(prompt_icon)
        layout.addLayout(lib_row)

        self.prompt_input = QPlainTextEdit()
        self.prompt_input.setPlaceholderText(
            "Промпт для формирования краткого содержания "
            "и/или DeepSeek-промпта…"
        )
        self.prompt_input.setMinimumHeight(160)
        layout.addWidget(self.prompt_input, 1)

        prompt_actions = QHBoxLayout()
        self.save_to_library_btn = QPushButton(
            "Сохранить как новый промпт…"
        )
        self.save_to_library_btn.clicked.connect(
            self._on_save_to_library
        )
        self.reset_prompt_btn = QPushButton("Сбросить правку")
        self.reset_prompt_btn.clicked.connect(
            self._on_reset_prompt
        )
        prompt_actions.addWidget(self.save_to_library_btn)
        prompt_actions.addWidget(self.reset_prompt_btn)
        prompt_actions.addStretch()
        layout.addLayout(prompt_actions)

        layout.addWidget(self._build_context_section())

        return w

    def _build_context_section(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(4)

        header = QHBoxLayout()
        header.addWidget(
            QLabel("<b>Контекст записи в промпте</b>")
        )
        header.addStretch()
        icon = make_info_icon("meta_context_to_prompt")
        if icon is not None:
            header.addWidget(icon)
        layout.addLayout(header)

        hint = QLabel(
            "<span style='color:#666'>Выберите, что из карточки "
            "записи добавить в промпт.</span>"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        ctx_row = QHBoxLayout()

        self.include_name_check = QCheckBox("Название записи")
        attach_tooltip(
            self.include_name_check, "meta_include_name_in_prompt"
        )
        self.include_name_check.setChecked(True)
        ctx_row.addWidget(self.include_name_check)

        self.include_project_check = QCheckBox("Проект")
        attach_tooltip(
            self.include_project_check,
            "meta_include_project_in_prompt",
        )
        self.include_project_check.setChecked(True)
        ctx_row.addWidget(self.include_project_check)

        self.include_comment_check = QCheckBox("Комментарий")
        attach_tooltip(
            self.include_comment_check,
            "meta_include_comment_in_prompt",
        )
        self.include_comment_check.setChecked(True)
        ctx_row.addWidget(self.include_comment_check)

        self.include_tags_check = QCheckBox("Теги")
        attach_tooltip(
            self.include_tags_check,
            "meta_include_tags_in_prompt",
        )
        self.include_tags_check.setChecked(True)
        ctx_row.addWidget(self.include_tags_check)

        ctx_row.addStretch()

        self.context_all_btn = QPushButton("Включить всё")
        self.context_all_btn.clicked.connect(
            self._on_context_enable_all
        )
        ctx_row.addWidget(self.context_all_btn)

        self.context_none_btn = QPushButton("Снять всё")
        self.context_none_btn.clicked.connect(
            self._on_context_disable_all
        )
        ctx_row.addWidget(self.context_none_btn)

        layout.addLayout(ctx_row)
        return box

    def _on_context_disable_all(self) -> None:
        self.include_name_check.setChecked(False)
        self.include_project_check.setChecked(False)
        self.include_comment_check.setChecked(False)
        self.include_tags_check.setChecked(False)

    def _on_context_enable_all(self) -> None:
        self.include_name_check.setChecked(True)
        self.include_project_check.setChecked(True)
        self.include_comment_check.setChecked(True)
        self.include_tags_check.setChecked(True)

    # ==================================================================
    # Вкладка «Скрам и DeepSeek» (без изменений)
    # ==================================================================
    def _build_scrum_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        info = QLabel(
            "Управление генерацией итоговых документов. "
            "Настройки TFS-выборки — на отдельной вкладке «TFS»."
        )
        info.setWordWrap(True)
        info.setStyleSheet("QLabel { color: #666; }")
        layout.addWidget(info)

        transcribe_header = QHBoxLayout()
        transcribe_header.addWidget(
            QLabel("<b>Транскрибация</b>")
        )
        transcribe_header.addStretch()
        transcribe_icon = make_info_icon("meta_skip_transcription")
        if transcribe_icon is not None:
            transcribe_header.addWidget(transcribe_icon)
        layout.addLayout(transcribe_header)

        self.skip_transcription_check = QCheckBox(
            "Выполнять транскрибацию для этой записи"
        )
        attach_tooltip(
            self.skip_transcription_check,
            "meta_skip_transcription",
        )
        self.skip_transcription_check.setChecked(True)
        layout.addWidget(self.skip_transcription_check)

        self.skip_transcription_hint = QLabel("")
        self.skip_transcription_hint.setWordWrap(True)
        self.skip_transcription_hint.setStyleSheet(
            "QLabel { color: #666; font-size: 11px; }"
        )
        layout.addWidget(self.skip_transcription_hint)

        self.skip_transcription_check.toggled.connect(
            self._on_skip_transcription_toggled
        )
        self._on_skip_transcription_toggled(
            self.skip_transcription_check.isChecked()
        )

        layout.addSpacing(8)

        summary_header = QHBoxLayout()
        summary_header.addWidget(
            QLabel("<b>Краткое содержание (summary)</b>")
        )
        summary_header.addStretch()
        summary_icon = make_info_icon("meta_generate_summary")
        if summary_icon is not None:
            summary_header.addWidget(summary_icon)
        layout.addLayout(summary_header)

        self.generate_summary_check = QCheckBox(
            "Формировать summary для этой записи"
        )
        attach_tooltip(
            self.generate_summary_check, "meta_generate_summary"
        )
        self.generate_summary_check.setChecked(False)
        layout.addWidget(self.generate_summary_check)

        deepseek_header = QHBoxLayout()
        deepseek_header.addWidget(
            QLabel("<b>Промпт для DeepSeek</b>")
        )
        deepseek_header.addStretch()
        layout.addLayout(deepseek_header)

        self.generate_deepseek_check = QCheckBox(
            "Сформировать файл промпта для DeepSeek"
        )
        attach_tooltip(
            self.generate_deepseek_check,
            "meta_generate_deepseek_prompt",
        )
        self.generate_deepseek_check.setChecked(True)
        layout.addWidget(self.generate_deepseek_check)

        scrum_header = QHBoxLayout()
        scrum_header.addWidget(QLabel("<b>Скрам-митинг</b>"))
        scrum_header.addStretch()
        scrum_icon = make_info_icon("meta_is_scrum")
        if scrum_icon is not None:
            scrum_header.addWidget(scrum_icon)
        layout.addLayout(scrum_header)

        self.is_scrum_check = QCheckBox(
            "Это скрам-митинг (использовать шаблон "
            "из Настройки → Скрам)"
        )
        attach_tooltip(self.is_scrum_check, "meta_is_scrum")
        layout.addWidget(self.is_scrum_check)

        protocol_form = QFormLayout()
        protocol_form.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight
        )

        self.protocol_path_input = QLineEdit()
        self.protocol_path_input.setReadOnly(True)
        self.protocol_path_input.setPlaceholderText(
            "Файл протокола не выбран"
        )

        self.browse_protocol_btn = QPushButton("Загрузить файл…")
        self.browse_protocol_btn.clicked.connect(
            self._on_browse_protocol
        )

        self.from_session_btn = QPushButton("Из записи…")
        self.from_session_btn.clicked.connect(
            self._on_pick_from_session
        )

        self.clear_protocol_btn = QPushButton("Сбросить")
        self.clear_protocol_btn.clicked.connect(
            self._on_clear_protocol
        )

        protocol_row = QHBoxLayout()
        protocol_row.addWidget(self.protocol_path_input, 1)
        protocol_row.addWidget(self.browse_protocol_btn)
        protocol_row.addWidget(self.from_session_btn)
        protocol_row.addWidget(self.clear_protocol_btn)
        protocol_icon = make_info_icon("meta_protocol")
        if protocol_icon is not None:
            protocol_row.addWidget(protocol_icon)

        protocol_form.addRow(
            "Предыдущий протокол:", self._wrap(protocol_row)
        )
        layout.addLayout(protocol_form)

        self._scrum_widgets = [
            self.protocol_path_input,
            self.browse_protocol_btn,
            self.from_session_btn,
            self.clear_protocol_btn,
        ]
        self.is_scrum_check.toggled.connect(
            self._update_scrum_visibility
        )
        self._update_scrum_visibility(False)

        layout.addStretch()
        return w

    # ==================================================================
    # Вкладка «TFS» (без изменений)
    # ==================================================================
    def _build_tfs_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )

        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        info_row = QHBoxLayout()
        info = QLabel(
            "Настройки выборки задач из <b>TFS / Azure DevOps "
            "Server</b>.<br><br>"
            "Если отметить «Сохранять выборку» — во время обработки "
            "записи будет выполнен WIQL-запрос, результат "
            "сохранится в <code>attachments/tfs_work_items.csv</code>."
        )
        info.setWordWrap(True)
        info.setStyleSheet("QLabel { color: #666; }")
        info_row.addWidget(info, 1)

        icon = make_info_icon("meta_tfs_tab_intro")
        if icon is not None:
            info_row.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)

        layout.addLayout(info_row)
        layout.addWidget(self._build_tfs_section())
        layout.addWidget(self._build_tfs_variables_section())
        layout.addStretch()

        scroll.setWidget(w)
        return scroll

    def _build_tfs_section(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.addWidget(QLabel("<b>TFS-выборка</b>"))
        header.addStretch()
        icon = make_info_icon("meta_tfs_section")
        if icon is not None:
            header.addWidget(icon)
        layout.addLayout(header)

        self.tfs_save_attachment_check = QCheckBox(
            "Сохранять выборку в вложения записи"
        )
        attach_tooltip(
            self.tfs_save_attachment_check,
            "meta_tfs_save_attachment",
        )
        self.tfs_save_attachment_check.setChecked(
            self._default_tfs_save_attachment
        )
        layout.addWidget(self.tfs_save_attachment_check)

        form = QFormLayout()
        form.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight
            | Qt.AlignmentFlag.AlignVCenter
        )

        project_row = QHBoxLayout()
        self.tfs_project_combo = QComboBox()
        self.tfs_project_combo.setEditable(False)
        self.tfs_project_combo.setMinimumWidth(280)
        self.tfs_project_combo.addItem("— без проекта —", "")
        project_row.addWidget(self.tfs_project_combo, 1)

        self.tfs_reload_projects_btn = QPushButton(
            "Обновить список"
        )
        self.tfs_reload_projects_btn.clicked.connect(
            lambda: self._load_tfs_projects(force=True)
        )
        project_row.addWidget(self.tfs_reload_projects_btn, 0)

        form.addRow(
            "Проект:",
            with_info(
                self._wrap(project_row), "meta_tfs_project"
            ),
        )

        self.tfs_template_combo = QComboBox()
        self.tfs_template_combo.setEditable(False)
        self.tfs_template_combo.setMinimumWidth(360)
        self.tfs_template_combo.addItem("— не выбрано —", "")

        self._populate_tfs_templates()

        form.addRow(
            "Шаблон WIQL:",
            with_info(
                self.tfs_template_combo, "meta_tfs_template"
            ),
        )

        layout.addLayout(form)

        self.tfs_wiql_preview = QLabel("")
        self.tfs_wiql_preview.setWordWrap(True)
        self.tfs_wiql_preview.setTextFormat(
            Qt.TextFormat.PlainText
        )
        self.tfs_wiql_preview.setStyleSheet(
            "QLabel {"
            "  background-color: #f5f5f5;"
            "  border: 1px solid #ddd;"
            "  border-radius: 4px;"
            "  padding: 6px;"
            "  font-family: monospace;"
            "  font-size: 10px;"
            "  color: #333;"
            "}"
        )
        self.tfs_wiql_preview.setVisible(False)
        layout.addWidget(self.tfs_wiql_preview)

        self.tfs_template_combo.currentIndexChanged.connect(
            self._on_tfs_template_changed
        )

        tfs_enabled = bool(
            self._tfs_settings.get("enabled")
            and self._tfs_settings.get("base_url")
            and self._tfs_settings.get("api_key")
        )

        self.tfs_status_label = QLabel("")
        self.tfs_status_label.setWordWrap(True)
        layout.addWidget(self.tfs_status_label)

        if not tfs_enabled:
            self.tfs_status_label.setText(
                "<span style='color:#c62828'>"
                "Интеграция с TFS не настроена. "
                "Откройте Настройки → TFS.</span>"
            )
            self.tfs_save_attachment_check.setEnabled(False)
            self.tfs_save_attachment_check.setChecked(False)
            self.tfs_project_combo.setEnabled(False)
            self.tfs_template_combo.setEnabled(False)
            self.tfs_reload_projects_btn.setEnabled(False)
        else:
            self.tfs_status_label.setText("")
            self._load_tfs_projects(force=False)

        self.tfs_save_attachment_check.toggled.connect(
            self._on_tfs_save_toggled
        )
        self._on_tfs_save_toggled(
            self.tfs_save_attachment_check.isChecked()
        )

        return box

    def _build_tfs_variables_section(self) -> QWidget:
        self._tfs_variables_box = QWidget()
        layout = QVBoxLayout(self._tfs_variables_box)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.addWidget(
            QLabel("<b>Переменные шаблона WIQL</b>")
        )
        header.addStretch()
        icon = make_info_icon("meta_tfs_variables")
        if icon is not None:
            header.addWidget(icon)
        layout.addLayout(header)

        hint = QLabel(
            "<span style='color:#666'>В выбранном шаблоне есть "
            "шаблонные переменные. Укажите их значения.</span>"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._tfs_variables_form = QFormLayout()
        self._tfs_variables_form.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight
            | Qt.AlignmentFlag.AlignVCenter
        )
        self._tfs_variables_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
        )

        form_container = QWidget()
        form_container.setLayout(self._tfs_variables_form)
        layout.addWidget(form_container)

        self._tfs_variables_box.setVisible(False)
        return self._tfs_variables_box

    def _rebuild_tfs_variables_inputs(self) -> None:
        if not hasattr(self, "_tfs_variables_form"):
            return

        while self._tfs_variables_form.count() > 0:
            item = self._tfs_variables_form.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._tfs_variable_inputs.clear()

        template_name = self.tfs_template_combo.currentData() or ""
        wiql = ""
        template_defaults: Dict[str, str] = {}
        for tpl in self._tfs_templates:
            if (tpl.get("name") or "").strip() == template_name:
                wiql = (tpl.get("wiql") or "").strip()
                raw_vars = tpl.get("variables")
                if isinstance(raw_vars, dict):
                    template_defaults = {
                        str(k): str(v if v is not None else "")
                        for k, v in raw_vars.items()
                        if str(k or "").strip()
                    }
                break

        variables = extract_variables(wiql) if wiql else []

        if not variables:
            self._tfs_variables_box.setVisible(False)
            return

        self._tfs_variables_box.setVisible(True)

        for name in variables:
            line = QLineEdit()
            current = self._tfs_variable_values.get(name, "").strip()
            default_val = template_defaults.get(name, "").strip()

            if current:
                line.setText(current)
            elif default_val:
                line.setText(default_val)
                line.setStyleSheet(
                    "QLineEdit { color: #666; font-style: italic; }"
                )

            line.setPlaceholderText(f"Значение для «{name}»…")
            self._tfs_variable_inputs[name] = line
            self._tfs_variables_form.addRow(f"{name}:", line)

    def _populate_tfs_templates(self) -> None:
        self.tfs_template_combo.blockSignals(True)
        self.tfs_template_combo.clear()
        self.tfs_template_combo.addItem("— не выбрано —", "")
        for tpl in self._tfs_templates:
            name = (tpl.get("name") or "").strip()
            if not name:
                continue
            self.tfs_template_combo.addItem(name, name)
        self.tfs_template_combo.blockSignals(False)

    def _load_tfs_projects(self, *, force: bool = False) -> None:
        self.tfs_project_combo.blockSignals(True)
        current = self.tfs_project_combo.currentData() or ""

        while self.tfs_project_combo.count() > 1:
            self.tfs_project_combo.removeItem(1)

        if self._on_request_tfs_projects is None:
            self.tfs_project_combo.blockSignals(False)
            return

        try:
            projects = self._on_request_tfs_projects() or []
        except Exception as exc:
            log.warning(
                "Не удалось получить проекты TFS: %s", exc
            )
            projects = []

        for name in projects:
            self.tfs_project_combo.addItem(name, name)

        if current:
            idx = self.tfs_project_combo.findData(current)
            if idx >= 0:
                self.tfs_project_combo.setCurrentIndex(idx)

        self.tfs_project_combo.blockSignals(False)

    def _on_tfs_template_changed(self, index: int) -> None:
        name = self.tfs_template_combo.currentData() or ""
        if not name:
            self.tfs_wiql_preview.setVisible(False)
            self.tfs_wiql_preview.setText("")
            self._rebuild_tfs_variables_inputs()
            return

        wiql = ""
        for tpl in self._tfs_templates:
            if (tpl.get("name") or "").strip() == name:
                wiql = (tpl.get("wiql") or "").strip()
                break

        if wiql:
            self.tfs_wiql_preview.setText(wiql)
            self.tfs_wiql_preview.setVisible(True)
        else:
            self.tfs_wiql_preview.setVisible(False)

        self._rebuild_tfs_variables_inputs()

    def _on_tfs_save_toggled(self, enabled: bool) -> None:
        for w in (
            self.tfs_project_combo,
            self.tfs_template_combo,
            self.tfs_reload_projects_btn,
        ):
            w.setEnabled(enabled)

        for line in self._tfs_variable_inputs.values():
            line.setEnabled(enabled)

        if enabled:
            project = self.tfs_project_combo.currentData() or ""
            if project and hasattr(
                self, "send_attachments_to_deepseek_check"
            ):
                self.send_attachments_to_deepseek_check.setChecked(
                    True
                )

    def _collect_tfs_variable_values(self) -> Dict[str, str]:
        result: Dict[str, str] = {}
        for name, line in self._tfs_variable_inputs.items():
            result[name] = line.text().strip()
        return result

    def _on_skip_transcription_toggled(self, enabled: bool) -> None:
        if not hasattr(self, "skip_transcription_hint"):
            return

        if enabled:
            self.skip_transcription_hint.setText(
                "<span style='color:#666'>"
                "Запись будет обработана как обычно.</span>"
            )
        else:
            self.skip_transcription_hint.setText(
                "<span style='color:#B8860B'>"
                "⚠ Транскрибация будет пропущена.</span>"
            )

        for widget_name in (
            "generate_summary_check",
            "generate_deepseek_check",
            "is_scrum_check",
        ):
            widget = getattr(self, widget_name, None)
            if widget is not None:
                widget.setEnabled(enabled)

    # ==================================================================
    # Вкладка «Вложения» (без изменений)
    # ==================================================================
    def _build_attachments_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        info = QLabel(
            "Файлы, которые можно передать в суммаризацию "
            "или включить в промпт DeepSeek."
        )
        info.setWordWrap(True)
        info.setStyleSheet("QLabel { color: #666; }")
        layout.addWidget(info)

        header = QHBoxLayout()
        header.addWidget(QLabel("<b>Вложения</b>"))
        header.addStretch()
        attach_icon = make_info_icon("meta_attachments")
        if attach_icon is not None:
            header.addWidget(attach_icon)
        layout.addLayout(header)

        self.attachments_list = QListWidget()
        self.attachments_list.setMinimumHeight(160)
        self.attachments_list.setSelectionMode(
            QListWidget.SelectionMode.ExtendedSelection
        )
        layout.addWidget(self.attachments_list, 1)

        attach_btn_row = QHBoxLayout()
        self.add_attachment_btn = QPushButton("Добавить файлы…")
        self.add_attachment_btn.clicked.connect(
            self._on_add_attachment
        )
        self.remove_attachment_btn = QPushButton(
            "Удалить выбранные"
        )
        self.remove_attachment_btn.clicked.connect(
            self._on_remove_attachment
        )
        attach_btn_row.addWidget(self.add_attachment_btn)
        attach_btn_row.addWidget(self.remove_attachment_btn)
        attach_btn_row.addStretch()
        layout.addLayout(attach_btn_row)

        send_header = QHBoxLayout()
        send_header.addWidget(
            QLabel("<b>Куда передавать вложения</b>")
        )
        send_header.addStretch()
        layout.addLayout(send_header)

        self.send_attachments_to_transcribe_check = QCheckBox(
            "Передать вложения в суммаризацию"
        )
        attach_tooltip(
            self.send_attachments_to_transcribe_check,
            "meta_send_to_transcribe",
        )
        layout.addWidget(
            self.send_attachments_to_transcribe_check
        )

        self.send_attachments_to_deepseek_check = QCheckBox(
            "Передать вложения в промпт DeepSeek"
        )
        attach_tooltip(
            self.send_attachments_to_deepseek_check,
            "meta_send_to_deepseek",
        )
        layout.addWidget(self.send_attachments_to_deepseek_check)

        return w

    # ------------------------------------------------------------------
    # Подключение сигналов (без изменений)
    # ------------------------------------------------------------------
    def _connect_signals(self) -> None:
        self.prompt_combo.currentIndexChanged.connect(
            self._on_prompt_selected
        )
        self.prompt_input.textChanged.connect(
            self._on_prompt_edited
        )

        self.name_input.textChanged.connect(
            self._refresh_name_preview
        )
        self.name_combo.currentIndexChanged.connect(
            self._on_name_template_changed
        )
        self.name_abbr_input.textChanged.connect(
            self._refresh_name_preview
        )

        self.is_scrum_check.toggled.connect(self._on_scrum_toggled)

        if hasattr(self, "tags_list"):
            self.tags_list.itemChanged.connect(
                self._on_tag_item_changed
            )

    # ------------------------------------------------------------------
    # Шаблоны названий (без изменений)
    # ------------------------------------------------------------------
    def _populate_name_templates(self) -> None:
        self.name_combo.blockSignals(True)
        self.name_combo.clear()
        self.name_combo.addItem("— без шаблона —", "")
        for item in self._name_templates:
            label = item.get("label") or item.get("template") or ""
            template = item.get("template") or ""
            if template:
                self.name_combo.addItem(label, template)
        self.name_combo.blockSignals(False)

    def _refresh_name_templates(
        self, selected_template: str = "",
    ) -> None:
        if self._get_name_templates_cb is not None:
            try:
                fresh = self._get_name_templates_cb() or []
                if isinstance(fresh, list):
                    self._name_templates = fresh
            except Exception as exc:
                log.warning(
                    "Не удалось получить шаблоны названий: %s", exc
                )

        self._populate_name_templates()

        if selected_template:
            idx = self.name_combo.findData(selected_template)
            if idx >= 0:
                self.name_combo.setCurrentIndex(idx)

    def _current_template(self) -> str:
        idx = self.name_combo.currentIndex()
        if idx <= 0:
            return ""
        return self.name_combo.itemData(idx) or ""

    def _on_name_template_changed(self, index: int) -> None:
        if index < 0:
            return
        self._refresh_name_preview()

    def _refresh_name_preview(self) -> None:
        template = self._current_template()
        raw_name = self.name_input.text().strip()
        abbr = self.name_abbr_input.text().strip()

        if not template:
            final = (
                raw_name
                or f"Запись {datetime.now():%Y-%m-%d %H-%M}"
            )
            self.name_preview_label.setText(
                f"Итоговое имя: {final}"
            )
            return

        final = format_name_template(
            template, name=raw_name, abbr=abbr
        )
        if not final.strip():
            final = f"Запись {datetime.now():%Y-%m-%d %H-%M}"
        self.name_preview_label.setText(f"Итоговое имя: {final}")

    def _on_save_name_template(self) -> None:
        template = self._current_template()
        raw_name = self.name_input.text().strip()

        candidate = template or raw_name
        if not candidate:
            QMessageBox.warning(
                self, "Шаблон",
                "Нечего сохранять: введите название "
                "или выберите шаблон.",
            )
            return

        label, ok = QInputDialog.getText(
            self,
            "Сохранить как шаблон",
            "Название шаблона (человекочитаемое):",
            text=(candidate[:40] if not template else ""),
        )
        if not ok or not label.strip():
            return
        label = label.strip()

        if self._on_save_name_template_cb is not None:
            try:
                self._on_save_name_template_cb(label, candidate)
            except Exception as exc:
                log.exception(
                    "Ошибка сохранения шаблона имени: %s", exc
                )
                QMessageBox.critical(
                    self, "Шаблон",
                    f"Не удалось сохранить шаблон:\n{exc}",
                )
                return

        self._refresh_name_templates(
            selected_template=candidate
        )
        QMessageBox.information(
            self, "Шаблон",
            f"Шаблон «{label}» сохранён.",
        )

    # ------------------------------------------------------------------
    # Скрам (без изменений)
    # ------------------------------------------------------------------
    def _on_scrum_toggled(self, checked: bool) -> None:
        if checked and not self.generate_deepseek_check.isChecked():
            self.generate_deepseek_check.setChecked(True)

    def _update_scrum_visibility(self, checked: bool) -> None:
        for w in self._scrum_widgets:
            w.setEnabled(checked)

    def _on_browse_protocol(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Выберите файл предыдущего протокола",
            os.path.expanduser("~"),
            "Документы (*.docx *.txt *.md *.pdf);;Все файлы (*)",
        )
        if path:
            self.protocol_path_input.setText(path)

    def _on_clear_protocol(self) -> None:
        self.protocol_path_input.setText("")

    def _on_pick_from_session(self) -> None:
        if (not self._sessions_root
                or not os.path.isdir(self._sessions_root)):
            QMessageBox.warning(
                self, "Протокол",
                f"Папка сессий не найдена:\n"
                f"{self._sessions_root or '(не задана)'}",
            )
            return

        candidates: List[Dict[str, str]] = []
        for name in sorted(
            os.listdir(self._sessions_root), reverse=True
        ):
            session_dir = os.path.join(self._sessions_root, name)
            if not os.path.isdir(session_dir):
                continue
            meta = {}
            session_json = os.path.join(
                session_dir, "session.json"
            )
            if os.path.exists(session_json):
                try:
                    with open(
                        session_json, "r", encoding="utf-8"
                    ) as f:
                        meta = json.load(f)
                except Exception:
                    pass
            for fname in (
                "manual_protocol.docx", "manual_protocol.md",
                "manual_protocol.txt", "manual_protocol.pdf",
                "protocol.docx", "protocol.md", "protocol.txt",
                "deepseek_prompt.docx", "deepseek_prompt.md",
                "deepseek_prompt.txt", "video.txt",
            ):
                p = os.path.join(session_dir, fname)
                if os.path.exists(p):
                    title = meta.get("name") or name
                    date = meta.get("date") or name.split("_")[0]
                    candidates.append({
                        "path": p,
                        "label": f"{date} — {title} ({fname})",
                    })
                    break

        if not candidates:
            QMessageBox.information(
                self, "Протокол",
                "В папке сессий не найдено ни одного файла "
                "протокола.",
            )
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("Выбор протокола")
        dlg.setMinimumWidth(640)
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel("Выберите предыдущий протокол:"))
        lst = QListWidget()
        for c in candidates:
            it = QListWidgetItem(c["label"])
            it.setData(Qt.ItemDataRole.UserRole, c["path"])
            lst.addItem(it)
        v.addWidget(lst)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        v.addWidget(btns)

        if (dlg.exec() == QDialog.DialogCode.Accepted
                and lst.currentItem()):
            path = lst.currentItem().data(
                Qt.ItemDataRole.UserRole
            )
            self.protocol_path_input.setText(path)

    # ------------------------------------------------------------------
    # Вложения (без изменений)
    # ------------------------------------------------------------------
    def _refresh_attachments_list(self) -> None:
        self.attachments_list.clear()
        for p in self._attachments:
            it = QListWidgetItem(os.path.basename(p))
            it.setToolTip(p)
            it.setData(Qt.ItemDataRole.UserRole, p)
            self.attachments_list.addItem(it)

    def _on_add_attachment(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Выберите файлы-вложения",
            os.path.expanduser("~"),
            "Документы (*.txt *.md *.docx *.pdf *.csv *.json);;"
            "Все файлы (*)",
        )
        if not files:
            return
        added = 0
        for f in files:
            if f and f not in self._attachments:
                self._attachments.append(f)
                added += 1
        if added:
            self._refresh_attachments_list()

    def _on_remove_attachment(self) -> None:
        items = self.attachments_list.selectedItems()
        if not items:
            return
        to_remove = {
            it.data(Qt.ItemDataRole.UserRole) for it in items
        }
        self._attachments = [
            p for p in self._attachments if p not in to_remove
        ]
        self._refresh_attachments_list()

    # ------------------------------------------------------------------
    # Заполнение
    # ------------------------------------------------------------------
    def _apply_initial(self) -> None:
        init = self._initial or {}

        raw_vars = init.get("tfs_variables") or {}
        if isinstance(raw_vars, dict):
            self._tfs_variable_values = {
                str(k): str(v) for k, v in raw_vars.items()
            }
        else:
            self._tfs_variable_values = {}

        # --- Документы из initial ---
        raw_docs = init.get("documents") or []
        if isinstance(raw_docs, list):
            self._documents = [
                str(p) for p in raw_docs if p
            ]
        if self._allow_document_only:
            self._refresh_documents_list()

        project = init.get("project", "")
        if project:
            idx = self.project_combo.findText(project)
            if idx >= 0:
                self.project_combo.setCurrentIndex(idx)
            else:
                self.project_combo.setEditText(project)
        elif self._projects:
            self.project_combo.setCurrentIndex(0)

        # --- Категория ---
        init_category = str(init.get("category") or "").strip()
        if init_category:
            idx = self.category_combo.findData(init_category)
            if idx >= 0:
                self.category_combo.setCurrentIndex(idx)
            else:
                # Категории нет в справочнике — добавляем
                # её пунктом «на лету», чтобы не потерять.
                self.category_combo.addItem(
                    init_category, init_category
                )
                self.category_combo.setCurrentIndex(
                    self.category_combo.count() - 1
                )
        else:
            self.category_combo.setCurrentIndex(0)

        init_name = (
            init.get("name", "") or init.get("description", "")
        )
        init_template = init.get("name_template", "")
        init_abbr = init.get("name_abbr", "")

        self.name_input.blockSignals(True)
        self.name_input.setText(init_name)
        self.name_input.blockSignals(False)

        self.name_abbr_input.blockSignals(True)
        self.name_abbr_input.setText(init_abbr)
        self.name_abbr_input.blockSignals(False)

        if init_template:
            idx = self.name_combo.findData(init_template)
            if idx >= 0:
                self.name_combo.blockSignals(True)
                self.name_combo.setCurrentIndex(idx)
                self.name_combo.blockSignals(False)
        else:
            self.name_combo.blockSignals(True)
            self.name_combo.setCurrentIndex(0)
            self.name_combo.blockSignals(False)

        self.comment_input.setPlainText(
            init.get("comment", "")
        )

        from PySide6.QtCore import QDate, QTime

        date_str = str(init.get("date") or "").strip()
        time_str = str(init.get("time") or "").strip()

        if date_str:
            try:
                y, m, d = date_str.split("-")
                self.date_edit.setDate(QDate(int(y), int(m), int(d)))
            except Exception:
                self.date_edit.setDate(QDate.currentDate())
        else:
            self.date_edit.setDate(QDate.currentDate())

        if time_str:
            try:
                parts = time_str.replace("-", ":").split(":")
                hh = int(parts[0]) if len(parts) > 0 else 0
                mm = int(parts[1]) if len(parts) > 1 else 0
                ss = int(parts[2]) if len(parts) > 2 else 0
                self.time_edit.setTime(QTime(hh, mm, ss))
            except Exception:
                self.time_edit.setTime(QTime.currentTime())
        else:
            self.time_edit.setTime(QTime.currentTime())

        # --- Теги ---
        raw_tags = init.get("tags", []) or []
        selected_tags: List[str] = []
        if isinstance(raw_tags, list):
            for t in raw_tags:
                if isinstance(t, str) and t.strip():
                    selected_tags.append(t.strip())
                elif isinstance(t, dict):
                    name = str(t.get("name") or "").strip()
                    if name:
                        selected_tags.append(name)
        self._selected_tags = selected_tags
        self._populate_tags_list()

        self.sync_ready_check.setChecked(
            bool(init.get("sync_ready", False))
        )

        if "skip_transcription" in init:
            do_transcribe = bool(init.get("skip_transcription"))
        else:
            do_transcribe = True

        self.skip_transcription_check.blockSignals(True)
        self.skip_transcription_check.setChecked(do_transcribe)
        self.skip_transcription_check.blockSignals(False)
        self._on_skip_transcription_toggled(do_transcribe)

        self.generate_summary_check.blockSignals(True)
        self.generate_summary_check.setChecked(
            bool(init.get("generate_summary", False))
        )
        self.generate_summary_check.blockSignals(False)

        if "generate_deepseek_prompt" in init:
            gen = bool(init.get("generate_deepseek_prompt"))
        else:
            gen = True
        self.generate_deepseek_check.setChecked(gen)

        # --- Промпт ---
        prompt_text = (
            init.get("prompt", "") or self._default_prompt
        )
        prompt_name = init.get("prompt_name", "")

        if prompt_name:
            idx = self.prompt_combo.findText(prompt_name)
            if idx >= 0:
                self.prompt_combo.blockSignals(True)
                self.prompt_combo.setCurrentIndex(idx)
                self.prompt_combo.blockSignals(False)
                if not init.get("prompt", "").strip():
                    lib_text = self.prompt_combo.itemData(idx) or ""
                    if lib_text:
                        prompt_text = lib_text
        else:
            found_idx = -1
            if prompt_text:
                for i in range(self.prompt_combo.count()):
                    data = self.prompt_combo.itemData(i)
                    if data and data.strip() == prompt_text.strip():
                        found_idx = i
                        break
            if found_idx >= 0:
                self.prompt_combo.blockSignals(True)
                self.prompt_combo.setCurrentIndex(found_idx)
                self.prompt_combo.blockSignals(False)
            else:
                self.prompt_combo.blockSignals(True)
                self.prompt_combo.setCurrentIndex(0)
                self.prompt_combo.blockSignals(False)

        self.prompt_input.blockSignals(True)
        self.prompt_input.setPlainText(prompt_text)
        self.prompt_input.blockSignals(False)
        self._prompt_edited = False

        # --- Контекст ---
        default_ctx = True

        if "include_name_in_prompt" in init:
            self.include_name_check.setChecked(
                bool(init["include_name_in_prompt"])
            )
        else:
            self.include_name_check.setChecked(default_ctx)

        if "include_project_in_prompt" in init:
            self.include_project_check.setChecked(
                bool(init["include_project_in_prompt"])
            )
        else:
            self.include_project_check.setChecked(default_ctx)

        if "include_comment_in_prompt" in init:
            self.include_comment_check.setChecked(
                bool(init["include_comment_in_prompt"])
            )
        else:
            self.include_comment_check.setChecked(default_ctx)

        if "include_tags_in_prompt" in init:
            self.include_tags_check.setChecked(
                bool(init["include_tags_in_prompt"])
            )
        else:
            self.include_tags_check.setChecked(default_ctx)

        # --- Скрам ---
        is_scrum = bool(init.get("is_scrum", False))
        self.is_scrum_check.blockSignals(True)
        self.is_scrum_check.setChecked(is_scrum)
        self.is_scrum_check.blockSignals(False)
        self._update_scrum_visibility(is_scrum)
        self.protocol_path_input.setText(
            init.get("previous_protocol_path", "")
        )

        # --- Вложения ---
        self._attachments = list(
            init.get("attachments", []) or []
        )
        self._refresh_attachments_list()
        self.send_attachments_to_transcribe_check.setChecked(
            bool(init.get("send_attachments_to_transcribe", False))
        )
        self.send_attachments_to_deepseek_check.setChecked(
            bool(init.get("send_attachments_to_deepseek", False))
        )

        # --- TFS ---
        tfs_project = str(init.get("tfs_project") or "").strip()
        if tfs_project:
            idx = self.tfs_project_combo.findData(tfs_project)
            if idx >= 0:
                self.tfs_project_combo.setCurrentIndex(idx)

        tfs_template = str(
            init.get("tfs_template_name") or ""
        ).strip()
        if tfs_template:
            idx = self.tfs_template_combo.findData(tfs_template)
            if idx >= 0:
                self.tfs_template_combo.setCurrentIndex(idx)
            self._on_tfs_template_changed(
                self.tfs_template_combo.currentIndex()
            )

        if "tfs_save_attachment" in init:
            self.tfs_save_attachment_check.setChecked(
                bool(init["tfs_save_attachment"])
            )
        else:
            self.tfs_save_attachment_check.setChecked(
                self._default_tfs_save_attachment
            )

        # --- НОВОЕ: связи ---
        raw_rels = init.get("relations") or []
        self._relations = []
        if isinstance(raw_rels, list):
            for r in raw_rels:
                if not isinstance(r, dict):
                    continue
                sid = str(r.get("target_session_id") or "").strip()
                tdir = str(r.get("target_dir") or "").strip()
                tname = str(r.get("target_name") or "").strip()
                trid = str(r.get("target_record_id") or "").strip()
                kind = str(
                    r.get("kind") or DEFAULT_RELATION_KIND
                ).strip().lower()
                if kind not in RELATION_KINDS:
                    kind = DEFAULT_RELATION_KIND
                if not (sid or tdir or trid):
                    continue
                self._relations.append({
                    "target_session_id": sid,
                    "target_dir": tdir,
                    "target_name": tname,
                    "target_record_id": trid,
                    "kind": kind,
                })
        self._refresh_relations_table()

        # --- Опции обработки документов ---
        if self._allow_document_only:
            self.doc_generate_summary_check.setChecked(
                bool(init.get("generate_summary", False))
            )
            self.doc_generate_deepseek_check.setChecked(
                bool(init.get("generate_deepseek_prompt", False))
            )
            self.doc_include_text_as_transcript_check.setChecked(
                bool(init.get("document_save_transcript", True))
            )
            self.doc_send_to_deepseek_check.setChecked(
                bool(init.get("document_to_deepseek", True))
            )

    # ------------------------------------------------------------------
    # Промпт (без изменений)
    # ------------------------------------------------------------------
    def _on_prompt_selected(self, index: int) -> None:
        if index < 0:
            return
        text = self.prompt_combo.itemData(index) or ""
        name = self.prompt_combo.itemText(index)

        if self._prompt_edited and self.prompt_input.toPlainText().strip():
            reply = QMessageBox.question(
                self,
                "Заменить промпт?",
                f"Текущий текст промпта был изменён вручную.\n"
                f"Заменить его на «{name}»?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        self.prompt_input.blockSignals(True)
        self.prompt_input.setPlainText(text)
        self.prompt_input.blockSignals(False)
        self._prompt_edited = False

    def _on_prompt_edited(self) -> None:
        self._prompt_edited = True

    def _on_reset_prompt(self) -> None:
        idx = self.prompt_combo.currentIndex()
        if idx >= 0:
            text = self.prompt_combo.itemData(idx) or ""
            if text:
                self.prompt_input.blockSignals(True)
                self.prompt_input.setPlainText(text)
                self.prompt_input.blockSignals(False)
                self._prompt_edited = False
                return
        if self._default_prompt:
            self.prompt_input.blockSignals(True)
            self.prompt_input.setPlainText(self._default_prompt)
            self.prompt_input.blockSignals(False)
            self._prompt_edited = False

    def _refresh_prompts_list(
        self, selected_name: str = "",
    ) -> None:
        try:
            if self._get_prompts_cb is not None:
                fresh = self._get_prompts_cb() or []
                if isinstance(fresh, list):
                    self._prompts = fresh
        except Exception as exc:
            log.warning(
                "Не удалось получить список промптов: %s", exc
            )

        self.prompt_combo.blockSignals(True)
        self.prompt_combo.clear()
        self.prompt_combo.addItem("— не выбрано —", "")
        for p in self._prompts:
            self.prompt_combo.addItem(p["name"], p["text"])

        if selected_name:
            idx = self.prompt_combo.findText(selected_name)
            if idx >= 0:
                self.prompt_combo.setCurrentIndex(idx)
                text = self.prompt_combo.itemData(idx) or ""
                self.prompt_input.blockSignals(True)
                self.prompt_input.setPlainText(text)
                self.prompt_input.blockSignals(False)
                self._prompt_edited = False
        self.prompt_combo.blockSignals(False)

    def _on_save_to_library(self) -> None:
        text = self.prompt_input.toPlainText().strip()
        if not text:
            QMessageBox.warning(
                self, "Промпт", "Текст промпта пустой"
            )
            return

        name, ok = QInputDialog.getText(
            self, "Сохранить в библиотеку",
            "Название промпта:",
        )
        if not ok or not name.strip():
            return
        name = name.strip()

        saved = False
        if self._on_save_prompt_cb is not None:
            try:
                self._on_save_prompt_cb(name, text)
                saved = True
            except Exception as exc:
                log.exception(
                    "Ошибка сохранения промпта через колбэк: %s",
                    exc,
                )
                QMessageBox.critical(
                    self, "Промпт",
                    f"Не удалось сохранить промпт:\n{exc}",
                )
                return

        if not saved:
            self.result_data["new_prompt"] = {
                "name": name, "text": text,
            }

        self._refresh_prompts_list(selected_name=name)

        QMessageBox.information(
            self, "Промпт",
            f"Промпт «{name}» сохранён в библиотеку.",
        )

    # ------------------------------------------------------------------
    # Кнопки
    # ------------------------------------------------------------------
    def _on_accept(self) -> None:
        project = (
            self.project_combo.currentText().strip() or "Default"
        )

        # --- Категория ---
        # currentData() — если выбрано из справочника;
        # currentText() — если пользователь ввёл вручную.
        category_data = self.category_combo.currentData() or ""
        category_text = self.category_combo.currentText().strip()
        if category_text == "— не указана —":
            category_text = ""
        category = str(category_data).strip() or category_text

        template = self._current_template()
        raw_name = self.name_input.text().strip()
        abbr = self.name_abbr_input.text().strip()

        if template:
            final_name = format_name_template(
                template, name=raw_name, abbr=abbr
            )
        else:
            final_name = raw_name

        if not final_name:
            final_name = (
                f"Запись {datetime.now():%Y-%m-%d %H-%M}"
            )

        comment = self.comment_input.toPlainText().strip()
        prompt = self.prompt_input.toPlainText().strip()

        prompt_name = ""
        idx = self.prompt_combo.currentIndex()
        if idx >= 0:
            prompt_name = self.prompt_combo.itemText(idx)
            if prompt_name == "— не выбрано —":
                prompt_name = ""

        is_scrum = bool(self.is_scrum_check.isChecked())

        selected_set = set(self._selected_tags)
        ordered_tags: List[str] = []
        for tag in self._tags:
            name = tag.get("name") or ""
            if name and name in selected_set:
                ordered_tags.append(name)
        for name in self._selected_tags:
            if name and name not in ordered_tags:
                ordered_tags.append(name)

        qd = self.date_edit.date()
        qt = self.time_edit.time()
        date_out = f"{qd.year():04d}-{qd.month():02d}-{qd.day():02d}"
        time_out = f"{qt.hour():02d}:{qt.minute():02d}:{qt.second():02d}"

        do_transcribe = bool(
            self.skip_transcription_check.isChecked()
        )

        # --- TFS ---
        tfs_project_value = (
            self.tfs_project_combo.currentData() or ""
        )
        tfs_template_value = (
            self.tfs_template_combo.currentData() or ""
        )
        tfs_save_value = bool(
            self.tfs_save_attachment_check.isChecked()
            and self.tfs_save_attachment_check.isEnabled()
        )

        tfs_vars_collected = self._collect_tfs_variable_values()

        # Валидация TFS-переменных.
        if tfs_template_value:
            wiql = ""
            template_defaults: Dict[str, str] = {}
            for tpl in self._tfs_templates:
                if (tpl.get("name") or "").strip() == tfs_template_value:
                    wiql = (tpl.get("wiql") or "").strip()
                    raw_vars = tpl.get("variables")
                    if isinstance(raw_vars, dict):
                        template_defaults = {
                            str(k): str(v if v is not None else "")
                            for k, v in raw_vars.items()
                            if str(k or "").strip()
                        }
                    break

            required = extract_variables(wiql)
            if required:
                missing = []
                for n in required:
                    has_value = bool(
                        tfs_vars_collected.get(n, "").strip()
                    )
                    has_default = bool(
                        template_defaults.get(n, "").strip()
                    )
                    if not has_value and not has_default:
                        missing.append(n)

                if missing and tfs_save_value:
                    QMessageBox.warning(
                        self, "TFS",
                        f"В шаблоне «{tfs_template_value}» есть "
                        f"переменные без значений и без дефолтов:"
                        f"\n\n  • " + "\n  • ".join(missing),
                    )
                    return

        # --- Валидация документов ---
        document_only = False
        documents_collected: List[str] = []

        if self._allow_document_only:
            documents_collected = list(self._documents)

            for p in documents_collected:
                if not os.path.isfile(p):
                    QMessageBox.warning(
                        self, "Документы",
                        f"Файл не найден:\n{p}\n\n"
                        f"Уберите его из списка документов.",
                    )
                    return

            document_only = bool(documents_collected)

        # --- Связи ---
        relations_out = [
            dict(r) for r in getattr(self, "_relations", [])
        ]

        result: Dict[str, Any] = {
            "project": project,
            "category": category,       # ← НОВОЕ
            "name": final_name,
            "description": final_name,
            "name_template": template,
            "name_abbr": abbr,
            "comment": comment,
            "tags": ordered_tags,
            "date": date_out,
            "time": time_out,
            "sync_ready": bool(self.sync_ready_check.isChecked()),
            "skip_transcription": do_transcribe,
            "prompt": prompt,
            "prompt_name": prompt_name,
            "prompt_edited": bool(self._prompt_edited),
            "generate_summary": bool(
                self.generate_summary_check.isChecked()
            ),
            "is_scrum": is_scrum,
            "previous_protocol_path": (
                self.protocol_path_input.text().strip()
            ),
            "generate_deepseek_prompt": bool(
                self.generate_deepseek_check.isChecked()
            ),
            "include_name_in_prompt": bool(
                self.include_name_check.isChecked()
            ),
            "include_project_in_prompt": bool(
                self.include_project_check.isChecked()
            ),
            "include_comment_in_prompt": bool(
                self.include_comment_check.isChecked()
            ),
            "include_tags_in_prompt": bool(
                self.include_tags_check.isChecked()
            ),
            "attachments": list(self._attachments),
            "send_attachments_to_transcribe": bool(
                self.send_attachments_to_transcribe_check.isChecked()
            ),
            "send_attachments_to_deepseek": bool(
                self.send_attachments_to_deepseek_check.isChecked()
            ),
            "tfs_project": tfs_project_value,
            "tfs_template_name": tfs_template_value,
            "tfs_save_attachment": tfs_save_value,
            "tfs_variables": dict(tfs_vars_collected),
            "documents": documents_collected,
            "document_only": document_only,
            # --- НОВОЕ: связи ---
            "relations": relations_out,
        }

        # --- Опции обработки документов ---
        if self._allow_document_only and document_only:
            result["document_generate_summary"] = bool(
                self.doc_generate_summary_check.isChecked()
            )
            result["document_generate_deepseek"] = bool(
                self.doc_generate_deepseek_check.isChecked()
            )
            result["document_save_transcript"] = bool(
                self.doc_include_text_as_transcript_check.isChecked()
            )
            result["document_to_deepseek"] = bool(
                self.doc_send_to_deepseek_check.isChecked()
            )

            result["generate_summary"] = bool(
                self.doc_generate_summary_check.isChecked()
            )
            result["generate_deepseek_prompt"] = bool(
                self.doc_generate_deepseek_check.isChecked()
            )
            result["send_attachments_to_deepseek"] = bool(
                self.doc_send_to_deepseek_check.isChecked()
            )

        if "new_prompt" in self.result_data:
            result["new_prompt"] = self.result_data["new_prompt"]

        self.result_data = result

        log.info(
            "Метаданные подтверждены: project=%s, name=%s, "
            "document_only=%s, documents=%d, relations=%d",
            project, final_name, document_only,
            len(documents_collected),
            len(relations_out),
        )
        self.accept()

    def _on_reject(self) -> None:
        log.info("Ввод метаданных отменён пользователем")
        self.result_data = {}
        self.reject()