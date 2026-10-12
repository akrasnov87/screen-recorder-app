"""Асинхронный клиент к REST API Azure DevOps Server / TFS.

Поддерживает Azure DevOps Server 2022 (REST API 7.0) и совместимые
версии (TFS 2017+). Аутентификация — Personal Access Token (PAT)
через заголовок Authorization: Basic base64(:PAT).

Реализует API:
  • GET  /_apis/projects?api-version=...          — список проектов
  • GET  /{project}/_apis/wit/queries?$depth=2    — сохранённые запросы
  • POST /{project}/_apis/wit/wiql                — выполнить WIQL
                                                    (плоский или link-запрос)
  • GET  /_apis/wit/workitems                     — данные по ID (GET)
  • POST /_apis/wit/workitemsbatch                — данные по ID (POST, до 200)
  • GET  /_apis/wit/workitems/{id}                — одна задача

Изменения:
  • Добавлена поддержка link-запросов (FROM WorkItemLinks):
      – query_wiql_links() — возвращает workItemRelations + список ID;
      – get_work_items_batch() — пакетное получение через
        POST /_apis/wit/workitemsbatch (обязательно для link-запросов,
        т.к. GET /workitems не принимает $expand=relations).
  • Добавлен авто-парсинг полей из SELECT WIQL (_parse_fields_from_wiql):
    позволяет не дублировать список полей в настройках — какие поля
    запрошены в WIQL, те и возвращаются.
  • Расширен DEFAULT_WORK_ITEM_FIELDS: добавлены OriginalEstimate,
    RemainingWork, CompletedWork, StoryPoints, Effort, TargetDate.
  • В _normalize_work_item() добавлены шорткаты original_estimate,
    remaining_work, completed_work, story_points, target_date, parent.
"""
from __future__ import annotations

import base64
import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import aiohttp

from .logger import get_logger

log = get_logger(__name__)


# Поля Work Item, которые имеет смысл запрашивать по умолчанию.
# Используется, если в настройках не задан свой список и не удалось
# автоматически извлечь поля из SELECT WIQL.
DEFAULT_WORK_ITEM_FIELDS: List[str] = [
    "System.Id",
    "System.Title",
    "System.WorkItemType",
    "System.State",
    "System.AssignedTo",
    "System.CreatedDate",
    "System.ChangedDate",
    "System.AreaPath",
    "System.IterationPath",
    "System.Tags",
    "System.Parent",
    "Microsoft.VSTS.Common.Priority",
    "Microsoft.VSTS.Common.Severity",
    "Microsoft.VSTS.Scheduling.OriginalEstimate",
    "Microsoft.VSTS.Scheduling.RemainingWork",
    "Microsoft.VSTS.Scheduling.CompletedWork",
    "Microsoft.VSTS.Scheduling.StoryPoints",
    "Microsoft.VSTS.Scheduling.Effort",
    "Microsoft.VSTS.Scheduling.TargetDate",
]


