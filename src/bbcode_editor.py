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

Изменения:
  • bbcode_to_plain теперь корректно обрабатывает [LIST],
    [/LIST], [LIST=1] и [*] — раньше [*] оставался в тексте
    как мусор (именно из-за этого summary со старым BB-кодом
    уходили в Bitrix24 с видимыми тегами).
  • bbcode_to_html умеет рендерить [LIST] → <ul>/<ol>
    и [*] → <li>.
  • bbcode_to_plain схлопывает множественные пустые строки.
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

    # --- Простые парные теги ---
    pairs = [
        (r"\[b\](.*?)\[/b\]", r"<b>\1</b>"),
        (r"\[i\](.*?)\[/i\]", r"<i>\1</i>"),
        (r"\[u\](.*?)\[/u\]", r"<u>\1</u>"),
        (r"\[s\](.*?)\[/s\]", r"<s>\1</s>"),
    ]
    for pattern, repl in pairs:
        s = re.sub(
            pattern, repl, s, flags=re.DOTALL | re.IGNORECASE
        )

    # --- Списки ---
    # ВАЖНО: Bitrix24 НЕ отдаёт [LIST] как есть, но в старых
    # записях он мог сохраниться. Плюс функция используется
    # для предпросмотра — пусть рендерит корректно.
    #
    # Порядок важен: сначала обрабатываем пары [LIST]...[/LIST],
    # потом отдельные [*]. Иначе [*] внутри [LIST] не найдётся
    # после того, как [LIST] уже заменён.

    # Нумерованный список [LIST=1]...[/LIST] → <ol>...</ol>
    s = re.sub(
        r"\[LIST=1\](.*?)\[/LIST\]",
        lambda m: "<ol>" + m.group(1) + "</ol>",
        s,
        flags=re.DOTALL | re.IGNORECASE,
    )
    # Маркированный список [LIST]...[/LIST] → <ul>...</ul>
    s = re.sub(
        r"\[LIST\](.*?)\[/LIST\]",
        lambda m: "<ul>" + m.group(1) + "</ul>",
        s,
        flags=re.DOTALL | re.IGNORECASE,
    )
    # Элемент списка: [*]Текст до следующего [*] или </ul>/</ol>
    s = re.sub(
        r"\[\*\](.*?)(?=\[\*\]|</ul>|</ol>|$)",
        lambda m: "<li>" + m.group(1).strip() + "</li>",
        s,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # --- Цитата ---
    s = re.sub(
        r"\[quote\](.*?)\[/quote\]",
        r'<blockquote style="border-left:3px solid #888; '
        r'margin:6px 0; padding:4px 10px; color:#555;">'
        r"\1</blockquote>",
        s,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # --- Код ---
    s = re.sub(
        r"\[code\](.*?)\[/code\]",
        r'<pre style="background:#f4f4f4; padding:6px; '
        r'font-family:monospace; border-radius:4px;">'
        r"\1</pre>",
        s,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # --- Спойлер ---
    s = re.sub(
        r"\[spoiler\](.*?)\[/spoiler\]",
        r'<details style="margin:6px 0;">'
        r'<summary style="cursor:pointer; color:#4a90d9;">'
        r"Показать спойлер</summary>\1</details>",
        s,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # --- URL с текстом ---
    s = re.sub(
        r"\[url=([^\]]+)\](.*?)\[/url\]",
        r'<a href="\1">\2</a>',
        s,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # --- URL без текста ---
    s = re.sub(
        r"\[url\](.*?)\[/url\]",
        r'<a href="\1">\1</a>',
        s,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # --- Картинка ---
    s = re.sub(
        r"\[img\](.*?)\[/img\]",
        r'<img src="\1" style="max-width:100%;" />',
        s,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # --- Переносы строк ---
    s = s.replace("\n", "<br>")

    return (
        "<div style='font-family:sans-serif; font-size:12pt;'>"
        f"{s}</div>"
    )


# ---------------------------------------------------------------------------
# BB → plain text
# ---------------------------------------------------------------------------
def bbcode_to_plain(text: str) -> str:
    """
    Убирает BB-разметку и оставляет только чистый текст.

    Используется для передачи summary в суммаризатор: большинство
    моделей не понимают BB-код и воспримут его как мусор.
    Также используется в markdown_to_plain_with_bb для очистки
    старых записей.

    ВАЖНО: Bitrix24 не поддерживает [LIST]/[*], но в старых
    записях они могли сохраниться. Функция обязана их удалять,
    иначе в чат уходит мусор вида «[LIST]» и «[*]».
    """
    if not text:
        return ""

    s = text

    # --- Простые парные теги ---
    for tag in ("b", "i", "u", "s", "quote", "code", "spoiler"):
        s = re.sub(
            rf"\[{tag}\](.*?)\[/{tag}\]",
            r"\1",
            s,
            flags=re.DOTALL | re.IGNORECASE,
        )

    # --- Списки ---
    # [LIST] и [/LIST] (и [LIST=1]) удаляем полностью.
    s = re.sub(
        r"\[/?LIST(?:=\d+)?\]",
        "",
        s,
        flags=re.IGNORECASE,
    )

    # [*] → юникод-буллет «• ». Это читаемо и в plain text,
    # и в Bitrix24. Если нужен ASCII — замените на "- ".
    s = s.replace("[*]", "• ")

    # --- Таблицы (Bitrix24) ---
    # Теги таблиц убираем, содержимое ячеек разделяем пробелом.
    for tag in ("table", "tr"):
        s = re.sub(
            rf"\[/?{tag}\]",
            "\n",
            s,
            flags=re.IGNORECASE,
        )
    for tag in ("td", "th"):
        s = re.sub(
            rf"\[{tag}\]",
            " ",
            s,
            flags=re.IGNORECASE,
        )
        s = re.sub(
            rf"\[/{tag}\]",
            " ",
            s,
            flags=re.IGNORECASE,
        )

    # --- URL с текстом ---
    s = re.sub(
        r"\[url=([^\]]+)\](.*?)\[/url\]",
        r"\2 (\1)",
        s,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # --- URL без параметра ---
    s = re.sub(
        r"\[url\](.*?)\[/url\]",
        r"\1",
        s,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # --- Картинка ---
    s = re.sub(
        r"\[img\](.*?)\[/img\]",
        r"[изображение: \1]",
        s,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # --- Любые оставшиеся теги вида [xxx] или [/xxx] ---
    s = re.sub(r"\[/?[a-zA-Z][^\]]*\]", "", s)

    # --- Схлопываем множественные пустые строки ---
    s = re.sub(r"\n{3,}", "\n\n", s)

    return s.strip()