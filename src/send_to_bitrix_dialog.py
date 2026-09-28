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

Протокол всегда отправляется в формате .docx:
  • если manual_protocol_path уже .docx — берём как есть;
  • если .md / .txt / .pdf — конвертируем во временный .docx
    через markdown_to_docx() и отправляем его.

Summary хранится в session.json в поле summary_bb (Markdown).
При отправке «текстом» конвертируется в BB-код Bitrix24
через markdown_to_bitrix.

Особенности UI:
  • Список получателей и кнопки управления им — в отдельном
    контейнере recipients_block с вертикальным QVBoxLayout.
  • Строки без chat_id нельзя отметить, они серые и с подсказкой
    «Укажите ID в Настройках…».
  • Превью материалов не показывается текстом — вместо этого
    отображаются ссылки на файлы, которые можно открыть двойным
    кликом или кнопкой «Открыть файл».
"""
from __future__ import annotations

import asyncio
import html
import os
import re
import tempfile
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QSizePolicy, QSpinBox, QVBoxLayout,
    QWidget,
)

from .bitrix_client import Bitrix24Client, Bitrix24Error
from .logger import get_logger
from .markdown_docx import markdown_to_docx
from .markdown_to_bitrix import (
    markdown_to_bitrix,
    markdown_to_plain,
    markdown_to_plain_with_bb,
)
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


def _find_summary_file(session_dir: str) -> str:
    """
    Ищет файл summary в папке сессии.

    Приоритет:
      1) summary.md                       — сохраняется при редактировании;
      2) summary_<…>.md                   — если пользователь экспортировал;
      3) video_summary.md                 — создаётся автоматически при
                                             обработке;
      4) summary_<…>.docx                 — если пользователь экспортировал;
      5) summary_<…>.txt                  — старые варианты.
    """
    if not session_dir or not os.path.isdir(session_dir):
        return ""

    try:
        entries = sorted(os.listdir(session_dir))
    except OSError:
        return ""

    candidates: List[tuple] = []  # (priority, path)
    for name in entries:
        low = name.lower()
        full = os.path.join(session_dir, name)
        if low == "summary.md":
            candidates.append((0, full))
        elif low.startswith("summary_") and low.endswith(".md"):
            candidates.append((1, full))
        elif low == "video_summary.md":
            candidates.append((2, full))
        elif low.startswith("summary_") and low.endswith(".docx"):
            candidates.append((3, full))
        elif low.startswith("summary_") and low.endswith(".txt"):
            candidates.append((4, full))

    if not candidates:
        return ""
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


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
            - summary_bb: текст summary (Markdown, может быть "")
            - comment: комментарий к записи
            - session_dir: путь к папке сессии (для «Показать в папке»)
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
        self.setMinimumSize(760, 640)
        self.resize(960, 900)

        self._protocol_path = self.session_info.get("protocol_path") or ""
        self._protocol_text = ""

        # В этом поле лежит Markdown. Имя «summary_bb» оставлено
        # для совместимости со старыми записями.
        self._summary_md = self.session_info.get("summary_bb") or ""

        self._session_dir = self.session_info.get("session_dir") or ""

        self._summary_plain = (
            markdown_to_plain_with_bb(self._summary_md).strip()
            if self._summary_md else ""
        )
        self._summary_bitrix = (
            markdown_to_bitrix(self._summary_md).strip()
            if self._summary_md else ""
        )

        self._summary_file_path = _find_summary_file(self._session_dir)

        self._selected_chat_ids: Set[str] = set()

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

        # Контейнер «список + кнопки».
        recipients_block = QWidget()
        recipients_block_layout = QVBoxLayout(recipients_block)
        recipients_block_layout.setContentsMargins(0, 0, 0, 0)
        recipients_block_layout.setSpacing(4)

        self.recipients_list = QListWidget()
        self.recipients_list.setMinimumHeight(140)
        self.recipients_list.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Expanding,
        )
        self.recipients_list.setToolTip(
            "Список проектов и сотрудников.\n\n"
            "Клик по строке переключает галочку. "
            "Можно выбрать сразу несколько получателей."
        )
        self.recipients_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.recipients_list.setTextElideMode(
            Qt.TextElideMode.ElideRight
        )
        self.recipients_list.setAlternatingRowColors(True)
        self.recipients_list.itemChanged.connect(
            self._on_recipient_item_changed
        )
        recipients_block_layout.addWidget(self.recipients_list, 1)

        self.empty_hint = QLabel(
            "<span style='color:#c62828'>"
            "Ни один получатель не отмечен. Отметьте чаты в списке "
            "или введите ID в поле «Доп. ID чата» ниже."
            "</span>"
        )
        self.empty_hint.setWordWrap(True)
        self.empty_hint.setVisible(True)
        recipients_block_layout.addWidget(self.empty_hint)

        list_btns = QHBoxLayout()
        list_btns.setContentsMargins(0, 2, 0, 0)
        list_btns.setSpacing(6)

        self.select_all_btn = QPushButton("Выбрать все")
        self.select_all_btn.setMinimumWidth(120)
        self.select_all_btn.setToolTip(
            "Отметить все чаты в списке (проекты и сотрудники)"
        )
        self.select_all_btn.clicked.connect(self._on_select_all)
        list_btns.addWidget(self.select_all_btn)

        self.unselect_all_btn = QPushButton("Снять все")
        self.unselect_all_btn.setMinimumWidth(120)
        self.unselect_all_btn.setToolTip(
            "Снять все галочки в списке получателей"
        )
        self.unselect_all_btn.clicked.connect(self._on_unselect_all)
        list_btns.addWidget(self.unselect_all_btn)

        self.show_selected_btn = QPushButton("Показать выбранные")
        self.show_selected_btn.setMinimumWidth(160)
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
        self.selected_count_label.setMinimumWidth(140)
        self.selected_count_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        list_btns.addWidget(self.selected_count_label)

        recipients_block_layout.addLayout(list_btns)

        root.addWidget(recipients_block, 1)

        # --- Формы: ручной ID и вебхук ---
        manual_form = QFormLayout()
        manual_form.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        manual_form.setFormAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )

        self.manual_chat_input = QLineEdit()
        self.manual_chat_input.setPlaceholderText(
            "Например: chat2101 или 123 — добавится к выбранным"
        )
        manual_form.addRow(
            "Доп. ID чата:",
            with_info(self.manual_chat_input, "bitrix_chat_id"),
        )
        root.addLayout(manual_form)

        root.addSpacing(4)

        chat_form = QFormLayout()
        chat_form.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        chat_form.setFormAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )

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
        opts_row.setContentsMargins(0, 0, 0, 0)
        opts_row.addWidget(self.system_check)
        opts_row.addWidget(self.no_preview_check)
        opts_row.addStretch()
        chat_form.addRow("", self._wrap_h(opts_row))

        root.addLayout(chat_form)

        # --- Заголовки сообщений ---
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
        headers_form.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )

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
        mode_box.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )

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
            "комментарием. Протокол всегда отправляется как .docx "
            "(если исходник .md/.txt/.pdf — конвертируется).\n"
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

        # --- Материалы: ссылки на файлы вместо превью текста ---
        materials_header = QHBoxLayout()
        materials_header.addWidget(QLabel("<b>Материалы к отправке</b>"))
        materials_header.addStretch()
        root.addLayout(materials_header)

        materials_hint = QLabel(
            "<span style='color:#666'>Содержимое не отображается здесь — "
            "чтобы проверить, откройте файл двойным кликом по ссылке "
            "или кнопкой «Открыть файл». В чат уйдёт то, что выбрано "
            "в режиме отправки ниже.<br>"
            "Протокол отправляется в формате .docx "
            "(при необходимости конвертируется автоматически).</span>"
        )
        materials_hint.setWordWrap(True)
        root.addWidget(materials_hint)

        # Протокол
        protocol_row = QHBoxLayout()
        protocol_row.addWidget(QLabel("Протокол:"))
        self.protocol_link = QLabel()
        self.protocol_link.setOpenExternalLinks(False)
        self.protocol_link.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction
        )
        self.protocol_link.linkActivated.connect(self._on_open_protocol_link)
        protocol_row.addWidget(self.protocol_link, 1)

        self.open_protocol_btn = QPushButton("Открыть файл")
        self.open_protocol_btn.clicked.connect(self._on_open_protocol)
        protocol_row.addWidget(self.open_protocol_btn)

        self.show_protocol_folder_btn = QPushButton("Показать в папке")
        self.show_protocol_folder_btn.clicked.connect(
            lambda: self._show_in_folder(self._protocol_path)
        )
        protocol_row.addWidget(self.show_protocol_folder_btn)
        root.addLayout(protocol_row)

        # Summary
        summary_row = QHBoxLayout()
        summary_row.addWidget(QLabel("Summary:"))
        self.summary_link = QLabel()
        self.summary_link.setOpenExternalLinks(False)
        self.summary_link.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction
        )
        self.summary_link.linkActivated.connect(self._on_open_summary_link)
        summary_row.addWidget(self.summary_link, 1)

        self.open_summary_btn = QPushButton("Открыть файл")
        self.open_summary_btn.clicked.connect(self._on_open_summary)
        summary_row.addWidget(self.open_summary_btn)

        self.show_summary_folder_btn = QPushButton("Показать в папке")
        self.show_summary_folder_btn.clicked.connect(
            lambda: self._show_in_folder(self._summary_target_path())
        )
        summary_row.addWidget(self.show_summary_folder_btn)
        root.addLayout(summary_row)

        # --- Кнопки отправки ---
        actions_row = QHBoxLayout()

        self.send_protocol_btn = QPushButton("Отправить протокол")
        self.send_protocol_btn.clicked.connect(
            lambda: self._on_send(which="protocol")
        )
        actions_row.addWidget(self.send_protocol_btn)

        self.send_summary_btn = QPushButton("Отправить summary")
        self.send_summary_btn.clicked.connect(
            lambda: self._on_send(which="summary")
        )
        actions_row.addWidget(self.send_summary_btn)

        self.send_both_btn = QPushButton("Отправить всё")
        self.send_both_btn.setToolTip(
            "Отправить двумя сообщениями: сначала протокол, "
            "потом summary.\n\n"
            "В режиме «Файлом с комментарием» оба файла уйдут "
            "одним сообщением с двумя вложениями "
            "(протокол — .docx, summary — .md).\n\n"
            "Массовая рассылка: содержимое уйдёт всем отмеченным "
            "получателям."
        )
        self.send_both_btn.clicked.connect(
            lambda: self._on_send(which="both")
        )
        actions_row.addWidget(self.send_both_btn)

        actions_row.addStretch()
        root.addLayout(actions_row)

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
    # Логика выбора получателей
    # ------------------------------------------------------------------
    def _init_recipient_state(self) -> None:
        self.recipients_list.blockSignals(True)
        self.recipients_list.clear()

        selected_ids: Set[str] = set()
        if self.chat_id:
            selected_ids.add(self.chat_id)

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
                    item.setFlags(Qt.ItemFlag.NoItemFlags)
                    item.setForeground(Qt.GlobalColor.darkGray)
                    item.setToolTip(
                        f"У проекта «{p['name']}» не задан ID чата.\n\n"
                        "Укажите его в Настройках → "
                        "Проекты и чаты Bitrix24 — после этого "
                        "проект можно будет выбрать здесь."
                    )
                self.recipients_list.addItem(item)

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
        header = QListWidgetItem(f"── {text} ──")
        header.setFlags(Qt.ItemFlag.NoItemFlags)
        header.setForeground(Qt.GlobalColor.darkGray)
        font = header.font()
        font.setBold(True)
        header.setFont(font)
        return header

    def _refresh_selected_chat_ids(self) -> None:
        ids: Set[str] = set()
        for i in range(self.recipients_list.count()):
            item = self.recipients_list.item(i)
            if item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                if item.checkState() == Qt.CheckState.Checked:
                    cid = (item.data(Qt.ItemDataRole.UserRole) or "").strip()
                    if cid:
                        ids.add(cid)
        self._selected_chat_ids = ids

        if self.selected_count_label is not None:
            n = len(ids)
            if n == 0:
                self.selected_count_label.setText("Ничего не выбрано")
            elif n == 1:
                self.selected_count_label.setText("Выбрано: 1")
            else:
                self.selected_count_label.setText(f"Выбрано: {n}")

        if self.empty_hint is not None:
            self.empty_hint.setVisible(not ids)

    def _on_recipient_item_changed(self, item: QListWidgetItem) -> None:
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

    # Регулярка ищет в конце строки дату (опционально с временем).
    # Формат даты: YYYY-MM-DD. Время — через дефис или двоеточие:
    # "2026-09-28", "2026-09-28 15-17", "2026-09-28 15:17",
    # "2026-09-28 15-17-30", "2026-09-28 15:17:37".
    _DATE_TAIL_RE = re.compile(
        r"\d{4}-\d{2}-\d{2}"
        r"(?:\s+\d{2}[-:]\d{2}(?:[-:]\d{2})?)?"
        r"\s*$"
    )

    @classmethod
    def _name_already_has_date(cls, name: str) -> bool:
        """True, если имя записи уже заканчивается на дату/время."""
        if not name:
            return False
        return bool(cls._DATE_TAIL_RE.search(name.strip()))

    @staticmethod
    def _normalize_date(date: str) -> str:
        """
        Приводит строку даты к формату 'YYYY-MM-DD HH:MM'.

        • Убирает секунды.
        • Меняет дефис между часом и минутой на двоеточие.
        • Если время 00:00 — оставляет только дату.
        """
        if not date:
            return ""
        date = date.strip()
        parts = date.split(" ", 1)
        if len(parts) == 1:
            return parts[0]

        day = parts[0]
        time_part = parts[1].replace("-", ":")
        # Обрезаем до HH:MM
        time_part = time_part[:5]
        if time_part in ("00:00", ""):
            return day
        return f"{day} {time_part}"

    def _build_default_header(self, which: str) -> str:
        """
        Формирует заголовок по умолчанию для протокола или summary.

        Если имя записи УЖЕ содержит дату (например, «Созвон — 2026-09-28
        15-17»), дата не добавляется повторно. Иначе — добавляется в
        нормализованном виде 'YYYY-MM-DD HH:MM'.
        """
        name = (self.session_info.get("name") or "").strip()
        date = (self.session_info.get("date") or "").strip()

        if which == "protocol":
            title = "Протокол"
        elif which == "summary":
            title = "Краткое описание"
        else:
            title = "Материалы"

        parts: List[str] = [title]
        if name:
            parts.append(name)

        # Добавляем дату только если её ещё нет в имени.
        if date and not self._name_already_has_date(name):
            normalized = self._normalize_date(date)
            if normalized:
                parts.append(normalized)

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
    # Ссылки на материалы
    # ------------------------------------------------------------------
    def _summary_target_path(self) -> str:
        if self._summary_file_path and os.path.exists(self._summary_file_path):
            return self._summary_file_path
        if self._session_dir:
            sj = os.path.join(self._session_dir, "session.json")
            if os.path.exists(sj):
                return sj
        return ""

    def _load_previews(self) -> None:
        # --- Протокол ---
        if self._protocol_path and os.path.exists(self._protocol_path):
            try:
                self._protocol_text = _read_text_safe(self._protocol_path)
            except Exception as exc:
                log.warning("Не удалось прочитать %s: %s",
                            self._protocol_path, exc)
                self._protocol_text = ""

            size = os.path.getsize(self._protocol_path)
            fname = os.path.basename(self._protocol_path)
            self.protocol_link.setText(
                f"<a href='open:protocol'>{html.escape(fname)}</a> "
                f"<span style='color:#666'>"
                f"({size / 1024:.1f} КБ, "
                f"{len(self._protocol_text)} символов — в чат уйдёт "
                f"в формате .docx)</span>"
            )
            self.open_protocol_btn.setEnabled(True)
            self.show_protocol_folder_btn.setEnabled(True)
        else:
            self._protocol_text = ""
            self.protocol_link.setText(
                "<span style='color:#c62828'>"
                "Протокол не прикреплён к этой записи.</span>"
            )
            self.open_protocol_btn.setEnabled(False)
            self.show_protocol_folder_btn.setEnabled(False)

        # --- Summary ---
        if self._summary_bitrix:
            target = self._summary_target_path()
            if target:
                fname = os.path.basename(target)
                self.summary_link.setText(
                    f"<a href='open:summary'>{html.escape(fname)}</a> "
                    f"<span style='color:#666'>"
                    f"({len(self._summary_plain)} символов, "
                    f"Markdown; в чат уйдёт с форматированием)"
                    f"</span>"
                )
                self.open_summary_btn.setEnabled(True)
                self.show_summary_folder_btn.setEnabled(True)
            else:
                self.summary_link.setText(
                    "<span style='color:#666'>"
                    "Краткое описание есть, но файла нет — "
                    "откройте запись в окне «Записи», чтобы "
                    "посмотреть и экспортировать.</span>"
                )
                self.open_summary_btn.setEnabled(False)
                self.show_summary_folder_btn.setEnabled(False)
        else:
            self.summary_link.setText(
                "<span style='color:#c62828'>"
                "У этой записи нет краткого описания.</span>"
            )
            self.open_summary_btn.setEnabled(False)
            self.show_summary_folder_btn.setEnabled(False)

    def _on_open_protocol_link(self, _url: str) -> None:
        self._on_open_protocol()

    def _on_open_summary_link(self, _url: str) -> None:
        self._on_open_summary()

    def _on_open_protocol(self) -> None:
        self._open_local_file(self._protocol_path, "Протокол")

    def _on_open_summary(self) -> None:
        self._open_local_file(self._summary_target_path(), "Summary")

    @staticmethod
    def _open_local_file(path: str, label: str) -> None:
        if not path or not os.path.exists(path):
            QMessageBox.information(
                None, label,
                f"Файл не найден:\n{path or '(путь не задан)'}",
            )
            return
        log.info("Открытие файла (%s): %s", label, path)
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    @staticmethod
    def _show_in_folder(path: str) -> None:
        if not path:
            return
        folder = path if os.path.isdir(path) else os.path.dirname(path)
        if not folder or not os.path.isdir(folder):
            QMessageBox.information(
                None, "Папка",
                f"Папка не найдена:\n{folder or '(путь не задан)'}",
            )
            return
        log.info("Открытие папки: %s", folder)
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

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

    def _prepare_protocol_docx(self, src_path: str) -> str:
        """
        Готовит .docx для отправки протокола.

        • Если src_path уже .docx — возвращает его как есть.
        • Если .md / .txt / .pdf / что-то ещё — читает содержимое,
          конвертирует через markdown_to_docx и возвращает путь
          к временному .docx.

        При любой ошибке конвертации логирует и возвращает исходный
        путь (лучше отправить .md, чем ничего).
        """
        ext = os.path.splitext(src_path)[1].lower()

        if ext == ".docx":
            log.info("Протокол уже в .docx — отправляем как есть: %s",
                     src_path)
            return src_path

        # Читаем исходник как текст.
        try:
            if ext in (".md", ".txt"):
                with open(src_path, "r", encoding="utf-8",
                          errors="replace") as f:
                    text = f.read()
            else:
                # .pdf и прочие — через _read_text_safe.
                text = _read_text_safe(src_path)
        except Exception as exc:
            log.exception("Не удалось прочитать протокол %s: %s",
                          src_path, exc)
            return src_path

        if not text.strip():
            log.warning("Протокол пустой, конвертация в .docx не нужна: %s",
                        src_path)
            return src_path

        # Куда сохранить временный .docx.
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        safe_name = _safe_filename(
            self.session_info.get("name") or "protocol"
        )
        tmp_path = os.path.join(
            tempfile.gettempdir(),
            f"protocol_{safe_name}_{stamp}.docx",
        )

        title = self.session_info.get("name") or ""

        try:
            markdown_to_docx(text, tmp_path, title=title)
        except Exception as exc:
            log.exception("Не удалось конвертировать протокол в .docx: %s",
                          exc)
            return src_path

        log.info(
            "Протокол сконвертирован в .docx: %s → %s (%d символов)",
            src_path, tmp_path, len(text),
        )
        return tmp_path

    def _prepare_file_for_item(self, item: Dict[str, Any]) -> str:
        which = item["which"]
        src = item.get("src_file") or ""

        # --- Протокол: всегда отправляем .docx ---
        if which == "protocol":
            if src and os.path.exists(src):
                docx_path = self._prepare_protocol_docx(src)
                # Если получили временный .docx (а не тот же src) —
                # помечаем его для удаления после отправки.
                if docx_path and docx_path != src:
                    item["_tmp"] = True
                    return docx_path
                return docx_path

            # src не задан или файла нет — формируем .docx из текста
            # протокола в памяти.
            body = item.get("body", "")
            header = item.get("header", "") or ""

            if not body.strip():
                log.warning("Протокол пустой — файл не сформирован")
                return ""

            stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            safe_name = _safe_filename(
                self.session_info.get("name") or "protocol"
            )
            tmp_path = os.path.join(
                tempfile.gettempdir(),
                f"protocol_{safe_name}_{stamp}.docx",
            )

            # Если header есть — добавляем его как H1 в начало.
            full_md = f"# {header}\n\n{body}" if header else body

            try:
                markdown_to_docx(
                    full_md, tmp_path,
                    title=self.session_info.get("name") or "",
                )
                item["_tmp"] = True
                log.info(
                    "Протокол сформирован из текста: %s (%d символов)",
                    tmp_path, len(full_md),
                )
                return tmp_path
            except Exception as exc:
                log.exception(
                    "Не удалось сформировать .docx протокола: %s", exc
                )
                return ""

        # --- Summary: Markdown-файл ---
        if which == "summary":
            md_text = item.get("body_md") or item.get("body_plain") or ""
            header = item.get("header", "") or ""

            if not md_text.strip():
                return ""

            tmpdir = tempfile.gettempdir()
            stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            safe_name = _safe_filename(
                self.session_info.get("name") or "document"
            )
            filename = f"summary_{safe_name}_{stamp}.md"
            full_text = (f"# {header}\n\n{md_text}" if header else md_text)
            path = os.path.join(tmpdir, filename)
            with open(path, "w", encoding="utf-8") as f:
                f.write(full_text)
            item["_tmp"] = True
            log.info(
                "Подготовлен временный файл summary: %s (%d символов)",
                path, len(full_text),
            )
            return path

        return ""

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

        targets: List[str] = []
        if which == "protocol":
            if not self._protocol_text.strip() and not (
                self._protocol_path and os.path.exists(self._protocol_path)
            ):
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
            if not self._protocol_text.strip() and not (
                self._protocol_path and os.path.exists(self._protocol_path)
            ):
                QMessageBox.warning(self, "Bitrix24",
                                    "Протокол не прикреплён.")
                return
            if not self._summary_bitrix.strip():
                QMessageBox.warning(self, "Bitrix24", "Summary пустое.")
                return
            targets = ["protocol", "summary"]

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
                body_md = self._summary_md
                plan.append({
                    "which": "summary",
                    "header": header,
                    "body": body_bitrix,
                    "body_plain": body_plain,
                    "body_md": body_md,
                    "src_file": "",
                    "is_file": self._is_file_mode("summary", len(body_plain)),
                })

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
            f"{html.escape(preview_text)}<br>"
            f"<span style='color:#666'>Протокол будет отправлен "
            f"в формате .docx.</span><br><br>"
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
            sent = 0
            errors: List[str] = []

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