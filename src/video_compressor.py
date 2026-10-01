"""Сжатие видео до целевого размера через ffmpeg.

Используется при публикации записи на сервер синхронизации,
когда медиафайл превышает лимит «Макс. размер артефакта».

Алгоритм:
  1. Определяется целевой размер (target_bytes).
  2. Оценивается требуемый видеобитрейт с учётом аудиодорожки
     и минимального порога.
  3. Запускается ffmpeg с -fs <target_bytes> — ffmpeg сам
     остановится, когда размер файла достигнет целевого.
  4. Прогресс считывается из stderr ffmpeg (строки вида
     "frame= 123 fps=...").

Сжатие выполняется в отдельный временный файл, оригинал
перезаписывается только после успешного завершения ffmpeg.
Перед сжатием создаётся резервная копия (.orig), которая
удаляется при успехе и восстанавливается при ошибке.

Изменения:
  • Модуль создан.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

from .logger import get_logger

log = get_logger(__name__)


# Минимальный видеобитрейт (kbps). Ниже — картинка рассыпается
# на квадраты. Верхний предел не задаём: ffmpeg возьмёт столько,
# сколько нужно для достижения target_bytes.
DEFAULT_MIN_VIDEO_BITRATE_KBPS = 200

# Битрейт аудио по умолчанию (kbps).
DEFAULT_AUDIO_BITRATE_KBPS = 96

# Пресет x264. Чем быстрее — тем больше размер при том же
# качестве, но и тем меньше времени на сжатие.
DEFAULT_PRESET = "veryfast"

# Допустимые пресеты x264.
ALLOWED_PRESETS = {
    "ultrafast", "superfast", "veryfast", "faster",
    "fast", "medium", "slow", "slower", "veryslow",
}


class CompressionError(RuntimeError):
    """Ошибка при сжатии видео."""
    def __init__(self, message: str) -> None:
        super().__init__(message)


# ---------------------------------------------------------------------------
# Вспомогательные
# ---------------------------------------------------------------------------
_PROGRESS_RE = re.compile(
    r"time=(\d+):(\d+):(\d+\.\d+)"
)


def _parse_progress_time(line: str) -> Optional[float]:
    """
    Извлекает время из строки ffmpeg.
    Возвращает секунды (float) или None.
    """
    m = _PROGRESS_RE.search(line)
    if not m:
        return None
    h, mm, ss = m.groups()
    return int(h) * 3600 + int(mm) * 60 + float(ss)


async def _get_duration_seconds(path: str) -> float:
    """
    Возвращает длительность видео в секундах через ffprobe.
    Если ffprobe недоступен — 0.
    """
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        text = stdout.decode("utf-8", errors="ignore").strip()
        if not text:
            return 0.0
        return float(text)
    except Exception as exc:
        log.warning(
            "ffprobe: не удалось получить длительность %s: %s",
            path, exc,
        )
        return 0.0


def _estimate_video_bitrate_kbps(
    original_size_bytes: int,
    target_size_bytes: int,
    duration_sec: float,
    audio_bitrate_kbps: int,
    min_video_bitrate_kbps: int,
) -> int:
    """
    Оценивает требуемый видеобитрейт (kbps) для достижения
    target_size_bytes.

    Формула:
        total_bits = target_bytes * 8
        total_kbps = total_bits / duration / 1000
        video_kbps = total_kbps - audio_kbps

    Ограничения:
      • не ниже min_video_bitrate_kbps;
      • не выше исходного битрейта (не раздуваем файл).
    """
    if duration_sec <= 0:
        # Не знаем длительность — используем минимум.
        return min_video_bitrate_kbps

    # Целевое общее количество kbps.
    target_kbps = (target_size_bytes * 8) / duration_sec / 1000.0

    # Видео получает всё, что осталось после аудио.
    video_kbps = target_kbps - audio_bitrate_kbps

    # Ограничения.
    video_kbps = max(min_video_bitrate_kbps, int(video_kbps))

    # Верхняя граница — исходный битрейт.
    original_kbps = (original_size_bytes * 8) / duration_sec / 1000.0
    original_video_kbps = max(
        min_video_bitrate_kbps, int(original_kbps - audio_bitrate_kbps)
    )
    if original_video_kbps > 0:
        video_kbps = min(video_kbps, original_video_kbps)

    return max(min_video_bitrate_kbps, int(video_kbps))


# ---------------------------------------------------------------------------
# Основная функция
# ---------------------------------------------------------------------------
async def compress_video_to_target_size(
    src_path: str,
    target_bytes: int,
    *,
    min_video_bitrate_kbps: int = DEFAULT_MIN_VIDEO_BITRATE_KBPS,
    audio_bitrate_kbps: int = DEFAULT_AUDIO_BITRATE_KBPS,
    preset: str = DEFAULT_PRESET,
    progress_cb: Optional[Callable[[str], None]] = None,
    cancel_event: Optional[asyncio.Event] = None,
) -> str:
    """
    Сжимает видео до целевого размера, перезаписывая оригинал.

    Шаги:
      1. Проверяем существование src_path.
      2. Создаём резервную копию src_path.orig.
      3. Определяем длительность через ffprobe.
      4. Оцениваем требуемый видеобитрейт.
      5. Запускаем ffmpeg с -fs <target_bytes> в temp-файл.
      6. При успехе — os.replace(temp, src_path), удаляем .orig.
      7. При ошибке — восстанавливаем src_path из .orig.

    Args:
        src_path:  путь к исходному видео.
        target_bytes: целевой размер в байтах.
        min_video_bitrate_kbps: минимальный битрейт видео.
        audio_bitrate_kbps: битрейт аудио после сжатия.
        preset:    пресет x264 (ultrafast, veryfast, medium, ...).
        progress_cb: колбэк для отчёта о прогрессе.
        cancel_event: если выставлен — прерываем сжатие.

    Returns:
        Путь к сжатому файлу (совпадает с src_path).

    Raises:
        CompressionError: если сжатие не удалось.
    """
    if not os.path.isfile(src_path):
        raise CompressionError(f"Файл не найден: {src_path}")

    if target_bytes <= 0:
        raise CompressionError(
            f"Некорректный целевой размер: {target_bytes}"
        )

    if preset not in ALLOWED_PRESETS:
        log.warning(
            "Неизвестный пресет x264 %r — используем %r",
            preset, DEFAULT_PRESET,
        )
        preset = DEFAULT_PRESET

    original_size = os.path.getsize(src_path)
    if original_size <= target_bytes:
        log.info(
            "Сжатие не требуется: %s (%d байт ≤ %d байт)",
            src_path, original_size, target_bytes,
        )
        return src_path

    log.info(
        "Сжатие видео: %s (%d МБ → %d МБ, preset=%s)",
        src_path, original_size // (1024 * 1024),
        target_bytes // (1024 * 1024), preset,
    )

    backup_path = src_path + ".orig"
    tmp_path = src_path + ".compressing.mp4"

    # --- Резервная копия оригинала ---
    backup_ok = False
    try:
        shutil.copy2(src_path, backup_path)
        backup_ok = True
        log.info("Создана резервная копия: %s", backup_path)
    except Exception as exc:
        log.warning(
            "Не удалось создать резервную копию %s: %s",
            src_path, exc,
        )

    # --- Длительность ---
    duration = await _get_duration_seconds(src_path)
    log.info("Длительность исходного видео: %.1f с", duration)

    # --- Оценка битрейта ---
    video_kbps = _estimate_video_bitrate_kbps(
        original_size_bytes=original_size,
        target_size_bytes=target_bytes,
        duration_sec=duration,
        audio_bitrate_kbps=audio_bitrate_kbps,
        min_video_bitrate_kbps=min_video_bitrate_kbps,
    )
    log.info(
        "Целевой битрейт: video=%d kbps, audio=%d kbps, preset=%s",
        video_kbps, audio_bitrate_kbps, preset,
    )

    if progress_cb:
        try:
            progress_cb(
                f"Сжатие видео: {os.path.basename(src_path)} "
                f"({original_size // (1024 * 1024)} МБ → "
                f"{target_bytes // (1024 * 1024)} МБ, "
                f"битрейт {video_kbps} kbps)"
            )
        except Exception:
            pass

    # --- Команда ffmpeg ---
    # -fs <target_bytes>: ffmpeg остановится, когда размер
    #                     выходного файла достигнет target_bytes.
    # -maxrate/-bufsize: ограничивают пиковый битрейт, чтобы
    #                     не вылететь за target_bytes раньше времени.
    cmd = [
        "ffmpeg", "-y",
        "-i", src_path,
        "-c:v", "libx264",
        "-preset", preset,
        "-b:v", f"{video_kbps}k",
        "-maxrate", f"{video_kbps}k",
        "-bufsize", f"{video_kbps * 2}k",
        "-c:a", "aac",
        "-b:a", f"{audio_bitrate_kbps}k",
        "-fs", str(target_bytes),
        "-movflags", "+faststart",
        tmp_path,
    ]

    log.debug("ffmpeg команда: %s", " ".join(cmd))

    proc: Optional[asyncio.subprocess.Process] = None
    stderr_tail: list = []

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        # --- Читаем stderr и парсим прогресс ---
        async def _read_stderr() -> None:
            assert proc is not None
            assert proc.stderr is not None
            last_reported = -1.0
            while True:
                line_bytes = await proc.stderr.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode(
                    "utf-8", errors="ignore"
                ).rstrip()
                if line:
                    stderr_tail.append(line)
                    if len(stderr_tail) > 30:
                        stderr_tail.pop(0)

                # Прогресс
                if progress_cb and duration > 0:
                    t = _parse_progress_time(line)
                    if t is not None:
                        pct = min(100, int(t * 100 / duration))
                        if pct >= last_reported + 1:
                            last_reported = pct
                            try:
                                progress_cb(
                                    f"Сжатие видео: {pct}%"
                                )
                            except Exception:
                                pass

        read_task = asyncio.ensure_future(_read_stderr())

        # --- Ожидание завершения с возможностью отмены ---
        if cancel_event is not None:
            wait_task = asyncio.ensure_future(proc.wait())
            cancel_task = asyncio.ensure_future(cancel_event.wait())
            try:
                done, pending = await asyncio.wait(
                    {wait_task, cancel_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancel_task in done:
                    log.info("Сжатие отменено пользователем")
                    try:
                        proc.terminate()
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=10)
                    except asyncio.TimeoutError:
                        try:
                            proc.kill()
                        except ProcessLookupError:
                            pass
                        await proc.wait()
                    wait_task.cancel()
                    raise CompressionError("Сжатие отменено")
                rc = wait_task.result()
            finally:
                cancel_task.cancel()
                if not wait_task.done():
                    wait_task.cancel()
        else:
            rc = await proc.wait()

        await read_task

        if rc != 0:
            last_lines = "\n".join(stderr_tail[-10:])
            raise CompressionError(
                f"ffmpeg вернул код {rc}. "
                f"Последние строки:\n{last_lines}"
            )

        if not os.path.isfile(tmp_path):
            raise CompressionError(
                "ffmpeg завершился успешно, но выходной файл "
                "не создан"
            )

        new_size = os.path.getsize(tmp_path)
        log.info(
            "Сжатие завершено: %d МБ → %d МБ",
            original_size // (1024 * 1024),
            new_size // (1024 * 1024),
        )

        # --- Заменяем оригинал ---
        os.replace(tmp_path, src_path)
        log.info("Оригинал перезаписан: %s", src_path)

        # --- Удаляем резервную копию ---
        if backup_ok:
            try:
                os.remove(backup_path)
                log.debug("Резервная копия удалена: %s", backup_path)
            except Exception as exc:
                log.warning(
                    "Не удалось удалить резервную копию %s: %s",
                    backup_path, exc,
                )

        if progress_cb:
            try:
                progress_cb(
                    f"Сжатие завершено: "
                    f"{new_size // (1024 * 1024)} МБ"
                )
            except Exception:
                pass

        return src_path

    except CompressionError:
        # Восстанавливаем оригинал из резервной копии.
        _restore_from_backup(src_path, backup_path, backup_ok)
        _cleanup_tmp(tmp_path)
        raise
    except Exception as exc:
        log.exception("Ошибка сжатия %s: %s", src_path, exc)
        _restore_from_backup(src_path, backup_path, backup_ok)
        _cleanup_tmp(tmp_path)
        raise CompressionError(f"Сжатие не удалось: {exc}")


def _restore_from_backup(
    src_path: str, backup_path: str, backup_ok: bool,
) -> None:
    """Восстанавливает оригинал из резервной копии."""
    if not backup_ok:
        return
    try:
        if os.path.isfile(src_path):
            # Оригинал мог быть уже частично перезаписан
            # (маловероятно, но на всякий случай).
            return
        if os.path.isfile(backup_path):
            os.replace(backup_path, src_path)
            log.info(
                "Оригинал восстановлен из резервной копии: %s",
                src_path,
            )
    except Exception as exc:
        log.error(
            "Не удалось восстановить оригинал %s: %s",
            src_path, exc,
        )


def _cleanup_tmp(tmp_path: str) -> None:
    """Удаляет временный файл сжатия."""
    try:
        if os.path.isfile(tmp_path):
            os.remove(tmp_path)
            log.debug("Временный файл удалён: %s", tmp_path)
    except Exception as exc:
        log.warning(
            "Не удалось удалить временный файл %s: %s",
            tmp_path, exc,
        )