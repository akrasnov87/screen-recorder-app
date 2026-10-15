"""Массовая рассылка готовых сообщений сотрудникам в Bitrix24.

Функция send_items_to_employees() принимает уже сформированный
список сообщений [{chat_id, employee_name, assignee, text_md, items}]
и рассылает их через Bitrix24Client.

Используется:
  • в SendToEmployeesDialog (standalone-рассылка);
  • в SendToBitrixDialog (post-step после успешной отправки
    сообщения в чат, для скрам-записей).

Модуль синхронный — вызывается из GUI-потока, внутри
создаёт собственный event loop в отдельном потоке и показывает
QProgressDialog.
"""
from __future__ import annotations

import asyncio
import html
import threading
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QMessageBox, QProgressDialog, QWidget,
)

from .bitrix_client import Bitrix24Client, Bitrix24Error
from .logger import get_logger
from .markdown_to_bitrix import markdown_to_bitrix

log = get_logger(__name__)


def send_items_to_employees(
    messages: List[Dict[str, Any]],
    bitrix_cfg: Dict[str, Any],
    *,
    parent: Optional[QWidget] = None,
    show_confirmation: bool = True,
    confirmation_title: str = "Отправка сотрудникам",
) -> bool:
    """
    Рассылает готовые сообщения сотрудникам в их личные чаты.

    Args:
        messages:          список сообщений (см. collect_messages
                           в EmployeesMatchingWidget).
        bitrix_cfg:        настройки Bitrix24 (webhook_url,
                           таймауты, system_message и т.д.).
        parent:            родительский виджет для диалогов.
        show_confirmation: если True — показывает подтверждение
                           перед отправкой. Если False — сразу
                           отправляет.
        confirmation_title: заголовок окна подтверждения.

    Returns:
        True, если все сообщения ушли успешно (или нечего было
        отправлять — пустой список).
        False, если была хотя бы одна ошибка отправки или
        пользователь отменил.
    """
    if not messages:
        log.info("send_items_to_employees: список пуст — нечего слать")
        return True

    webhook = (bitrix_cfg.get("webhook_url") or "").strip()
    if not webhook:
        QMessageBox.warning(
            parent, "Bitrix24",
            "Вебхук Bitrix24 не настроен. "
            "Откройте Настройки → Bitrix24.",
        )
        return False

    # --- Подтверждение ---
    if show_confirmation:
        preview_lines: List[str] = []
        for m in messages[:15]:
            preview_lines.append(
                f"• {m['assignee']} → {m['employee_name']} "
                f"({m['chat_id']}) — {len(m['items'])} поруч."
            )
        if len(messages) > 15:
            preview_lines.append(
                f"… и ещё {len(messages) - 15}"
            )
        preview_text = "\n".join(preview_lines)

        reply = QMessageBox.question(
            parent, confirmation_title,
            f"<b>Будет отправлено {len(messages)} сообщений:</b>"
            f"<br><br>"
            f"<pre style='font-family:monospace'>"
            f"{html.escape(preview_text)}</pre>"
            f"<br>Продолжить?",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            log.info(
                "send_items_to_employees: пользователь отменил "
                "отправку"
            )
            return False

    # --- Параметры клиента ---
    connect_timeout = float(
        bitrix_cfg.get("connect_timeout", 15)
    )
    read_timeout = float(bitrix_cfg.get("read_timeout", 60))
    max_chars = int(
        bitrix_cfg.get("max_message_chars", 15000)
    )
    system = bool(bitrix_cfg.get("system_message", False))
    url_preview = not bool(
        bitrix_cfg.get("disable_url_preview", False)
    )

    # --- Прогресс ---
    dlg = QProgressDialog(
        "Отправка сотрудникам…",
        None, 0, len(messages), parent,
    )
    dlg.setWindowTitle("Bitrix24")
    dlg.setWindowModality(Qt.WindowModality.WindowModal)
    dlg.setMinimumDuration(0)
    dlg.show()
    QGuiApplication.processEvents()

    errors: List[str] = []

    def _worker() -> None:
        async def _run() -> None:
            async with Bitrix24Client(
                webhook_url=webhook,
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
                max_message_chars=max_chars,
            ) as client:
                for i, m in enumerate(messages, start=1):
                    try:
                        text_bb = markdown_to_bitrix(m["text_md"])
                        await client.send_message(
                            dialog_id=m["chat_id"],
                            text=text_bb,
                            system=system,
                            url_preview=url_preview,
                        )
                        log.info(
                            "Отправлено поручений: %s → %s "
                            "(%d поруч.) [%d/%d]",
                            m["assignee"], m["chat_id"],
                            len(m["items"]), i, len(messages),
                        )
                    except Bitrix24Error as exc:
                        log.error(
                            "Не удалось отправить %s → %s: %s",
                            m["assignee"], m["chat_id"], exc,
                        )
                        errors.append(
                            f"{m['assignee']} → "
                            f"{m['chat_id']}: {exc}"
                        )

        try:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(_run())
            finally:
                loop.close()
        except Exception as exc:
            log.exception(
                "Ошибка отправки сотрудникам: %s", exc
            )
            errors.append(f"Общая ошибка: {exc}")

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    while t.is_alive():
        QGuiApplication.processEvents()
        t.join(0.05)

    dlg.close()

    # --- Итог ---
    if errors:
        err_text = "\n".join(errors[:15])
        if len(errors) > 15:
            err_text += f"\n… и ещё {len(errors) - 15}"
        QMessageBox.warning(
            parent, "Bitrix24",
            f"Отправлено: {len(messages) - len(errors)} "
            f"из {len(messages)}\n\n"
            f"Ошибки:\n{err_text}",
        )
        return False

    QMessageBox.information(
        parent, "Bitrix24",
        f"Отправлено сообщений: {len(messages)}",
    )
    return True