"""Конвертер BB-кода в plain text и HTML.

Оставлены только функции, реально используемые в проекте:
  • bbcode_to_plain — вызывается из markdown_to_plain_with_bb
    для очистки старых записей, где summary хранился в BB-коде;
  • bbcode_to_html — используется для предпросмотра BB-текста
    (историческая функция, может пригодиться при отладке).

Удалено как неиспользуемое:
  • bbcode_to_bitrix() — конвертация в BB идёт через markdown_to_bitrix;
  • BBCodeEditorDialog — заменён на MarkdownEditorDialog;
  • BBCodeViewerDialog — заменён на MarkdownViewerDialog.
"""
from __future__ import annotations

import html
import re


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