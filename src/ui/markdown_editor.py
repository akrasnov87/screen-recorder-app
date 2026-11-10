"""Простой редактор Markdown с предпросмотром и экспортом в DOCX.

Поддерживаемые элементы (для панели инструментов):
    **жирный**      — **текст**
    *курсив*        — *текст*
    # Заголовок     — H1..H3
    - список        — маркированный
    1. список       — нумерованный
    > цитата        — цитата
    `код`           — инлайн-код
    ```блок```      — блок кода
    [текст](url)    — ссылка
    ---             — горизонтальная линия

Слева — исходный Markdown, справа — отрендеренный документ.
Кнопка «Сохранить в DOCX…» выгружает текущий текст в .docx
через markdown_docx.markdown_to_docx().

Редактор и предпросмотр синхронизируют прокрутку в обе стороны:
скролл слева ведёт за собой предпросмотр и наоборот. Позиция
привязывается по карте «строка Markdown → блок предпросмотра»,
которая пересобирается после каждого рендеринга (строки Markdown
и блоки рендера не совпадают 1:1 — абзац из N строк это один
блок, пустые строки исчезают).
"""
from __future__ import annotations

import os
import re
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QTextCursor, QTextDocument
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel,
    QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QTextBrowser,
    QVBoxLayout, QWidget,
)

from ..logger import get_logger
from ..markdown_docx import markdown_to_docx

log = get_logger(__name__)


# ----------------------------------------------------------------------
# Синхронизация прокрутки: строки Markdown ↔ блоки предпросмотра
# ----------------------------------------------------------------------
#
# Пропорциональный скролл (доля прокрутки слева → доля справа)
# не работает: абзац из N строк рендерится одним блоком,
# заголовок/пункт списка/код-блок — по одному блоку, пустые
# строки исчезают. Сдвиг растёт с длиной документа. Поэтому
# после каждого рендеринга строится карта «строка → блок»
# сопоставлением блоков по нормализованному тексту.

_SIG_LEN = 24     # длина сигнатуры блока для сопоставления
_LOOKAHEAD = 8    # сколько блоков вперёд искать совпадение

_MD_LINE_MARKER_RE = re.compile(
    r"^(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+|>\s*)"
)
_MD_INLINE_MARKER_RE = re.compile(r"[*_`]+")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_HEADING_RE = re.compile(r"^#{1,6}\s")
_MD_LIST_RE = re.compile(r"^(?:[-*+]|\d+[.)])\s")
_MD_HR_RE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})$")
_WS_RE = re.compile(r"\s+")


def _normalize_block_text(raw: str) -> str:
    """Нормализует текст для сопоставления блоков.

    Убирает инлайн-маркеры (жирный, курсив, код, ссылки),
    схлопывает пробелы, приводит к нижнему регистру.
    """
    text = raw.replace("\u2029", " ")
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _MD_INLINE_MARKER_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text.casefold()


def _md_block_signature(raw: str) -> str:
    """Сигнатура блока Markdown: нормализованный текст, усечённый."""
    lines = raw.split("\n")
    if lines:
        lines[0] = _MD_LINE_MARKER_RE.sub("", lines[0].strip())
    return _normalize_block_text("\n".join(lines))[:_SIG_LEN]


def _is_md_special(stripped: str) -> bool:
    """Является ли строка отдельным блоком (не частью абзаца)."""
    return (
        _MD_HEADING_RE.match(stripped) is not None
        or _MD_LIST_RE.match(stripped) is not None
        or stripped.startswith(">")
        or stripped.startswith("```")
        or _MD_HR_RE.match(stripped) is not None
    )


