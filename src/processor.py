"""Конвейер обработки: конвертация → транскрибация → суммаризация → DeepSeek-промпт."""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import QObject, Signal

from .litellm_client import LiteLLMClient, LiteLLMError
from .logger import get_logger
from .markdown_to_bitrix import markdown_to_plain_with_bb
from .task_queue import TaskQueue
from .transcribe_client import TranscribeClient

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
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.config = config or {}
        self.task_queue = task_queue
        self._is_recording_cb = is_recording_cb
        # Отмена теперь через asyncio.Event — её можно взять
        # и передать в TranscribeClient._poll, и проверить
        # между шагами конвейера.
        self._cancel_events: Dict[str, asyncio.Event] = {}
        self._running = False
        self._last_retry_check = 0.0
        log.info("VideoProcessor инициализирован "
                 "(is_recording_cb=%s)", is_recording_cb is not None)

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

        # Регистрируем событие отмены ДО старта шагов,
        # чтобы cancel_processing, вызванный сразу после старта,
        # не потерялся.
        cancel_event = asyncio.Event()
        self._cancel_events[task_id] = cancel_event

        log.info("=" * 60)
        log.info("Начало обработки задачи %s", task_id)
        log.info("Видео: %s", video_path)
        log.info("Метаданные: project=%s, name=%s, generate_summary=%s, "
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
                 os.path.basename(metadata.get("manual_protocol_path") or "") or "—")

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

            # --- Шаг 2: транскрибация + суммаризация ---
            transcribe_cfg = self.config.get("transcribe", {})
            transcribe_url = (transcribe_cfg.get("url") or "").strip()

            sum_cfg = self.config.get("summarizer", {}) or {}
            provider = str(sum_cfg.get("provider", "server")).strip().lower()
            if provider not in ("server", "litellm"):
                provider = "server"

            # Флаг формирования summary — берётся из метаданных записи.
            # По умолчанию (если поля нет) — False (не формировать).
            generate_summary = bool(metadata.get("generate_summary", False))

            gl = self.config.get("glossary", {}) or {}
            gl_terms = gl.get("terms", []) or []
            gl_text = self._build_glossary_text(gl_terms)
            gl_to_summarizer = bool(gl.get("send_to_summarizer", True))
            gl_to_deepseek = bool(gl.get("send_to_deepseek", True))

            transcript_path = ""
            summary_path = ""

            if transcribe_url:
                log.info(
                    "[%s] Шаг 2/2: транскрибация "
                    "(провайдер суммаризации: %s, summary: %s)",
                    task_id, provider,
                    "on" if generate_summary else "off",
                )
                self.task_queue.update_task_status(task_id, "transcribing", 50)
                self.task_progress.emit(task_id, 50, "transcribing")

                # Промпт пользователя
                prompt = (
                    metadata.get("prompt")
                    or self.config.get("metadata", {}).get("default_prompt", "")
                )
                log.debug("[%s] Промпт пользователя (%d символов)",
                          task_id, len(prompt))

                # Контекст записи — шапка перед промптом
                ctx_header = self._build_context_header(
                    metadata=metadata,
                    include_name=bool(metadata.get("include_name_in_prompt")),
                    include_project=bool(metadata.get("include_project_in_prompt")),
                    include_comment=bool(metadata.get("include_comment_in_prompt")),
                )
                if ctx_header:
                    prompt = ctx_header + "\n\n" + prompt
                    log.info("[%s] В промпт добавлен контекст записи "
                             "(%d символов)", task_id, len(ctx_header))

                # Summary — конвертируем в plain text и дописываем.
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

                # Вложения
                if metadata.get("send_attachments_to_transcribe"):
                    att_text = self._read_attachments_text(
                        metadata.get("attachments", []) or []
                    )
                    if att_text:
                        prompt = (
                            prompt
                            + "\n\n===== ДОПОЛНИТЕЛЬНЫЙ КОНТЕКСТ (вложения) =====\n\n"
                            + att_text
                        )
                        log.info("[%s] К промпту добавлены вложения "
                                 "(%d символов)", task_id, len(att_text))

                # Глоссарий
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

                # Если summary для записи не формируется — не передаём
                # промпт на сервер транскрибации: он там не нужен.
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

                # Отмена в процессе транскрибации
                if isinstance(transcript, dict) and \
                        transcript.get("status") == "cancelled":
                    log.info("[%s] Транскрибация отменена", task_id)
                    self.task_queue.mark_failed(task_id, "cancelled by user")
                    self.task_failed.emit(task_id, "cancelled by user")
                    return {"error": "cancelled", "task_id": task_id}

                if isinstance(transcript, dict) and transcript.get("error"):
                    err = str(transcript["error"])
                    log.error("[%s] Ошибка транскрибации: %s", task_id, err)
                    self.task_queue.mark_failed(task_id, f"transcribe: {err}")
                    self.task_failed.emit(task_id, f"transcribe: {err}")
                    return {"error": err, "task_id": task_id}

                if isinstance(transcript, dict) and \
                        transcript.get("status") == "error":
                    err = str(transcript.get("error")
                              or "неизвестная ошибка на сервере")
                    log.error("[%s] Сервер вернул status=error: %s",
                              task_id, err)
                    self.task_queue.mark_failed(task_id, f"transcribe: {err}")
                    self.task_failed.emit(task_id, f"transcribe: {err}")
                    return {"error": err, "task_id": task_id}

                self._raise_if_cancelled(task_id, cancel_event)

                transcript_path = str(Path(video_path).with_suffix(".txt"))
                summary_path = str(Path(video_path).with_name("video_summary.md"))
                self._save_transcript(transcript, transcript_path)

                # --- Суммаризация ---
                if not generate_summary:
                    log.info(
                        "[%s] Формирование summary отключено для этой "
                        "записи — пропускаем шаг суммаризации "
                        "(провайдер=%s)",
                        task_id, provider,
                    )
                elif provider == "litellm":
                    self.task_queue.update_task_status(task_id, "summarizing", 70)
                    self.task_progress.emit(task_id, 70, "summarizing")
                    try:
                        summary_text = await self._summarize_with_litellm(
                            task_id=task_id,
                            script=str(transcript.get("script") or ""),
                            user_prompt=prompt,
                            video_title=metadata.get("name", "") or "",
                        )
                        if summary_text:
                            self._write_file(summary_path, summary_text)
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
                    # server-режим: сервер уже отдал summary в transcript,
                    # сохраняем его на диск (если он есть).
                    self._save_summary(transcript, summary_path)
            else:
                log.info("[%s] Транскрибация пропущена: URL сервера не задан",
                         task_id)

            self._raise_if_cancelled(task_id, cancel_event)

            # --- Шаг 3: DeepSeek-промпт ---
            if metadata.get("generate_deepseek_prompt") and transcript_path:
                use_scrum = bool(metadata.get("is_scrum", False))
                # Ручной протокол
                manual_protocol_path = str(
                    metadata.get("manual_protocol_path") or ""
                ).strip()
                manual_protocol_text = ""
                if manual_protocol_path and os.path.exists(manual_protocol_path):
                    manual_protocol_text = self._read_any_text(manual_protocol_path)
                    log.info("[%s] Ручной протокол прочитан: %s (%d символов)",
                             task_id, manual_protocol_path,
                             len(manual_protocol_text))
                # Summary — конвертируем в plain и добавим в формате
                # «Краткое описание».
                summary_bb = str(metadata.get("summary_bb") or "").strip()
                summary_for_deepseek = (
                    markdown_to_plain_with_bb(summary_bb) if summary_bb else ""
                )

                try:
                    prompt_path = self._build_deepseek_prompt(
                        session_dir=os.path.dirname(video_path),
                        transcript_path=transcript_path,
                        previous_protocol_path=metadata.get(
                            "previous_protocol_path", ""
                        ),
                        attachments=metadata.get("attachments", []) or [],
                        include_attachments=bool(
                            metadata.get("send_attachments_to_deepseek", False)
                        ),
                        use_scrum_template=use_scrum,
                        user_prompt=str(metadata.get("prompt") or ""),
                        glossary_text=gl_text if gl_to_deepseek else "",
                        metadata=metadata,
                        summary_text=summary_for_deepseek,
                        manual_protocol_text=manual_protocol_text,
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
                    log.exception("[%s] Не удалось собрать DeepSeek-промпт: %s",
                                  task_id, exc)
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
            # Чистим событие отмены — задача завершена
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
        """
        Конвертирует видео в аудио через ffmpeg.

        Отмена: если cancel_event выставлен — завершаем дочерний
        процесс ffmpeg через terminate(), затем kill().
        """
        out = str(Path(video_path).with_suffix(f".{fmt}"))
        codec = "libmp3lame" if fmt == "mp3" else "aac"
        cmd = [
            "ffmpeg", "-y", "-i", video_path, "-vn",
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

            # Ждём завершения, но проверяем отмену раз в 0.5 с
            if cancel_event is not None:
                wait_task = asyncio.ensure_future(proc.wait())
                cancel_task = asyncio.ensure_future(cancel_event.wait())
                try:
                    done, pending = await asyncio.wait(
                        {wait_task, cancel_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if cancel_task in done:
                        # отмена — убиваем ffmpeg
                        log.info("Конвертация отменена — завершаем ffmpeg "
                                 "(PID=%s)", proc.pid)
                        try:
                            proc.terminate()
                        except ProcessLookupError:
                            pass
                        try:
                            await asyncio.wait_for(proc.wait(), timeout=5.0)
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
                         result.get("status") if isinstance(result, dict)
                         else "—")
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
            text = self._read_any_text(p)
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
        """
        Формирует файл с готовым промптом для DeepSeek.

        Дополнительно:
          • summary_text          — краткое описание записи (plain, без разметки);
          • manual_protocol_text  — вручную подготовленный протокол.
        """
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
                include_project=bool(metadata.get("include_project_in_prompt")),
                include_comment=bool(metadata.get("include_comment_in_prompt")),
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
                    self.config.get("metadata", {}).get("default_prompt", "")
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

        transcript_text = ""
        try:
            with open(transcript_path, "r", encoding="utf-8") as f:
                transcript_text = f.read()
        except Exception as exc:
            log.warning("Не удалось прочитать стенограмму %s: %s",
                        transcript_path, exc)

        previous_text = ""
        if previous_protocol_path and os.path.exists(previous_protocol_path):
            previous_text = self._read_any_text(previous_protocol_path)

        attachments_text = ""
        if include_attachments and attachments:
            attachments_text = self._read_attachments_text(attachments)
            if attachments_text:
                log.info("В промпт DeepSeek добавлены вложения "
                         "(%d символов)", len(attachments_text))

        # --- Сборка ---
        parts: List[str] = []

        if ctx_header:
            parts.append(ctx_header)
            parts.append("")

        parts.append(template)
        parts.append("")

        # Ручной протокол — самый приоритетный контекст
        if manual_protocol_text:
            parts.append("=" * 60)
            parts.append("РУЧНОЙ ПРОТОКОЛ (загружен пользователем)")
            parts.append("=" * 60)
            parts.append("")
            parts.append(manual_protocol_text)
            parts.append("")

        # Краткое описание записи
        if summary_text:
            parts.append("=" * 60)
            parts.append("КРАТКОЕ ОПИСАНИЕ ЗАПИСИ (введено пользователем)")
            parts.append("=" * 60)
            parts.append("")
            parts.append(summary_text)
            parts.append("")

        if previous_text or use_scrum_template:
            parts.append("=" * 60)
            parts.append("ПРЕДЫДУЩИЙ ПРОТОКОЛ")
            parts.append("=" * 60)
            parts.append("")
            parts.append(previous_text or "(Предыдущий протокол не приложен.)")

        if glossary_text:
            parts.append("")
            parts.append("=" * 60)
            parts.append("ГЛОССАРИЙ (используй правильные формулировки)")
            parts.append("=" * 60)
            parts.append("")
            parts.append(glossary_text)

        if attachments_text:
            parts.append("")
            parts.append("=" * 60)
            parts.append("ВЛОЖЕНИЯ")
            parts.append("=" * 60)
            parts.append("")
            parts.append(attachments_text)

        parts.append("")
        parts.append("=" * 60)
        parts.append("СТЕНОГРАММА СЕГОДНЯШНЕГО СОВЕЩАНИЯ")
        parts.append("=" * 60)
        parts.append("")
        parts.append(transcript_text)

        full_text = "\n".join(parts)
        return self._export_prompt_file(
            session_dir=session_dir,
            full_text=full_text,
            fmt=export_format,
        )

    def _export_prompt_file(self, session_dir: str, full_text: str,
                            fmt: str) -> str:
        base_name = "deepseek_prompt"
        try:
            if fmt == "txt":
                path = os.path.join(session_dir, f"{base_name}.txt")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(full_text)
                log.info("Промпт сохранён (txt): %s (%d символов)",
                         path, len(full_text))
                return path

            if fmt == "md":
                path = os.path.join(session_dir, f"{base_name}.md")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(full_text)
                log.info("Промпт сохранён (md): %s (%d символов)",
                         path, len(full_text))
                return path

            path = os.path.join(session_dir, f"{base_name}.docx")
            try:
                from docx import Document  # type: ignore
            except ImportError:
                log.warning("python-docx не установлен. "
                            "Сохраняю .txt вместо .docx")
                path = os.path.join(session_dir, f"{base_name}.txt")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(full_text)
                return path

            doc = Document()
            for line in full_text.splitlines():
                doc.add_paragraph(line)
            doc.save(path)
            log.info("Промпт сохранён (docx): %s (%d символов)",
                     path, len(full_text))
            return path
        except Exception as exc:
            log.exception("Ошибка сохранения промпта: %s", exc)
            return ""

    @staticmethod
    def _read_any_text(path: str) -> str:
        ext = os.path.splitext(path)[1].lower()
        if ext in (".txt", ".md", ".csv", ".json", ".log"):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    return f.read()
            except Exception as exc:
                log.warning("Не удалось прочитать %s: %s", path, exc)
                return ""
        if ext == ".docx":
            try:
                from docx import Document  # type: ignore
                doc = Document(path)
                return "\n".join(p.text for p in doc.paragraphs)
            except ImportError:
                log.warning("python-docx не установлен, docx не прочитан: %s", path)
                return ""
            except Exception as exc:
                log.warning("Ошибка чтения .docx %s: %s", path, exc)
                return ""
        log.warning("Неподдерживаемое расширение вложения: %s (%s)", ext, path)
        return ""

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
        """
        Отменяет задачу.

        Работает даже если задача «висит» внутри транскрибации:
        событие отмены прокидывается в TranscribeClient._poll
        и в convert_to_audio, которые реагируют немедленно.
        """
        log.warning("Отмена задачи %s", task_id)
        event = self._cancel_events.get(task_id)
        if event is None:
            log.warning("Задача %s не найдена среди активных "
                        "(возможно, уже завершена)", task_id)
            # Всё равно помечаем в очереди — на случай гонок
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
                        await asyncio.sleep(3)
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
            await asyncio.sleep(2)
        log.info("Фоновый воркер остановлен")

    async def _maybe_retry_failed(self) -> None:
        cfg = self.config.get("queue", {})
        if not cfg.get("auto_retry_enabled", True):
            return

        interval_sec = int(cfg.get("retry_interval_minutes", 5)) * 60
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