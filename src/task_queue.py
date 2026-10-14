"""Очередь задач с сохранением состояния (локальный режим)."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from .logger import get_logger
from .utils import generate_task_id

log = get_logger(__name__)


class TaskQueue:
    """Потокобезопасная очередь задач."""

    def __init__(self, queue_path: str | None = None) -> None:
        if queue_path is None:
            queue_path = "/tmp/screen-recorder/queue.json"
        self.queue_path = queue_path
        os.makedirs(os.path.dirname(queue_path), exist_ok=True)
        self._lock = threading.RLock()
        self._tasks: List[Dict[str, Any]] = []
        log.info("TaskQueue инициализирована: %s", queue_path)
        self.load_queue()

    # ------------------------------------------------------------------
    # Добавление / выборка
    # ------------------------------------------------------------------
    def add_task(self, task: Dict[str, Any]) -> str:
        with self._lock:
            task_id = task.get("task_id") or generate_task_id()
            task["task_id"] = task_id
            task.setdefault("status", "pending")
            task.setdefault("progress", 0)
            task.setdefault("retries", 0)
            task.setdefault("last_error", "")
            task.setdefault("created_at", datetime.now().isoformat())

            # Синхронизируем task_id внутри metadata
            meta = task.get("metadata")
            if isinstance(meta, dict):
                meta["task_id"] = task_id

            self._tasks.append(task)
            log.info("Добавлена задача %s (видео=%s)",
                     task_id, task.get("video_path"))
            self.save_queue()
            return task_id

    def get_next_task(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            for t in self._tasks:
                if t.get("status") == "pending":
                    log.debug("Следующая задача: %s", t.get("task_id"))
                    return t
            return None

    # ------------------------------------------------------------------
    # Обновление
    # ------------------------------------------------------------------
    def update_task_status(self, task_id: str, status: str, progress: int) -> None:
        with self._lock:
            for t in self._tasks:
                if t.get("task_id") == task_id:
                    old = t.get("status")
                    t["status"] = status
                    t["progress"] = progress
                    t["updated_at"] = datetime.now().isoformat()
                    log.info("Задача %s: %s → %s (%d%%)",
                             task_id, old, status, progress)
                    break
            else:
                log.warning("Задача %s не найдена для обновления", task_id)
            self.save_queue()

    def mark_failed(self, task_id: str, error: str) -> None:
        """Помечает задачу как error с сохранением текста ошибки."""
        with self._lock:
            for t in self._tasks:
                if t.get("task_id") == task_id:
                    t["status"] = "error"
                    t["last_error"] = str(error)[:500]
                    t["last_attempt_at"] = datetime.now().isoformat()
                    t["updated_at"] = t["last_attempt_at"]
                    log.warning("Задача %s помечена как error: %s",
                                task_id, error)
                    break
            self.save_queue()

    def reset_for_retry(self, task_id: str) -> bool:
        """Переводит error-задачу в pending, увеличивает retries."""
        with self._lock:
            for t in self._tasks:
                if t.get("task_id") == task_id:
                    t["status"] = "pending"
                    t["progress"] = 0
                    t["retries"] = int(t.get("retries", 0)) + 1
                    t["updated_at"] = datetime.now().isoformat()
                    log.info("Задача %s сброшена для повтора (retries=%d)",
                             task_id, t["retries"])
                    self.save_queue()
                    return True
            log.warning("Не найдена задача %s для повтора", task_id)
            return False

    def remove_task(self, task_id: str) -> None:
        with self._lock:
            before = len(self._tasks)
            self._tasks = [t for t in self._tasks if t.get("task_id") != task_id]
            if len(self._tasks) < before:
                log.info("Задача %s удалена", task_id)
            self.save_queue()

    def clear_completed(self) -> int:
        """Удаляет завершённые задачи. Возвращает количество удалённых."""
        with self._lock:
            before = len(self._tasks)
            self._tasks = [t for t in self._tasks if t.get("status") != "completed"]
            removed = before - len(self._tasks)
            if removed:
                log.info("Удалено завершённых задач: %d", removed)
                self.save_queue()
            return removed

    # ------------------------------------------------------------------
    # Статус / выборка
    # ------------------------------------------------------------------
    def get_queue_status(self) -> Dict[str, Any]:
        with self._lock:
            status = {
                "total": len(self._tasks),
                "pending": sum(1 for t in self._tasks if t.get("status") == "pending"),
                "in_progress": sum(
                    1 for t in self._tasks
                    if t.get("status") in ("converting", "transcribing")
                ),
                "completed": sum(1 for t in self._tasks if t.get("status") == "completed"),
                "error": sum(1 for t in self._tasks if t.get("status") == "error"),
            }
            log.debug("Статус очереди: %s", status)
            return status

    def get_queue(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(t) for t in self._tasks]

    def get_failed_tasks(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(t) for t in self._tasks if t.get("status") == "error"]

    def find_by_id(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            for t in self._tasks:
                if t.get("task_id") == task_id:
                    return dict(t)
            return None

    # ------------------------------------------------------------------
    # Сохранение / загрузка
    # ------------------------------------------------------------------
    def save_queue(self) -> None:
        with self._lock:
            try:
                with open(self.queue_path, "w", encoding="utf-8") as f:
                    json.dump(self._tasks, f, indent=2, ensure_ascii=False)
                log.debug("Очередь сохранена (%d задач)", len(self._tasks))
            except OSError as exc:
                log.error("Ошибка сохранения очереди: %s", exc)

    def load_queue(self) -> None:
        if os.path.exists(self.queue_path):
            try:
                with open(self.queue_path, "r", encoding="utf-8") as f:
                    self._tasks = json.load(f)
                log.info("Загружено задач из файла: %d", len(self._tasks))
            except (OSError, json.JSONDecodeError) as exc:
                log.error("Ошибка загрузки очереди: %s", exc)
                self._tasks = []
        else:
            log.debug("Файл очереди не найден, начинаем с пустой")

        # Восстанавливаем незавершённые задачи
        for t in self._tasks:
            if t.get("status") in ("converting", "transcribing", "uploading"):
                log.warning("Восстановление задачи %s: %s → pending",
                            t.get("task_id"), t.get("status"))
                t["status"] = "pending"
                t["progress"] = 0
            # Гарантируем наличие полей
            t.setdefault("retries", 0)
            t.setdefault("last_error", "")