# src/protocol_summary_helper.py
"""Помощник формирования summary из протокола.

Когда пользователь прикрепляет или редактирует протокол записи,
приложение может предложить сгенерировать (или обновить) summary
на основе этого протокола.

Логика:
  • проверяем, что LiteLLM настроен (base_url, model);
  • читаем текст протокола (read_any_text);
  • вызываем LiteLLM с system_prompt из настроек суммаризации;
  • сохраняем результат в session.json → summary_bb и summary.md.

Выбор промпта для summary (в порядке приоритета):
  1) summary, привязанный к выбранному в записи промпту
     (поле "summary_prompt" в Библиотеке промптов);
  2) глобальный «Промпт для генерации summary» из настроек;
  3) «Промпт по умолчанию»;
  4) хардкод «Составь краткое содержание записи.».

Модуль не зависит от UI и может вызываться:
  • из sessions_window.py после сохранения протокола;
  • из import_window.py при импорте записи с протоколом;
  • вручную (для тестов / CLI).
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Callable, Dict, Optional

from .file_readers import read_any_text
from .litellm_client import LiteLLMClient, LiteLLMError
from .logger import get_logger
from .markdown_to_bitrix import markdown_to_plain_with_bb

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Проверка доступности LiteLLM
# ---------------------------------------------------------------------------
def is_litellm_configured(config_manager) -> bool:
    """
    Проверяет техническую готовность LiteLLM.

    Не проверяет флаг генерации из протокола — см.
    is_protocol_summary_enabled.
    """
    if config_manager is None:
        return False

    try:
        cfg = config_manager.get_summarizer_settings()
    except Exception as exc:
        log.warning("is_litellm_configured: %s", exc)
        return False

    if str(cfg.get("provider", "")).lower() != "litellm":
        return False

    litellm = cfg.get("litellm") or {}
    base_url = str(litellm.get("base_url") or "").strip()
    if not base_url:
        return False

    return True


def is_protocol_summary_enabled(config_manager) -> bool:
    """
    Разрешена ли генерация summary из протокола.

    Условия:
      • техническая готовность LiteLLM (см. is_litellm_configured);
      • summarizer.protocol_summary_enabled == True.
    """
    if not is_litellm_configured(config_manager):
        return False

    try:
        cfg = config_manager.get_summarizer_settings()
    except Exception:
        return False

    return bool(cfg.get("protocol_summary_enabled", True))


def get_litellm_model_name(config_manager) -> str:
    """Возвращает имя модели LiteLLM (для отображения в UI)."""
    if config_manager is None:
        return ""
    try:
        cfg = config_manager.get_summarizer_settings()
    except Exception:
        return ""
    return str((cfg.get("litellm") or {}).get("model") or "")


# ---------------------------------------------------------------------------
# Чтение текста протокола
# ---------------------------------------------------------------------------
_PROTOCOL_CANDIDATES = (
    "manual_protocol.docx",
    "manual_protocol.md",
    "manual_protocol.txt",
    "manual_protocol.pdf",
    "protocol.docx",
    "protocol.md",
    "protocol.txt",
    "protocol.pdf",
)


def find_protocol_in_session(session_dir: str) -> str:
    """Ищет протокол в папке записи. Возвращает "" если нет."""
    if not session_dir or not os.path.isdir(session_dir):
        return ""
    for name in _PROTOCOL_CANDIDATES:
        p = os.path.join(session_dir, name)
        if os.path.isfile(p):
            return p
    return ""


def read_protocol_text(
    protocol_path: str,
    *,
    max_chars: int = 5_000_000,
) -> str:
    """Читает текст протокола. Возвращает "" при ошибке."""
    if not protocol_path or not os.path.isfile(protocol_path):
        return ""
    try:
        text = read_any_text(protocol_path, max_chars)
    except Exception as exc:
        log.warning(
            "Не удалось прочитать протокол %s: %s",
            protocol_path, exc,
        )
        return ""
    return (text or "").strip()


# ---------------------------------------------------------------------------
# Генерация summary
# ---------------------------------------------------------------------------
def generate_summary_from_protocol_sync(
    *,
    config_manager,
    session_dir: str,
    protocol_path: str = "",
    progress_cb: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """
    Синхронная обёртка: генерирует summary из протокола через
    LiteLLM и сохраняет его в session.json → summary_bb
    и в файл summary.md.

    Args:
        config_manager: ConfigManager приложения.
        session_dir:    папка записи.
        protocol_path:  путь к протоколу. Если пусто — ищется
                        автоматически в session_dir.
        progress_cb:    колбэк прогресса (для UI).

    Returns:
        {
          "ok": bool,
          "summary": str,       # сгенерированный текст
          "protocol_path": str, # какой протокол использован
          "chars": int,         # длина summary
          "error": str,         # описание ошибки (если ok=False)
        }
    """
    result: Dict[str, Any] = {
        "ok": False,
        "summary": "",
        "protocol_path": "",
        "chars": 0,
        "error": "",
    }

    if not session_dir or not os.path.isdir(session_dir):
        result["error"] = f"Папка записи не найдена: {session_dir}"
        return result

    if not is_litellm_configured(config_manager):
        result["error"] = "LiteLLM не настроен (Настройки → Суммаризация)"
        return result

    if not protocol_path:
        protocol_path = find_protocol_in_session(session_dir)
    if not protocol_path:
        result["error"] = "Протокол не найден в папке записи"
        return result

    if progress_cb:
        progress_cb(
            f"Чтение протокола: {os.path.basename(protocol_path)}"
        )

    protocol_text = read_protocol_text(protocol_path)
    if not protocol_text:
        result["error"] = "Протокол пустой или нечитаемый"
        result["protocol_path"] = protocol_path
        return result

    result["protocol_path"] = protocol_path

    if progress_cb:
        progress_cb("Генерация summary через LiteLLM…")

    try:
        summary_text = asyncio.run(
            _generate_summary_async(
                config_manager=config_manager,
                session_dir=session_dir,
                protocol_text=protocol_text,
                progress_cb=progress_cb,
            )
        )
    except LiteLLMError as exc:
        log.error("Ошибка LiteLLM: %s", exc)
        result["error"] = f"Ошибка LiteLLM: {exc}"
        return result
    except Exception as exc:
        log.exception("Ошибка генерации summary: %s", exc)
        result["error"] = f"Ошибка: {exc}"
        return result

    if not summary_text or not summary_text.strip():
        result["error"] = "LiteLLM вернул пустой ответ"
        return result

    if progress_cb:
        progress_cb("Сохранение summary…")

    save_error = _save_summary_to_session(
        session_dir=session_dir,
        summary_text=summary_text,
    )
    if save_error:
        result["error"] = f"Не удалось сохранить summary: {save_error}"
        result["summary"] = summary_text
        result["chars"] = len(summary_text)
        return result

    result["ok"] = True
    result["summary"] = summary_text
    result["chars"] = len(summary_text)

    log.info(
        "Summary сгенерирован из протокола %s: %d символов",
        os.path.basename(protocol_path), len(summary_text),
    )
    return result


async def _generate_summary_async(
    *,
    config_manager,
    session_dir: str,
    protocol_text: str,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> str:
    """Асинхронный вызов LiteLLM для генерации summary."""
    cfg = config_manager.get_summarizer_settings()
    litellm_cfg = cfg.get("litellm") or {}

    base_url = str(litellm_cfg.get("base_url") or "").strip()
    api_key = str(litellm_cfg.get("api_key") or "")
    model = str(litellm_cfg.get("model") or "gpt-4o-mini")
    temperature = float(litellm_cfg.get("temperature", 0.25))
    max_tokens = int(litellm_cfg.get("max_tokens", 2200))
    connect_timeout = float(litellm_cfg.get("connect_timeout", 15))
    read_timeout = float(litellm_cfg.get("read_timeout", 300))
    system_prompt = str(litellm_cfg.get("system_prompt") or "").strip()

    # Контекст записи (name/project) + выбранный промпт.
    # Читаем session.json один раз: и для контекста, и чтобы
    # узнать, какой промпт выбран в записи (для его summary).
    context_lines = []
    session_prompt_name = ""
    session_prompt_text = ""
    try:
        import json
        session_json = os.path.join(session_dir, "session.json")
        if os.path.isfile(session_json):
            with open(session_json, "r", encoding="utf-8") as f:
                meta = json.load(f)
            if isinstance(meta, dict):
                name = str(meta.get("name") or "").strip()
                project = str(meta.get("project") or "").strip()
                comment = str(meta.get("comment") or "").strip()
                if name:
                    context_lines.append(f"Название записи: {name}")
                if project:
                    context_lines.append(f"Проект: {project}")
                if comment:
                    context_lines.append(f"Комментарий: {comment}")
                session_prompt_name = str(
                    meta.get("prompt_name") or ""
                ).strip()
                session_prompt_text = str(
                    meta.get("prompt") or ""
                ).strip()
    except Exception:
        pass

    # Пользовательский промпт. Приоритеты:
    #   1) summary, привязанный к выбранному в записи промпту
    #      (Библиотека промптов → поле «Промпт для summary»);
    #   2) глобальный «Промпт для генерации summary»
    #      (Настройки → Промпты и имена);
    #   3) «Промпт по умолчанию»;
    #   4) хардкод.
    prompt_summary = ""
    summary_prompt = ""
    default_prompt = ""
    try:
        prompt_summary = (
            config_manager.get_prompt_summary_prompt(
                session_prompt_name, session_prompt_text
            ) or ""
        ).strip()
    except Exception:
        pass
    try:
        summary_prompt = (
            config_manager.get_summary_prompt() or ""
        )
    except Exception:
        pass
    try:
        default_prompt = (
            config_manager.get_default_prompt() or ""
        )
    except Exception:
        pass

    user_prompt = (
        prompt_summary
        or summary_prompt.strip()
        or default_prompt.strip()
        or "Составь краткое содержание записи."
    )

    # Глоссарий (если включён)
    glossary_text = ""
    try:
        glossary = config_manager.get_glossary_settings()
        if glossary.get("send_to_summarizer"):
            terms = glossary.get("terms") or []
            lines = []
            for t in terms:
                if isinstance(t, dict):
                    term = str(t.get("term") or "").strip()
                    desc = str(t.get("description") or "").strip()
                else:
                    term = str(t or "").strip()
                    desc = ""
                if term:
                    lines.append(f"- {term} — {desc}" if desc else f"- {term}")
            if lines:
                glossary_text = "\n".join(lines)
    except Exception:
        pass

    parts = []
    if context_lines:
        parts.append("===== КОНТЕКСТ ЗАПИСИ =====")
        parts.append("\n".join(context_lines))
        parts.append("")
    parts.append("===== ИНСТРУКЦИЯ =====")
    parts.append(user_prompt)
    if glossary_text:
        parts.append("")
        parts.append("===== ГЛОССАРИЙ =====")
        parts.append(glossary_text)
    parts.append("")
    parts.append("===== ПРОТОКОЛ =====")
    parts.append(protocol_text)

    user_message = "\n".join(parts)

    async with LiteLLMClient(
        base_url=base_url,
        api_key=api_key,
        model=model,
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
    ) as client:
        return await client.chat_completion(
            system_prompt=system_prompt,
            user_prompt=user_message,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )


def _save_summary_to_session(
    *,
    session_dir: str,
    summary_text: str,
) -> str:
    """
    Сохраняет summary в session.json (summary_bb) и summary.md.

    Returns:
        "" при успехе, иначе — текст ошибки.
    """
    import json

    session_json = os.path.join(session_dir, "session.json")
    summary_md_path = os.path.join(session_dir, "summary.md")

    try:
        if os.path.isfile(session_json):
            with open(session_json, "r", encoding="utf-8") as f:
                meta = json.load(f)
        else:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}

        meta["summary_bb"] = summary_text

        tmp = session_json + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
        os.replace(tmp, session_json)
    except Exception as exc:
        log.exception(
            "Не удалось сохранить summary в session.json: %s", exc
        )
        return f"session.json: {exc}"

    try:
        with open(summary_md_path, "w", encoding="utf-8") as f:
            f.write(summary_text)
    except Exception as exc:
        log.warning(
            "Не удалось сохранить summary.md: %s", exc
        )
        # не критично — summary уже в session.json

    log.info(
        "Summary сохранён: session=%s, chars=%d",
        os.path.basename(session_dir), len(summary_text),
    )
    return ""