class TfsError(RuntimeError):
    """Ошибка при обращении к TFS."""

    def __init__(
        self,
        message: str,
        status: int = 0,
        body: str = "",
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def _basic_auth_header(pat: str) -> str:
    """
    Формирует заголовок Authorization для TFS.

    Для Azure DevOps Server / TFS PAT передаётся как Basic с
    пустым логином: base64(":<PAT>").
    """
    token = base64.b64encode(f":{pat}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


class TfsClient:
    """Асинхронный клиент к REST API TFS / Azure DevOps Server."""

    def __init__(
        self,
        base_url: str,
        pat: str,
        *,
        api_version: str = "7.0",
        connect_timeout: float = 15.0,
        read_timeout: float = 60.0,
    ) -> None:
        if not base_url:
            raise TfsError("Не задан base_url для TFS")
        if not pat:
            raise TfsError("Не задан PAT для TFS")

        self.base_url = base_url.rstrip("/")
        self.pat = pat
        self.api_version = api_version or "7.0"
        self._connect_timeout = float(connect_timeout)
        self._read_timeout = float(read_timeout)
        self._session: Optional[aiohttp.ClientSession] = None

        log.debug(
            "TfsClient создан: base_url=%s, api_version=%s, "
            "connect=%.1f, read=%.1f",
            self.base_url, self.api_version,
            self._connect_timeout, self._read_timeout,
        )

    # ------------------------------------------------------------------
    # Контекстный менеджер
    # ------------------------------------------------------------------
    async def __aenter__(self) -> "TfsClient":
        timeout = aiohttp.ClientTimeout(
            total=None,
            connect=self._connect_timeout,
            sock_connect=self._connect_timeout,
            sock_read=self._read_timeout,
        )
        self._session = aiohttp.ClientSession(
            timeout=timeout,
            headers=self._headers(),
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def get_work_items_universal(
        self,
        wiql: str,
        project: str = "",
        *,
        max_items: int = 0,
    ) -> Dict[str, Any]:
        """
        Универсальная обёртка: сама определяет тип WIQL
        (плоский или link) и возвращает плоский список задач.

        Для link-запросов дерево разворачивается в плоский
        список, а родитель каждого узла сохраняется в
        поле parent_id.

        Returns:
            {
              "kind": "flat" | "link",
              "items": [<нормализованные задачи>],
              "relations": [...],   # только для link
              "count": int,
            }
        """
        if not wiql or not wiql.strip():
            return {
                "kind": "flat",
                "items": [],
                "relations": [],
                "count": 0,
            }

        if self.is_link_query(wiql):
            result = await self.get_work_items_by_wiql_links(
                wiql, project
            )
            items = list(result.get("items") or [])

            # Присваиваем parent_id из relations.
            items_by_id = {
                int(it["id"]): it
                for it in items if it.get("id")
            }
            for rel in result.get("relations") or []:
                src = rel.get("source")
                tgt = rel.get("target")
                if not isinstance(src, dict) or not src.get("id"):
                    continue
                if not isinstance(tgt, dict) or not tgt.get("id"):
                    continue
                try:
                    child = items_by_id.get(int(tgt["id"]))
                    parent_id = int(src["id"])
                except (TypeError, ValueError):
                    continue
                if child is not None:
                    child["parent_id"] = parent_id

            if max_items > 0:
                items = items[:max_items]

            return {
                "kind": "link",
                "items": items,
                "relations": result.get("relations") or [],
                "count": len(items),
            }

        items = await self.get_work_items_by_wiql(
            wiql, project,
            max_items=max_items,
        )
        return {
            "kind": "flat",
            "items": items,
            "relations": [],
            "count": len(items),
        }

    # ------------------------------------------------------------------
    # Низкоуровневые помощники
    # ------------------------------------------------------------------
    def _headers(
        self, extra: Optional[Dict[str, str]] = None
    ) -> Dict[str, str]:
        h = {
            "Authorization": _basic_auth_header(self.pat),
            "Accept": "application/json",
        }
        if extra:
            h.update(extra)
        return h

    async def _read_body(self, resp: aiohttp.ClientResponse) -> str:
        try:
            text = await resp.text()
        except Exception:
            return ""
        text = text.strip()
        return (text[:500] + "…") if len(text) > 500 else text

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        context: str = "запрос",
    ) -> Any:
        if self._session is None:
            raise TfsError("aiohttp-сессия не открыта")

        url = f"{self.base_url}{path}"
        log.debug("TfsClient: %s %s (params=%s)", method, url, params)

        try:
            async with self._session.request(
                method,
                url,
                json=json_body,
                params=params,
            ) as resp:
                if resp.status >= 400:
                    body = await self._read_body(resp)
                    msg = (
                        f"TfsClient: {context}: HTTP {resp.status} "
                        f"от {url}"
                    )
                    if body:
                        msg += f" — {body}"
                    log.error(msg)
                    raise TfsError(msg, status=resp.status, body=body)

                ctype = (resp.headers.get("Content-Type") or "").lower()
                if "application/json" in ctype:
                    try:
                        return await resp.json()
                    except Exception as exc:
                        body = await self._read_body(resp)
                        msg = (
                            f"TfsClient: {context}: не удалось "
                            f"разобрать JSON (HTTP {resp.status}): "
                            f"{exc}. Тело: {body}"
                        )
                        log.error(msg)
                        raise TfsError(
                            msg, status=resp.status, body=body
                        )
                return await resp.text()
        except TfsError:
            raise
        except aiohttp.ClientConnectorError as exc:
            msg = f"TfsClient: ошибка подключения к {url}: {exc}"
            log.error(msg)
            raise TfsError(msg)
        except aiohttp.ServerTimeoutError:
            msg = (
                f"TfsClient: таймаут ({self._read_timeout:.0f} с) "
                f"при {context}"
            )
            log.error(msg)
            raise TfsError(msg)
        except Exception as exc:
            log.exception("TfsClient: ошибка %s: %s", context, exc)
            raise TfsError(f"TfsClient: {context}: {exc}")

    # ------------------------------------------------------------------
    # Проверка подключения
    # ------------------------------------------------------------------
    async def list_projects(self) -> List[Dict[str, Any]]:
        """
        GET /_apis/projects — список проектов коллекции.

        Используется как «ping» (проверка подключения) и для
        заполнения выпадающего списка проектов в UI.
        """
        data = await self._request_json(
            "GET",
            "/_apis/projects",
            params={"api-version": self.api_version},
            context="list_projects",
        )
        value = data.get("value") or []
        log.info(
            "TFS: получено проектов: %d", len(value)
        )
        return value

    async def ping(self) -> str:
        """
        Проверяет подключение: список проектов + количество.

        Returns:
            Строка вида "ok: 5 проектов (первый: MyProject)".
        """
        projects = await self.list_projects()
        if not projects:
            return "ok: 0 проектов"
        first = projects[0].get("name") or "?"
        return f"ok: {len(projects)} проект(ов), первый: {first}"

    # ------------------------------------------------------------------
    # Сохранённые запросы
    # ------------------------------------------------------------------
    async def list_queries(
        self, project: str = ""
    ) -> List[Dict[str, Any]]:
        """
        GET /{project}/_apis/wit/queries — сохранённые запросы.

        Возвращает «плоский» список словарей с полями:
          id, name, path, wiql, is_folder.
        Папки разворачиваются рекурсивно.

        Если project пустой — используется корень коллекции.
        """
        if project:
            path = f"/{quote(project)}/_apis/wit/queries"
        else:
            path = "/_apis/wit/queries"

        data = await self._request_json(
            "GET",
            path,
            params={
                "api-version": self.api_version,
                "$depth": 2,
                "$expand": "wiql",
            },
            context=f"list_queries({project})",
        )

        flat: List[Dict[str, Any]] = []

        def _walk(items: List[Dict[str, Any]]) -> None:
            for it in items or []:
                is_folder = bool(it.get("isFolder"))
                wiql = ""
                if not is_folder:
                    wiql = (
                        (it.get("wiql") or "").strip()
                    )
                flat.append({
                    "id": str(it.get("id") or ""),
                    "name": str(it.get("name") or ""),
                    "path": str(it.get("path") or ""),
                    "wiql": wiql,
                    "is_folder": is_folder,
                })
                children = it.get("children") or []
                if children:
                    _walk(children)

        _walk(data.get("value") or [])

        queries = [q for q in flat if not q["is_folder"]]
        log.info(
            "TFS: сохранённых запросов в %r: %d (всего узлов: %d)",
            project or "<root>", len(queries), len(flat),
        )
        return queries

    # ------------------------------------------------------------------
    # Определение типа WIQL
    # ------------------------------------------------------------------
    @staticmethod
    def is_link_query(wiql: str) -> bool:
        """Определяет, является ли WIQL link-запросом."""
        if not wiql:
            return False
        return bool(
            re.search(
                r"\bFROM\s+WorkItemLinks\b",
                wiql,
                flags=re.IGNORECASE,
            )
        )

    # ------------------------------------------------------------------
    # Плоский WIQL
    # ------------------------------------------------------------------
    async def query_wiql(
        self,
        wiql: str,
        project: str = "",
        *,
        top: int = 0,
    ) -> List[int]:
        """
        POST /{project}/_apis/wit/wiql — выполнить плоский WIQL.

        Подходит для запросов FROM WorkItems.
        Для link-запросов (FROM WorkItemLinks) используйте
        query_wiql_links().

        Args:
            wiql:    текст запроса.
            project: контекст проекта (если WIQL использует
                     @project, иначе можно оставить пустым).
            top:     ограничение на количество возвращаемых ID
                     (0 — без ограничения).

        Returns:
            Список ID найденных work items.
        """
        if not wiql or not wiql.strip():
            raise TfsError("Пустой WIQL-запрос")

        if self.is_link_query(wiql):
            raise TfsError(
                "query_wiql() не поддерживает link-запросы "
                "(FROM WorkItemLinks). Используйте "
                "query_wiql_links()."
            )

        if project:
            path = f"/{quote(project)}/_apis/wit/wiql"
        else:
            path = "/_apis/wit/wiql"

        params: Dict[str, Any] = {"api-version": self.api_version}
        if top > 0:
            params["$top"] = int(top)

        data = await self._request_json(
            "POST",
            path,
            json_body={"query": wiql},
            params=params,
            context="query_wiql",
        )

        items = data.get("workItems") or []
        ids = [int(it["id"]) for it in items if it.get("id")]
        log.info(
            "TFS: WIQL вернул %d ID (project=%r, top=%d)",
            len(ids), project or "<root>", top,
        )
        return ids

    # ------------------------------------------------------------------
    # Link-запросы (FROM WorkItemLinks)
    # ------------------------------------------------------------------
    async def query_wiql_links(
        self,
        wiql: str,
        project: str = "",
    ) -> Dict[str, Any]:
        """
        POST /{project}/_apis/wit/wiql — выполнить link-запрос.

        Link-запрос (FROM WorkItemLinks ... MODE (Recursive))
        возвращает не плоский список задач, а список связей:
            {"workItemRelations": [
                {"source": null,      "target": {"id": 100}},
                {"source": {"id": 100}, "target": {"id": 101}},
                ...
            ]}

        Args:
            wiql:    текст link-запроса.
            project: контекст проекта (@project).

        Returns:
            {
              "relations": [<исходные workItemRelations>],
              "ids": [<уникальные ID>],
            }
        """
        if not wiql or not wiql.strip():
            raise TfsError("Пустой WIQL-запрос")

        if project:
            path = f"/{quote(project)}/_apis/wit/wiql"
        else:
            path = "/_apis/wit/wiql"

        data = await self._request_json(
            "POST",
            path,
            json_body={"query": wiql},
            params={"api-version": self.api_version},
            context="query_wiql_links",
        )

        relations = data.get("workItemRelations") or []
        ids_set: set = set()

        for rel in relations:
            src = rel.get("source")
            tgt = rel.get("target")
            if isinstance(src, dict) and src.get("id"):
                try:
                    ids_set.add(int(src["id"]))
                except (TypeError, ValueError):
                    pass
            if isinstance(tgt, dict) and tgt.get("id"):
                try:
                    ids_set.add(int(tgt["id"]))
                except (TypeError, ValueError):
                    pass

        ids = sorted(ids_set)
        log.info(
            "TFS: link-запрос вернул %d связей, %d уникальных ID "
            "(project=%r)",
            len(relations), len(ids), project or "<root>",
        )
        return {"relations": relations, "ids": ids}

    # ------------------------------------------------------------------
    # Work Items: GET /_apis/wit/workitems
    # ------------------------------------------------------------------
    async def get_work_items(
        self,
        ids: List[int],
        fields: Optional[List[str]] = None,
        *,
        chunk_size: int = 200,
    ) -> List[Dict[str, Any]]:
        """
        GET /_apis/wit/workitems — данные по списку ID.

        Ограничение TFS: не более 200 ID за запрос. Поэтому
        список автоматически разбивается на чанки.

        Args:
            ids:    список ID.
            fields: список полей (System.Id и т.п.). Если None —
                    берётся DEFAULT_WORK_ITEM_FIELDS.
            chunk_size: размер чанка (не более 200).

        Returns:
            Список словарей-задач с полями из fields + поле "id".
        """
        if not ids:
            return []

        if fields is None:
            fields = DEFAULT_WORK_ITEM_FIELDS

        chunk_size = max(1, min(int(chunk_size), 200))

        result: List[Dict[str, Any]] = []
        total = len(ids)

        for start in range(0, total, chunk_size):
            chunk = ids[start:start + chunk_size]
            ids_str = ",".join(str(i) for i in chunk)

            data = await self._request_json(
                "GET",
                "/_apis/wit/workitems",
                params={
                    "api-version": self.api_version,
                    "ids": ids_str,
                    "fields": ",".join(fields),
                    "$expand": "None",
                },
                context=f"get_work_items({len(chunk)} ids)",
            )

            for item in data.get("value") or []:
                result.append(self._normalize_work_item(item))

            log.debug(
                "TFS: получен чанк из %d work items (всего %d/%d)",
                len(chunk), len(result), total,
            )

        log.info(
            "TFS: получено %d work items из %d ID",
            len(result), total,
        )
        return result

    # ------------------------------------------------------------------
    # Work Items: POST /_apis/wit/workitemsbatch
    # ------------------------------------------------------------------
    async def get_work_items_batch(
        self,
        ids: List[int],
        fields: Optional[List[str]] = None,
        *,
        chunk_size: int = 200,
    ) -> List[Dict[str, Any]]:
        """
        POST /_apis/wit/workitemsbatch — пакетное получение Work Items.

        Этот метод рекомендуется для link-запросов: API принимает
        до 200 ID за раз и корректно работает с полями.

        Args:
            ids:    список ID.
            fields: список полей. Если None — DEFAULT_WORK_ITEM_FIELDS.
            chunk_size: размер чанка (не более 200).

        Returns:
            Список нормализованных задач.
        """
        if not ids:
            return []

        if fields is None:
            fields = DEFAULT_WORK_ITEM_FIELDS

        chunk_size = max(1, min(int(chunk_size), 200))

        result: List[Dict[str, Any]] = []
        total = len(ids)

        for start in range(0, total, chunk_size):
            chunk = ids[start:start + chunk_size]

            data = await self._request_json(
                "POST",
                "/_apis/wit/workitemsbatch",
                json_body={
                    "ids": chunk,
                    "fields": fields,
                },
                params={"api-version": self.api_version},
                context=f"get_work_items_batch({len(chunk)} ids)",
            )

            for item in data.get("value") or []:
                result.append(self._normalize_work_item(item))

            log.debug(
                "TFS: получен batch-чанк из %d work items "
                "(всего %d/%d)",
                len(chunk), len(result), total,
            )

        log.info(
            "TFS: batch-запрос вернул %d work items из %d ID",
            len(result), total,
        )
        return result

    async def get_work_item(
        self,
        item_id: int,
        fields: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        GET /_apis/wit/workitems/{id} — одна задача.

        Возвращает словарь или None, если задачи нет.
        """
        if fields is None:
            fields = DEFAULT_WORK_ITEM_FIELDS

        try:
            data = await self._request_json(
                "GET",
                f"/_apis/wit/workitems/{int(item_id)}",
                params={
                    "api-version": self.api_version,
                    "fields": ",".join(fields),
                },
                context=f"get_work_item({item_id})",
            )
        except TfsError as exc:
            if exc.status == 404:
                log.info(
                    "TFS: задача %s не найдена", item_id
                )
                return None
            raise

        return self._normalize_work_item(data)

    # ------------------------------------------------------------------
    # Комбо-операция: WIQL + получить поля
    # ------------------------------------------------------------------
    async def get_work_items_by_wiql(
        self,
        wiql: str,
        project: str = "",
        *,
        fields: Optional[List[str]] = None,
        max_items: int = 200,
    ) -> List[Dict[str, Any]]:
        """
        Удобная обёртка: выполнить плоский WIQL и сразу получить данные.

        Если fields не передан явно — пытается извлечь список
        полей из секции SELECT самого WIQL. Это избавляет от
        необходимости дублировать поля в настройках: какие поля
        запрошены в WIQL, те и вернутся.

        Args:
            wiql:      текст WIQL (FROM WorkItems).
            project:   контекст проекта (может быть пустым).
            fields:    список полей (None = извлечь из WIQL или
                       использовать DEFAULT_WORK_ITEM_FIELDS).
            max_items: ограничение на количество результатов
                       (0 — без ограничения).

        Returns:
            Список нормализованных задач.
        """
        top = int(max_items) if max_items and max_items > 0 else 0
        ids = await self.query_wiql(wiql, project, top=top)
        if not ids:
            return []

        if fields is None:
            parsed = self._parse_fields_from_wiql(wiql)
            if parsed:
                log.info(
                    "TFS: поля из WIQL: %s", ", ".join(parsed)
                )
                fields = parsed
            else:
                log.debug(
                    "TFS: не удалось извлечь поля из WIQL, "
                    "используем DEFAULT_WORK_ITEM_FIELDS"
                )
                fields = DEFAULT_WORK_ITEM_FIELDS

        return await self.get_work_items(ids, fields)

    async def get_work_items_by_wiql_links(
        self,
        wiql: str,
        project: str = "",
        *,
        fields: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Комбо для link-запроса: получить связи + данные задач.

        Returns:
            {
              "relations": [...],   # исходные workItemRelations
              "items": [...],       # нормализованные задачи
              "items_by_id": {...}, # {id: item}
            }
        """
        links_data = await self.query_wiql_links(wiql, project)
        ids = links_data.get("ids") or []
        if not ids:
            return {
                "relations": links_data.get("relations") or [],
                "items": [],
                "items_by_id": {},
            }

        if fields is None:
            parsed = self._parse_fields_from_wiql(wiql)
            if parsed:
                log.info(
                    "TFS: поля из link-WIQL: %s",
                    ", ".join(parsed),
                )
                fields = parsed
            else:
                fields = DEFAULT_WORK_ITEM_FIELDS

        items = await self.get_work_items_batch(ids, fields)
        items_by_id = {int(it["id"]): it for it in items if it.get("id")}

        return {
            "relations": links_data.get("relations") or [],
            "items": items,
            "items_by_id": items_by_id,
        }

    # ------------------------------------------------------------------
    # Парсинг полей из WIQL
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_fields_from_wiql(wiql: str) -> List[str]:
        """
        Извлекает список полей из секции SELECT WIQL.

        Поддерживает:
          • SELECT [Field1], [Field2] FROM ...
          • многострочный SELECT

        Возвращает список полей (например, System.Id,
        Microsoft.VSTS.Scheduling.RemainingWork).
        Пустой список, если извлечь не удалось.
        """
        if not wiql:
            return []

        m = re.search(
            r"\bSELECT\b(.*?)\bFROM\b",
            wiql,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not m:
            return []

        select_part = m.group(1)

        fields = re.findall(r"\[([^\]]+)\]", select_part)

        seen = set()
        result: List[str] = []
        for f in fields:
            f = f.strip()
            if f and f not in seen:
                result.append(f)
                seen.add(f)

        return result

    # ------------------------------------------------------------------
    # Нормализация
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_work_item(
        item: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Приводит «сырую» задачу TFS к удобному словарю.

        Поля, которых нет в fields, будут отсутствовать в
        результате, за исключением id (он всегда есть).
        """
        fields = item.get("fields") or {}
        result: Dict[str, Any] = {
            "id": int(item.get("id") or 0),
            "rev": int(item.get("rev") or 0),
            "url": str(item.get("url") or ""),
        }
        result["fields"] = dict(fields)

        result["title"] = str(fields.get("System.Title") or "")
        result["work_item_type"] = str(
            fields.get("System.WorkItemType") or ""
        )
        result["state"] = str(fields.get("System.State") or "")
        result["assigned_to"] = _extract_identity(
            fields.get("System.AssignedTo")
        )
        result["created_date"] = str(
            fields.get("System.CreatedDate") or ""
        )
        result["changed_date"] = str(
            fields.get("System.ChangedDate") or ""
        )
        result["area_path"] = str(
            fields.get("System.AreaPath") or ""
        )
        result["iteration_path"] = str(
            fields.get("System.IterationPath") or ""
        )
        result["tags"] = str(fields.get("System.Tags") or "")
        result["priority"] = _extract_number(
            fields.get("Microsoft.VSTS.Common.Priority")
        )
        result["severity"] = str(
            fields.get("Microsoft.VSTS.Common.Severity") or ""
        )
        result["rating"] = str(
            fields.get("Microsoft.VSTS.Common.Rating") or ""
        )

        # --- Scheduling (часы, оценка, оставшееся) ---
        result["original_estimate"] = _extract_number(
            fields.get(
                "Microsoft.VSTS.Scheduling.OriginalEstimate"
            )
        )
        result["remaining_work"] = _extract_number(
            fields.get(
                "Microsoft.VSTS.Scheduling.RemainingWork"
            )
        )
        result["completed_work"] = _extract_number(
            fields.get(
                "Microsoft.VSTS.Scheduling.CompletedWork"
            )
        )
        result["story_points"] = _extract_number(
            fields.get("Microsoft.VSTS.Scheduling.StoryPoints")
        )
        result["effort"] = _extract_number(
            fields.get("Microsoft.VSTS.Scheduling.Effort")
        )
        result["size"] = _extract_number(
            fields.get("Microsoft.VSTS.Scheduling.Size")
        )
        result["target_date"] = str(
            fields.get("Microsoft.VSTS.Scheduling.TargetDate") or ""
        )
        result["parent_id"] = _extract_number(
            fields.get("System.Parent")
        )

        return result


def _extract_identity(value: Any) -> str:
    """Извлекает displayName из IdentityRef или строку."""
    if not value:
        return ""
    if isinstance(value, dict):
        return str(
            value.get("displayName")
            or value.get("uniqueName")
            or ""
        )
    return str(value)


def _extract_number(value: Any) -> int:
    """Приводит значение к int, при ошибке — 0."""
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        # Может быть float вроде "3.5" — попробуем округлить.
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0