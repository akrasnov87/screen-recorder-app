"""Управление настройками с шифрованием паролей."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any, Dict, List

import keyring
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from .logger import get_logger

log = get_logger(__name__)

# Дефолтный шаблон промпта для DeepSeek
DEFAULT_SCRUM_PROMPT = (
    "Во вложении стенограмма статусного совещания с командой. "
    "Так же во вложении протокол предыдущего созвона. "
    "Сделай протокол сегодняшнего совещания с фиксацией всех основных "
    "моментов/рисков в разрезе каждого сотрудника.\n\n"
    "Протокол должен содержать:\n"
    " - заголовок (ПРОТОКОЛ СОВЕЩАНИЯ ЕЖД-год-месяц-день)\n"
    " - дата и время\n"
    " - тема\n"
    " - модератор\n"
    " - список участников\n"
    " - вступительная часть\n"
    " - статус по сотрудникам: краткий статус работ за прошлый день "
    "(без цитат и лишних обсуждений, только ключевые моменты, что делал "
    "ранее), план работ на сегодня, риски, которые озвучил сотрудник\n"
    " - список поручений для сотрудников со сроками реализации, если это "
    "было озвучено (нужны только новые поручения на сегодня, не используй "
    "старые из протокола).\n\n"
    "Придерживайся этой структуры, ничего лишнего не добавляй, если этого "
    "не было озвучено в стенограмме."
)

# Дефолтные шаблоны названий записи.
# Плейсхолдеры: {name} {abbr} {date} {time} {datetime}
# Также поддерживаются русские варианты: {название} {сокр} {дата} {время}
DEFAULT_NAME_TEMPLATES: List[Dict[str, str]] = [
    {
        "label": "Название + дата",
        "template": "{name} — {date}",
    },
    {
        "label": "Название + дата и время",
        "template": "{name} — {date} {time}",
    },
    {
        "label": "Проект: название (сокр) — дата",
        "template": "{name} ({abbr}) — {date}",
    },
    {
        "label": "Совещание: название — дата",
        "template": "Совещание: {name} — {date}",
    },
    {
        "label": "Только название",
        "template": "{name}",
    },
]


DEFAULT_CONFIG: Dict[str, Any] = {
    "projects": [
        "Россети",
        "iserv",
        "Внутренние",
        "Тестовые",
    ],
    "metadata": {
        "prompts": [
            {
                "name": "Протокол совещания",
                "text": (
                    "Составь протокол совещания на русском языке.\n"
                    "Структура:\n"
                    "1. Участники (если упоминались)\n"
                    "2. Основные темы обсуждения\n"
                    "3. Принятые решения\n"
                    "4. Задачи с ответственными и сроками\n"
                    "5. Открытые вопросы"
                ),
            },
            {
                "name": "Краткое содержание",
                "text": (
                    "Составь краткое содержание записи на русском языке. "
                    "Выдели ключевые темы и решения. В конце — список action items."
                ),
            },
            {
                "name": "Технический созвон",
                "text": (
                    "Составь техническое резюме созвона на русском языке. "
                    "Выдели: обсуждённые технические вопросы, принятые решения, "
                    "команды/конфигурации (если упоминались), задачи для разработчиков."
                ),
            },
        ],
        "default_prompt": (
            "Составь краткое содержание записи на русском языке. "
            "Выдели ключевые темы, решения и задачи с ответственными. "
            "В конце — список action items."
        ),
        # --- Шаблоны названий записи ---
        "name_templates": DEFAULT_NAME_TEMPLATES,
    },
    "transcribe": {
        "url": "http://localhost:8000",
        "access_key": "",
        "connect_timeout": 15,
        "read_timeout": 120,
        "max_wait": 7200,
    },
    "recording": {
        "monitor": 0,
        "with_microphone": True,
        "show_watermark": True,
        "hotkey_start": "Ctrl+Shift+R",
        "hotkey_stop": "Ctrl+Shift+S",
        "show_metadata_on_start": True,
        "show_metadata_on_stop": True,
        "show_overlay_panel": True,
        "show_start_notification": True,
    },
    "queue": {
        "auto_retry_enabled": True,
        "retry_interval_minutes": 5,
        "max_retries": 10,
    },
    "compression": {
        "audio_format": "mp3",
        "audio_bitrate": 192,
        "video_bitrate": 4000,
        "compression_level": 5,
    },
    "storage": {
        "temp_path": "/tmp/screen-recorder",
        "retention_hours": 24,
    },
    "logging": {
        "log_path": "/tmp/screen-recorder/app.log",
        "level": "DEBUG",
    },
    # --- Скрам-митинги ---
    "scrum": {
        # Шаблон промпта для DeepSeek
        "prompt_template": DEFAULT_SCRUM_PROMPT,
        # Формат экспорта промпта: txt | md | docx
        "export_format": "docx",
    },
    # --- Формирование протокола (резюме) ---
    "summarizer": {
        "provider": "server",          # server | litellm
        "litellm": {
            "base_url": "http://localhost:4000",
            "api_key": "",
            "model": "gpt-4o-mini",
            "temperature": 0.25,
            "max_tokens": 2200,
            "connect_timeout": 15,
            "read_timeout": 300,
            "system_prompt": (
                "Ты опытный редактор. Сформируй итоговый документ строго "
                "по инструкции пользователя. Не добавляй вступлений, "
                "заключений и мета-комментариев, не предлагай правок. "
                "Отвечай на русском языке, если в инструкции не сказано иное."
            ),
        },
    },
    # --- Глоссарий терминов ---
    # Список терминов/аббревиатур, которые нужно «знать» модели.
    #   terms              — список {"term": ..., "description": ...}
    #   send_to_summarizer — дописывать глоссарий в промпт суммаризации
    #                        (summary_prompt для сервера или user-prompt
    #                         для LiteLLM);
    #   send_to_deepseek   — добавлять блок «ГЛОССАРИЙ» в файл
    #                        deepseek_prompt.*.
    "glossary": {
        "terms": [
            {"term": "ЕЖД", "description": "Единый журнал дежурств"},
        ],
        "send_to_summarizer": True,
        "send_to_deepseek": True,
    },
}

_SERVICE_NAME = "screen-recorder-app"
_SALT = b"screen-recorder-salt-v1"


class ConfigManager:
    """Менеджер конфигурации."""

    def __init__(self, config_path: str | None = None) -> None:
        if config_path is None:
            config_path = os.path.join(
                os.path.expanduser("~"), ".config", "screen-recorder", "config.json"
            )
        self.config_path = Path(config_path)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        log.info("Инициализация ConfigManager: %s", self.config_path)
        self.config: Dict[str, Any] = self.get_defaults()
        self.load()

    def _build_fernet(self) -> Fernet:
        key = keyring.get_password(_SERVICE_NAME, "__fernet_key__")
        if not key:
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=32,
                salt=_SALT,
                iterations=100_000,
            )
            key = base64.urlsafe_b64encode(kdf.derive(os.urandom(16))).decode()
            try:
                keyring.set_password(_SERVICE_NAME, "__fernet_key__", key)
            except Exception:
                key_file = self.config_path.parent / ".fernet_key"
                if not key_file.exists():
                    key_file.write_text(key)
                else:
                    key = key_file.read_text()
        return Fernet(key.encode() if isinstance(key, str) else key)

    def get_defaults(self) -> Dict[str, Any]:
        return json.loads(json.dumps(DEFAULT_CONFIG))

    def load(self) -> Dict[str, Any]:
        log.info("Загрузка конфигурации из %s", self.config_path)
        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.config = self._merge(self.get_defaults(), data)
                log.info("Конфигурация загружена (%d секций)", len(self.config))
            except Exception as exc:
                log.error("Ошибка загрузки конфигурации: %s", exc)
                self.config = self.get_defaults()
        else:
            log.info("Файл конфигурации не найден, используются значения по умолчанию")
            self.config = self.get_defaults()
        return self.config

    def save(self, config: Dict[str, Any] | None = None) -> None:
        if config is not None:
            self.config = config
        log.info("Сохранение конфигурации в %s", self.config_path)
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2, ensure_ascii=False)
            log.info("Конфигурация сохранена")
        except Exception as exc:
            log.error("Ошибка сохранения конфигурации: %s", exc)

    @staticmethod
    def _merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(base)
        for k, v in override.items():
            if k in result and isinstance(result[k], dict) and isinstance(v, dict):
                result[k] = ConfigManager._merge(result[k], v)
            else:
                result[k] = v
        return result

    # ----- Помощники -----
    def get_prompts(self) -> list[dict]:
        prompts = self.config.get("metadata", {}).get("prompts", [])
        if not isinstance(prompts, list):
            return []
        result = []
        for p in prompts:
            if isinstance(p, dict) and p.get("name") and p.get("text"):
                result.append({"name": str(p["name"]), "text": str(p["text"])})
            elif isinstance(p, str) and p.strip():
                first_line = p.strip().splitlines()[0][:60]
                result.append({"name": first_line, "text": p})
        return result

    def get_default_prompt(self) -> str:
        return self.config.get("metadata", {}).get("default_prompt", "") or ""

    # --- Шаблоны названий ---
    def get_name_templates(self) -> List[Dict[str, str]]:
        meta = self.config.get("metadata", {}) or {}
        raw = meta.get("name_templates", [])
        if not isinstance(raw, list):
            return []
        result: List[Dict[str, str]] = []
        for item in raw:
            if isinstance(item, dict):
                label = str(item.get("label") or "").strip()
                tpl = str(item.get("template") or "").strip()
                if tpl:
                    result.append({"label": label or tpl, "template": tpl})
            elif isinstance(item, str) and item.strip():
                result.append({"label": item.strip(), "template": item.strip()})
        return result

    def add_name_template(self, label: str, template: str) -> None:
        label = (label or "").strip()
        template = (template or "").strip()
        if not template:
            return
        meta = self.config.setdefault("metadata", {})
        templates = meta.setdefault("name_templates", [])
        replaced = False
        for i, item in enumerate(templates):
            if isinstance(item, dict) and item.get("template") == template:
                templates[i] = {"label": label or template, "template": template}
                replaced = True
                break
            elif isinstance(item, str) and item == template:
                templates[i] = {"label": label or template, "template": template}
                replaced = True
                break
        if not replaced:
            templates.append({"label": label or template, "template": template})
        self.save()
        log.info("Шаблон названия %s: %r",
                 "обновлён" if replaced else "добавлен", template)

    def get_queue_settings(self) -> Dict[str, Any]:
        q = self.config.get("queue", {})
        return {
            "auto_retry_enabled": bool(q.get("auto_retry_enabled", True)),
            "retry_interval_minutes": int(q.get("retry_interval_minutes", 5)),
            "max_retries": int(q.get("max_retries", 10)),
        }

    def get_log_settings(self) -> Dict[str, Any]:
        cfg = self.config.get("logging", {})
        return {
            "log_path": cfg.get("log_path", "/tmp/screen-recorder/app.log"),
            "level": cfg.get("level", "DEBUG"),
        }

    def get_transcribe_settings(self) -> Dict[str, Any]:
        cfg = self.config.get("transcribe", {})
        return {
            "url": cfg.get("url", ""),
            "access_key": cfg.get("access_key", ""),
            "connect_timeout": int(cfg.get("connect_timeout", 15)),
            "read_timeout": int(cfg.get("read_timeout", 120)),
            "max_wait": int(cfg.get("max_wait", 7200)),
        }

    def get_scrum_settings(self) -> Dict[str, Any]:
        cfg = self.config.get("scrum", {})
        return {
            "prompt_template": cfg.get("prompt_template", DEFAULT_SCRUM_PROMPT),
            "export_format": cfg.get("export_format", "docx"),
        }

    def get_summarizer_settings(self) -> Dict[str, Any]:
        cfg = self.config.get("summarizer", {}) or {}
        provider = str(cfg.get("provider", "server")).strip().lower()
        if provider not in ("server", "litellm"):
            provider = "server"

        l = cfg.get("litellm", {}) or {}
        litellm = {
            "base_url": str(l.get("base_url", "http://localhost:4000")).rstrip("/"),
            "api_key": str(l.get("api_key", "")),
            "model": str(l.get("model", "gpt-4o-mini")),
            "temperature": float(l.get("temperature", 0.25)),
            "max_tokens": int(l.get("max_tokens", 2200)),
            "connect_timeout": int(l.get("connect_timeout", 15)),
            "read_timeout": int(l.get("read_timeout", 300)),
            "system_prompt": str(l.get("system_prompt", "")),
        }
        return {
            "provider": provider,
            "litellm": litellm,
        }

    # --- Глоссарий ---
    def get_glossary_settings(self) -> Dict[str, Any]:
        """
        Возвращает настройки глоссария:
          • terms              — список {"term": ..., "description": ...};
          • send_to_summarizer — bool;
          • send_to_deepseek   — bool.
        """
        cfg = self.config.get("glossary", {}) or {}
        raw_terms = cfg.get("terms", [])
        terms: List[Dict[str, str]] = []
        if isinstance(raw_terms, list):
            for item in raw_terms:
                if isinstance(item, dict):
                    term = str(item.get("term") or "").strip()
                    desc = str(item.get("description") or "").strip()
                    if term:
                        terms.append({"term": term, "description": desc})
                elif isinstance(item, str) and item.strip():
                    terms.append({"term": item.strip(), "description": ""})
        return {
            "terms": terms,
            "send_to_summarizer": bool(cfg.get("send_to_summarizer", True)),
            "send_to_deepseek": bool(cfg.get("send_to_deepseek", True)),
        }