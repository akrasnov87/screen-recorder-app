"""Окно «Библиотека» — полнотекстовый поиск по записям."""
from __future__ import annotations

import os
import shutil
from datetime import datetime
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDateEdit, QDialog,
    QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMenu, QMenuBar, QMessageBox, QProgressBar, QPushButton,
    QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QTextBrowser,
    QVBoxLayout, QWidget,
)

from .library_search import (
    SearchFilters, SearchHit, list_projects, search,
)
from .logger import get_logger
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

    def __init__(self, sessions_root: str, filters: SearchFilters) -> None:
        super().__init__()
        self.sessions_root = sessions_root
        self.filters = filters

    def run(self) -> None:
        try:
            def cb(cur: int, total: int) -> None:
                self.progress.emit(cur, total)

            hits = search(self.sessions_root, self.filters, progress_cb=cb)
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

        self.setWindowTitle("Библиотека — поиск по записям")
        self.setMinimumSize(1280, 820)
        self.setModal(False)

        self._build_ui()
        self._reload_projects()
        log.info("LibraryWindow открыто, sessions_root=%s", sessions_root)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 4, 8, 8)
        root.setSpacing(6)

        # --- Меню ---
        bar = QMenuBar(self)
        layout: QVBoxLayout = self.layout()
        layout.insertWidget(0, bar)

        m_file = bar.addMenu("Файл")
        act_refresh = QAction("Обновить список проектов", self)
        act_refresh.setShortcut(QKeySequence("F5"))
        act_refresh.triggered.connect(self._reload_projects)
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
            "Полнотекстовый поиск по сохранённым записям. Ищет по файлам "
            "в папке <code>sessions/</code> — без базы данных. "
            "Для стенограмм используется нечёткий поиск, устойчивый к опечаткам. "
            "Сузьте область фильтрами «Проект» и «Период», чтобы ускорить работу."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("QLabel { color: #666; }")
        intro_row.addWidget(intro, 1)
        intro_icon = make_info_icon("lib_intro")
        if intro_icon is not None:
            intro_row.addWidget(intro_icon, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(intro_row)

        # --- Форма фильтров ---
        filters_box = QVBoxLayout()

        # строка 1: запрос
        row1 = QHBoxLayout()

        self.query_input = QLineEdit()
        self.query_input.setPlaceholderText(
            "Например: миграция на новый стек, ЕЖД, риск по срокам…"
        )
        self.query_input.returnPressed.connect(self._start_search)
        row1.addWidget(QLabel("Запрос:"))
        row1.addWidget(with_info(self.query_input, "lib_query"), 1)

        self.search_btn = QPushButton("Найти")
        self.search_btn.setDefault(True)
        self.search_btn.setToolTip("Запустить поиск (Enter в поле запроса)")
        self.search_btn.clicked.connect(self._start_search)
        row1.addWidget(self.search_btn)

        self.cancel_btn = QPushButton("Стоп")
        self.cancel_btn.setToolTip("Прервать текущий поиск")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel_search)
        row1.addWidget(self.cancel_btn)

        filters_box.addLayout(row1)

        # строка 2: где искать
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Искать в:"))

        self.cb_transcripts = QCheckBox("Стенограммах")
        self.cb_transcripts.setChecked(True)
        self.cb_transcripts.setToolTip(
            "Поиск по файлам video.txt.\n"
            "Используется нечёткий поиск (fuzzy), устойчивый к опечаткам."
        )
        self.cb_protocols = QCheckBox("Протоколах")
        self.cb_protocols.setChecked(True)
        self.cb_protocols.setToolTip(
            "Поиск по protocol.* / deepseek_prompt.* / manual_protocol.*"
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

        # строка 3: проект / даты / fuzzy
        row3 = QHBoxLayout()

        self.project_combo = QComboBox()
        self.project_combo.addItem("— все —", "")
        self.project_combo.setMinimumWidth(180)
        row3.addWidget(QLabel("Проект:"))
        row3.addWidget(with_info(self.project_combo, "lib_project"))

        row3.addSpacing(12)
        self.date_from = QDateEdit()
        self.date_from.setCalendarPopup(True)
        self.date_from.setDisplayFormat("yyyy-MM-dd")
        self.date_from.setDate(self._default_from_date())
        row3.addWidget(QLabel("С:"))
        row3.addWidget(with_info(self.date_from, "lib_date_from"))

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
        self.fuzzy_spin.setValue(82)
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

        # --- Сплиттер: слева результаты, справа превью ---
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Левая часть: заголовок с иконкой + таблица
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

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels([
            "Дата", "Название", "Проект", "Где", "Совпадение", "Файл",
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
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.table.itemDoubleClicked.connect(
            lambda _item: self._open_session_folder()
        )

        hv = self.table.horizontalHeader()
        hv.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hv.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hv.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hv.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hv.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        hv.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        left_layout.addWidget(self.table, 1)

        splitter.addWidget(left_container)

        # Правая часть: заголовок с иконкой + превью
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

        splitter.setSizes([760, 520])
        root.addWidget(splitter, 1)

        # --- Нижняя строка ---
        bottom = QHBoxLayout()
        self.status_label = QLabel("")
        bottom.addWidget(self.status_label)
        bottom.addStretch()

        self.open_folder_btn = QPushButton("Открыть папку записи")
        attach_tooltip(self.open_folder_btn, "lib_open_folder")
        self.open_folder_btn.clicked.connect(self._open_session_folder)
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
        self.close_btn.setToolTip("Закрыть окно библиотеки (Ctrl+W)")
        self.close_btn.clicked.connect(self.close)
        bottom.addWidget(self.close_btn)

        root.addLayout(bottom)

    # ------------------------------------------------------------------
    # Даты по умолчанию
    # ------------------------------------------------------------------
    @staticmethod
    def _default_from_date():
        from PySide6.QtCore import QDate
        return QDate.currentDate().addMonths(-1)

    @staticmethod
    def _default_to_date():
        from PySide6.QtCore import QDate
        return QDate.currentDate()

    # ------------------------------------------------------------------
    # Проекты
    # ------------------------------------------------------------------
    def _reload_projects(self) -> None:
        try:
            projects = list_projects(self.sessions_root)
        except Exception as exc:
            log.warning("Не удалось получить список проектов: %s", exc)
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
            f.date_from = datetime(qd_from.year(), qd_from.month(), qd_from.day())
            f.date_to = datetime(qd_to.year(), qd_to.month(), qd_to.day())
        f.fuzzy_threshold = max(0.5, min(1.0, self.fuzzy_spin.value() / 100.0))
        return f

    def _start_search(self) -> None:
        if self._thread is not None and self._thread.isRunning():
            return
        filters = self._build_filters()
        if not filters.query:
            QMessageBox.information(self, "Библиотека",
                                    "Введите поисковый запрос.")
            return

        self._hits = []
        self.table.setRowCount(0)
        self.preview.clear()
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.search_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.status_label.setText("Поиск…")

        log.info(
            "Поиск: query=%r, проекты=%s, даты=%s..%s, "
            "транскрипт=%s, протокол=%s, summary=%s, вложения=%s, "
            "fuzzy=%.2f",
            filters.query, filters.project or "все",
            filters.date_from.date() if filters.date_from else "—",
            filters.date_to.date() if filters.date_to else "—",
            filters.search_transcripts, filters.search_protocols,
            filters.search_summaries, filters.search_attachments,
            filters.fuzzy_threshold,
        )

        self._thread = SearchThread(self.sessions_root, filters)
        self._thread.progress.connect(self._on_search_progress)
        self._thread.finished_ok.connect(self._on_search_finished)
        self._thread.failed.connect(self._on_search_failed)
        self._thread.start()

    def _cancel_search(self) -> None:
        if self._thread is not None and self._thread.isRunning():
            log.info("Запрошена отмена поиска")
            self._thread.requestInterruption()
            self._thread.quit()
            self._thread.wait(2000)
        self._on_search_cancelled()

    def _on_search_progress(self, cur: int, total: int) -> None:
        if total <= 0:
            return
        self.progress.setValue(int(cur * 100 / total))

    def _on_search_finished(self, hits: list) -> None:
        self._hits = list(hits or [])
        self._render_hits()
        self._on_search_done(len(self._hits))

    def _on_search_failed(self, error: str) -> None:
        log.error("Поиск провален: %s", error)
        QMessageBox.critical(self, "Библиотека", f"Ошибка поиска:\n{error}")
        self._on_search_done(-1)

    def _on_search_cancelled(self) -> None:
        log.info("Поиск отменён пользователем")
        self._on_search_done(-1)

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
            self.table.setItem(row, 1, QTableWidgetItem(h.session_name))
            self.table.setItem(row, 2, QTableWidgetItem(h.project))

            source_item = QTableWidgetItem(
                SOURCE_LABELS.get(h.source, h.source)
            )
            if h.source == "transcript":
                source_item.setForeground(Qt.GlobalColor.darkBlue)
            elif h.source == "protocol":
                source_item.setForeground(Qt.GlobalColor.darkGreen)
            elif h.source == "summary":
                source_item.setForeground(Qt.GlobalColor.darkMagenta)
            self.table.setItem(row, 3, source_item)

            kind = (
                "точное" if h.match_kind == "exact"
                else f"fuzzy {int(h.score * 100)}%"
            )
            snippet_item = QTableWidgetItem(f"[{kind}] {h.snippet}")
            snippet_item.setToolTip(h.snippet)
            self.table.setItem(row, 4, snippet_item)

            file_item = QTableWidgetItem(h.file_label)
            file_item.setToolTip(h.file_path)
            self.table.setItem(row, 5, file_item)

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

        html = [
            f"<h3>{h.session_name}</h3>",
            f"<p><b>Проект:</b> {h.project or '—'} &nbsp; "
            f"<b>Дата:</b> {h.date or '—'}</p>",
            f"<p><b>Где найдено:</b> {SOURCE_LABELS.get(h.source, h.source)} "
            f"({h.file_label})</p>",
            f"<p><b>Тип совпадения:</b> "
            f"{'точное' if h.match_kind == 'exact' else f'нечёткое (fuzzy, {int(h.score * 100)}%)'}"
            f"</p>",
            "<hr>",
            f"<pre style='white-space:pre-wrap; font-family:monospace;'>"
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
        menu.addAction("Открыть папку записи", self._open_session_folder)
        menu.addAction("Открыть найденный файл", self._open_found_file)
        menu.addSeparator()
        menu.addAction("Сохранить файл как…", self._save_found_file)
        menu.addSeparator()
        menu.addAction("Скопировать путь в буфер", self._copy_path)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _copy_path(self) -> None:
        h = self._selected_hit()
        if not h:
            return
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(h.file_path)
        self.status_label.setText("Путь скопирован в буфер обмена")

    # ------------------------------------------------------------------
    # Действия
    # ------------------------------------------------------------------
    def _open_session_folder(self) -> None:
        h = self._selected_hit()
        if not h:
            QMessageBox.warning(self, "Библиотека", "Выберите результат")
            return
        if not os.path.isdir(h.session_dir):
            QMessageBox.warning(self, "Библиотека",
                                f"Папка не найдена:\n{h.session_dir}")
            return
        log.info("Открытие папки записи из библиотеки: %s", h.session_dir)
        QDesktopServices.openUrl(QUrl.fromLocalFile(h.session_dir))

    def _open_found_file(self) -> None:
        h = self._selected_hit()
        if not h:
            QMessageBox.warning(self, "Библиотека", "Выберите результат")
            return
        if not os.path.exists(h.file_path):
            QMessageBox.warning(self, "Библиотека",
                                f"Файл не найден:\n{h.file_path}")
            return
        log.info("Открытие найденного файла: %s", h.file_path)
        QDesktopServices.openUrl(QUrl.fromLocalFile(h.file_path))

    def _save_found_file(self) -> None:
        h = self._selected_hit()
        if not h:
            QMessageBox.warning(self, "Библиотека", "Выберите результат")
            return
        if not os.path.exists(h.file_path):
            QMessageBox.warning(self, "Библиотека",
                                f"Файл не найден:\n{h.file_path}")
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
            log.info("Файл сохранён: %s → %s", h.file_path, target)
            QMessageBox.information(self, "Библиотека",
                                    f"Файл сохранён:\n{target}")
        except Exception as exc:
            log.exception("Ошибка сохранения файла: %s", exc)
            QMessageBox.critical(self, "Библиотека",
                                 f"Не удалось сохранить:\n{exc}")

    # ------------------------------------------------------------------
    # Справка
    # ------------------------------------------------------------------
    def _show_help(self) -> None:
        QMessageBox.information(
            self, "Как работает поиск",
            "Поиск выполняется по файлам в папке sessions/ — без базы данных.\n\n"
            "Искать можно в:\n"
            "  • стенограммах (video.txt);\n"
            "  • протоколах (protocol.*, deepseek_prompt.*, manual_protocol.*);\n"
            "  • summary (session.json → summary_bb);\n"
            "  • вложениях (attachments/*).\n\n"
            "Для стенограмм с опечатками используется нечёткий поиск "
            "(fuzzy). Сначала ищутся точные вхождения, затем — похожие "
            "слова. Порог схожести регулируется ползунком «Fuzzy»:\n"
            "  • 82% — разумный баланс;\n"
            "  • ниже — больше совпадений, но выше шум;\n"
            "  • 100% — только точные слова.\n\n"
            "Фильтры по проекту и датам сужают область поиска, что "
            "ускоряет работу на больших архивах.\n\n"
            "Двойной клик по результату открывает папку записи.",
        )