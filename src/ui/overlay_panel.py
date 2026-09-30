"""Плавающая панель управления поверх всех окон.

Изменения:
  • Размеры панели, задержка автоскрытия и число строк лога
    читаются из config["app"]:
      overlay_hide_delay_ms, overlay_log_lines,
      overlay_width, overlay_height.
  • Удалён неиспользуемый метод set_recording_controls_enabled().
"""
from __future__ import annotations

from typing import Dict, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QProgressBar, QPushButton, QTextEdit,
    QVBoxLayout, QWidget,
)

from ..logger import get_logger

log = get_logger(__name__)


class OverlayPanel(QWidget):
    """Панель управления записью поверх всех окон."""

    start_requested = Signal()
    pause_requested = Signal()
    stop_requested = Signal()

    def __init__(
        self,
        config: Optional[Dict] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._config = config or {}
        app_cfg = self._config.get("app", {}) or {}

        self._hide_delay_ms = int(
            app_cfg.get("overlay_hide_delay_ms", 5000)
        )
        self._log_lines = int(app_cfg.get("overlay_log_lines", 5))
        self._panel_width = int(app_cfg.get("overlay_width", 340))
        self._panel_height = int(app_cfg.get("overlay_height", 190))

        log.debug(
            "Инициализация OverlayPanel "
            "(hide=%dмс, log_lines=%d, %dx%d)",
            self._hide_delay_ms, self._log_lines,
            self._panel_width, self._panel_height,
        )

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(self._panel_width, self._panel_height)

        self._drag_pos = None
        self._hide_timer = QTimer(self)
        self._hide_timer.setInterval(self._hide_delay_ms)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide_panel)

        self._build_ui()
        log.debug("OverlayPanel готов")

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)

        self.status_label = QLabel("Готов")
        self.status_label.setStyleSheet(
            "color: white; font-weight: bold;"
        )
        layout.addWidget(self.status_label)

        buttons = QHBoxLayout()
        self.start_btn = QPushButton("●")
        self.start_btn.setToolTip("Начать запись")
        self.start_btn.clicked.connect(self._on_start_clicked)

        self.pause_btn = QPushButton("❚❚")
        self.pause_btn.setToolTip("Пауза")
        self.pause_btn.clicked.connect(self._on_pause_clicked)

        self.stop_btn = QPushButton("■")
        self.stop_btn.setToolTip("Стоп")
        self.stop_btn.clicked.connect(self._on_stop_clicked)

        for b in (self.start_btn, self.pause_btn, self.stop_btn):
            b.setFixedSize(44, 44)
            buttons.addWidget(b)
        buttons.addStretch()
        layout.addLayout(buttons)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFixedHeight(60)
        layout.addWidget(self.log_view)

    # ---------- Обработчики кнопок ----------
    def _on_start_clicked(self) -> None:
        log.info("Overlay: нажата кнопка «Старт»")
        self.start_requested.emit()

    def _on_pause_clicked(self) -> None:
        log.info("Overlay: нажата кнопка «Пауза»")
        self.pause_requested.emit()

    def _on_stop_clicked(self) -> None:
        log.info("Overlay: нажата кнопка «Стоп»")
        self.stop_requested.emit()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(self.rect(), 14, 14)
        painter.fillPath(path, QColor(20, 20, 20, 220))

    # ---------- Публичные методы ----------
    def show_panel(self) -> None:
        log.debug("Overlay: показ панели")
        self.show()
        self.raise_()
        self.start_hide_timer()

    def hide_panel(self) -> None:
        log.debug("Overlay: скрытие панели")
        self.hide()

    def update_status(self, status: str) -> None:
        log.debug("Overlay: статус = %s", status)
        self.status_label.setText(status[:80])

    def update_progress(self, progress: int, task: str = "") -> None:
        log.debug("Overlay: прогресс = %d%%, задача = %s", progress, task)
        self.progress.setValue(max(0, min(100, progress)))
        if task:
            self.status_label.setText(task[:80])

    def add_log(self, message: str) -> None:
        # ВАЖНО: не логируем через log — это привело бы к рекурсии,
        # т.к. GUI-хук логгера сам вызывает этот метод.
        self.log_view.append(message)
        lines = self.log_view.toPlainText().splitlines()
        if len(lines) > self._log_lines:
            self.log_view.setPlainText(
                "\n".join(lines[-self._log_lines:])
            )

    def start_hide_timer(self) -> None:
        log.debug("Overlay: таймер автоскрытия перезапущен")
        self._hide_timer.start()

    # ---------- Перетаскивание ----------
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = (
                event.globalPosition().toPoint()
                - self.frameGeometry().topLeft()
            )
            log.debug("Overlay: начало перетаскивания")
            event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_pos and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        log.debug("Overlay: конец перетаскивания")
        self._drag_pos = None

    def reset(self) -> None:
        """Сбрасывает прогресс, лог, статус и таймер для новой сессии."""
        log.debug("Overlay: сброс состояния")
        self.progress.setValue(0)
        self.log_view.clear()
        self.status_label.setText("Готов")
        self._hide_timer.stop()