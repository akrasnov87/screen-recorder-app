"""Диалоговое окно для ввода метаданных записи с библиотекой промптов.

Изменения:
  • Интерфейс переделан на вкладки: «Основное», «Промпт»,
    «Скрам и DeepSeek», «Вложения».
  • Добавлен блок «Теги» (на вкладке «Основное»).
  • Вкладка «Промпт» содержит библиотеку промптов и контекст
    записи в промпте.
  • Чекбоксы контекста записи («Название», «Проект»,
    «Комментарий», «Теги») включены по умолчанию.
  • Если в initial нет prompt_name, но prompt/default_prompt
    совпадает с одним из промптов библиотеки — этот промпт
    автоматически выбирается в комбобоксе.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit,
    QPushButton, QTabWidget, QVBoxLayout, QWidget,
)

from .logger import get_logger
from .tooltips import attach_tooltip, make_info_icon, with_info

log = get_logger(__name__)


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


class MetadataDialog(QDialog):
    """
    Окно ввода метаданных. Переделано на вкладки:
      • Основное
      • Промпт
      • Скрам и DeepSeek
      • Вложения

    Возвращает self.result_data — тот же формат словаря,
    что и раньше (обратная совместимость с main.py и
    sessions_window.py).
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
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(820)
        self.setMinimumHeight(600)

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

        self.result_data: Dict[str, Any] = {}
        self._prompt_edited = False
        self._attachments: List[str] = []

        log.debug(
            "MetadataDialog: title=%r, projects=%d, prompts=%d, "
            "name_templates=%d, tags=%d, sessions_root=%s",
            title, len(self._projects), len(self._prompts),
            len(self._name_templates), len(self._tags),
            sessions_root or "—",
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

        # --- Вкладки ---
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        self.tabs.addTab(self._build_main_tab(), "Основное")
        self.tabs.addTab(self._build_prompt_tab(), "Промпт")
        self.tabs.addTab(
            self._build_scrum_tab(), "Скрам и DeepSeek"
        )
        self.tabs.addTab(
            self._build_attachments_tab(), "Вложения"
        )

        # --- Кнопки ---
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
    # Вкладка «Основное»
    # ------------------------------------------------------------------
    def _build_main_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        info = QLabel(
            "Базовая информация о записи: проект, название, "
            "комментарий и теги."
        )
        info.setWordWrap(True)
        info.setStyleSheet("QLabel { color: #666; }")
        layout.addWidget(info)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        # --- Проект ---
        self.project_combo = QComboBox()
        self.project_combo.setEditable(True)
        self.project_combo.addItems(self._projects)
        self.project_combo.setCurrentIndex(-1)
        form.addRow(
            "Проект:",
            with_info(self.project_combo, "meta_project"),
        )

        # --- Название + шаблон ---
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

        # --- Превью итогового имени + кнопка сохранения шаблона ---
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

        # --- Комментарий ---
        self.comment_input = QPlainTextEdit()
        self.comment_input.setPlaceholderText(
            "Свободный комментарий…"
        )
        self.comment_input.setFixedHeight(70)
        form.addRow(
            "Комментарий:",
            with_info(self.comment_input, "meta_comment"),
        )

        layout.addLayout(form)

        # --- Теги ---
        layout.addWidget(self._build_tags_section())

        layout.addStretch()
        return w

    @staticmethod
    def _wrap(layout) -> QWidget:
        w = QWidget()
        w.setLayout(layout)
        return w

    # ------------------------------------------------------------------
    # Блок тегов
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
            "тегов из справочника. Можно добавить новый тег — он "
            "попадёт в справочник (Настройки → Теги). Снять все "
            "теги — кнопкой «Снять все».</span>"
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
        log.debug("Теги записи: %s", self._selected_tags)

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
        log.info("Тег «%s» добавлен и выбран у записи", name)

    def _on_clear_tags(self) -> None:
        self._selected_tags = []
        self._populate_tags_list()
        log.debug("Все теги сняты")

    # ------------------------------------------------------------------
    # Вкладка «Промпт»
    # ------------------------------------------------------------------
    def _build_prompt_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        info = QLabel(
            "Промпт — инструкция, которая используется при "
            "суммаризации и при формировании файла DeepSeek. "
            "Можно выбрать готовый из библиотеки или написать "
            "свой."
        )
        info.setWordWrap(True)
        info.setStyleSheet("QLabel { color: #666; }")
        layout.addWidget(info)

        # --- Выбор промпта из библиотеки ---
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

        # --- Текст промпта ---
        self.prompt_input = QPlainTextEdit()
        self.prompt_input.setPlaceholderText(
            "Промпт для формирования краткого содержания "
            "и/или DeepSeek-промпта…"
        )
        self.prompt_input.setMinimumHeight(160)
        layout.addWidget(self.prompt_input, 1)

        # --- Кнопки управления промптом ---
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

        # --- Контекст записи в промпте ---
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
            "записи добавить в промпт. Информация добавляется "
            "отдельным блоком «КОНТЕКСТ ЗАПИСИ» перед инструкцией. "
            "Это помогает ИИ корректнее писать результат. "
            "По умолчанию включены все четыре пункта — "
            "снимите галочки, если не нужно.</span>"
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
        self.context_all_btn.setToolTip(
            "Проставить все галочки контекста"
        )
        self.context_all_btn.clicked.connect(
            self._on_context_enable_all
        )
        ctx_row.addWidget(self.context_all_btn)

        self.context_none_btn = QPushButton("Снять всё")
        self.context_none_btn.setToolTip(
            "Снять все галочки контекста — в промпт пойдёт "
            "только текст инструкции"
        )
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
        log.info("Сняты все чекбоксы контекста записи")

    # ------------------------------------------------------------------
    # Вкладка «Скрам и DeepSeek»
    # ------------------------------------------------------------------
    def _build_scrum_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        info = QLabel(
            "Управление генерацией итоговых документов: "
            "краткого содержания и файла-промпта для DeepSeek. "
            "Здесь же — признак скрам-митинга и файл предыдущего "
            "протокола."
        )
        info.setWordWrap(True)
        info.setStyleSheet("QLabel { color: #666; }")
        layout.addWidget(info)

        # --- Summary ---
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

        # --- DeepSeek ---
        deepseek_header = QHBoxLayout()
        deepseek_header.addWidget(
            QLabel("<b>Промпт для DeepSeek</b>")
        )
        deepseek_header.addStretch()
        layout.addLayout(deepseek_header)

        self.generate_deepseek_check = QCheckBox(
            "Сформировать файл промпта для DeepSeek "
            "(включено по умолчанию)"
        )
        attach_tooltip(
            self.generate_deepseek_check,
            "meta_generate_deepseek_prompt",
        )
        self.generate_deepseek_check.setChecked(True)
        layout.addWidget(self.generate_deepseek_check)

        deepseek_hint = QLabel(
            "<span style='color:#666'>Шаблон промпта зависит "
            "от признака «Скрам-митинг»:<br>"
            "  • <b>Скрам включён</b> → используется шаблон из "
            "Настройки → Скрам (плюс предыдущий протокол "
            "и стенограмма).<br>"
            "  • <b>Скрам выключен</b> → используется промпт "
            "с вкладки «Промпт».</span>"
        )
        deepseek_hint.setWordWrap(True)
        layout.addWidget(deepseek_hint)

        # --- Скрам ---
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
        self.from_session_btn.setToolTip(
            "Выбрать предыдущий протокол из существующих сессий"
        )
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

    # ------------------------------------------------------------------
    # Вкладка «Вложения»
    # ------------------------------------------------------------------
    def _build_attachments_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        info = QLabel(
            "Файлы, которые можно передать в суммаризацию "
            "или включить в промпт DeepSeek. Текст будет "
            "автоматически извлечён из <code>.txt</code>, "
            "<code>.md</code>, <code>.docx</code>, "
            "<code>.pdf</code>."
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

        # --- Куда передавать ---
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
    # Подключение сигналов
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
    # Контекст в промпте
    # ------------------------------------------------------------------
    def _on_context_enable_all(self) -> None:
        self.include_name_check.setChecked(True)
        self.include_project_check.setChecked(True)
        self.include_comment_check.setChecked(True)
        self.include_tags_check.setChecked(True)
        log.info("Включены все чекбоксы контекста записи")

    # ------------------------------------------------------------------
    # Шаблоны названий
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
        log.info(
            "Сохранён шаблон названия: label=%r, template=%r",
            label, candidate,
        )

    # ------------------------------------------------------------------
    # Скрам
    # ------------------------------------------------------------------
    def _on_scrum_toggled(self, checked: bool) -> None:
        if checked and not self.generate_deepseek_check.isChecked():
            self.generate_deepseek_check.setChecked(True)
            log.info(
                "Скрам включён — автоматически включено "
                "формирование промпта для DeepSeek"
            )

    def _update_scrum_visibility(self, checked: bool) -> None:
        for w in self._scrum_widgets:
            w.setEnabled(checked)
        log.debug("Видимость скрам-полей: %s", checked)

    def _on_browse_protocol(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Выберите файл предыдущего протокола",
            os.path.expanduser("~"),
            "Документы (*.docx *.txt *.md *.pdf);;Все файлы (*)",
        )
        if path:
            self.protocol_path_input.setText(path)
            log.info("Выбран файл протокола: %s", path)

    def _on_clear_protocol(self) -> None:
        old = self.protocol_path_input.text()
        self.protocol_path_input.setText("")
        log.info("Файл протокола сброшен (был: %s)", old or "—")

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
                "протокола.\n\n"
                "Поддерживаются: manual_protocol.*, "
                "protocol.*, deepseek_prompt.*, video.txt.",
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
            log.info("Протокол выбран из записи: %s", path)

    # ------------------------------------------------------------------
    # Вложения
    # ------------------------------------------------------------------
    def _refresh_attachments_list(self) -> None:
        self.attachments_list.clear()
        for p in self._attachments:
            it = QListWidgetItem(os.path.basename(p))
            it.setToolTip(p)
            it.setData(Qt.ItemDataRole.UserRole, p)
            self.attachments_list.addItem(it)
        log.debug(
            "Список вложений обновлён: %d файлов",
            len(self._attachments),
        )

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
            log.info(
                "Добавлено вложений: %d (всего %d)",
                added, len(self._attachments),
            )
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
        log.info(
            "Удалено вложений: %d (осталось %d)",
            len(to_remove), len(self._attachments),
        )

    # ------------------------------------------------------------------
    # Заполнение
    # ------------------------------------------------------------------
    def _apply_initial(self) -> None:
        init = self._initial or {}

        project = init.get("project", "")
        if project:
            idx = self.project_combo.findText(project)
            if idx >= 0:
                self.project_combo.setCurrentIndex(idx)
            else:
                self.project_combo.setEditText(project)
        elif self._projects:
            self.project_combo.setCurrentIndex(0)

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

        # --- Summary / DeepSeek ---
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

        # ------------------------------------------------------------------
        # Промпт: если явного текста нет — берём default_prompt;
        # если он совпадает с одним из промптов библиотеки —
        # выбираем его в комбобоксе.
        # ------------------------------------------------------------------
        prompt_text = (
            init.get("prompt", "") or self._default_prompt
        )
        prompt_name = init.get("prompt_name", "")

        # 1) Если prompt_name задан явно — просто выбираем его.
        if prompt_name:
            idx = self.prompt_combo.findText(prompt_name)
            if idx >= 0:
                self.prompt_combo.blockSignals(True)
                self.prompt_combo.setCurrentIndex(idx)
                self.prompt_combo.blockSignals(False)
                # Синхронизируем текст из библиотеки, если
                # в initial не был указан собственный prompt.
                if not init.get("prompt", "").strip():
                    lib_text = self.prompt_combo.itemData(idx) or ""
                    if lib_text:
                        prompt_text = lib_text
        else:
            # 2) Явного имени нет. Пробуем найти в библиотеке
            #    промпт с текстом, равным prompt_text.
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
                log.debug(
                    "Промпт по умолчанию совпал с библиотечным: "
                    "%r", self.prompt_combo.itemText(found_idx),
                )
            else:
                # Ничего не нашли — оставляем «— не выбрано —»,
                # но текст всё равно подставим в редактор.
                self.prompt_combo.blockSignals(True)
                self.prompt_combo.setCurrentIndex(0)
                self.prompt_combo.blockSignals(False)

        self.prompt_input.blockSignals(True)
        self.prompt_input.setPlainText(prompt_text)
        self.prompt_input.blockSignals(False)
        self._prompt_edited = False

        # ------------------------------------------------------------------
        # Контекст в промпт.
        # По умолчанию (если в initial нет соответствующих ключей)
        # все четыре галочки включены.
        # ------------------------------------------------------------------
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

        log.debug(
            "Начальные значения применены: project=%r, name=%r, "
            "template=%r, abbr=%r, tags=%s, generate_summary=%s, "
            "prompt=%d символов (combo=%r), is_scrum=%s, "
            "generate_deepseek=%s, ctx_name=%s, ctx_project=%s, "
            "ctx_comment=%s, ctx_tags=%s, attachments=%d",
            project, init_name, init_template, init_abbr,
            self._selected_tags,
            self.generate_summary_check.isChecked(),
            len(prompt_text),
            self.prompt_combo.currentText(),
            is_scrum, gen,
            self.include_name_check.isChecked(),
            self.include_project_check.isChecked(),
            self.include_comment_check.isChecked(),
            self.include_tags_check.isChecked(),
            len(self._attachments),
        )

    # ------------------------------------------------------------------
    # Промпт
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
                log.debug(
                    "Замена промпта отменена пользователем"
                )
                return

        self.prompt_input.blockSignals(True)
        self.prompt_input.setPlainText(text)
        self.prompt_input.blockSignals(False)
        self._prompt_edited = False
        log.debug(
            "Выбран промпт из библиотеки: %s (%d символов)",
            name, len(text),
        )

    def _on_prompt_edited(self) -> None:
        if not self._prompt_edited:
            log.debug("Промпт отредактирован вручную")
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
                log.info(
                    "Промпт сброшен к значению из библиотеки"
                )
                return
        if self._default_prompt:
            self.prompt_input.blockSignals(True)
            self.prompt_input.setPlainText(self._default_prompt)
            self.prompt_input.blockSignals(False)
            self._prompt_edited = False
            log.info("Промпт сброшен к значению по умолчанию")

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
        log.debug(
            "Список промптов обновлён: %d записей",
            len(self._prompts),
        )

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

        log.info(
            "Сохранение промпта в библиотеку: name=%r, "
            "%d символов", name, len(text),
        )

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
            log.warning(
                "Колбэк on_save_prompt не передан — "
                "сохранение отложено до «Продолжить»"
            )

        self._refresh_prompts_list(selected_name=name)

        QMessageBox.information(
            self, "Промпт",
            f"Промпт «{name}» сохранён в библиотеку.",
        )
        log.info("Промпт «%s» сохранён в библиотеку", name)

    # ------------------------------------------------------------------
    # Кнопки
    # ------------------------------------------------------------------
    def _on_accept(self) -> None:
        project = (
            self.project_combo.currentText().strip() or "Default"
        )

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

        # Собираем теги в порядке справочника.
        selected_set = set(self._selected_tags)
        ordered_tags: List[str] = []
        for tag in self._tags:
            name = tag.get("name") or ""
            if name and name in selected_set:
                ordered_tags.append(name)
        for name in self._selected_tags:
            if name and name not in ordered_tags:
                ordered_tags.append(name)

        result: Dict[str, Any] = {
            "project": project,
            "name": final_name,
            "description": final_name,
            "name_template": template,
            "name_abbr": abbr,
            "comment": comment,
            "tags": ordered_tags,
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
        }
        if "new_prompt" in self.result_data:
            result["new_prompt"] = self.result_data["new_prompt"]

        self.result_data = result
        log.info(
            "Метаданные подтверждены: project=%s, name=%s, "
            "template=%r, abbr=%r, tags=%s, prompt=%d символов "
            "(изменён: %s), generate_summary=%s, "
            "is_scrum=%s, generate_deepseek=%s, "
            "ctx_name=%s, ctx_project=%s, ctx_comment=%s, "
            "ctx_tags=%s, protocol=%s, attachments=%d, "
            "send_to_transcribe=%s, send_to_deepseek=%s",
            project, final_name, template, abbr, ordered_tags,
            len(prompt),
            result["prompt_edited"],
            result["generate_summary"],
            is_scrum,
            result["generate_deepseek_prompt"],
            result["include_name_in_prompt"],
            result["include_project_in_prompt"],
            result["include_comment_in_prompt"],
            result["include_tags_in_prompt"],
            os.path.basename(
                result["previous_protocol_path"]
            ) or "—",
            len(self._attachments),
            result["send_attachments_to_transcribe"],
            result["send_attachments_to_deepseek"],
        )
        self.accept()

    def _on_reject(self) -> None:
        log.info("Ввод метаданных отменён пользователем")
        self.result_data = {}
        self.reject()