"""Окно импорта готовых материалов: видео/аудио, стенограмма, протокол.

Изменения:
  • Добавлен блок «Теги» — можно сразу проставить метки
    на импортируемую запись. Теги попадают в session.json
    и участвуют в фильтре раздела «Библиотека».
"""
from __future__ import annotations

import os
import shutil
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import QDate, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
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
        date (datetime), name, project, comment, tags (list[str]),
        enqueue (bool), open_folder (bool)
    """

    def __init__(
        self,
        projects: List[str],
        sessions_root: str,
        parent: Optional[QWidget] = None,
        tags: Optional[List[Dict[str, str]]] = None,
        on_save_tag: Optional[Callable[[str, str], None]] = None,
        get_tags: Optional[
            Callable[[], List[Dict[str, str]]]
        ] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Импорт материалов")
        self.setModal(True)
        self.setMinimumWidth(820)

        self._projects = list(projects or [])
        self._sessions_root = sessions_root
        self.result_data: Dict[str, Any] = {}

        self._tags: List[Dict[str, str]] = list(tags or [])
        self._on_save_tag_cb = on_save_tag
        self._get_tags_cb = get_tags
        self._selected_tags: List[str] = []

        self._build_ui()
        self._refresh_ok_state()
        log.debug(
            "ImportWindow открыто, проектов=%d, тегов=%d, "
            "sessions_root=%s",
            len(self._projects), len(self._tags), sessions_root,
        )

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

        root.addLayout(form)

        # --- Разделитель ---
        sep = QLabel("<hr>")
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
        meta_form.addRow(
            "Комментарий:", with_info(self.comment_input, "imp_comment")
        )

        root.addLayout(meta_form)

        # --- Теги ---
        root.addWidget(self._build_tags_section())

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
        self.enqueue_check.toggled.connect(self._refresh_ok_state)
        opts_row.addWidget(self.enqueue_check)

        self.open_folder_check = QCheckBox(
            "Открыть папку записи после импорта"
        )
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
        buttons.button(
            QDialogButtonBox.StandardButton.Cancel
        ).setText("Отмена")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # ------------------------------------------------------------------
    # Блок тегов
    # ------------------------------------------------------------------
    def _build_tags_section(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(4)

        header = QHBoxLayout()
        header.addWidget(QLabel("<b>Теги</b>"))
        header.addStretch()
        icon = make_info_icon("imp_tags")
        if icon is not None:
            header.addWidget(icon)
        layout.addLayout(header)

        hint = QLabel(
            "<span style='color:#666'>Отметьте один или несколько "
            "тегов из справочника — они будут присвоены "
            "импортируемой записи. Новый тег можно добавить "
            "кнопкой ниже. Снять все теги — кнопкой "
            "«Снять все».</span>"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.tags_list = QListWidget()
        self.tags_list.setMinimumHeight(110)
        self.tags_list.setSelectionMode(
            QListWidget.SelectionMode.NoSelection
        )
        self.tags_list.setAlternatingRowColors(True)
        attach_tooltip(self.tags_list, "imp_tags_list")
        self.tags_list.itemChanged.connect(
            self._on_tag_item_changed
        )
        self._populate_tags_list()
        layout.addWidget(self.tags_list)

        btns = QHBoxLayout()

        self.add_tag_btn = QPushButton("Добавить новый тег…")
        self.add_tag_btn.setToolTip(
            "Добавить тег, которого нет в справочнике.\n"
            "После сохранения он появится в Настройки → Теги "
            "и сразу будет отмечен у записи."
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
        log.debug("Теги импорта: %s", self._selected_tags)

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
        log.info("Тег «%s» добавлен и выбран у импорта", name)

    def _on_clear_tags(self) -> None:
        self._selected_tags = []
        self._populate_tags_list()
        log.debug("Все теги импорта сняты")

    def _ordered_selected_tags(self) -> List[str]:
        """Возвращает выбранные теги в порядке справочника."""
        selected_set = set(self._selected_tags)
        ordered: List[str] = []
        for tag in self._tags:
            name = tag.get("name") or ""
            if name and name in selected_set:
                ordered.append(name)
        for name in self._selected_tags:
            if name and name not in ordered:
                ordered.append(name)
        return ordered

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

        at_least_one_file = has_video or has_transcript or has_protocol

        if not at_least_one_file:
            self.ok_btn.setEnabled(False)
            self.ok_btn.setToolTip(
                "Выберите хотя бы один файл для импорта"
            )
            return

        if not has_name:
            self.ok_btn.setEnabled(False)
            self.ok_btn.setToolTip("Введите название записи")
            return

        if self.enqueue_check.isChecked() and not has_video:
            self.ok_btn.setEnabled(False)
            self.ok_btn.setToolTip(
                "Для постановки в очередь нужно выбрать видео "
                "или аудио"
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

        for label, path in (
            ("видео", video),
            ("стенограмма", transcript),
            ("протокол", protocol),
        ):
            if path and not os.path.isfile(path):
                QMessageBox.warning(
                    self, "Импорт",
                    f"Файл {label} не найден:\n{path}",
                )
                return

        qd = self.date_edit.date()
        date_dt = datetime(qd.year(), qd.month(), qd.day())

        selected_tags = self._ordered_selected_tags()

        self.result_data = {
            "video_path": video,
            "transcript_path": transcript,
            "protocol_path": protocol,
            "date": date_dt,
            "name": self.name_input.text().strip(),
            "project": (
                self.project_combo.currentText().strip()
                or "Default"
            ),
            "comment": self.comment_input.toPlainText().strip(),
            "tags": selected_tags,
            "enqueue": self.enqueue_check.isChecked(),
            "open_folder": self.open_folder_check.isChecked(),
        }
        log.info(
            "Импорт подтверждён: date=%s, name=%r, project=%r, "
            "tags=%s, video=%s, transcript=%s, protocol=%s, "
            "enqueue=%s",
            date_dt.strftime("%Y-%m-%d"),
            self.result_data["name"], self.result_data["project"],
            selected_tags,
            bool(video), bool(transcript), bool(protocol),
            self.result_data["enqueue"],
        )
        self.accept()