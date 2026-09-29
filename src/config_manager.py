"""Управление настройками с шифрованием паролей.

Изменения:
  • Добавлены ключи default_project и default_chat_id —
    имя проекта по умолчанию и ID чата по умолчанию.
  • Добавлены методы get_default_project/set_default_project
    и get_default_chat_id/set_default_chat_id.
  • Добавлен справочник тегов: секция config["tags"],
    методы get_tags()/get_tag_names()/add_tag()/set_tags().
"""
from __future__ import annotations

import base64
import json
import os
import stat
from pathlib import Path
from typing import Any, Dict, List, Optional

import keyring
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from .logger import get_logger

log = get_logger(__name__)


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

DEFAULT_NAME_TEMPLATES: List[Dict[str, str]] = [
    {"label": "Название + дата", "template": "{name} — {date}"},
    {"label": "Название + дата и время", "template": "{name} — {date} {time}"},
    {"label": "Проект: название (сокр) — дата",
     "template": "{name} ({abbr}) — {date}"},
    {"label": "Совещание: название — дата",
     "template": "Совещание: {name} — {date}"},
    {"label": "Только название", "template": "{name}"},
]


DEFAULT_CONFIG: Dict[str, Any] = {
    "yandex_vm": {
        # Корневая папка, в подпапках которой лежат конфиги ВМ.
        # Каждая подпапка = одна ВМ.
        # Внутри ожидаются:
        #   schedule.cron  — расписание работы ВМ;
        #   exceptions.txt — исключения (переопределения).
        "root_path": "",
    },
    # --- Проекты (новый формат: name + chat_id) ---
    "projects": [
        {"name": "Россети",     "chat_id": ""},
        {"name": "iserv",       "chat_id": ""},
        {"name": "Внутренние",  "chat_id": ""},
        {"name": "Тестовые",    "chat_id": ""},
    ],
    # --- Справочник тегов ---
    # Каждый тег: {"name": str, "color": "#RRGGBB"}
    # color — необязательный, используется для подсветки в UI.
    "tags": [
        {"name": "важное",       "color": "#C62828"},
        {"name": "риски",        "color": "#EF6C00"},
        {"name": "решения",      "color": "#2E7D32"},
        {"name": "для клиента",  "color": "#1565C0"},
    ],
    # --- Настройки по умолчанию для новых записей ---
    # Имя проекта, которое подставляется в карточку метаданных
    # для новых записей. Если пусто — берётся первый из projects.
    "default_project": "",
    # ID чата Bitrix24, куда отправлять протоколы/summary
    # по умолчанию. Если пусто — берётся чат проекта записи.
    "default_chat_id": "",
    # --- Сотрудники ---
    "employees": [],
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
                    "Выдели ключевые темы и решения. В конце — список "
                    "action items."
                ),
            },
            {
                "name": "Технический созвон",
                "text": (
                    "Составь техническое резюме созвона на русском языке. "
                    "Выдели: обсуждённые технические вопросы, принятые "
                    "решения, команды/конфигурации (если упоминались), "
                    "задачи для разработчиков."
                ),
            },
        ],
        "default_prompt": (
            "Составь краткое содержание записи на русском языке. "
            "Выдели ключевые темы и решения. В конце — список "
            "action items."
        ),
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
        "pause_when_recording": True,
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
        "max_bytes_mb": 10,
        "backup_count": 5,
    },
    "scrum": {
        "prompt_template": DEFAULT_SCRUM_PROMPT,
        "export_format": "docx",
    },
    "summarizer": {
        "enabled": False,
        "provider": "server",
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
                "Отвечай на русском языке, если в инструкции не сказано "
                "иное."
            ),
        },
    },
    "glossary": {
        "terms": [
            {"term": "ЕЖД", "description": "Единый журнал дежурств"},
        ],
        "send_to_summarizer": True,
        "send_to_deepseek": True,
    },
    "bitrix": {
        "enabled": False,
        "webhook_url": "",
        "connect_timeout": 15,
        "read_timeout": 60,
        "default_send": "protocol",
        "include_header": True,
        "system_message": False,
        "disable_url_preview": False,
        "file_message_max_chars": 3000,
        "upload_folder_id": 0,
        "max_message_chars": 15000,
        "retry_count": 3,
        "retry_delay": 2.0,
    },
    "app": {
        "max_file_read_chars": 5_000_000,
        "ffmpeg_start_check_delay": 0.3,
        "ffmpeg_stop_timeout": 10,
        "ffmpeg_kill_timeout": 5,
        "processor_poll_interval": 2.0,
        "processor_retry_check_interval": 300,
        "overlay_hide_delay_ms": 5000,
        "overlay_hide_after_task_ms": 3000,
        "overlay_log_lines": 5,
        "overlay_width": 340,
        "overlay_height": 190,
        "notification_timeout_ms": 4000,
        "notification_max_title": 50,
        "notification_max_message": 100,
        "tooltip_toast_max_width": 420,
        "tooltip_toast_threshold": 120,
        "tooltip_toast_hide_delay_ms": 120,
        "search_default_fuzzy": 82,
        "search_default_context_chars": 400,
        "search_default_max_prompt_hits": 100,
        "fuzzy_max_word_distance": 200,
        "search_cancel_wait_ms": 3000,
    },
}

