"""Окно «Библиотека» — полнотекстовый поиск по записям.

Изменения:
  • Дефолтные значения полей поиска (fuzzy, context, max_hits)
    читаются из config["app"].
  • SearchFilters получает fuzzy_max_word_distance.
  • Добавлен фильтр по тегу: ComboBox tag_combo + чекбокс
    tag_enabled. Тег можно выбрать из справочника
    (Настройки → Теги) или из уже использованных в записях.
"""
from __future__ import annotations

import os
import shutil
from datetime import datetime
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, QDate, QThread, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDateEdit, QDialog,
    QFileDialog, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMenu, QMenuBar, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QSpinBox, QSplitter, QTableWidget,
    QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget,
)

from ..library_search import (
    SearchFilters, SearchHit, build_prompt_from_hits, list_projects,
    list_tags, save_prompt_docx, save_prompt_markdown, search,
)
from ..logger import get_logger
from .tooltips import (
    attach_tooltip, attach_tooltip_text, make_info_icon, with_info,
)

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Поток поиска (чтобы UI не подвисал)
# ---------------------------------------------------------------------------
class SearchThread(QThread):
    finished_ok = Signal(list)
    failed = Signal(str)
    progress = Signal(int, int)

    def __init__(
        self, sessions_root: str, filters: SearchFilters,
    ) -> None:
        super().__init__()
        self.sessions_root = sessions_root
        self.filters = filters

    def run(self) -> None:
        try:
            def cb(cur: int, total: int) -> None:
                self.progress.emit(cur, total)

            def is_cancelled() -> bool:
                return self.isInterruptionRequested()

            hits = search(
                self.sessions_root,
                self.filters,
                progress_cb=cb,
                is_cancelled=is_cancelled,
            )
            self.finished_ok.emit(hits)
        except Exception as exc:
            log.exception("Ошибка поиска: %s", exc)
            self.failed.emit(str(exc))


# ---------------------------------------------------------------------------
# Окно
# ---------------------------------------------------------------------------
SOURCE_LABELS = {
    "transcript": "Стенограмма",
    "protocol": "Протокол",
    "summary": "Summary",
    "attachment": "Вложение",
}


