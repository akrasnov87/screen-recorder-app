"""Клиент Bitrix24 для отправки сообщений в чаты через вебхук.

Использует aiohttp — общая зависимость проекта (см. transcribe_client,
litellm_client). Не требует requests.

Поддерживает:
  • отправку текстовых сообщений (im.message.add);
  • загрузку файлов на Диск (disk.folder.uploadfile с двумя шагами);
  • привязку файлов к чату (im.disk.file.commit);
  • отправку одного или нескольких файлов-вложений в чат
    одним сообщением с общим комментарием;
  • автоматический выбор папки чата (im.disk.folder.get) для загрузки.
"""
from __future__ import annotations

import os
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


# Максимальная длина сообщения в Bitrix24 (по документации ~ 20 000).
# Оставляем запас — режем на 15 000, чтобы избежать отказов.
MAX_MESSAGE_CHARS = 15000


def _truncate(text: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    """Обрезает текст до limit символов, добавляя многоточие."""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n… (сообщение обрезано — превышен лимит Bitrix24)"


class Bitrix24Client:
    """
    Асинхронный клиент Bitrix24 для отправки сообщений и файлов в чат.

    Использование:
        async with Bitrix24Client(webhook_url) as client:
            await client.send_message("chat2101", "Текст")
            await client.send_file_message("chat2101", "/path/file.docx",
                                           comment="Протокол")
            # Несколько файлов одним сообщением:
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
    ) -> None:
        if not webhook_url:
            raise Bitrix24Error("Не задан webhook_url для Bitrix24")
        self.webhook_url = webhook_url.rstrip("/")
        self._connect_timeout = float(connect_timeout)
        self._read_timeout = float(read_timeout)
        self._session: Optional[aiohttp.ClientSession] = None

        log.debug(
            "Bitrix24Client создан: url=%s, connect=%.1f, read=%.1f",
            self.webhook_url, self._connect_timeout, self._read_timeout,
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
                    raise Bitrix24Error(
                        msg, status=resp.status, body=body
                    )

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

        safe_text = _truncate(text)
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

    async def list_folder_children(self, folder_id: int) -> List[Dict[str, Any]]:
        """disk.folder.getchildren — содержимое папки."""
        data = await self.call(
            "disk.folder.getchildren", {"id": int(folder_id)}
        )
        result = data.get("result") or []
        return result if isinstance(result, list) else []

    @staticmethod
    def _extract_root_id(storage: Dict[str, Any]) -> int:
        """
        Достаёт ID корневой папки из хранилища.

        Bitrix24 в разных порталах возвращает разные схемы:
          • ROOT_OBJECT_ID: "25"           — плоское поле (чаще всего);
          • ROOT_OBJECT: {"ID": 25, ...}   — вложенный словарь (старые
            версии или отдельные порталы).
        Обрабатываем оба варианта.
        """
        # Схема 1: ROOT_OBJECT_ID (строка или число)
        root_id = storage.get("ROOT_OBJECT_ID")
        if root_id is not None:
            try:
                return int(root_id)
            except (TypeError, ValueError):
                pass

        # Схема 2: ROOT_OBJECT: {"ID": ...}
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
        """
        Возвращает ID корневой папки первого доступного хранилища.

        Приоритеты:
          1) ENTITY_TYPE == "shared" (общий диск);
          2) ENTITY_TYPE == "common" (общий диск — новая схема Bitrix24);
          3) первое хранилище с валидным ROOT_OBJECT_ID / ROOT_OBJECT.

        Если хранилищ нет (например, у вебхука нет прав на disk),
        возвращает 0 — это сигнал «не удалось определить».
        """
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

        # Приоритет 1 и 2: общий диск (shared / common)
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

        # Приоритет 3: первое хранилище с валидным ROOT_OBJECT*
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
        """
        Возвращает ID папки на Диске, привязанной к чату.

        В Bitrix24 у каждого чата есть своя папка на Диске.
        Для этого используется метод im.disk.folder.get, который
        ожидает числовой CHAT_ID (например, '39110', а не 'chat39110').

        Если вебхук не является участником диалога, Bitrix24 вернёт
        ACCESS_ERROR (HTTP 403) — это нормальная ситуация, например,
        для личного чата другого сотрудника. В этом случае возвращаем 0,
        и вызывающий код загрузит файл в корень общего диска.

        Args:
            dialog_id: ID чата ('chat39110' или '39110').

        Returns:
            ID папки или 0, если получить не удалось.
        """
        if not dialog_id:
            return 0

        # im.disk.folder.get ожидает ЧИСЛОВОЙ ID чата.
        # Отрезаем префикс "chat", если он есть.
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
            # ACCESS_ERROR — типичная ситуация, когда вебхук
            # не является участником диалога (например, это личный
            # чат другого сотрудника). Это не критично — просто
            # загрузим файл в корень общего диска.
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
        """
        Прикрепляет файлы с Диска к чату через im.disk.file.commit.

        Bitrix24 возвращает вложенную структуру:
            result: {
                FILES: {
                    "upload<id>": {"id": <число>, "chatId": ..., ...},
                    "upload<id2>": {"id": <число>, ...},
                    ...
                }
            }
        Может вернуть и плоский результат: {"ID": <число>} или просто число.

        Args:
            dialog_id:     ID чата (chat39110 или 39110).
            disk_file_ids: ID файла (int) или список ID (List[int]).

        Returns:
            Список FILE_ID, готовых к использованию в im.message.add
            (параметр FILES=[id1, id2, ...]).
        """
        # Нормализуем в список
        if isinstance(disk_file_ids, (int, str)):
            ids_list = [int(disk_file_ids)]
        else:
            ids_list = [int(x) for x in (disk_file_ids or [])]

        if not ids_list:
            return []

        # Нормализуем dialog_id
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

        # --- Вариант 1: result — число (старые версии Bitrix24) ---
        if isinstance(result, (int, str)) and str(result).isdigit():
            file_id = int(result)
            log.info(
                "Bitrix24: файл привязан к чату %s, FILE_ID=%s",
                dialog_id, file_id,
            )
            return [file_id]

        if isinstance(result, dict):
            # --- Вариант 2: result.ID / result.FILE_ID ---
            fid = result.get("ID") or result.get("FILE_ID")
            if fid and str(fid).isdigit():
                file_id = int(fid)
                log.info(
                    "Bitrix24: файл привязан к чату %s, FILE_ID=%s",
                    dialog_id, file_id,
                )
                return [file_id]

            # --- Вариант 3: result.FILES.<uploadXXX>.id ---
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

        # --- Ничего не распознали ---
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


    # Транслитерация кириллицы → латиница для имён файлов Bitrix24.
    # Используется, когда нужно сохранить читаемость имени на Диске
    # и избежать URL-encoding в Content-Disposition.
    _TRANSLIT_MAP = {
        "а": "a",  "б": "b",  "в": "v",  "г": "g",  "д": "d",
        "е": "e",  "ё": "e",  "ж": "zh", "з": "z",  "и": "i",
        "й": "y",  "к": "k",  "л": "l",  "м": "m",  "н": "n",
        "о": "o",  "п": "p",  "р": "r",  "с": "s",  "т": "t",
        "у": "u",  "ф": "f",  "х": "h",  "ц": "ts", "ч": "ch",
        "ш": "sh", "щ": "sch","ъ": "",   "ы": "y",  "ь": "",
        "э": "e",  "ю": "yu", "я": "ya",
        "А": "A",  "Б": "B",  "В": "V",  "Г": "G",  "Д": "D",
        "Е": "E",  "Ё": "E",  "Ж": "Zh", "З": "Z",  "И": "I",
        "Й": "Y",  "К": "K",  "Л": "L",  "М": "M",  "Н": "N",
        "О": "O",  "П": "P",  "Р": "R",  "С": "S",  "Т": "T",
        "У": "U",  "Ф": "F",  "Х": "H",  "Ц": "Ts", "Ч": "Ch",
        "Ш": "Sh", "Щ": "Sch","Ъ": "",   "Ы": "Y",  "Ь": "",
        "Э": "E",  "Ю": "Yu", "Я": "Ya",
    }


    def _to_ascii_filename(name: str) -> str:
        """
        Приводит имя файла к ASCII-only.

        • Кириллица транслитерируется в латиницу.
        • Пробелы, точки, дефисы и подчёркивания сохраняются.
        • Остальные не-ASCII и «небезопасные» символы заменяются '_'.
        • Если после всех замен имя пустое — возвращает 'file'.
        """
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
        # Схлопываем подряд идущие подчёркивания и пробелы.
        while "__" in result:
            result = result.replace("__", "_")
        while "  " in result:
            result = result.replace("  ", " ")

        return result or "file"

    async def upload_file(self, file_path: str, folder_id: int = 0) -> int:
        """
        Загружает файл на Диск Bitrix24.

        Реализовано через двухэтапную схему:

          1) POST disk.folder.uploadfile.json с multipart (поля id и file)
             — сервер возвращает {result: {ID, ...}} при успехе,
             ЛИБО {result: {field: 'file', uploadUrl: '...'}}.

          2) POST на uploadUrl с multipart (поле file).

        Чтобы избежать ошибки DISK_OBJ_22000 («файл с таким именем уже
        есть»), передаём generateUniqueName=1 и добавляем к имени файла
        временной отпечаток.

        Args:
            file_path: путь к файлу на локальной машине.
            folder_id: ID папки. Если 0 — используется корень
                       первого доступного хранилища.

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
                "Проверьте права вебхука (нужен метод disk) и корректность "
                "CHAT_ID. Также можно вручную указать ID папки в "
                "Настройках → Bitrix24."
            )

        # ─── Готовим уникальное имя файла ───
        # Исходное имя: manual_protocol.docx
        # Станет:      manual_protocol_20260928_114602.docx
        orig_name = os.path.basename(file_path)
        stem, ext = os.path.splitext(orig_name)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Bitrix24 не всегда корректно декодирует URL-encoded имена файлов
        # в Content-Disposition (RFC 5987): сохраняет их как есть —
        # получается «protocol_%D0%A1%D0%BE%D0%B7...docx».
        # Чтобы имя файла на Диске было читаемым, используем ASCII-only:
        #  • если имя уже ASCII — оставляем как есть;
        #  • если содержит не-ASCII — транслитерируем (см. ниже).
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
            # generateUniqueName=1 — сервер сам придумает уникальное имя,
            # если такое уже есть. Подстраховка на случай гонок.
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
                    descr = data.get("error_description") or data.get("error")
                    msg = f"Bitrix24 upload: {descr}"
                    log.error(msg)
                    raise Bitrix24Error(msg, body=str(data)[:500])

                result = data.get("result") or {}

                # Успех сразу? Сервер вернул ID.
                file_id = result.get("ID")
                if file_id:
                    log.info(
                        "Bitrix24: файл загружен (шаг 1), FILE_ID=%s",
                        file_id,
                    )
                    return int(file_id)

                # Иначе — вторая стадия: uploadUrl.
                upload_url = result.get("uploadUrl")
                if not upload_url:
                    msg = (
                        f"Bitrix24 upload: не получен ни ID, ни uploadUrl. "
                        f"Ответ: {str(data)[:500]}"
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
                    descr = data.get("error_description") or data.get("error")
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
    ) -> Dict[str, Any]:
        """
        Отправляет один или несколько файлов в чат Bitrix24
        как вложения + комментарий.

        Порядок действий:
          1) Определяем папку (папка чата или явный folder_id).
          2) Загружаем каждый файл на Диск (upload_file → disk_file_id).
          3) Привязываем файлы к чату (im.disk.file.commit → [file_id]).
          4) Отправляем ОДНО сообщение с FILES=[id1, id2, ...] и
             MESSAGE=comment.

        Args:
            dialog_id:          ID чата (chat2101 или 2101).
            file_paths:         путь к файлу (str) или список путей
                                (List[str]).
            comment:            текст сообщения (обычно заголовок).
            folder_id:          явный ID папки (0 = авто).
            system:             системное сообщение.
            url_preview:        предпросмотр ссылок.
            prefer_chat_folder: если True — сначала пробуем папку чата.

        Returns:
            Ответ от im.message.add (dict).
        """
        if not dialog_id:
            raise Bitrix24Error("Не указан dialog_id")

        # Нормализуем в список путей
        if isinstance(file_paths, (str, os.PathLike)):
            paths: List[str] = [str(file_paths)]
        else:
            paths = [str(p) for p in (file_paths or []) if p]

        if not paths:
            raise Bitrix24Error("Не указан ни один файл")

        # Проверяем существование всех файлов
        for p in paths:
            if not os.path.exists(p):
                raise Bitrix24Error(f"Файл не найден: {p}")

        # --- Шаг 1: выбор папки ---
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
                "используем корень общего диска (определяется "
                "автоматически через disk.storage.getlist)"
            )

        # --- Шаг 2: загрузка всех файлов на Диск ---
        disk_file_ids: List[int] = []
        for p in paths:
            try:
                fid = await self.upload_file(p, folder_id=target_folder_id)
                disk_file_ids.append(int(fid))
            except Bitrix24Error as exc:
                log.error(
                    "Bitrix24: не удалось загрузить %s: %s", p, exc
                )
                # Продолжаем с остальными; если упадёт всё — вернём ошибку ниже
                continue

        if not disk_file_ids:
            raise Bitrix24Error(
                "Bitrix24: ни один файл не удалось загрузить на Диск"
            )

        # --- Шаг 3: привязка файлов к чату ---
        file_ids_for_message = await self.commit_file_to_chat(
            dialog_id, disk_file_ids
        )

        if not file_ids_for_message:
            raise Bitrix24Error(
                "Bitrix24: не удалось получить ID файлов для отправки"
            )

        # --- Шаг 4: отправка одного сообщения со всеми файлами ---
        params: Dict[str, Any] = {
            "DIALOG_ID": dialog_id,
            "MESSAGE": _truncate(comment or ""),
            "SYSTEM": "Y" if system else "N",
            "URL_PREVIEW": "Y" if url_preview else "N",
            "FILES": file_ids_for_message,
        }
        log.info(
            "Bitrix24: отправка %d файлов в чат %s "
            "(FILE_IDs=%s, комментарий=%d символов)",
            len(file_ids_for_message), dialog_id,
            file_ids_for_message, len(comment or ""),
        )
        return await self.call("im.message.add", params)

    # ------------------------------------------------------------------
    # Проверка подключения
    # ------------------------------------------------------------------
    async def ping(self) -> str:
        """
        Проверяет связность и валидность вебхука через profile.
        Возвращает имя пользователя вебхука.
        """
        data = await self.call("profile", {})
        result = data.get("result") or {}
        name = (
            f"{result.get('NAME', '')} {result.get('LAST_NAME', '')}"
        ).strip() or result.get("EMAIL") or "OK"
        log.info("Bitrix24 ping: OK (%s)", name)
        return name