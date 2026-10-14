"""Локальная транскрибация через библиотеку video2text (faster-whisper).

Используется как fallback, когда сервер транскрибации недоступен,
а пользователь согласился выполнить транскрибацию локально.

Особенности:
  • Локальная транскрибация ИДЁТ ДОЛГО (часы на больших файлах).
    Поэтому эмитим прогресс через callback, чтобы UI мог показать
    «жизнь» процесса.
  • Поддерживает отмену через asyncio.Event: callback периодически
    проверяет event и, если он выставлен, прерывает обработку.
  • Возвращает результат в формате, СОВМЕСТИМОМ с ответом сервера:
        {
            "status": "completed",
            "script": "<полный текст>",
            "text": "<полный текст>",
            "language": "ru",
            "duration": 1234.5,
            "segments": [ {...}, ... ],
            "_from_local": True,
        }
    Это позволяет вызывающему коду (processor.py) не различать
    серверный и локальный источник.

Изменения:
  • Модуль создан.
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Опции по умолчанию
# ---------------------------------------------------------------------------
DEFAULT_MODEL_SIZE: str = "medium"
DEFAULT_DEVICE: str = "cpu"
DEFAULT_COMPUTE_TYPE: str = "int8"
DEFAULT_BEAM_SIZE: int = 5
DEFAULT_VAD_FILTER: bool = True


# Минимальный интервал между вызовами progress_callback (сек).
# Слишком частые вызовы будут забивать UI-поток.
_PROGRESS_THROTTLE_SEC: float = 1.0


class LocalTranscriberError(RuntimeError):
    """Ошибка локальной транскрибации."""
    pass


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------
# Храним текст последней ошибки импорта — чтобы UI мог показать
# пользователю причину, а не только «не установлена».
_LAST_IMPORT_ERROR: str = ""


def _reset_video2text_modules() -> None:
    """
    Сбрасывает из sys.modules кэш импорта video2text и его
    подмодулей.

    Зачем это нужно: если во время ПЕРВОГО импорта video2text
    что-то упало (например, не нашлась зависимость на момент
    запуска приложения), Python запоминает это в sys.modules
    и все последующие import video2text будут падать с той же
    ошибкой, даже если потом библиотека была установлена или
    её зависимости подтянулись.

    Сброс кэша позволяет перепроверить импорт «с нуля».
    """
    import sys
    prefixes = ("video2text",)
    for key in list(sys.modules.keys()):
        if any(key == p or key.startswith(p + ".") for p in prefixes):
            try:
                del sys.modules[key]
            except Exception:
                pass


def is_video2text_available() -> bool:
    """
    Проверяет, установлена ли библиотека video2text.

    Перед импортом сбрасывает кэш sys.modules для video2text —
    это позволяет обнаружить библиотеку, установленную ПОСЛЕ
    запуска приложения, без полного перезапуска процесса.

    Не поднимает исключений.
    """
    global _LAST_IMPORT_ERROR

    _reset_video2text_modules()

    try:
        import video2text  # noqa: F401
        _LAST_IMPORT_ERROR = ""
        log.debug(
            "video2text доступен: %s",
            getattr(video2text, "__file__", "?"),
        )
        return True
    except Exception as exc:
        _LAST_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
        log.warning(
            "video2text недоступен: %s", _LAST_IMPORT_ERROR,
        )
        return False


def video2text_import_error() -> str:
    """
    Возвращает текст последней ошибки импорта video2text.

    Пустая строка, если импорт прошёл успешно или проверка
    ещё не выполнялась.
    """
    return _LAST_IMPORT_ERROR


def video2text_version() -> str:
    """
    Возвращает версию video2text или "" если недоступен.

    После сброса кэша модулей импортирует заново.
    """
    _reset_video2text_modules()
    try:
        import video2text
        return str(getattr(video2text, "__version__", "") or "")
    except Exception as exc:
        log.debug("Не удалось получить версию video2text: %s", exc)
        return ""


async def transcribe_locally(
    *,
    input_path: str,
    output_dir: str,
    options: Dict[str, Any],
    progress_cb: Optional[Callable[[str, int, int], None]] = None,
    cancel_event: Optional[asyncio.Event] = None,
) -> Dict[str, Any]:
    """
    Выполняет транскрибацию локально через video2text.

    Args:
        input_path:  путь к аудио/видео файлу.
        output_dir:  папка сессии — туда будет сохранён
                     <basename>.txt (и, при желании, .segments.json).
        options:     словарь с настройками:
                       model_size, device, compute_type,
                       language, beam_size, vad_filter.
        progress_cb: колбэк (message: str, percent: int, segments_total: int).
                     percent = -1, если ещё не знаем общее количество.
        cancel_event: если выставлен — прерываем обработку.

    Returns:
        Словарь с полями:
          status, script, text, language, duration, segments,
          _from_local=True, output_path, elapsed_sec

    Raises:
        LocalTranscriberError: при любой ошибке (нет библиотеки,
                               нет файла, ошибка faster-whisper).
    """
    t0 = time.monotonic()

    def _emit(msg: str, pct: int = -1, total: int = 0) -> None:
        if progress_cb is None:
            return
        try:
            progress_cb(msg, pct, total)
        except Exception as exc:
            log.debug("progress_cb упал: %s", exc)

    # --- 0. Проверка входных данных ---
    if not input_path or not os.path.isfile(input_path):
        raise LocalTranscriberError(
            f"Файл не найден: {input_path}"
        )

    if not output_dir or not os.path.isdir(output_dir):
        raise LocalTranscriberError(
            f"Папка сессии не найдена: {output_dir}"
        )

    # --- 1. Проверка библиотеки ---
    if not is_video2text_available():
        raise LocalTranscriberError(
            "Библиотека video2text не установлена.\n\n"
            "Установите её командой:\n"
            "  pip install video2text"
        )

    # --- 2. Проверка ffmpeg ---
    from .utils import check_ffmpeg_installed
    if not check_ffmpeg_installed():
        raise LocalTranscriberError(
            "ffmpeg не найден в PATH. Локальная транскрибация "
            "требует ffmpeg для извлечения аудио из видео."
        )

    # --- 3. Параметры ---
    model_size = str(
        options.get("model_size") or DEFAULT_MODEL_SIZE
    ).strip()
    device = str(
        options.get("device") or DEFAULT_DEVICE
    ).strip()
    compute_type = str(
        options.get("compute_type") or DEFAULT_COMPUTE_TYPE
    ).strip()
    language = options.get("language")
    if language is not None:
        language = str(language).strip() or None
    beam_size = int(options.get("beam_size") or DEFAULT_BEAM_SIZE)
    vad_filter = bool(
        options.get("vad_filter", DEFAULT_VAD_FILTER)
    )

    log.info(
        "LocalTranscriber: старт — file=%s, model=%s, device=%s, "
        "compute=%s, lang=%s, beam=%d, vad=%s",
        os.path.basename(input_path),
        model_size, device, compute_type,
        language or "auto", beam_size, vad_filter,
    )

    _emit(
        f"Локальная транскрибация: подготовка "
        f"(модель {model_size}, устройство {device})…",
        0,
    )

    # --- 4. Импорт video2text (ленивый, чтобы не тормозить старт) ---
    try:
        import video2text
    except Exception as exc:
        raise LocalTranscriberError(
            f"Не удалось импортировать video2text: {exc}"
        )

    # --- 5. Прогресс-callback для video2text ---
    last_emit_time = [0.0]
    last_pct = [-1]

    def _progress(processed: int, total: int) -> None:
        """
        Колбэк прогресса от video2text.

        video2text вызывает его по мере обработки сегментов.
        Троттлим — не чаще одного раза в _PROGRESS_THROTTLE_SEC.
        """
        # --- Проверка отмены ---
        if cancel_event is not None and cancel_event.is_set():
            # video2text не поддерживает отмену извне; поднимаем
            # исключение, чтобы разорвать его цикл.
            raise _CancelledFromCallback()

        now = time.monotonic()
        if now - last_emit_time[0] < _PROGRESS_THROTTLE_SEC:
            return
        last_emit_time[0] = now

        pct = -1
        if total > 0:
            pct = int(processed * 100 / total)

        if pct == last_pct[0]:
            # Не эмитим ту же самую цифру.
            return
        last_pct[0] = pct

        if pct >= 0:
            msg = (
                f"Локальная транскрибация: сегмент "
                f"{processed}/{total} ({pct}%)…"
            )
        else:
            msg = (
                f"Локальная транскрибация: обработано "
                f"{processed} сегментов…"
            )
        _emit(msg, pct, total)

    # --- 6. Запуск транскрибации в отдельном потоке ---
    # video2text.transcribe_file() — синхронная функция.
    # Вызываем её через asyncio.to_thread, чтобы не блокировать
    # event loop (иначе воркер перестанет обрабатывать новые
    # задачи, отмену и heartbeat).
    #
    # ВАЖНО: progress_cb будет вызываться из ЧУЖОГО потока.
    # Внутри _progress мы только эмитим через внешний callback —
    # это уже ответственность вызывающего кода сделать
    # потокобезопасный emit (см. main.py).

    # Имя выходного файла: <input_stem>.txt в output_dir.
    input_stem = Path(input_path).stem
    output_path = os.path.join(output_dir, f"{input_stem}.txt")

    def _run_sync() -> Any:
        try:
            return video2text.transcribe_file(
                input_path=input_path,
                output_path=output_path,
                model_size=model_size,
                device=device,
                compute_type=compute_type,
                language=language,
                beam_size=beam_size,
                vad_filter=vad_filter,
                progress_callback=_progress,
            )
        except TypeError:
            # Fallback: старые версии video2text могут не принимать
            # какие-то параметры. Пробуем минимальный набор.
            log.warning(
                "video2text.transcribe_file не принял полный "
                "набор параметров — использую минимальный."
            )
            return video2text.transcribe_file(
                input_path=input_path,
                output_path=output_path,
                model_size=model_size,
                device=device,
                compute_type=compute_type,
                language=language,
            )

    try:
        result = await asyncio.to_thread(_run_sync)
    except _CancelledFromCallback:
        elapsed = time.monotonic() - t0
        log.info(
            "LocalTranscriber: отменено пользователем "
            "(прошло %.1f с)", elapsed,
        )
        return {
            "status": "cancelled",
            "error": "cancelled by user",
            "script": "",
            "text": "",
            "_from_local": True,
            "elapsed_sec": elapsed,
        }
    except Exception as exc:
        elapsed = time.monotonic() - t0
        log.exception(
            "LocalTranscriber: ошибка через %.1f с: %s",
            elapsed, exc,
        )
        raise LocalTranscriberError(
            f"Локальная транскрибация завершилась ошибкой: {exc}"
        )

    elapsed = time.monotonic() - t0
    log.info(
        "LocalTranscriber: завершено за %.1f с "
        "(модель=%s, устройство=%s)",
        elapsed, model_size, device,
    )

    # --- 7. Нормализация результата ---
    return _normalize_result(
        result=result,
        output_path=output_path,
        input_path=input_path,
        language=language,
        elapsed_sec=elapsed,
    )


# ---------------------------------------------------------------------------
# Внутреннее исключение для прерывания video2text из callback
# ---------------------------------------------------------------------------
class _CancelledFromCallback(Exception):
    """Поднимается внутри progress_callback при отмене."""
    pass


# ---------------------------------------------------------------------------
# Нормализация ответа video2text
# ---------------------------------------------------------------------------
def _normalize_result(
    *,
    result: Any,
    output_path: str,
    input_path: str,
    language: Optional[str],
    elapsed_sec: float,
) -> Dict[str, Any]:
    """
    Приводит результат video2text к формату, ожидаемому processor.py.

    Формат сервера:
        {
            "status": "completed",
            "script": "<полный текст>",
            "text": "<полный текст>",
            ...
        }

    Формат video2text (TranscriptionResult):
        result.segments  — список сегментов
        result.language  — определённый язык
        result.duration  — длительность
        result.get_full_text() — полный текст
    """
    script = ""
    segments_out: List[Dict[str, Any]] = []
    detected_language = language or ""
    duration = 0.0

    try:
        # Пытаемся использовать публичный API.
        if hasattr(result, "get_full_text"):
            script = str(result.get_full_text() or "")
        elif hasattr(result, "text"):
            script = str(result.text or "")
        elif isinstance(result, dict):
            script = str(
                result.get("text") or result.get("script") or ""
            )
        elif isinstance(result, str):
            script = result

        # Сегменты.
        raw_segments = getattr(result, "segments", None)
        if raw_segments is None and isinstance(result, dict):
            raw_segments = result.get("segments")
        if raw_segments:
            for seg in raw_segments:
                segments_out.append(_normalize_segment(seg))

        # Язык.
        if hasattr(result, "language"):
            detected_language = str(
                getattr(result, "language") or detected_language
            )
        elif isinstance(result, dict) and result.get("language"):
            detected_language = str(result["language"])

        # Длительность.
        if hasattr(result, "duration"):
            try:
                duration = float(getattr(result, "duration") or 0.0)
            except (TypeError, ValueError):
                duration = 0.0
        elif isinstance(result, dict):
            try:
                duration = float(result.get("duration") or 0.0)
            except (TypeError, ValueError):
                duration = 0.0
    except Exception as exc:
        log.warning(
            "LocalTranscriber: не удалось нормализовать результат: %s",
            exc,
        )

    # Если script пустой, но есть output_path — попробуем прочитать файл.
    if not script.strip() and output_path and os.path.isfile(output_path):
        try:
            with open(output_path, "r", encoding="utf-8") as f:
                script = f.read()
            log.debug(
                "LocalTranscriber: текст подхвачен из %s (%d символов)",
                output_path, len(script),
            )
        except Exception as exc:
            log.warning(
                "LocalTranscriber: не удалось прочитать %s: %s",
                output_path, exc,
            )

    if not script.strip():
        log.warning(
            "LocalTranscriber: результат пустой "
            "(input=%s, output=%s)",
            input_path, output_path,
        )

    return {
        "status": "completed",
        "script": script,
        "text": script,
        "language": detected_language,
        "duration": duration,
        "segments": segments_out,
        "_from_local": True,
        "output_path": output_path,
        "elapsed_sec": elapsed_sec,
    }


def _normalize_segment(seg: Any) -> Dict[str, Any]:
    """Приводит один сегмент video2text к словарю."""
    if isinstance(seg, dict):
        return {
            "start": float(seg.get("start") or 0.0),
            "end": float(seg.get("end") or 0.0),
            "text": str(seg.get("text") or ""),
        }
    return {
        "start": float(getattr(seg, "start", 0.0) or 0.0),
        "end": float(getattr(seg, "end", 0.0) or 0.0),
        "text": str(getattr(seg, "text", "") or ""),
    }