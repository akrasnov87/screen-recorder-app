"""Простой редактор и просмотрщик BB-кода.

Поддерживаемые теги:
    [b]...[/b]           — жирный
    [i]...[/i]           — курсив
    [u]...[/u]           — подчёркивание
    [s]...[/s]           — зачёркивание
    [url=ссылка]...[/url] — ссылка
    [url]ссылка[/url]    — ссылка (автотекст)
    [img]ссылка[/img]    — изображение
    [quote]...[/quote]   — цитата
    [code]...[/code]     — блок кода
    [spoiler]...[/spoiler] — спойлер
"""
from __future__ import annotations

import html
import re
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPlainTextEdit,
    QPushButton, QSplitter, QTextBrowser, QVBoxLayout, QWidget,
)

from .logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# BB → HTML
# ---------------------------------------------------------------------------
def bbcode_to_html(text: str) -> str:
    """Простейший конвертер BB-кода в HTML.

    Никакой санитайзации не делает — текст считается доверенным.
    """
    if not text:
        return ""

    s = html.escape(text)

    pairs = [
        (r"\[b\](.*?)\[/b\]", r"<b>\1</b>"),
        (r"\[i\](.*?)\[/i\]", r"<i>\1</i>"),
        (r"\[u\](.*?)\[/u\]", r"<u>\1</u>"),
        (r"\[s\](.*?)\[/s\]", r"<s>\1</s>"),
    ]
    for pattern, repl in pairs:
        s = re.sub(pattern, repl, s, flags=re.DOTALL | re.IGNORECASE)

    s = re.sub(
        r"\[quote\](.*?)\[/quote\]",
        r'<blockquote style="border-left:3px solid #888; '
        r'margin:6px 0; padding:4px 10px; color:#555;">\1</blockquote>',
        s,
        flags=re.DOTALL | re.IGNORECASE,
    )

    s = re.sub(
        r"\[code\](.*?)\[/code\]",
        r'<pre style="background:#f4f4f4; padding:6px; '
        r'font-family:monospace; border-radius:4px;">\1</pre>',
        s,
        flags=re.DOTALL | re.IGNORECASE,
    )

    s = re.sub(
        r"\[spoiler\](.*?)\[/spoiler\]",
        r'<details style="margin:6px 0;">'
        r'<summary style="cursor:pointer; color:#4a90d9;">'
        r'Показать спойлер</summary>\1</details>',
        s,
        flags=re.DOTALL | re.IGNORECASE,
    )

    s = re.sub(
        r"\[url=([^\]]+)\](.*?)\[/url\]",
        r'<a href="\1">\2</a>',
        s,
        flags=re.DOTALL | re.IGNORECASE,
    )

    s = re.sub(
        r"\[url\](.*?)\[/url\]",
        r'<a href="\1">\1</a>',
        s,
        flags=re.DOTALL | re.IGNORECASE,
    )

    s = re.sub(
        r"\[img\](.*?)\[/img\]",
        r'<img src="\1" style="max-width:100%;" />',
        s,
        flags=re.DOTALL | re.IGNORECASE,
    )

    s = s.replace("\n", "<br>")
    return f"<div style='font-family:sans-serif; font-size:12pt;'>{s}</div>"