def _split_md_blocks(
    lines: list[str],
) -> list[tuple[int, int, str]]:
    """Разбивает Markdown на блоки в порядке рендеринга.

    Возвращает список (start_line, end_line, сигнатура):
    код-фенс, заголовок, пункт списка, цитата, hr — по одному
    блоку, абзац (подряд идущие обычные строки) — один блок.
    Пустые строки в результат не попадают.
    """
    blocks: list[tuple[int, int, str]] = []
    i, n = 0, len(lines)
    while i < n:
        stripped = lines[i].strip()
        if not stripped:
            i += 1
            continue
        if stripped.startswith("```"):
            j = i + 1
            while j < n and not lines[j].strip().startswith("```"):
                j += 1
            content = "\n".join(lines[i + 1:j])
            blocks.append((i, j, _md_block_signature(content)))
            i = j + 1
            continue
        if _is_md_special(stripped):
            blocks.append((i, i, _md_block_signature(stripped)))
            i += 1
            continue
        j = i
        while j < n and lines[j].strip() and not _is_md_special(
            lines[j].strip()
        ):
            j += 1
        blocks.append(
            (i, j - 1, _md_block_signature("\n".join(lines[i:j])))
        )
        i = j
    return blocks


def build_sync_maps(
    md_text: str, rendered_doc: QTextDocument
) -> tuple[list[int], list[int]]:
    """Строит карты «строка → блок предпросмотра» и обратную.

    Блоки Markdown сопоставляются с блоками отрендеренного
    документа жадным двустрелочным поиском по нормализованному
    тексту. При промахе (таблицы, вложенные списки и прочая
    экзотика) блоки потребляются по очереди, поэтому карта
    остаётся монотонной, а сдвиг — локальным.
    """
    lines = md_text.split("\n")
    n_lines = max(1, len(lines))
    line_to_block = [0] * n_lines

    # Блоки рендера материализуем один раз: и сопоставление идёт
    # по готовому списку, и нет O(n) обхода на каждый кандидат.
    rendered_texts: list[str] = []
    block = rendered_doc.begin()
    while block.isValid():
        rendered_texts.append(_normalize_block_text(block.text()))
        block = block.next()
    n_blocks = max(1, len(rendered_texts))

    r = 0
    for start, end, sig in _split_md_blocks(lines):
        target = None
        if len(sig) >= 4:
            for cand in range(r, min(r + _LOOKAHEAD, n_blocks)):
                if sig in rendered_texts[cand]:
                    target = cand
                    break
        if target is None:
            target = r if r < n_blocks else n_blocks - 1
        for line in range(start, end + 1):
            line_to_block[line] = target
        r = target + 1

    # Пустые строки, не покрытые блоками, наследуют ближайший
    # предшествующий блок (самая первая строка остаётся блоком 0).
    for i in range(1, n_lines):
        if line_to_block[i] == 0 and line_to_block[i - 1] != 0:
            line_to_block[i] = line_to_block[i - 1]

    block_to_line = [n_lines - 1] * n_blocks
    seen = [False] * n_blocks
    for line, b in enumerate(line_to_block):
        if not seen[b]:
            block_to_line[b] = line
            seen[b] = True

    # Qt иногда рендерит пустые артефактные блоки (например, после
    # код-фенса), которым не соответствует ни одна строка Markdown.
    # Чтобы обратная синхронизация не прыгала в конец документа,
    # такие блоки наследуют строку ближайшего предшествующего
    # покрытого блока.
    last = 0
    for b in range(n_blocks):
        if seen[b]:
            last = block_to_line[b]
        else:
            block_to_line[b] = last

    return line_to_block, block_to_line


