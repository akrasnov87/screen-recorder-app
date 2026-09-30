"""Встроенный медиаплеер для просмотра видео и прослушивания аудио.

Использует Qt Multimedia (PySide6.QtMultimedia + QtMultimediaWidgets).
Если модуль недоступен (не установлен PySide6-Addons или нет
необходимых GStreamer-плагинов), плеер работает в режиме
«только fallback»: открывает файл системным приложением и
логирует предупреждение.

Основная причина использования встроенного плеера — не полагаться
на системный VLC/xdg-open, который в snap-окружении может падать
с ошибкой «symbol lookup error: undefined symbol __libc_pthread_init».
"""
from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QSlider,
    QStyle, QVBoxLayout, QWidget,
)

from .logger import get_logger

log = get_logger(__name__)


# --- Проверка доступности QtMultimedia ---
_QT_MULTIMEDIA_AVAILABLE = False
_QT_MULTIMEDIA_IMPORT_ERROR: str = ""

try:
    from PySide6.QtMultimedia import (  # type: ignore
        QAudioOutput, QMediaPlayer,
    )
    from PySide6.QtMultimediaWidgets import (  # type: ignore
        QVideoWidget,
    )
    _QT_MULTIMEDIA_AVAILABLE = True
    log.debug("QtMultimedia доступен — встроенный плеер активен")
except Exception as exc:  # pragma: no cover
    _QT_MULTIMEDIA_IMPORT_ERROR = str(exc)
    log.warning(
        "QtMultimedia недоступен: %s. Встроенный плеер будет "
        "использовать fallback на системное приложение.",
        exc,
    )


def is_builtin_player_available() -> bool:
    """Возвращает True, если встроенный плеер доступен."""
    return _QT_MULTIMEDIA_AVAILABLE


def get_builtin_player_error() -> str:
    """Возвращает текст ошибки импорта QtMultimedia (если был)."""
    return _QT_MULTIMEDIA_IMPORT_ERROR


def _format_duration(ms: int) -> str:
    """Форматирует миллисекунды в mm:ss (или hh:mm:ss)."""
    if ms <= 0:
        return "00:00"
    total_sec = ms // 1000
    hours = total_sec // 3600
    minutes = (total_sec % 3600) // 60
    seconds = total_sec % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


