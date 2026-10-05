"""Окно «Поручения» — сводный раздел по всем записям.

Возможности:
  • таблица всех поручений из sessions/;
  • фильтры: статус, исполнитель, проект, контекст (сессия);
  • сортировка по колонке;
  • мультивыбор → отправка в Bitrix24 с форматированием;
  • быстрое редактирование статуса;
  • открытие редактора поручений конкретной записи.
"""
from __future__ import annotations

import asyncio
import html
import os
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QDate, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDateEdit, QDialog,
    QDialogButtonBox, QFileDialog, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QProgressDialog,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from ..bitrix_client import Bitrix24Client, Bitrix24Error
from ..file_readers import read_json_file
from ..logger import get_logger
from ..markdown_to_bitrix import markdown_to_bitrix
from ..tasks_manager import (
    ACTION_ITEMS_FILE,
    STATUS_COLORS,
    STATUS_LABELS,
    STATUS_ORDER,
    days_until_due,
    delete_item,
    due_date_priority,
    is_overdue,
    load_action_items,
    scan_all_action_items,
    update_item,
)
from .tasks_editor_dialog import TasksEditorDialog
from .tooltips import attach_tooltip, make_info_icon, with_info
from .send_to_employees_dialog import SendToEmployeesDialog

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Диалог отправки в Bitrix24
# ---------------------------------------------------------------------------
class _SendTasksDialog(QDialog):
    """Форма отправки выбранных поручений в Bitrix24."""

    def __init__(
        self,
        items: List[Dict[str, Any]],
        bitrix_cfg: Dict[str, Any],
        projects: List[Dict[str, str]],
        employees: List[Dict[str, str]],
        default_chat_id: str = "",
        *,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Отправка поручений в Bitrix24")
        self.setModal(True)
        self.setMinimumSize(800, 620)

        self._items = items
        self._bitrix_cfg = bitrix_cfg or {}
        self._projects = list(projects or [])
        self._employees = list(employees or [])
        self._default_chat_id = default_chat_id or ""

        self._build_ui()
        self._refresh_preview()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        info = QLabel(
            "Поручения будут отправлены одним сообщением в "
            "выбранный чат. Отметьте получателя и при "
            "необходимости отредактируйте текст."
        )
        info.setWordWrap(True)
        info.setStyleSheet("QLabel { color: #666; }")
        root.addWidget(info)

        # --- Получатель ---
        rec_row = QHBoxLayout()
        rec_row.addWidget(QLabel("Получатель:"))

        self.recipient_combo = QComboBox()
        self.recipient_combo.setMinimumWidth(320)
        self.recipient_combo.addItem(
            "— выберите чат —", ""
        )
        if self._default_chat_id:
            self.recipient_combo.addItem(
                f"По умолчанию ({self._default_chat_id})",
                self._default_chat_id,
            )
        for p in self._projects:
            cid = (p.get("chat_id") or "").strip()
            label = f"Проект: {p.get('name')}"
            if cid:
                label += f"  ({cid})"
            else:
                label += "  — чат не задан"
            self.recipient_combo.addItem(label, cid)
        for e in self._employees:
            cid = (e.get("chat_id") or "").strip()
            label = f"Сотрудник: {e.get('name')}"
            if cid:
                label += f"  ({cid})"
            else:
                label += "  — чат не задан"
            self.recipient_combo.addItem(label, cid)

        rec_row.addWidget(self.recipient_combo, 1)

        self.manual_chat_input = QLineEdit()
        self.manual_chat_input.setPlaceholderText(
            "Или введите ID чата вручную (chat2101)"
        )
        rec_row.addWidget(self.manual_chat_input)

        root.addLayout(rec_row)

        # --- Формат ---
        fmt_row = QHBoxLayout()
        fmt_row.addWidget(QLabel("Формат:"))

        self.format_combo = QComboBox()
        self.format_combo.addItem("Список", "list")
        self.format_combo.addItem("Таблица", "table")
        fmt_row.addWidget(self.format_combo)

        fmt_row.addSpacing(12)

        self.group_by_assignee_check = QCheckBox(
            "Группировать по исполнителю"
        )
        self.group_by_assignee_check.setChecked(True)
        fmt_row.addWidget(self.group_by_assignee_check)

        fmt_row.addSpacing(12)

        self.header_check = QCheckBox(
            "Добавлять заголовок «Поручения»"
        )
        self.header_check.setChecked(True)
        fmt_row.addWidget(self.header_check)

        fmt_row.addStretch()
        root.addLayout(fmt_row)

        # --- Предпросмотр ---
        root.addWidget(QLabel("<b>Предпросмотр сообщения:</b>"))
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setMinimumHeight(280)
        root.addWidget(self.preview, 1)

        self.format_combo.currentIndexChanged.connect(
            self._refresh_preview
        )
        self.group_by_assignee_check.toggled.connect(
            self._refresh_preview
        )
        self.header_check.toggled.connect(self._refresh_preview)

        # --- Кнопки ---
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.button(
            QDialogButtonBox.StandardButton.Ok
        ).setText("Отправить")
        buttons.button(
            QDialogButtonBox.StandardButton.Cancel
        ).setText("Отмена")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # ------------------------------------------------------------------
    # Построение сообщения
    # ------------------------------------------------------------------
    def _build_text(self) -> str:
        fmt = self.format_combo.currentData() or "list"
        group = self.group_by_assignee_check.isChecked()
        header = self.header_check.isChecked()

        parts: List[str] = []

        if header:
            parts.append(f"**Поручения** ({len(self._items)} шт.)")
            parts.append("")

        if fmt == "table":
            parts.append("| Исполнитель | Поручение | Срок | Статус |")
            parts.append("| --- | --- | --- | --- |")
            for it in self._items:
                assignee = it.get("assignee") or "—"
                text = (it.get("text") or "").replace("|", "/")
                due = it.get("due_date") or "—"
                status = STATUS_LABELS.get(
                    it.get("status") or "created", "—"
                )
                parts.append(
                    f"| {assignee} | {text} | {due} | {status} |"
                )
        else:
            # Список
            if group:
                by_assignee: Dict[str, List[Dict[str, Any]]] = {}
                for it in self._items:
                    key = it.get("assignee") or "(без исполнителя)"
                    by_assignee.setdefault(key, []).append(it)
                for assignee, items in sorted(by_assignee.items()):
                    parts.append(f"**{assignee}**")
                    for it in items:
                        status = STATUS_LABELS.get(
                            it.get("status") or "created", "—"
                        )
                        due = (
                            f" (срок: {it['due_date']})"
                            if it.get("due_date") else ""
                        )
                        parts.append(
                            f"- {it.get('text') or ''} "
                            f"[{status}]{due}"
                        )
                    parts.append("")
            else:
                for it in self._items:
                    assignee = (
                        f"**{it.get('assignee')}** — "
                        if it.get("assignee") else ""
                    )
                    status = STATUS_LABELS.get(
                        it.get("status") or "created", "—"
                    )
                    due = (
                        f" (срок: {it['due_date']})"
                        if it.get("due_date") else ""
                    )
                    parts.append(
                        f"- {assignee}{it.get('text') or ''} "
                        f"[{status}]{due}"
                    )

        return "\n".join(parts).strip()

    def _refresh_preview(self) -> None:
        md = self._build_text()
        # Преобразуем в BB-код для предпросмотра как Bitrix24.
        try:
            self.preview.setPlainText(markdown_to_bitrix(md))
        except Exception:
            self.preview.setPlainText(md)

    # ------------------------------------------------------------------
    # Отправка
    # ------------------------------------------------------------------
    def _resolve_chat_id(self) -> str:
        manual = self.manual_chat_input.text().strip()
        if manual:
            return manual
        return self.recipient_combo.currentData() or ""

    def _on_accept(self) -> None:
        chat_id = self._resolve_chat_id()
        if not chat_id:
            QMessageBox.warning(
                self, "Bitrix24",
                "Укажите получателя (чат) или введите ID вручную.",
            )
            return

        webhook = (self._bitrix_cfg.get("webhook_url") or "").strip()
        if not webhook:
            QMessageBox.warning(
                self, "Bitrix24",
                "Вебхук Bitrix24 не настроен. Откройте "
                "Настройки → Bitrix24.",
            )
            return

        text_md = self._build_text()
        if not text_md:
            QMessageBox.warning(
                self, "Bitrix24", "Нет текста для отправки."
            )
            return

        text_bb = markdown_to_bitrix(text_md)

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

        # Простая синхронная отправка в отдельном потоке,
        # чтобы UI не подвис.
        dlg = QProgressDialog(
            "Отправка в Bitrix24…", None, 0, 0, self
        )
        dlg.setWindowTitle("Bitrix24")
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.show()
        QGuiApplication.processEvents()

        error: List[str] = []

        def _worker() -> None:
            async def _run() -> None:
                async with Bitrix24Client(
                    webhook_url=webhook,
                    connect_timeout=connect_timeout,
                    read_timeout=read_timeout,
                    max_message_chars=max_chars,
                ) as client:
                    await client.send_message(
                        dialog_id=chat_id,
                        text=text_bb,
                        system=system,
                        url_preview=url_preview,
                    )

            try:
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(_run())
                finally:
                    loop.close()
            except Exception as exc:
                log.exception("Отправка поручений не удалась: %s", exc)
                error.append(str(exc))

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        while t.is_alive():
            QGuiApplication.processEvents()
            t.join(0.05)

        dlg.close()

        if error:
            QMessageBox.critical(
                self, "Bitrix24",
                f"Не удалось отправить:\n\n{error[0]}",
            )
            return

        QMessageBox.information(
            self, "Bitrix24",
            f"Отправлено поручений: {len(self._items)}",
        )
        self.accept()


# ---------------------------------------------------------------------------
# Основное окно
# ---------------------------------------------------------------------------
class TasksWindow(QDialog):
    """Сводное окно поручений по всем записям."""

    def __init__(
        self,
        sessions_root: str,
        config_manager=None,
        *,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Поручения")
        self.setMinimumSize(1280, 760)
        self.setModal(False)

        self._sessions_root = sessions_root
        self._config_manager = config_manager
        self._items: List[Dict[str, Any]] = []

        self._build_ui()
        self.refresh()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        # --- Инфо ---
        info_row = QHBoxLayout()
        info = QLabel(
            "Сводная таблица поручений по всем записям. "
            "Можно фильтровать по статусу, исполнителю и "
            "проекту, выбирать несколько строк и отправлять "
            "их в Bitrix24 одним сообщением."
        )
        info.setWordWrap(True)
        info.setStyleSheet("QLabel { color: #666; }")
        info_row.addWidget(info, 1)
        icon = make_info_icon("tasks_window_intro")
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

        filters.addSpacing(8)
        filters.addWidget(QLabel("Исполнитель:"))
        self.assignee_filter = QComboBox()
        self.assignee_filter.addItem("— все —", "")
        self.assignee_filter.setMinimumWidth(180)
        self.assignee_filter.currentIndexChanged.connect(
            self._reload_table
        )
        filters.addWidget(self.assignee_filter)

        filters.addSpacing(8)
        filters.addWidget(QLabel("Проект:"))
        self.project_filter = QComboBox()
        self.project_filter.addItem("— все —", "")
        self.project_filter.setMinimumWidth(160)
        self.project_filter.currentIndexChanged.connect(
            self._reload_table
        )
        filters.addWidget(self.project_filter)

        filters.addSpacing(8)
        filters.addWidget(QLabel("Контекст:"))
        self.context_filter = QComboBox()
        self.context_filter.addItem("— все —", "")
        self.context_filter.setMinimumWidth(220)
        self.context_filter.currentIndexChanged.connect(
            self._reload_table
        )
        filters.addWidget(self.context_filter)

        filters.addSpacing(8)

        search_container = QWidget()
        search_row = QHBoxLayout(search_container)
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(4)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(
            "Поиск по тексту поручения, названию записи, "
            "исполнителю…"
        )
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self._reload_table)
        self.search_input.textChanged.connect(
            self._on_search_text_changed
        )
        attach_tooltip(self.search_input, "tasks_search_input")
        search_row.addWidget(self.search_input, 1)

        self.search_clear_btn = QPushButton("Сбросить")
        self.search_clear_btn.setToolTip(
            "Очистить поле поиска"
        )
        self.search_clear_btn.clicked.connect(
            self._on_clear_search
        )
        search_row.addWidget(self.search_clear_btn, 0)

        filters.addWidget(search_container, 1)

        root.addLayout(filters)

        # --- Фильтры по срокам ---
        filters2 = QHBoxLayout()

        self.overdue_only_check = QCheckBox(
            "Только просроченные"
        )
        self.overdue_only_check.setToolTip(
            "Показать поручения, у которых срок уже истёк "
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
        #self._on_due_filter_toggled(False)

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
            "Статус", "Поручение", "Исполнитель",
            "Срок", "Проект", "Контекст (запись)", "ID",
        ])
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.table.itemDoubleClicked.connect(
            lambda _it: self._open_editor_for_current()
        )
        hv = self.table.horizontalHeader()
        hv.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hv.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hv.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hv.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hv.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        hv.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        hv.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        root.addWidget(self.table, 1)

        # --- Действия ---
        actions = QHBoxLayout()

        self.refresh_btn = QPushButton("Обновить")
        self.refresh_btn.clicked.connect(self.refresh)
        actions.addWidget(self.refresh_btn)

        self.select_all_btn = QPushButton("Выбрать все")
        self.select_all_btn.clicked.connect(self._select_all)
        actions.addWidget(self.select_all_btn)

        self.clear_selection_btn = QPushButton("Снять выбор")
        self.clear_selection_btn.clicked.connect(self._clear_selection)
        actions.addWidget(self.clear_selection_btn)

        actions.addSpacing(12)

        self.edit_btn = QPushButton("Открыть редактор записи…")
        self.edit_btn.setToolTip(
            "Открыть редактор поручений той записи, к которой "
            "относится выбранное поручение."
        )
        self.edit_btn.clicked.connect(self._open_editor_for_current)
        actions.addWidget(self.edit_btn)

        self.delete_btn = QPushButton("Удалить выбранные")
        self.delete_btn.clicked.connect(self._delete_selected)
        actions.addWidget(self.delete_btn)

        actions.addStretch()

        self.status_combo = QComboBox()
        for s in STATUS_ORDER:
            self.status_combo.addItem(STATUS_LABELS[s], s)
        self.set_status_btn = QPushButton("Применить статус")
        self.set_status_btn.setToolTip(
            "Проставить выбранный статус всем выделенным "
            "поручениям."
        )
        self.set_status_btn.clicked.connect(self._apply_status)
        actions.addWidget(self.status_combo)
        actions.addWidget(self.set_status_btn)

        root.addLayout(actions)

        # --- Итог ---
        bottom = QHBoxLayout()
        self.stats_label = QLabel("")
        self.stats_label.setStyleSheet(
            "QLabel { color: #444; font-weight: bold; }"
        )
        bottom.addWidget(self.stats_label)
        bottom.addStretch()

        self.send_bitrix_btn = QPushButton(
            "Отправить выбранные в Bitrix24…"
        )
        self.send_bitrix_btn.setToolTip(
            "Сформировать одно сообщение из выбранных поручений "
            "и отправить его в чат Bitrix24."
        )
        self.send_bitrix_btn.clicked.connect(self._send_to_bitrix)
        bottom.addWidget(self.send_bitrix_btn)

        self.send_employees_btn = QPushButton(
            "Отправить сотрудникам…"
        )
        self.send_employees_btn.setToolTip(
            "Сопоставить исполнителей из поручений с сотрудниками "
            "из справочника и отправить каждому его поручения "
            "отдельным сообщением."
        )
        self.send_employees_btn.clicked.connect(
            self._send_to_employees
        )
        bottom.addWidget(self.send_employees_btn)

        self.close_btn = QPushButton("Закрыть")
        self.close_btn.clicked.connect(self.close)
        bottom.addWidget(self.close_btn)

        root.addLayout(bottom)
        # --- Первичная инициализация состояния фильтров ---
        # Вызывается ОДИН раз, когда все виджеты уже созданы.
        self._on_due_filter_toggled(False)

    def _send_to_employees(self) -> None:
        """Открывает диалог сопоставления и рассылки по сотрудникам."""
        items = self._selected_items()
        if not items:
            QMessageBox.information(
                self, "Поручения",
                "Выберите поручения для отправки.",
            )
            return

        if self._config_manager is None:
            QMessageBox.warning(
                self, "Поручения",
                "Нет доступа к настройкам (ConfigManager).",
            )
            return

        bitrix_cfg = self._config_manager.get_bitrix_settings()
        if not bitrix_cfg.get("enabled"):
            QMessageBox.information(
                self, "Bitrix24",
                "Интеграция с Bitrix24 отключена.\n\n"
                "Включите её в Настройки → Bitrix24.",
            )
            return

        employees = self._config_manager.get_employees()
        if not employees:
            QMessageBox.information(
                self, "Поручения",
                "Справочник сотрудников пуст.\n\n"
                "Добавьте сотрудников в Настройки → Сотрудники, "
                "чтобы использовать этот способ отправки.",
            )
            return

        # Проверяем, есть ли вообще у поручений исполнители.
        has_assignees = any(
            (it.get("assignee") or "").strip()
            for it in items
        )
        if not has_assignees:
            QMessageBox.information(
                self, "Поручения",
                "У выбранных поручений не заполнено поле "
                "«Исполнитель».\n\n"
                "Отправка сотрудникам невозможна — не с чем "
                "сопоставлять.",
            )
            return

        dlg = SendToEmployeesDialog(
            items=items,
            employees=employees,
            bitrix_cfg=bitrix_cfg,
            parent=self,
        )
        dlg.exec()

    def _on_due_filter_toggled(self, enabled: bool) -> None:
        if not hasattr(self, "due_from"):
            # Вызвано до _build_ui — игнорируем.
            return
        self.due_from.setEnabled(enabled)
        if hasattr(self, "due_to"):
            self.due_to.setEnabled(enabled)
        if hasattr(self, "sort_by_due_check"):
            self._reload_table()

    def _on_search_text_changed(self, _text: str) -> None:
        """Обновляет счётчик найденного при вводе в поле поиска."""
        self._update_search_hint()

    def _on_clear_search(self) -> None:
        """Очищает поле поиска и перезагружает таблицу."""
        self.search_input.clear()
        self._reload_table()
        self._update_search_hint()

    def _update_search_hint(self) -> None:
        """
        Показывает в подсказке поля поиска, сколько записей
        найдено по текущему запросу. Полезно, когда список
        большой и нужно быстро оценить результат.
        """
        query = (self.search_input.text() or "").strip()
        if not query:
            self.search_input.setToolTip(
                "Поиск идёт по тексту поручения, названию "
                "записи (контексту), исполнителю, "
                "комментарию и проекту."
            )
            return

        total = len(self._items)
        shown = self.table.rowCount()
        self.search_input.setToolTip(
            f"Найдено: {shown} из {total} поручений."
        )

    # ------------------------------------------------------------------
    # Данные
    # ------------------------------------------------------------------
    def refresh(self) -> None:
        """Перечитывает все action_items.json из sessions/."""
        self._items = scan_all_action_items(self._sessions_root)
        self._rebuild_filter_options()
        self._reload_table()
        log.info(
            "TasksWindow: обновлено, всего поручений=%d",
            len(self._items),
        )

    def _rebuild_filter_options(self) -> None:
        # --- Исполнители ---
        cur_a = self.assignee_filter.currentData() or ""
        assignees = sorted({
            (it.get("assignee") or "").strip()
            for it in self._items
            if (it.get("assignee") or "").strip()
        })
        self.assignee_filter.blockSignals(True)
        self.assignee_filter.clear()
        self.assignee_filter.addItem("— все —", "")
        for a in assignees:
            self.assignee_filter.addItem(a, a)
        idx = self.assignee_filter.findData(cur_a)
        if idx >= 0:
            self.assignee_filter.setCurrentIndex(idx)
        self.assignee_filter.blockSignals(False)

        # --- Проекты ---
        cur_p = self.project_filter.currentData() or ""
        projects = sorted({
            (it.get("project") or "").strip()
            for it in self._items
            if (it.get("project") or "").strip()
        })
        # Проект лежит не в поручении, а в session.json.
        # Подгружаем отдельно.
        if not projects:
            projects = self._collect_projects_from_sessions()

        self.project_filter.blockSignals(True)
        self.project_filter.clear()
        self.project_filter.addItem("— все —", "")
        for p in projects:
            self.project_filter.addItem(p, p)
        idx = self.project_filter.findData(cur_p)
        if idx >= 0:
            self.project_filter.setCurrentIndex(idx)
        self.project_filter.blockSignals(False)

        # --- Контексты (записи) ---
        cur_c = self.context_filter.currentData() or ""
        contexts = sorted({
            (it.get("session_name") or "").strip()
            for it in self._items
            if (it.get("session_name") or "").strip()
        })
        self.context_filter.blockSignals(True)
        self.context_filter.clear()
        self.context_filter.addItem("— все —", "")
        for c in contexts:
            self.context_filter.addItem(c, c)
        idx = self.context_filter.findData(cur_c)
        if idx >= 0:
            self.context_filter.setCurrentIndex(idx)
        self.context_filter.blockSignals(False)

    def _collect_projects_from_sessions(self) -> List[str]:
        projects = set()
        for it in self._items:
            sdir = it.get("session_dir") or ""
            if not sdir:
                continue
            meta = read_json_file(
                os.path.join(sdir, "session.json")
            ) or {}
            p = (meta.get("project") or "").strip()
            if p:
                projects.add(p)
        return sorted(projects)

    def _project_of(self, item: Dict[str, Any]) -> str:
        sdir = item.get("session_dir") or ""
        if not sdir:
            return ""
        meta = read_json_file(
            os.path.join(sdir, "session.json")
        ) or {}
        return (meta.get("project") or "").strip()

    def _filtered_items(self) -> List[Dict[str, Any]]:
        status_f = self.status_filter.currentData() or ""
        assignee_f = self.assignee_filter.currentData() or ""
        project_f = self.project_filter.currentData() or ""
        context_f = self.context_filter.currentData() or ""
        query = (self.search_input.text() or "").strip().lower()

        overdue_only = self.overdue_only_check.isChecked()
        due_enabled = self.due_filter_enabled.isChecked()
        due_from = self.due_from.date()
        due_to = self.due_to.date()

        # Нормализуем запрос: схлопываем повторные пробелы и
        # убираем пробелы по краям — чтобы «  Иванов  И.И.»
        # совпадал так же, как «Иванов И.И.».
        query_norm = " ".join(query.split())

        result: List[Dict[str, Any]] = []
        for it in self._items:
            if status_f and it.get("status") != status_f:
                continue
            if assignee_f and (it.get("assignee") or "") != assignee_f:
                continue
            if context_f and (it.get("session_name") or "") != context_f:
                continue
            if project_f and self._project_of(it) != project_f:
                continue

            # --- Поиск по свободному тексту ---
            # ВАЖНО: ищем не только по тексту поручения, но и по
            # названию записи (контексту), исполнителю и
            # комментарию. Так одно поле закрывает все типовые
            # сценарии: «найди всё про Иванова», «что было на
            # совещании по проекту X», «все поручения с
            # "миграцией"».
            if query_norm:
                haystack = " ".join([
                    it.get("text") or "",
                    it.get("comment") or "",
                    it.get("assignee") or "",
                    it.get("session_name") or "",
                    self._project_of(it) or "",
                ]).lower()
                haystack_norm = " ".join(haystack.split())
                if query_norm not in haystack_norm:
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
                (x.get("session_name") or ""),
            ))

        return result

    def _reload_table(self) -> None:
        items = self._filtered_items()
        self.table.setRowCount(0)

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
            color = STATUS_COLORS.get(status, "#000000")

            overdue = is_overdue(it)

            raw_due = (it.get("due_date") or "").strip()
            due_is_today = False
            if raw_due:
                try:
                    y, m, d = raw_due.split("-")
                    due_qdate = QDate(int(y), int(m), int(d))
                    due_is_today = (due_qdate == today)
                except Exception:
                    due_is_today = False

            # --- Статус ---
            status_item = QTableWidgetItem(label)
            status_item.setForeground(QColor(color))
            self.table.setItem(row, 0, status_item)

            # --- Текст поручения ---
            text = it.get("text") or ""
            if overdue:
                text = "⚠  " + text
            text_item = QTableWidgetItem(text)
            self.table.setItem(row, 1, text_item)

            # --- Исполнитель ---
            self.table.setItem(
                row, 2, QTableWidgetItem(it.get("assignee") or "—")
            )

            # --- Срок ---
            due_text = raw_due or "—"
            if overdue:
                days = days_until_due(it) or 0
                due_text = (
                    f"{raw_due}  (просрочено на {abs(days)} дн.)"
                )
            elif due_is_today:
                due_text = f"{raw_due}  (сегодня)"

            due_item = QTableWidgetItem(due_text)
            if overdue:
                due_item.setForeground(overdue_fg)
            elif due_is_today:
                due_item.setForeground(today_fg)
            self.table.setItem(row, 3, due_item)

            # --- Проект ---
            self.table.setItem(
                row, 4,
                QTableWidgetItem(self._project_of(it) or "—"),
            )

            # --- Контекст ---
            self.table.setItem(
                row, 5,
                QTableWidgetItem(it.get("session_name") or "—"),
            )

            # --- ID ---
            id_item = QTableWidgetItem(it.get("id") or "")
            id_item.setForeground(QColor("#888"))
            self.table.setItem(row, 6, id_item)

            # --- Подсветка ---
            if overdue:
                for col in range(self.table.columnCount()):
                    cell = self.table.item(row, col)
                    if cell is None:
                        continue
                    cell.setBackground(overdue_bg)
                    if col != 0:
                        cell.setForeground(overdue_fg)
            elif due_is_today and status != "done":
                for col in range(self.table.columnCount()):
                    cell = self.table.item(row, col)
                    if cell is None:
                        continue
                    cell.setBackground(today_bg)

        # --- Итоги ---
        total = len(self._items)
        overdue_count = sum(
            1 for x in self._items if is_overdue(x)
        )
        try:
            n_sel = len(
                self.table.selectionModel().selectedRows()
            )
        except Exception:
            n_sel = 0

        stats = (
            f"Всего: {total} · показано: {self.table.rowCount()}"
            f" · выбрано: {n_sel}"
        )
        if overdue_count:
            stats += f" · просрочено: {overdue_count}"

        query = (self.search_input.text() or "").strip()
        if query:
            stats += f" · поиск: «{query}»"

        self.stats_label.setText(stats)
        self._update_search_hint()

    def _update_selection_stats(self) -> None:
        try:
            n_sel = len(
                self.table.selectionModel().selectedRows()
            )
        except Exception:
            n_sel = 0
        total = len(self._items)
        overdue_count = sum(
            1 for x in self._items if is_overdue(x)
        )
        stats = (
            f"Всего: {total} · "
            f"показано: {self.table.rowCount()} · "
            f"выбрано: {n_sel}"
        )
        if overdue_count:
            stats += f" · просрочено: {overdue_count}"
        self.stats_label.setText(stats)

    # ------------------------------------------------------------------
    # Действия
    # ------------------------------------------------------------------
    def _select_all(self) -> None:
        self.table.selectAll()

    def _clear_selection(self) -> None:
        self.table.clearSelection()

    def _selected_items(self) -> List[Dict[str, Any]]:
        rows = sorted(
            {i.row() for i in self.table.selectionModel().selectedRows()}
        )
        result: List[Dict[str, Any]] = []
        for r in rows:
            id_item = self.table.item(r, 6)
            if id_item is None:
                continue
            item_id = id_item.text()
            for it in self._items:
                if it.get("id") == item_id:
                    result.append(it)
                    break
        return result

    def _open_editor_for_current(self) -> None:
        items = self._selected_items()
        if not items:
            QMessageBox.information(
                self, "Поручения", "Выберите поручение."
            )
            return
        sdir = items[0].get("session_dir") or ""
        if not sdir or not os.path.isdir(sdir):
            QMessageBox.warning(
                self, "Поручения",
                "Папка записи не найдена.",
            )
            return
        dlg = TasksEditorDialog(
            session_dir=sdir,
            session_name=items[0].get("session_name") or "",
            parent=self,
        )
        dlg.exec()
        self.refresh()

    def _delete_selected(self) -> None:
        items = self._selected_items()
        if not items:
            QMessageBox.information(
                self, "Поручения", "Выберите поручения."
            )
            return
        reply = QMessageBox.question(
            self, "Удаление",
            f"Удалить {len(items)} поручени(й)?",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        deleted = 0
        for it in items:
            sdir = it.get("session_dir") or ""
            if not sdir:
                continue
            if delete_item(sdir, it.get("id") or ""):
                deleted += 1

        QMessageBox.information(
            self, "Поручения",
            f"Удалено: {deleted}",
        )
        self.refresh()

    def _apply_status(self) -> None:
        items = self._selected_items()
        if not items:
            QMessageBox.information(
                self, "Поручения", "Выберите поручения."
            )
            return
        new_status = self.status_combo.currentData() or "created"
        for it in items:
            sdir = it.get("session_dir") or ""
            if not sdir:
                continue
            update_item(
                sdir, it.get("id") or "",
                {"status": new_status},
            )
        self.refresh()

    # ------------------------------------------------------------------
    # Отправка в Bitrix24
    # ------------------------------------------------------------------
    def _send_to_bitrix(self) -> None:
        items = self._selected_items()
        if not items:
            QMessageBox.information(
                self, "Поручения",
                "Выберите поручения для отправки.",
            )
            return

        if self._config_manager is None:
            QMessageBox.warning(
                self, "Поручения",
                "Нет доступа к настройкам (ConfigManager).",
            )
            return

        bitrix_cfg = self._config_manager.get_bitrix_settings()
        if not bitrix_cfg.get("enabled"):
            QMessageBox.information(
                self, "Bitrix24",
                "Интеграция с Bitrix24 отключена.\n\n"
                "Включите её в Настройки → Bitrix24.",
            )
            return

        projects = self._config_manager.get_projects()
        employees = self._config_manager.get_employees()
        default_chat_id = (
            self._config_manager.get_resolved_default_chat_id()
        )

        dlg = _SendTasksDialog(
            items=items,
            bitrix_cfg=bitrix_cfg,
            projects=projects,
            employees=employees,
            default_chat_id=default_chat_id,
            parent=self,
        )
        dlg.exec()