_SERVICE_NAME = "screen-recorder-app"
_KEYRING_KEY_NAME = "__fernet_key__"
_KEY_FILE_NAME = ".fernet_key"


class ConfigManager:
    """Менеджер конфигурации."""

    def __init__(self, config_path: str | None = None) -> None:
        if config_path is None:
            config_path = os.path.join(
                os.path.expanduser("~"), ".config",
                "screen-recorder", "config.json"
            )
        self.config_path = Path(config_path)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)

        self._key_file = self.config_path.parent / _KEY_FILE_NAME
        self._fernet: Optional[Fernet] = None

        log.info("Инициализация ConfigManager: %s", self.config_path)
        self.config: Dict[str, Any] = self.get_defaults()
        self.load()

    # ------------------------------------------------------------------
    # Шифрование
    # ------------------------------------------------------------------
    def _load_key_from_keyring(self) -> Optional[str]:
        try:
            return keyring.get_password(_SERVICE_NAME, _KEYRING_KEY_NAME)
        except Exception as exc:
            log.debug("keyring недоступен: %s", exc)
            return None

    def _save_key_to_keyring(self, key: str) -> bool:
        try:
            keyring.set_password(_SERVICE_NAME, _KEYRING_KEY_NAME, key)
            return True
        except Exception as exc:
            log.debug("Не удалось сохранить ключ в keyring: %s", exc)
            return False

    def _load_key_from_file(self) -> Optional[str]:
        if not self._key_file.exists():
            return None
        try:
            return self._key_file.read_text(encoding="utf-8").strip() or None
        except Exception as exc:
            log.warning("Не удалось прочитать ключ из %s: %s",
                        self._key_file, exc)
            return None

    def _save_key_to_file(self, key: str) -> bool:
        try:
            self._key_file.write_text(key, encoding="utf-8")
            try:
                os.chmod(self._key_file, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
            return True
        except Exception as exc:
            log.warning("Не удалось сохранить ключ в %s: %s",
                        self._key_file, exc)
            return False

    def _ensure_fernet(self) -> Fernet:
        if self._fernet is not None:
            return self._fernet

        key: Optional[str] = self._load_key_from_keyring()
        if not key:
            key = self._load_key_from_file()
            if key:
                self._save_key_to_keyring(key)

        if not key:
            key = Fernet.generate_key().decode("ascii")
            log.warning(
                "Мастер-ключ шифрования не найден — сгенерирован новый."
            )
            self._save_key_to_keyring(key)
            self._save_key_to_file(key)

        try:
            self._fernet = Fernet(key.encode("ascii"))
        except Exception as exc:
            log.exception("Некорректный мастер-ключ: %s", exc)
            key = Fernet.generate_key().decode("ascii")
            self._save_key_to_keyring(key)
            self._save_key_to_file(key)
            self._fernet = Fernet(key.encode("ascii"))

        return self._fernet

    def get_raw_key(self) -> str:
        key = self._load_key_from_keyring() or self._load_key_from_file()
        if not key:
            self._ensure_fernet()
            key = self._load_key_from_keyring() or self._load_key_from_file()
        return key or ""

    def import_key(self, key_b64: str) -> bool:
        key_b64 = (key_b64 or "").strip()
        if not key_b64:
            return False
        try:
            Fernet(key_b64.encode("ascii"))
        except Exception as exc:
            log.error("import_key: некорректный ключ: %s", exc)
            return False

        self._save_key_to_keyring(key_b64)
        self._save_key_to_file(key_b64)
        self._fernet = Fernet(key_b64.encode("ascii"))
        log.info("Мастер-ключ восстановлен из base64")
        return True

    def encrypt(self, plaintext: str) -> str:
        if not plaintext:
            return ""
        token = self._ensure_fernet().encrypt(plaintext.encode("utf-8"))
        return token.decode("ascii")

    def decrypt(self, token_b64: str) -> str:
        if not token_b64:
            return ""
        try:
            raw = self._ensure_fernet().decrypt(token_b64.encode("ascii"))
            return raw.decode("utf-8")
        except InvalidToken:
            log.error("decrypt: недействительный токен")
            return ""
        except Exception as exc:
            log.error("decrypt: ошибка: %s", exc)
            return ""

    def get_yandex_vm_settings(self) -> Dict[str, Any]:
        cfg = self.config.get("yandex_vm", {}) or {}
        return {
            "root_path": str(cfg.get("root_path", "")).strip(),
        }

    # ------------------------------------------------------------------
    # Загрузка / сохранение
    # ------------------------------------------------------------------
    def get_defaults(self) -> Dict[str, Any]:
        return json.loads(json.dumps(DEFAULT_CONFIG))

    def load(self) -> Dict[str, Any]:
        log.info("Загрузка конфигурации из %s", self.config_path)
        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.config = self._merge(self.get_defaults(), data)
                log.info("Конфигурация загружена (%d секций)",
                         len(self.config))
            except Exception as exc:
                log.error("Ошибка загрузки конфигурации: %s", exc)
                self.config = self.get_defaults()
        else:
            log.info("Файл конфигурации не найден, используются "
                     "значения по умолчанию")
            self.config = self.get_defaults()
        return self.config

    def save(self, config: Dict[str, Any] | None = None) -> None:
        if config is not None:
            self.config = config
        log.info("Сохранение конфигурации в %s", self.config_path)
        try:
            tmp = self.config_path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.config_path)
            log.info("Конфигурация сохранена")
        except Exception as exc:
            log.error("Ошибка сохранения конфигурации: %s", exc)

    @staticmethod
    def _merge(base: Dict[str, Any],
               override: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(base)
        for k, v in override.items():
            if (k in result and isinstance(result[k], dict)
                    and isinstance(v, dict)):
                result[k] = ConfigManager._merge(result[k], v)
            else:
                result[k] = v
        return result

    # ------------------------------------------------------------------
    # Помощники
    # ------------------------------------------------------------------
    def get_prompts(self) -> list[dict]:
        prompts = self.config.get("metadata", {}).get("prompts", [])
        if not isinstance(prompts, list):
            return []
        result = []
        for p in prompts:
            if isinstance(p, dict) and p.get("name") and p.get("text"):
                result.append({"name": str(p["name"]),
                               "text": str(p["text"])})
            elif isinstance(p, str) and p.strip():
                first_line = p.strip().splitlines()[0][:60]
                result.append({"name": first_line, "text": p})
        return result

    def get_default_prompt(self) -> str:
        return self.config.get("metadata", {}).get("default_prompt", "") or ""

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
                result.append({"label": item.strip(),
                               "template": item.strip()})
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
                templates[i] = {"label": label or template,
                                "template": template}
                replaced = True
                break
            elif isinstance(item, str) and item == template:
                templates[i] = {"label": label or template,
                                "template": template}
                replaced = True
                break
        if not replaced:
            templates.append({"label": label or template,
                              "template": template})
        self.save()
        log.info("Шаблон названия %s: %r",
                 "обновлён" if replaced else "добавлен", template)

    def get_queue_settings(self) -> Dict[str, Any]:
        q = self.config.get("queue", {})
        return {
            "auto_retry_enabled": bool(q.get("auto_retry_enabled", True)),
            "retry_interval_minutes": int(q.get("retry_interval_minutes", 5)),
            "max_retries": int(q.get("max_retries", 10)),
            "pause_when_recording": bool(q.get("pause_when_recording", True)),
        }

    def get_log_settings(self) -> Dict[str, Any]:
        cfg = self.config.get("logging", {})
        return {
            "log_path": cfg.get("log_path", "/tmp/screen-recorder/app.log"),
            "level": cfg.get("level", "DEBUG"),
            "max_bytes_mb": int(cfg.get("max_bytes_mb", 10)),
            "backup_count": int(cfg.get("backup_count", 5)),
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
            "prompt_template": cfg.get("prompt_template",
                                       DEFAULT_SCRUM_PROMPT),
            "export_format": cfg.get("export_format", "docx"),
        }

    def get_summarizer_settings(self) -> Dict[str, Any]:
        cfg = self.config.get("summarizer", {}) or {}
        enabled = bool(cfg.get("enabled", False))
        provider = str(cfg.get("provider", "server")).strip().lower()
        if provider not in ("server", "litellm"):
            provider = "server"

        l = cfg.get("litellm", {}) or {}
        litellm = {
            "base_url": str(l.get("base_url",
                                  "http://localhost:4000")).rstrip("/"),
            "api_key": str(l.get("api_key", "")),
            "model": str(l.get("model", "gpt-4o-mini")),
            "temperature": float(l.get("temperature", 0.25)),
            "max_tokens": int(l.get("max_tokens", 2200)),
            "connect_timeout": int(l.get("connect_timeout", 15)),
            "read_timeout": int(l.get("read_timeout", 300)),
            "system_prompt": str(l.get("system_prompt", "")),
        }
        return {"enabled": enabled, "provider": provider, "litellm": litellm}

    def get_glossary_settings(self) -> Dict[str, Any]:
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

    def get_app_settings(self) -> Dict[str, Any]:
        a = self.config.get("app", {}) or {}
        d = DEFAULT_CONFIG["app"]
        return {k: a.get(k, v) for k, v in d.items()}

    # ------------------------------------------------------------------
    # Проекты
    # ------------------------------------------------------------------
    def get_projects(self) -> List[Dict[str, str]]:
        raw = self.config.get("projects", []) or []
        result: List[Dict[str, str]] = []
        for item in raw:
            if isinstance(item, str):
                name = item.strip()
                if name:
                    result.append({"name": name, "chat_id": ""})
            elif isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                chat_id = str(item.get("chat_id") or "").strip()
                result.append({"name": name, "chat_id": chat_id})
        return result

    def get_project_names(self) -> List[str]:
        return [p["name"] for p in self.get_projects()]

    def get_project_chat_id(self, project_name: str) -> str:
        if not project_name:
            return ""
        name = project_name.strip()
        for p in self.get_projects():
            if p["name"] == name:
                return p["chat_id"]
        return ""

    # ------------------------------------------------------------------
    # Проект и чат по умолчанию
    # ------------------------------------------------------------------
    def get_default_project(self) -> str:
        value = str(
            self.config.get("default_project") or ""
        ).strip()
        return value

    def set_default_project(self, name: str) -> None:
        self.config["default_project"] = (name or "").strip()
        self.save()
        log.info("Проект по умолчанию: %r",
                 self.config["default_project"])

    def get_default_chat_id(self) -> str:
        value = str(
            self.config.get("default_chat_id") or ""
        ).strip()
        return value

    def set_default_chat_id(self, chat_id: str) -> None:
        self.config["default_chat_id"] = (chat_id or "").strip()
        self.save()
        log.info("Чат по умолчанию: %r",
                 self.config["default_chat_id"])

    def get_resolved_default_chat_id(self) -> str:
        explicit = self.get_default_chat_id()
        if explicit:
            return explicit
        default_project = self.get_default_project()
        if default_project:
            return self.get_project_chat_id(default_project)
        return ""

    # ------------------------------------------------------------------
    # Сотрудники
    # ------------------------------------------------------------------
    def get_employees(self) -> List[Dict[str, str]]:
        raw = self.config.get("employees", []) or []
        result: List[Dict[str, str]] = []
        for item in raw:
            if isinstance(item, str):
                name = item.strip()
                if name:
                    result.append({"name": name, "chat_id": ""})
            elif isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                chat_id = str(item.get("chat_id") or "").strip()
                result.append({"name": name, "chat_id": chat_id})
        return result

    def get_employee_names(self) -> List[str]:
        return [e["name"] for e in self.get_employees()]

    def get_employee_chat_id(self, employee_name: str) -> str:
        if not employee_name:
            return ""
        name = employee_name.strip()
        for e in self.get_employees():
            if e["name"] == name:
                return e["chat_id"]
        return ""

    # ------------------------------------------------------------------
    # Теги
    # ------------------------------------------------------------------
    def get_tags(self) -> List[Dict[str, str]]:
        """
        Возвращает список тегов из справочника.

        Каждый элемент: {"name": str, "color": str}.
        Поддерживается обратная совместимость: старые записи,
        где tags был просто списком строк, автоматически
        превращаются в словари с пустым цветом.
        """
        raw = self.config.get("tags", []) or []
        result: List[Dict[str, str]] = []
        seen = set()
        for item in raw:
            if isinstance(item, str):
                name = item.strip()
                if name and name not in seen:
                    result.append({"name": name, "color": ""})
                    seen.add(name)
            elif isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                if not name or name in seen:
                    continue
                color = str(item.get("color") or "").strip()
                result.append({"name": name, "color": color})
                seen.add(name)
        return result

    def get_tag_names(self) -> List[str]:
        return [t["name"] for t in self.get_tags()]

    def get_tag_color(self, name: str) -> str:
        if not name:
            return ""
        target = name.strip()
        for t in self.get_tags():
            if t["name"] == target:
                return t.get("color") or ""
        return ""

    def add_tag(self, name: str, color: str = "") -> bool:
        """
        Добавляет тег в справочник. Если тег с таким именем уже
        есть — обновляет цвет. Возвращает True, если что-то
        изменилось.
        """
        name = (name or "").strip()
        if not name:
            return False
        color = (color or "").strip()

        tags = self.config.setdefault("tags", [])
        if not isinstance(tags, list):
            tags = []
            self.config["tags"] = tags

        for i, item in enumerate(tags):
            if isinstance(item, dict):
                existing = str(item.get("name") or "").strip()
            elif isinstance(item, str):
                existing = item.strip()
            else:
                existing = ""
            if existing == name:
                new_item = {"name": name, "color": color}
                if tags[i] != new_item:
                    tags[i] = new_item
                    self.save()
                    log.info("Тег обновлён: %r (цвет=%r)", name, color)
                return True

        tags.append({"name": name, "color": color})
        self.save()
        log.info("Тег добавлен: %r (цвет=%r)", name, color)
        return True

    def set_tags(self, tags: List[Dict[str, str]]) -> None:
        """Полностью заменяет справочник тегов."""
        cleaned: List[Dict[str, str]] = []
        seen = set()
        for item in tags or []:
            if isinstance(item, str):
                name = item.strip()
                color = ""
            elif isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                color = str(item.get("color") or "").strip()
            else:
                continue
            if not name or name in seen:
                continue
            cleaned.append({"name": name, "color": color})
            seen.add(name)
        self.config["tags"] = cleaned
        self.save()
        log.info("Справочник тегов обновлён: %d шт.", len(cleaned))

    # ------------------------------------------------------------------
    # Bitrix24
    # ------------------------------------------------------------------
    def get_bitrix_settings(self) -> Dict[str, Any]:
        cfg = self.config.get("bitrix", {}) or {}
        return {
            "enabled": bool(cfg.get("enabled", False)),
            "webhook_url": str(cfg.get("webhook_url", "")).strip(),
            "connect_timeout": int(cfg.get("connect_timeout", 15)),
            "read_timeout": int(cfg.get("read_timeout", 60)),
            "default_send": str(cfg.get("default_send", "protocol")),
            "include_header": bool(cfg.get("include_header", True)),
            "system_message": bool(cfg.get("system_message", False)),
            "disable_url_preview": bool(cfg.get("disable_url_preview",
                                                False)),
            "file_message_max_chars": int(
                cfg.get("file_message_max_chars", 3000)
            ),
            "upload_folder_id": int(cfg.get("upload_folder_id", 0)),
            "max_message_chars": int(
                cfg.get("max_message_chars", 15000)
            ),
            "retry_count": int(cfg.get("retry_count", 3)),
            "retry_delay": float(cfg.get("retry_delay", 2.0)),
        }