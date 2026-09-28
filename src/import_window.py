"""Окно импорта готовых материалов: видео/аудио, стенограмма, протокол."""
from __future__ import annotations

import os
import shutil
from datetime import datetime
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from .logger import get_logger
from .tooltips import attach_tooltip, make_info_icon, with_info

log = get_logger(__name__)


# Разрешённые расширения по типам
_VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".wmv",
               ".mp3", ".wav", ".m4a", ".aac", ".opus", ".ogg")
_TRANSCRIPT_EXTS = (".txt", ".md", ".docx", ".json", ".srt", ".vtt")
_PROTOCOL_EXTS = (".docx", ".txt", ".md", ".pdf")


def _file_filter(exts: tuple) -> str:
    patterns = " ".join(f"*{e}" for e in exts)
    return f"Поддерживаемые файлы ({patterns});;Все файлы (*)"


class ImportWindow(QDialog):
    """
    Диалог импорта готовых материалов в программу.

    Результат: self.result_data (dict) заполняется при «Импортировать».
    В нём:
        video_path, transcript_path, protocol_path,
        date (datetime), name, project, comment,
        enqueue (bool)
    """

    def __init__(
        self,
        projects: List[str],
        sessions_root: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Импорт материалов")
        self.setModal(True)
        self.setMinimumWidth(760)

        self._projects = list(projects or [])
        self._sessions_root = sessions_root
        self.result_data: Dict[str, Any] = {}

        self._build_ui()
        self._refresh_ok_state()
        log.debug("ImportWindow открыто, проектов=%d, sessions_root=%s",
                  len(self._projects), sessions_root)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # Вводная плашка
        intro_row = QHBoxLayout()
        intro = QLabel(
            "Импорт ранее подготовленных материалов: видео или аудио, "
            "готовая стенограмма, протокол. Файлы будут скопированы "
            "в новую папку записи и появятся в разделах «Записи» "
            "и «Библиотека»."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("QLabel { color: #666; }")
        intro_row.addWidget(intro, 1)
        icon = make_info_icon("imp_intro")
        if icon is not None:
            intro_row.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(intro_row)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        # --- Видео / аудио ---
        self.video_input = QLineEdit()
        self.video_input.setPlaceholderText("Не выбран")
        self.video_input.setReadOnly(True)
        self.video_btn = QPushButton("Выбрать…")
        self.video_btn.clicked.connect(self._pick_video)
        self.video_clear_btn = QPushButton("✕")
        self.video_clear_btn.setFixedWidth(28)
        self.video_clear_btn.setToolTip("Очистить поле")
        self.video_clear_btn.clicked.connect(
            lambda: self._clear_field(self.video_input)
        )

        video_row = QWidget()
        vh = QHBoxLayout(video_row)
        vh.setContentsMargins(0, 0, 0, 0)
        vh.setSpacing(4)
        vh.addWidget(self.video_input, 1)
        vh.addWidget(self.video_btn, 0)
        vh.addWidget(self.video_clear_btn, 0)

        form.addRow("Видео / аудио:", with_info(video_row, "imp_video"))

        # --- Стенограмма ---
        self.transcript_input = QLineEdit()
        self.transcript_input.setPlaceholderText("Не выбрана")
        self.transcript_input.setReadOnly(True)
        self.transcript_btn = QPushButton("Выбрать…")
        self.transcript_btn.clicked.connect(self._pick_transcript)
        self.transcript_clear_btn = QPushButton("✕")
        self.transcript_clear_btn.setFixedWidth(28)
        self.transcript_clear_btn.setToolTip("Очистить поле")
        self.transcript_clear_btn.clicked.connect(
            lambda: self._clear_field(self.transcript_input)
        )

        tr_row = QWidget()
        trh = QHBoxLayout(tr_row)
        trh.setContentsMargins(0, 0, 0, 0)
        trh.setSpacing(4)
        trh.addWidget(self.transcript_input, 1)
        trh.addWidget(self.transcript_btn, 0)
        trh.addWidget(self.transcript_clear_btn, 0)

        form.addRow("Стенограмма:", with_info(tr_row, "imp_transcript"))

        # --- Протокол ---
        self.protocol_input = QLineEdit()
        self.protocol_input.setPlaceholderText("Не выбран")
        self.protocol_input.setReadOnly(True)
        self.protocol_btn = QPushButton("Выбрать…")
        self.protocol_btn.clicked.connect(self._pick_protocol)
        self.protocol_clear_btn = QPushButton("✕")
        self.protocol_clear_btn.setFixedWidth(28)
        self.protocol_clear_btn.setToolTip("Очистить поле")
        self.protocol_clear_btn.clicked.connect(
            lambda: self._clear_field(self.protocol_input)
        )

        pr_row = QWidget()
        prh = QHBoxLayout(pr_row)
        prh.setContentsMargins(0, 0, 0, 0)
        prh.setSpacing(4)
        prh.addWidget(self.protocol_input, 1)
        prh.addWidget(self.protocol_btn, 0)
        prh.addWidget(self.protocol_clear_btn, 0)

        form.addRow("Протокол:", with_info(pr_row, "imp_protocol"))

        # --- Разделитель ---
        sep = QLabel("<hr>")
        root.addLayout(form)
        root.addWidget(sep)

        # --- Метаданные ---
        meta_form = QFormLayout()
        meta_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        # Дата
        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.date_edit.setDate(QDate.currentDate())
        meta_form.addRow("Дата записи:", with_info(self.date_edit, "imp_date"))

        # Название
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText(
            "Например: Совещание по проекту X"
        )
        self.name_input.textChanged.connect(self._refresh_ok_state)
        meta_form.addRow("Название:", with_info(self.name_input, "imp_name"))

        # Проект
        self.project_combo = QComboBox()
        self.project_combo.setEditable(True)
        self.project_combo.addItems(self._projects)
        if self._projects:
            self.project_combo.setCurrentIndex(0)
        meta_form.addRow("Проект:", with_info(self.project_combo, "imp_project"))

        # Комментарий
        self.comment_input = QPlainTextEdit()
        self.comment_input.setPlaceholderText(
            "Необязательный комментарий к импортируемой записи…"
        )
        self.comment_input.setFixedHeight(70)
        meta_form.addRow("Комментарий:", with_info(self.comment_input, "imp_comment"))

        root.addLayout(meta_form)

        # --- Опции ---
        opts_row = QHBoxLayout()

        self.enqueue_check = QCheckBox(
            "Поставить в очередь обработки"
        )
        self.enqueue_check.setToolTip(
            "Если включено — запись будет обработана как обычно:\n"
            "конвертация, транскрибация (если есть видео/аудио), "
            "суммаризация, формирование DeepSeek-промпта.\n\n"
            "Если выключено — файлы просто сохранятся, "
            "обработка не запустится."
        )
        self.enqueue_check.setChecked(False)
        opts_row.addWidget(self.enqueue_check)

        self.open_folder_check = QCheckBox("Открыть папку записи после импорта")
        self.open_folder_check.setChecked(True)
        self.open_folder_check.setToolTip(
            "После импорта открыть папку сессии в файловом менеджере."
        )
        opts_row.addWidget(self.open_folder_check)

        opts_row.addStretch()
        root.addLayout(opts_row)

        # --- Кнопки ---
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        self.ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_btn.setText("Импортировать")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # ------------------------------------------------------------------
    # Выбор файлов
    # ------------------------------------------------------------------
    def _clear_field(self, field: QLineEdit) -> None:
        field.setText("")
        field.setToolTip("")
        self._refresh_ok_state()

    def _pick_video(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Выберите видео или аудио",
            os.path.expanduser("~"),
            _file_filter(_VIDEO_EXTS),
        )
        if path:
            self.video_input.setText(path)
            self.video_input.setToolTip(path)
            log.info("Импорт: выбрано видео %s", path)
            self._refresh_ok_state()

    def _pick_transcript(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Выберите стенограмму",
            os.path.expanduser("~"),
            _file_filter(_TRANSCRIPT_EXTS),
        )
        if path:
            self.transcript_input.setText(path)
            self.transcript_input.setToolTip(path)
            log.info("Импорт: выбрана стенограмма %s", path)
            self._refresh_ok_state()

    def _pick_protocol(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Выберите протокол",
            os.path.expanduser("~"),
            _file_filter(_PROTOCOL_EXTS),
        )
        if path:
            self.protocol_input.setText(path)
            self.protocol_input.setToolTip(path)
            log.info("Импорт: выбран протокол %s", path)
            self._refresh_ok_state()

    # ------------------------------------------------------------------
    # Валидация
    # ------------------------------------------------------------------
    def _refresh_ok_state(self) -> None:
        has_video = bool(self.video_input.text().strip())
        has_transcript = bool(self.transcript_input.text().strip())
        has_protocol = bool(self.protocol_input.text().strip())
        has_name = bool(self.name_input.text().strip())

        # Хотя бы один файл — обязателен
        at_least_one_file = has_video or has_transcript or has_protocol

        if not at_least_one_file:
            self.ok_btn.setEnabled(False)
            self.ok_btn.setToolTip("Выберите хотя бы один файл для импорта")
            return

        if not has_name:
            self.ok_btn.setEnabled(False)
            self.ok_btn.setToolTip("Введите название записи")
            return

        # Если включена очередь — нужен видео/аудио файл, иначе обрабатывать нечего
        if self.enqueue_check.isChecked() and not has_video:
            self.ok_btn.setEnabled(False)
            self.ok_btn.setToolTip(
                "Для постановки в очередь нужно выбрать видео или аудио"
            )
            return

        self.ok_btn.setEnabled(True)
        self.ok_btn.setToolTip("")

    # ------------------------------------------------------------------
    # Импорт
    # ------------------------------------------------------------------
    def _on_accept(self) -> None:
        video = self.video_input.text().strip()
        transcript = self.transcript_input.text().strip()
        protocol = self.protocol_input.text().strip()

        # Проверяем существование
        for label, path in (("видео", video), ("стенограмма", transcript),
                            ("протокол", protocol)):
            if path and not os.path.isfile(path):
                QMessageBox.warning(
                    self, "Импорт",
                    f"Файл {label} не найден:\n{path}",
                )
                return

        qd = self.date_edit.date()
        date_dt = datetime(qd.year(), qd.month(), qd.day())

        self.result_data = {
            "video_path": video,
            "transcript_path": transcript,
            "protocol_path": protocol,
            "date": date_dt,
            "name": self.name_input.text().strip(),
            "project": self.project_combo.currentText().strip() or "Default",
            "comment": self.comment_input.toPlainText().strip(),
            "enqueue": self.enqueue_check.isChecked(),
            "open_folder": self.open_folder_check.isChecked(),
        }
        log.info(
            "Импорт подтверждён: date=%s, name=%r, project=%r, "
            "video=%s, transcript=%s, protocol=%s, enqueue=%s",
            date_dt.strftime("%Y-%m-%d"),
            self.result_data["name"], self.result_data["project"],
            bool(video), bool(transcript), bool(protocol),
            self.result_data["enqueue"],
        )
        self.accept()