class LibraryWindow(QDialog):
    """Поиск по записям: стенограммы, протоколы, summary, вложения."""

    def __init__(
        self,
        sessions_root: str,
        config_manager=None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.sessions_root = sessions_root
        self.config_manager = config_manager
        self._hits: List[SearchHit] = []
        self._thread: Optional[SearchThread] = None

        self._app_cfg: Dict[str, Any] = {}
        if config_manager is not None:
            try:
                self._app_cfg = config_manager.get_app_settings()
            except Exception as exc:
                log.warning(
                    "Не удалось прочитать app-настройки: %s", exc
                )
                self._app_cfg = {}

        self.setWindowTitle("Библиотека — поиск по записям")
        self.setMinimumSize(1280, 900)
        self.setModal(False)

        self._build_ui()
        self._reload_projects()
        self._reload_tags()
        log.info(
            "LibraryWindow открыто, sessions_root=%s", sessions_root
        )

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 4, 8, 8)
        root.setSpacing(6)

        bar = QMenuBar(self)
        layout: QVBoxLayout = self.layout()
        layout.insertWidget(0, bar)

        m_file = bar.addMenu("Файл")
        act_refresh = QAction(
            "Обновить список проектов и тегов", self
        )
        act_refresh.setShortcut(QKeySequence("F5"))
        act_refresh.triggered.connect(self._on_refresh_meta)
        m_file.addAction(act_refresh)

        act_close = QAction("Закрыть окно", self)
        act_close.setShortcut(QKeySequence("Ctrl+W"))
        act_close.triggered.connect(self.close)
        m_file.addAction(act_close)

        m_help = bar.addMenu("Справка")
        act_help = QAction("Как работает поиск", self)
        act_help.triggered.connect(self._show_help)
        m_help.addAction(act_help)

        # --- Вводная плашка ---
        intro_row = QHBoxLayout()
        intro = QLabel(
            "Полнотекстовый поиск по сохранённым записям. Ищет "
            "по файлам в папке <code>sessions/</code> — без базы "
            "данных. Для стенограмм используется нечёткий поиск, "
            "устойчивый к опечаткам. Сузьте область фильтрами "
            "«Проект», «Тег» и «Период», чтобы ускорить работу."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("QLabel { color: #666; }")
        intro_row.addWidget(intro, 1)
        intro_icon = make_info_icon("lib_intro")
        if intro_icon is not None:
            intro_row.addWidget(
                intro_icon, 0, Qt.AlignmentFlag.AlignTop
            )
        root.addLayout(intro_row)

        # --- Форма фильтров ---
        filters_box = QVBoxLayout()

        row1 = QHBoxLayout()
        self.query_input = QLineEdit()
        self.query_input.setPlaceholderText(
            "Например: миграция на новый стек, ЕЖД, "
            "риск по срокам…"
        )
        self.query_input.returnPressed.connect(self._start_search)
        row1.addWidget(QLabel("Запрос:"))
        row1.addWidget(with_info(self.query_input, "lib_query"), 1)

        self.search_btn = QPushButton("Найти")
        self.search_btn.setDefault(True)
        self.search_btn.setToolTip(
            "Запустить поиск (Enter в поле запроса)"
        )
        self.search_btn.clicked.connect(self._start_search)
        row1.addWidget(self.search_btn)

        self.cancel_btn = QPushButton("Стоп")
        self.cancel_btn.setToolTip("Прервать текущий поиск")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel_search)
        row1.addWidget(self.cancel_btn)

        filters_box.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Искать в:"))

        self.cb_transcripts = QCheckBox("Стенограммах")
        self.cb_transcripts.setChecked(True)
        self.cb_transcripts.setToolTip(
            "Поиск по файлам video.txt.\n"
            "Используется нечёткий поиск (fuzzy), устойчивый "
            "к опечаткам."
        )
        self.cb_protocols = QCheckBox("Протоколах")
        self.cb_protocols.setChecked(True)
        self.cb_protocols.setToolTip(
            "Поиск по protocol.* / deepseek_prompt.* / "
            "manual_protocol.*"
        )
        self.cb_summaries = QCheckBox("Summary")
        self.cb_summaries.setChecked(True)
        self.cb_summaries.setToolTip(
            "Поиск по краткому описанию записи "
            "(session.json → summary_bb)."
        )
        self.cb_attachments = QCheckBox("Вложениях")
        self.cb_attachments.setToolTip(
            "Поиск по файлам в папке attachments/."
        )

        for cb in (self.cb_transcripts, self.cb_protocols,
                   self.cb_summaries, self.cb_attachments):
            row2.addWidget(cb)

        attach_tooltip(self.cb_transcripts, "lib_search_in")

        row2.addStretch()
        filters_box.addLayout(row2)

        row3 = QHBoxLayout()

        self.project_combo = QComboBox()
        self.project_combo.addItem("— все —", "")
        self.project_combo.setMinimumWidth(180)
        row3.addWidget(QLabel("Проект:"))
        row3.addWidget(
            with_info(self.project_combo, "lib_project")
        )

        row3.addSpacing(12)
        self.tag_enabled = QCheckBox("Тег:")
        self.tag_enabled.setChecked(False)
        attach_tooltip(self.tag_enabled, "lib_tag_enabled")
        self.tag_enabled.toggled.connect(
            self._on_tag_enabled_toggled
        )
        row3.addWidget(self.tag_enabled)

        self.tag_combo = QComboBox()
        self.tag_combo.setMinimumWidth(180)
        attach_tooltip(self.tag_combo, "lib_tag")
        row3.addWidget(self.tag_combo)

        row3.addSpacing(12)
        self.date_from = QDateEdit()
        self.date_from.setCalendarPopup(True)
        self.date_from.setDisplayFormat("yyyy-MM-dd")
        self.date_from.setDate(self._default_from_date())
        row3.addWidget(QLabel("С:"))
        row3.addWidget(
            with_info(self.date_from, "lib_date_from")
        )

        self.date_to = QDateEdit()
        self.date_to.setCalendarPopup(True)
        self.date_to.setDisplayFormat("yyyy-MM-dd")
        self.date_to.setDate(self._default_to_date())
        row3.addWidget(QLabel("По:"))
        row3.addWidget(with_info(self.date_to, "lib_date_to"))

        self.date_enabled = QCheckBox("Ограничить по датам")
        self.date_enabled.setChecked(True)
        attach_tooltip(self.date_enabled, "lib_date_enabled")
        row3.addWidget(self.date_enabled)

        row3.addSpacing(12)
        self.fuzzy_spin = QSpinBox()
        self.fuzzy_spin.setRange(50, 100)
        self.fuzzy_spin.setValue(
            int(self._app_cfg.get("search_default_fuzzy", 82))
        )
        self.fuzzy_spin.setSuffix(" %")
        row3.addWidget(QLabel("Fuzzy:"))
        row3.addWidget(with_info(self.fuzzy_spin, "lib_fuzzy"))

        row3.addStretch()
        filters_box.addLayout(row3)

        root.addLayout(filters_box)

        # --- Прогресс ---
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        # --- Сплиттер ---
        splitter = QSplitter(Qt.Orientation.Horizontal)

        left_container = QWidget()
        left_layout = QVBoxLayout(left_container)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(2)

        left_header = QHBoxLayout()
        left_header.addWidget(QLabel("<b>Результаты поиска</b>"))
        left_header.addStretch()
        results_icon = make_info_icon("lib_results")
        if results_icon is not None:
            left_header.addWidget(results_icon)
        left_layout.addLayout(left_header)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels([
            "Дата", "Название", "Проект", "Теги", "Где",
            "Совпадение", "Файл",
        ])
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.table.customContextMenuRequested.connect(
            self._show_context_menu
        )
        self.table.itemSelectionChanged.connect(
            self._on_selection_changed
        )
        self.table.itemDoubleClicked.connect(
            lambda _item: self._open_session_folder()
        )

        hv = self.table.horizontalHeader()
        hv.setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        hv.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hv.setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        hv.setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents
        )
        hv.setSectionResizeMode(
            4, QHeaderView.ResizeMode.ResizeToContents
        )
        hv.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        hv.setSectionResizeMode(
            6, QHeaderView.ResizeMode.ResizeToContents
        )
        left_layout.addWidget(self.table, 1)

        splitter.addWidget(left_container)

        right_container = QWidget()
        right_layout = QVBoxLayout(right_container)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(2)

        right_header = QHBoxLayout()
        right_header.addWidget(QLabel("<b>Превью</b>"))
        right_header.addStretch()
        preview_icon = make_info_icon("lib_preview")
        if preview_icon is not None:
            right_header.addWidget(preview_icon)
        right_layout.addLayout(right_header)

        self.preview = QTextBrowser()
        self.preview.setOpenExternalLinks(False)
        right_layout.addWidget(self.preview, 1)

        splitter.addWidget(right_container)

        splitter.setSizes([860, 520])
        root.addWidget(splitter, 1)

        # --- Панель формирования промпта ---
        root.addWidget(self._build_prompt_panel())

        # --- Нижняя строка ---
        bottom = QHBoxLayout()
        self.status_label = QLabel("")
        bottom.addWidget(self.status_label)
        bottom.addStretch()

        self.open_folder_btn = QPushButton("Открыть папку записи")
        attach_tooltip(self.open_folder_btn, "lib_open_folder")
        self.open_folder_btn.clicked.connect(
            self._open_session_folder
        )
        bottom.addWidget(self.open_folder_btn)

        self.open_file_btn = QPushButton("Открыть найденный файл")
        attach_tooltip(self.open_file_btn, "lib_open_file")
        self.open_file_btn.clicked.connect(self._open_found_file)
        bottom.addWidget(self.open_file_btn)

        self.save_file_btn = QPushButton("Сохранить файл как…")
        attach_tooltip(self.save_file_btn, "lib_save_file")
        self.save_file_btn.clicked.connect(self._save_found_file)
        bottom.addWidget(self.save_file_btn)

        self.close_btn = QPushButton("Закрыть")
        self.close_btn.setToolTip(
            "Закрыть окно библиотеки (Ctrl+W)"
        )
        self.close_btn.clicked.connect(self.close)
        bottom.addWidget(self.close_btn)

        root.addLayout(bottom)

    # ------------------------------------------------------------------
    # Панель формирования промпта
    # ------------------------------------------------------------------
    def _build_prompt_panel(self) -> QWidget:
        box = QGroupBox(
            "Формирование промпта из результатов поиска"
        )
        layout = QVBoxLayout(box)
        layout.setContentsMargins(10, 6, 10, 8)
        layout.setSpacing(6)

        params_row = QHBoxLayout()

        self.prompt_enabled_check = QCheckBox(
            "Формировать промпт по совпадениям"
        )
        attach_tooltip(
            self.prompt_enabled_check, "lib_prompt_enabled"
        )
        self.prompt_enabled_check.setChecked(True)
        params_row.addWidget(self.prompt_enabled_check)

        params_row.addSpacing(16)

        params_row.addWidget(QLabel("Контекст до:"))
        self.prompt_before_spin = QSpinBox()
        self.prompt_before_spin.setRange(0, 5000)
        self.prompt_before_spin.setSingleStep(50)
        default_ctx = int(
            self._app_cfg.get(
                "search_default_context_chars", 400
            )
        )
        self.prompt_before_spin.setValue(default_ctx)
        self.prompt_before_spin.setSuffix(" симв.")
        params_row.addWidget(
            with_info(
                self.prompt_before_spin, "lib_prompt_before",
                stretch=False,
            )
        )

        params_row.addSpacing(8)
        params_row.addWidget(QLabel("после:"))
        self.prompt_after_spin = QSpinBox()
        self.prompt_after_spin.setRange(0, 5000)
        self.prompt_after_spin.setSingleStep(50)
        self.prompt_after_spin.setValue(default_ctx)
        self.prompt_after_spin.setSuffix(" симв.")
        params_row.addWidget(
            with_info(
                self.prompt_after_spin, "lib_prompt_after",
                stretch=False,
            )
        )

        params_row.addSpacing(16)
        params_row.addWidget(QLabel("Максимум фрагментов:"))
        self.prompt_max_spin = QSpinBox()
        self.prompt_max_spin.setRange(1, 500)
        self.prompt_max_spin.setValue(
            int(self._app_cfg.get(
                "search_default_max_prompt_hits", 100
            ))
        )
        params_row.addWidget(
            with_info(
                self.prompt_max_spin, "lib_prompt_max",
                stretch=False,
            )
        )

        params_row.addSpacing(16)
        self.prompt_dedup_check = QCheckBox(
            "Схлопывать дубликаты"
        )
        attach_tooltip(
            self.prompt_dedup_check, "lib_prompt_dedup"
        )
        self.prompt_dedup_check.setChecked(True)
        params_row.addWidget(self.prompt_dedup_check)

        params_row.addStretch()
        layout.addLayout(params_row)

        instr_row = QHBoxLayout()
        instr_row.addWidget(QLabel("Инструкция для ИИ:"))
        self.prompt_instruction_input = QPlainTextEdit()
        self.prompt_instruction_input.setPlaceholderText(
            "Например: проанализируй найденные фрагменты и "
            "составь сводку по упоминаниям рисков с указанием "
            "дат и ответственных."
        )
        self.prompt_instruction_input.setFixedHeight(60)
        instr_row.addWidget(
            with_info(
                self.prompt_instruction_input,
                "lib_prompt_instruction",
            ),
            1,
        )
        layout.addLayout(instr_row)

        btn_row = QHBoxLayout()

        self.prompt_preview_btn = QPushButton(
            "Показать промпт…"
        )
        self.prompt_preview_btn.setToolTip(
            "Показать сформированный промпт в отдельном окне — "
            "удобно проверить содержимое перед сохранением."
        )
        self.prompt_preview_btn.clicked.connect(
            self._preview_prompt
        )
        btn_row.addWidget(self.prompt_preview_btn)

        self.prompt_save_docx_btn = QPushButton(
            "Скачать промпт (DOCX)…"
        )
        attach_tooltip(
            self.prompt_save_docx_btn, "lib_prompt_save_docx"
        )
        self.prompt_save_docx_btn.clicked.connect(
            self._save_prompt_docx
        )
        btn_row.addWidget(self.prompt_save_docx_btn)

        self.prompt_save_md_btn = QPushButton(
            "Скачать промпт (Markdown)…"
        )
        attach_tooltip(
            self.prompt_save_md_btn, "lib_prompt_save_md"
        )
        self.prompt_save_md_btn.clicked.connect(
            self._save_prompt_md
        )
        btn_row.addWidget(self.prompt_save_md_btn)

        btn_row.addStretch()

        self.prompt_info_label = QLabel(
            "<span style='color:#666'>Промпт формируется "
            "из текущих результатов поиска. Сначала выполните "
            "поиск.</span>"
        )
        self.prompt_info_label.setWordWrap(True)
        btn_row.addWidget(self.prompt_info_label, 1)

        layout.addLayout(btn_row)

        self.prompt_enabled_check.toggled.connect(
            self._on_prompt_enabled_toggled
        )
        self._on_prompt_enabled_toggled(
            self.prompt_enabled_check.isChecked()
        )

        return box

    def _on_prompt_enabled_toggled(self, enabled: bool) -> None:
        for w in (
            self.prompt_before_spin,
            self.prompt_after_spin,
            self.prompt_max_spin,
            self.prompt_dedup_check,
            self.prompt_instruction_input,
        ):
            w.setEnabled(enabled)
        self._update_prompt_buttons_state()

    def _update_prompt_buttons_state(self) -> None:
        enabled = (
            self.prompt_enabled_check.isChecked()
            and bool(self._hits)
        )
        for btn in (
            self.prompt_preview_btn,
            self.prompt_save_docx_btn,
            self.prompt_save_md_btn,
        ):
            btn.setEnabled(enabled)

        if not self._hits:
            self.prompt_info_label.setText(
                "<span style='color:#666'>Промпт формируется "
                "из текущих результатов поиска. Сначала выполните "
                "поиск.</span>"
            )
        else:
            self.prompt_info_label.setText(
                f"<span style='color:#666'>Доступно совпадений "
                f"для промпта: <b>{len(self._hits)}</b>.</span>"
            )

    # ------------------------------------------------------------------
    # Формирование промпта
    # ------------------------------------------------------------------
    def _build_prompt_text(self) -> str:
        if not self._hits:
            return ""

        query = self.query_input.text().strip()
        instruction = (
            self.prompt_instruction_input.toPlainText().strip()
        )
        before = self.prompt_before_spin.value()
        after = self.prompt_after_spin.value()
        max_hits = self.prompt_max_spin.value()
        dedup = self.prompt_dedup_check.isChecked()

        try:
            text = build_prompt_from_hits(
                hits=self._hits,
                query=query,
                user_instruction=instruction,
                before_chars=before,
                after_chars=after,
                max_hits=max_hits,
                deduplicate=dedup,
                include_meta=True,
            )
        except Exception as exc:
            log.exception("Ошибка сборки промпта: %s", exc)
            QMessageBox.critical(
                self, "Библиотека",
                f"Не удалось сформировать промпт:\n{exc}",
            )
            return ""

        if not text:
            QMessageBox.warning(
                self, "Библиотека",
                "Не удалось извлечь контекст ни из одного "
                "совпадения.\n\n"
                "Возможные причины:\n"
                "  • файлы совпадений были перемещены или "
                "удалены;\n"
                "  • в результатах только нечитаемые форматы.",
            )
            return ""

        return text

    def _preview_prompt(self) -> None:
        text = self._build_prompt_text()
        if not text:
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("Промпт по результатам поиска")
        dlg.setModal(True)
        dlg.setMinimumSize(900, 700)

        layout = QVBoxLayout(dlg)

        info = QLabel(
            "Предпросмотр сформированного промпта. Текст можно "
            "выделить и скопировать. Для сохранения файла "
            "закройте окно и используйте кнопки «Скачать промпт»."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        browser = QTextBrowser()
        browser.setOpenExternalLinks(False)
        browser.setMarkdown(text)
        layout.addWidget(browser, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()

        copy_btn = QPushButton("Скопировать в буфер")

        def _copy():
            from PySide6.QtWidgets import QApplication
            QApplication.clipboard().setText(text)
            self.status_label.setText(
                "Промпт скопирован в буфер обмена"
            )

        copy_btn.clicked.connect(_copy)
        btn_row.addWidget(copy_btn)

        close_btn = QPushButton("Закрыть")
        close_btn.clicked.connect(dlg.accept)
        btn_row.addWidget(close_btn)

        layout.addLayout(btn_row)
        dlg.exec()

    def _default_prompt_filename(self, ext: str) -> str:
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        return os.path.join(
            os.path.expanduser("~"),
            f"prompt_search_{stamp}.{ext}",
        )

    def _save_prompt_docx(self) -> None:
        text = self._build_prompt_text()
        if not text:
            return

        target, _ = QFileDialog.getSaveFileName(
            self,
            "Сохранить промпт как DOCX",
            self._default_prompt_filename("docx"),
            "Документы Word (*.docx);;Все файлы (*)",
        )
        if not target:
            return
        if not target.lower().endswith(".docx"):
            target += ".docx"

        try:
            save_prompt_docx(
                text, target,
                title="Промпт по результатам поиска",
            )
        except Exception as exc:
            QMessageBox.critical(
                self, "Сохранение промпта",
                f"Не удалось сохранить DOCX:\n{exc}",
            )
            return

        log.info("Промпт сохранён (DOCX): %s", target)
        self.status_label.setText(f"Промпт сохранён: {target}")

        reply = QMessageBox.question(
            self, "Промпт сохранён",
            f"Документ сохранён:\n{target}\n\n"
            f"Открыть его сейчас?",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply == QMessageBox.StandardButton.Yes:
            QDesktopServices.openUrl(QUrl.fromLocalFile(target))

    def _save_prompt_md(self) -> None:
        text = self._build_prompt_text()
        if not text:
            return

        target, _ = QFileDialog.getSaveFileName(
            self,
            "Сохранить промпт как Markdown",
            self._default_prompt_filename("md"),
            "Markdown (*.md);;Все файлы (*)",
        )
        if not target:
            return
        if not target.lower().endswith(".md"):
            target += ".md"

        try:
            save_prompt_markdown(text, target)
        except Exception as exc:
            QMessageBox.critical(
                self, "Сохранение промпта",
                f"Не удалось сохранить Markdown:\n{exc}",
            )
            return

        log.info("Промпт сохранён (Markdown): %s", target)
        self.status_label.setText(f"Промпт сохранён: {target}")

        reply = QMessageBox.question(
            self, "Промпт сохранён",
            f"Файл сохранён:\n{target}\n\n"
            f"Открыть его сейчас?",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply == QMessageBox.StandardButton.Yes:
            QDesktopServices.openUrl(QUrl.fromLocalFile(target))

    # ------------------------------------------------------------------
    # Даты по умолчанию
    # ------------------------------------------------------------------
    @staticmethod
    def _default_from_date():
        return QDate.currentDate().addMonths(-1)

    @staticmethod
    def _default_to_date():
        return QDate.currentDate()

    # ------------------------------------------------------------------
    # Проекты и теги
    # ------------------------------------------------------------------
    def _on_refresh_meta(self) -> None:
        self._reload_projects()
        self._reload_tags()

    def _reload_projects(self) -> None:
        try:
            projects = list_projects(self.sessions_root)
        except Exception as exc:
            log.warning(
                "Не удалось получить список проектов: %s", exc
            )
            projects = []
        current = self.project_combo.currentData() or ""
        self.project_combo.blockSignals(True)
        self.project_combo.clear()
        self.project_combo.addItem("— все —", "")
        for p in projects:
            self.project_combo.addItem(p, p)
        idx = self.project_combo.findData(current)
        if idx >= 0:
            self.project_combo.setCurrentIndex(idx)
        self.project_combo.blockSignals(False)
        log.debug("Проекты обновлены: %d", len(projects))

    def _reload_tags(self) -> None:
        """
        Заполняет выпадающий список тегов.

        Объединяет справочник из настроек и теги, реально
        встречающиеся в сохранённых записях.
        """
        tags_from_config: List[str] = []
        if self.config_manager is not None:
            try:
                tags_from_config = self.config_manager.get_tag_names()
            except Exception as exc:
                log.warning(
                    "Не удалось прочитать справочник тегов: %s",
                    exc,
                )

        try:
            tags_from_sessions = list_tags(self.sessions_root)
        except Exception as exc:
            log.warning(
                "Не удалось получить теги из сессий: %s", exc
            )
            tags_from_sessions = []

        # Объединяем, сохраняя порядок: сначала справочник,
        # потом теги из записей, которых нет в справочнике.
        merged: List[str] = []
        seen = set()
        for t in tags_from_config + tags_from_sessions:
            t = (t or "").strip()
            if t and t not in seen:
                merged.append(t)
                seen.add(t)

        current = self.tag_combo.currentData() or ""

        self.tag_combo.blockSignals(True)
        self.tag_combo.clear()
        self.tag_combo.addItem("— без тега —", "__untagged__")
        for t in merged:
            self.tag_combo.addItem(t, t)
        idx = self.tag_combo.findData(current)
        if idx >= 0:
            self.tag_combo.setCurrentIndex(idx)
        self.tag_combo.blockSignals(False)

        self.tag_combo.setEnabled(self.tag_enabled.isChecked())
        log.debug(
            "Теги обновлены: из справочника %d, из сессий %d, "
            "итого %d",
            len(tags_from_config), len(tags_from_sessions),
            len(merged),
        )

    def _on_tag_enabled_toggled(self, enabled: bool) -> None:
        self.tag_combo.setEnabled(enabled)

    # ------------------------------------------------------------------
    # Поиск
    # ------------------------------------------------------------------
    def _build_filters(self) -> SearchFilters:
        f = SearchFilters()
        f.query = self.query_input.text().strip()
        f.search_transcripts = self.cb_transcripts.isChecked()
        f.search_protocols = self.cb_protocols.isChecked()
        f.search_summaries = self.cb_summaries.isChecked()
        f.search_attachments = self.cb_attachments.isChecked()
        f.project = self.project_combo.currentData() or ""
        if self.date_enabled.isChecked():
            qd_from = self.date_from.date()
            qd_to = self.date_to.date()
            f.date_from = datetime(
                qd_from.year(), qd_from.month(), qd_from.day()
            )
            f.date_to = datetime(
                qd_to.year(), qd_to.month(), qd_to.day()
            )
        f.fuzzy_threshold = max(
            0.5, min(1.0, self.fuzzy_spin.value() / 100.0)
        )
        f.fuzzy_max_word_distance = int(
            self._app_cfg.get("fuzzy_max_word_distance", 200)
        )

        # --- Фильтр по тегу ---
        if self.tag_enabled.isChecked():
            tag_data = self.tag_combo.currentData() or ""
            if tag_data == "__untagged__":
                # «— без тега —»: оставляем только записи без тегов.
                f.tag = ""
                f.tag_includes_untagged = True
            else:
                f.tag = str(tag_data).strip()
                f.tag_includes_untagged = False

        return f

    def _start_search(self) -> None:
        if self._thread is not None and self._thread.isRunning():
            return
        filters = self._build_filters()
        if not filters.query:
            QMessageBox.information(
                self, "Библиотека", "Введите поисковый запрос."
            )
            return

        self._hits = []
        self.table.setRowCount(0)
        self.preview.clear()
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.search_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.status_label.setText("Поиск…")
        self._update_prompt_buttons_state()

        log.info(
            "Поиск: query=%r, проекты=%s, даты=%s..%s, "
            "транскрипт=%s, протокол=%s, summary=%s, вложения=%s, "
            "fuzzy=%.2f, mwd=%d, tag=%r, untagged=%s",
            filters.query, filters.project or "все",
            filters.date_from.date() if filters.date_from else "—",
            filters.date_to.date() if filters.date_to else "—",
            filters.search_transcripts, filters.search_protocols,
            filters.search_summaries, filters.search_attachments,
            filters.fuzzy_threshold, filters.fuzzy_max_word_distance,
            filters.tag, filters.tag_includes_untagged,
        )

        self._thread = SearchThread(
            self.sessions_root, filters
        )
        self._thread.progress.connect(self._on_search_progress)
        self._thread.finished_ok.connect(self._on_search_finished)
        self._thread.failed.connect(self._on_search_failed)
        self._thread.start()

    def _cancel_search(self) -> None:
        if self._thread is not None and self._thread.isRunning():
            log.info("Запрошена отмена поиска")
            self._thread.requestInterruption()
            wait_ms = int(
                self._app_cfg.get("search_cancel_wait_ms", 3000)
            )
            self._thread.wait(wait_ms)
        self._on_search_cancelled()

    def _on_search_progress(self, cur: int, total: int) -> None:
        if total <= 0:
            return
        self.progress.setValue(int(cur * 100 / total))

    def _on_search_finished(self, hits: list) -> None:
        self._hits = list(hits or [])
        self._render_hits()
        self._on_search_done(len(self._hits))
        self._update_prompt_buttons_state()

    def _on_search_failed(self, error: str) -> None:
        log.error("Поиск провален: %s", error)
        QMessageBox.critical(
            self, "Библиотека", f"Ошибка поиска:\n{error}"
        )
        self._on_search_done(-1)
        self._update_prompt_buttons_state()

    def _on_search_cancelled(self) -> None:
        log.info("Поиск отменён пользователем")
        self._on_search_done(-1)
        self._update_prompt_buttons_state()

    def _on_search_done(self, count: int) -> None:
        self.progress.setVisible(False)
        self.search_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        if count < 0:
            self.status_label.setText("Поиск отменён")
        else:
            self.status_label.setText(f"Найдено: {count}")
        self._thread = None

    def _render_hits(self) -> None:
        self.table.setRowCount(0)
        for h in self._hits:
            row = self.table.rowCount()
            self.table.insertRow(row)

            self.table.setItem(row, 0, QTableWidgetItem(h.date))
            self.table.setItem(
                row, 1, QTableWidgetItem(h.session_name)
            )
            self.table.setItem(
                row, 2, QTableWidgetItem(h.project)
            )

            tags_text = ", ".join(h.tags) if h.tags else "—"
            tags_item = QTableWidgetItem(tags_text)
            if h.tags:
                tags_item.setToolTip(
                    "Теги записи: " + ", ".join(h.tags)
                )
            self.table.setItem(row, 3, tags_item)

            source_item = QTableWidgetItem(
                SOURCE_LABELS.get(h.source, h.source)
            )
            if h.source == "transcript":
                source_item.setForeground(
                    Qt.GlobalColor.darkBlue
                )
            elif h.source == "protocol":
                source_item.setForeground(
                    Qt.GlobalColor.darkGreen
                )
            elif h.source == "summary":
                source_item.setForeground(
                    Qt.GlobalColor.darkMagenta
                )
            self.table.setItem(row, 4, source_item)

            kind = (
                "точное" if h.match_kind == "exact"
                else f"fuzzy {int(h.score * 100)}%"
            )
            snippet_item = QTableWidgetItem(
                f"[{kind}] {h.snippet}"
            )
            snippet_item.setToolTip(h.snippet)
            self.table.setItem(row, 5, snippet_item)

            file_item = QTableWidgetItem(h.file_label)
            file_item.setToolTip(h.file_path)
            self.table.setItem(row, 6, file_item)

        if self._hits:
            self.table.selectRow(0)

    # ------------------------------------------------------------------
    # Выбор и превью
    # ------------------------------------------------------------------
    def _selected_hit(self) -> Optional[SearchHit]:
        i = self.table.currentRow()
        if i < 0 or i >= len(self._hits):
            return None
        return self._hits[i]

    def _on_selection_changed(self) -> None:
        h = self._selected_hit()
        if not h:
            self.preview.clear()
            return

        tags_html = ""
        if h.tags:
            tags_html = (
                f"<p><b>Теги:</b> {', '.join(h.tags)}</p>"
            )

        html = [
            f"<h3>{h.session_name}</h3>",
            f"<p><b>Проект:</b> {h.project or '—'} &nbsp; "
            f"<b>Дата:</b> {h.date or '—'}</p>",
            tags_html,
            f"<p><b>Где найдено:</b> "
            f"{SOURCE_LABELS.get(h.source, h.source)} "
            f"({h.file_label})</p>",
            f"<p><b>Тип совпадения:</b> "
            f"{'точное' if h.match_kind == 'exact' else f'нечёткое (fuzzy, {int(h.score * 100)}%)'}"
            f"</p>",
            "<hr>",
            f"<pre style='white-space:pre-wrap; "
            f"font-family:monospace;'>"
            f"{self._highlight(h.snippet)}</pre>",
        ]
        self.preview.setHtml("".join(html))

    @staticmethod
    def _highlight(text: str) -> str:
        import html as _html
        return _html.escape(text)

    # ------------------------------------------------------------------
    # Контекстное меню
    # ------------------------------------------------------------------
    def _show_context_menu(self, pos) -> None:
        h = self._selected_hit()
        if not h:
            return
        menu = QMenu(self)
        menu.addAction(
            "Открыть папку записи", self._open_session_folder
        )
        menu.addAction(
            "Открыть найденный файл", self._open_found_file
        )
        menu.addSeparator()
        menu.addAction(
            "Сохранить файл как…", self._save_found_file
        )
        menu.addSeparator()
        menu.addAction(
            "Скопировать путь в буфер", self._copy_path
        )
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _copy_path(self) -> None:
        h = self._selected_hit()
        if not h:
            return
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(h.file_path)
        self.status_label.setText(
            "Путь скопирован в буфер обмена"
        )

    # ------------------------------------------------------------------
    # Действия
    # ------------------------------------------------------------------
    def _open_session_folder(self) -> None:
        h = self._selected_hit()
        if not h:
            QMessageBox.warning(
                self, "Библиотека", "Выберите результат"
            )
            return
        if not os.path.isdir(h.session_dir):
            QMessageBox.warning(
                self, "Библиотека",
                f"Папка не найдена:\n{h.session_dir}",
            )
            return
        log.info(
            "Открытие папки записи из библиотеки: %s",
            h.session_dir,
        )
        QDesktopServices.openUrl(
            QUrl.fromLocalFile(h.session_dir)
        )

    def _open_found_file(self) -> None:
        h = self._selected_hit()
        if not h:
            QMessageBox.warning(
                self, "Библиотека", "Выберите результат"
            )
            return
        if not os.path.exists(h.file_path):
            QMessageBox.warning(
                self, "Библиотека",
                f"Файл не найден:\n{h.file_path}",
            )
            return
        log.info("Открытие найденного файла: %s", h.file_path)
        QDesktopServices.openUrl(
            QUrl.fromLocalFile(h.file_path)
        )

    def _save_found_file(self) -> None:
        h = self._selected_hit()
        if not h:
            QMessageBox.warning(
                self, "Библиотека", "Выберите результат"
            )
            return
        if not os.path.exists(h.file_path):
            QMessageBox.warning(
                self, "Библиотека",
                f"Файл не найден:\n{h.file_path}",
            )
            return

        base = os.path.basename(h.file_path)
        ext = os.path.splitext(base)[1]
        default_path = os.path.join(
            os.path.expanduser("~"), f"{h.session_name}_{base}"
        )

        target, _ = QFileDialog.getSaveFileName(
            self,
            "Сохранить файл как",
            default_path,
            f"Файлы (*{ext});;Все файлы (*)",
        )
        if not target:
            return

        try:
            shutil.copy2(h.file_path, target)
            log.info(
                "Файл сохранён: %s → %s", h.file_path, target
            )
            QMessageBox.information(
                self, "Библиотека",
                f"Файл сохранён:\n{target}",
            )
        except Exception as exc:
            log.exception("Ошибка сохранения файла: %s", exc)
            QMessageBox.critical(
                self, "Библиотека",
                f"Не удалось сохранить:\n{exc}",
            )

    # ------------------------------------------------------------------
    # Справка
    # ------------------------------------------------------------------
    def _show_help(self) -> None:
        QMessageBox.information(
            self, "Как работает поиск",
            "Поиск выполняется по файлам в папке sessions/ — "
            "без базы данных.\n\n"
            "Искать можно в:\n"
            "  • стенограммах (video.txt);\n"
            "  • протоколах (protocol.*, deepseek_prompt.*, "
            "manual_protocol.*);\n"
            "  • summary (session.json → summary_bb);\n"
            "  • вложениях (attachments/*).\n\n"
            "Для стенограмм с опечатками используется нечёткий "
            "поиск (fuzzy). Сначала ищутся точные вхождения, "
            "затем — похожие слова. Порог схожести регулируется "
            "ползунком «Fuzzy»:\n"
            "  • 82% — разумный баланс;\n"
            "  • ниже — больше совпадений, но выше шум;\n"
            "  • 100% — только точные слова.\n\n"
            "Фильтры по проекту, тегу и датам сужают область "
            "поиска, что ускоряет работу на больших архивах.\n\n"
            "Фильтр «Тег»:\n"
            "  • отметьте галочку «Тег:», чтобы включить фильтр;\n"
            "  • выберите нужный тег из списка — поиск идёт "
            "только по записям с этим тегом;\n"
            "  • «— без тега —» оставляет только записи без "
            "тегов.\n\n"
            "Кнопка «Стоп» прерывает поиск немедленно — "
            "будут показаны уже найденные результаты.\n\n"
            "Двойной клик по результату открывает папку записи."
        )