class _ScrollSyncMixin:
    """Двунаправленная синхронизация прокрутки Markdown ↔ предпросмотр.

    Позиция определяется верхней видимой строкой/блоком и картой
    «строка → блок», построенной при рендеринге. Прокрутка — только
    через verticalScrollBar().setValue(): текстовый курсор редактора
    не трогаем (setTextCursor сдвинул бы позицию ввода).
    """

    def _setup_scroll_sync(
        self, editor: QPlainTextEdit, preview: QTextBrowser
    ) -> None:
        self._sync_editor = editor
        self._sync_preview = preview
        self._line_to_block: list[int] = []
        self._block_to_line: list[int] = []
        # Антизацикловка: Qt-сигналы синхронные, поэтому вся цепочка
        # setValue → valueChanged → setValue проходит под одним флагом.
        self._syncing = False
        editor.verticalScrollBar().valueChanged.connect(
            self._on_editor_scrolled
        )
        preview.verticalScrollBar().valueChanged.connect(
            self._on_preview_scrolled
        )

    def _rebuild_sync_map(self, md_text: str) -> None:
        self._line_to_block, self._block_to_line = build_sync_maps(
            md_text, self._sync_preview.document()
        )

    def _on_editor_scrolled(self) -> None:
        if self._syncing:
            return
        self._syncing = True
        try:
            self._sync_editor_to_preview()
        finally:
            self._syncing = False

    def _on_preview_scrolled(self) -> None:
        if self._syncing:
            return
        self._syncing = True
        try:
            self._sync_preview_to_editor()
        finally:
            self._syncing = False

    def _sync_editor_to_preview(self) -> None:
        if not self._line_to_block:
            return
        line = self._first_visible_block_number(self._sync_editor)
        if line < len(self._line_to_block):
            self._scroll_view_to_block(
                self._sync_preview, self._line_to_block[line]
            )

    def _sync_preview_to_editor(self) -> None:
        if not self._block_to_line:
            return
        block = self._first_visible_block_number(self._sync_preview)
        if block < len(self._block_to_line):
            self._scroll_view_to_block(
                self._sync_editor, self._block_to_line[block]
            )

    @staticmethod
    def _first_visible_block_number(view) -> int:
        """Номер верхнего видимого блока виджета.

        QPlainTextEdit даёт firstVisibleBlock() за O(1). У
        QTextBrowser этого метода нет (PySide6 его не экспортирует)
        — ищем бинарным поиском: cursorRect возвращает
        viewport-координаты, у верхнего видимого блока верхняя
        граница <= 0, у последующих — > 0.
        """
        first_visible = getattr(view, "firstVisibleBlock", None)
        if first_visible is not None:
            return first_visible().blockNumber()
        doc = view.document()
        n = doc.blockCount()
        if n <= 1:
            return 0
        lo, hi, result = 0, n - 1, 0
        while lo <= hi:
            mid = (lo + hi) // 2
            cursor = QTextCursor(doc.findBlockByNumber(mid))
            if view.cursorRect(cursor).top() <= 0:
                result = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return result

    @staticmethod
    def _scroll_view_to_block(view, block: int) -> None:
        """Прокручивает виджет так, чтобы блок оказался наверху."""
        doc = view.document()
        if block < 0 or block >= doc.blockCount():
            return
        cursor = QTextCursor(doc.findBlockByNumber(block))
        rect = view.cursorRect(cursor)
        if rect.isNull():
            return
        # cursorRect отдаёт viewport-координаты, поэтому
        # документную y считаем как y + текущая прокрутка.
        y = rect.top() + view.verticalScrollBar().value()
        view.verticalScrollBar().setValue(int(y))


