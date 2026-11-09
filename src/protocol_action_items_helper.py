# src/protocol_action_items_helper.py
"""Помощник формирования поручений из протокола.

Когда пользователь нажимает «Обновить промпт поручений» в окне
«Записи», приложение:
  1. Пересобирает action_items_prompt.<ext> из протокола записи
     (это делает action_items_prompt_builder.regenerate_action_items_prompt).
  2. Если включена настройка scrum.generate_action_items_from_protocol
     и LiteLLM настроен — дополнительно вызывает LiteLLM, парсит
     JSON-ответ и добавляет поручения в action_items.json
     с дедупликацией.

Модуль не зависит от UI и может вызываться:
  • из sessions_window.py (кнопка «Обновить промпт поручений»);
  • из CLI/тестов.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config_manager import DEFAULT_ACTION_ITEMS_PROMPT
from .file_readers import read_any_text
from .litellm_client import LiteLLMClient, LiteLLMError
from .logger import get_logger
from .tasks_json_parser import parse_action_items_json
from .tasks_manager import (
    get_next_item_number,
    load_action_items,
    save_action_items,
)

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Проверки
# ---------------------------------------------------------------------------
def is_litellm_configured_for_action_items(config_manager) -> bool:
    """
    Техническая готовность LiteLLM для извлечения поручений.

    Не проверяет protocol_summary_enabled — это отдельный
    сценарий. Требует только:
      • provider == "litellm";
      • задан base_url.
    """
    if config_manager is None:
        return False
    try:
        cfg = config_manager.get_summarizer_settings()
    except Exception as exc:
        log.warning(
            "is_litellm_configured_for_action_items: %s", exc
        )
        return False

    if str(cfg.get("provider", "")).lower() != "litellm":
        return False

    litellm = cfg.get("litellm") or {}
    return bool(str(litellm.get("base_url") or "").strip())


def is_action_items_from_protocol_enabled(
    config_manager,
) -> bool:
    """
    Включена ли настройка «Формировать поручения на основе
    протокола» (scrum.generate_action_items_from_protocol).

    Дополнительно проверяет техническую готовность LiteLLM.
    """
    if not is_litellm_configured_for_action_items(config_manager):
        return False

    try:
        cfg = config_manager.config.get("scrum", {}) or {}
    except Exception:
        return False

    return bool(cfg.get("generate_action_items_from_protocol", True))


# ---------------------------------------------------------------------------
# Чтение протокола и стенограммы
# ---------------------------------------------------------------------------
def _read_protocol_and_transcript(
    session_dir: str,
    *,
    max_chars: int = 5_000_000,
) -> Tuple[str, str, str]:
    """
    Читает протокол и стенограмму из папки сессии.

    Returns:
        (protocol_text, transcript_text, protocol_path)
    """
    from .action_items_prompt_builder import find_protocol_in_session

    protocol_path = find_protocol_in_session(session_dir)
    protocol_text = ""
    if protocol_path:
        try:
            protocol_text = read_any_text(protocol_path, max_chars) or ""
        except Exception as exc:
            log.warning(
                "Не удалось прочитать протокол %s: %s",
                protocol_path, exc,
            )
            protocol_text = ""

    transcript_text = ""
    transcript_path = os.path.join(session_dir, "video.txt")
    if os.path.isfile(transcript_path):
        try:
            transcript_text = read_any_text(
                transcript_path, max_chars
            ) or ""
        except Exception as exc:
            log.warning(
                "Не удалось прочитать стенограмму %s: %s",
                transcript_path, exc,
            )
            transcript_text = ""

    return protocol_text.strip(), transcript_text.strip(), protocol_path


def _extract_action_items_section(protocol_text: str) -> Tuple[str, bool]:
    """
    Вырезает из протокола только раздел с поручениями.

    Возвращает кортеж: (извлеченный_текст, найден_ли_раздел).
    Если маркер не найден, возвращает (исходный_текст, False).
    """
    if not protocol_text:
        return "", False

    lines = protocol_text.splitlines()
    start_idx = -1

    # Расширяем список маркеров для поиска (без учета регистра)
    markers = [
        "ПОРУЧЕНИЯ НА",
        "ПОРУЧЕНИЯ",
        "ACTION ITEMS",
        "ЗАДАЧИ НА",
        "СПИСОК ПОРУЧЕНИЙ",
        "РЕШЕНИЯ И ПОРУЧЕНИЯ",
    ]

    for i, line in enumerate(lines):
        line_upper = line.upper()
        # Проверяем, есть ли в строке один из маркеров
        if any(marker in line_upper for marker in markers):
            start_idx = i
            break

    if start_idx == -1:
        # Если раздел не найден — возвращаем весь текст
        return protocol_text, False

    # Возвращаем текст с найденного заголовка до конца
    extracted_text = "\n".join(lines[start_idx:]).strip()
    return extracted_text, True


# ---------------------------------------------------------------------------
# Дедупликация
# ---------------------------------------------------------------------------
def _normalize_key(text: str) -> str:
    """Нормализует текст для сравнения: lowercase, без пробелов."""
    return " ".join((text or "").lower().split())


def _item_key(item: Dict[str, Any]) -> Tuple[str, str, str]:
    """
    Ключ для дедупликации: (нормализованный текст,
    нормализованный исполнитель, срок).

    Так повторный вызов LiteLLM с тем же протоколом не создаст
    дубликатов, но если у поручения поменялся исполнитель или
    срок — оно будет добавлено как новое.
    """
    return (
        _normalize_key(item.get("text") or ""),
        _normalize_key(item.get("assignee") or ""),
        _normalize_key(item.get("due_date") or ""),
    )


def _dedupe_against_existing(
    existing: List[Dict[str, Any]],
    incoming: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], int]:
    """
    Отбрасывает из incoming элементы, которые уже есть в existing.

    Returns:
        (new_items, skipped_count)
    """
    existing_keys = {
        _item_key(it) for it in existing if it.get("text")
    }
    result: List[Dict[str, Any]] = []
    skipped = 0

    for it in incoming:
        if not (it.get("text") or "").strip():
            continue
        key = _item_key(it)
        if key in existing_keys:
            skipped += 1
            continue
        existing_keys.add(key)
        result.append(it)

    return result, skipped


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------
def regenerate_action_items_with_llm_sync(
    *,
    config_manager,
    session_dir: str,
    session_name: str = "",
    session_date: str = "",
    progress_cb: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """
    Полный цикл извлечения поручений из протокола через LiteLLM
    и добавления их в action_items.json.

    Args:
        config_manager: ConfigManager приложения.
        session_dir:    папка записи.
        session_name:   название записи (для контекста в промпте).
        session_date:   дата записи (для контекста в промпте).
        progress_cb:    колбэк прогресса (для UI).

    Returns:
        {
          "ok": bool,
          "added": int,          # добавлено новых поручений
          "skipped": int,        # пропущено как дубликаты
          "total": int,          # всего поручений в файле после
          "protocol_path": str,
          "transcript_used": bool,
          "section_found": bool, # найден ли раздел с поручениями
          "error": str,          # описание ошибки (если ok=False)
          "warning": str,        # предупреждение (не ошибка)
        }
    """
    result: Dict[str, Any] = {
        "ok": False,
        "added": 0,
        "skipped": 0,
        "total": 0,
        "protocol_path": "",
        "transcript_used": False,
        "section_found": False,
        "error": "",
        "warning": "",
    }

    if not session_dir or not os.path.isdir(session_dir):
        result["error"] = f"Папка записи не найдена: {session_dir}"
        return result

    if not is_litellm_configured_for_action_items(config_manager):
        result["error"] = (
            "LiteLLM не настроен (Настройки → Суммаризация): "
            "провайдер должен быть «LiteLLM» и задан base_url."
        )
        return result

    if progress_cb:
        progress_cb("Чтение протокола и стенограммы…")

    protocol_text, transcript_text, protocol_path = (
        _read_protocol_and_transcript(session_dir)
    )
    result["protocol_path"] = protocol_path
    result["transcript_used"] = bool(transcript_text)

    if not protocol_text and not transcript_text:
        result["error"] = (
            "Не найден ни протокол, ни стенограмма. "
            "Извлекать поручения не из чего."
        )
        return result

    if not protocol_text:
        result["warning"] = (
            "Протокол не найден — поручения будут извлечены "
            "только из стенограммы."
        )

    # --- Промпт ---
    try:
        scrum_cfg = config_manager.config.get("scrum", {}) or {}
    except Exception:
        scrum_cfg = {}

    template = str(
        scrum_cfg.get("action_items_prompt_template")
        or DEFAULT_ACTION_ITEMS_PROMPT
    )

    # --- Модель / таймауты ---
    try:
        sum_cfg = config_manager.get_summarizer_settings()
    except Exception as exc:
        result["error"] = f"Не удалось прочитать настройки: {exc}"
        return result

    l = sum_cfg.get("litellm") or {}
    base_url = str(l.get("base_url") or "").strip()
    api_key = str(l.get("api_key") or "")
    model = str(l.get("model") or "gpt-4o-mini")
    max_tokens = int(l.get("max_tokens", 8192))
    connect_timeout = float(l.get("connect_timeout", 15))
    read_timeout = float(l.get("read_timeout", 300))

    # --- Умная сборка user-сообщения ---
    parts: List[str] = []
    if session_name:
        parts.append(f"# {session_name}")
        parts.append("")
    if session_date:
        parts.append(f"Дата совещания: {session_date}")
        parts.append("")

    section_found = False  # Флаг: нашли ли мы раздел с поручениями

    if protocol_text:
        # Пытаемся вырезать только раздел с поручениями
        extracted_protocol, section_found = _extract_action_items_section(
            protocol_text
        )

        if section_found and len(extracted_protocol) < len(protocol_text):
            parts.append("=== ПОРУЧЕНИЯ ИЗ ПРОТОКОЛА ===")
            parts.append(extracted_protocol)
            parts.append("")
            log.info(
                "В LLM передается только раздел с поручениями "
                "из протокола (%d симв.)", len(extracted_protocol)
            )
            # Обнуляем стенограмму, чтобы не раздувать контекст
            transcript_text = ""
        else:
            # Если раздел не найден, передаем весь протокол
            parts.append(
                "=== ПОЛНЫЙ ПРОТОКОЛ "
                "(раздел с поручениями не найден) ==="
            )
            parts.append(protocol_text)
            parts.append("")

    if transcript_text:
        parts.append("=== СТЕНОГРАММА ===")
        parts.append(transcript_text)

    user_message = "\n".join(parts)

    # Сохраняем в результат, был ли найден раздел
    result["section_found"] = section_found

    if not section_found:
        warning_msg = (
            "В протоколе не найден явный раздел с поручениями. "
            "Модель обрабатывает весь текст целиком, что может "
            "привести к обрезке ответа."
        )
        # Добавляем к общему warning, если он уже есть
        if result.get("warning"):
            result["warning"] += " " + warning_msg
        else:
            result["warning"] = warning_msg

    if progress_cb:
        progress_cb(
            f"Запрос к LiteLLM (модель: {model}, "
            f"вход: {len(user_message)} симв.)…"
        )

    # --- Вызов LiteLLM ---
    try:
        resp = asyncio.run(
            _call_litellm(
                base_url=base_url,
                api_key=api_key,
                model=model,
                system_prompt=template,
                user_message=user_message,
                temperature=0.05,
                max_tokens=max_tokens,
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
            )
        )
    except LiteLLMError as exc:
        result["error"] = f"Ошибка LiteLLM: {exc}"
        return result
    except Exception as exc:
        log.exception(
            "regenerate_action_items_with_llm_sync: %s", exc
        )
        result["error"] = f"Ошибка: {exc}"
        return result

    raw = resp.get("content") or ""
    finish_reason = str(resp.get("finish_reason") or "")
    if finish_reason == "length":
        log.warning(
            "Ответ LLM обрезан по лимиту токенов "
            "(finish_reason=length, max_tokens=%d)", max_tokens,
        )
        result["warning"] = (
            "Ответ LLM обрезан по лимиту токенов — "
            "часть поручений может быть потеряна."
        )

    if not raw or not raw.strip():
        result["error"] = "LiteLLM вернул пустой ответ"
        return result

    # --- Сохраняем сырой ответ (для отладки) ---
    _save_raw_response(session_dir, raw)

    # --- Парсинг ---
    parsed = parse_action_items_json(raw)
    if not parsed:
        result["error"] = (
            f"Не удалось распарсить JSON от LLM: "
            f"{parsed.warning or 'неизвестная ошибка'}"
        )
        return result

    if progress_cb:
        progress_cb(
            f"LLM вернул поручений: {len(parsed.items)}. "
            f"Проверка дубликатов…"
        )

    # --- Загрузка текущих поручений и дедупликация ---
    data = load_action_items(session_dir)
    if session_name:
        data["session_name"] = session_name

    existing = data.get("items") or []
    new_items, skipped = _dedupe_against_existing(
        existing, parsed.items
    )

    if not new_items:
        result["ok"] = True
        result["skipped"] = skipped
        result["total"] = len(existing)
        if skipped:
            result["warning"] = (
                f"Все {skipped} поручений уже есть в списке — "
                f"новых не добавлено."
            )
        else:
            result["warning"] = "LLM не вернул ни одного поручения."
        return result

    # --- Проставляем номера и source ---
    for it in new_items:
        if not it.get("number"):
            it["number"] = get_next_item_number(session_dir)
        it["source"] = "llm"
        if not it.get("context"):
            it["context"] = session_name or ""

    existing.extend(new_items)
    data["items"] = existing

    if not save_action_items(
        session_dir, data, skip_normalize=True
    ):
        result["error"] = "Не удалось сохранить action_items.json"
        return result

    result["ok"] = True
    result["added"] = len(new_items)
    result["skipped"] = skipped
    result["total"] = len(existing)

    log.info(
        "Поручения из протокола: добавлено %d, пропущено "
        "дубликатов %d, всего %d (session=%s)",
        len(new_items), skipped, len(existing),
        os.path.basename(session_dir),
    )
    return result


# ---------------------------------------------------------------------------
# Внутренние
# ---------------------------------------------------------------------------
async def _call_litellm(
    *,
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_message: str,
    temperature: float,
    max_tokens: int,
    connect_timeout: float,
    read_timeout: float,
) -> Dict[str, Any]:
    """Возвращает {"content": str, "finish_reason": str, "usage": dict}."""
    async with LiteLLMClient(
        base_url=base_url,
        api_key=api_key,
        model=model,
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
    ) as client:
        return await client.chat_completion_detailed(
            system_prompt=system_prompt,
            user_prompt=user_message,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )


def _save_raw_response(session_dir: str, raw: str) -> None:
    """Сохраняет сырой ответ LLM для отладки."""
    from datetime import datetime

    path = os.path.join(
        session_dir, "action_items_response_raw.txt"
    )
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(
                "===== ACTION ITEMS RAW RESPONSE =====\n"
                f"Сформирован: {datetime.now().isoformat()}\n"
                f"Символов: {len(raw)}\n"
                "\n"
                f"{raw}\n"
            )
    except Exception as exc:
        log.warning(
            "Не удалось сохранить сырой ответ %s: %s", path, exc
        )