# ============================================================================
# Встроенный плеер
# ============================================================================
class MediaPlayerDialog(QDialog):
    """
    Модальное окно встроенного плеера.

    Автоматически определяет тип медиа по расширению:
      • видео — показывается видеовиджет;
      • аудио — видеовиджет скрывается, показывается заглушка.

    Поддерживаются горячие клавиши:
      • Space       — play/pause
      • ←/→         — перемотка на 5 секунд
      • ↑/↓         — громкость ±5%
      • Esc         — закрыть окно
    """

    position_changed = Signal(int)  # ms
    duration_changed = Signal(int)  # ms

    VIDEO_EXTS = (
        ".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".wmv",
        ".m4v", ".mpg", ".mpeg", ".3gp",
    )
    AUDIO_EXTS = (
        ".mp3", ".wav", ".m4a", ".aac", ".opus", ".ogg", ".flac",
    )

    def __init__(
        self,
        file_path: str,
        *,
        title: str = "",
        window_width: int = 960,
        window_height: int = 640,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)

        if not _QT_MULTIMEDIA_AVAILABLE:
            raise RuntimeError(
                "Встроенный плеер недоступен: QtMultimedia не "
                "установлен или не поддерживается системой.\n"
                f"Причина: {_QT_MULTIMEDIA_IMPORT_ERROR or 'неизвестна'}"
            )

        if not file_path or not os.path.isfile(file_path):
            raise FileNotFoundError(f"Файл не найден: {file_path}")

        self._file_path = file_path
        self._is_audio = self._detect_is_audio(file_path)

        self.setWindowTitle(
            title or os.path.basename(file_path) or "Медиаплеер"
        )
        self.setModal(False)
        self.resize(int(window_width), int(window_height))
        self.setMinimumSize(480, 320)

        self._build_ui()
        self._setup_player()
        self._connect_shortcuts()

        self._load(file_path)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        # --- Заголовок с именем файла ---
        self.title_label = QLabel(os.path.basename(self._file_path))
        self.title_label.setStyleSheet(
            "QLabel { color: #444; font-weight: bold; }"
        )
        self.title_label.setWordWrap(True)
        root.addWidget(self.title_label)

        # --- Видео / аудио-заглушка ---
        if not self._is_audio:
            self.video_widget = QVideoWidget(self)
            self.video_widget.setStyleSheet(
                "QVideoWidget { background-color: black; }"
            )
            root.addWidget(self.video_widget, 1)
        else:
            self.video_widget = None
            audio_placeholder = QLabel(
                "🎵  Воспроизведение аудио\n\n"
                f"{os.path.basename(self._file_path)}"
            )
            audio_placeholder.setAlignment(
                Qt.AlignmentFlag.AlignCenter
            )
            audio_placeholder.setStyleSheet(
                "QLabel {"
                "  background-color: #1e1e1e;"
                "  color: #d0d0d0;"
                "  font-size: 16px;"
                "  padding: 40px;"
                "  border-radius: 6px;"
                "}"
            )
            root.addWidget(audio_placeholder, 1)

        # --- Позиция / длительность ---
        progress_row = QHBoxLayout()

        self.position_label = QLabel("00:00")
        self.position_label.setMinimumWidth(56)
        self.position_label.setAlignment(
            Qt.AlignmentFlag.AlignRight
            | Qt.AlignmentFlag.AlignVCenter
        )
        progress_row.addWidget(self.position_label)

        self.position_slider = QSlider(Qt.Orientation.Horizontal)
        self.position_slider.setRange(0, 0)
        self.position_slider.setToolTip(
            "Перемотка. Можно кликнуть по любой точке шкалы."
        )
        progress_row.addWidget(self.position_slider, 1)

        self.duration_label = QLabel("00:00")
        self.duration_label.setMinimumWidth(56)
        self.duration_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft
            | Qt.AlignmentFlag.AlignVCenter
        )
        progress_row.addWidget(self.duration_label)

        root.addLayout(progress_row)

        # --- Управление ---
        controls = QHBoxLayout()

        style = self.style()

        self.play_btn = QPushButton()
        self.play_btn.setIcon(
            style.standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
        )
        self.play_btn.setToolTip(
            "Воспроизведение / пауза (Space)"
        )
        self.play_btn.setFixedSize(44, 32)
        self.play_btn.clicked.connect(self._toggle_play)
        controls.addWidget(self.play_btn)

        self.stop_btn = QPushButton()
        self.stop_btn.setIcon(
            style.standardIcon(QStyle.StandardPixmap.SP_MediaStop)
        )
        self.stop_btn.setToolTip("Стоп")
        self.stop_btn.setFixedSize(44, 32)
        self.stop_btn.clicked.connect(self._stop)
        controls.addWidget(self.stop_btn)

        controls.addSpacing(12)

        self.back_btn = QPushButton()
        self.back_btn.setIcon(
            style.standardIcon(QStyle.StandardPixmap.SP_MediaSeekBackward)
        )
        self.back_btn.setToolTip("Назад на 5 секунд (←)")
        self.back_btn.setFixedSize(36, 32)
        self.back_btn.clicked.connect(lambda: self._seek_relative(-5000))
        controls.addWidget(self.back_btn)

        self.fwd_btn = QPushButton()
        self.fwd_btn.setIcon(
            style.standardIcon(QStyle.StandardPixmap.SP_MediaSeekForward)
        )
        self.fwd_btn.setToolTip("Вперёд на 5 секунд (→)")
        self.fwd_btn.setFixedSize(36, 32)
        self.fwd_btn.clicked.connect(lambda: self._seek_relative(5000))
        controls.addWidget(self.fwd_btn)

        controls.addSpacing(12)

        self.volume_label = QLabel("🔊")
        controls.addWidget(self.volume_label)

        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(80)
        self.volume_slider.setFixedWidth(140)
        self.volume_slider.setToolTip("Громкость (↑/↓)")
        self.volume_slider.valueChanged.connect(self._on_volume_changed)
        controls.addWidget(self.volume_slider)

        controls.addStretch()

        self.status_label = QLabel("")
        self.status_label.setStyleSheet(
            "QLabel { color: #666; }"
        )
        controls.addWidget(self.status_label)

        self.open_external_btn = QPushButton("Открыть снаружи")
        self.open_external_btn.setToolTip(
            "Открыть этот файл системным приложением "
            "(может пригодиться, если встроенный плеер не "
            "поддерживает формат)"
        )
        self.open_external_btn.clicked.connect(
            self._open_external
        )
        controls.addWidget(self.open_external_btn)

        self.close_btn = QPushButton("Закрыть")
        self.close_btn.clicked.connect(self.close)
        controls.addWidget(self.close_btn)

        root.addLayout(controls)

    def _connect_shortcuts(self) -> None:
        QShortcut(QKeySequence("Space"), self).activated.connect(
            self._toggle_play
        )
        QShortcut(QKeySequence("Left"), self).activated.connect(
            lambda: self._seek_relative(-5000)
        )
        QShortcut(QKeySequence("Right"), self).activated.connect(
            lambda: self._seek_relative(5000)
        )
        QShortcut(QKeySequence("Up"), self).activated.connect(
            lambda: self._adjust_volume(+5)
        )
        QShortcut(QKeySequence("Down"), self).activated.connect(
            lambda: self._adjust_volume(-5)
        )

    # ------------------------------------------------------------------
    # Плеер
    # ------------------------------------------------------------------
    def _setup_player(self) -> None:
        self._player = QMediaPlayer(self)
        self._audio_output = QAudioOutput(self)
        self._audio_output.setVolume(0.8)

        self._player.setAudioOutput(self._audio_output)
        if self.video_widget is not None:
            self._player.setVideoOutput(self.video_widget)

        self._player.positionChanged.connect(self._on_position_changed)
        self._player.durationChanged.connect(self._on_duration_changed)
        self._player.playbackStateChanged.connect(
            self._on_playback_state_changed
        )
        self._player.errorOccurred.connect(self._on_player_error)

        self.position_slider.sliderMoved.connect(self._on_slider_moved)

    def _load(self, file_path: str) -> None:
        url = QUrl.fromLocalFile(os.path.abspath(file_path))
        self._player.setSource(url)
        self._player.play()
        log.info(
            "MediaPlayerDialog: открытие %s (аудио=%s)",
            file_path, self._is_audio,
        )

    @staticmethod
    def _detect_is_audio(path: str) -> bool:
        ext = os.path.splitext(path)[1].lower()
        if ext in MediaPlayerDialog.AUDIO_EXTS:
            return True
        return False

    # ------------------------------------------------------------------
    # Управление
    # ------------------------------------------------------------------
    def _toggle_play(self) -> None:
        state = self._player.playbackState()
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
        else:
            self._player.play()

    def _stop(self) -> None:
        self._player.stop()

    def _seek_relative(self, delta_ms: int) -> None:
        new_pos = max(0, self._player.position() + delta_ms)
        duration = self._player.duration()
        if duration > 0:
            new_pos = min(new_pos, duration)
        self._player.setPosition(new_pos)

    def _on_slider_moved(self, value: int) -> None:
        self._player.setPosition(int(value))

    def _adjust_volume(self, delta: int) -> None:
        new_val = max(0, min(100, self.volume_slider.value() + delta))
        self.volume_slider.setValue(new_val)

    def _on_volume_changed(self, value: int) -> None:
        self._audio_output.setVolume(value / 100.0)

    def _open_external(self) -> None:
        QDesktopServices.openUrl(
            QUrl.fromLocalFile(os.path.abspath(self._file_path))
        )

    # ------------------------------------------------------------------
    # Сигналы плеера
    # ------------------------------------------------------------------
    def _on_position_changed(self, pos: int) -> None:
        if not self.position_slider.isSliderDown():
            self.position_slider.setValue(int(pos))
        self.position_label.setText(_format_duration(pos))
        self.position_changed.emit(pos)

    def _on_duration_changed(self, duration: int) -> None:
        self.position_slider.setRange(0, max(0, duration))
        self.duration_label.setText(_format_duration(duration))
        self.duration_changed.emit(duration)

    def _on_playback_state_changed(self, state) -> None:
        style = self.style()
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self.play_btn.setIcon(
                style.standardIcon(QStyle.StandardPixmap.SP_MediaPause)
            )
            self.status_label.setText("")
        elif state == QMediaPlayer.PlaybackState.PausedState:
            self.play_btn.setIcon(
                style.standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
            )
            self.status_label.setText("Пауза")
        else:
            self.play_btn.setIcon(
                style.standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
            )

    def _on_player_error(self, error, error_string: str) -> None:
        log.warning(
            "MediaPlayerDialog: ошибка плеера %s — %s",
            error, error_string,
        )
        self.status_label.setText(
            f"<span style='color:#c62828'>Ошибка: "
            f"{error_string or error}</span>"
        )

    # ------------------------------------------------------------------
    # Закрытие
    # ------------------------------------------------------------------
    def closeEvent(self, event) -> None:
        try:
            self._player.stop()
        except Exception:
            pass
        super().closeEvent(event)