class MarkdownEditorDialog(QDialog, _ScrollSyncMixin):
    """
    Модальный редактор Markdown.

    Слева — QPlainTextEdit (исходник Markdown),
    справа — QTextBrowser (отрендеренный HTML).
    Сверху — панель инструментов для вставки разметки.
    """

    def __init__(
        self,
        text: str = "",
        title: str = "Редактор Markdown",
        parent: Optional[QWidget] = None,
        default_docx_path: str = "",
        threshold_chars: int = 0,
        threshold_label: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumSize(1100, 720)

        self._result_text: str = text or ""
        # Путь по умолчанию для экспорта в .docx — например,
        # <session_dir>/manual_protocol.docx.
        self._default_docx_path: str = default_docx_path or ""

        # Порог длины текста, после которого в Bitrix24 текст
        # уходит файлом, а не сообщением. 0 — предупреждение
        # не показывается.
        self._threshold_chars: int = max(0, int(threshold_chars or 0))
        self._threshold_label: str = threshold_label or ""

        self._build_ui()
        self._apply_initial(text)
        self._refresh_preview()
        self._refresh_counter()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        info = QLabel(
            "Введите текст в формате Markdown слева — справа появится "
            "готовый документ. Панель инструментов вставляет разметку "
            "в текущую позицию курсора."
        )
        info.setWordWrap(True)
        root.addWidget(info)

        # --- Панель инструментов ---
        toolbar = QHBoxLayout()

        def add_btn(label: str, tip: str, handler) -> QPushButton:
            b = QPushButton(label)
            b.setToolTip(tip)
            b.setFixedHeight(28)
            b.clicked.connect(handler)
            toolbar.addWidget(b)
            return b

        add_btn("B", "Жирный  **текст**",
                lambda: self._wrap_selection("**", "**"))
        add_btn("I", "Курсив  *текст*",
                lambda: self._wrap_selection("*", "*"))
        add_btn("`код`", "Инлайн-код  `текст`",
                lambda: self._wrap_selection("`", "`"))
        toolbar.addSpacing(12)
        add_btn("H1", "Заголовок 1  # текст",
                lambda: self._prefix_lines("# "))
        add_btn("H2", "Заголовок 2  ## текст",
                lambda: self._prefix_lines("## "))
        add_btn("H3", "Заголовок 3  ### текст",
                lambda: self._prefix_lines("### "))
        toolbar.addSpacing(12)
        add_btn("• Список", "Маркированный список  - текст",
                lambda: self._prefix_lines("- "))
        add_btn("1. Список", "Нумерованный список  1. текст",
                lambda: self._prefix_lines("1. "))
        add_btn("> Цитата", "Цитата  > текст",
                lambda: self._prefix_lines("> "))
        toolbar.addSpacing(12)
        add_btn("Код-блок", "Блок кода  ```…```",
                self._insert_code_block)
        add_btn("Ссылка", "Ссылка  [текст](url)",
                self._insert_link)
        add_btn("Разделитель", "Горизонтальная линия  ---",
                self._insert_hr)

        toolbar.addSpacing(12)
        add_btn(
            "Сохранить в DOCX…",
            "Экспортировать текущий Markdown в документ .docx",
            self._export_docx,
        )

        toolbar.addStretch()
        root.addLayout(toolbar)

        # --- Сплиттер ---
        splitter = QSplitter(Qt.Orientation.Horizontal)

        self.editor = QPlainTextEdit()
        mono = QFont("Monospace")
        mono.setStyleHint(QFont.StyleHint.TypeWriter)
        mono.setPointSize(11)
        self.editor.setFont(mono)
        self.editor.setPlaceholderText(
            "# Заголовок протокола\n\n"
            "## Дата и участники\n\n"
            "- Иванов И.И.\n"
            "- Петров П.П.\n\n"
            "## Обсуждение\n\n"
            "> Краткая цитата из обсуждения\n\n"
            "## Задачи\n\n"
            "1. Подготовить макет — до 30.09\n"
            "2. Согласовать с заказчиком — до 05.10\n"
        )
        splitter.addWidget(self.editor)

        self.preview = QTextBrowser()
        self.preview.setOpenExternalLinks(True)
        splitter.addWidget(self.preview)

        splitter.setSizes([560, 540])
        root.addWidget(splitter, 1)

        # Синхронизация прокрутки в обе стороны. Карта
        # «строка → блок» строится в _refresh_preview после
        # каждого рендеринга предпросмотра.
        self._setup_scroll_sync(self.editor, self.preview)

        # --- Счётчик символов + предупреждение о длине ---
        root.addWidget(self._build_counter_bar())

        # --- Кнопки ---
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self._on_reject)
        root.addWidget(buttons)

    def _build_counter_bar(self) -> QWidget:
        """
        Нижняя панель: счётчик символов и предупреждение,
        если текст превышает порог «текст → файл» для Bitrix24.
        """
        box = QWidget()
        layout = QHBoxLayout(box)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(12)

        self.counter_label = QLabel("")
        self.counter_label.setStyleSheet(
            "QLabel { color: #666; }"
        )
        layout.addWidget(self.counter_label)

        self.limit_label = QLabel("")
        self.limit_label.setStyleSheet(
            "QLabel { color: #666; }"
        )
        layout.addWidget(self.limit_label)

        layout.addStretch()

        self.warning_label = QLabel("")
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet(
            "QLabel {"
            "  color: #B8860B;"
            "  background-color: #FFF8E1;"
            "  border: 1px solid #FFE082;"
            "  border-radius: 4px;"
            "  padding: 4px 8px;"
            "}"
        )
        self.warning_label.setVisible(False)
        layout.addWidget(self.warning_label, 1)

        return box    

    def _apply_initial(self, text: str) -> None:
        self.editor.blockSignals(True)
        self.editor.setPlainText(text or "")
        self.editor.blockSignals(False)
        self.editor.textChanged.connect(self._refresh_preview)
        self.editor.textChanged.connect(self._refresh_counter)

    # ------------------------------------------------------------------
    # Вставка разметки
    # ------------------------------------------------------------------
    def _wrap_selection(self, prefix: str, suffix: str) -> None:
        cursor = self.editor.textCursor()
        selected = cursor.selectedText()
        cursor.insertText(f"{prefix}{selected}{suffix}")
        if not selected:
            pos = cursor.position() - len(suffix)
            cursor.setPosition(pos)
            self.editor.setTextCursor(cursor)
        self.editor.setFocus()

    def _prefix_lines(self, prefix: str) -> None:
        """Добавляет префикс к каждой выделенной строке (или к текущей)."""
        cursor = self.editor.textCursor()
        if not cursor.hasSelection():
            cursor.select(QTextCursor.SelectionType.LineUnderCursor)

        start = cursor.selectionStart()
        end = cursor.selectionEnd()

        cursor.setPosition(start)
        cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
        cursor.beginEditBlock()

        while True:
            cursor.insertText(prefix)
            end += len(prefix)
            if cursor.position() >= end:
                break
            if not cursor.movePosition(QTextCursor.MoveOperation.NextBlock):
                break

        cursor.endEditBlock()
        self.editor.setFocus()

    def _insert_code_block(self) -> None:
        cursor = self.editor.textCursor()
        selected = cursor.selectedText().replace("\u2029", "\n")
        if selected:
            cursor.insertText(f"```\n{selected}\n```")
        else:
            cursor.insertText("```\nкод\n```")
            pos = cursor.position() - 5
            cursor.setPosition(pos)
            self.editor.setTextCursor(cursor)
        self.editor.setFocus()

    def _insert_link(self) -> None:
        cursor = self.editor.textCursor()
        selected = cursor.selectedText()
        if selected:
            cursor.insertText(f"[{selected}](https://example.com)")
        else:
            cursor.insertText("[текст ссылки](https://example.com)")
        self.editor.setFocus()

    def _insert_hr(self) -> None:
        cursor = self.editor.textCursor()
        cursor.insertText("\n---\n")
        self.editor.setFocus()

    # ------------------------------------------------------------------
    # Предпросмотр
    # ------------------------------------------------------------------
    def _refresh_preview(self) -> None:
        text = self.editor.toPlainText()
        # Флаг _syncing на всё время замены документа: setMarkdown
        # сбрасывает скролл предпросмотра на 0, и valueChanged(0)
        # должен проигнорировать обратную синхронизацию — иначе
        # редактор уехал бы наверх при каждом нажатии клавиши.
        self._syncing = True
        try:
            try:
                # Qt умеет рендерить Markdown «из коробки».
                self.preview.setMarkdown(text)
            except Exception as exc:
                log.exception("Ошибка предпросмотра Markdown: %s", exc)
                self.preview.setPlainText(f"Ошибка предпросмотра: {exc}")
            self._rebuild_sync_map(text)
            self._sync_editor_to_preview()
        finally:
            self._syncing = False

    def _refresh_counter(self) -> None:
        """
        Обновляет счётчик символов и показывает предупреждение,
        если текст превышает порог «текст → файл» Bitrix24.
        """
        text = self.editor.toPlainText()
        chars = len(text)
        lines = text.count("\n") + 1 if text else 0
        size_bytes = len(text.encode("utf-8"))

        self.counter_label.setText(
            f"Символов: {chars} · строк: {lines} · "
            f"UTF-8: {size_bytes / 1024:.1f} КБ"
        )

        if self._threshold_chars > 0:
            self.limit_label.setText(
                f"Порог «текст → файл»: {self._threshold_chars} символов"
            )
        else:
            self.limit_label.setText("")

        # --- Предупреждение о превышении ---
        if self._threshold_chars <= 0:
            self.warning_label.setVisible(False)
            self.warning_label.setText("")
            return

        if chars <= self._threshold_chars:
            self.warning_label.setVisible(False)
            self.warning_label.setText("")
            return

        # Предупреждение активно.
        excess = chars - self._threshold_chars
        if self._threshold_label:
            context = f" в {self._threshold_label}"
        else:
            context = ""

        self.warning_label.setText(
            f"⚠ Текст ({chars} символов) превышает порог "
            f"«текст → файл»{context} на {excess} символов.\n"
            f"При отправке в Bitrix24 сообщение может уйти "
            f"файлом, а не текстом. Уменьшите объём текста "
            f"или проверьте настройку «Порог „текст → файл“» "
            f"в Настройках → Bitrix24."
        )
        self.warning_label.setVisible(True)

    # ------------------------------------------------------------------
    # Экспорт в DOCX
    # ------------------------------------------------------------------
    def _export_docx(self) -> None:
        """Экспортирует текущий текст в .docx через markdown_to_docx()."""
        text = self.editor.toPlainText()
        if not text.strip():
            QMessageBox.warning(
                self, "Экспорт в DOCX",
                "Документ пустой — нечего экспортировать.",
            )
            return

        # Куда сохранять: либо подсказанный путь (рядом с сессией),
        # либо домашняя папка.
        if self._default_docx_path:
            default_path = self._default_docx_path
        else:
            base = os.path.expanduser("~")
            default_path = os.path.join(base, "protocol.docx")

        target, _ = QFileDialog.getSaveFileName(
            self,
            "Сохранить протокол как DOCX",
            default_path,
            "Документы Word (*.docx);;Все файлы (*)",
        )
        if not target:
            return

        if not target.lower().endswith(".docx"):
            target += ".docx"

        # Название документа — берём из первой строки H1, если она есть.
        title = ""
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("# "):
                title = line[2:].strip()
                break

        try:
            markdown_to_docx(text, target, title=title)
        except Exception as exc:
            log.exception("Ошибка экспорта в DOCX: %s", exc)
            QMessageBox.critical(
                self, "Экспорт в DOCX",
                f"Не удалось сохранить файл:\n{exc}",
            )
            return

        QMessageBox.information(
            self, "Экспорт в DOCX",
            f"Документ сохранён:\n{target}",
        )
        log.info("Markdown-редактор: экспорт в DOCX — %s", target)

    # ------------------------------------------------------------------
    # Кнопки
    # ------------------------------------------------------------------
    def _on_accept(self) -> None:
        self._result_text = self.editor.toPlainText()
        log.info("Markdown-редактор: сохранено (%d символов)",
                 len(self._result_text))
        self.accept()

    def _on_reject(self) -> None:
        log.info("Markdown-редактор: отменено")
        self.reject()

    def result_text(self) -> str:
        return self._result_text

