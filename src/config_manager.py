"""Управление настройками с шифрованием паролей.

Изменения:
  • Добавлена секция config["tfs"] — интеграция с TFS / Azure
    DevOps Server (просмотр списка задач). PAT шифруется.
  • Добавлены методы get_tfs_settings(), set_tfs_settings(),
    get_tfs_wiql_templates(), add_tfs_wiql_template(),
    remove_tfs_wiql_template().
  • Сохранены все прежние ключи и методы без изменений.
  • ДОБАВЛЕНО: настройки окна «Скрам» в config["app"]:
      – scrum_window_width, scrum_window_height;
      – scrum_protocol_tab_max_chars;
      – scrum_day_tab_max_records;
      – scrum_left_splitter_ratio;
      – scrum_tfs_max_items.
  • ДОБАВЛЕНО: app.scrum_sort_by_assignee — сортировка таблиц
    «Поручения» и «Задачи TFS» в окне «Скрам» по колонке
    «Исполнитель» (по возрастанию, пустые — в конце).
    Применяется всегда и ко всем обновлениям данных.
  • НОВОЕ: справочник категорий записей (config["categories"]).
    По аналогии с тегами — список словарей {"name": <str>}.
    Методы: get_categories(), get_category_names(),
    set_categories().
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
from .platform_utils import (
    default_config_path,
    default_log_path,
    default_temp_dir,
)

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
DEFAULT_ACTION_ITEMS_PROMPT = (
    "Ты — ассистент, который извлекает поручения из протокола "
    "совещания.\n\n"
    "На вход подаётся стенограмма и/или протокол. "
    "Твоя задача — вернуть ТОЛЬКО валидный JSON без "
    "markdown-обёрток, комментариев и пояснений.\n\n"
    "ФОРМАТ ОТВЕТА (строго):\n"
    "{\n"
    "  \"items\": [\n"
    "    {\n"
    "      \"text\": \"Краткое описание задачи (1-2 предложения)\",\n"
    "      \"assignee\": \"ФИО ответственного\",\n"
    "      \"status\": \"created\",\n"
    "      \"due_date\": \"YYYY-MM-DD\",\n"
    "      \"comment\": \"\"\n"
    "    }\n"
    "  ]\n"
    "}\n\n"
    "ПРАВИЛА:\n"
    "1. Включай ТОЛЬКО реальные поручения — действия, которые "
    "кто-то должен выполнить. Не превращай в поручения статусы "
    "работ, обсуждения, риски, ход мыслей.\n"
    "2. Если в тексте сказано «по итогам месяца», «в конце дня», "
    "«в пятницу», «в среду» — используй дату из контекста "
    "(дата совещания) или добавь дней, если явно указано.\n"
    "3. Если дата не указана — оставь due_date пустой строкой.\n"
    "4. Если ответственный не указан — оставь assignee пустой "
    "строкой.\n"
    "5. Поле status всегда \"created\" — поручение только что "
    "создано.\n"
    "6. Поле text — краткое, по делу, без воды. Сохраняй "
    "ключевые уточнения (номера задач, ссылки на артефакты, "
    "контекст «совместно с…»).\n"
    "7. Если в поручении участвует несколько человек "
    "(«совместно с…», «вместе с…») — в assignee укажи "
    "основного, остальных добавь в comment.\n"
    "8. Никаких комментариев, объяснений, markdown-разметки — "
    "только чистый JSON.\n\n"
    "ПРИМЕР ВХОДА:\n"
    "\"По ПАК-МК: доработать отчёт, чтобы новые виды актов "
    "отображались. Ответственный — Александр Пушкин, срок — "
    "05.10.2026.\"\n\n"
    "ПРИМЕР ВЫХОДА:\n"
    "{\"items\":[{\"text\":\"Доработать отчёт по ПАК-МК, чтобы "
    "новые виды актов отображались.\",\"assignee\":\"Александр "
    "Пушкин\",\"status\":\"created\",\"due_date\":\"2026-10-05\","
    "\"comment\":\"\"}]}\n"
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


# WIQL-запрос по умолчанию для интеграции с TFS.
# Возвращает активные задачи текущего пользователя.
DEFAULT_TFS_WIQL: str = (
    "SELECT [System.Id] FROM WorkItems "
    "WHERE [System.AssignedTo] = @Me "
    "AND [System.State] <> 'Closed' "
    "AND [System.State] <> 'Done' "
    "ORDER BY [System.ChangedDate] DESC"
)


DEFAULT_CONFIG: Dict[str, Any] = {
    "projects": [
        {"name": "Россети",     "chat_id": ""},
        {"name": "iserv",       "chat_id": ""},
        {"name": "Внутренние",  "chat_id": ""},
        {"name": "Тестовые",    "chat_id": ""},
    ],
    "categories": [
        {"name": "Совещание"},
        {"name": "Созвон"},
        {"name": "Встреча с клиентом"},
        {"name": "Внутренняя встреча"},
    ],
    "tags": [
        {"name": "важное",       "color": "#C62828"},
        {"name": "риски",        "color": "#EF6C00"},
        {"name": "решения",      "color": "#2E7D32"},
        {"name": "для клиента",  "color": "#1565C0"},
    ],
    "default_project": "",
    "default_chat_id": "",
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
        "active_service": "default",
        "services": [
            {
                "id": "default",
                "name": "По умолчанию",
                "url": "http://localhost:8000",
                "access_key": "",
                "connect_timeout": 15,
                "read_timeout": 120,
                "max_wait": 7200,
            },
        ],
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
        "temp_path": default_temp_dir(),
        "retention_hours": 24,
    },
    "logging": {
        "log_path": default_log_path(),
        "level": "DEBUG",
        "max_bytes_mb": 10,
        "backup_count": 5,
    },
    "scrum": {
        "prompt_template": DEFAULT_SCRUM_PROMPT,
        "action_items_prompt_template": DEFAULT_ACTION_ITEMS_PROMPT,
        "export_format": "docx",
        "generate_action_items": False,
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
    "sync": {
        "enabled": False,
        "base_url": "",
        "api_key": "",
        "connect_timeout": 15,
        "read_timeout": 120,
        "auto_upload_after_processing": True,
        "auto_pull_enabled": True,
        "auto_pull_interval": 300,
        "sync_page_size": 200,
        "max_artifact_mb": 50,
        "send_video_link": True,
        "allow_delete_local_media_after_upload": False,
        "sync_projects_and_tags": True,
        "delete_local_on_server_delete": False,
        "force_overwrite_on_download": False,
        "send_media_to_server": False,
        "delete_local_media_after_media_upload": False,
        "use_hash_check": True,
        "compress_media_if_too_large": True,
        "compression_min_video_bitrate_kbps": 200,
        "compression_audio_bitrate_kbps": 96,
        "compression_preset": "veryfast",
        "retry_count": 3,
        "retry_delay": 2.0,
    },
    "tfs": {
        "enabled": False,
        "base_url": "",
        "api_key": "",
        "api_version": "7.0",
        "connect_timeout": 15,
        "read_timeout": 60,
        "default_wiql": DEFAULT_TFS_WIQL,
        "default_project": "",
        "max_items": 200,
        "cache_ttl_sec": 60,
        "wiql_templates": [],
    },
    "app": {
        "max_file_read_chars": 5_000_000,
        "processor_poll_interval": 2.0,
        "processor_retry_check_interval": 300,

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
        "media_prefer_builtin_player": True,
        "media_auto_download_from_server": True,
        "media_download_timeout": 600,
        "media_player_window_width": 960,
        "media_player_window_height": 640,
        "attachment_name_max_chars": 50,
        "sync_auto_publish_ready_only": True,
        "sync_publish_on_ready": True,
        "tfs_attachment_format": "csv",
        "tfs_attachment_max_items": 0,
        "tfs_projects_cache_ttl": 300,
        "tfs_save_attachment": True,
        "relations_max_in_dialog": 200,

        # --- Окно «Скрам» ---
        "scrum_window_width": 1920,
        "scrum_window_height": 1024,
        "scrum_protocol_tab_max_chars": 500_000,
        "scrum_day_tab_max_records": 50,
        "scrum_left_panel_width": 700,
        "scrum_tasks_panel_height": 380,
        "scrum_tfs_max_items": 200,
        "scrum_always_on_top": True,
        "scrum_day_view_mode": "summary",
        # --- НОВОЕ: сортировка по исполнителю ---
        "scrum_sort_by_assignee": True,
        # --- НОВОЕ: панель управления TFS в окне «Скрам» ---
        "scrum_tfs_panel_collapsed_by_default": True,
    }
}

_SERVICE_NAME = "screen-recorder-app"
_KEYRING_KEY_NAME = "__fernet_key__"
_KEY_FILE_NAME = ".fernet_key"


class ConfigManager:
    """Менеджер конфигурации."""

    def __init__(self, config_path: str | None = None) -> None:
        if config_path is None:
            config_path = default_config_path()
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

    def get_tfs_projects_cached(
        self, *, force: bool = False,
    ) -> Dict[str, Any]:
        """Возвращает кэшированный список проектов TFS."""
        import time

        cfg = self.get_tfs_settings()
        if not (cfg.get("enabled")
                and cfg.get("base_url")
                and cfg.get("api_key")):
            return {
                "ok": False,
                "projects": [],
                "cached_at": 0.0,
                "error": "TFS не настроен",
            }

        cache = self.config.setdefault("tfs_projects_cache", {})
        if not isinstance(cache, dict):
            cache = {}
            self.config["tfs_projects_cache"] = cache

        ttl = int(
            self.config.get("app", {}).get(
                "tfs_projects_cache_ttl", 300
            )
        )
        cached_at = float(cache.get("cached_at") or 0.0)
        now = time.time()

        cached_projects = list(cache.get("projects") or [])

        if (not force and cached_projects
                and ttl > 0 and (now - cached_at) < ttl):
            return {
                "ok": True,
                "projects": cached_projects,
                "cached_at": cached_at,
                "error": "",
            }

        try:
            from .tfs_client import TfsClient, TfsError
            import asyncio as _asyncio

            async def _fetch():
                async with TfsClient(
                    base_url=cfg["base_url"],
                    pat=cfg["api_key"],
                    api_version=cfg.get("api_version", "7.0"),
                    connect_timeout=float(
                        cfg.get("connect_timeout", 15)
                    ),
                    read_timeout=float(
                        cfg.get("read_timeout", 60)
                    ),
                ) as client:
                    return await client.list_projects()

            projects_raw = _asyncio.run(_fetch())
            names = [
                str(p.get("name") or "")
                for p in projects_raw
                if p.get("name")
            ]
            names.sort()

            cache["projects"] = names
            cache["cached_at"] = now
            self.save()

            log.info(
                "Список проектов TFS обновлён: %d", len(names)
            )
            return {
                "ok": True,
                "projects": names,
                "cached_at": now,
                "error": "",
            }
        except Exception as exc:
            log.warning(
                "Не удалось получить проекты TFS: %s", exc
            )
            return {
                "ok": bool(cached_projects),
                "projects": cached_projects,
                "cached_at": cached_at,
                "error": str(exc),
            }

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
            "log_path": cfg.get("log_path", default_log_path()),
            "level": cfg.get("level", "DEBUG"),
            "max_bytes_mb": int(cfg.get("max_bytes_mb", 10)),
            "backup_count": int(cfg.get("backup_count", 5)),
        }

    def get_transcribe_settings(self) -> Dict[str, Any]:
        """Возвращает настройки АКТИВНОГО сервиса транскрибации."""
        cfg = self.config.get("transcribe", {}) or {}

        services = cfg.get("services") or []
        if not isinstance(services, list):
            services = []

        active_id = str(cfg.get("active_service") or "").strip()

        if not services:
            legacy_url = str(cfg.get("url") or "").strip()
            legacy_key = str(cfg.get("access_key") or "")
            services = [{
                "id": "default",
                "name": "По умолчанию",
                "url": legacy_url,
                "access_key": legacy_key,
                "connect_timeout": int(
                    cfg.get("connect_timeout", 15)
                ),
                "read_timeout": int(cfg.get("read_timeout", 120)),
                "max_wait": int(cfg.get("max_wait", 7200)),
            }]
            active_id = "default"

        active: Optional[Dict[str, Any]] = None
        for s in services:
            if not isinstance(s, dict):
                continue
            if str(s.get("id") or "") == active_id:
                active = s
                break

        if active is None:
            for s in services:
                if isinstance(s, dict) and str(s.get("id") or ""):
                    active = s
                    active_id = str(s.get("id") or "")
                    break

        if active is None:
            return {
                "service_id": "",
                "service_name": "",
                "url": "",
                "access_key": "",
                "connect_timeout": 15,
                "read_timeout": 120,
                "max_wait": 7200,
                "services": [],
            }

        return {
            "service_id": active_id,
            "service_name": str(active.get("name") or ""),
            "url": str(active.get("url") or "").strip(),
            "access_key": str(active.get("access_key") or ""),
            "connect_timeout": int(
                active.get("connect_timeout", 15)
            ),
            "read_timeout": int(
                active.get("read_timeout", 120)
            ),
            "max_wait": int(active.get("max_wait", 7200)),
            "services": [
                {
                    "id": str(s.get("id") or ""),
                    "name": str(s.get("name") or ""),
                    "url": str(s.get("url") or "").strip(),
                    "access_key": str(s.get("access_key") or ""),
                    "connect_timeout": int(
                        s.get("connect_timeout", 15)
                    ),
                    "read_timeout": int(
                        s.get("read_timeout", 120)
                    ),
                    "max_wait": int(s.get("max_wait", 7200)),
                }
                for s in services
                if isinstance(s, dict)
            ],
        }

    def set_transcribe_settings(
        self, settings: Dict[str, Any]
    ) -> None:
        raw_services = settings.get("services") or []
        if not isinstance(raw_services, list):
            raw_services = []

        cleaned: List[Dict[str, Any]] = []
        seen_ids: set = set()

        for s in raw_services:
            if not isinstance(s, dict):
                continue
            sid = str(s.get("id") or "").strip()
            name = str(s.get("name") or "").strip()
            url = str(s.get("url") or "").strip()
            if not url:
                continue
            if not sid:
                sid = f"service_{len(cleaned) + 1}"
            base_id = sid
            n = 1
            while sid in seen_ids:
                n += 1
                sid = f"{base_id}_{n}"
            seen_ids.add(sid)

            cleaned.append({
                "id": sid,
                "name": name or sid,
                "url": url,
                "access_key": str(s.get("access_key") or ""),
                "connect_timeout": int(
                    s.get("connect_timeout", 15)
                ),
                "read_timeout": int(
                    s.get("read_timeout", 120)
                ),
                "max_wait": int(s.get("max_wait", 7200)),
            })

        active = str(
            settings.get("active_service") or ""
        ).strip()

        if active not in seen_ids:
            active = cleaned[0]["id"] if cleaned else ""

        cfg = self.config.setdefault("transcribe", {})

        for legacy in ("url", "access_key", "connect_timeout",
                       "read_timeout", "max_wait"):
            cfg.pop(legacy, None)

        cfg["active_service"] = active
        cfg["services"] = cleaned

        self.save()
        log.info(
            "Настройки транскрибации сохранены: services=%d, "
            "active=%r",
            len(cleaned), active,
        )

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

    def get_sessions_root(self) -> str:
        temp_path = (
            self.config.get("storage", {}).get(
                "temp_path", default_temp_dir()
            )
        )
        return os.path.join(temp_path, "sessions")

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
    # Категории (справочник)
    # ------------------------------------------------------------------
    def get_categories(self) -> List[Dict[str, str]]:
        """
        Возвращает список категорий записей.

        Формат каждого элемента: {"name": <str>}.
        Совместим с форматом projects/tags — на случай, если
        в старых конфигах хранились словари с другими ключами.
        """
        raw = self.config.get("categories", []) or []
        result: List[Dict[str, str]] = []
        seen = set()

        for item in raw:
            if isinstance(item, str):
                name = item.strip()
                if name and name not in seen:
                    result.append({"name": name})
                    seen.add(name)
            elif isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                if not name or name in seen:
                    continue
                result.append({"name": name})
                seen.add(name)

        return result

    def get_category_names(self) -> List[str]:
        """Возвращает список названий категорий (для ComboBox)."""
        return [c["name"] for c in self.get_categories()]

    def set_categories(
        self, categories: List[Dict[str, str]],
    ) -> None:
        """
        Сохраняет справочник категорий.

        Принимает список словарей {"name": ...} или список строк.
        Пустые и дублирующиеся имена отбрасываются.
        """
        cleaned: List[Dict[str, str]] = []
        seen = set()

        for item in categories or []:
            if isinstance(item, str):
                name = item.strip()
            elif isinstance(item, dict):
                name = str(item.get("name") or "").strip()
            else:
                continue
            if not name or name in seen:
                continue
            cleaned.append({"name": name})
            seen.add(name)

        self.config["categories"] = cleaned
        self.save()
        log.info(
            "Справочник категорий обновлён: %d шт.", len(cleaned)
        )

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

    def set_projects(self, projects: List[Dict[str, str]]) -> None:
        cleaned: List[Dict[str, str]] = []
        seen = set()
        for item in projects or []:
            if isinstance(item, str):
                name = item.strip()
                chat_id = ""
            elif isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                chat_id = str(item.get("chat_id") or "").strip()
            else:
                continue
            if not name or name in seen:
                continue
            cleaned.append({"name": name, "chat_id": chat_id})
            seen.add(name)
        self.config["projects"] = cleaned
        self.save()
        log.info("Справочник проектов обновлён: %d шт.", len(cleaned))

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

    # ------------------------------------------------------------------
    # Синхронизация с удалённым сервером (screc-server)
    # ------------------------------------------------------------------
    def get_sync_settings(self) -> Dict[str, Any]:
        cfg = self.config.get("sync", {}) or {}

        raw_key = str(cfg.get("api_key", "") or "")
        if raw_key.startswith("enc:"):
            api_key = self.decrypt(raw_key[4:])
        else:
            api_key = raw_key

        return {
            "enabled": bool(cfg.get("enabled", False)),
            "base_url": str(cfg.get("base_url", "")).strip().rstrip("/"),
            "api_key": api_key,
            "connect_timeout": float(cfg.get("connect_timeout", 15)),
            "read_timeout": float(cfg.get("read_timeout", 120)),
            "auto_upload_after_processing": bool(
                cfg.get("auto_upload_after_processing", True)
            ),
            "auto_pull_enabled": bool(
                cfg.get("auto_pull_enabled", True)
            ),
            "auto_pull_interval": int(
                cfg.get("auto_pull_interval", 300)
            ),
            "sync_page_size": int(cfg.get("sync_page_size", 200)),
            "max_artifact_mb": int(cfg.get("max_artifact_mb", 50)),
            "send_video_link": bool(cfg.get("send_video_link", True)),
            "allow_delete_local_media_after_upload": bool(
                cfg.get("allow_delete_local_media_after_upload", False)
            ),
            "sync_projects_and_tags": bool(
                cfg.get("sync_projects_and_tags", True)
            ),
            "delete_local_on_server_delete": bool(
                cfg.get("delete_local_on_server_delete", False)
            ),
            "force_overwrite_on_download": bool(
                cfg.get("force_overwrite_on_download", False)
            ),
            "send_media_to_server": bool(
                cfg.get("send_media_to_server", False)
            ),
            "delete_local_media_after_media_upload": bool(
                cfg.get(
                    "delete_local_media_after_media_upload", False
                )
            ),
            "use_hash_check": bool(
                cfg.get("use_hash_check", True)
            ),
            "compress_media_if_too_large": bool(
                cfg.get("compress_media_if_too_large", True)
            ),
            "compression_min_video_bitrate_kbps": int(
                cfg.get("compression_min_video_bitrate_kbps", 200)
            ),
            "compression_audio_bitrate_kbps": int(
                cfg.get("compression_audio_bitrate_kbps", 96)
            ),
            "compression_preset": str(
                cfg.get("compression_preset", "veryfast")
            ),
            "retry_count": int(cfg.get("retry_count", 3)),
            "retry_delay": float(cfg.get("retry_delay", 2.0)),
        }

    def set_sync_settings(self, settings: Dict[str, Any]) -> None:
        cfg = self.config.setdefault("sync", {})

        api_key = str(settings.get("api_key", "") or "")
        if api_key:
            cfg["api_key"] = "enc:" + self.encrypt(api_key)
        else:
            cfg["api_key"] = ""

        for key in (
            "enabled", "base_url",
            "connect_timeout", "read_timeout",
            "auto_upload_after_processing", "auto_pull_enabled",
            "auto_pull_interval", "sync_page_size",
            "max_artifact_mb", "send_video_link",
            "allow_delete_local_media_after_upload",
            "sync_projects_and_tags",
            "delete_local_on_server_delete",
            "force_overwrite_on_download",
            "send_media_to_server",
            "delete_local_media_after_media_upload",
            "use_hash_check",
            "retry_count", "retry_delay",
            "compress_media_if_too_large",
            "compression_min_video_bitrate_kbps",
            "compression_audio_bitrate_kbps",
            "compression_preset",
        ):
            if key in settings:
                cfg[key] = settings[key]

        self.save()
        log.info(
            "Настройки синхронизации сохранены: enabled=%s, "
            "base_url=%r, auto_upload=%s, auto_pull=%s, "
            "send_media=%s, delete_local_on_delete=%s, "
            "force_overwrite=%s, use_hash_check=%s",
            cfg.get("enabled"), cfg.get("base_url"),
            cfg.get("auto_upload_after_processing"),
            cfg.get("auto_pull_enabled"),
            cfg.get("send_media_to_server"),
            cfg.get("delete_local_on_server_delete"),
            cfg.get("force_overwrite_on_download"),
            cfg.get("use_hash_check"),
        )

    def is_sync_enabled(self) -> bool:
        cfg = self.get_sync_settings()
        return bool(
            cfg.get("enabled")
            and cfg.get("base_url")
            and cfg.get("api_key")
        )

    # ------------------------------------------------------------------
    # TFS / Azure DevOps Server
    # ------------------------------------------------------------------
    def get_tfs_settings(self) -> Dict[str, Any]:
        cfg = self.config.get("tfs", {}) or {}

        raw_key = str(cfg.get("api_key", "") or "")
        if raw_key.startswith("enc:"):
            api_key = self.decrypt(raw_key[4:])
        else:
            api_key = raw_key

        templates_raw = cfg.get("wiql_templates", [])
        if not isinstance(templates_raw, list):
            templates_raw = []

        templates: List[Dict[str, Any]] = []
        for item in templates_raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            wiql = str(item.get("wiql") or "").strip()
            if not name or not wiql:
                continue

            vars_raw = item.get("variables")
            variables: Dict[str, str] = {}
            if isinstance(vars_raw, dict):
                for k, v in vars_raw.items():
                    k_str = str(k or "").strip()
                    if not k_str:
                        continue
                    variables[k_str] = str(v if v is not None else "")

            templates.append({
                "name": name,
                "wiql": wiql,
                "variables": variables,
            })

        return {
            "enabled": bool(cfg.get("enabled", False)),
            "base_url": str(cfg.get("base_url", "")).strip().rstrip("/"),
            "api_key": api_key,
            "api_version": str(cfg.get("api_version", "7.0")).strip()
                           or "7.0",
            "connect_timeout": float(cfg.get("connect_timeout", 15)),
            "read_timeout": float(cfg.get("read_timeout", 60)),
            "default_wiql": str(
                cfg.get("default_wiql", DEFAULT_TFS_WIQL)
            ),
            "default_project": str(
                cfg.get("default_project", "") or ""
            ).strip(),
            "max_items": int(cfg.get("max_items", 200)),
            "cache_ttl_sec": int(cfg.get("cache_ttl_sec", 60)),
            "wiql_templates": templates,
        }

    def set_tfs_settings(self, settings: Dict[str, Any]) -> None:
        cfg = self.config.setdefault("tfs", {})

        api_key = str(settings.get("api_key", "") or "")
        if api_key:
            cfg["api_key"] = "enc:" + self.encrypt(api_key)
        else:
            cfg["api_key"] = ""

        for key in (
            "enabled", "base_url", "api_version",
            "connect_timeout", "read_timeout",
            "default_wiql", "default_project",
            "max_items", "cache_ttl_sec",
        ):
            if key in settings:
                cfg[key] = settings[key]

        if "wiql_templates" in settings:
            templates_in = settings.get("wiql_templates") or []
            cleaned: List[Dict[str, Any]] = []
            for item in templates_in:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                wiql = str(item.get("wiql") or "").strip()
                if not name or not wiql:
                    continue

                vars_raw = item.get("variables")
                variables: Dict[str, str] = {}
                if isinstance(vars_raw, dict):
                    for k, v in vars_raw.items():
                        k_str = str(k or "").strip()
                        if not k_str:
                            continue
                        variables[k_str] = str(
                            v if v is not None else ""
                        )

                cleaned.append({
                    "name": name,
                    "wiql": wiql,
                    "variables": variables,
                })
            cfg["wiql_templates"] = cleaned

        self.save()
        log.info(
            "Настройки TFS сохранены: enabled=%s, base_url=%r, "
            "api_version=%s, default_project=%r, templates=%d",
            cfg.get("enabled"), cfg.get("base_url"),
            cfg.get("api_version"),
            cfg.get("default_project"),
            len(cfg.get("wiql_templates") or []),
        )

    def is_tfs_enabled(self) -> bool:
        cfg = self.get_tfs_settings()
        return bool(
            cfg.get("enabled")
            and cfg.get("base_url")
            and cfg.get("api_key")
        )

    def get_tfs_wiql_templates(self) -> List[Dict[str, str]]:
        return self.get_tfs_settings().get("wiql_templates", [])

    def add_tfs_wiql_template(
        self,
        name: str,
        wiql: str,
        variables: Optional[Dict[str, str]] = None,
    ) -> bool:
        name = (name or "").strip()
        wiql = (wiql or "").strip()
        if not name or not wiql:
            return False

        vars_clean: Dict[str, str] = {}
        if isinstance(variables, dict):
            for k, v in variables.items():
                k_str = str(k or "").strip()
                if not k_str:
                    continue
                vars_clean[k_str] = str(v if v is not None else "")

        cfg = self.config.setdefault("tfs", {})
        templates = cfg.setdefault("wiql_templates", [])
        if not isinstance(templates, list):
            templates = []
            cfg["wiql_templates"] = templates

        replaced = False
        for i, item in enumerate(templates):
            if not isinstance(item, dict):
                continue
            if str(item.get("name") or "").strip() == name:
                templates[i] = {
                    "name": name,
                    "wiql": wiql,
                    "variables": vars_clean,
                }
                replaced = True
                break

        if not replaced:
            templates.append({
                "name": name,
                "wiql": wiql,
                "variables": vars_clean,
            })

        self.save()
        log.info(
            "WIQL-шаблон TFS %s: %r (%d символов, дефолтов: %d)",
            "обновлён" if replaced else "добавлен",
            name, len(wiql), len(vars_clean),
        )
        return True

    def set_tfs_wiql_template_variables(
        self,
        name: str,
        variables: Dict[str, str],
    ) -> bool:
        name = (name or "").strip()
        if not name:
            return False

        vars_clean: Dict[str, str] = {}
        if isinstance(variables, dict):
            for k, v in variables.items():
                k_str = str(k or "").strip()
                if not k_str:
                    continue
                vars_clean[k_str] = str(v if v is not None else "")

        cfg = self.config.setdefault("tfs", {})
        templates = cfg.get("wiql_templates") or []
        if not isinstance(templates, list):
            return False

        for i, item in enumerate(templates):
            if not isinstance(item, dict):
                continue
            if str(item.get("name") or "").strip() == name:
                templates[i] = {
                    "name": item.get("name") or name,
                    "wiql": item.get("wiql") or "",
                    "variables": vars_clean,
                }
                self.save()
                log.info(
                    "WIQL-шаблон %r: обновлены дефолты "
                    "переменных (%d шт.)",
                    name, len(vars_clean),
                )
                return True

        log.warning(
            "set_tfs_wiql_template_variables: шаблон %r не найден",
            name,
        )
        return False

    def remove_tfs_wiql_template(self, name: str) -> bool:
        name = (name or "").strip()
        if not name:
            return False

        cfg = self.config.setdefault("tfs", {})
        templates = cfg.get("wiql_templates") or []
        if not isinstance(templates, list):
            return False

        before = len(templates)
        cfg["wiql_templates"] = [
            item for item in templates
            if not (isinstance(item, dict)
                    and str(item.get("name") or "").strip() == name)
        ]
        if len(cfg["wiql_templates"]) == before:
            return False

        self.save()
        log.info("WIQL-шаблон TFS удалён: %r", name)
        return True