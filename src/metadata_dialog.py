"""Диалоговое окно для ввода метаданных записи с библиотекой промптов.

Особенности:
  • Название записи вводится в отдельном QLineEdit.
  • Шаблон выбирается в QComboBox.
  • Промпт для DeepSeek можно формировать независимо от скрам-митинга.
  • Пользователь может включать в промпт контекст записи
    (название / проект / комментарий).
  • Отдельный флаг «Формировать summary» для конкретной записи —
    переопределяет глобальную настройку из SettingsWindow.
  • Флаг «Сформировать файл промпта для DeepSeek» включён по умолчанию
    для новых записей. При редактировании существующей записи
    сохраняется её историческое значение из session.json.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from .logger import get_logger
from .tooltips import attach_tooltip, make_info_icon, with_info

log = get_logger(__name__)


_NAME_PLACEHOLDERS = {
    "{name}", "{название}",
    "{abbr}", "{сокр}",
    "{date}", "{дата}",
    "{time}", "{время}",
    "{datetime}",
}


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
    Окно ввода метаданных: проект, название, комментарий, промпт, скрам,
    вложения, формирование промпта для DeepSeek, контекст в промпте,
    флаг формирования summary для конкретной записи.
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
        get_prompts: Optional[Callable[[], List[Dict[str, str]]]] = None,
        name_templates: Optional[List[Dict[str, str]]] = None,
        on_save_name_template: Optional[Callable[[str, str], None]] = None,
        get_name_templates: Optional[Callable[[], List[Dict[str, str]]]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(920)
        self.setMinimumHeight(950)

        self._projects = projects or []
        self._prompts = list(prompts or [])
        self._initial = initial or {}
        self._default_prompt = default_prompt or ""
        self._sessions_root = sessions_root
        self._on_save_prompt_cb = on_save_prompt
        self._get_prompts_cb = get_prompts

        self._name_templates: List[Dict[str, str]] = list(name_templates or [])
        self._on_save_name_template_cb = on_save_name_template
        self._get_name_templates_cb = get_name_templates

        self.result_data: Dict[str, Any] = {}
        self._prompt_edited = False
        self._attachments: List[str] = []

        log.debug(
            "MetadataDialog: title=%r, projects=%d, prompts=%d, "
            "name_templates=%d, sessions_root=%s",
            title, len(self._projects), len(self._prompts),
            len(self._name_templates), sessions_root or "—",
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

        info = QLabel(
            "Заполните информацию о записи. Название можно ввести вручную "
            "и применить к нему шаблон (например, добавить дату)."
        )
        info.setWordWrap(True)
        root.addWidget(info)

        # --- Основное ---
        form = QFormLayout()

        self.project_combo = QComboBox()
        self.project_combo.setEditable(True)
        self.project_combo.addItems(self._projects)
        self.project_combo.setCurrentIndex(-1)

        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("Например: Совещание по проекту X")

        self.name_combo = QComboBox()
        self.name_combo.setEditable(False)
        self.name_combo.setMinimumWidth(240)
        self._populate_name_templates()

        self.name_abbr_input = QLineEdit()
        self.name_abbr_input.setPlaceholderText("Сокр.")
        self.name_abbr_input.setMaximumWidth(120)

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

        self.name_preview_label = QLabel("")
        self.name_preview_label.setStyleSheet(
            "QLabel { color: #666; font-style: italic; }"
        )
        self.name_preview_label.setWordWrap(True)

        self.save_name_tpl_btn = QPushButton("Сохранить как шаблон…")
        self.save_name_tpl_btn.setToolTip(
            "Сохранить текущий текст как новый шаблон.\n"
            "Плейсхолдеры: {name} {abbr} {date} {time} {datetime}"
        )
        self.save_name_tpl_btn.clicked.connect(self._on_save_name_template)

        name_hint_row = QHBoxLayout()
        name_hint_row.addWidget(self.name_preview_label, 1)
        name_hint_row.addWidget(self.save_name_tpl_btn, 0)

        self.comment_input = QPlainTextEdit()
        self.comment_input.setPlaceholderText("Свободный комментарий…")
        self.comment_input.setFixedHeight(70)

        form.addRow("Проект:", with_info(self.project_combo, "meta_project"))
        form.addRow("Название:", name_row)
        form.addRow("", name_hint_row)
        form.addRow(
            "Комментарий:",
            with_info(self.comment_input, "meta_comment"),
        )

        root.addLayout(form)

        # --- Формирование summary ---
        summary_header = QHBoxLayout()
        summary_header.addWidget(QLabel("<b>Краткое содержание (summary)</b>"))
        summary_header.addStretch()
        summary_icon = make_info_icon("meta_generate_summary")
        if summary_icon is not None:
            summary_header.addWidget(summary_icon)
        root.addLayout(summary_header)

        self.generate_summary_check = QCheckBox(
            "Формировать summary для этой записи"
        )
        attach_tooltip(self.generate_summary_check, "meta_generate_summary")
        # По умолчанию — не формировать (соответствует DEFAULT_CONFIG).
        self.generate_summary_check.setChecked(False)
        root.addWidget(self.generate_summary_check)

        # --- Промпт ---
        prompt_header = QHBoxLayout()
        prompt_header.addWidget(QLabel("<b>Промпт для суммаризации / DeepSeek</b>"))
        prompt_header.addStretch()

        self.prompt_combo = QComboBox()
        self.prompt_combo.addItem("— не выбрано —", "")
        for p in self._prompts:
            self.prompt_combo.addItem(p["name"], p["text"])
        self.prompt_combo.setMinimumWidth(280)

        prompt_header.addWidget(QLabel("Библиотека:"))
        prompt_header.addWidget(self.prompt_combo)
        prompt_icon = make_info_icon("meta_prompt")
        if prompt_icon is not None:
            prompt_header.addWidget(prompt_icon)
        root.addLayout(prompt_header)

        self.prompt_input = QPlainTextEdit()
        self.prompt_input.setPlaceholderText(
            "Промпт для формирования краткого содержания и/или DeepSeek-промпта…"
        )
        self.prompt_input.setMinimumHeight(120)
        root.addWidget(self.prompt_input)

        prompt_actions = QHBoxLayout()
        self.save_to_library_btn = QPushButton("Сохранить как новый промпт…")
        self.save_to_library_btn.clicked.connect(self._on_save_to_library)
        self.reset_prompt_btn = QPushButton("Сбросить правку")
        self.reset_prompt_btn.clicked.connect(self._on_reset_prompt)
        prompt_actions.addWidget(self.save_to_library_btn)
        prompt_actions.addWidget(self.reset_prompt_btn)
        prompt_actions.addStretch()
        root.addLayout(prompt_actions)

        # --- Контекст записи в промпт ---
        ctx_header = QHBoxLayout()
        ctx_header.addWidget(QLabel("<b>Контекст записи в промпте</b>"))
        ctx_header.addStretch()
        ctx_header.addWidget(make_info_icon("meta_context_to_prompt"))
        root.addLayout(ctx_header)

        ctx_hint = QLabel(
            "Выберите, что из карточки записи добавить в промпт. "
            "Информация добавляется отдельным блоком «КОНТЕКСТ ЗАПИСИ» "
            "перед инструкцией. Это помогает ИИ корректнее писать "
            "результат (правильно называть встречу, учитывать проект, "
            "обращать внимание на комментарий)."
        )
        ctx_hint.setWordWrap(True)
        ctx_hint.setStyleSheet("QLabel { color: #666; }")
        root.addWidget(ctx_hint)

        ctx_box = QHBoxLayout()
        self.include_name_check = QCheckBox("Название записи")
        attach_tooltip(self.include_name_check, "meta_include_name_in_prompt")
        ctx_box.addWidget(self.include_name_check)

        self.include_project_check = QCheckBox("Проект")
        attach_tooltip(self.include_project_check, "meta_include_project_in_prompt")
        ctx_box.addWidget(self.include_project_check)

        self.include_comment_check = QCheckBox("Комментарий")
        attach_tooltip(self.include_comment_check, "meta_include_comment_in_prompt")
        ctx_box.addWidget(self.include_comment_check)

        ctx_box.addStretch()

        self.context_all_btn = QPushButton("Включить всё")
        self.context_all_btn.setToolTip(
            "Проставить все три галочки контекста"
        )
        self.context_all_btn.clicked.connect(self._on_context_enable_all)
        ctx_box.addWidget(self.context_all_btn)

        root.addLayout(ctx_box)

        # --- Промпт для DeepSeek ---
        root.addWidget(QLabel("<b>Промпт для DeepSeek</b>"))

        deepseek_box = QVBoxLayout()
        self.generate_deepseek_check = QCheckBox(
            "Сформировать файл промпта для DeepSeek "
            "(включено по умолчанию)"
        )
        attach_tooltip(self.generate_deepseek_check,
                       "meta_generate_deepseek_prompt")
        deepseek_box.addWidget(self.generate_deepseek_check)

        deepseek_hint = QLabel(
            "Шаблон промпта зависит от признака «Скрам-митинг»:\n"
            "  • <b>Скрам включён</b> → используется шаблон из "
            "Настройки → Скрам (плюс предыдущий протокол и стенограмма).\n"
            "  • <b>Скрам выключен</b> → используется промпт, "
            "введённый выше."
        )
        deepseek_hint.setWordWrap(True)
        deepseek_hint.setStyleSheet("QLabel { color: #666; }")
        deepseek_box.addWidget(deepseek_hint)

        root.addLayout(deepseek_box)

        # --- Скрам ---
        root.addWidget(QLabel("<b>Скрам-митинг</b>"))

        scrum_box = QFormLayout()
        self.is_scrum_check = QCheckBox(
            "Это скрам-митинг (использовать шаблон из Настройки → Скрам)"
        )
        attach_tooltip(self.is_scrum_check, "meta_is_scrum")
        scrum_box.addRow("", self.is_scrum_check)

        self.protocol_path_input = QLineEdit()
        self.protocol_path_input.setReadOnly(True)
        self.protocol_path_input.setPlaceholderText("Файл протокола не выбран")

        self.browse_protocol_btn = QPushButton("Загрузить файл…")
        self.browse_protocol_btn.clicked.connect(self._on_browse_protocol)

        self.from_session_btn = QPushButton("Из записи…")
        self.from_session_btn.setToolTip(
            "Выбрать предыдущий протокол из существующих сессий"
        )
        self.from_session_btn.clicked.connect(self._on_pick_from_session)

        self.clear_protocol_btn = QPushButton("Сбросить")
        self.clear_protocol_btn.clicked.connect(self._on_clear_protocol)

        protocol_row = QHBoxLayout()
        protocol_row.addWidget(self.protocol_path_input, 1)
        protocol_row.addWidget(self.browse_protocol_btn)
        protocol_row.addWidget(self.from_session_btn)
        protocol_row.addWidget(self.clear_protocol_btn)
        protocol_icon = make_info_icon("meta_protocol")
        if protocol_icon is not None:
            protocol_row.addWidget(protocol_icon)
        scrum_box.addRow("Предыдущий протокол:", protocol_row)

        root.addLayout(scrum_box)

        self._scrum_widgets = [
            self.protocol_path_input,
            self.browse_protocol_btn,
            self.from_session_btn,
            self.clear_protocol_btn,
        ]
        self.is_scrum_check.toggled.connect(self._update_scrum_visibility)
        self._update_scrum_visibility(False)

        # --- Вложения ---
        attach_label_row = QHBoxLayout()
        attach_label_row.addWidget(QLabel("<b>Вложения</b>"))
        attach_label_row.addStretch()
        attach_icon = make_info_icon("meta_attachments")
        if attach_icon is not None:
            attach_label_row.addWidget(attach_icon)
        root.addLayout(attach_label_row)

        attach_box = QVBoxLayout()

        info_attach = QLabel(
            "Файлы, которые можно передать в суммаризацию "
            "или включить в промпт DeepSeek. Текст будет автоматически "
            "извлечён из <code>.txt</code>, <code>.md</code>, "
            "<code>.docx</code>."
        )
        info_attach.setWordWrap(True)
        attach_box.addWidget(info_attach)

        self.attachments_list = QListWidget()
        self.attachments_list.setMinimumHeight(100)
        self.attachments_list.setSelectionMode(
            QListWidget.SelectionMode.ExtendedSelection
        )
        attach_box.addWidget(self.attachments_list)

        attach_btn_row = QHBoxLayout()
        self.add_attachment_btn = QPushButton("Добавить файлы…")
        self.add_attachment_btn.clicked.connect(self._on_add_attachment)
        self.remove_attachment_btn = QPushButton("Удалить выбранные")
        self.remove_attachment_btn.clicked.connect(self._on_remove_attachment)
        attach_btn_row.addWidget(self.add_attachment_btn)
        attach_btn_row.addWidget(self.remove_attachment_btn)
        attach_btn_row.addStretch()
        attach_box.addLayout(attach_btn_row)

        self.send_attachments_to_transcribe_check = QCheckBox(
            "Передать вложения в суммаризацию"
        )
        attach_tooltip(
            self.send_attachments_to_transcribe_check,
            "meta_send_to_transcribe",
        )

        self.send_attachments_to_deepseek_check = QCheckBox(
            "Передать вложения в промпт DeepSeek"
        )
        attach_tooltip(
            self.send_attachments_to_deepseek_check,
            "meta_send_to_deepseek",
        )

        attach_box.addWidget(self.send_attachments_to_transcribe_check)
        attach_box.addWidget(self.send_attachments_to_deepseek_check)

        root.addLayout(attach_box)

        # --- Нижние кнопки ---
        root.addStretch()
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Продолжить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Пропустить")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self._on_reject)
        root.addWidget(buttons)

    def _connect_signals(self) -> None:
        self.prompt_combo.currentIndexChanged.connect(self._on_prompt_selected)
        self.prompt_input.textChanged.connect(self._on_prompt_edited)

        self.name_input.textChanged.connect(self._refresh_name_preview)
        self.name_combo.currentIndexChanged.connect(self._on_name_template_changed)
        self.name_abbr_input.textChanged.connect(self._refresh_name_preview)

        self.is_scrum_check.toggled.connect(self._on_scrum_toggled)

    # ------------------------------------------------------------------
    # Контекст в промпте
    # ------------------------------------------------------------------
    def _on_context_enable_all(self) -> None:
        self.include_name_check.setChecked(True)
        self.include_project_check.setChecked(True)
        self.include_comment_check.setChecked(True)
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

    def _refresh_name_templates(self, selected_template: str = "") -> None:
        if self._get_name_templates_cb is not None:
            try:
                fresh = self._get_name_templates_cb() or []
                if isinstance(fresh, list):
                    self._name_templates = fresh
            except Exception as exc:
                log.warning("Не удалось получить шаблоны названий: %s", exc)

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
            final = raw_name or f"Запись {datetime.now():%Y-%m-%d %H-%M}"
            self.name_preview_label.setText(f"Итоговое имя: {final}")
            return

        final = format_name_template(template, name=raw_name, abbr=abbr)
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
                "Нечего сохранять: введите название или выберите шаблон.",
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
                log.exception("Ошибка сохранения шаблона имени: %s", exc)
                QMessageBox.critical(self, "Шаблон",
                                     f"Не удалось сохранить шаблон:\n{exc}")
                return

        self._refresh_name_templates(selected_template=candidate)
        QMessageBox.information(self, "Шаблон",
                                f"Шаблон «{label}» сохранён.")
        log.info("Сохранён шаблон названия: label=%r, template=%r",
                 label, candidate)

    # ------------------------------------------------------------------
    # Скрам
    # ------------------------------------------------------------------
    def _on_scrum_toggled(self, checked: bool) -> None:
        if checked and not self.generate_deepseek_check.isChecked():
            self.generate_deepseek_check.setChecked(True)
            log.info("Скрам включён — автоматически включено формирование "
                     "промпта для DeepSeek")

    def _update_scrum_visibility(self, checked: bool) -> None:
        for w in self._scrum_widgets:
            w.setEnabled(checked)
        log.debug("Видимость скрам-полей: %s", checked)

    def _on_browse_protocol(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Выберите файл предыдущего протокола",
            os.path.expanduser("~"),
            "Документы (*.docx *.txt *.md);;Все файлы (*)",
        )
        if path:
            self.protocol_path_input.setText(path)
            log.info("Выбран файл протокола: %s", path)

    def _on_clear_protocol(self) -> None:
        old = self.protocol_path_input.text()
        self.protocol_path_input.setText("")
        log.info("Файл протокола сброшен (был: %s)", old or "—")

    def _on_pick_from_session(self) -> None:
        if not self._sessions_root or not os.path.isdir(self._sessions_root):
            QMessageBox.warning(
                self, "Протокол",
                f"Папка сессий не найдена:\n{self._sessions_root or '(не задана)'}",
            )
            return

        candidates: List[Dict[str, str]] = []
        for name in sorted(os.listdir(self._sessions_root), reverse=True):
            session_dir = os.path.join(self._sessions_root, name)
            if not os.path.isdir(session_dir):
                continue
            meta = {}
            session_json = os.path.join(session_dir, "session.json")
            if os.path.exists(session_json):
                try:
                    with open(session_json, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                except Exception:
                    pass
            for fname in (
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
                "В папке сессий не найдено ни одного файла протокола.\n\n"
                "Поддерживаются: protocol.(docx|md|txt), "
                "deepseek_prompt.(docx|md|txt), video.txt.",
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

        if dlg.exec() == QDialog.DialogCode.Accepted and lst.currentItem():
            path = lst.currentItem().data(Qt.ItemDataRole.UserRole)
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
        log.debug("Список вложений обновлён: %d файлов", len(self._attachments))

    def _on_add_attachment(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Выберите файлы-вложения",
            os.path.expanduser("~"),
            "Документы (*.txt *.md *.docx *.pdf *.csv *.json);;Все файлы (*)",
        )
        if not files:
            return
        added = 0
        for f in files:
            if f and f not in self._attachments:
                self._attachments.append(f)
                added += 1
        if added:
            log.info("Добавлено вложений: %d (всего %d)",
                     added, len(self._attachments))
            self._refresh_attachments_list()

    def _on_remove_attachment(self) -> None:
        items = self.attachments_list.selectedItems()
        if not items:
            return
        to_remove = {it.data(Qt.ItemDataRole.UserRole) for it in items}
        self._attachments = [p for p in self._attachments if p not in to_remove]
        self._refresh_attachments_list()
        log.info("Удалено вложений: %d (осталось %d)",
                 len(to_remove), len(self._attachments))

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

        init_name = init.get("name", "") or init.get("description", "")
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

        self.comment_input.setPlainText(init.get("comment", ""))

        # --- Формирование summary ---
        # Если в initial задано явное значение — используем его.
        # Иначе — false (не формировать).
        self.generate_summary_check.blockSignals(True)
        self.generate_summary_check.setChecked(
            bool(init.get("generate_summary", False))
        )
        self.generate_summary_check.blockSignals(False)

        prompt_text = init.get("prompt", "") or self._default_prompt
        prompt_name = init.get("prompt_name", "")
        if prompt_name:
            idx = self.prompt_combo.findText(prompt_name)
            if idx >= 0:
                self.prompt_combo.blockSignals(True)
                self.prompt_combo.setCurrentIndex(idx)
                self.prompt_combo.blockSignals(False)

        self.prompt_input.blockSignals(True)
        self.prompt_input.setPlainText(prompt_text)
        self.prompt_input.blockSignals(False)
        self._prompt_edited = False

        # Скрам
        is_scrum = bool(init.get("is_scrum", False))
        self.is_scrum_check.blockSignals(True)
        self.is_scrum_check.setChecked(is_scrum)
        self.is_scrum_check.blockSignals(False)
        self._update_scrum_visibility(is_scrum)
        self.protocol_path_input.setText(init.get("previous_protocol_path", ""))

        # DeepSeek
        # По умолчанию включено: формировать файл промпта для DeepSeek
        # при каждой обработке. Пользователь может снять галочку вручную
        # в карточке записи, если для конкретной записи это не нужно.
        # При редактировании существующей записи сохраняется её
        # историческое значение из session.json.
        if "generate_deepseek_prompt" in init:
            gen = bool(init.get("generate_deepseek_prompt"))
        else:
            gen = True
        self.generate_deepseek_check.setChecked(gen)

        # Контекст в промпт
        self.include_name_check.setChecked(
            bool(init.get("include_name_in_prompt", False))
        )
        self.include_project_check.setChecked(
            bool(init.get("include_project_in_prompt", False))
        )
        self.include_comment_check.setChecked(
            bool(init.get("include_comment_in_prompt", False))
        )

        # Вложения
        self._attachments = list(init.get("attachments", []) or [])
        self._refresh_attachments_list()
        self.send_attachments_to_transcribe_check.setChecked(
            bool(init.get("send_attachments_to_transcribe", False))
        )
        self.send_attachments_to_deepseek_check.setChecked(
            bool(init.get("send_attachments_to_deepseek", False))
        )

        log.debug(
            "Начальные значения применены: project=%r, name=%r, "
            "template=%r, abbr=%r, generate_summary=%s, "
            "prompt=%d символов, is_scrum=%s, "
            "generate_deepseek=%s, ctx_name=%s, ctx_project=%s, "
            "ctx_comment=%s, attachments=%d",
            project, init_name, init_template, init_abbr,
            self.generate_summary_check.isChecked(),
            len(prompt_text), is_scrum, gen,
            self.include_name_check.isChecked(),
            self.include_project_check.isChecked(),
            self.include_comment_check.isChecked(),
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
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                log.debug("Замена промпта отменена пользователем")
                return

        self.prompt_input.blockSignals(True)
        self.prompt_input.setPlainText(text)
        self.prompt_input.blockSignals(False)
        self._prompt_edited = False
        log.debug("Выбран промпт из библиотеки: %s (%d символов)",
                  name, len(text))

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
                log.info("Промпт сброшен к значению из библиотеки")
                return
        if self._default_prompt:
            self.prompt_input.blockSignals(True)
            self.prompt_input.setPlainText(self._default_prompt)
            self.prompt_input.blockSignals(False)
            self._prompt_edited = False
            log.info("Промпт сброшен к значению по умолчанию")

    def _refresh_prompts_list(self, selected_name: str = "") -> None:
        try:
            if self._get_prompts_cb is not None:
                fresh = self._get_prompts_cb() or []
                if isinstance(fresh, list):
                    self._prompts = fresh
        except Exception as exc:
            log.warning("Не удалось получить список промптов: %s", exc)

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
        log.debug("Список промптов обновлён: %d записей", len(self._prompts))

    def _on_save_to_library(self) -> None:
        text = self.prompt_input.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "Промпт", "Текст промпта пустой")
            return

        name, ok = QInputDialog.getText(
            self, "Сохранить в библиотеку", "Название промпта:"
        )
        if not ok or not name.strip():
            return
        name = name.strip()

        log.info("Сохранение промпта в библиотеку: name=%r, %d символов",
                 name, len(text))

        saved = False
        if self._on_save_prompt_cb is not None:
            try:
                self._on_save_prompt_cb(name, text)
                saved = True
            except Exception as exc:
                log.exception("Ошибка сохранения промпта через колбэк: %s", exc)
                QMessageBox.critical(
                    self, "Промпт",
                    f"Не удалось сохранить промпт:\n{exc}",
                )
                return

        if not saved:
            self.result_data["new_prompt"] = {"name": name, "text": text}
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
        project = self.project_combo.currentText().strip() or "Default"

        template = self._current_template()
        raw_name = self.name_input.text().strip()
        abbr = self.name_abbr_input.text().strip()

        if template:
            final_name = format_name_template(template, name=raw_name, abbr=abbr)
        else:
            final_name = raw_name

        if not final_name:
            final_name = f"Запись {datetime.now():%Y-%m-%d %H-%M}"

        comment = self.comment_input.toPlainText().strip()
        prompt = self.prompt_input.toPlainText().strip()

        prompt_name = ""
        idx = self.prompt_combo.currentIndex()
        if idx >= 0:
            prompt_name = self.prompt_combo.itemText(idx)
            if prompt_name == "— не выбрано —":
                prompt_name = ""

        is_scrum = bool(self.is_scrum_check.isChecked())

        result: Dict[str, Any] = {
            "project": project,
            "name": final_name,
            "description": final_name,
            "name_template": template,
            "name_abbr": abbr,
            "comment": comment,
            "prompt": prompt,
            "prompt_name": prompt_name,
            "prompt_edited": bool(self._prompt_edited),
            # --- Формирование summary ---
            "generate_summary": bool(
                self.generate_summary_check.isChecked()
            ),
            # --- Скрам ---
            "is_scrum": is_scrum,
            "previous_protocol_path": self.protocol_path_input.text().strip(),
            # --- DeepSeek ---
            "generate_deepseek_prompt": bool(
                self.generate_deepseek_check.isChecked()
            ),
            # --- Контекст в промпте ---
            "include_name_in_prompt": bool(self.include_name_check.isChecked()),
            "include_project_in_prompt": bool(self.include_project_check.isChecked()),
            "include_comment_in_prompt": bool(self.include_comment_check.isChecked()),
            # --- Вложения ---
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
            "template=%r, abbr=%r, prompt=%d символов (изменён: %s), "
            "generate_summary=%s, "
            "is_scrum=%s, generate_deepseek=%s, "
            "ctx_name=%s, ctx_project=%s, ctx_comment=%s, "
            "protocol=%s, attachments=%d, "
            "send_to_transcribe=%s, send_to_deepseek=%s",
            project, final_name, template, abbr, len(prompt),
            result["prompt_edited"],
            result["generate_summary"],
            is_scrum,
            result["generate_deepseek_prompt"],
            result["include_name_in_prompt"],
            result["include_project_in_prompt"],
            result["include_comment_in_prompt"],
            os.path.basename(result["previous_protocol_path"]) or "—",
            len(self._attachments),
            result["send_attachments_to_transcribe"],
            result["send_attachments_to_deepseek"],
        )
        self.accept()

    def _on_reject(self) -> None:
        log.info("Ввод метаданных отменён пользователем")
        self.result_data = {}
        self.reject()