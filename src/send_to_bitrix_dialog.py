"""Диалог отправки протокола/summary в чат Bitrix24.

Поддерживает массовую рассылку: одно и то же содержимое можно
отправить сразу в несколько чатов — выбранных из справочника
проектов, сотрудников или введённых вручную.

Режимы отправки:
  • текстом в чат (im.message.add с MESSAGE);
  • файлом с комментарием (загрузка на Диск + im.message.add с FILE_ID).

Папка для загрузки файла выбирается автоматически:
  • если задан upload_folder_id > 0 в настройках — используется он;
  • иначе — папка самого чата (у каждого чата в Bitrix24 она своя).

Если выбрано «Отправить всё» и оба материала уходят файлами, они
упаковываются в ОДНО сообщение с двумя вложениями.

Особенности UI (актуальная версия):
  • Список получателей — QListWidget с чекбоксами, разделён на
    секции «Проекты» и «Сотрудники».
  • Строки без chat_id нельзя отметить, они серые и с подсказкой
    «Укажите ID в Настройках…».
  • Есть кнопки «Выбрать все», «Снять все», «Показать выбранные»,
    плюс счётчик выбранных получателей.
  • Под списком — предупреждение красным, если ничего не выбрано.
  • Список растягивается вместе с окном, при большом количестве
    элементов появляется вертикальный скролл.
  • Горизонтальный скролл отключён, длинные имена обрезаются
    многоточием.
"""
from __future__ import annotations

import asyncio
import html
import os
import tempfile
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QVBoxLayout,
    QWidget,
)

from .bbcode_editor import bbcode_to_bitrix, bbcode_to_plain
from .bitrix_client import Bitrix24Client, Bitrix24Error
from .logger import get_logger
from .tooltips import with_info

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------
def _read_text_safe(path: str, max_chars: int = 200000) -> str:
    """Читает файл как текст, с ограничением длины."""
    if not path or not os.path.exists(path):
        return ""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".docx":
            from docx import Document  # type: ignore
            doc = Document(path)
            return "\n".join(p.text for p in doc.paragraphs)[:max_chars]
        if ext == ".pdf":
            try:
                from pypdf import PdfReader  # type: ignore
                reader = PdfReader(path)
                text = "\n".join(
                    (pg.extract_text() or "") for pg in reader.pages
                )
                return text[:max_chars]
            except ImportError:
                return ""
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(max_chars)
    except Exception as exc:
        log.warning("Не удалось прочитать %s: %s", path, exc)
        return ""


def _safe_filename(name: str) -> str:
    """Безопасное имя файла для временных документов."""
    bad = '<>:"/\\|?*\n\r\t'
    cleaned = "".join(("_" if c in bad else c) for c in (name or "document"))
    cleaned = cleaned.strip() or "document"
    return cleaned[:60]


