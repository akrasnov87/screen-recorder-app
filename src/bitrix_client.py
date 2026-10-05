"""Клиент Bitrix24 для отправки сообщений в чаты через вебхук.

Изменения:
  • Лимит длины сообщения теперь настраивается через параметр
    конструктора max_message_chars (значение из config["bitrix"]).
  • _truncate — метод класса, а не модульная функция.
  • Добавлен параметр override_filename в upload_file и
    send_file_message: позволяет передать пользовательское имя
    файла (например, заголовок протокола) с сохранением
    расширения. Имя транслитерируется в латиницу и очищается
    от недопустимых символов через _sanitize_display_name.
  • В send_file_message добавлен fallback для пустого MESSAGE:
    Bitrix24 не принимает пустой текст даже при наличии FILES
    (ошибка EMPTY_MESSAGE / MESSAGE_EMPTY). Если comment пустой —
    в MESSAGE подставляется имя первого файла (после
    override_filename), чтобы сообщение не было пустым.
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

import aiohttp

from .logger import get_logger

log = get_logger(__name__)


class Bitrix24Error(RuntimeError):
    """Ошибка при обращении к Bitrix24."""
    def __init__(self, message: str, status: int = 0, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


# Транслитерация кириллицы → латиница для имён файлов Bitrix24.
_TRANSLIT_MAP = {
    "а": "a",  "б": "b",  "в": "v",  "г": "g",  "д": "d",
    "е": "e",  "ё": "e",  "ж": "zh", "з": "z",  "и": "i",
    "й": "y",  "к": "k",  "л": "l",  "м": "m",  "н": "n",
    "о": "o",  "п": "p",  "р": "r",  "с": "s",  "т": "t",
    "у": "u",  "ф": "f",  "х": "h",  "ц": "ts", "ч": "ch",
    "ш": "sh", "щ": "sch", "ъ": "",   "ы": "y",  "ь": "",
    "э": "e",  "ю": "yu", "я": "ya",
    "А": "A",  "Б": "B",  "В": "V",  "Г": "G",  "Д": "D",
    "Е": "E",  "Ё": "E",  "Ж": "Zh", "З": "Z",  "И": "I",
    "Й": "Y",  "К": "K",  "Л": "L",  "М": "M",  "Н": "N",
    "О": "O",  "П": "P",  "Р": "R",  "С": "S",  "Т": "T",
    "У": "U",  "Ф": "F",  "Х": "H",  "Ц": "Ts", "Ч": "Ch",
    "Ш": "Sh", "Щ": "Sch", "Ъ": "",   "Ы": "Y",  "Ь": "",
    "Э": "E",  "Ю": "Yu", "Я": "Ya",
}


# Символы, недопустимые в именах файлов (в т.ч. Windows).
_BAD_FILENAME_CHARS = '<>:"/\\|?*\n\r\t'


def _to_ascii_filename(name: str) -> str:
    """Приводит имя файла к ASCII-only."""
    if not name:
        return "file"

    out_chars = []
    for ch in name:
        if ch in _TRANSLIT_MAP:
            out_chars.append(_TRANSLIT_MAP[ch])
        elif ch.isascii() and (ch.isalnum() or ch in "._- "):
            out_chars.append(ch)
        else:
            out_chars.append("_")

    result = "".join(out_chars).strip()
    while "__" in result:
        result = result.replace("__", "_")
    while "  " in result:
        result = result.replace("  ", " ")

    return result or "file"


def _sanitize_display_name(name: str, ext: str) -> str:
    """
    Приводит пользовательское имя (например, заголовок протокола)
    к безопасному для файловой системы виду.

    Шаги:
      1. Определяем расширение. Если в name уже есть ext в конце —
         не дублируем. Иначе добавляем ext.
      2. Убираем недопустимые символы (заменяем на "_").
      3. Схлопываем пробелы, убираем ведущие/замыкающие пробелы
         и точки.
      4. Транслитерируем кириллицу в латиницу через
         _to_ascii_filename.
      5. Обрезаем по границе UTF-8 с сохранением расширения.

    Args:
        name: заголовок или имя файла (может содержать недопустимые
              символы).
        ext:  расширение с точкой, например ".docx".

    Returns:
        Безопасное имя файла. Если ничего не осталось — "document<ext>".
    """
    suffix = ext or ".bin"

    if not name:
        return f"document{suffix}"

    # 1. Не дублируем расширение.
    name_ext = os.path.splitext(name)[1].lower()
    if name_ext and name_ext == suffix.lower():
        stem = name[: -len(name_ext)]
    else:
        stem = name

    # 2. Заменяем недопустимые символы.
    cleaned = "".join(
        ("_" if c in _BAD_FILENAME_CHARS else c) for c in stem
    )

    # 3. Схлопываем пробелы и убираем мусор по краям.
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")

    if not cleaned:
        cleaned = "document"

    # 3a. Заменяем пробелы на "_", чтобы Bitrix24 не превращал их
    #     в "%20" в URL файла.
    cleaned = cleaned.replace(" ", "_")

    # 4. Транслитерация.
    cleaned = _to_ascii_filename(cleaned)

    # 4a. Убираем возможные двойные подчёркивания и мусор по краям
    #     (после транслита могли появиться лишние "_").
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    cleaned = cleaned.strip(" ._-")

    if not cleaned:
        cleaned = "document"

    return f"{cleaned}{suffix}"


class Bitrix24Client:
    """
    Асинхронный клиент Bitrix24 для отправки сообщений и файлов в чат.

    Использование:
        async with Bitrix24Client(webhook_url) as client:
            await client.send_message("chat2101", "Текст")
            await client.send_file_message(
                "chat2101",
                ["/path/a.docx", "/path/b.md"],
                comment="Протокол + summary",
            )
    """

    def __init__(
        self,
        webhook_url: str,
        connect_timeout: float = 15.0,
        read_timeout: float = 60.0,
        max_message_chars: int = 15000,
    ) -> None:
        if not webhook_url:
            raise Bitrix24Error("Не задан webhook_url для Bitrix24")
        self.webhook_url = webhook_url.rstrip("/")
        self._connect_timeout = float(connect_timeout)
        self._read_timeout = float(read_timeout)
        self._max_message_chars = int(max_message_chars)
        self._session: Optional[aiohttp.ClientSession] = None

        log.debug(
            "Bitrix24Client создан: url=%s, connect=%.1f, read=%.1f, "
            "max_message=%d",
            self.webhook_url, self._connect_timeout, self._read_timeout,
            self._max_message_chars,
        )

    # ------------------------------------------------------------------
    # Контекстный менеджер
    # ------------------------------------------------------------------
    async def __aenter__(self) -> "Bitrix24Client":
        timeout = aiohttp.ClientTimeout(
            total=None,
            connect=self._connect_timeout,
            sock_connect=self._connect_timeout,
            sock_read=self._read_timeout,
        )
        self._session = aiohttp.ClientSession(timeout=timeout)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # Низкоуровневый вызов
    # ------------------------------------------------------------------
    async def _read_body(self, resp: aiohttp.ClientResponse) -> str:
        try:
            text = await resp.text()
        except Exception:
            return ""
        text = text.strip()
        return (text[:500] + "…") if len(text) > 500 else text

    def _truncate(self, text: str) -> str:
        """Обрезает текст до self._max_message_chars, добавляя многоточие."""
        limit = self._max_message_chars
        if len(text) <= limit:
            return text
        return text[:limit] + (
            "\n\n… (сообщение обрезано — превышен лимит Bitrix24)"
        )

    async def call(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST {webhook}/{method}.json с JSON-телом."""
        if self._session is None:
            raise Bitrix24Error("aiohttp-сессия не открыта")

        url = f"{self.webhook_url}/{method}.json"
        payload = params or {}
        log.info("Bitrix24: вызов %s, параметров=%d", method, len(payload))

        try:
            async with self._session.post(url, json=payload) as resp:
                if resp.status >= 400:
                    body = await self._read_body(resp)
                    msg = f"Bitrix24: HTTP {resp.status} от {url}"
                    if body:
                        msg += f" — {body}"
                    log.error(msg)
                    raise Bitrix24Error(msg, status=resp.status, body=body)

                try:
                    data = await resp.json()
                except Exception as exc:
                    body = await self._read_body(resp)
                    msg = (
                        f"Bitrix24: не удалось разобрать JSON "
                        f"(HTTP {resp.status}): {exc}. Тело: {body}"
                    )
                    log.error(msg)
                    raise Bitrix24Error(msg, status=resp.status, body=body)

                if isinstance(data, dict) and data.get("error"):
                    descr = data.get("error_description") or data.get("error")
                    msg = f"Bitrix24 API: {descr}"
                    log.error(msg)
                    raise Bitrix24Error(
                        msg, status=resp.status, body=str(data)[:500]
                    )

                return data
        except Bitrix24Error:
            raise
        except aiohttp.ClientConnectorError as exc:
            msg = f"Bitrix24: ошибка подключения: {exc}"
            log.error(msg)
            raise Bitrix24Error(msg)
        except aiohttp.ServerTimeoutError:
            msg = f"Bitrix24: таймаут ({self._read_timeout:.0f} с)"
            log.error(msg)
            raise Bitrix24Error(msg)
        except Exception as exc:
            log.exception("Bitrix24: ошибка запроса: %s", exc)
            raise Bitrix24Error(f"Bitrix24: {exc}")

    # ------------------------------------------------------------------
    # Отправка текстового сообщения
    # ------------------------------------------------------------------
    async def send_message(
        self,
        dialog_id: str,
        text: str,
        *,
        system: bool = False,
        url_preview: bool = True,
    ) -> Dict[str, Any]:
        """Отправляет текстовое сообщение в чат/диалог."""
        if not dialog_id:
            raise Bitrix24Error("Не указан dialog_id для отправки")
        if not text or not text.strip():
            raise Bitrix24Error("Пустой текст сообщения")

        safe_text = self._truncate(text)
        params = {
            "DIALOG_ID": dialog_id,
            "MESSAGE": safe_text,
            "SYSTEM": "Y" if system else "N",
            "URL_PREVIEW": "Y" if url_preview else "N",
        }
        return await self.call("im.message.add", params)

    # ------------------------------------------------------------------
    # Диск Bitrix24
    # ------------------------------------------------------------------
    async def list_storages(self) -> List[Dict[str, Any]]:
        """disk.storage.getlist — список доступных хранилищ."""
        data = await self.call("disk.storage.getlist", {})
        result = data.get("result") or []
        return result if isinstance(result, list) else []

    async def list_folder_children(
        self, folder_id: int,
    ) -> List[Dict[str, Any]]:
        """disk.folder.getchildren — содержимое папки."""
        data = await self.call(
            "disk.folder.getchildren", {"id": int(folder_id)}
        )
        result = data.get("result") or []
        return result if isinstance(result, list) else []

    @staticmethod
    def _extract_root_id(storage: Dict[str, Any]) -> int:
        """Достаёт ID корневой папки из хранилища."""
        root_id = storage.get("ROOT_OBJECT_ID")
        if root_id is not None:
            try:
                return int(root_id)
            except (TypeError, ValueError):
                pass

        root = storage.get("ROOT_OBJECT")
        if isinstance(root, dict):
            rid = root.get("ID")
            if rid is not None:
                try:
                    return int(rid)
                except (TypeError, ValueError):
                    pass

        return 0

    async def get_root_folder_id(self) -> int:
        """Возвращает ID корневой папки первого доступного хранилища."""
        try:
            storages = await self.list_storages()
        except Bitrix24Error as exc:
            log.warning("Bitrix24: disk.storage.getlist не сработал: %s", exc)
            return 0

        if not storages:
            log.warning(
                "Bitrix24: список хранилищ пуст. "
                "Проверьте права вебхука (нужен disk)."
            )
            return 0

        for s in storages:
            etype = (s.get("ENTITY_TYPE") or "").lower()
            if etype in ("shared", "common"):
                fid = self._extract_root_id(s)
                if fid:
                    log.info(
                        "Bitrix24: корневая папка общего диска id=%s "
                        "(ENTITY_TYPE=%s)", fid, etype,
                    )
                    return fid

        for s in storages:
            fid = self._extract_root_id(s)
            if fid:
                log.info(
                    "Bitrix24: корневая папка id=%s "
                    "(из первого доступного хранилища, ENTITY_TYPE=%s)",
                    fid, s.get("ENTITY_TYPE"),
                )
                return fid

        log.warning(
            "Bitrix24: не удалось определить ID корневой папки. "
            "Ответ: %s", str(storages)[:500],
        )
        return 0

    async def get_chat_folder_id(self, dialog_id: str) -> int:
        """Возвращает ID папки на Диске, привязанной к чату."""
        if not dialog_id:
            return 0

        raw = dialog_id.strip()
        if raw.lower().startswith("chat"):
            raw = raw[4:]
        raw = raw.strip()

        if not raw.isdigit():
            log.warning(
                "Bitrix24: не удалось разобрать CHAT_ID из %r",
                dialog_id,
            )
            return 0

        chat_id_int = int(raw)

        try:
            data = await self.call(
                "im.disk.folder.get",
                {"CHAT_ID": chat_id_int},
            )
            result = data.get("result") or {}
            folder_id = result.get("ID")
            if folder_id:
                log.info(
                    "Bitrix24: папка чата %s найдена, id=%s",
                    dialog_id, folder_id,
                )
                return int(folder_id)
            log.warning(
                "Bitrix24: папка чата %s не найдена. Ответ: %s",
                dialog_id, str(data)[:300],
            )
        except Bitrix24Error as exc:
            if exc.status == 403:
                log.info(
                    "Bitrix24: вебхук не имеет доступа к папке чата %s "
                    "(ACCESS_ERROR). Файл будет загружен в корень "
                    "общего диска.", dialog_id,
                )
            else:
                log.warning(
                    "Bitrix24: не удалось получить папку чата %s: %s",
                    dialog_id, exc,
                )
        return 0

    async def commit_file_to_chat(
        self,
        dialog_id: str,
        disk_file_ids: Any,
    ) -> List[int]:
        """Прикрепляет файлы с Диска к чату через im.disk.file.commit."""
        if isinstance(disk_file_ids, (int, str)):
            ids_list = [int(disk_file_ids)]
        else:
            ids_list = [int(x) for x in (disk_file_ids or [])]

        if not ids_list:
            return []

        raw = dialog_id.strip()
        if raw.lower().startswith("chat"):
            raw = raw[4:]
        raw = raw.strip()

        params: Dict[str, Any] = {
            "UPLOAD_ID": ids_list[0] if len(ids_list) == 1 else ids_list,
        }
        if raw.isdigit():
            params["CHAT_ID"] = int(raw)
        else:
            params["DIALOG_ID"] = dialog_id

        try:
            data = await self.call("im.disk.file.commit", params)
        except Bitrix24Error as exc:
            log.warning(
                "Bitrix24: im.disk.file.commit не сработал (%s), "
                "используем исходные disk_file_ids", exc,
            )
            return ids_list

        result = data.get("result")

        if isinstance(result, (int, str)) and str(result).isdigit():
            file_id = int(result)
            log.info(
                "Bitrix24: файл привязан к чату %s, FILE_ID=%s",
                dialog_id, file_id,
            )
            return [file_id]

        if isinstance(result, dict):
            fid = result.get("ID") or result.get("FILE_ID")
            if fid and str(fid).isdigit():
                file_id = int(fid)
                log.info(
                    "Bitrix24: файл привязан к чату %s, FILE_ID=%s",
                    dialog_id, file_id,
                )
                return [file_id]

            files = result.get("FILES")
            if isinstance(files, dict):
                collected: List[int] = []
                for key, item in files.items():
                    if not isinstance(item, dict):
                        continue
                    fid = item.get("id") or item.get("ID")
                    if fid and str(fid).isdigit():
                        collected.append(int(fid))
                        log.debug(
                            "Bitrix24: файл из ключа %s → FILE_ID=%s",
                            key, fid,
                        )
                if collected:
                    log.info(
                        "Bitrix24: привязано файлов к чату %s: %d "
                        "(FILE_IDs=%s)",
                        dialog_id, len(collected), collected,
                    )
                    return collected

        log.warning(
            "Bitrix24: im.disk.file.commit вернул неожиданную "
            "структуру. Используем исходные disk_file_ids=%s. Ответ: %s",
            ids_list, str(data)[:300],
        )
        return ids_list

    async def ensure_subfolder(self, parent_id: int, name: str) -> int:
        """
        Возвращает ID подпапки `name` внутри `parent_id`.
        Создаёт, если её нет.
        """
        children = await self.list_folder_children(parent_id)
        for item in children:
            if (
                (item.get("TYPE") or "").lower() == "folder"
                and (item.get("NAME") or "").strip().lower()
                == name.strip().lower()
            ):
                return int(item["ID"])

        data = await self.call(
            "disk.folder.addsubfolder",
            {"id": int(parent_id), "data": {"NAME": name}},
        )
        result = data.get("result") or {}
        new_id = result.get("ID")
        if not new_id:
            raise Bitrix24Error(
                f"Bitrix24: не удалось создать папку «{name}»."
            )
        log.info("Bitrix24: создана папка %r id=%s", name, new_id)
        return int(new_id)

    async def upload_file(
        self,
        file_path: str,
        folder_id: int = 0,
        *,
        override_filename: Optional[str] = None,
    ) -> int:
        """
        Загружает файл на Диск Bitrix24 (двухэтапная схема).

        Args:
            file_path:        путь к файлу на диске.
            folder_id:        ID папки на Диске (0 — корень).
            override_filename: если задан — используется как имя
                              файла в Bitrix24. Кириллица
                              транслитерируется, недопустимые
                              символы заменяются на "_",
                              расширение сохраняется из file_path
                              (или добавляется, если его нет).
                              Если None — имя формируется из
                              basename + транслит + таймстамп.

        Returns:
            FILE_ID (int) — ID файла на Диске Bitrix24.
        """
        if not os.path.exists(file_path):
            raise Bitrix24Error(f"Файл не найден: {file_path}")
        if self._session is None:
            raise Bitrix24Error("aiohttp-сессия не открыта")

        if not folder_id:
            folder_id = await self.get_root_folder_id()

        if not folder_id:
            raise Bitrix24Error(
                "Bitrix24: не удалось определить папку для загрузки. "
                "Проверьте права вебхука (нужен метод disk) и "
                "корректность CHAT_ID. Также можно вручную указать "
                "ID папки в Настройках → Bitrix24."
            )

        orig_name = os.path.basename(file_path)
        stem, ext = os.path.splitext(orig_name)

        if override_filename:
            # Пользовательское имя: заголовок протокола или
            # аналогичное. Санитизация + транслит + обрезка.
            unique_name = _sanitize_display_name(
                override_filename, ext
            )
            log.info(
                "Bitrix24: имя файла задано вручную: %r → %r",
                override_filename, unique_name,
            )
        else:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_stem = _to_ascii_filename(stem)
            unique_name = f"{safe_stem}_{stamp}{ext}"

        try:
            size = os.path.getsize(file_path)
        except OSError:
            size = 0

        log.info(
            "Bitrix24: загрузка файла %s как %s (%.1f КБ) в папку id=%s",
            orig_name, unique_name, size / 1024, folder_id,
        )

        try:
            with open(file_path, "rb") as f:
                file_bytes = f.read()
        except Exception as exc:
            raise Bitrix24Error(
                f"Не удалось прочитать файл {file_path}: {exc}"
            )

        # --- Шаг 1: disk.folder.uploadfile ---
        url = f"{self.webhook_url}/disk.folder.uploadfile.json"

        try:
            form1 = aiohttp.FormData()
            form1.add_field("id", str(int(folder_id)))
            form1.add_field("generateUniqueName", "1")
            form1.add_field(
                "file",
                file_bytes,
                filename=unique_name,
                content_type="application/octet-stream",
            )

            async with self._session.post(url, data=form1) as resp:
                if resp.status >= 400:
                    body = await self._read_body(resp)
                    msg = f"Bitrix24 upload: HTTP {resp.status} — {body}"
                    log.error(msg)
                    raise Bitrix24Error(msg, status=resp.status, body=body)

                try:
                    data = await resp.json()
                except Exception as exc:
                    body = await self._read_body(resp)
                    msg = (
                        f"Bitrix24 upload: не удалось разобрать JSON "
                        f"(HTTP {resp.status}): {exc}. Тело: {body}"
                    )
                    log.error(msg)
                    raise Bitrix24Error(msg, status=resp.status, body=body)

                if isinstance(data, dict) and data.get("error"):
                    descr = (data.get("error_description")
                             or data.get("error"))
                    msg = f"Bitrix24 upload: {descr}"
                    log.error(msg)
                    raise Bitrix24Error(msg, body=str(data)[:500])

                result = data.get("result") or {}

                file_id = result.get("ID")
                if file_id:
                    log.info(
                        "Bitrix24: файл загружен (шаг 1), FILE_ID=%s",
                        file_id,
                    )
                    return int(file_id)

                upload_url = result.get("uploadUrl")
                if not upload_url:
                    msg = (
                        f"Bitrix24 upload: не получен ни ID, ни "
                        f"uploadUrl. Ответ: {str(data)[:500]}"
                    )
                    log.error(msg)
                    raise Bitrix24Error(msg, body=str(data)[:500])

                log.info(
                    "Bitrix24: получен uploadUrl, выполняем шаг 2 "
                    "(token до %d символов)",
                    len(str(upload_url)),
                )
        except Bitrix24Error:
            raise
        except Exception as exc:
            log.exception("Bitrix24: ошибка шага 1 загрузки: %s", exc)
            raise Bitrix24Error(f"Bitrix24 upload (шаг 1): {exc}")

        # --- Шаг 2: POST на uploadUrl с файлом ---
        try:
            form2 = aiohttp.FormData()
            form2.add_field(
                "file",
                file_bytes,
                filename=unique_name,
                content_type="application/octet-stream",
            )

            async with self._session.post(upload_url, data=form2) as resp:
                if resp.status >= 400:
                    body = await self._read_body(resp)
                    msg = (
                        f"Bitrix24 upload (шаг 2): HTTP {resp.status} "
                        f"— {body}"
                    )
                    log.error(msg)
                    raise Bitrix24Error(msg, status=resp.status, body=body)

                try:
                    data = await resp.json()
                except Exception as exc:
                    body = await self._read_body(resp)
                    msg = (
                        f"Bitrix24 upload (шаг 2): не удалось разобрать "
                        f"JSON (HTTP {resp.status}): {exc}. Тело: {body}"
                    )
                    log.error(msg)
                    raise Bitrix24Error(msg, status=resp.status, body=body)

                if isinstance(data, dict) and data.get("error"):
                    descr = (data.get("error_description")
                             or data.get("error"))
                    msg = f"Bitrix24 upload (шаг 2): {descr}"
                    log.error(msg)
                    raise Bitrix24Error(msg, body=str(data)[:500])

                result = data.get("result") or {}
                file_id = result.get("ID")
                if not file_id:
                    msg = (
                        f"Bitrix24 upload (шаг 2): не получен ID файла. "
                        f"Ответ: {str(data)[:500]}"
                    )
                    log.error(msg)
                    raise Bitrix24Error(msg, body=str(data)[:500])

                log.info(
                    "Bitrix24: файл загружен (шаг 2), FILE_ID=%s, "
                    "ссылка=%s",
                    file_id, result.get("DETAIL_URL", "—"),
                )
                return int(file_id)
        except Bitrix24Error:
            raise
        except Exception as exc:
            log.exception("Bitrix24: ошибка шага 2 загрузки: %s", exc)
            raise Bitrix24Error(f"Bitrix24 upload (шаг 2): {exc}")

    async def send_file_message(
        self,
        dialog_id: str,
        file_paths: Any,
        comment: str = "",
        *,
        folder_id: int = 0,
        system: bool = False,
        url_preview: bool = False,
        prefer_chat_folder: bool = True,
        override_filenames: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        Отправляет один или несколько файлов в чат Bitrix24.

        ВАЖНО: Bitrix24 не принимает пустой MESSAGE даже при
        наличии FILES (ошибки MESSAGE_EMPTY / EMPTY_MESSAGE).
        Если comment пустой — в MESSAGE подставляется имя первого
        файла (после override_filename или basename).

        Args:
            override_filenames: словарь {локальный_путь: желаемое_имя}.
                                Для файлов, которых нет в словаре,
                                имя формируется автоматически
                                (basename + транслит + таймстамп).
        """
        if not dialog_id:
            raise Bitrix24Error("Не указан dialog_id")

        if isinstance(file_paths, (str, os.PathLike)):
            paths: List[str] = [str(file_paths)]
        else:
            paths = [str(p) for p in (file_paths or []) if p]

        if not paths:
            raise Bitrix24Error("Не указан ни один файл")

        for p in paths:
            if not os.path.exists(p):
                raise Bitrix24Error(f"Файл не найден: {p}")

        target_folder_id = int(folder_id or 0)

        if target_folder_id <= 0 and prefer_chat_folder:
            chat_folder = await self.get_chat_folder_id(dialog_id)
            if chat_folder > 0:
                target_folder_id = chat_folder
                log.info(
                    "Bitrix24: используем папку чата %s (id=%s) "
                    "для загрузки %d файлов",
                    dialog_id, target_folder_id, len(paths),
                )

        if target_folder_id <= 0:
            log.info(
                "Bitrix24: папка чата недоступна или не задана — "
                "используем корень общего диска"
            )

        override_map: Dict[str, str] = dict(override_filenames or {})

        disk_file_ids: List[int] = []
        for p in paths:
            try:
                fid = await self.upload_file(
                    p,
                    folder_id=target_folder_id,
                    override_filename=override_map.get(p),
                )
                disk_file_ids.append(int(fid))
            except Bitrix24Error as exc:
                log.error(
                    "Bitrix24: не удалось загрузить %s: %s", p, exc
                )
                continue

        if not disk_file_ids:
            raise Bitrix24Error(
                "Bitrix24: ни один файл не удалось загрузить на Диск"
            )

        file_ids_for_message = await self.commit_file_to_chat(
            dialog_id, disk_file_ids
        )

        if not file_ids_for_message:
            raise Bitrix24Error(
                "Bitrix24: не удалось получить ID файлов для отправки"
            )

        # Bitrix24 требует непустой MESSAGE даже при наличии FILES.
        # Если comment пустой — используем имя первого файла
        # (после override_filename или basename), чтобы сообщение
        # не было пустым и в чате было понятно, что за файл.
        message_text = self._truncate(comment or "")
        if not message_text.strip():
            first_path = paths[0]
            fallback_name = override_map.get(first_path)
            if not fallback_name:
                fallback_name = os.path.basename(first_path)
            message_text = fallback_name or "Файл"
            log.info(
                "Bitrix24: MESSAGE был пуст — подставлено имя "
                "первого файла: %r", message_text,
            )

        params: Dict[str, Any] = {
            "DIALOG_ID": dialog_id,
            "MESSAGE": message_text,
            "SYSTEM": "Y" if system else "N",
            "URL_PREVIEW": "Y" if url_preview else "N",
            "FILES": file_ids_for_message,
        }
        log.info(
            "Bitrix24: отправка %d файлов в чат %s "
            "(FILE_IDs=%s, MESSAGE=%d символов, comment=%d символов)",
            len(file_ids_for_message), dialog_id,
            file_ids_for_message, len(message_text), len(comment or ""),
        )
        return await self.call("im.message.add", params)

    # ------------------------------------------------------------------
    # Проверка подключения
    # ------------------------------------------------------------------
    async def ping(self) -> str:
        """Проверяет связность и валидность вебхука через profile."""
        data = await self.call("profile", {})
        result = data.get("result") or {}
        name = (
            f"{result.get('NAME', '')} {result.get('LAST_NAME', '')}"
        ).strip() or result.get("EMAIL") or "OK"
        log.info("Bitrix24 ping: OK (%s)", name)
        return name