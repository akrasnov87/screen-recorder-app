"""Окно «ВМ Yandex» — просмотр и редактирование конфигов ВМ.

Каждая ВМ — подпапка в корневой папке (config["yandex_vm"].
root_path). Внутри подпапки ожидаются файлы:
  • schedule.cron  — расписание работы ВМ;
  • exceptions.txt — исключения (переопределения).

Редактор позволяет править оба файла прямо в окне и сразу
сохранять изменения на диск (атомарная запись).

Индикация несохранённых изменений:
  • Каждый YandexVMEditor эмитит сигнал dirty_changed(bool),
    который срабатывает ТОЛЬКО при реальном пользовательском
    редактировании (а не при программной установке текста).
  • YandexVMDialog слушает этот сигнал и обновляет префикс «●»
    в заголовке вкладки.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import List, Optional

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import (
    QDesktopServices, QFont, QKeySequence, QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QFileDialog, QHBoxLayout,
    QLabel, QListWidget, QListWidgetItem, QMessageBox,
    QPlainTextEdit, QPushButton, QSplitter, QTabWidget,
    QVBoxLayout, QWidget,
)

from .logger import get_logger
from .tooltips import attach_tooltip, make_info_icon

log = get_logger(__name__)


# Имена файлов по умолчанию внутри папки ВМ.
_SCHEDULE_DEFAULT = "schedule.cron"
_EXCEPTIONS_DEFAULT = "exceptions.txt"

# Маски для поиска файлов, если стандартных нет.
_SCHEDULE_MASKS = (
    "schedule.cron", "schedule.txt", "schedule",
    "расписание.cron", "расписание.txt",
)
_EXCEPTIONS_MASKS = (
    "exceptions.txt", "exceptions.cron", "exceptions",
    "исключения.txt",
)


# ---------------------------------------------------------------------------
# Утилиты работы с файлами
# ---------------------------------------------------------------------------
def _find_file(folder: str, candidates: tuple) -> str:
    """
    Ищет файл в папке по списку имён.
    Если стандартных нет — ищет по ключевым словам в имени.
    """
    if not folder or not os.path.isdir(folder):
        return ""

    try:
        entries = os.listdir(folder)
    except OSError:
        return ""

    # 1) Точное совпадение имени (регистронезависимо).
    lowered = {e.lower(): e for e in entries}
    for cand in candidates:
        if cand.lower() in lowered:
            return os.path.join(folder, lowered[cand.lower()])

    # 2) Поиск по ключевым словам.
    keywords: List[str] = []
    for cand in candidates:
        stem = os.path.splitext(cand)[0].lower()
        for kw in ("schedule", "cron", "расписание"):
            if kw in stem and kw not in keywords:
                keywords.append(kw)
        for kw in ("exception", "исключен"):
            if kw in stem and kw not in keywords:
                keywords.append(kw)

    for name in sorted(entries):
        low = name.lower()
        for kw in keywords:
            if kw in low:
                full = os.path.join(folder, name)
                if os.path.isfile(full):
                    return full

    return ""


def _read_file(path: str, default: str = "") -> str:
    if not path or not os.path.isfile(path):
        return default
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception as exc:
        log.warning("Не удалось прочитать %s: %s", path, exc)
        return default


def _write_file_atomic(path: str, content: str) -> bool:
    """Атомарная запись файла через .tmp + os.replace."""
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
        return True
    except Exception as exc:
        log.exception("Не удалось записать %s: %s", path, exc)
        return False


# ---------------------------------------------------------------------------
# Редактор одного файла
# ---------------------------------------------------------------------------
class YandexVMEditor(QWidget):
    """
    Редактор одного файла с индикацией несохранённых изменений.

    Сигнал dirty_changed(bool) эмитится только при реальных
    пользовательских правках (см. _on_text_changed) и при
    сбросе «грязного» флага в set_file()/save().
    """

    dirty_changed = Signal(bool)

    def __init__(
        self,
        title: str,
        tooltip_key: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._title = title
        self._path = ""
        self._dirty = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # --- Заголовок с путём ---
        head = QHBoxLayout()
        self.path_label = QLabel("")
        self.path_label.setStyleSheet(
            "QLabel { color: #666; font-style: italic; }"
        )
        self.path_label.setWordWrap(True)
        head.addWidget(self.path_label, 1)

        icon = make_info_icon(tooltip_key)
        if icon is not None:
            head.addWidget(icon, 0)

        layout.addLayout(head)

        # --- Редактор ---
        self.editor = QPlainTextEdit()
        mono = QFont("Monospace")
        mono.setStyleHint(QFont.StyleHint.TypeWriter)
        mono.setPointSize(11)
        self.editor.setFont(mono)
        self.editor.setPlaceholderText(
            f"Содержимое файла «{self._title}»…"
        )
        self.editor.textChanged.connect(self._on_text_changed)
        attach_tooltip(self.editor, "yandex_vm_editor")
        layout.addWidget(self.editor, 1)

        # --- Кнопки ---
        btns = QHBoxLayout()

        self.save_btn = QPushButton("Сохранить")
        self.save_btn.setToolTip(
            "Записать изменения в файл (Ctrl+S)"
        )
        self.save_btn.clicked.connect(self._on_save)
        btns.addWidget(self.save_btn)

        self.reload_btn = QPushButton("Перезагрузить")
        self.reload_btn.setToolTip(
            "Отменить несохранённые правки и перечитать файл "
            "с диска"
        )
        self.reload_btn.clicked.connect(self._on_reload)
        btns.addWidget(self.reload_btn)

        self.open_folder_btn = QPushButton("Открыть папку")
        self.open_folder_btn.setToolTip(
            "Открыть папку с этим файлом в файловом менеджере"
        )
        self.open_folder_btn.clicked.connect(
            self._on_open_folder
        )
        btns.addWidget(self.open_folder_btn)

        btns.addStretch()

        self.status_label = QLabel("")
        self.status_label.setStyleSheet(
            "QLabel { color: #666; }"
        )
        btns.addWidget(self.status_label)

        layout.addLayout(btns)

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._update_buttons()

    # ------------------------------------------------------------------
    # Публичный интерфейс
    # ------------------------------------------------------------------
    def set_file(self, path: str) -> None:
        """
        Загружает файл по пути. Если path == "" — пустой редактор.

        Программная установка текста НЕ должна помечать
        редактор «грязным». Поэтому:
          1) блокируем сигналы QPlainTextEdit на время setPlainText;
          2) сбрасываем self._dirty вручную;
          3) эмитим dirty_changed(False) — слушатели снимут «●».
        """
        self._path = path or ""
        text = _read_file(self._path, default="")

        self.editor.blockSignals(True)
        self.editor.setPlainText(text)
        self.editor.blockSignals(False)

        self._dirty = False
        self.dirty_changed.emit(False)

        if self._path and os.path.isfile(self._path):
            try:
                size = os.path.getsize(self._path)
            except OSError:
                size = 0
            try:
                mtime = datetime.fromtimestamp(
                    os.path.getmtime(self._path)
                ).strftime("%Y-%m-%d %H:%M:%S")
            except OSError:
                mtime = "—"
            self.path_label.setText(
                f"<code>{self._path}</code> "
                f"<span style='color:#888'>"
                f"({size} Б, изменён {mtime})</span>"
            )
        elif self._path:
            self.path_label.setText(
                f"<code>{self._path}</code> "
                f"<span style='color:#c62828'>"
                f"(файл ещё не создан — будет создан при "
                f"сохранении)</span>"
            )
        else:
            self.path_label.setText(
                "<span style='color:#c62828'>"
                "Файл не найден в папке ВМ.</span>"
            )

        self.status_label.setText("")
        self._update_buttons()

    def save(self) -> bool:
        """Сохраняет содержимое редактора на диск."""
        if not self._path:
            QMessageBox.warning(
                self, self._title,
                "Не задан путь к файлу.",
            )
            return False

        text = self.editor.toPlainText()
        ok = _write_file_atomic(self._path, text)
        if not ok:
            QMessageBox.critical(
                self, self._title,
                f"Не удалось сохранить файл:\n{self._path}",
            )
            self.status_label.setText(
                "<span style='color:#c62828'>Ошибка сохранения"
                "</span>"
            )
            return False

        # Сбрасываем «грязный» флаг явно и уведомляем слушателей.
        self._dirty = False
        self.dirty_changed.emit(False)

        # set_file() перечитает файл и обновит размер/mtime.
        # Он тоже эмитит dirty_changed(False) — это безопасно.
        self.set_file(self._path)

        self.status_label.setText(
            f"<span style='color:#2E7D32'>Сохранено: "
            f"{datetime.now():%H:%M:%S}</span>"
        )
        log.info("ВМ Yandex: сохранён файл %s", self._path)
        return True

    def has_unsaved(self) -> bool:
        return self._dirty

    # ------------------------------------------------------------------
    # Обработчики
    # ------------------------------------------------------------------
    def _on_save(self) -> None:
        self.save()

    def _on_reload(self) -> None:
        if self._dirty:
            reply = QMessageBox.question(
                self, self._title,
                "Отменить несохранённые изменения и перечитать "
                "файл с диска?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        self.set_file(self._path)
        self.status_label.setText(
            "<span style='color:#666'>Перечитано с диска"
            "</span>"
        )

    def _on_open_folder(self) -> None:
        if not self._path:
            return
        folder = os.path.dirname(self._path)
        if not folder or not os.path.isdir(folder):
            QMessageBox.information(
                self, self._title,
                f"Папка не найдена:\n{folder}",
            )
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def _on_text_changed(self) -> None:
        """
        Срабатывает на QPlainTextEdit.textChanged.

        Программная установка текста в set_file() защищена
        blockSignals(True) — там этот слот не вызовется.
        При пользовательском редактировании — помечаем dirty
        и эмитим сигнал наружу.
        """
        if not self._dirty:
            self._dirty = True
            self._update_buttons()
            self.dirty_changed.emit(True)

    def _update_buttons(self) -> None:
        self.save_btn.setEnabled(bool(self._path))
        self.reload_btn.setEnabled(bool(self._path))
        self.open_folder_btn.setEnabled(bool(self._path))

        if self._dirty:
            self.status_label.setText(
                "<span style='color:#B8860B'>● есть "
                "несохранённые изменения</span>"
            )
        else:
            self.status_label.setText("")


# ---------------------------------------------------------------------------
# Главное окно
# ---------------------------------------------------------------------------
class YandexVMDialog(QDialog):
    """
    Окно «ВМ Yandex».

    Слева — список виртуальных машин (подпапки корневой папки),
    справа — вкладки «Планировщик» и «Исключения» с редакторами.
    Изменения записываются на диск по кнопке «Сохранить».
    """

    def __init__(
        self,
        root_path: str,
        config_manager=None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._config_manager = config_manager
        self._root_path = root_path or ""

        self.setWindowTitle("ВМ Yandex — управление конфигурациями")
        self.setModal(False)
        self.setMinimumSize(1100, 720)

        self._current_vm_dir: str = ""
        self._build_ui()
        self._reload_vm_list()

        log.info(
            "YandexVMDialog открыт, root=%s",
            self._root_path or "—",
        )

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # --- Плашка с описанием и путём ---
        top = QHBoxLayout()

        info = QLabel(
            "<b>Виртуальные машины Yandex Cloud.</b> "
            "<span style='color:#666'>Каждая ВМ — подпапка "
            "в корневой папке. Внутри лежат файлы "
            "<code>schedule.cron</code> и "
            "<code>exceptions.txt</code>. Их можно "
            "редактировать прямо здесь.</span>"
        )
        info.setWordWrap(True)
        top.addWidget(info, 1)

        self.root_label = QLabel("")
        self.root_label.setStyleSheet(
            "QLabel { color: #444; font-style: italic; }"
        )
        self.root_label.setWordWrap(True)
        top.addWidget(self.root_label, 1)

        icon = make_info_icon("yandex_vm_root")
        if icon is not None:
            top.addWidget(icon, 0)

        root.addLayout(top)

        tools = QHBoxLayout()

        self.refresh_btn = QPushButton("Обновить список")
        self.refresh_btn.setToolTip(
            "Перечитать корневую папку и обновить список ВМ"
        )
        self.refresh_btn.clicked.connect(self._reload_vm_list)
        tools.addWidget(self.refresh_btn)

        self.change_root_btn = QPushButton("Выбрать папку…")
        self.change_root_btn.setToolTip(
            "Сменить корневую папку с конфигурациями ВМ"
        )
        self.change_root_btn.clicked.connect(
            self._on_change_root
        )
        tools.addWidget(self.change_root_btn)

        tools.addStretch()

        self.count_label = QLabel("")
        self.count_label.setStyleSheet(
            "QLabel { color: #666; }"
        )
        tools.addWidget(self.count_label)

        root.addLayout(tools)

        # --- Сплиттер: список ВМ + вкладки ---
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Слева — список ВМ
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(4)

        left_header = QHBoxLayout()
        left_header.addWidget(QLabel("<b>Виртуальные машины</b>"))
        left_header.addStretch()
        left_icon = make_info_icon("yandex_vm_list")
        if left_icon is not None:
            left_header.addWidget(left_icon)
        left_layout.addLayout(left_header)

        self.vm_list = QListWidget()
        self.vm_list.setAlternatingRowColors(True)
        self.vm_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.vm_list.currentItemChanged.connect(
            self._on_vm_selected
        )
        attach_tooltip(self.vm_list, "yandex_vm_list")
        left_layout.addWidget(self.vm_list, 1)

        self.vm_meta_label = QLabel("")
        self.vm_meta_label.setStyleSheet(
            "QLabel { color: #666; }"
        )
        self.vm_meta_label.setWordWrap(True)
        left_layout.addWidget(self.vm_meta_label)

        splitter.addWidget(left)

        # Справа — вкладки
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(4)

        self.vm_title_label = QLabel(
            "<b>Выберите ВМ в списке слева</b>"
        )
        self.vm_title_label.setWordWrap(True)
        right_layout.addWidget(self.vm_title_label)

        self.tabs = QTabWidget()
        right_layout.addWidget(self.tabs, 1)

        self.schedule_editor = YandexVMEditor(
            title="Планировщик (schedule.cron)",
            tooltip_key="yandex_vm_schedule",
            parent=self,
        )
        self.exceptions_editor = YandexVMEditor(
            title="Исключения (exceptions.txt)",
            tooltip_key="yandex_vm_exceptions",
            parent=self,
        )

        self.tabs.addTab(self.schedule_editor, "Планировщик")
        self.tabs.addTab(
            self.exceptions_editor, "Исключения"
        )

        # Подписываемся на dirty_changed, а не на textChanged.
        # Так программная установка текста не будет ставить «●».
        self.schedule_editor.dirty_changed.connect(
            lambda dirty: self._update_tab_title(
                0, self.schedule_editor, dirty
            )
        )
        self.exceptions_editor.dirty_changed.connect(
            lambda dirty: self._update_tab_title(
                1, self.exceptions_editor, dirty
            )
        )

        splitter.addWidget(right)
        splitter.setSizes([320, 780])

        root.addWidget(splitter, 1)

        # --- Нижние кнопки ---
        bottom = QHBoxLayout()

        self.save_all_btn = QPushButton(
            "Сохранить все изменения"
        )
        self.save_all_btn.setToolTip(
            "Сохранить оба файла (расписание и исключения) "
            "текущей ВМ на диск"
        )
        self.save_all_btn.clicked.connect(self._on_save_all)
        bottom.addWidget(self.save_all_btn)

        self.open_vm_folder_btn = QPushButton(
            "Открыть папку ВМ"
        )
        self.open_vm_folder_btn.setToolTip(
            "Открыть папку выбранной ВМ в файловом менеджере"
        )
        self.open_vm_folder_btn.clicked.connect(
            self._on_open_vm_folder
        )
        bottom.addWidget(self.open_vm_folder_btn)

        bottom.addStretch()

        self.close_btn = QPushButton("Закрыть")
        self.close_btn.clicked.connect(self._on_close)
        bottom.addWidget(self.close_btn)

        root.addLayout(bottom)

        # Горячая клавиша Ctrl+S для активной вкладки.
        sc = QShortcut(QKeySequence("Ctrl+S"), self)
        sc.activated.connect(self._on_save_current_tab)

    # ------------------------------------------------------------------
    # Заголовки вкладок
    # ------------------------------------------------------------------
    def _update_tab_title(
        self,
        idx: int,
        editor: YandexVMEditor,
        dirty: bool,
    ) -> None:
        base = "Планировщик" if idx == 0 else "Исключения"
        if dirty:
            base = "● " + base
        if self.tabs.tabText(idx) != base:
            self.tabs.setTabText(idx, base)

    def _reset_tab_titles(self) -> None:
        self.tabs.setTabText(0, "Планировщик")
        self.tabs.setTabText(1, "Исключения")

    def _on_save_current_tab(self) -> None:
        idx = self.tabs.currentIndex()
        if idx == 0:
            self.schedule_editor.save()
        elif idx == 1:
            self.exceptions_editor.save()

    # ------------------------------------------------------------------
    # Список ВМ
    # ------------------------------------------------------------------
    def _reload_vm_list(self) -> None:
        """Перечитывает корневую папку и заполняет список ВМ."""
        self.vm_list.blockSignals(True)
        self.vm_list.clear()
        self.vm_list.blockSignals(False)

        root = self._root_path or ""
        if root:
            self.root_label.setText(
                f"<b>Корневая папка:</b> <code>{root}</code>"
            )
        else:
            self.root_label.setText(
                "<span style='color:#c62828'>"
                "Корневая папка не задана</span>"
            )

        if not root or not os.path.isdir(root):
            self.count_label.setText("ВМ: 0")
            self._clear_editors(
                "Корневая папка не задана или не существует."
            )
            return

        try:
            entries = sorted(os.listdir(root))
        except OSError as exc:
            log.error(
                "Не удалось прочитать %s: %s", root, exc
            )
            QMessageBox.warning(
                self, "ВМ Yandex",
                f"Не удалось прочитать корневую папку:\n"
                f"{root}\n\n{exc}",
            )
            self.count_label.setText("ВМ: 0")
            self._clear_editors(
                "Не удалось прочитать корневую папку."
            )
            return

        vms: List[str] = []
        for name in entries:
            full = os.path.join(root, name)
            if os.path.isdir(full) and not name.startswith("."):
                vms.append(name)

        self.vm_list.blockSignals(True)
        for name in vms:
            full = os.path.join(root, name)
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, full)

            schedule = _find_file(full, _SCHEDULE_MASKS)
            exceptions = _find_file(full, _EXCEPTIONS_MASKS)
            marks: List[str] = []
            if schedule:
                marks.append("schedule")
            if exceptions:
                marks.append("exceptions")
            if marks:
                item.setToolTip(
                    f"Найдено: {', '.join(marks)}"
                )
            else:
                item.setToolTip(
                    "Файлы конфигурации не найдены — при "
                    "сохранении будут созданы"
                )
                item.setForeground(Qt.GlobalColor.gray)

            self.vm_list.addItem(item)
        self.vm_list.blockSignals(False)

        self.count_label.setText(f"ВМ: {len(vms)}")

        if vms:
            self.vm_list.setCurrentRow(0)
        else:
            self._clear_editors(
                "В корневой папке нет подпапок с ВМ."
            )
            QMessageBox.information(
                self, "ВМ Yandex",
                f"В корневой папке нет ни одной подпапки:\n"
                f"{root}\n\n"
                f"Создайте подпапку для каждой ВМ — внутри "
                f"неё будут храниться schedule.cron и "
                f"exceptions.txt.",
            )

    def _clear_editors(self, reason: str) -> None:
        self._current_vm_dir = ""
        self.vm_title_label.setText(
            f"<span style='color:#c62828'>{reason}</span>"
        )
        self.schedule_editor.set_file("")
        self.exceptions_editor.set_file("")
        self.vm_meta_label.setText("")
        self.open_vm_folder_btn.setEnabled(False)
        self.save_all_btn.setEnabled(False)

    def _on_vm_selected(
        self,
        current: Optional[QListWidgetItem],
        previous: Optional[QListWidgetItem],
    ) -> None:
        if current is None:
            self._clear_editors("ВМ не выбрана.")
            return

        vm_dir = current.data(Qt.ItemDataRole.UserRole) or ""
        if not vm_dir or not os.path.isdir(vm_dir):
            self._clear_editors(
                "Папка выбранной ВМ не существует."
            )
            return

        # Защита от потери несохранённых правок.
        if (self.schedule_editor.has_unsaved()
                or self.exceptions_editor.has_unsaved()):
            reply = QMessageBox.question(
                self, "ВМ Yandex",
                "В текущей ВМ есть несохранённые изменения.\n\n"
                "Переключиться на другую ВМ без сохранения?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                # Возвращаем выделение на предыдущий элемент.
                if previous is not None:
                    self.vm_list.blockSignals(True)
                    self.vm_list.setCurrentItem(previous)
                    self.vm_list.blockSignals(False)
                return

        self._current_vm_dir = vm_dir
        vm_name = os.path.basename(vm_dir)

        self.vm_title_label.setText(
            f"<b>ВМ: {vm_name}</b> "
            f"<span style='color:#666'>({vm_dir})</span>"
        )

        schedule_path = _find_file(vm_dir, _SCHEDULE_MASKS)
        exceptions_path = _find_file(vm_dir, _EXCEPTIONS_MASKS)

        # Если не нашли — предлагаем стандартные имена.
        if not schedule_path:
            schedule_path = os.path.join(
                vm_dir, _SCHEDULE_DEFAULT
            )
        if not exceptions_path:
            exceptions_path = os.path.join(
                vm_dir, _EXCEPTIONS_DEFAULT
            )

        self.schedule_editor.set_file(schedule_path)
        self.exceptions_editor.set_file(exceptions_path)

        # Сбрасываем префиксы вкладок (на случай, если остались
        # от предыдущей ВМ — set_file эмитит dirty_changed(False),
        # но на всякий случай дублируем).
        self._reset_tab_titles()

        # Метаинформация о папке.
        try:
            files_count = len(os.listdir(vm_dir))
        except OSError:
            files_count = 0
        self.vm_meta_label.setText(
            f"Файлов в папке: {files_count}"
        )

        self.open_vm_folder_btn.setEnabled(True)
        self.save_all_btn.setEnabled(True)

        log.debug(
            "ВМ Yandex: выбрана «%s», schedule=%s, exceptions=%s",
            vm_name, schedule_path, exceptions_path,
        )

    # ------------------------------------------------------------------
    # Действия
    # ------------------------------------------------------------------
    def _on_change_root(self) -> None:
        start = self._root_path or os.path.expanduser("~")
        folder = QFileDialog.getExistingDirectory(
            self,
            "Выберите корневую папку с конфигурациями ВМ",
            start,
        )
        if not folder:
            return

        self._root_path = folder

        if self._config_manager is not None:
            try:
                cfg = self._config_manager.config
                cfg.setdefault("yandex_vm", {})[
                    "root_path"
                ] = folder
                self._config_manager.save()
                log.info(
                    "ВМ Yandex: корневая папка сохранена в "
                    "config: %s", folder,
                )
            except Exception as exc:
                log.exception(
                    "Не удалось сохранить корневую папку: %s",
                    exc,
                )

        self._reload_vm_list()

    def _on_save_all(self) -> None:
        if not self._current_vm_dir:
            QMessageBox.information(
                self, "ВМ Yandex",
                "Сначала выберите ВМ в списке.",
            )
            return

        ok_schedule = self.schedule_editor.save()
        ok_exceptions = self.exceptions_editor.save()

        # Префиксы снимаются автоматически через dirty_changed(False),
        # который эмитится в save() и set_file(). Дублируем на случай,
        # если save() вернул False из-за ошибки записи.
        self._reset_tab_titles()

        if ok_schedule and ok_exceptions:
            QMessageBox.information(
                self, "ВМ Yandex",
                f"Файлы ВМ «"
                f"{os.path.basename(self._current_vm_dir)}"
                f"» сохранены.",
            )
        else:
            QMessageBox.warning(
                self, "ВМ Yandex",
                "Не все файлы удалось сохранить. "
                "См. сообщения об ошибках.",
            )

    def _on_open_vm_folder(self) -> None:
        if not self._current_vm_dir:
            return
        if not os.path.isdir(self._current_vm_dir):
            QMessageBox.information(
                self, "ВМ Yandex",
                f"Папка не найдена:\n{self._current_vm_dir}",
            )
            return
        QDesktopServices.openUrl(
            QUrl.fromLocalFile(self._current_vm_dir)
        )

    def _on_close(self) -> None:
        if (self.schedule_editor.has_unsaved()
                or self.exceptions_editor.has_unsaved()):
            reply = QMessageBox.question(
                self, "ВМ Yandex",
                "Есть несохранённые изменения.\n\n"
                "Закрыть окно без сохранения?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        self.close()