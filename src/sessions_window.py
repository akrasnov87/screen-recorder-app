"""Окно со списком всех записей (сессий)."""
from __future__ import annotations

import json
import os
import shutil
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QFileDialog, QFrame, QHBoxLayout, QHeaderView,
    QLabel, QMenu, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from .logger import get_logger
from .task_queue import TaskQueue

log = get_logger(__name__)


# --- Статусы ---
STATUS_UPLOADED = "Сохранено"
STATUS_TRANSCRIBING = "Транскрибация"
STATUS_PROCESSED = "Обработан"
STATUS_ERROR = "Ошибка"
STATUS_UNKNOWN = "Неизвестно"


STATUS_COLORS = {
    STATUS_PROCESSED:    "#2E7D32",
    STATUS_UPLOADED:     "#37474F",
    STATUS_TRANSCRIBING: "#B8860B",
    STATUS_ERROR:        "#C62828",
    STATUS_UNKNOWN:      "#616161",
}


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


class SessionsWindow(QDialog):
    """Список всех записей в папке sessions/."""

    def __init__(
        self,
        sessions_root: str,
        task_queue: TaskQueue,
        processor,
        config_manager=None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.sessions_root = sessions_root
        self.task_queue = task_queue
        self.processor = processor
        self.config_manager = config_manager
        self.setWindowTitle("Записи")
        self.setMinimumSize(1280, 720)
        self.setModal(False)
        self._rows: List[Dict[str, Any]] = []

        self._build_ui()
        self.refresh()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        header = QHBoxLayout()
        self.summary_label = QLabel("")
        header.addWidget(self.summary_label)
        header.addStretch()
        self.refresh_btn = QPushButton("Обновить")
        self.refresh_btn.clicked.connect(self.refresh)
        header.addWidget(self.refresh_btn)
        root.addLayout(header)

        # 9 колонок
        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels([
            "Дата и время", "Название", "Проект", "Статус",
            "Источник", "Скрам", "Вложения", "Task ID", "Папка",
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
        hv = self.table.horizontalHeader()
        hv.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hv.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hv.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hv.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hv.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        hv.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        hv.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        hv.setSectionResizeMode(7, QHeaderView.ResizeMode.ResizeToContents)
        hv.setSectionResizeMode(8, QHeaderView.ResizeMode.Stretch)
        root.addWidget(self.table)

        root.addWidget(self._build_legend())

        actions = QHBoxLayout()

        self.open_folder_btn = QPushButton("Открыть папку")
        self.open_folder_btn.clicked.connect(self._open_folder)
        actions.addWidget(self.open_folder_btn)

        self.open_video_btn = QPushButton("Открыть видео")
        self.open_video_btn.clicked.connect(self._open_video)
        actions.addWidget(self.open_video_btn)

        self.open_prompt_btn = QPushButton("Открыть промпт DeepSeek")
        self.open_prompt_btn.setToolTip(
            "Открыть сгенерированный промпт DeepSeek (только для скрам-митингов)"
        )
        self.open_prompt_btn.clicked.connect(self._open_deepseek_prompt)
        actions.addWidget(self.open_prompt_btn)

        self.export_prompt_btn = QPushButton("Экспорт промпта…")
        self.export_prompt_btn.setToolTip(
            "Сохранить промпт DeepSeek в выбранном формате (docx / md / txt)"
        )
        self.export_prompt_btn.clicked.connect(self._export_prompt)
        actions.addWidget(self.export_prompt_btn)

        self.open_attachments_btn = QPushButton("Открыть вложения")
        self.open_attachments_btn.setToolTip(
            "Открыть папку с вложениями этой записи"
        )
        self.open_attachments_btn.clicked.connect(self._open_attachments)
        actions.addWidget(self.open_attachments_btn)

        self.add_attachment_btn = QPushButton("Добавить вложение…")
        self.add_attachment_btn.setToolTip(
            "Добавить файл(ы) к существующей записи и обновить session.json"
        )
        self.add_attachment_btn.clicked.connect(self._add_attachment_to_session)
        actions.addWidget(self.add_attachment_btn)

        self.restart_btn = QPushButton("Перезапустить")
        self.restart_btn.clicked.connect(self._restart_processing)
        actions.addWidget(self.restart_btn)

        self.status_btn = QPushButton("Изменить статус ▾")
        self._build_status_menu()
        actions.addWidget(self.status_btn)

        self.delete_btn = QPushButton("Удалить")
        self.delete_btn.clicked.connect(self._delete_session)
        actions.addWidget(self.delete_btn)

        actions.addStretch()
        self.close_btn = QPushButton("Закрыть")
        self.close_btn.clicked.connect(self.close)
        actions.addWidget(self.close_btn)

        root.addLayout(actions)

    def _build_status_menu(self) -> None:
        menu = QMenu(self)

        act_uploaded = menu.addAction("Сбросить в «Сохранено»")
        act_uploaded.triggered.connect(lambda: self._change_status(STATUS_UPLOADED))

        act_processed = menu.addAction("Пометить как «Обработан»")
        act_processed.triggered.connect(lambda: self._change_status(STATUS_PROCESSED))

        act_error = menu.addAction("Пометить как «Ошибка»")
        act_error.triggered.connect(lambda: self._change_status(STATUS_ERROR))

        act_transcribing = menu.addAction("Поставить в очередь")
        act_transcribing.triggered.connect(self._enqueue_current)

        menu.addSeparator()
        act_refresh = menu.addAction("Обновить список")
        act_refresh.triggered.connect(self.refresh)

        self.status_btn.setMenu(menu)

    def _build_legend(self) -> QWidget:
        box = QFrame()
        box.setFrameShape(QFrame.Shape.StyledPanel)
        box.setStyleSheet(
            "QFrame { background-color: palette(window); "
            "border: 1px solid palette(mid); border-radius: 6px; }"
        )
        layout = QHBoxLayout(box)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(18)

        layout.addWidget(QLabel("<b>Статусы:</b>"))
        items = [
            (STATUS_UPLOADED, "видео сохранено, обработка не выполнялась"),
            (STATUS_TRANSCRIBING, "задача в очереди: конвертация/транскрибация"),
            (STATUS_PROCESSED, "все шаги завершены — есть audio и/или transcript"),
            (STATUS_ERROR, "задача завершилась с ошибкой; доступен ручной перезапуск"),
            (STATUS_UNKNOWN, "не удалось определить состояние"),
        ]
        for status, hint in items:
            layout.addWidget(self._make_legend_item(status, hint))
        layout.addStretch()
        return box

    @staticmethod
    def _make_legend_item(status: str, hint: str) -> QWidget:
        w = QWidget()
        w.setToolTip(hint)
        row = QHBoxLayout(w)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        swatch = QLabel()
        swatch.setFixedSize(12, 12)
        swatch.setStyleSheet(
            f"background-color: {STATUS_COLORS.get(status, '#616161')}; "
            f"border-radius: 2px;"
        )
        row.addWidget(swatch)
        text = QLabel(status)
        row.addWidget(text)
        hint_lbl = QLabel("?")
        hint_lbl.setToolTip(hint)
        hint_lbl.setStyleSheet("color: gray; font-weight: bold; padding-left: 2px;")
        row.addWidget(hint_lbl)
        return w

    # ------------------------------------------------------------------
    # Сбор данных
    # ------------------------------------------------------------------
    def _collect_sessions(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        if not os.path.isdir(self.sessions_root):
            log.warning("Папка сессий не найдена: %s", self.sessions_root)
            return rows

        tasks_index: Dict[str, Dict[str, Any]] = {}
        try:
            for t in self.task_queue.get_queue():
                vp = t.get("video_path", "")
                if vp:
                    tasks_index[os.path.abspath(vp)] = t
        except Exception as exc:
            log.warning("Не удалось получить очередь: %s", exc)

        try:
            entries = sorted(os.listdir(self.sessions_root))
        except OSError as exc:
            log.error("Не удалось прочитать %s: %s", self.sessions_root, exc)
            return rows

        for name in entries:
            session_dir = os.path.join(self.sessions_root, name)
            if not os.path.isdir(session_dir):
                continue

            meta_path = os.path.join(session_dir, "session.json")
            meta = _read_json(meta_path) or {}

            video_path = os.path.join(session_dir, "video.mp4")
            has_video = os.path.exists(video_path)
            has_transcript = os.path.exists(os.path.join(session_dir, "video.txt"))
            has_audio = os.path.exists(os.path.join(session_dir, "video.mp3"))

            task = tasks_index.get(os.path.abspath(video_path))
            task_id = (task or {}).get("task_id", "")

            if task is not None:
                st = task.get("status", "")
                if st == "completed":
                    status = STATUS_PROCESSED
                elif st == "error":
                    status = STATUS_ERROR
                elif st in ("pending", "converting", "transcribing"):
                    status = STATUS_TRANSCRIBING
                else:
                    status = STATUS_UNKNOWN
            else:
                if has_transcript or has_audio:
                    status = STATUS_PROCESSED
                elif has_video:
                    status = STATUS_UPLOADED
                else:
                    status = STATUS_UNKNOWN

            date_str = meta.get("date") or ""
            time_str = ""
            try:
                parts = name.split("_")
                if len(parts) >= 2:
                    date_str = date_str or parts[0]
                    time_str = parts[1].replace("-", ":")
            except Exception:
                pass
            if not date_str:
                try:
                    import datetime as _dt
                    ts = os.path.getmtime(session_dir)
                    date_str = _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                    time_str = _dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S")
                except Exception:
                    pass

            # DeepSeek-промпт
            prompt_path = ""
            for fname in (
                "deepseek_prompt.docx", "deepseek_prompt.md",
                "deepseek_prompt.txt",
            ):
                p = os.path.join(session_dir, fname)
                if os.path.exists(p):
                    prompt_path = p
                    break

            # Вложения
            attachments = list(meta.get("attachments", []) or [])
            attachments_dir = os.path.join(session_dir, "attachments")
            if not attachments and os.path.isdir(attachments_dir):
                attachments = [
                    os.path.join(attachments_dir, f)
                    for f in sorted(os.listdir(attachments_dir))
                    if os.path.isfile(os.path.join(attachments_dir, f))
                ]

            rows.append({
                "dir": session_dir,
                "name": meta.get("name") or name,
                "project": meta.get("project") or "",
                "status": status,
                "source": meta.get("source") or "record",
                "is_scrum": bool(meta.get("is_scrum", False)),
                "datetime": f"{date_str} {time_str}".strip(),
                "video_path": video_path,
                "has_video": has_video,
                "task_id": task_id,
                "prompt_path": prompt_path,
                "attachments": attachments,
            })

        rows.sort(key=lambda r: r["datetime"], reverse=True)
        return rows

    # ------------------------------------------------------------------
    # Обновление
    # ------------------------------------------------------------------
    def refresh(self) -> None:
        self._rows = self._collect_sessions()

        total = len(self._rows)
        processed = sum(1 for r in self._rows if r["status"] == STATUS_PROCESSED)
        uploaded = sum(1 for r in self._rows if r["status"] == STATUS_UPLOADED)
        in_progress = sum(1 for r in self._rows
                          if r["status"] == STATUS_TRANSCRIBING)
        errors = sum(1 for r in self._rows if r["status"] == STATUS_ERROR)

        self.summary_label.setText(
            f"Всего: {total} | Обработан: {processed} | "
            f"Сохранено: {uploaded} | В обработке: {in_progress} | "
            f"Ошибок: {errors}"
        )

        self.table.setRowCount(0)
        for r in self._rows:
            row = self.table.rowCount()
            self.table.insertRow(row)

            self.table.setItem(row, 0, QTableWidgetItem(r["datetime"]))
            self.table.setItem(row, 1, QTableWidgetItem(r["name"]))
            self.table.setItem(row, 2, QTableWidgetItem(r["project"]))

            status_item = QTableWidgetItem(r["status"])
            if r["status"] == STATUS_ERROR:
                status_item.setForeground(Qt.GlobalColor.red)
            elif r["status"] == STATUS_PROCESSED:
                status_item.setForeground(Qt.GlobalColor.darkGreen)
            elif r["status"] == STATUS_TRANSCRIBING:
                status_item.setForeground(Qt.GlobalColor.darkYellow)
            self.table.setItem(row, 3, status_item)

            src = "Загрузка" if r["source"] == "upload" else "Запись"
            self.table.setItem(row, 4, QTableWidgetItem(src))
            self.table.setItem(row, 5, QTableWidgetItem("да" if r["is_scrum"] else "—"))

            att_count = len(r.get("attachments", []) or [])
            self.table.setItem(
                row, 6, QTableWidgetItem(str(att_count) if att_count else "—")
            )

            self.table.setItem(row, 7, QTableWidgetItem(r["task_id"] or "—"))
            self.table.setItem(row, 8, QTableWidgetItem(r["dir"]))

        log.debug("Список записей обновлён: %d сессий", total)

    # ------------------------------------------------------------------
    # Внутренние операции
    # ------------------------------------------------------------------
    def _remove_from_queue(self, task_id: str) -> bool:
        if not task_id:
            return False
        try:
            self.task_queue.remove_task(task_id)
            log.info("Задача %s удалена из очереди", task_id)
            return True
        except Exception as exc:
            log.warning("Не удалось удалить задачу %s: %s", task_id, exc)
            return False

    def _delete_processed_files(self, session_dir: str) -> None:
        for fname in ("video.mp3", "video.txt", "video.aac",
                      "video.wav", "video.opus"):
            p = os.path.join(session_dir, fname)
            if os.path.exists(p):
                try:
                    os.remove(p)
                    log.info("Удалён файл: %s", p)
                except Exception as exc:
                    log.warning("Не удалось удалить %s: %s", p, exc)

    def _enqueue_session(self, r: Dict[str, Any]) -> Optional[str]:
        if not os.path.exists(r["video_path"]):
            log.error("Видео не найдено: %s", r["video_path"])
            return None
        meta = _read_json(os.path.join(r["dir"], "session.json")) or {}
        meta["video_path"] = r["video_path"]
        meta["session_dir"] = r["dir"]
        meta.pop("task_id", None)
        try:
            task_id = self.task_queue.add_task(
                {"video_path": r["video_path"], "metadata": meta}
            )
            log.info("Задача %s добавлена в очередь (session=%s)",
                     task_id, r["dir"])
            return task_id
        except Exception as exc:
            log.exception("Не удалось добавить задачу: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Перезапуск / статус
    # ------------------------------------------------------------------
    def _restart_processing(self) -> None:
        r = self._selected_row()
        if not r:
            QMessageBox.warning(self, "Записи", "Выберите запись")
            return
        if not os.path.exists(r["video_path"]):
            QMessageBox.warning(self, "Записи",
                                f"Видео не найдено:\n{r['video_path']}")
            return
        if QMessageBox.question(
            self, "Перезапустить обработку",
            f"Запись «{r['name']}» будет обработана заново:\n"
            "• старая задача удалится из очереди,\n"
            "• файлы video.mp3 / video.txt удалятся,\n"
            "• запись добавится в очередь заново.\n\nПродолжить?",
        ) != QMessageBox.StandardButton.Yes:
            return
        if r["task_id"]:
            self._remove_from_queue(r["task_id"])
        self._delete_processed_files(r["dir"])
        task_id = self._enqueue_session(r)
        if task_id:
            QMessageBox.information(self, "Записи",
                                    f"Запись добавлена в очередь: {task_id}")
            self.refresh()
        else:
            QMessageBox.critical(self, "Ошибка",
                                 "Не удалось добавить запись в очередь")

    def _change_status(self, new_status: str) -> None:
        r = self._selected_row()
        if not r:
            QMessageBox.warning(self, "Записи", "Выберите запись")
            return
        confirm_msg = {
            STATUS_UPLOADED:
                f"Сбросить статус «{r['name']}» в «Сохранено»?\n\n"
                "• задача удалится из очереди,\n"
                "• файлы video.mp3 / video.txt удалятся.",
            STATUS_PROCESSED:
                f"Пометить «{r['name']}» как «Обработан»?\n\n"
                "• задача удалится из очереди,\n"
                "• файлы video.mp3 / video.txt останутся.",
            STATUS_ERROR:
                f"Пометить «{r['name']}» как «Ошибка»?\n\n"
                "• задача удалится из очереди,\n"
                "• файлы видео и транскрипта останутся.",
        }.get(new_status, f"Изменить статус на «{new_status}»?")
        if QMessageBox.question(self, "Изменить статус", confirm_msg) != \
                QMessageBox.StandardButton.Yes:
            return
        if r["task_id"]:
            self._remove_from_queue(r["task_id"])
        if new_status == STATUS_UPLOADED:
            self._delete_processed_files(r["dir"])
        if new_status == STATUS_PROCESSED:
            has_audio = os.path.exists(os.path.join(r["dir"], "video.mp3"))
            has_txt = os.path.exists(os.path.join(r["dir"], "video.txt"))
            if not (has_audio or has_txt):
                QMessageBox.warning(
                    self, "Записи",
                    "Не найдены video.mp3 или video.txt — файлы обработки "
                    "отсутствуют.\n\nСтатус изменён не будет. "
                    "Используйте «Перезапустить», если нужно обработать запись.",
                )
                self.refresh()
                return
        log.info("Ручное изменение статуса %s: %s → %s",
                 r["dir"], r["status"], new_status)
        self.refresh()

    def _enqueue_current(self) -> None:
        r = self._selected_row()
        if not r:
            QMessageBox.warning(self, "Записи", "Выберите запись")
            return
        if r["task_id"]:
            QMessageBox.information(self, "Записи",
                                    f"Запись уже в очереди: {r['task_id']}")
            return
        if not os.path.exists(r["video_path"]):
            QMessageBox.warning(self, "Записи",
                                f"Видео не найдено:\n{r['video_path']}")
            return
        task_id = self._enqueue_session(r)
        if task_id:
            QMessageBox.information(self, "Записи",
                                    f"Запись добавлена в очередь: {task_id}")
            self.refresh()
        else:
            QMessageBox.critical(self, "Ошибка",
                                 "Не удалось добавить запись в очередь")

    # ------------------------------------------------------------------
    # DeepSeek-промпт
    # ------------------------------------------------------------------
    def _open_deepseek_prompt(self) -> None:
        r = self._selected_row()
        if not r:
            QMessageBox.warning(self, "Записи", "Выберите запись")
            return
        path = r.get("prompt_path", "")
        if not path or not os.path.exists(path):
            QMessageBox.information(
                self, "DeepSeek",
                "Для этой записи промпт не сформирован.\n\n"
                "Промпт создаётся автоматически, только если запись помечена "
                "как «Скрам-митинг».",
            )
            return
        log.info("Открытие DeepSeek-промпта: %s", path)
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _export_prompt(self) -> None:
        r = self._selected_row()
        if not r:
            QMessageBox.warning(self, "Записи", "Выберите запись")
            return
        path = r.get("prompt_path", "")
        if not path or not os.path.exists(path):
            QMessageBox.information(
                self, "DeepSeek",
                "Для этой записи промпт не сформирован.",
            )
            return

        preferred = "docx"
        if self.config_manager is not None:
            try:
                preferred = self.config_manager.get_scrum_settings().get(
                    "export_format", "docx"
                )
            except Exception:
                pass

        from PySide6.QtWidgets import QInputDialog
        formats = ["docx", "md", "txt"]
        try:
            pref_idx = formats.index(preferred)
        except ValueError:
            pref_idx = 0

        fmt, ok = QInputDialog.getItem(
            self,
            "Экспорт промпта",
            "Формат файла:",
            formats,
            pref_idx,
            False,
        )
        if not ok or not fmt:
            return

        text = ""
        try:
            if path.endswith(".docx"):
                try:
                    from docx import Document
                    doc = Document(path)
                    text = "\n".join(p.text for p in doc.paragraphs)
                except ImportError:
                    QMessageBox.warning(
                        self, "Экспорт",
                        "python-docx не установлен — невозможно прочитать .docx.",
                    )
                    return
            else:
                with open(path, "r", encoding="utf-8") as f:
                    text = f.read()
        except Exception as exc:
            log.exception("Не удалось прочитать промпт: %s", exc)
            QMessageBox.critical(self, "Экспорт", f"Ошибка чтения: {exc}")
            return

        base_name = os.path.splitext(os.path.basename(path))[0]
        default_path = os.path.join(r["dir"], f"{base_name}.{fmt}")
        target_path, _ = QFileDialog.getSaveFileName(
            self,
            "Сохранить промпт как",
            default_path,
            "Word (*.docx);;Markdown (*.md);;Text (*.txt);;All files (*)",
        )
        if not target_path:
            return

        try:
            if target_path.endswith(".docx"):
                from docx import Document
                doc = Document()
                for line in text.splitlines():
                    doc.add_paragraph(line)
                doc.save(target_path)
            else:
                with open(target_path, "w", encoding="utf-8") as f:
                    f.write(text)
            log.info("Промпт экспортирован: %s", target_path)
            QMessageBox.information(self, "Экспорт",
                                    f"Файл сохранён:\n{target_path}")
        except Exception as exc:
            log.exception("Ошибка сохранения промпта: %s", exc)
            QMessageBox.critical(self, "Экспорт", f"Не удалось сохранить: {exc}")

    # ------------------------------------------------------------------
    # Вложения
    # ------------------------------------------------------------------
    def _open_attachments(self) -> None:
        r = self._selected_row()
        if not r:
            QMessageBox.warning(self, "Записи", "Выберите запись")
            return
        att_dir = os.path.join(r["dir"], "attachments")
        if not os.path.isdir(att_dir):
            QMessageBox.information(
                self, "Вложения",
                "У этой записи нет вложений.",
            )
            return
        log.info("Открытие папки вложений: %s", att_dir)
        QDesktopServices.openUrl(QUrl.fromLocalFile(att_dir))

    def _add_attachment_to_session(self) -> None:
        """Добавляет файл(ы) в <session_dir>/attachments/ и обновляет session.json."""
        r = self._selected_row()
        if not r:
            QMessageBox.warning(self, "Записи", "Выберите запись")
            return

        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Выберите файлы-вложения",
            os.path.expanduser("~"),
            "Документы (*.txt *.md *.docx *.pdf *.csv *.json);;Все файлы (*)",
        )
        if not files:
            return

        att_dir = os.path.join(r["dir"], "attachments")
        os.makedirs(att_dir, exist_ok=True)

        session_json = os.path.join(r["dir"], "session.json")
        meta = _read_json(session_json) or {}
        current = list(meta.get("attachments", []) or [])

        added = 0
        for src in files:
            if not os.path.exists(src):
                continue
            base = os.path.basename(src)
            dst = os.path.join(att_dir, base)
            if os.path.exists(dst):
                stem, ext = os.path.splitext(base)
                i = 1
                while os.path.exists(dst):
                    dst = os.path.join(att_dir, f"{stem}_{i}{ext}")
                    i += 1
            try:
                shutil.copy2(src, dst)
                current.append(dst)
                added += 1
                log.info("Добавлено вложение: %s → %s", src, dst)
            except Exception as exc:
                log.exception("Ошибка копирования вложения %s: %s", src, exc)

        if added:
            meta["attachments"] = current
            try:
                with open(session_json, "w", encoding="utf-8") as f:
                    json.dump(meta, f, indent=2, ensure_ascii=False)
            except Exception as exc:
                log.exception("Не удалось обновить session.json: %s", exc)
                QMessageBox.critical(self, "Ошибка",
                                     f"Не удалось обновить session.json:\n{exc}")
                return
            QMessageBox.information(
                self, "Вложения",
                f"Добавлено файлов: {added}\n\n"
                "Чтобы вложения попали в транскрибацию или промпт DeepSeek, "
                "установите соответствующие флаги в session.json и нажмите "
                "«Перезапустить».",
            )
            self.refresh()

    # ------------------------------------------------------------------
    # Обычные действия
    # ------------------------------------------------------------------
    def _selected_row(self) -> Optional[Dict[str, Any]]:
        i = self.table.currentRow()
        if i < 0 or i >= len(self._rows):
            return None
        return self._rows[i]

    def _open_folder(self) -> None:
        r = self._selected_row()
        if not r:
            QMessageBox.warning(self, "Записи", "Выберите запись")
            return
        folder = r["dir"]
        if not os.path.isdir(folder):
            QMessageBox.warning(self, "Записи", f"Папка не найдена:\n{folder}")
            return
        log.info("Открытие папки сессии: %s", folder)
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def _open_video(self) -> None:
        r = self._selected_row()
        if not r:
            QMessageBox.warning(self, "Записи", "Выберите запись")
            return
        video = r["video_path"]
        if not os.path.exists(video):
            QMessageBox.warning(self, "Записи", f"Видео не найдено:\n{video}")
            return
        log.info("Открытие видео: %s", video)
        QDesktopServices.openUrl(QUrl.fromLocalFile(video))

    def _delete_session(self) -> None:
        r = self._selected_row()
        if not r:
            QMessageBox.warning(self, "Записи", "Выберите запись")
            return
        if QMessageBox.question(
            self, "Удалить запись",
            f"Удалить папку записи «{r['name']}» со всем содержимым?\n\n"
            f"{r['dir']}\n\nДействие необратимо.",
        ) != QMessageBox.StandardButton.Yes:
            return
        if r["task_id"]:
            self._remove_from_queue(r["task_id"])
        try:
            shutil.rmtree(r["dir"])
            log.warning("Папка сессии удалена: %s", r["dir"])
            self.refresh()
        except Exception as exc:
            log.exception("Не удалось удалить %s: %s", r["dir"], exc)
            QMessageBox.critical(self, "Ошибка", f"Не удалось удалить: {exc}")