# ---------------------------------------------------------------------------
# BB → plain text
# ---------------------------------------------------------------------------
def bbcode_to_plain(text: str) -> str:
    """
    Убирает BB-разметку и оставляет только чистый текст.

    Используется для передачи summary в суммаризатор: большинство
    моделей не понимают BB-код и воспримут его как мусор.
    """
    if not text:
        return ""

    s = text

    # Простые парные теги
    for tag in ("b", "i", "u", "s", "quote", "code", "spoiler"):
        s = re.sub(
            rf"\[{tag}\](.*?)\[/{tag}\]",
            r"\1",
            s,
            flags=re.DOTALL | re.IGNORECASE,
        )

    # URL с текстом — оставляем текст + URL в скобках
    s = re.sub(
        r"\[url=([^\]]+)\](.*?)\[/url\]",
        r"\2 (\1)",
        s,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # URL без параметра — оставляем сам URL
    s = re.sub(
        r"\[url\](.*?)\[/url\]",
        r"\1",
        s,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # Картинки — просто URL
    s = re.sub(
        r"\[img\](.*?)\[/img\]",
        r"[изображение: \1]",
        s,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # Убираем любые оставшиеся теги вида [xxx]
    s = re.sub(r"\[/?[a-zA-Z][^\]]*\]", "", s)

    return s.strip()


# ---------------------------------------------------------------------------
# BB → BB Bitrix24
# ---------------------------------------------------------------------------
def bbcode_to_bitrix(text: str) -> str:
    """
    Преобразует внутренний BB-код в BB-код Bitrix24.

    Bitrix24 рендерит BB-теги в верхнем регистре ([B], [I], [URL], ...).
    Внутренний формат использует нижний регистр ([b], [i], [url], ...).

    Особенности:
      • Простые теги (b, i, u, s, url, img, code, list, table, ...)
        приводятся к верхнему регистру — Bitrix24 отрендерит их.
      • [quote] — нет прямого аналога в Bitrix24. Заменяется на "> ".
      • [spoiler] — Bitrix24 поддерживает, оставляем как есть.
      • Прочие теги игнорируются (остаются как текст).

    Результат можно передавать в Bitrix24 через API im.message.add —
    портал отрендерит его как форматированный текст.
    """
    if not text:
        return ""

    s = text

    # --- Простые парные теги: нижний → верхний регистр ---
    simple_tags = (
        "b", "i", "u", "s",
        "url", "img",
        "code",
        "list", "table", "tr", "td",
        "color", "size", "font",
        "left", "center", "right", "justify",
    )
    for tag in simple_tags:
        # Открывающие: [tag] и [tag=...]
        s = re.sub(
            rf"\[{tag}(\]|=)",
            lambda m, t=tag: f"[{t.upper()}{m.group(1)}",
            s,
            flags=re.IGNORECASE,
        )
        # Закрывающие: [/tag]
        s = re.sub(
            rf"\[/{tag}\]",
            f"[/{tag.upper()}]",
            s,
            flags=re.IGNORECASE,
        )

    # --- Spoiler: Bitrix24 поддерживает ---
    s = re.sub(r"\[spoiler\]", "[SPOILER]", s, flags=re.IGNORECASE)
    s = re.sub(r"\[/spoiler\]", "[/SPOILER]", s, flags=re.IGNORECASE)

    # --- Quote: нет прямого аналога, обозначаем как цитату ---
    s = re.sub(r"\[quote\]", "\n> ", s, flags=re.IGNORECASE)
    s = re.sub(r"\[/quote\]", "\n", s, flags=re.IGNORECASE)

    # --- Нормализуем переводы строк ---
    s = re.sub(r"\n{3,}", "\n\n", s)

    return s.strip()


# ---------------------------------------------------------------------------
# Редактор
# ---------------------------------------------------------------------------
class BBCodeEditorDialog(QDialog):
    """
    Модальный редактор BB-кода.

    Слева — plain-text ввод, справа — предпросмотр.
    Сверху — панель инструментов для вставки тегов.
    """

    def __init__(
        self,
        text: str = "",
        title: str = "Редактор BB-кода",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumSize(960, 640)

        self._result_text: str = text or ""

        self._build_ui()
        self._apply_initial(text)
        self._refresh_preview()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        info = QLabel(
            "Используйте панель инструментов или вводите BB-теги вручную. "
            "Предпросмотр обновляется автоматически."
        )
        info.setWordWrap(True)
        root.addWidget(info)

        toolbar = QHBoxLayout()

        def add_btn(label: str, tip: str, handler) -> QPushButton:
            b = QPushButton(label)
            b.setToolTip(tip)
            b.setFixedHeight(28)
            b.clicked.connect(handler)
            toolbar.addWidget(b)
            return b

        add_btn("B", "Жирный  [b]…[/b]",
                lambda: self._wrap_selection("[b]", "[/b]"))
        add_btn("I", "Курсив  [i]…[/i]",
                lambda: self._wrap_selection("[i]", "[/i]"))
        add_btn("U", "Подчёркнутый  [u]…[/u]",
                lambda: self._wrap_selection("[u]", "[/u]"))
        add_btn("S", "Зачёркнутый  [s]…[/s]",
                lambda: self._wrap_selection("[s]", "[/s]"))
        toolbar.addSpacing(12)
        add_btn("Цитата", "Цитата  [quote]…[/quote]",
                lambda: self._wrap_selection("[quote]", "[/quote]"))
        add_btn("Код", "Блок кода  [code]…[/code]",
                lambda: self._wrap_selection("[code]", "[/code]"))
        add_btn("Спойлер", "Спойлер  [spoiler]…[/spoiler]",
                lambda: self._wrap_selection("[spoiler]", "[/spoiler]"))
        toolbar.addSpacing(12)
        add_btn("Ссылка", "Ссылка  [url=адрес]текст[/url]",
                self._insert_url)
        add_btn("Картинка", "Картинка  [img]адрес[/img]",
                self._insert_image)
        toolbar.addStretch()
        root.addLayout(toolbar)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText(
            "Введите текст с BB-кодом…\n\n"
            "Например:\n"
            "[b]Краткое резюме[/b]\n\n"
            "[quote]обсудили миграцию на новый стек[/quote]"
        )
        splitter.addWidget(self.editor)

        self.preview = QTextBrowser()
        self.preview.setOpenExternalLinks(True)
        splitter.addWidget(self.preview)

        splitter.setSizes([480, 480])
        root.addWidget(splitter, 1)

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

    def _apply_initial(self, text: str) -> None:
        self.editor.blockSignals(True)
        self.editor.setPlainText(text or "")
        self.editor.blockSignals(False)
        self.editor.textChanged.connect(self._refresh_preview)

    def _wrap_selection(self, prefix: str, suffix: str) -> None:
        cursor = self.editor.textCursor()
        selected = cursor.selectedText()
        cursor.insertText(f"{prefix}{selected}{suffix}")
        if not selected:
            pos = cursor.position() - len(suffix)
            cursor.setPosition(pos)
            self.editor.setTextCursor(cursor)
        self.editor.setFocus()

    def _insert_url(self) -> None:
        cursor = self.editor.textCursor()
        selected = cursor.selectedText()
        if selected:
            cursor.insertText(f"[url={selected}]{selected}[/url]")
        else:
            cursor.insertText("[url=https://example.com]текст ссылки[/url]")
        self.editor.setFocus()

    def _insert_image(self) -> None:
        cursor = self.editor.textCursor()
        cursor.insertText("[img]https://example.com/image.png[/img]")
        self.editor.setFocus()

    def _refresh_preview(self) -> None:
        text = self.editor.toPlainText()
        try:
            self.preview.setHtml(bbcode_to_html(text))
        except Exception as exc:
            log.exception("Ошибка предпросмотра BB-кода: %s", exc)
            self.preview.setPlainText(f"Ошибка предпросмотра: {exc}")

    def _on_accept(self) -> None:
        self._result_text = self.editor.toPlainText()
        log.info("BB-редактор: сохранено (%d символов)",
                 len(self._result_text))
        self.accept()

    def _on_reject(self) -> None:
        log.info("BB-редактор: отменено")
        self.reject()

    def result_text(self) -> str:
        return self._result_text


# ---------------------------------------------------------------------------
# Просмотрщик (только чтение)
# ---------------------------------------------------------------------------
class BBCodeViewerDialog(QDialog):
    """
    Модальное окно для просмотра BB-кода без редактирования.

    Слева — plain-text исходник (только чтение), справа — отрендеренный
    HTML-предпросмотр. Никаких кнопок редактирования — только «Закрыть».
    """

    def __init__(
        self,
        text: str = "",
        title: str = "Просмотр summary",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumSize(960, 640)

        root = QVBoxLayout(self)

        info = QLabel(
            "Просмотр сохранённого краткого описания. "
            "Чтобы изменить — закройте окно и нажмите «Изменить summary…»."
        )
        info.setWordWrap(True)
        root.addWidget(info)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        self.source = QPlainTextEdit()
        self.source.setReadOnly(True)
        self.source.setPlainText(text or "")
        splitter.addWidget(self.source)

        self.preview = QTextBrowser()
        self.preview.setOpenExternalLinks(True)
        try:
            self.preview.setHtml(bbcode_to_html(text or ""))
        except Exception as exc:
            log.exception("Ошибка рендера BB-кода: %s", exc)
            self.preview.setPlainText(str(exc))
        splitter.addWidget(self.preview)

        splitter.setSizes([400, 560])
        root.addWidget(splitter, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Close,
            parent=self,
        )
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("Закрыть")
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        root.addWidget(buttons)