# ============================================================================
# Универсальная функция открытия медиа
# ============================================================================
def open_media(
    file_path: str,
    *,
    title: str = "",
    prefer_builtin: bool = True,
    window_width: int = 960,
    window_height: int = 640,
    parent: Optional[QWidget] = None,
) -> bool:
    """
    Открывает медиафайл в встроенном плеере (если доступен) либо
    системным приложением.

    Returns:
        True, если использован встроенный плеер.
        False, если выполнен fallback на системное приложение
        (или файл не существует).
    """
    if not file_path or not os.path.isfile(file_path):
        log.warning("open_media: файл не найден: %s", file_path)
        return False

    if prefer_builtin and _QT_MULTIMEDIA_AVAILABLE:
        try:
            dlg = MediaPlayerDialog(
                file_path,
                title=title or os.path.basename(file_path),
                window_width=window_width,
                window_height=window_height,
                parent=parent,
            )
            dlg.show()
            return True
        except Exception as exc:
            log.exception(
                "open_media: не удалось открыть встроенный плеер: %s",
                exc,
            )
            # продолжаем к fallback

    log.info(
        "open_media: fallback на системное приложение для %s",
        file_path,
    )
    QDesktopServices.openUrl(QUrl.fromLocalFile(file_path))
    return False


def probe_media_support() -> str:
    """
    Возвращает человекочитаемый статус поддержки встроенного плеера.

    Удобно для отображения в настройках или диагностики.
    """
    if _QT_MULTIMEDIA_AVAILABLE:
        return "Встроенный плеер доступен (Qt Multimedia)"
    return (
        "Встроенный плеер недоступен: "
        f"{_QT_MULTIMEDIA_IMPORT_ERROR or 'QtMultimedia не установлен'}"
    )