"""Встраиваемый виджет результата анализа поручений и TFS.

Используется в окне «Скрам» — показывается в нижней части
левого блока, после панели TFS.

Возможности:
  • Сворачивание/разворачивание заголовком.
  • Отображение результата в QTextBrowser с Markdown.
  • Кнопки «Скопировать», «Сохранить в DOCX…»,
    «Сохранить в Markdown…», «Повторить», «Очистить».
  • Автопрокрутка в начало при новом результате.
  • Сохранение состояния (свёрнуто/развёрнуто) в рамках сессии
    окна «Скрам».

Изменения:
  • Модуль создан.
  • НОВОЕ: уменьшены вертикальные отступы между элементами
    виджета — чтобы три блока (поручения / TFS / результат
    анализа) помещались в левой части окна «Скрам» без
    прокрутки. Минимальная высота QTextBrowser уменьшена
    до 80 px.
"""
from __future__ import annotations

import html
import os
from datetime import datetime
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QHBoxLayout, QLabel,
    QMessageBox, QPushButton, QSizePolicy, QTextBrowser,
    QVBoxLayout, QWidget,
)

from ..logger import get_logger
from ..markdown_docx import markdown_to_docx
from ..utils import safe_local_path

log = get_logger(__name__)


class AnalysisResultWidget(QWidget):
    """
    Встраиваемая панель результата анализа.

    Работает в двух состояниях:
      • пустое — показывается подсказка «результат появится здесь»;
      • заполненное — заголовок с названием промпта и кнопки +
        отрендеренный результат.

    Сворачивание — клик по заголовку. По умолчанию развёрнуто.
    """

    # Сигнал: пользователь запросил повторный запуск анализа.
    rerun_requested = Signal()

    # Сигнал: пользователь очистил результат.
    cleared = Signal()

    def __init__(
        self,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)

        self._result_text: str = ""
        self._prompt_name: str = ""
        self._expanded: bool = True

        self._build_ui()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        # Уменьшили вертикальные отступы.
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(0)

        # --- Заголовок-переключатель ---
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(4)

        self.header_btn = QPushButton()
        self.header_btn.setFlat(True)
        self.header_btn.setCursor(
            Qt.CursorShape.PointingHandCursor
        )
        self.header_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.header_btn.setStyleSheet(
            "QPushButton {"
            "  text-align: left;"
            "  border: none;"
            "  background: transparent;"
            "  color: #1565C0;"
            "  font-weight: bold;"
            "  font-size: 10pt;"
            "  padding: 1px 0;"
            "}"
            "QPushButton:hover { color: #0d47a1; }"
        )
        self.header_btn.clicked.connect(self._toggle_expanded)
        header_row.addWidget(self.header_btn, 1)

        layout.addLayout(header_row)

        # --- Тело (разворачиваемая часть) ---
        self.body = QWidget()
        body_layout = QVBoxLayout(self.body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(1)

        # --- Браузер с результатом ---
        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(True)
        self.browser.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        # Уменьшили min height: было 120.
        self.browser.setMinimumHeight(80)
        body_layout.addWidget(self.browser, 1)

        # --- Нижние кнопки ---
        btns_row = QHBoxLayout()
        btns_row.setContentsMargins(0, 0, 0, 0)
        btns_row.setSpacing(4)

        self.copy_btn = QPushButton("Скопировать")
        self.copy_btn.setToolTip(
            "Скопировать результат в буфер обмена."
        )
        self.copy_btn.clicked.connect(self._copy_result)
        btns_row.addWidget(self.copy_btn)

        self.save_docx_btn = QPushButton("В DOCX…")
        self.save_docx_btn.setToolTip(
            "Сохранить результат как .docx."
        )
        self.save_docx_btn.clicked.connect(self._save_as_docx)
        btns_row.addWidget(self.save_docx_btn)

        self.save_md_btn = QPushButton("В Markdown…")
        self.save_md_btn.setToolTip(
            "Сохранить результат как .md."
        )
        self.save_md_btn.clicked.connect(self._save_as_markdown)
        btns_row.addWidget(self.save_md_btn)

        btns_row.addStretch()

        self.rerun_btn = QPushButton("Повторить")
        self.rerun_btn.setToolTip(
            "Запустить анализ заново с тем же промптом."
        )
        self.rerun_btn.clicked.connect(self._on_rerun)
        btns_row.addWidget(self.rerun_btn)

        self.clear_btn = QPushButton("Очистить")
        self.clear_btn.setToolTip(
            "Убрать результат из панели."
        )
        self.clear_btn.clicked.connect(self._on_clear)
        btns_row.addWidget(self.clear_btn)

        body_layout.addLayout(btns_row)

        layout.addWidget(self.body, 1)

        # --- Компактные кнопки ---
        _compact_btn_qss = (
            "QPushButton {"
            "  padding: 2px 8px;"
            "  margin: 0;"
            "  font-size: 9pt;"
            "}"
        )
        for _b in (
            self.copy_btn, self.save_docx_btn, self.save_md_btn,
            self.rerun_btn, self.clear_btn,
        ):
            _b.setStyleSheet(_compact_btn_qss)
            _b.setFixedHeight(22)

        # Начальное состояние — пустое, показываем подсказку.
        self._render_empty_state()
        self._refresh_header()
        self._refresh_buttons_state()

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------
    def set_result(
        self,
        text: str,
        *,
        prompt_name: str = "",
    ) -> None:
        """
        Устанавливает результат анализа.

        Автоматически разворачивает панель и прокручивает
        содержимое в начало.
        """
        self._result_text = text or ""
        self._prompt_name = prompt_name or ""

        if not self._result_text.strip():
            self._render_empty_state(
                "LLM вернул пустой ответ."
            )
        else:
            # Пробуем отрендерить как Markdown.
            try:
                self.browser.setMarkdown(self._result_text)
            except Exception as exc:
                log.warning(
                    "Не удалось отрендерить результат как Markdown: %s",
                    exc,
                )
                self.browser.setPlainText(self._result_text)

            # Прокрутка в начало.
            try:
                cursor = self.browser.textCursor()
                cursor.movePosition(cursor.MoveOperation.Start)
                self.browser.setTextCursor(cursor)
            except Exception:
                pass

        # Автоматически разворачиваем и обновляем заголовок.
        self._expanded = True
        self._refresh_header()
        self._refresh_buttons_state()
        self._apply_expanded_state()

        log.info(
            "AnalysisResultWidget: установлен результат "
            "(промпт=%r, %d символов)",
            self._prompt_name, len(self._result_text),
        )

    def set_error(self, error: str) -> None:
        """Устанавливает состояние «ошибка»."""
        self._result_text = ""
        self._prompt_name = self._prompt_name or ""

        error_html = (
            "<div style='font-family:sans-serif; font-size:11pt;'>"
            "<p style='color:#c62828; font-weight:bold;'>"
            "Ошибка выполнения запроса:</p>"
            f"<pre style='white-space:pre-wrap; color:#c62828; "
            f"background:#fff5f5; border:1px solid #f5c6c6; "
            f"border-radius:4px; padding:8px;'>"
            f"{html.escape(error)}"
            "</pre>"
            "<p style='color:#666; font-size:10pt;'>"
            "Проверьте настройки LiteLLM "
            "(Настройки → Суммаризация) и повторите попытку."
            "</p>"
            "</div>"
        )
        self.browser.setHtml(error_html)
        self._expanded = True
        self._refresh_header()
        self._refresh_buttons_state()
        self._apply_expanded_state()

        log.error(
            "AnalysisResultWidget: установлена ошибка: %s", error
        )

    def clear_result(self) -> None:
        """Очищает результат и возвращает панель в пустое состояние."""
        self._result_text = ""
        self._prompt_name = ""
        self._render_empty_state()
        self._refresh_header()
        self._refresh_buttons_state()

    def has_result(self) -> bool:
        """Есть ли результат в панели."""
        return bool(self._result_text.strip())

    # ------------------------------------------------------------------
    # Внутренняя логика
    # ------------------------------------------------------------------
    def _render_empty_state(
        self, reason: str = "",
    ) -> None:
        """Показывает пустое состояние."""
        if reason:
            body = (
                f"<p style='color:#c62828; padding:12px; "
                f"font-size:11pt;'>{html.escape(reason)}</p>"
            )
        else:
            body = (
                "<p style='color:#888; padding:16px; "
                "font-size:11pt; font-style:italic;'>"
                "Результат анализа появится здесь.<br><br>"
                "Нажмите <b>«Анализ поручений и TFS…»</b>, "
                "выберите промпт из библиотеки — данные поручений "
                "и задач TFS уйдут в LLM."
                "</p>"
            )
        self.browser.setHtml(
            "<html><body style='background:transparent;'>"
            + body
            + "</body></html>"
        )

    def _refresh_header(self) -> None:
        """Обновляет текст заголовка-переключателя."""
        arrow = "▼" if self._expanded else "▶"

        if self.has_result():
            title = "Результат анализа"
            if self._prompt_name:
                title += f" — {self._prompt_name}"
            timestamp = datetime.now().strftime("%H:%M:%S")
            self.header_btn.setText(
                f"{arrow}  {title}  ({timestamp})"
            )
        elif self._result_text:
            self.header_btn.setText(
                f"{arrow}  Результат анализа"
            )
        else:
            self.header_btn.setText(
                f"{arrow}  Результат анализа (пусто)"
            )

        self.header_btn.setToolTip(
            "Клик — свернуть или развернуть панель результата."
        )

    def _refresh_buttons_state(self) -> None:
        """Включает/выключает кнопки в зависимости от наличия результата."""
        has = self.has_result()
        self.copy_btn.setEnabled(has)
        self.save_docx_btn.setEnabled(has)
        self.save_md_btn.setEnabled(has)
        self.clear_btn.setEnabled(has)
        # rerun_btn всегда активна — можно запустить анализ и
        # без предварительного результата.

    def _toggle_expanded(self) -> None:
        """Переключает состояние свёрнуто/развёрнуто."""
        self._expanded = not self._expanded
        self._refresh_header()
        self._apply_expanded_state()

    def _apply_expanded_state(self) -> None:
        """Применяет видимость тела в зависимости от состояния."""
        self.body.setVisible(self._expanded)

    # ------------------------------------------------------------------
    # Действия
    # ------------------------------------------------------------------
    def _on_rerun(self) -> None:
        """Запрос на повторный анализ."""
        self.rerun_requested.emit()

    def _on_clear(self) -> None:
        """Очистка результата."""
        self.clear_result()
        self.cleared.emit()

    def _copy_result(self) -> None:
        if not self._result_text:
            return
        try:
            QApplication.clipboard().setText(self._result_text)
            log.info(
                "AnalysisResultWidget: результат скопирован "
                "в буфер (%d символов)",
                len(self._result_text),
            )
        except Exception as exc:
            log.warning(
                "Не удалось скопировать результат: %s", exc
            )

    def _default_filename(self, ext: str) -> str:
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        base = "analysis"
        if self._prompt_name:
            safe = "".join(
                c if c.isalnum() or c in "-_ " else "_"
                for c in self._prompt_name
            ).strip().replace(" ", "_")
            if safe:
                base = safe
        return os.path.join(
            os.path.expanduser("~"),
            f"{base}_{stamp}.{ext}",
        )

    def _save_as_docx(self) -> None:
        if not self._result_text:
            return
        target, _ = QFileDialog.getSaveFileName(
            self,
            "Сохранить результат как DOCX",
            self._default_filename("docx"),
            "Документы Word (*.docx);;Все файлы (*)",
        )
        if not target:
            return
        target = safe_local_path(target)
        if not target.lower().endswith(".docx"):
            target += ".docx"

        try:
            markdown_to_docx(
                self._result_text,
                target,
                title=self._prompt_name or "Анализ",
            )
        except Exception as exc:
            log.exception(
                "Не удалось сохранить DOCX: %s", exc
            )
            QMessageBox.critical(
                self, "Сохранение",
                f"Не удалось сохранить файл:\n{exc}",
            )
            return

        QMessageBox.information(
            self, "Сохранение",
            f"Файл сохранён:\n{target}",
        )
        log.info(
            "AnalysisResultWidget: сохранён DOCX: %s", target
        )

    def _save_as_markdown(self) -> None:
        if not self._result_text:
            return
        target, _ = QFileDialog.getSaveFileName(
            self,
            "Сохранить результат как Markdown",
            self._default_filename("md"),
            "Markdown (*.md);;Все файлы (*)",
        )
        if not target:
            return
        target = safe_local_path(target)
        if not target.lower().endswith(".md"):
            target += ".md"

        try:
            with open(target, "w", encoding="utf-8") as f:
                f.write(self._result_text)
        except Exception as exc:
            log.exception(
                "Не удалось сохранить Markdown: %s", exc
            )
            QMessageBox.critical(
                self, "Сохранение",
                f"Не удалось сохранить файл:\n{exc}",
            )
            return

        QMessageBox.information(
            self, "Сохранение",
            f"Файл сохранён:\n{target}",
        )
        log.info(
            "AnalysisResultWidget: сохранён Markdown: %s", target
        )