class MarkdownViewerDialog(QDialog, _ScrollSyncMixin):
    """
    Модальное окно для просмотра Markdown без редактирования.

    Слева — readonly QPlainTextEdit с исходником,
    справа — отрендеренный QTextBrowser.
    Только кнопка «Закрыть».
    """

    def __init__(
        self,
        text: str = "",
        title: str = "Просмотр Markdown",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumSize(1000, 640)

        root = QVBoxLayout(self)

        info = QLabel(
            "Просмотр сохранённого текста. Чтобы изменить — закройте "
            "окно и используйте «Изменить…»."
        )
        info.setWordWrap(True)
        root.addWidget(info)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        self.source = QPlainTextEdit()
        mono = QFont("Monospace")
        mono.setStyleHint(QFont.StyleHint.TypeWriter)
        mono.setPointSize(11)
        self.source.setFont(mono)
        self.source.setReadOnly(True)
        self.source.setPlainText(text or "")
        splitter.addWidget(self.source)

        self.preview = QTextBrowser()
        self.preview.setOpenExternalLinks(True)
        try:
            self.preview.setMarkdown(text or "")
        except Exception as exc:
            log.exception("Ошибка рендера Markdown: %s", exc)
            self.preview.setPlainText(str(exc))
        splitter.addWidget(self.preview)

        # Синхронизация прокрутки в обе стороны. Оба виджета
        # read-only, текст не меняется — карта строится один раз.
        self._setup_scroll_sync(self.source, self.preview)
        self._rebuild_sync_map(text or "")

        splitter.setSizes([500, 500])
        root.addWidget(splitter, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Close,
            parent=self,
        )
        buttons.button(
            QDialogButtonBox.StandardButton.Close
        ).setText("Закрыть")
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        root.addWidget(buttons)