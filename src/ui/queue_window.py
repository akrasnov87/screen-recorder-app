"""Окно просмотра и управления очередью задач."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..logger import get_logger
from ..task_queue import TaskQueue

log = get_logger(__name__)


class QueueWindow(QDialog):
    """Просмотр очереди задач и ручной запуск."""

    def __init__(self, task_queue: TaskQueue, processor,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.task_queue = task_queue
        self.processor = processor
        self.setWindowTitle("Очередь задач")
        self.setMinimumSize(1000, 600)
        self.setModal(False)
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # Заголовок
        header = QHBoxLayout()
        self.summary_label = QLabel("")
        header.addWidget(self.summary_label)
        header.addStretch()

        self.refresh_btn = QPushButton("Обновить")
        self.refresh_btn.clicked.connect(self.refresh)
        header.addWidget(self.refresh_btn)
        root.addLayout(header)

        # Таблица
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels([
            "Task ID", "Создана", "Статус", "Прогресс",
            "Попытки", "Ошибка", "Видео",
        ])
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        header_view = self.table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        header_view.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        root.addWidget(self.table)

        # Кнопки действий
        actions = QHBoxLayout()

        self.retry_btn = QPushButton("Повторить")
        self.retry_btn.clicked.connect(self._retry_selected)
        actions.addWidget(self.retry_btn)

        self.retry_all_btn = QPushButton("Повторить все ошибки")
        self.retry_all_btn.clicked.connect(self._retry_all_failed)
        actions.addWidget(self.retry_all_btn)

        self.delete_btn = QPushButton("Удалить")
        self.delete_btn.clicked.connect(self._delete_selected)
        actions.addWidget(self.delete_btn)

        self.clear_completed_btn = QPushButton("Удалить завершённые")
        self.clear_completed_btn.clicked.connect(self._clear_completed)
        actions.addWidget(self.clear_completed_btn)

        actions.addStretch()

        self.close_btn = QPushButton("Закрыть")
        self.close_btn.clicked.connect(self.close)
        actions.addWidget(self.close_btn)

        root.addLayout(actions)

    # ------------------------------------------------------------------
    # Данные
    # ------------------------------------------------------------------
    def refresh(self) -> None:
        """Перечитывает очередь и обновляет таблицу."""
        tasks = self.task_queue.get_queue()
        status = self.task_queue.get_queue_status()
        self.summary_label.setText(
            f"Всего: {status['total']} | "
            f"В ожидании: {status['pending']} | "
            f"В работе: {status['in_progress']} | "
            f"Завершено: {status['completed']} | "
            f"Ошибок: {status['error']}"
        )

        self.table.setRowCount(0)
        for t in tasks:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(str(t.get("task_id", ""))))
            self.table.setItem(row, 1, QTableWidgetItem(str(t.get("created_at", ""))))
            status_item = QTableWidgetItem(str(t.get("status", "")))
            if t.get("status") == "error":
                status_item.setForeground(Qt.GlobalColor.red)
            self.table.setItem(row, 2, status_item)
            self.table.setItem(row, 3, QTableWidgetItem(f"{t.get('progress', 0)}%"))
            self.table.setItem(row, 4, QTableWidgetItem(str(t.get("retries", 0))))
            self.table.setItem(row, 5, QTableWidgetItem(str(t.get("last_error", ""))[:120]))
            self.table.setItem(row, 6, QTableWidgetItem(str(t.get("video_path", ""))[:120]))

        log.debug("Очередь обновлена в окне: %d задач", len(tasks))

    # ------------------------------------------------------------------
    # Действия
    # ------------------------------------------------------------------
    def _selected_task_id(self) -> str:
        row = self.table.currentRow()
        if row < 0:
            return ""
        item = self.table.item(row, 0)
        return item.text() if item else ""

    def _retry_selected(self) -> None:
        task_id = self._selected_task_id()
        if not task_id:
            QMessageBox.warning(self, "Очередь", "Выберите задачу")
            return
        log.info("Ручной повтор задачи %s", task_id)
        self.processor.retry_task(task_id)
        self.refresh()

    def _retry_all_failed(self) -> None:
        failed = self.task_queue.get_failed_tasks()
        if not failed:
            QMessageBox.information(self, "Очередь", "Нет задач с ошибками")
            return
        if QMessageBox.question(
            self, "Повторить все",
            f"Поставить в очередь {len(failed)} задач?",
        ) != QMessageBox.StandardButton.Yes:
            return
        for t in failed:
            self.processor.retry_task(t.get("task_id", ""))
        self.refresh()

    def _delete_selected(self) -> None:
        task_id = self._selected_task_id()
        if not task_id:
            QMessageBox.warning(self, "Очередь", "Выберите задачу")
            return
        if QMessageBox.question(
            self, "Удалить задачу",
            f"Удалить задачу {task_id} из очереди?",
        ) != QMessageBox.StandardButton.Yes:
            return
        self.task_queue.remove_task(task_id)
        log.info("Задача %s удалена вручную", task_id)
        self.refresh()

    def _clear_completed(self) -> None:
        n = self.task_queue.clear_completed()
        QMessageBox.information(self, "Очередь", f"Удалено: {n}")
        self.refresh()