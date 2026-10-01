"""Конвейер обработки: конвертация → транскрибация → суммаризация → DeepSeek-промпт.

Изменения:
  • Все синхронные файловые операции вынесены в asyncio.to_thread().
  • Чтение текста через file_readers.read_any_text.
  • Таймауты и интервалы читаются из config["app"].
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import QObject, Signal

from .file_readers import read_any_text
from .litellm_client import LiteLLMClient, LiteLLMError
from .logger import get_logger
from .markdown_to_bitrix import markdown_to_plain_with_bb
from .task_queue import TaskQueue
from .transcribe_client import TranscribeClient
from .platform_utils import ffmpeg_binary_name

log = get_logger(__name__)


class VideoProcessor(QObject):
    """Асинхронная обработка видео. Файлы остаются локально."""

    task_started = Signal(str)
    task_progress = Signal(str, int, str)
    task_completed = Signal(str, dict)
    task_failed = Signal(str, str)

    def __init__(
        self,
        config: Dict,
        task_queue: TaskQueue,
        is_recording_cb: Optional[Callable[[], bool]] = None,
        config_manager=None,   # ← добавить
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.config = config or {}
        self.task_queue = task_queue
        self._is_recording_cb = is_recording_cb
        self._config_manager = config_manager   # ← добавить
        self._cancel_events: Dict[str, asyncio.Event] = {}
        self._running = False
        self._last_retry_check = 0.0

        app_cfg = (self.config.get("app", {}) or {})
        self._poll_interval = float(app_cfg.get("processor_poll_interval", 2.0))
        self._retry_check_interval = float(
            app_cfg.get("processor_retry_check_interval", 300)
        )
        self._max_file_read_chars = int(
            app_cfg.get("max_file_read_chars", 5_000_000)
        )
        self._ffmpeg_start_check_delay = float(
            app_cfg.get("ffmpeg_start_check_delay", 0.3)
        )
        self._ffmpeg_stop_timeout = float(
            app_cfg.get("ffmpeg_stop_timeout", 10)
        )

        log.info("VideoProcessor инициализирован "
                 "(is_recording_cb=%s, poll=%.1fs, retry=%.0fs)",
                 is_recording_cb is not None,
                 self._poll_interval, self._retry_check_interval)

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------
    async def process_video(self, video_path: str,
                            metadata: Dict[str, Any]) -> Dict[str, Any]:
        task_id = metadata.get("task_id", "")
        if not task_id:
            task_id = self.task_queue.add_task({
                "video_path": video_path, "metadata": metadata,
            })
            metadata["task_id"] = task_id

        cancel_event = asyncio.Event()
        self._cancel_events[task_id] = cancel_event

        log.info("=" * 60)
        log.info("Начало обработки задачи %s", task_id)
        log.info("Видео: %s", video_path)
        log.info(
            "Метаданные: project=%s, name=%s, generate_summary=%s, "
            "is_scrum=%s, generate_deepseek=%s, "
            "ctx_name=%s, ctx_project=%s, ctx_comment=%s, "
            "attachments=%d, summary_bb=%d, manual_protocol=%s",
            metadata.get("project"), metadata.get("name"),
            metadata.get("generate_summary"),
            metadata.get("is_scrum"),
            metadata.get("generate_deepseek_prompt"),
            metadata.get("include_name_in_prompt"),
            metadata.get("include_project_in_prompt"),
            metadata.get("include_comment_in_prompt"),
            len(metadata.get("attachments", []) or []),
            len(metadata.get("summary_bb") or ""),
            os.path.basename(metadata.get("manual_protocol_path") or "")
            or "—",
        )

        t0 = time.monotonic()
        try:
            self.task_started.emit(task_id)

            # --- Шаг 1: конвертация в аудио ---
            log.info("[%s] Шаг 1/2: конвертация в аудио", task_id)
            self.task_queue.update_task_status(task_id, "converting", 10)
            self.task_progress.emit(task_id, 10, "converting")

            comp = self.config.get("compression", {})
            audio_fmt = comp.get("audio_format", "mp3")
            audio_bitrate = comp.get("audio_bitrate", 192)

            t1 = time.monotonic()
            audio_path = await self.convert_to_audio(
                video_path, audio_fmt, audio_bitrate,
                cancel_event=cancel_event,
            )
            log.info("[%s] Конвертация завершена за %.1f с: %s",
                     task_id, time.monotonic() - t1, audio_path)

            self._raise_if_cancelled(task_id, cancel_event)

            # ============================================================
            # Шаг 2: транскрибация + суммаризация
            # ============================================================
            transcribe_cfg = self.config.get("transcribe", {})
            transcribe_url = (transcribe_cfg.get("url") or "").strip()
            transcribe_available = bool(transcribe_url)
            vm_session = None

            # --- Проверка доступности транскрибации и ВМ ---
            if transcribe_url and self._config_manager is not None:
                from .transcribe_vm_controller import (
                    TranscribeVMController,
                )

                controller = TranscribeVMController(
                    self._config_manager
                )

                self.task_progress.emit(
                    task_id, 45,
                    "Проверка сервиса транскрибации…"
                )

                try:
                    prep = await controller.prepare_for_transcribe(
                        progress_cb=lambda msg: self.task_progress.emit(
                            task_id, 45, msg
                        ),
                        cancel_event=cancel_event,
                    )
                except Exception as exc:
                    log.exception(
                        "[%s] Ошибка проверки ВМ: %s", task_id, exc
                    )
                    prep = None

                if prep is not None:
                    if prep.can_transcribe:
                        transcribe_available = True
                        vm_session = prep.vm_session
                        log.info(
                            "[%s] Транскрибация доступна "
                            "(ВМ: %s)",
                            task_id,
                            "использована"
                            if vm_session else "не требовалась",
                        )
                    else:
                        transcribe_available = False
                        log.warning(
                            "[%s] Транскрибация недоступна: %s",
                            task_id, prep.skipped_reason,
                        )
                        self.task_progress.emit(
                            task_id, 45,
                            f"Транскрибация пропущена: "
                            f"{prep.skipped_reason[:60]}"
                        )

            sum_cfg = self.config.get("summarizer", {}) or {}
            provider = str(sum_cfg.get("provider", "server")).strip().lower()
            if provider not in ("server", "litellm"):
                provider = "server"

            generate_summary = bool(metadata.get("generate_summary", False))

            gl = self.config.get("glossary", {}) or {}
            gl_terms = gl.get("terms", []) or []
            gl_text = self._build_glossary_text(gl_terms)
            gl_to_summarizer = bool(gl.get("send_to_summarizer", True))
            gl_to_deepseek = bool(gl.get("send_to_deepseek", True))

            transcript_path = ""
            summary_path = ""

            if transcribe_available:
                log.info(
                    "[%s] Шаг 2/2: транскрибация "
                    "(провайдер суммаризации: %s, summary: %s)",
                    task_id, provider,
                    "on" if generate_summary else "off",
                )
                self.task_queue.update_task_status(task_id, "transcribing", 50)
                self.task_progress.emit(task_id, 50, "transcribing")

                prompt = (
                    metadata.get("prompt")
                    or self.config.get("metadata", {}).get(
                        "default_prompt", ""
                    )
                )
                log.debug("[%s] Промпт пользователя (%d символов)",
                          task_id, len(prompt))

                ctx_header = self._build_context_header(
                    metadata=metadata,
                    include_name=bool(
                        metadata.get("include_name_in_prompt")
                    ),
                    include_project=bool(
                        metadata.get("include_project_in_prompt")
                    ),
                    include_comment=bool(
                        metadata.get("include_comment_in_prompt")
                    ),
                )
                if ctx_header:
                    prompt = ctx_header + "\n\n" + prompt
                    log.info("[%s] В промпт добавлен контекст записи "
                             "(%d символов)", task_id, len(ctx_header))

                summary_bb = str(metadata.get("summary_bb") or "").strip()
                if summary_bb:
                    summary_plain = markdown_to_plain_with_bb(summary_bb)
                    if summary_plain:
                        prompt = (
                            prompt
                            + "\n\n===== КРАТКОЕ ОПИСАНИЕ ЗАПИСИ =====\n\n"
                            + summary_plain
                        )
                        log.info(
                            "[%s] К промпту добавлено краткое описание "
                            "(%d символов источника → %d plain)",
                            task_id, len(summary_bb), len(summary_plain),
                        )

                if metadata.get("send_attachments_to_transcribe"):
                    att_text = await asyncio.to_thread(
                        self._read_attachments_text,
                        metadata.get("attachments", []) or [],
                    )
                    if att_text:
                        prompt = (
                            prompt
                            + "\n\n===== ДОПОЛНИТЕЛЬНЫЙ КОНТЕКСТ "
                              "(вложения) =====\n\n"
                            + att_text
                        )
                        log.info("[%s] К промпту добавлены вложения "
                                 "(%d символов)", task_id, len(att_text))

                if gl_to_summarizer and gl_text:
                    prompt = (
                        prompt
                        + "\n\n===== ГЛОССАРИЙ (используй правильные "
                          "формулировки) =====\n\n"
                        + gl_text
                    )
                    log.info("[%s] К промпту добавлен глоссарий "
                             "(%d терминов, %d символов)",
                             task_id, len(gl_terms), len(gl_text))

                target_for_transcribe = audio_path if audio_path else video_path
                log.info("[%s] Для транскрибации используется: %s",
                         task_id, target_for_transcribe)

                if provider == "server" and generate_summary:
                    server_prompt = prompt
                else:
                    server_prompt = ""
                    if provider == "server" and not generate_summary:
                        log.info(
                            "[%s] Summary отключено — промпт на сервер "
                            "транскрибации не передаём",
                            task_id,
                        )

                t2 = time.monotonic()
                transcript = await self.transcribe(
                    target_for_transcribe, transcribe_cfg,
                    prompt=server_prompt,
                    cancel_event=cancel_event,
                )
                log.info("[%s] Транскрибация завершена за %.1f с",
                         task_id, time.monotonic() - t2)

                if isinstance(transcript, dict) and \
                        transcript.get("status") == "cancelled":
                    log.info("[%s] Транскрибация отменена", task_id)
                    self.task_queue.mark_failed(task_id,
                                                "cancelled by user")
                    self.task_failed.emit(task_id, "cancelled by user")
                    return {"error": "cancelled", "task_id": task_id}

                if isinstance(transcript, dict) and transcript.get("error"):
                    err = str(transcript["error"])
                    log.error("[%s] Ошибка транскрибации: %s", task_id, err)
                    self.task_queue.mark_failed(task_id,
                                                f"transcribe: {err}")
                    self.task_failed.emit(task_id, f"transcribe: {err}")
                    return {"error": err, "task_id": task_id}

                if isinstance(transcript, dict) and \
                        transcript.get("status") == "error":
                    err = str(transcript.get("error")
                              or "неизвестная ошибка на сервере")
                    log.error("[%s] Сервер вернул status=error: %s",
                              task_id, err)
                    self.task_queue.mark_failed(task_id,
                                                f"transcribe: {err}")
                    self.task_failed.emit(task_id, f"transcribe: {err}")
                    return {"error": err, "task_id": task_id}

                self._raise_if_cancelled(task_id, cancel_event)

                transcript_path = str(Path(video_path).with_suffix(".txt"))
                summary_path = str(
                    Path(video_path).with_name("video_summary.md")
                )
                await asyncio.to_thread(
                    self._save_transcript, transcript, transcript_path
                )

                if not generate_summary:
                    log.info(
                        "[%s] Формирование summary отключено для этой "
                        "записи — пропускаем шаг суммаризации "
                        "(провайдер=%s)",
                        task_id, provider,
                    )
                elif provider == "litellm":
                    self.task_queue.update_task_status(
                        task_id, "summarizing", 70
                    )
                    self.task_progress.emit(task_id, 70, "summarizing")
                    try:
                        summary_text = await self._summarize_with_litellm(
                            task_id=task_id,
                            script=str(transcript.get("script") or ""),
                            user_prompt=prompt,
                            video_title=metadata.get("name", "") or "",
                        )
                        if summary_text:
                            await asyncio.to_thread(
                                self._write_file, summary_path, summary_text
                            )
                            transcript["summary"] = summary_text
                        else:
                            log.warning(
                                "[%s] LiteLLM вернул пустой ответ — "
                                "файл summary не создан", task_id,
                            )
                    except LiteLLMError as exc:
                        err = f"litellm: {exc}"
                        log.error("[%s] Ошибка LiteLLM: %s", task_id, err)
                        self.task_queue.mark_failed(task_id, err)
                        self.task_failed.emit(task_id, err)
                        return {"error": err, "task_id": task_id}
                else:
                    await asyncio.to_thread(
                        self._save_summary, transcript, summary_path
                    )
            else:
                if not transcribe_url:
                    log.info(
                        "[%s] Транскрибация пропущена: URL сервера "
                        "не задан", task_id,
                    )
                else:
                    log.info(
                        "[%s] Транскрибация пропущена: сервис "
                        "недоступен и ВМ не настроена "
                        "(или не запустилась)",
                        task_id,
                    )

            self._raise_if_cancelled(task_id, cancel_event)

            # --- Шаг 3: DeepSeek-промпт ---
            if metadata.get("generate_deepseek_prompt") and transcript_path:
                use_scrum = bool(metadata.get("is_scrum", False))
                manual_protocol_path = str(
                    metadata.get("manual_protocol_path") or ""
                ).strip()
                manual_protocol_text = ""
                if manual_protocol_path and os.path.exists(
                    manual_protocol_path
                ):
                    manual_protocol_text = await asyncio.to_thread(
                        read_any_text, manual_protocol_path,
                        self._max_file_read_chars,
                    )
                    log.info(
                        "[%s] Ручной протокол прочитан: %s (%d символов)",
                        task_id, manual_protocol_path,
                        len(manual_protocol_text),
                    )

                summary_bb = str(metadata.get("summary_bb") or "").strip()
                summary_for_deepseek = (
                    markdown_to_plain_with_bb(summary_bb)
                    if summary_bb else ""
                )

                try:
                    prompt_path = await asyncio.to_thread(
                        self._build_deepseek_prompt,
                        os.path.dirname(video_path),
                        transcript_path,
                        metadata.get("previous_protocol_path", ""),
                        metadata.get("attachments", []) or [],
                        bool(metadata.get("send_attachments_to_deepseek",
                                          False)),
                        use_scrum,
                        str(metadata.get("prompt") or ""),
                        gl_text if gl_to_deepseek else "",
                        metadata,
                        summary_for_deepseek,
                        manual_protocol_text,
                    )
                    if prompt_path:
                        log.info(
                            "[%s] Промпт DeepSeek: %s (template=%s, "
                            "glossary=%s, summary=%s, manual_protocol=%s)",
                            task_id, prompt_path,
                            "scrum" if use_scrum else "user",
                            bool(gl_to_deepseek and gl_text),
                            bool(summary_for_deepseek),
                            bool(manual_protocol_text),
                        )
                except Exception as exc:
                    log.exception(
                        "[%s] Не удалось собрать DeepSeek-промпт: %s",
                        task_id, exc,
                    )
            else:
                log.info(
                    "[%s] DeepSeek-промпт не формируется: "
                    "generate_deepseek_prompt=%s, transcript_path=%r",
                    task_id,
                    metadata.get("generate_deepseek_prompt"),
                    transcript_path,
                )

            # --- Завершение ---
            self.task_queue.update_task_status(task_id, "completed", 100)
            self.task_progress.emit(task_id, 100, "completed")

            result = {
                "video": video_path,
                "audio": audio_path,
                "transcript": transcript_path,
                "summary": summary_path if transcript_path else "",
                "output_dir": os.path.dirname(video_path),
                # --- Информация о VM-сессии для UI ---
                "vm_session": (
                    {
                        "schedule_modified": (
                            vm_session.schedule_modified
                            if vm_session else False
                        ),
                        "schedule_path": (
                            vm_session.schedule_path
                            if vm_session else ""
                        ),
                        "vm_name": (
                            vm_session.vm_manager.vm_name
                            if vm_session and vm_session.vm_manager
                            else ""
                        ),
                    }
                    if vm_session else None
                ),
            }
            total = time.monotonic() - t0
            log.info("[%s] Задача завершена за %.1f с. Файлы: %s",
                     task_id, total, result["output_dir"])
            log.info("=" * 60)
            self.task_completed.emit(task_id, result)
            return result

        except _CancelledError:
            total = time.monotonic() - t0
            log.info("[%s] Задача отменена пользователем (%.1f с)",
                     task_id, total)
            self.task_queue.mark_failed(task_id, "cancelled by user")
            self.task_failed.emit(task_id, "cancelled by user")
            return {"error": "cancelled", "task_id": task_id}

        except Exception as exc:
            total = time.monotonic() - t0
            log.exception("[%s] Ошибка обработки (%.1f с): %s",
                          task_id, total, exc)
            self.task_queue.mark_failed(task_id, str(exc))
            self.task_failed.emit(task_id, str(exc))
            return {"error": str(exc), "task_id": task_id}

        finally:
            self._cancel_events.pop(task_id, None)

    # ------------------------------------------------------------------
    # Шаги
    # ------------------------------------------------------------------
    async def convert_to_audio(
        self,
        video_path: str,
        fmt: str,
        bitrate: int,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> str:
        out = str(Path(video_path).with_suffix(f".{fmt}"))
        codec = "libmp3lame" if fmt == "mp3" else "aac"
        cmd = [
            ffmpeg_binary_name(), "-y", "-i", video_path, "-vn",
            "-acodec", codec, "-b:a", f"{bitrate}k", out,
        ]
        log.debug("ffmpeg конвертация: %s", " ".join(cmd))

        if cancel_event is not None and cancel_event.is_set():
            log.info("Конвертация отменена до старта ffmpeg")
            return ""

        proc: Optional[asyncio.subprocess.Process] = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )

            if cancel_event is not None:
                wait_task = asyncio.ensure_future(proc.wait())
                cancel_task = asyncio.ensure_future(cancel_event.wait())
                try:
                    done, pending = await asyncio.wait(
                        {wait_task, cancel_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if cancel_task in done:
                        log.info("Конвертация отменена — завершаем ffmpeg "
                                 "(PID=%s)", proc.pid)
                        try:
                            proc.terminate()
                        except ProcessLookupError:
                            pass
                        try:
                            await asyncio.wait_for(
                                proc.wait(),
                                timeout=self._ffmpeg_stop_timeout,
                            )
                        except asyncio.TimeoutError:
                            try:
                                proc.kill()
                            except ProcessLookupError:
                                pass
                            await proc.wait()
                        wait_task.cancel()
                        return ""
                    rc = wait_task.result()
                finally:
                    cancel_task.cancel()
                    if not wait_task.done():
                        wait_task.cancel()
            else:
                rc = await proc.wait()

            if rc != 0:
                log.error("ffmpeg вернул код %d", rc)

            if os.path.exists(out):
                log.debug("Создан аудиофайл: %s (%.2f МБ)",
                          out, os.path.getsize(out) / 1024 / 1024)
                return out

            log.warning("Аудиофайл не создан: %s", out)
            return ""
        except Exception as exc:
            log.exception("Ошибка конвертации: %s", exc)
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass
            return ""

    async def transcribe(
        self,
        video_path: str,
        config: Dict[str, Any],
        prompt: str = "",
        cancel_event: Optional[asyncio.Event] = None,
    ) -> Dict[str, Any]:
        base_url = config.get("url", "")
        access_key = config.get("access_key", "")
        connect_timeout = float(config.get("connect_timeout", 15))
        read_timeout = float(config.get("read_timeout", 120))
        max_wait = float(config.get("max_wait", 7200))

        log.info("Транскрибация через %s (prompt=%d символов, "
                 "connect=%.0f, read=%.0f, max_wait=%.0f)",
                 base_url, len(prompt),
                 connect_timeout, read_timeout, max_wait)

        try:
            async with TranscribeClient(
                base_url=base_url,
                session_token=None,
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
            ) as client:
                log.debug("Авторизация на сервере транскрибации")
                await client.login(access_key)
                log.debug("Отправка файла на транскрибацию: %s", video_path)
                result = await client.process_video(
                    file_path=Path(video_path),
                    summary_language="ru",
                    transcription_language="auto",
                    wait=True,
                    poll_interval=2.0,
                    summary_prompt=prompt,
                    max_wait=max_wait,
                    cancel_event=cancel_event,
                )
                log.info("Транскрибация: получен результат (status=%s)",
                         result.get("status")
                         if isinstance(result, dict) else "—")
                return result
        except Exception as exc:
            log.exception("Ошибка транскрибации: %s", exc)
            return {"error": str(exc)}

    # ------------------------------------------------------------------
    # Суммаризация через LiteLLM
    # ------------------------------------------------------------------
    async def _summarize_with_litellm(
        self,
        task_id: str,
        script: str,
        user_prompt: str,
        video_title: str = "",
    ) -> str:
        cfg = self.config.get("summarizer", {}) or {}
        l = cfg.get("litellm", {}) or {}

        base_url = str(l.get("base_url", "")).strip()
        if not base_url:
            raise LiteLLMError("Не задан base_url для LiteLLM")

        api_key = str(l.get("api_key", ""))
        model = str(l.get("model", "gpt-4o-mini"))
        temperature = float(l.get("temperature", 0.25))
        max_tokens = int(l.get("max_tokens", 2200))
        connect_timeout = float(l.get("connect_timeout", 15))
        read_timeout = float(l.get("read_timeout", 300))
        system_prompt = str(l.get("system_prompt", "")).strip()

        if not script.strip():
            raise LiteLLMError("Пустой script — нечего суммаризировать")

        if not user_prompt or not user_prompt.strip():
            user_prompt = (
                self.config.get("metadata", {}).get("default_prompt")
                or "Составь краткое содержание записи."
            )

        parts: List[str] = []
        if video_title:
            parts.append(f"# {video_title}")
            parts.append("")
        parts.append("ИНСТРУКЦИЯ:")
        parts.append(user_prompt.strip())
        parts.append("")
        parts.append("=" * 60)
        parts.append("СТЕНОГРАММА")
        parts.append("=" * 60)
        parts.append("")
        parts.append(script)
        user_message = "\n".join(parts)

        log.info("[%s] LiteLLM: model=%s, sys=%d симв, user=%d симв",
                 task_id, model, len(system_prompt), len(user_message))

        async with LiteLLMClient(
            base_url=base_url,
            api_key=api_key,
            model=model,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
        ) as client:
            return await client.chat_completion(
                system_prompt=system_prompt,
                user_prompt=user_message,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
            )

    # ------------------------------------------------------------------
    # Контекст записи
    # ------------------------------------------------------------------
    @staticmethod
    def _build_context_header(
        metadata: Dict[str, Any],
        include_name: bool,
        include_project: bool,
        include_comment: bool,
    ) -> str:
        lines: List[str] = []

        if include_name:
            name = str(metadata.get("name") or "").strip()
            if name:
                lines.append(f"Название записи: {name}")

        if include_project:
            project = str(metadata.get("project") or "").strip()
            if project:
                lines.append(f"Проект: {project}")

        if include_comment:
            comment = str(metadata.get("comment") or "").strip()
            if comment:
                lines.append(f"Комментарий: {comment}")

        if not lines:
            return ""

        return (
            "===== КОНТЕКСТ ЗАПИСИ =====\n\n"
            + "\n".join(lines)
            + "\n\n===== ИНСТРУКЦИЯ ====="
        )

    # ------------------------------------------------------------------
    # Вложения
    # ------------------------------------------------------------------
    def _read_attachments_text(self, paths: List[str]) -> str:
        if not paths:
            return ""

        blocks: List[str] = []
        for p in paths:
            if not p or not os.path.exists(p):
                log.warning("Вложение не найдено: %s", p)
                continue
            text = read_any_text(p, self._max_file_read_chars)
            if not text:
                continue
            blocks.append(f"### Файл: {os.path.basename(p)}\n{text}")
            log.debug("Прочитано вложение: %s (%d символов)",
                      p, len(text))

        return "\n\n".join(blocks)

    # ------------------------------------------------------------------
    # Глоссарий
    # ------------------------------------------------------------------
    @staticmethod
    def _build_glossary_text(terms: List[Dict[str, Any]]) -> str:
        if not terms:
            return ""
        lines: List[str] = []
        for item in terms:
            if isinstance(item, dict):
                t = str(item.get("term") or "").strip()
                d = str(item.get("description") or "").strip()
            else:
                t = str(item or "").strip()
                d = ""
            if not t:
                continue
            if d:
                lines.append(f"- {t} — {d}")
            else:
                lines.append(f"- {t}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # DeepSeek-промпт
    # ------------------------------------------------------------------
    def _build_deepseek_prompt(
        self,
        session_dir: str,
        transcript_path: str,
        previous_protocol_path: str = "",
        attachments: Optional[List[str]] = None,
        include_attachments: bool = False,
        use_scrum_template: bool = True,
        user_prompt: str = "",
        glossary_text: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        summary_text: str = "",
        manual_protocol_text: str = "",
    ) -> str:
        log.info(
            "Сборка DeepSeek-промпта: transcript=%s, prev=%s, attach=%s, "
            "use_scrum_template=%s, user_prompt=%d симв, glossary=%d симв, "
            "summary=%d симв, manual_protocol=%d симв",
            transcript_path, previous_protocol_path or "—",
            include_attachments, use_scrum_template, len(user_prompt or ""),
            len(glossary_text or ""), len(summary_text or ""),
            len(manual_protocol_text or ""),
        )

        ctx_header = ""
        if metadata:
            ctx_header = self._build_context_header(
                metadata=metadata,
                include_name=bool(metadata.get("include_name_in_prompt")),
                include_project=bool(
                    metadata.get("include_project_in_prompt")
                ),
                include_comment=bool(
                    metadata.get("include_comment_in_prompt")
                ),
            )
            if ctx_header:
                log.info("В DeepSeek-промпт добавлен контекст записи "
                         "(%d символов)", len(ctx_header))

        if use_scrum_template:
            scrum_cfg = self.config.get("scrum", {})
            template = scrum_cfg.get("prompt_template") or ""
            template_source = "scrum"
        else:
            template = (user_prompt or "").strip()
            if not template:
                template = (
                    self.config.get("metadata", {})
                    .get("default_prompt", "")
                    or ""
                )
                template_source = "default"
            else:
                template_source = "user"
        log.info("Шаблон DeepSeek-промпта: источник=%s, %d символов",
                 template_source, len(template))

        export_format = (
            self.config.get("scrum", {}).get("export_format") or "docx"
        ).lower()
        if export_format not in ("docx", "md", "txt"):
            export_format = "docx"

        # --- Стенограмма (всегда plain text) ---
        transcript_text = read_any_text(
            transcript_path, self._max_file_read_chars
        )

        # --- Предыдущий протокол: сохраняем формат исходного файла ---
        # Путь сохранён, но сам текст для .docx читать не будем —
        # вставим его как отдельный документ в финальной сборке.
        previous_path = previous_protocol_path or ""
        previous_ext = ""
        if previous_path and os.path.exists(previous_path):
            previous_ext = os.path.splitext(previous_path)[1].lower()
        else:
            previous_path = ""

        previous_plain_text = ""
        if previous_path and previous_ext in (".txt", ".json"):
            # Для .txt/.json читаем как plain — их нечего форматировать.
            previous_plain_text = read_any_text(
                previous_path, self._max_file_read_chars
            )
        elif previous_path and previous_ext in (".md", ".docx", ".pdf"):
            # Для .md/.docx/.pdf форматирование сохраним при сборке
            # финального файла. Здесь — только фиксируем путь.
            log.info(
                "Предыдущий протокол %s (формат %s) будет вставлен "
                "с сохранением форматирования",
                previous_path, previous_ext,
            )
        elif previous_path:
            # Неизвестное расширение — читаем как plain на всякий случай.
            previous_plain_text = read_any_text(
                previous_path, self._max_file_read_chars
            )

        # --- Вложения: сохраняем пути, а не только текст ---
        # Вложения тоже могут быть .docx/.md — сохраняем их пути,
        # чтобы вставить с форматированием.
        attachment_paths: List[str] = []
        if include_attachments and attachments:
            for p in attachments:
                if p and os.path.exists(p):
                    attachment_paths.append(p)
            if attachment_paths:
                log.info(
                    "В промпт DeepSeek будет добавлено вложений: %d "
                    "(с сохранением форматирования для .md/.docx)",
                    len(attachment_paths),
                )

        # --- Формируем структурированные блоки ---
        blocks: List[Dict[str, Any]] = []

        if ctx_header:
            blocks.append({
                "kind": "text",
                "title": "",
                "text": ctx_header,
            })

        if template.strip():
            blocks.append({
                "kind": "text",
                "title": "",
                "text": template,
            })

        if manual_protocol_text:
            blocks.append({
                "kind": "text",
                "title": "РУЧНОЙ ПРОТОКОЛ (загружен пользователем)",
                "text": manual_protocol_text,
            })

        if summary_text:
            blocks.append({
                "kind": "text",
                "title": "КРАТКОЕ ОПИСАНИЕ ЗАПИСИ (введено пользователем)",
                "text": summary_text,
            })

        # --- Предыдущий протокол ---
        if previous_path:
            blocks.append({
                "kind": "file",
                "title": "ПРЕДЫДУЩИЙ ПРОТОКОЛ",
                "path": previous_path,
                "ext": previous_ext,
                "fallback_text": previous_plain_text,
            })
        elif use_scrum_template:
            blocks.append({
                "kind": "text",
                "title": "ПРЕДЫДУЩИЙ ПРОТОКОЛ",
                "text": "(Предыдущий протокол не приложен.)",
            })

        # --- Глоссарий ---
        if glossary_text:
            blocks.append({
                "kind": "text",
                "title": "ГЛОССАРИЙ (используй правильные формулировки)",
                "text": glossary_text,
            })

        # --- Вложения ---
        for p in attachment_paths:
            ext = os.path.splitext(p)[1].lower()
            blocks.append({
                "kind": "file",
                "title": f"ВЛОЖЕНИЕ: {os.path.basename(p)}",
                "path": p,
                "ext": ext,
                "fallback_text": "",
            })

        # --- Стенограмма ---
        blocks.append({
            "kind": "text",
            "title": "СТЕНОГРАММА СЕГОДНЯШНЕГО СОВЕЩАНИЯ",
            "text": transcript_text,
        })

        return self._export_prompt_file(
            session_dir=session_dir,
            blocks=blocks,
            fmt=export_format,
        )

    # ------------------------------------------------------------------
    # Рендеринг блоков промпта в DOCX / MD / TXT
    # ------------------------------------------------------------------
    def _render_blocks_to_docx(
        self,
        blocks: List[Dict[str, Any]],
        doc,
    ) -> None:
        """
        Наполняет python-docx Document блоками промпта.

        Правила:
          • text-блок: заголовок (если есть) стилем Heading 2,
            затем абзацы текста как есть.
          • file-блок:
              – .docx — вставляем как отдельный документ
                с сохранением абзацев, стилей, жирного/курсива,
                списков (насколько это возможно);
              – .md   — конвертируем через markdown_to_docx
                во временный файл и вставляем как .docx;
              – .txt/.json/прочее — читаем как plain,
                вставляем абзацами;
              – .pdf  — читаем как plain (pypdf не отдаёт
                форматирование).
        """
        from docx import Document  # type: ignore
        from docx.shared import Pt  # type: ignore
        from docx.enum.text import WD_ALIGN_PARAGRAPH  # type: ignore

        for idx, b in enumerate(blocks):
            kind = b.get("kind", "text")
            title = (b.get("title") or "").strip()

            # Разделитель между блоками (визуальный)
            if idx > 0:
                sep = doc.add_paragraph()
                run = sep.add_run("─" * 60)
                run.font.size = Pt(9)
                run.font.color.rgb = None
                sep.alignment = WD_ALIGN_PARAGRAPH.CENTER

            # --- Заголовок блока ---
            if title:
                doc.add_heading(title, level=2)

            # --- Text-блок: парсим Markdown и добавляем с форматированием ---
            if kind == "text":
                text = b.get("text") or ""
                if text.strip():
                    self._append_markdown_text_to_docx(doc, text)
                continue

            # --- File-блок ---
            if kind == "file":
                path = b.get("path") or ""
                ext = (b.get("ext") or "").lower()
                fallback = b.get("fallback_text") or ""

                if not path or not os.path.exists(path):
                    if fallback:
                        for line in fallback.splitlines():
                            doc.add_paragraph(line)
                    else:
                        doc.add_paragraph("(Файл недоступен.)")
                    continue

                try:
                    if ext == ".docx":
                        self._append_docx_content(doc, path)
                    elif ext == ".md":
                        tmp_docx = self._md_to_tmp_docx(path)
                        if tmp_docx:
                            try:
                                self._append_docx_content(
                                    doc, tmp_docx
                                )
                            finally:
                                try:
                                    os.remove(tmp_docx)
                                except Exception:
                                    pass
                        else:
                            # Fallback: как plain text
                            plain = read_any_text(
                                path, self._max_file_read_chars
                            )
                            for line in plain.splitlines():
                                doc.add_paragraph(line)
                    else:
                        # .txt, .json, .pdf, прочее — как plain
                        plain = read_any_text(
                            path, self._max_file_read_chars
                        )
                        if plain:
                            for line in plain.splitlines():
                                doc.add_paragraph(line)
                        elif fallback:
                            for line in fallback.splitlines():
                                doc.add_paragraph(line)
                        else:
                            doc.add_paragraph("(Пустой файл.)")
                except Exception as exc:
                    log.exception(
                        "Не удалось вставить файл %s как docx: %s",
                        path, exc,
                    )
                    doc.add_paragraph(
                        f"(Не удалось вставить файл: "
                        f"{os.path.basename(path)})"
                    )

    def _append_markdown_text_to_docx(self, doc, text: str) -> None:
        """
        Разбирает Markdown-текст и добавляет его в существующий
        python-docx Document с сохранением разметки:

          • заголовки # ## ### → Heading N
          • **жирный** → bold
          • *курсив* → italic
          • `код` → моноширинный
          • - / * / + → List Bullet
          • 1. 2. → List Number
          • > цитата → italic + отступ
          • ```…``` → моноширинный блок
          • --- → горизонтальная линия
          • [текст](url) → текст + (url)

        Используется для text-блоков DeepSeek-промпта, чтобы
        инструкция и стенограмма выглядели как настоящий документ,
        а не как «сырой» Markdown.
        """
        from docx.shared import Pt  # type: ignore
        from docx.enum.text import WD_ALIGN_PARAGRAPH  # type: ignore

        try:
            from .markdown_docx import _add_inline  # type: ignore
        except Exception:
            # Fallback: если по какой-то причине функция недоступна,
            # добавляем строки как обычные абзацы.
            for line in text.splitlines():
                doc.add_paragraph(line)
            return

        import re

        _HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
        _ULIST_RE = re.compile(r"^[-*+]\s+(.*)$")
        _OLIST_RE = re.compile(r"^(\d+)\.\s+(.*)$")
        _HR_RE = re.compile(r"^-{3,}$|^\*{3,}$|^_{3,}$")
        _CODE_FENCE_RE = re.compile(r"^```")

        lines = text.splitlines()
        i = 0
        n = len(lines)

        while i < n:
            raw = lines[i]
            stripped = raw.strip()

            # --- Пустая строка ---
            if not stripped:
                i += 1
                continue

            # --- Блок кода ---
            if _CODE_FENCE_RE.match(stripped):
                i += 1
                code_lines: List[str] = []
                while i < n and not _CODE_FENCE_RE.match(
                    lines[i].strip()
                ):
                    code_lines.append(lines[i])
                    i += 1
                if i < n:
                    i += 1
                p = doc.add_paragraph()
                run = p.add_run("\n".join(code_lines))
                run.font.name = "Consolas"
                run.font.size = Pt(10)
                p.paragraph_format.left_indent = Pt(12)
                continue

            # --- Горизонтальная линия ---
            if _HR_RE.match(stripped):
                p = doc.add_paragraph()
                run = p.add_run("─" * 40)
                run.font.size = Pt(9)
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                i += 1
                continue

            # --- Заголовок ---
            m = _HEADING_RE.match(stripped)
            if m:
                level = min(len(m.group(1)), 4)
                doc.add_heading(m.group(2).strip(), level=level)
                i += 1
                continue

            # --- Цитата ---
            if stripped.startswith(">"):
                quote_lines: List[str] = []
                while i < n and lines[i].strip().startswith(">"):
                    quote_lines.append(
                        lines[i].strip().lstrip(">").strip()
                    )
                    i += 1
                p = doc.add_paragraph()
                _add_inline(p, " ".join(quote_lines))
                p.paragraph_format.left_indent = Pt(24)
                for run in p.runs:
                    run.italic = True
                continue

            # --- Маркированный список ---
            m = _ULIST_RE.match(stripped)
            if m:
                while i < n:
                    m2 = _ULIST_RE.match(lines[i].strip())
                    if not m2:
                        break
                    p = doc.add_paragraph(style="List Bullet")
                    _add_inline(p, m2.group(1))
                    i += 1
                continue

            # --- Нумерованный список ---
            m = _OLIST_RE.match(stripped)
            if m:
                while i < n:
                    m2 = _OLIST_RE.match(lines[i].strip())
                    if not m2:
                        break
                    p = doc.add_paragraph(style="List Number")
                    _add_inline(p, m2.group(2))
                    i += 1
                continue

            # --- Обычный абзац ---
            p = doc.add_paragraph()
            _add_inline(p, stripped)
            i += 1

    @staticmethod
    def _append_docx_content(target_doc, src_path: str) -> None:
        """
        Вставляет содержимое .docx в целевой Document,
        сохраняя форматирование на уровне runs (bold/italic/
        underline, размер шрифта), стилей абзацев
        (Heading 1..N, List Bullet, List Number, Quote)
        и нумерации через numPr (если стиль нестандартный).

        Исправления:
        • Определяем список не только по style.name, но и по numPr
            (numId/ilvl) — так ловятся списки, у которых стиль
            'Normal' или 'List Paragraph', но маркер задан через
            numbering.
        • Учитываем локализованные имена стилей (русские названия
            заголовков и списков).
        • Дополнительно проверяем префикс 'List' — так ловятся
            'List Paragraph' и другие производные.
        """
        from docx import Document  # type: ignore
        from docx.shared import Pt  # type: ignore

        src = Document(src_path)

        # Локализованные названия стилей, которые Word может отдать
        # в русской версии.
        _BULLET_STYLE_ALIASES = {
            "list bullet", "list bullet 1", "list bullet 2",
            "list paragraph",
            "маркированный список", "маркированный список 1",
        }
        _NUMBER_STYLE_ALIASES = {
            "list number", "list number 1", "list number 2",
            "нумерованный список", "нумерованный список 1",
        }
        _QUOTE_STYLE_ALIASES = {
            "quote", "intense quote", "цитата", "выделенная цитата",
        }

        def _has_numbering(para) -> bool:
            """Есть ли у абзаца свойства нумерации (numPr)."""
            try:
                ppr = para._p.pPr
                if ppr is None:
                    return False
                numpr = ppr.numPr
                return numpr is not None
            except Exception:
                return False

        def _numbering_level(para) -> int:
            """ilvl (0-based). 0 — верхний уровень."""
            try:
                ilvl = para._p.pPr.numPr.ilvl
                return int(ilvl.val) if ilvl is not None else 0
            except Exception:
                return 0

        for para in src.paragraphs:
            text = para.text or ""
            style_name = (para.style.name if para.style else "") or ""
            style_lower = style_name.strip().lower()

            # --- Определяем целевой стиль ---
            target_style = None
            num_level = 0

            # Заголовки: "Heading 1" ... "Heading 6", "Заголовок 1" ...
            if style_name.startswith("Heading "):
                try:
                    level = int(style_name.split(" ")[1])
                    level = max(1, min(level, 6))
                    target_style = f"Heading {level}"
                except (IndexError, ValueError):
                    target_style = None
            elif style_lower.startswith("заголовок "):
                try:
                    level = int(style_lower.split(" ")[1])
                    level = max(1, min(level, 6))
                    target_style = f"Heading {level}"
                except (IndexError, ValueError):
                    target_style = None

            # Списки — по имени стиля
            if target_style is None:
                if style_lower in _BULLET_STYLE_ALIASES:
                    target_style = "List Bullet"
                elif style_lower in _NUMBER_STYLE_ALIASES:
                    target_style = "List Number"
                elif style_lower in _QUOTE_STYLE_ALIASES:
                    target_style = "Quote"

            # Списки — по numPr (если стиль не сработал)
            if target_style is None and _has_numbering(para):
                num_level = _numbering_level(para)
                # Как отличить маркированный от нумерованного:
                #   смотрим abstractNumId в numbering part.
                #   Это сложно без доступа к numbering.xml,
                #   поэтому используем эвристику: если стиль
                #   абзаца содержит 'bullet' или текст начинается
                #   с типичного маркера — bullet; иначе number.
                # Более надёжно: попробуем найти abstractNum по numId.
                is_bullet = False
                try:
                    numbering_part = src.part.numbering_part
                    numbering_xml = numbering_part.element
                    # numId абзаца
                    num_id_el = para._p.pPr.numPr.numId
                    num_id = (
                        int(num_id_el.val)
                        if num_id_el is not None else None
                    )
                    if num_id is not None:
                        # Ищем <w:num w:numId="N"> и его abstractNumId
                        abstract_id = None
                        for num in numbering_xml.findall(
                            "{http://schemas.openxmlformats.org/"
                            "wordprocessingml/2006/main}num"
                        ):
                            if int(num.get(
                                "{http://schemas.openxmlformats.org/"
                                "wordprocessingml/2006/main}numId"
                            )) == num_id:
                                abs_el = num.find(
                                    "{http://schemas.openxmlformats.org/"
                                    "wordprocessingml/2006/main}"
                                    "abstractNumId"
                                )
                                if abs_el is not None:
                                    abstract_id = int(
                                        abs_el.get(
                                            "{http://schemas.openxml"
                                            "formats.org/wordprocessingml/"
                                            "2006/main}val"
                                        )
                                    )
                                break
                        # Ищем abstractNum и его первый lvl
                        if abstract_id is not None:
                            for absnum in numbering_xml.findall(
                                "{http://schemas.openxmlformats.org/"
                                "wordprocessingml/2006/main}abstractNum"
                            ):
                                if int(absnum.get(
                                    "{http://schemas.openxml"
                                    "formats.org/wordprocessingml/"
                                    "2006/main}abstractNumId"
                                )) == abstract_id:
                                    lvl = absnum.find(
                                        "{http://schemas.openxml"
                                        "formats.org/wordprocessingml/"
                                        "2006/main}lvl"
                                    )
                                    if lvl is not None:
                                        numfmt = lvl.find(
                                            "{http://schemas.openxml"
                                            "formats.org/wordprocessingml/"
                                            "2006/main}numFmt"
                                        )
                                        if numfmt is not None:
                                            fmt_val = numfmt.get(
                                                "{http://schemas.openxml"
                                                "formats.org/wordprocessing"
                                                "ml/2006/main}val"
                                            )
                                            if fmt_val == "bullet":
                                                is_bullet = True
                                    break
                except Exception as exc:
                    log.debug(
                        "Не удалось определить тип списка по numPr: %s",
                        exc,
                    )

                target_style = "List Bullet" if is_bullet else "List Number"

            # --- Создаём целевой абзац ---
            if target_style:
                try:
                    new_para = target_doc.add_paragraph(
                        style=target_style
                    )
                except KeyError:
                    log.debug(
                        "Стиль %r отсутствует в целевом документе — "
                        "используем обычный абзац", target_style,
                    )
                    new_para = target_doc.add_paragraph()
            else:
                new_para = target_doc.add_paragraph()

            # Копируем runs с их форматированием
            if not para.runs:
                continue

            for run in para.runs:
                new_run = new_para.add_run(run.text)
                new_run.bold = run.bold
                new_run.italic = run.italic
                new_run.underline = run.underline
                if run.font.size is not None:
                    new_run.font.size = run.font.size
                if run.font.name:
                    new_run.font.name = run.font.name

        # Таблицы (если есть) — вставляем как простые таблицы
        for table in src.tables:
            try:
                rows = len(table.rows)
                cols = len(table.columns)
                if rows == 0 or cols == 0:
                    continue
                new_table = target_doc.add_table(
                    rows=rows, cols=cols
                )
                for r_idx, row in enumerate(table.rows):
                    for c_idx, cell in enumerate(row.cells):
                        new_table.cell(r_idx, c_idx).text = (
                            cell.text or ""
                        )
            except Exception as exc:
                log.warning(
                    "Не удалось вставить таблицу из %s: %s",
                    src_path, exc,
                )

    def _md_to_tmp_docx(self, md_path: str) -> str:
        """
        Конвертирует .md в временный .docx через markdown_to_docx.
        Возвращает путь к временному файлу или "".
        """
        try:
            from .markdown_docx import markdown_to_docx
        except Exception as exc:
            log.warning(
                "markdown_to_docx недоступен: %s", exc
            )
            return ""

        try:
            md_text = read_any_text(
                md_path, self._max_file_read_chars
            )
        except Exception as exc:
            log.warning(
                "Не удалось прочитать md %s: %s", md_path, exc
            )
            return ""

        if not md_text.strip():
            return ""

        import tempfile
        stem = os.path.splitext(os.path.basename(md_path))[0]
        tmp = os.path.join(
            tempfile.gettempdir(),
            f"prompt_embed_{stem}_{os.getpid()}.docx",
        )
        try:
            markdown_to_docx(md_text, tmp, title="")
        except Exception as exc:
            log.exception(
                "Не удалось сконвертировать %s в docx: %s",
                md_path, exc,
            )
            return ""
        return tmp

    def _export_prompt_file(
        self,
        session_dir: str,
        blocks: List[Dict[str, Any]],
        fmt: str,
    ) -> str:
        base_name = "deepseek_prompt"

        # ------------------------------------------------------------------
        # TXT: собираем plain text из блоков
        # ------------------------------------------------------------------
        if fmt == "txt":
            path = os.path.join(session_dir, f"{base_name}.txt")
            parts: List[str] = []
            for idx, b in enumerate(blocks):
                title = (b.get("title") or "").strip()
                kind = b.get("kind", "text")

                if idx > 0:
                    parts.append("")
                    parts.append("=" * 60)
                    parts.append("")

                if title:
                    parts.append(title)
                    parts.append("=" * 60)
                    parts.append("")

                if kind == "text":
                    parts.append(b.get("text") or "")
                elif kind == "file":
                    p = b.get("path") or ""
                    ext = (b.get("ext") or "").lower()
                    if p and os.path.exists(p):
                        if ext == ".docx":
                            # В .txt сохраняем как plain text
                            try:
                                from docx import Document
                                d = Document(p)
                                parts.append(
                                    "\n".join(
                                        para.text
                                        for para in d.paragraphs
                                    )
                                )
                            except Exception as exc:
                                log.warning(
                                    "Не удалось прочитать docx %s: %s",
                                    p, exc,
                                )
                                parts.append(
                                    b.get("fallback_text") or ""
                                )
                        else:
                            txt = read_any_text(
                                p, self._max_file_read_chars
                            )
                            parts.append(txt or b.get("fallback_text") or "")
                    else:
                        parts.append(b.get("fallback_text") or "")

            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(parts))
            log.info("Промпт сохранён (txt): %s", path)
            return path

        # ------------------------------------------------------------------
        # MD: собираем Markdown
        # ------------------------------------------------------------------
        if fmt == "md":
            path = os.path.join(session_dir, f"{base_name}.md")
            md_parts: List[str] = []
            for idx, b in enumerate(blocks):
                title = (b.get("title") or "").strip()
                kind = b.get("kind", "text")

                if idx > 0:
                    md_parts.append("")
                    md_parts.append("---")
                    md_parts.append("")

                if title:
                    md_parts.append(f"## {title}")
                    md_parts.append("")

                if kind == "text":
                    md_parts.append(b.get("text") or "")
                elif kind == "file":
                    p = b.get("path") or ""
                    ext = (b.get("ext") or "").lower()
                    if p and os.path.exists(p):
                        if ext == ".md":
                            # Сохраняем Markdown как есть
                            md_parts.append(
                                read_any_text(
                                    p, self._max_file_read_chars
                                )
                            )
                        elif ext == ".docx":
                            # В .md конвертируем docx → markdown
                            # через docx_to_markdown (если есть)
                            # или как plain text.
                            md_parts.append(
                                self._docx_to_markdown(p)
                            )
                        else:
                            md_parts.append(
                                read_any_text(
                                    p, self._max_file_read_chars
                                )
                                or b.get("fallback_text") or ""
                            )
                    else:
                        md_parts.append(b.get("fallback_text") or "")

            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(md_parts))
            log.info("Промпт сохранён (md): %s", path)
            return path

        # ------------------------------------------------------------------
        # DOCX: собираем Document с сохранением форматирования
        # ------------------------------------------------------------------
        path = os.path.join(session_dir, f"{base_name}.docx")
        try:
            from docx import Document  # type: ignore
        except ImportError:
            log.warning(
                "python-docx не установлен. Сохраняю .txt вместо .docx"
            )
            path_txt = os.path.join(session_dir, f"{base_name}.txt")
            # Рекурсивно вызываем себя с fmt=txt
            return self._export_prompt_file(
                session_dir=session_dir,
                blocks=blocks,
                fmt="txt",
            )

        try:
            doc = Document()
            self._render_blocks_to_docx(blocks, doc)
            doc.save(path)
            log.info(
                "Промпт сохранён (docx): %s (блоков: %d)",
                path, len(blocks),
            )
            return path
        except Exception as exc:
            log.exception("Ошибка сохранения промпта: %s", exc)
            return ""

    @staticmethod
    def _docx_to_markdown(docx_path: str) -> str:
        """
        Простейшая конвертация .docx → Markdown.

        Сохраняет заголовки (Heading N), списки, жирный/курсив.
        Используется, когда DeepSeek-промпт сохраняется в .md,
        но содержит .docx-вложение/протокол.
        """
        try:
            from docx import Document  # type: ignore
        except ImportError:
            return ""

        try:
            doc = Document(docx_path)
        except Exception as exc:
            log.warning(
                "Не удалось прочитать docx %s: %s", docx_path, exc
            )
            return ""

        lines: List[str] = []
        for para in doc.paragraphs:
            text = para.text or ""
            style = (para.style.name if para.style else "") or ""

            if style.startswith("Heading "):
                try:
                    level = int(style.split(" ")[1])
                    level = max(1, min(level, 6))
                except (IndexError, ValueError):
                    level = 1
                lines.append("#" * level + " " + text)
                continue

            if style == "List Bullet":
                lines.append(f"- {text}")
                continue
            if style == "List Number":
                lines.append(f"1. {text}")
                continue
            if style in ("Quote", "Intense Quote"):
                lines.append(f"> {text}")
                continue

            # Инлайн-форматирование — грубо, по runs.
            if para.runs:
                parts: List[str] = []
                for run in para.runs:
                    t = run.text or ""
                    if not t:
                        continue
                    if run.bold and run.italic:
                        t = f"***{t}***"
                    elif run.bold:
                        t = f"**{t}**"
                    elif run.italic:
                        t = f"*{t}*"
                    parts.append(t)
                lines.append("".join(parts))
            else:
                lines.append(text)

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Управление
    # ------------------------------------------------------------------
    def get_processing_status(self) -> Dict[str, Any]:
        return self.task_queue.get_queue_status()

    def get_failed_tasks(self) -> list:
        return self.task_queue.get_failed_tasks()

    def retry_task(self, task_id: str) -> bool:
        log.info("Ручной запуск задачи %s", task_id)
        return self.task_queue.reset_for_retry(task_id)

    def cancel_processing(self, task_id: str) -> bool:
        log.warning("Отмена задачи %s", task_id)
        event = self._cancel_events.get(task_id)
        if event is None:
            log.warning("Задача %s не найдена среди активных "
                        "(возможно, уже завершена)", task_id)
            self.task_queue.mark_failed(task_id, "cancelled by user")
            return False
        event.set()
        return True

    # ------------------------------------------------------------------
    # Вспомогательное
    # ------------------------------------------------------------------
    def _raise_if_cancelled(
        self, task_id: str, event: asyncio.Event
    ) -> None:
        if event.is_set():
            raise _CancelledError(task_id)

    @staticmethod
    def _save_transcript(data: Dict[str, Any], path: str) -> None:
        try:
            text = (
                data.get("script")
                or data.get("text")
                or data.get("summary")
                or str(data)
            )
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            log.info("Расшифровка сохранена: %s (%d символов, %d строк)",
                     path, len(text), text.count("\n") + 1)
        except Exception as exc:
            log.error("Не удалось сохранить расшифровку %s: %s", path, exc)

    @staticmethod
    def _save_summary(data: Dict[str, Any], path: str) -> None:
        try:
            summary = data.get("summary")
            if not summary or not str(summary).strip():
                log.info("Summary пустой — файл не создаём: %s", path)
                return
            with open(path, "w", encoding="utf-8") as f:
                f.write(str(summary))
            log.info("Резюме сохранено: %s (%d символов)",
                     path, len(str(summary)))
        except Exception as exc:
            log.error("Не удалось сохранить резюме %s: %s", path, exc)

    @staticmethod
    def _write_file(path: str, text: str) -> None:
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            log.info("Файл записан: %s (%d символов)", path, len(text))
        except Exception as exc:
            log.error("Не удалось записать файл %s: %s", path, exc)

    # ------------------------------------------------------------------
    # Фоновая обработка + авто-ретрай
    # ------------------------------------------------------------------
    async def run_forever(self) -> None:
        self._running = True
        log.info("Фоновый воркер обработки запущен (локальный режим)")
        while self._running:
            if self._is_recording_cb is not None:
                try:
                    if self._is_recording_cb():
                        await asyncio.sleep(self._poll_interval)
                        continue
                except Exception as exc:
                    log.warning("Ошибка в is_recording_cb: %s", exc)

            task = self.task_queue.get_next_task()
            if task is not None:
                log.info("Воркер взял задачу: %s", task.get("task_id"))
                try:
                    await self.process_video(
                        task["video_path"], task.get("metadata", {})
                    )
                except Exception as exc:
                    log.exception("Ошибка в воркере: %s", exc)
                continue

            await self._maybe_retry_failed()
            await asyncio.sleep(self._poll_interval)
        log.info("Фоновый воркер остановлен")

    async def _maybe_retry_failed(self) -> None:
        cfg = self.config.get("queue", {})
        if not cfg.get("auto_retry_enabled", True):
            return

        interval_sec = self._retry_check_interval
        max_retries = int(cfg.get("max_retries", 10))

        now = time.monotonic()
        if now - self._last_retry_check < interval_sec:
            return
        self._last_retry_check = now

        failed = self.task_queue.get_failed_tasks()
        if not failed:
            return

        log.info("Авто-ретрай: найдено %d неудачных задач", len(failed))
        for t in failed:
            tid = t.get("task_id", "")
            retries = int(t.get("retries", 0))
            if retries >= max_retries:
                log.warning("Задача %s исчерпала попытки (%d) — пропуск",
                            tid, retries)
                continue
            log.info("Авто-ретрай задачи %s (попытка %d/%d)",
                     tid, retries + 1, max_retries)
            self.task_queue.reset_for_retry(tid)

    def stop(self) -> None:
        log.info("Остановка воркера обработки")
        self._running = False


class _CancelledError(Exception):
    """Внутреннее исключение для прерывания конвейера по отмене."""
    def __init__(self, task_id: str = "") -> None:
        super().__init__(f"cancelled: {task_id}")
        self.task_id = task_id