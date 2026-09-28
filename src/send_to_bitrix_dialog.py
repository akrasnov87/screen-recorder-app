"""Диалог отправки протокола/summary в чат Bitrix24.

Поддерживает два режима:
  • текстом в чат (im.message.add с MESSAGE);
  • файлом с комментарием (загрузка на Диск + im.message.add с FILE_ID).

Папка для загрузки файла выбирается автоматически:
  • если задан upload_folder_id > 0 в настройках — используется он;
  • иначе — папка самого чата (у каждого чата в Bitrix24 она своя).

Если выбрано «Отправить всё» и оба материала уходят файлами, они
упаковываются в ОДНО сообщение с двумя вложениями.
"""
from __future__ import annotations

import asyncio
import html
import os
import tempfile
from datetime import datetime
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from .bbcode_editor import bbcode_to_bitrix, bbcode_to_plain
from .bitrix_client import Bitrix24Client, Bitrix24Error
from .logger import get_logger

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

    Параметры:
        session_info: dict с полями:
            - name: имя записи
            - project: проект
            - date: дата
            - protocol_path: путь к протоколу (может быть "")
            - protocol_label: имя файла протокола
            - summary_bb: текст summary (BB-код, может быть "")
            - comment: комментарий к записи
        chat_id: ID чата Bitrix24 (определяется по проекту).
        bitrix_cfg: настройки интеграции (из ConfigManager).
    """

    def __init__(
        self,
        session_info: Dict[str, Any],
        chat_id: str,
        bitrix_cfg: Dict[str, Any],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.session_info = session_info or {}
        self.chat_id = chat_id or ""
        self.bitrix_cfg = bitrix_cfg or {}

        self.setWindowTitle(
            f"Отправка в Bitrix24 — {self.session_info.get('name', '')}"
        )
        self.setModal(True)
        self.setMinimumSize(900, 900)

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

        self._build_ui()
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

        # --- Чат и вебхук ---
        chat_form = QFormLayout()
        self.chat_input = QLineEdit(self.chat_id)
        self.chat_input.setPlaceholderText("Например: chat2101 или 2101")
        self.chat_input.setToolTip(
            "ID чата Bitrix24, куда будет отправлено сообщение.\n\n"
            "Определяется автоматически по проекту записи "
            "(Настройки → Проекты и чаты Bitrix24).\n\n"
            "Можно переопределить вручную."
        )
        chat_form.addRow("Чат Bitrix24:", self.chat_input)

        self.webhook_input = QLineEdit(
            self.bitrix_cfg.get("webhook_url", "") or ""
        )
        self.webhook_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.webhook_input.setPlaceholderText(
            "https://portal.bitrix24.ru/rest/1/token/"
        )
        self.webhook_input.setToolTip(
            "URL входящего вебхука Bitrix24.\n\n"
            "Настраивается в Настройки → Bitrix24.\n"
            "Можно переопределить для конкретной отправки."
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
        chat_form.addRow("Вебхук Bitrix24:", webhook_row)

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
            "Пустое поле — заголовок не добавляется.</span>"
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

        # Заполняем заголовки значениями по умолчанию
        self._recalc_protocol_header_default()
        self._recalc_summary_header_default()

        # Включаем/выключаем поля при переключении чекбокса
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
        self.protocol_view.setMinimumHeight(140)
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
        self.summary_view.setMinimumHeight(120)
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
            "одним сообщением с двумя вложениями."
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
    # Заголовки по умолчанию
    # ------------------------------------------------------------------
    def _build_default_header(self, which: str) -> str:
        """Формирует заголовок по умолчанию для протокола или summary."""
        name = self.session_info.get("name", "")
        date = self.session_info.get("date", "")

        # Компактный формат даты: из "2026-09-28 00:00:00" → "2026-09-28".
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
        """Включает/выключает порог в зависимости от режима."""
        mode = self.send_mode_combo.currentData() or "auto"
        self.auto_file_threshold.setEnabled(mode == "auto")

    def _is_file_mode(self, which: str, body_len: int) -> bool:
        """Определяет, отправлять файлом или текстом."""
        mode = self.send_mode_combo.currentData() or "auto"
        if mode == "file":
            return True
        if mode == "text":
            return False
        # auto
        return body_len > self.auto_file_threshold.value()

    # ------------------------------------------------------------------
    # Загрузка превью
    # ------------------------------------------------------------------
    def _load_previews(self) -> None:
        # Протокол
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
            self.send_protocol_btn.setEnabled(bool(self._protocol_text.strip()))
        else:
            self.protocol_view.setPlainText(
                "Протокол не прикреплён к этой записи.\n\n"
                "Прикрепите его через меню «Протокол → Прикрепить протокол…» "
                "в окне «Записи»."
            )
            self.protocol_status.setText("— не прикреплён")
            self.send_protocol_btn.setEnabled(False)

        # Summary
        if self._summary_bitrix:
            self.summary_view.setPlainText(self._summary_plain[:8000])
            self.summary_status.setText(
                f"— {len(self._summary_plain)} символов "
                f"(BB → plain для превью, в чат уйдёт с форматированием)"
            )
            self.send_summary_btn.setEnabled(True)
        else:
            self.summary_view.setPlainText(
                "У этой записи нет краткого описания.\n\n"
                "Добавьте его через меню «Summary → Изменить summary…» "
                "в окне «Записи»."
            )
            self.summary_status.setText("— пусто")
            self.send_summary_btn.setEnabled(False)

        self.send_both_btn.setEnabled(
            bool(self._protocol_text.strip()) and bool(self._summary_bitrix)
        )

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
        """Собирает текстовое сообщение (для режима «текстом»)."""
        header = item.get("header", "")
        body = item.get("body", "")
        if header:
            return f"[B]{header}[/B]\n\n{body}".strip()
        return body.strip()

    def _prepare_file_for_item(self, item: Dict[str, Any]) -> str:
        """
        Возвращает путь к файлу для отправки вложением.

        • Для протокола: если src_file уже существует (.docx/.pdf/...),
          используем его как есть.
        • Для summary (или если src_file нет): создаём временный файл
          в формате .md или .txt.
        """
        which = item["which"]
        src = item.get("src_file") or ""

        # Если есть исходный файл — используем его
        if src and os.path.exists(src):
            log.info("Отправка исходного файла: %s", src)
            return src

        # Иначе — генерируем временный файл
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

        # Для протокола без исходного файла — создаём .txt
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
    # Отправка
    # ------------------------------------------------------------------
    def _on_send(self, which: str) -> None:
        webhook = self.webhook_input.text().strip()
        chat_id = self.chat_input.text().strip()

        if not webhook:
            QMessageBox.warning(self, "Bitrix24",
                                "Укажите URL вебхука Bitrix24.")
            return
        if not chat_id:
            QMessageBox.warning(
                self, "Bitrix24",
                "Не указан ID чата.\n\n"
                "Проверьте настройки проекта: "
                "Настройки → Проекты и чаты Bitrix24.",
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

        # Готовим «план» отправки
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

        # --- Превью: файлы уходят одним сообщением ---
        file_items_preview = [p for p in plan if p["is_file"]]
        text_items_preview = [p for p in plan if not p["is_file"]]

        preview_lines: List[str] = []
        if file_items_preview:
            names = ", ".join(p["which"] for p in file_items_preview)
            preview_lines.append(
                f"• файлом (одним сообщением): {names}"
            )
        for p in text_items_preview:
            preview_lines.append(
                f"• текстом: {p['which']}, {len(p['body'])} символов"
            )
        preview_text = "\n".join(preview_lines) or "(нечего отправлять)"

        reply = QMessageBox.question(
            self, "Отправка в Bitrix24",
            f"Чат: <b>{html.escape(chat_id)}</b><br><br>"
            f"{html.escape(preview_text)}<br><br>Продолжить?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        connect_timeout = float(self.bitrix_cfg.get("connect_timeout", 15))
        read_timeout = float(self.bitrix_cfg.get("read_timeout", 60))
        system = self.system_check.isChecked()
        url_preview = not self.no_preview_check.isChecked()

        # Настройка upload_folder_id:
        #   0   = папка чата (автоматически)
        #  >0   = жёстко переопределить папку
        forced_folder_id = int(
            self.bitrix_cfg.get("upload_folder_id", 0) or 0
        )
        prefer_chat_folder = forced_folder_id <= 0

        # --- Группируем: все файлы — в одно сообщение, тексты — отдельно ---
        file_items = [it for it in plan if it["is_file"]]
        text_items = [it for it in plan if not it["is_file"]]

        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

        async def _run_send() -> tuple:
            sent = 0
            errors: List[str] = []

            async with Bitrix24Client(
                webhook_url=webhook,
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
            ) as client:
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
                        # Комментарий — объединяем заголовки файлов
                        headers = [
                            it.get("header", "") for it in file_items
                        ]
                        headers = [h for h in headers if h]
                        comment = " / ".join(headers) if headers else ""

                        try:
                            log.info(
                                "Bitrix24: отправка %d файлов одним "
                                "сообщением в чат %s",
                                len(prepared), chat_id,
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
                                "Bitrix24: ошибка отправки файлов: %s",
                                exc,
                            )
                            errors.append(f"файлы: {exc}")
                        finally:
                            # Убираем временные файлы
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
                            "Bitrix24: текст #%d (%s, %d символов) "
                            "→ чат %s",
                            idx, it["which"], len(text), chat_id,
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
                            "Bitrix24: ошибка отправки текста #%d: %s",
                            idx, exc,
                        )
                        errors.append(f"текст #{idx}: {exc}")
                    except Exception as exc:
                        log.exception(
                            "Bitrix24: неожиданная ошибка текста #%d: %s",
                            idx, exc,
                        )
                        errors.append(f"текст #{idx}: {exc}")

            return sent, errors

        try:
            sent_count, errors = asyncio.run(_run_send())
        except Exception as exc:
            log.exception("Bitrix24: ошибка при отправке: %s", exc)
            QMessageBox.critical(self, "Bitrix24", f"Ошибка:\n{exc}")
            return
        finally:
            if QGuiApplication.overrideCursor() is not None:
                QGuiApplication.restoreOverrideCursor()

        # Результат
        if not errors:
            QMessageBox.information(
                self, "Bitrix24",
                f"Отправлено сообщений: {sent_count}\n\nЧат: {chat_id}",
            )
            log.info(
                "Bitrix24: успешно отправлено %d сообщений в %s",
                sent_count, chat_id,
            )
        elif sent_count == 0:
            QMessageBox.critical(
                self, "Bitrix24",
                "Не удалось отправить ничего:\n\n" + "\n".join(errors),
            )
        else:
            QMessageBox.warning(
                self, "Bitrix24",
                f"Отправлено: {sent_count}\n\n"
                "Ошибки:\n" + "\n".join(errors),
            )