# ---------------------------------------------------------------------------
# Диалог
# ---------------------------------------------------------------------------
class SendToBitrixDialog(QDialog):
    """
    Диалог отправки протокола / summary в чат Bitrix24.

    Поддерживает массовую рассылку: один и тот же материал уходит
    сразу в несколько выбранных чатов (проекты / сотрудники / ручные ID).

    Параметры:
        session_info: dict с полями:
            - name: имя записи
            - project: проект
            - date: дата
            - protocol_path: путь к протоколу (может быть "")
            - protocol_label: имя файла протокола
            - summary_bb: текст summary (BB-код, может быть "")
            - comment: комментарий к записи
        chat_id: ID чата Bitrix24 (определяется по проекту) — начальный
                 выбранный получатель, может быть пустым.
        bitrix_cfg: настройки интеграции (из ConfigManager).
        projects: список проектов [{"name", "chat_id"}, ...].
        employees: список сотрудников [{"name", "chat_id"}, ...].
    """

    def __init__(
        self,
        session_info: Dict[str, Any],
        chat_id: str,
        bitrix_cfg: Dict[str, Any],
        projects: Optional[List[Dict[str, str]]] = None,
        employees: Optional[List[Dict[str, str]]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.session_info = session_info or {}
        self.chat_id = chat_id or ""
        self.bitrix_cfg = bitrix_cfg or {}
        self.projects = list(projects or [])
        self.employees = list(employees or [])

        self.setWindowTitle(
            f"Отправка в Bitrix24 — {self.session_info.get('name', '')}"
        )
        self.setModal(True)
        self.setMinimumSize(900, 820)

        self._protocol_path = self.session_info.get("protocol_path") or ""
        self._protocol_text = ""
        self._summary_bb = self.session_info.get("summary_bb") or ""

        # Plain-версия summary — для превью пользователю.
        self._summary_plain = (
            bbcode_to_plain(self._summary_bb).strip()
            if self._summary_bb else ""
        )
        # Bitrix24-версия summary — то, что реально уйдёт в чат:
        # BB-код с тегами в верхнем регистре ([B], [I], [URL], ...).
        self._summary_bitrix = (
            bbcode_to_bitrix(self._summary_bb).strip()
            if self._summary_bb else ""
        )

        # Множество выбранных chat_id (для быстрого доступа).
        self._selected_chat_ids: Set[str] = set()

        # Указатели на элементы управления, создаваемые в _build_ui.
        # Нужны для методов, которые могут быть вызваны до их создания.
        self.selected_count_label: Optional[QLabel] = None
        self.empty_hint: Optional[QLabel] = None

        self._build_ui()
        self._init_recipient_state()
        self._load_previews()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # --- Информация о записи ---
        info_row = QHBoxLayout()
        name = self.session_info.get("name", "")
        project = self.session_info.get("project", "")
        date = self.session_info.get("date", "")
        comment = self.session_info.get("comment", "")

        info_text = (
            f"<b>{html.escape(name)}</b><br>"
            f"Проект: <b>{html.escape(project or '—')}</b> &nbsp;|&nbsp; "
            f"Дата: {html.escape(date or '—')}"
        )
        if comment:
            info_text += (
                f"<br><span style='color:#666'>Комментарий: "
                f"{html.escape(comment)}</span>"
            )
        info = QLabel(info_text)
        info.setWordWrap(True)
        info_row.addWidget(info, 1)
        root.addLayout(info_row)

        # --- Получатели (мультивыбор) ---
        rec_header = QHBoxLayout()
        rec_header.addWidget(QLabel("<b>Кому отправить</b>"))
        rec_header.addStretch()
        root.addLayout(rec_header)

        rec_hint = QLabel(
            "<span style='color:#666'>Отметьте галочками один или "
            "несколько чатов. Можно комбинировать проекты, сотрудников "
            "и вводить ID вручную — сообщение уйдёт каждому адресату "
            "отдельно.</span>"
        )
        rec_hint.setWordWrap(True)
        root.addWidget(rec_hint)

        # Список получателей с чекбоксами.
        self.recipients_list = QListWidget()
        self.recipients_list.setMinimumHeight(260)
        self.recipients_list.setToolTip(
            "Список проектов и сотрудников.\n\n"
            "Клик по строке переключает галочку. "
            "Можно выбрать сразу несколько получателей."
        )
        # Горизонтальный скролл не нужен — длинные имена обрезаются.
        self.recipients_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.recipients_list.setTextElideMode(
            Qt.TextElideMode.ElideRight
        )
        self.recipients_list.itemChanged.connect(
            self._on_recipient_item_changed
        )

        # Растягиваем список по вертикали вместе с окном.
        list_row = QHBoxLayout()
        list_row.addWidget(self.recipients_list, 1)
        root.addLayout(list_row, 1)

        # --- Предупреждение о пустом выборе ---
        self.empty_hint = QLabel(
            "<span style='color:#c62828'>"
            "Ни один получатель не отмечен. Отметьте чаты в списке "
            "или введите ID в поле «Доп. ID чата» ниже."
            "</span>"
        )
        self.empty_hint.setWordWrap(True)
        self.empty_hint.setVisible(True)  # обновится в _init_recipient_state
        root.addWidget(self.empty_hint)

        # --- Кнопки управления списком ---
        list_btns = QHBoxLayout()
        self.select_all_btn = QPushButton("Выбрать все")
        self.select_all_btn.setToolTip(
            "Отметить все чаты в списке (проекты и сотрудники)"
        )
        self.select_all_btn.clicked.connect(self._on_select_all)
        list_btns.addWidget(self.select_all_btn)

        self.unselect_all_btn = QPushButton("Снять все")
        self.unselect_all_btn.setToolTip(
            "Снять все галочки в списке получателей"
        )
        self.unselect_all_btn.clicked.connect(self._on_unselect_all)
        list_btns.addWidget(self.unselect_all_btn)

        self.show_selected_btn = QPushButton("Показать выбранные")
        self.show_selected_btn.setToolTip(
            "Показать только отмеченные чаты — удобно для проверки "
            "перед массовой рассылкой"
        )
        self.show_selected_btn.setCheckable(True)
        self.show_selected_btn.toggled.connect(
            self._on_toggle_show_selected
        )
        list_btns.addWidget(self.show_selected_btn)

        list_btns.addStretch()

        self.selected_count_label = QLabel("Ничего не выбрано")
        self.selected_count_label.setStyleSheet(
            "QLabel { color: #444; font-weight: bold; }"
        )
        list_btns.addWidget(self.selected_count_label)
        root.addLayout(list_btns)

        # --- Ручной ID ---
        manual_form = QFormLayout()
        self.manual_chat_input = QLineEdit()
        self.manual_chat_input.setPlaceholderText(
            "Например: chat2101 или 123 — добавится к выбранным"
        )
        manual_form.addRow(
            "Доп. ID чата:",
            with_info(self.manual_chat_input, "bitrix_chat_id"),
        )
        root.addLayout(manual_form)

        # --- Вебхук ---
        chat_form = QFormLayout()

        self.webhook_input = QLineEdit(
            self.bitrix_cfg.get("webhook_url", "") or ""
        )
        self.webhook_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.webhook_input.setPlaceholderText(
            "https://portal.bitrix24.ru/rest/1/token/"
        )

        self.show_webhook = QCheckBox("Показать")
        self.show_webhook.toggled.connect(
            lambda checked: self.webhook_input.setEchoMode(
                QLineEdit.EchoMode.Normal if checked
                else QLineEdit.EchoMode.Password
            )
        )

        webhook_row = QWidget()
        wh = QHBoxLayout(webhook_row)
        wh.setContentsMargins(0, 0, 0, 0)
        wh.setSpacing(6)
        wh.addWidget(self.webhook_input, 1)
        wh.addWidget(self.show_webhook, 0)
        chat_form.addRow(
            "Вебхук Bitrix24:",
            with_info(webhook_row, "bitrix_webhook"),
        )

        # Опции отправки
        self.system_check = QCheckBox("Системное сообщение (SYSTEM=Y)")
        self.system_check.setChecked(
            bool(self.bitrix_cfg.get("system_message", False))
        )
        self.system_check.setToolTip(
            "Если включено — сообщение отображается как системное, "
            "без аватара отправителя."
        )

        self.no_preview_check = QCheckBox("Отключить предпросмотр ссылок")
        self.no_preview_check.setChecked(
            bool(self.bitrix_cfg.get("disable_url_preview", False))
        )

        opts_row = QHBoxLayout()
        opts_row.addWidget(self.system_check)
        opts_row.addWidget(self.no_preview_check)
        opts_row.addStretch()
        chat_form.addRow("", self._wrap_h(opts_row))
        root.addLayout(chat_form)

        # --- Заголовки сообщений (редактируемые) ---
        header_header = QHBoxLayout()
        self.header_check = QCheckBox("Добавлять заголовок к сообщениям")
        self.header_check.setChecked(
            bool(self.bitrix_cfg.get("include_header", True))
        )
        self.header_check.setToolTip(
            "Если снять — сообщения уйдут без заголовка, "
            "только текст протокола / summary."
        )
        header_header.addWidget(self.header_check)
        header_header.addStretch()
        root.addLayout(header_header)

        headers_hint = QLabel(
            "<span style='color:#666'>Заголовок добавляется в начало "
            "сообщения. Оформить его можно вручную, если нужно. "
            "Пустое поле — заголовок не добавляется. При массовой "
            "рассылке заголовок у всех получателей одинаковый.</span>"
        )
        headers_hint.setWordWrap(True)
        root.addWidget(headers_hint)

        headers_form = QFormLayout()

        self.protocol_header_input = QLineEdit()
        self.protocol_header_input.setPlaceholderText(
            "Например: Протокол: Название встречи — 2026-09-28"
        )
        self.protocol_header_input.setToolTip(
            "Заголовок для сообщения с протоколом.\n\n"
            "Заполняется автоматически по шаблону "
            "«Протокол: <название> — <дата>». Можно отредактировать "
            "или очистить поле — тогда сообщение уйдёт без заголовка."
        )

        self.summary_header_input = QLineEdit()
        self.summary_header_input.setPlaceholderText(
            "Например: Краткое описание: Название встречи — 2026-09-28"
        )
        self.summary_header_input.setToolTip(
            "Заголовок для сообщения с кратким описанием (summary).\n\n"
            "Заполняется автоматически по шаблону "
            "«Краткое описание: <название> — <дата>». Можно "
            "отредактировать или очистить поле — тогда сообщение "
            "уйдёт без заголовка."
        )

        headers_form.addRow(
            "Заголовок протокола:", self.protocol_header_input
        )
        headers_form.addRow(
            "Заголовок summary:", self.summary_header_input
        )
        root.addLayout(headers_form)

        self._recalc_protocol_header_default()
        self._recalc_summary_header_default()

        self.header_check.toggled.connect(self._on_header_toggled)
        self._on_header_toggled(self.header_check.isChecked())

        # --- Режим отправки ---
        mode_box = QFormLayout()

        self.send_mode_combo = QComboBox()
        self.send_mode_combo.addItem(
            "Автоматически (текст или файл)", "auto"
        )
        self.send_mode_combo.addItem("Текстом в чат", "text")
        self.send_mode_combo.addItem("Файлом с комментарием", "file")
        self.send_mode_combo.setToolTip(
            "Как отправлять содержимое в чат:\n\n"
            "• «Текстом» — весь текст уходит сообщением.\n"
            "• «Файлом» — документы загружаются на Диск Bitrix24, "
            "в чат отправляются вложения с превью и коротким "
            "комментарием. Если отправляется и протокол, и summary — "
            "оба файла уходят одним сообщением.\n"
            "• «Автоматически» — небольшие тексты уходят как текст, "
            "крупные (протоколы, длинные summary) — как файл."
        )
        mode_box.addRow("Режим отправки:", self.send_mode_combo)

        self.auto_file_threshold = QSpinBox()
        self.auto_file_threshold.setRange(500, 100000)
        self.auto_file_threshold.setSingleStep(500)
        self.auto_file_threshold.setValue(
            int(self.bitrix_cfg.get("file_message_max_chars", 3000))
        )
        self.auto_file_threshold.setSuffix(" символов")
        self.auto_file_threshold.setToolTip(
            "Порог для автоматического режима:\n"
            "если текст длиннее — уходит файлом, если короче — "
            "сообщением."
        )
        mode_box.addRow("Порог «текст → файл»:", self.auto_file_threshold)

        root.addLayout(mode_box)

        self.send_mode_combo.currentIndexChanged.connect(
            self._on_send_mode_changed
        )
        self._on_send_mode_changed()

        # --- Проверка подключения ---
        test_row = QHBoxLayout()
        self.test_btn = QPushButton("Проверить подключение")
        self.test_btn.clicked.connect(self._on_test)
        test_row.addWidget(self.test_btn)
        test_row.addStretch()
        root.addLayout(test_row)

        # --- Превью протокола ---
        proto_header = QHBoxLayout()
        proto_header.addWidget(QLabel("<b>Протокол</b>"))
        self.protocol_status = QLabel("")
        self.protocol_status.setStyleSheet("QLabel { color: #666; }")
        proto_header.addWidget(self.protocol_status)
        proto_header.addStretch()
        root.addLayout(proto_header)

        self.protocol_view = QPlainTextEdit()
        self.protocol_view.setReadOnly(True)
        self.protocol_view.setMinimumHeight(120)
        root.addWidget(self.protocol_view)

        proto_btns = QHBoxLayout()
        self.send_protocol_btn = QPushButton("Отправить протокол")
        self.send_protocol_btn.clicked.connect(
            lambda: self._on_send(which="protocol")
        )
        proto_btns.addWidget(self.send_protocol_btn)
        proto_btns.addStretch()
        root.addLayout(proto_btns)

        # --- Превью summary ---
        sum_header = QHBoxLayout()
        sum_header.addWidget(QLabel("<b>Summary (краткое описание)</b>"))
        self.summary_status = QLabel("")
        self.summary_status.setStyleSheet("QLabel { color: #666; }")
        sum_header.addWidget(self.summary_status)
        sum_header.addStretch()
        root.addLayout(sum_header)

        self.summary_view = QPlainTextEdit()
        self.summary_view.setReadOnly(True)
        self.summary_view.setMinimumHeight(100)
        root.addWidget(self.summary_view)

        sum_btns = QHBoxLayout()
        self.send_summary_btn = QPushButton("Отправить summary")
        self.send_summary_btn.clicked.connect(
            lambda: self._on_send(which="summary")
        )
        sum_btns.addWidget(self.send_summary_btn)

        self.send_both_btn = QPushButton("Отправить всё")
        self.send_both_btn.setToolTip(
            "Отправить двумя сообщениями: сначала протокол, "
            "потом summary.\n\n"
            "В режиме «Файлом с комментарием» оба файла уйдут "
            "одним сообщением с двумя вложениями.\n\n"
            "Массовая рассылка: содержимое уйдёт всем отмеченным "
            "получателям."
        )
        self.send_both_btn.clicked.connect(
            lambda: self._on_send(which="both")
        )
        sum_btns.addWidget(self.send_both_btn)
        sum_btns.addStretch()
        root.addLayout(sum_btns)

        # --- Нижние кнопки ---
        bottom = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Close,
            parent=self,
        )
        bottom.button(
            QDialogButtonBox.StandardButton.Close
        ).setText("Закрыть")
        bottom.rejected.connect(self.reject)
        bottom.accepted.connect(self.accept)
        root.addWidget(bottom)

    @staticmethod
    def _wrap_h(layout) -> QWidget:
        w = QWidget()
        w.setLayout(layout)
        return w

    # ------------------------------------------------------------------
    # Логика выбора получателей (мультивыбор)
    # ------------------------------------------------------------------
    def _init_recipient_state(self) -> None:
        """
        Заполняет список получателей проектами и сотрудниками,
        отмечая галочками те, чей chat_id совпадает с переданным
        self.chat_id (если такой есть).

        Строки без chat_id:
          • серые;
          • без флага ItemIsUserCheckable — отметить нельзя;
          • с всплывающей подсказкой, где искать ID.
        """
        self.recipients_list.blockSignals(True)
        self.recipients_list.clear()

        selected_ids: Set[str] = set()
        if self.chat_id:
            selected_ids.add(self.chat_id)

        # --- Проекты ---
        if self.projects:
            header = self._make_section_header("Проекты")
            self.recipients_list.addItem(header)

            for p in self.projects:
                chat_id = (p.get("chat_id") or "").strip()
                label = p["name"]
                if chat_id:
                    label += f"  ({chat_id})"
                else:
                    label += "  — чат не задан (нельзя выбрать)"

                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, chat_id)

                if chat_id:
                    item.setFlags(
                        Qt.ItemFlag.ItemIsEnabled
                        | Qt.ItemFlag.ItemIsUserCheckable
                    )
                    if chat_id in selected_ids:
                        item.setCheckState(Qt.CheckState.Checked)
                    else:
                        item.setCheckState(Qt.CheckState.Unchecked)
                else:
                    # Без chat_id — отметить нельзя.
                    item.setFlags(Qt.ItemFlag.NoItemFlags)
                    item.setForeground(Qt.GlobalColor.darkGray)
                    item.setToolTip(
                        f"У проекта «{p['name']}» не задан ID чата.\n\n"
                        "Укажите его в Настройках → "
                        "Проекты и чаты Bitrix24 — после этого "
                        "проект можно будет выбрать здесь."
                    )
                self.recipients_list.addItem(item)

        # --- Сотрудники ---
        if self.employees:
            header = self._make_section_header("Сотрудники")
            self.recipients_list.addItem(header)

            for e in self.employees:
                chat_id = (e.get("chat_id") or "").strip()
                label = e["name"]
                if chat_id:
                    label += f"  ({chat_id})"
                else:
                    label += "  — чат не задан (нельзя выбрать)"

                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, chat_id)

                if chat_id:
                    item.setFlags(
                        Qt.ItemFlag.ItemIsEnabled
                        | Qt.ItemFlag.ItemIsUserCheckable
                    )
                    if chat_id in selected_ids:
                        item.setCheckState(Qt.CheckState.Checked)
                    else:
                        item.setCheckState(Qt.CheckState.Unchecked)
                else:
                    item.setFlags(Qt.ItemFlag.NoItemFlags)
                    item.setForeground(Qt.GlobalColor.darkGray)
                    item.setToolTip(
                        f"У сотрудника «{e['name']}» не задан ID чата.\n\n"
                        "Укажите его в Настройках → Сотрудники — "
                        "после этого сотрудника можно будет "
                        "выбрать здесь."
                    )
                self.recipients_list.addItem(item)

        self.recipients_list.blockSignals(False)
        self._refresh_selected_chat_ids()
        self._update_send_buttons_state()

    @staticmethod
    def _make_section_header(text: str) -> QListWidgetItem:
        """Создаёт заголовок секции списка получателей."""
        header = QListWidgetItem(f"── {text} ──")
        header.setFlags(Qt.ItemFlag.NoItemFlags)
        header.setForeground(Qt.GlobalColor.darkGray)
        font = header.font()
        font.setBold(True)
        header.setFont(font)
        return header

    def _refresh_selected_chat_ids(self) -> None:
        """Пересобирает self._selected_chat_ids из отмеченных пунктов
        и обновляет счётчик + видимость подсказки о пустом выборе."""
        ids: Set[str] = set()
        for i in range(self.recipients_list.count()):
            item = self.recipients_list.item(i)
            if item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                if item.checkState() == Qt.CheckState.Checked:
                    cid = (item.data(Qt.ItemDataRole.UserRole) or "").strip()
                    if cid:
                        ids.add(cid)
        self._selected_chat_ids = ids

        # Обновляем счётчик.
        if self.selected_count_label is not None:
            n = len(ids)
            if n == 0:
                self.selected_count_label.setText("Ничего не выбрано")
            elif n == 1:
                self.selected_count_label.setText("Выбрано: 1")
            else:
                self.selected_count_label.setText(f"Выбрано: {n}")

        # Обновляем видимость подсказки о пустом выборе.
        if self.empty_hint is not None:
            self.empty_hint.setVisible(not ids)

    def _on_recipient_item_changed(self, item: QListWidgetItem) -> None:
        """Реакция на переключение галочки у элемента списка."""
        self._refresh_selected_chat_ids()
        self._update_send_buttons_state()

    def _on_select_all(self) -> None:
        self.recipients_list.blockSignals(True)
        for i in range(self.recipients_list.count()):
            item = self.recipients_list.item(i)
            if item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                cid = (item.data(Qt.ItemDataRole.UserRole) or "").strip()
                if cid:
                    item.setCheckState(Qt.CheckState.Checked)
        self.recipients_list.blockSignals(False)
        self._refresh_selected_chat_ids()
        self._update_send_buttons_state()

    def _on_unselect_all(self) -> None:
        self.recipients_list.blockSignals(True)
        for i in range(self.recipients_list.count()):
            item = self.recipients_list.item(i)
            if item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                item.setCheckState(Qt.CheckState.Unchecked)
        self.recipients_list.blockSignals(False)
        self._refresh_selected_chat_ids()
        self._update_send_buttons_state()

    def _on_toggle_show_selected(self, checked: bool) -> None:
        """Скрывает невыбранные элементы списка, если включено."""
        for i in range(self.recipients_list.count()):
            item = self.recipients_list.item(i)
            if item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                if checked:
                    item.setHidden(
                        item.checkState() != Qt.CheckState.Checked
                    )
                else:
                    item.setHidden(False)

    def _update_send_buttons_state(self) -> None:
        """Кнопки отправки активны, если есть хотя бы один получатель."""
        has_recipients = bool(self._selected_chat_ids)
        for btn in (
            self.send_protocol_btn,
            self.send_summary_btn,
            self.send_both_btn,
        ):
            if not has_recipients:
                btn.setEnabled(False)
                btn.setToolTip("Отметьте хотя бы одного получателя")
            else:
                btn.setEnabled(True)
                btn.setToolTip("")

    def _resolve_chat_ids(self) -> List[str]:
        """
        Возвращает список итоговых chat_id:
          • все отмеченные в списке;
          • плюс ручной ID из поля «Доп. ID чата», если он заполнен.

        Дубликаты убираются с сохранением порядка.
        """
        ids: List[str] = []
        seen: Set[str] = set()

        for cid in self._selected_chat_ids:
            if cid and cid not in seen:
                ids.append(cid)
                seen.add(cid)

        manual = self.manual_chat_input.text().strip()
        if manual and manual not in seen:
            ids.append(manual)
            seen.add(manual)

        return ids

    def _describe_recipients(self, ids: List[str]) -> str:
        """
        Возвращает человекочитаемое описание получателей для
        подтверждающего диалога.
        """
        if not ids:
            return "(нет получателей)"

        name_by_id: Dict[str, str] = {}
        for p in self.projects:
            cid = (p.get("chat_id") or "").strip()
            if cid:
                name_by_id.setdefault(cid, f"проект «{p['name']}»")
        for e in self.employees:
            cid = (e.get("chat_id") or "").strip()
            if cid:
                name_by_id.setdefault(cid, f"сотрудник «{e['name']}»")

        lines: List[str] = []
        for cid in ids[:20]:
            name = name_by_id.get(cid, "ручной ввод")
            lines.append(f"• {cid} — {name}")
        if len(ids) > 20:
            lines.append(f"… и ещё {len(ids) - 20} получателей")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Заголовки по умолчанию
    # ------------------------------------------------------------------
    def _build_default_header(self, which: str) -> str:
        """Формирует заголовок по умолчанию для протокола или summary."""
        name = self.session_info.get("name", "")
        date = self.session_info.get("date", "")

        if date:
            date = date.strip()
            if " " in date:
                first = date.split(" ", 1)[0]
                rest = date.split(" ", 1)[1]
                if rest in ("00:00:00", "00:00"):
                    date = first
                else:
                    date = f"{first} {rest}"

        if which == "protocol":
            title = "Протокол"
        elif which == "summary":
            title = "Краткое описание"
        else:
            title = "Материалы"

        parts = [title]
        if name:
            parts.append(name)
        if date:
            parts.append(date)

        if len(parts) == 1:
            return parts[0]
        return f"{parts[0]}: " + " — ".join(parts[1:])

    def _recalc_protocol_header_default(self) -> None:
        self.protocol_header_input.setText(
            self._build_default_header("protocol")
        )

    def _recalc_summary_header_default(self) -> None:
        self.summary_header_input.setText(
            self._build_default_header("summary")
        )

    def _on_header_toggled(self, checked: bool) -> None:
        self.protocol_header_input.setEnabled(checked)
        self.summary_header_input.setEnabled(checked)

    # ------------------------------------------------------------------
    # Режим отправки
    # ------------------------------------------------------------------
    def _on_send_mode_changed(self) -> None:
        mode = self.send_mode_combo.currentData() or "auto"
        self.auto_file_threshold.setEnabled(mode == "auto")

    def _is_file_mode(self, which: str, body_len: int) -> bool:
        mode = self.send_mode_combo.currentData() or "auto"
        if mode == "file":
            return True
        if mode == "text":
            return False
        return body_len > self.auto_file_threshold.value()

    # ------------------------------------------------------------------
    # Загрузка превью
    # ------------------------------------------------------------------
    def _load_previews(self) -> None:
        if self._protocol_path and os.path.exists(self._protocol_path):
            self._protocol_text = _read_text_safe(self._protocol_path)
            self.protocol_view.setPlainText(
                self._protocol_text[:8000]
                or "(файл пуст или не читается)"
            )
            size = os.path.getsize(self._protocol_path)
            self.protocol_status.setText(
                f"— {os.path.basename(self._protocol_path)} "
                f"({size / 1024:.1f} КБ, {len(self._protocol_text)} символов)"
            )
        else:
            self.protocol_view.setPlainText(
                "Протокол не прикреплён к этой записи.\n\n"
                "Прикрепите его через меню «Протокол → Прикрепить протокол…» "
                "в окне «Записи»."
            )
            self.protocol_status.setText("— не прикреплён")

        if self._summary_bitrix:
            self.summary_view.setPlainText(self._summary_plain[:8000])
            self.summary_status.setText(
                f"— {len(self._summary_plain)} символов "
                f"(BB → plain для превью, в чат уйдёт с форматированием)"
            )
        else:
            self.summary_view.setPlainText(
                "У этой записи нет краткого описания.\n\n"
                "Добавьте его через меню «Summary → Изменить summary…» "
                "в окне «Записи»."
            )
            self.summary_status.setText("— пусто")

    # ------------------------------------------------------------------
    # Проверка подключения
    # ------------------------------------------------------------------
    def _on_test(self) -> None:
        webhook = self.webhook_input.text().strip()
        if not webhook:
            QMessageBox.warning(
                self, "Bitrix24",
                "Укажите URL вебхука Bitrix24.",
            )
            return

        connect_timeout = float(self.bitrix_cfg.get("connect_timeout", 15))
        read_timeout = float(self.bitrix_cfg.get("read_timeout", 60))

        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

        async def _run_ping() -> str:
            async with Bitrix24Client(
                webhook_url=webhook,
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
            ) as client:
                return await client.ping()

        try:
            name = asyncio.run(_run_ping())
        except Bitrix24Error as exc:
            log.warning("Проверка Bitrix24 не удалась: %s", exc)
            QMessageBox.warning(
                self, "Bitrix24",
                f"Не удалось подключиться:\n\n{exc}",
            )
            return
        except Exception as exc:
            log.exception("Неожиданная ошибка при ping Bitrix24: %s", exc)
            QMessageBox.critical(self, "Bitrix24", f"Ошибка:\n{exc}")
            return
        finally:
            if QGuiApplication.overrideCursor() is not None:
                QGuiApplication.restoreOverrideCursor()

        QMessageBox.information(
            self, "Bitrix24",
            f"Подключение успешно.\n\nПользователь вебхука: {name}",
        )

    # ------------------------------------------------------------------
    # Формирование сообщения
    # ------------------------------------------------------------------
    def _build_text_message(self, item: Dict[str, Any]) -> str:
        header = item.get("header", "")
        body = item.get("body", "")
        if header:
            return f"[B]{header}[/B]\n\n{body}".strip()
        return body.strip()

    def _prepare_file_for_item(self, item: Dict[str, Any]) -> str:
        which = item["which"]
        src = item.get("src_file") or ""

        if src and os.path.exists(src):
            log.info("Отправка исходного файла: %s", src)
            return src

        body = item.get("body", "")
        header = item.get("header", "") or ""

        tmpdir = tempfile.gettempdir()
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        safe_name = _safe_filename(
            self.session_info.get("name") or "document"
        )

        if which == "summary":
            text = body
            filename = f"summary_{safe_name}_{stamp}.md"
            full_text = (f"# {header}\n\n{text}" if header else text)
            path = os.path.join(tmpdir, filename)
            with open(path, "w", encoding="utf-8") as f:
                f.write(full_text)
            item["_tmp"] = True
            log.info(
                "Подготовлен временный файл summary: %s (%d символов)",
                path, len(full_text),
            )
            return path

        text = body
        filename = f"protocol_{safe_name}_{stamp}.txt"
        full_text = (f"{header}\n\n{text}" if header else text)
        path = os.path.join(tmpdir, filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write(full_text)
        item["_tmp"] = True
        log.info(
            "Подготовлен временный файл протокола: %s (%d символов)",
            path, len(full_text),
        )
        return path

    # ------------------------------------------------------------------
    # Отправка (массовая рассылка)
    # ------------------------------------------------------------------
    def _on_send(self, which: str) -> None:
        webhook = self.webhook_input.text().strip()
        chat_ids = self._resolve_chat_ids()

        if not webhook:
            QMessageBox.warning(self, "Bitrix24",
                                "Укажите URL вебхука Bitrix24.")
            return

        if not chat_ids:
            QMessageBox.warning(
                self, "Bitrix24",
                "Не выбрано ни одного получателя.\n\n"
                "Отметьте чаты в списке или введите ID в поле "
                "«Доп. ID чата».",
            )
            return

        # Какие цели отправляем
        targets: List[str] = []
        if which == "protocol":
            if not self._protocol_text.strip():
                QMessageBox.warning(self, "Bitrix24",
                                    "Протокол не прикреплён.")
                return
            targets = ["protocol"]
        elif which == "summary":
            if not self._summary_bitrix.strip():
                QMessageBox.warning(self, "Bitrix24", "Summary пустое.")
                return
            targets = ["summary"]
        elif which == "both":
            if not self._protocol_text.strip():
                QMessageBox.warning(self, "Bitrix24",
                                    "Протокол не прикреплён.")
                return
            if not self._summary_bitrix.strip():
                QMessageBox.warning(self, "Bitrix24", "Summary пустое.")
                return
            targets = ["protocol", "summary"]

        # Готовим «план» отправки (одинаковый для всех получателей)
        plan: List[Dict[str, Any]] = []
        for t in targets:
            if t == "protocol":
                header = (
                    self.protocol_header_input.text().strip()
                    if self.header_check.isChecked() else ""
                )
                body = self._protocol_text
                src_file = (
                    self._protocol_path
                    if self._protocol_path and os.path.exists(
                        self._protocol_path
                    ) else ""
                )
                plan.append({
                    "which": "protocol",
                    "header": header,
                    "body": body,
                    "src_file": src_file,
                    "is_file": self._is_file_mode("protocol", len(body)),
                })
            elif t == "summary":
                header = (
                    self.summary_header_input.text().strip()
                    if self.header_check.isChecked() else ""
                )
                body_plain = self._summary_plain
                body_bitrix = self._summary_bitrix
                plan.append({
                    "which": "summary",
                    "header": header,
                    "body": body_bitrix,
                    "body_plain": body_plain,
                    "src_file": "",
                    "is_file": self._is_file_mode("summary", len(body_plain)),
                })

        # --- Превью ---
        file_items_preview = [p for p in plan if p["is_file"]]
        text_items_preview = [p for p in plan if not p["is_file"]]

        preview_lines: List[str] = []
        if file_items_preview:
            names = ", ".join(p["which"] for p in file_items_preview)
            preview_lines.append(f"• файлом: {names}")
        for p in text_items_preview:
            preview_lines.append(
                f"• текстом: {p['which']}, {len(p['body'])} символов"
            )
        preview_text = "\n".join(preview_lines) or "(нечего отправлять)"

        recipients_text = self._describe_recipients(chat_ids)

        reply = QMessageBox.question(
            self, "Массовая отправка в Bitrix24",
            f"<b>Получателей:</b> {len(chat_ids)}<br><br>"
            f"<b>Что отправить:</b><br>"
            f"{html.escape(preview_text)}<br><br>"
            f"<b>Кому:</b><br>"
            f"<pre style='font-family:monospace'>"
            f"{html.escape(recipients_text)}</pre>"
            f"<br>Продолжить?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        connect_timeout = float(self.bitrix_cfg.get("connect_timeout", 15))
        read_timeout = float(self.bitrix_cfg.get("read_timeout", 60))
        system = self.system_check.isChecked()
        url_preview = not self.no_preview_check.isChecked()

        forced_folder_id = int(
            self.bitrix_cfg.get("upload_folder_id", 0) or 0
        )
        prefer_chat_folder = forced_folder_id <= 0

        file_items = [it for it in plan if it["is_file"]]
        text_items = [it for it in plan if not it["is_file"]]

        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

        async def _run_send_for_one(
            client: Bitrix24Client, chat_id: str
        ) -> tuple:
            """
            Выполняет отправку для одного получателя.
            Возвращает (sent_count, errors_list).
            """
            sent = 0
            errors: List[str] = []

            # --- Файлы: одно сообщение со всеми вложениями ---
            if file_items:
                prepared: List[str] = []
                cleanup_paths: List[str] = []
                for it in file_items:
                    p = self._prepare_file_for_item(it)
                    if p:
                        prepared.append(p)
                        if it.get("_tmp"):
                            cleanup_paths.append(p)

                if not prepared:
                    errors.append("файлы: нечего отправлять")
                else:
                    headers = [
                        it.get("header", "") for it in file_items
                    ]
                    headers = [h for h in headers if h]
                    comment = " / ".join(headers) if headers else ""

                    try:
                        log.info(
                            "Bitrix24: [%s] отправка %d файлов одним "
                            "сообщением",
                            chat_id, len(prepared),
                        )
                        await client.send_file_message(
                            dialog_id=chat_id,
                            file_paths=prepared,
                            comment=comment,
                            folder_id=forced_folder_id,
                            system=system,
                            url_preview=url_preview,
                            prefer_chat_folder=prefer_chat_folder,
                        )
                        sent += 1
                    except Bitrix24Error as exc:
                        log.error(
                            "Bitrix24: [%s] ошибка отправки файлов: %s",
                            chat_id, exc,
                        )
                        errors.append(f"файлы: {exc}")
                    finally:
                        for p in cleanup_paths:
                            try:
                                os.remove(p)
                            except Exception:
                                pass

            # --- Тексты: каждое сообщение отдельно ---
            for idx, it in enumerate(text_items, start=1):
                try:
                    text = self._build_text_message(it)
                    log.info(
                        "Bitrix24: [%s] текст #%d (%s, %d символов)",
                        chat_id, idx, it["which"], len(text),
                    )
                    await client.send_message(
                        dialog_id=chat_id,
                        text=text,
                        system=system,
                        url_preview=url_preview,
                    )
                    sent += 1
                except Bitrix24Error as exc:
                    log.error(
                        "Bitrix24: [%s] ошибка отправки текста #%d: %s",
                        chat_id, idx, exc,
                    )
                    errors.append(f"текст #{idx}: {exc}")
                except Exception as exc:
                    log.exception(
                        "Bitrix24: [%s] неожиданная ошибка текста #%d: %s",
                        chat_id, idx, exc,
                    )
                    errors.append(f"текст #{idx}: {exc}")

            return sent, errors

        async def _run_send_all() -> tuple:
            """
            Выполняет рассылку по всем выбранным получателям.
            Возвращает:
                (total_sent, errors_by_chat, ok_chats, fail_chats)
            """
            total_sent = 0
            errors_by_chat: Dict[str, List[str]] = {}
            ok_chats: List[str] = []
            fail_chats: List[str] = []

            async with Bitrix24Client(
                webhook_url=webhook,
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
            ) as client:
                for cid in chat_ids:
                    sent, errs = await _run_send_for_one(client, cid)
                    total_sent += sent
                    if errs:
                        errors_by_chat[cid] = errs
                        fail_chats.append(cid)
                    else:
                        ok_chats.append(cid)

            return total_sent, errors_by_chat, ok_chats, fail_chats

        try:
            (
                sent_count,
                errors_by_chat,
                ok_chats,
                fail_chats,
            ) = asyncio.run(_run_send_all())
        except Exception as exc:
            log.exception("Bitrix24: ошибка массовой отправки: %s", exc)
            QMessageBox.critical(self, "Bitrix24", f"Ошибка:\n{exc}")
            return
        finally:
            if QGuiApplication.overrideCursor() is not None:
                QGuiApplication.restoreOverrideCursor()

        # --- Результат ---
        self._show_send_report(
            sent_count=sent_count,
            errors_by_chat=errors_by_chat,
            ok_chats=ok_chats,
            fail_chats=fail_chats,
            total_chats=len(chat_ids),
        )

    def _show_send_report(
        self,
        sent_count: int,
        errors_by_chat: Dict[str, List[str]],
        ok_chats: List[str],
        fail_chats: List[str],
        total_chats: int,
    ) -> None:
        """Показывает пользователю отчёт о массовой рассылке."""
        log.info(
            "Bitrix24: массовая рассылка завершена. "
            "Получателей: %d, успешно: %d, с ошибками: %d, "
            "сообщений отправлено: %d",
            total_chats, len(ok_chats), len(fail_chats), sent_count,
        )

        if not fail_chats:
            QMessageBox.information(
                self, "Bitrix24",
                f"<b>Рассылка завершена успешно.</b><br><br>"
                f"Получателей: <b>{total_chats}</b><br>"
                f"Сообщений отправлено: <b>{sent_count}</b>",
            )
            return

        lines: List[str] = []
        lines.append(
            f"<b>Получателей:</b> {total_chats}<br>"
            f"<b>Успешно:</b> {len(ok_chats)}<br>"
            f"<b>С ошибкой:</b> {len(fail_chats)}<br>"
            f"<b>Всего отправлено сообщений:</b> {sent_count}<br><br>"
        )

        if ok_chats:
            lines.append("<b>Успешно отправлено в:</b><br>")
            for cid in ok_chats[:30]:
                lines.append(f"&nbsp;&nbsp;• {html.escape(cid)}<br>")
            if len(ok_chats) > 30:
                lines.append(
                    f"&nbsp;&nbsp;… и ещё {len(ok_chats) - 30}<br>"
                )
            lines.append("<br>")

        lines.append("<b>Ошибки:</b><br>")
        for cid, errs in list(errors_by_chat.items())[:15]:
            joined = "; ".join(html.escape(e) for e in errs)
            lines.append(f"&nbsp;&nbsp;• <b>{html.escape(cid)}</b>: {joined}<br>")
        if len(errors_by_chat) > 15:
            lines.append(
                f"&nbsp;&nbsp;… и ещё {len(errors_by_chat) - 15} "
                f"получателей с ошибками<br>"
            )

        box = (
            QMessageBox.warning if ok_chats
            else QMessageBox.critical
        )
        box(
            self, "Bitrix24 — результат рассылки",
            "".join(lines),
        )