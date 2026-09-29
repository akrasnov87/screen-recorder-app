"""Единая система подсказок для полей интерфейса.

Изменения:
  • Добавлены подсказки default_project и default_chat_id.
  • Добавлены подсказки для тегов: tags_list, meta_tags,
    meta_tags_list, meta_include_tags_in_prompt, lib_tag,
    lib_tag_enabled.
"""
from __future__ import annotations

from typing import Dict, Optional

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QVBoxLayout, QWidget,
)

from .logger import get_logger

log = get_logger(__name__)

TOAST_THRESHOLD = 120
TOAST_HIDE_DELAY_MS = 120


TOOLTIPS: Dict[str, str] = {
    # --- Общие ---
    "projects_list": (
        "Список проектов, доступных при вводе метаданных записи.\n\n"
        "Каждая строка таблицы — один проект:\n"
        "  • Проект      — название, отображается в выпадающем списке "
        "в окне метаданных и в фильтрах (Записи, Библиотека).\n"
        "  • Чат Bitrix24 — ID чата для отправки протоколов и summary.\n\n"
        "Можно добавлять, удалять и менять порядок проектов.\n"
        "Порядок влияет на то, какой проект выбран по умолчанию "
        "при старте записи (если не задан явный проект по умолчанию "
        "ниже в блоке «Проект и чат по умолчанию»)."
    ),
    "tags_list": (
        "Справочник тегов — меток, которыми можно помечать записи.\n\n"
        "Каждая строка таблицы — один тег:\n"
        "  • Тег   — название. Двойной клик — редактирование.\n"
        "  • Цвет  — HEX-код (например #C62828). Двойной клик "
        "открывает палитру.\n\n"
        "Одна запись может иметь несколько тегов. Теги "
        "используются:\n"
        "  • в карточке метаданных записи — блок «Теги»;\n"
        "  • в фильтре раздела «Библиотека» — можно искать "
        "только среди записей с выбранным тегом.\n\n"
        "Порядок тегов в таблице влияет на порядок в списке "
        "карточки метаданных."
    ),
    "default_project": (
        "Проект, который автоматически подставляется в карточку "
        "метаданных для НОВЫХ записей.\n\n"
        "Значение выпадающего списка:\n"
        "  • «— первый из списка —» — используется проект, стоящий "
        "первым в таблице проектов выше.\n"
        "  • Любое другое имя — именно этот проект будет выбран "
        "по умолчанию.\n\n"
        "В карточке конкретной записи пользователь может "
        "переопределить значение вручную.\n\n"
        "Если введённое имя отсутствует в списке проектов — "
        "программа использует первый проект из таблицы."
    ),
    "default_chat_id": (
        "ID чата Bitrix24, куда по умолчанию отправляются "
        "протоколы и summary.\n\n"
        "Приоритеты при отправке:\n"
        "  1) этот ID чата (если задан);\n"
        "  2) чат проекта записи (столбец «Чат Bitrix24» в таблице "
        "проектов выше);\n"
        "  3) пустое поле — пользователь вводит ID вручную в диалоге "
        "отправки.\n\n"
        "Формат: <code>chat2101</code> или просто <code>2101</code>. "
        "Метод <code>im.message.add</code> принимает оба варианта.\n\n"
        "Это удобно, когда все протоколы по умолчанию уходят в один "
        "общий чат (например, «Команда») независимо от проекта."
    ),
    "employees_list": (
        "Справочник сотрудников: ФИО и ID личного чата в Bitrix24.\n\n"
        "Каждая строка таблицы — один сотрудник:\n"
        "  • ФИО — как показывать сотрудника в списке получателей.\n"
        "  • Чат Bitrix24 — числовой ID пользователя, которому "
        "адресуется сообщение.\n\n"
        "Используется в окне отправки в чат: можно выбрать "
        "не проект, а конкретного сотрудника — тогда протокол "
        "или summary уйдут в личный диалог с ним.\n\n"
        "Можно добавлять, удалять и менять порядок сотрудников."
    ),
    "employees_table": (
        "Список сотрудников. Двойной клик по ячейке — редактирование.\n\n"
        "Колонки:\n"
        "  • ФИО — как сотрудник отображается в списке получателей.\n"
        "  • Чат Bitrix24 — числовой ID пользователя "
        "(без префикса chat). Например, 123.\n\n"
        "Найти ID можно через API методом im.recent.get — ищите "
        "диалог типа user, поле id."
    ),
    "prompts_table": (
        "Библиотека готовых промптов для сервера транскрибации.\n\n"
        "Промпт — это инструкция, которую сервер применяет "
        "к расшифровке. Например: «Составь протокол совещания», "
        "«Выдели action items».\n\n"
        "В окне метаданных записи можно выбрать промпт из этой "
        "библиотеки или написать свой."
    ),
    "default_prompt": (
        "Промпт, который подставляется автоматически, если "
        "пользователь не выбрал другой в окне метаданных.\n\n"
        "Используется для записей без явного промпта."
    ),
    "name_templates": (
        "Шаблоны названий для записей.\n\n"
        "Используются в окне метаданных записи — это выпадающий "
        "список рядом с полем «Название».\n\n"
        "Плейсхолдеры:\n"
        "  {name} / {название} — введённое имя;\n"
        "  {abbr} / {сокр}      — сокращение;\n"
        "  {date} / {дата}      — текущая дата YYYY-MM-DD;\n"
        "  {time} / {время}     — текущее время HH-MM;\n"
        "  {datetime}           — дата и время."
    ),

    # --- Транскрибация ---
    "tr_url": (
        "Адрес сервера транскрибации.\n\n"
        "Пример: http://localhost:8000 или "
        "https://transcribe.company.ru\n\n"
        "Если поле пустое — шаг транскрибации будет пропущен, "
        "останется только конвертация видео в аудио."
    ),
    "tr_key": (
        "Access key для авторизации на сервере транскрибации.\n\n"
        "Выдаётся администратором сервера."
    ),
    "tr_connect_timeout": (
        "Сколько секунд ждать установки TCP-соединения с сервером."
    ),
    "tr_read_timeout": (
        "Сколько секунд ждать ответа на каждый отдельный "
        "HTTP-запрос."
    ),
    "tr_max_wait": (
        "Общий лимит ожидания результата транскрибации.\n\n"
        "По умолчанию 7200 секунд (2 часа)."
    ),

    # --- Суммаризация ---
    "sum_enabled": (
        "Формировать ли краткое содержание (summary) для новых "
        "записей."
    ),
    "sum_provider": (
        "Где формируется протокол/резюме записи."
    ),
    "sum_litellm": (
        "Настройки локального вызова LiteLLM."
    ),
    "sum_litellm_url": (
        "Базовый адрес LiteLLM-прокси."
    ),
    "sum_litellm_key": (
        "API-ключ для LiteLLM."
    ),
    "sum_litellm_model": (
        "Название модели, зарегистрированное в LiteLLM."
    ),
    "sum_litellm_temperature": (
        "Температура сэмплирования (0.0–2.0)."
    ),
    "sum_litellm_max_tokens": (
        "Максимальная длина ответа модели в токенах."
    ),
    "sum_litellm_connect_timeout": (
        "Сколько секунд ждать установки соединения с LiteLLM."
    ),
    "sum_litellm_read_timeout": (
        "Сколько секунд ждать ответа от LiteLLM."
    ),
    "sum_litellm_system": (
        "Системное сообщение для модели."
    ),

    # --- Глоссарий ---
    "glossary_terms": (
        "Список терминов и аббревиатур, которые будут переданы ИИ."
    ),
    "glossary_send_to_summarizer": (
        "Добавлять глоссарий в промпт суммаризации."
    ),
    "glossary_send_to_deepseek": (
        "Добавлять глоссарий в файл deepseek_prompt.*"
    ),

    # --- Запись ---
    "monitor": (
        "Монитор, который будет записан."
    ),
    "mic": (
        "Записывать звук с микрофона."
    ),
    "watermark": (
        "Накладывать на видео водяной знак."
    ),
    "hotkey_start": (
        "Глобальная горячая клавиша для старта/паузы/"
        "возобновления записи."
    ),
    "hotkey_stop": (
        "Глобальная горячая клавиша для остановки записи."
    ),
    "metadata_on_start": (
        "Показывать окно ввода метаданных при старте записи."
    ),
    "metadata_on_stop": (
        "Показывать окно метаданных повторно при остановке записи."
    ),
    "overlay_panel": (
        "Показывать плавающую панель управления поверх всех окон."
    ),
    "start_notification": (
        "Показывать системное уведомление о начале записи."
    ),

    # --- App: ffmpeg ---
    "app_ffmpeg_start_check_delay": (
        "Задержка (в секундах) после запуска ffmpeg перед "
        "проверкой, что процесс не упал сразу.\n\n"
        "По умолчанию 0.3 сек."
    ),
    "app_ffmpeg_stop_timeout": (
        "Сколько секунд ждать корректного завершения ffmpeg "
        "после сигнала остановки (SIGINT).\n\n"
        "По умолчанию 10 секунд."
    ),
    "app_ffmpeg_kill_timeout": (
        "Сколько секунд ждать после SIGKILL, прежде чем признать, "
        "что процесс завис навсегда.\n\n"
        "Обычно 5 секунд достаточно."
    ),

    # --- App: overlay ---
    "app_overlay_hide_delay_ms": (
        "Через сколько миллисекунд бездействия плавающая панель "
        "автоматически скроется."
    ),
    "app_overlay_log_lines": (
        "Сколько последних строк лога показывать на плавающей "
        "панели."
    ),

    # --- Logging ---
    "log_path": (
        "Путь к файлу лога приложения."
    ),
    "log_level": (
        "Уровень детализации лога."
    ),
    "log_max_bytes_mb": (
        "Максимальный размер файла лога в мегабайтах, после "
        "которого он ротируется."
    ),
    "log_backup_count": (
        "Сколько старых файлов лога хранить."
    ),

    # --- Settings export/import ---
    "settings_export": (
        "Сохранить текущие настройки в файл JSON."
    ),
    "settings_import": (
        "Загрузить настройки из ранее сохранённого файла JSON."
    ),

    # --- Метаданные записи ---
    "meta_generate_summary": (
        "Формировать краткое содержание (summary) для этой записи."
    ),
    "meta_project": (
        "Проект, к которому относится запись."
    ),
    "meta_name": (
        "Название записи."
    ),
    "meta_comment": (
        "Свободный комментарий к записи."
    ),
    "meta_prompt": (
        "Промпт для сервера транскрибации."
    ),
    "meta_is_scrum": (
        "Пометить запись как скрам-митинг."
    ),
    "meta_protocol": (
        "Файл протокола предыдущего совещания."
    ),
    "meta_attachments": (
        "Файлы-вложения к записи."
    ),
    "meta_send_to_transcribe": (
        "Передать содержимое вложений на сервер транскрибации."
    ),
    "meta_send_to_deepseek": (
        "Включить содержимое вложений в промпт DeepSeek."
    ),
    "meta_context_to_prompt": (
        "Контекст записи в промпте."
    ),
    "meta_include_name_in_prompt": (
        "Добавлять название записи в промпт."
    ),
    "meta_include_project_in_prompt": (
        "Добавлять проект в промпт."
    ),
    "meta_include_comment_in_prompt": (
        "Добавлять комментарий к записи в промпт."
    ),
    "meta_include_tags_in_prompt": (
        "Добавлять теги записи в промпт."
    ),
    "meta_tags": (
        "Теги записи — произвольные метки для быстрого поиска.\n\n"
        "Одна запись может иметь несколько тегов. Справочник "
        "тегов настраивается в Настройки → Теги.\n\n"
        "Теги используются в фильтре раздела «Библиотека»."
    ),
    "meta_tags_list": (
        "Отметьте один или несколько тегов из справочника. "
        "Снять все — кнопкой «Снять все».\n\n"
        "Кнопка «Добавить новый тег…» создаёт тег, которого "
        "нет в справочнике, и сразу помечает им запись."
    ),

    # --- Библиотека ---
    "lib_intro": (
        "Полнотекстовый поиск по всем сохранённым записям."
    ),
    "lib_query": (
        "Поисковый запрос."
    ),
    "lib_search_in": (
        "Где искать совпадения."
    ),
    "lib_project": (
        "Ограничение поиска одним проектом."
    ),
    "lib_date_from": (
        "Начало периода поиска (включительно)."
    ),
    "lib_date_to": (
        "Конец периода поиска (включительно)."
    ),
    "lib_date_enabled": (
        "Включить фильтр по датам."
    ),
    "lib_fuzzy": (
        "Порог схожести для нечёткого поиска (fuzzy)."
    ),
    "lib_results": (
        "Результаты поиска."
    ),
    "lib_preview": (
        "Превью выбранного результата."
    ),
    "lib_open_folder": (
        "Открыть папку записи."
    ),
    "lib_open_file": (
        "Открыть именно тот файл, где найдено совпадение."
    ),
    "lib_save_file": (
        "Скопировать найденный файл в любое место на диске."
    ),
    "lib_prompt_enabled": (
        "Включить формирование промпта из результатов поиска."
    ),
    "lib_prompt_before": (
        "Сколько символов включать в промпт ПЕРЕД совпадением."
    ),
    "lib_prompt_after": (
        "Сколько символов включать в промпт ПОСЛЕ совпадения."
    ),
    "lib_prompt_max": (
        "Максимальное количество совпадений в промпте."
    ),
    "lib_prompt_dedup": (
        "Схлопывать пересекающиеся фрагменты."
    ),
    "lib_prompt_instruction": (
        "Инструкция для ИИ."
    ),
    "lib_prompt_save_docx": (
        "Сохранить сформированный промпт в документ .docx."
    ),
    "lib_prompt_save_md": (
        "Сохранить сформированный промпт в формате Markdown."
    ),
    "lib_tag": (
        "Фильтр поиска по тегу записи.\n\n"
        "Выберите тег из списка — поиск будет идти только "
        "по записям с этим тегом.\n\n"
        "«— без тега —» оставляет только записи без тегов.\n\n"
        "Список тегов формируется из справочника "
        "(Настройки → Теги) и из уже сохранённых записей."
    ),
    "lib_tag_enabled": (
        "Включить фильтр поиска по тегу."
    ),

    # --- App: библиотека ---
    "app_search_default_fuzzy": (
        "Значение по умолчанию для порога нечёткого поиска."
    ),
    "app_search_default_context_chars": (
        "Сколько символов включать в промпт до и после "
        "совпадения."
    ),
    "app_search_default_max_prompt_hits": (
        "Максимум совпадений в промпте по умолчанию."
    ),
    "app_fuzzy_max_word_distance": (
        "Максимальное расстояние между словами запроса."
    ),
    "app_search_cancel_wait_ms": (
        "Сколько миллисекунд ждать завершения поиска после "
        "запроса отмены."
    ),

    # --- Импорт ---
    "imp_intro": ("Импорт готовых материалов в программу."),
    "imp_video": ("Видео- или аудиофайл записи."),
    "imp_transcript": ("Готовая стенограмма записи."),
    "imp_protocol": ("Готовый протокол записи."),
    "imp_date": ("Дата, к которой относится запись."),
    "imp_name": ("Название записи."),
    "imp_project": ("Проект, к которому относится запись."),
    "imp_comment": ("Необязательный комментарий к записи."),
    "imp_tags": (
        "Теги импортируемой записи.\n\n"
        "Одна запись может иметь несколько тегов. Справочник "
        "тегов настраивается в Настройки → Теги.\n\n"
        "Теги сохраняются в session.json и используются "
        "в фильтре раздела «Библиотека»."
    ),
    "imp_tags_list": (
        "Отметьте один или несколько тегов из справочника — "
        "они будут присвоены импортируемой записи.\n\n"
        "Кнопка «Добавить новый тег…» создаёт тег, которого "
        "нет в справочнике, и сразу помечает им запись.\n"
        "Снять все — кнопкой «Снять все»."
    ),

    # --- Bitrix24 ---
    "bitrix_enabled": (
        "Включить интеграцию с Bitrix24."
    ),
    "bitrix_webhook": (
        "URL входящего вебхука Bitrix24."
    ),
    "bitrix_connect_timeout": (
        "Таймаут установки TCP-соединения с порталом Bitrix24."
    ),
    "bitrix_read_timeout": (
        "Таймаут ожидания ответа на HTTP-запрос."
    ),
    "bitrix_default_send": (
        "Что предлагается отправить по умолчанию."
    ),
    "bitrix_header": (
        "Добавлять в начало сообщения заголовок."
    ),
    "bitrix_system": (
        "Отправлять как системное сообщение (SYSTEM=Y)."
    ),
    "bitrix_no_preview": (
        "Отключить предпросмотр ссылок в сообщении."
    ),
    "bitrix_files_section": (
        "Настройки отправки файлов в Bitrix24."
    ),
    "bitrix_file_threshold": (
        "Порог, после которого текст отправляется файлом."
    ),
    "bitrix_upload_folder": (
        "Папка на Диске Bitrix24, куда загружать файлы."
    ),
    "bitrix_max_message_chars": (
        "Максимальная длина одного сообщения Bitrix24."
    ),
    "bitrix_retry_count": (
        "Сколько раз пытаться повторить отправку в Bitrix24."
    ),
    "bitrix_retry_delay": (
        "Пауза в секундах между попытками повтора отправки."
    ),
    "bitrix_chat_id": (
        "ID чата Bitrix24 вручную."
    ),
    "bitrix_recipients_list": (
        "Список получателей для массовой рассылки."
    ),
    "bitrix_recipient_type": (
        "Кому отправить сообщение (устаревший параметр)."
    ),
    "bitrix_recipient": (
        "Выбор конкретного получателя (устаревший параметр)."
    ),

    # --- Очередь ---
    "auto_retry": (
        "Автоматически повторять задачи с ошибкой."
    ),
    "retry_interval": (
        "Как часто проверять очередь на наличие error-задач."
    ),
    "max_retries": (
        "Сколько раз пытаться повторить одну задачу."
    ),
    "queue_pause_when_recording": (
        "Не обрабатывать очередь задач, пока идёт запись экрана."
    ),

    # --- Скрам ---
    "scrum_template": (
        "Шаблон промпта для DeepSeek."
    ),
    "scrum_format": (
        "Формат, в котором сохраняется готовый промпт DeepSeek."
    ),

    # --- Форматы и сжатие ---
    "audio_format": (
        "Формат аудио, в который конвертируется видео."
    ),
    "audio_bitrate": (
        "Битрейт аудио при конвертации."
    ),
    "video_bitrate": (
        "Битрейт видео при записи экрана."
    ),
    "compression_level": (
        "Уровень сжатия видео (0–9)."
    ),

    # --- Хранилище ---
    "temp_path": (
        "Корневая папка для хранения записей и очереди задач."
    ),
    "retention_hours": (
        "Сколько часов хранить завершённые записи."
    ),
}


class TooltipToast(QWidget):
    """Всплывающее окно для длинных подсказок."""

    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.ToolTip
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)

        self.label = QLabel(self)
        self.label.setWordWrap(True)
        self.label.setMaximumWidth(420)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setStyleSheet(
            "QLabel { color: #f0f0f0; background: transparent; }"
        )
        layout.addWidget(self.label)

        self.setStyleSheet(
            "TooltipToast {"
            "  background-color: #2b2b2b;"
            "  border: 1px solid #555;"
            "  border-radius: 6px;"
            "}"
        )


class TooltipManager(QObject):
    """Менеджер подсказок (ленивая инициализация QWidget)."""

    def __init__(self) -> None:
        super().__init__()
        self._toast: Optional[TooltipToast] = None
        self._hide_timer: Optional[QTimer] = None
        self._current_widget: Optional[QWidget] = None

    def _ensure_widgets(self) -> bool:
        if self._toast is not None:
            return True
        if QApplication.instance() is None:
            return False
        self._toast = TooltipToast()
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(TOAST_HIDE_DELAY_MS)
        self._hide_timer.timeout.connect(self._toast.hide)
        return True

    def install(self, widget: QWidget, text: str) -> None:
        if not text:
            return

        if len(text) >= TOAST_THRESHOLD:
            widget.setToolTip("")
            widget.installEventFilter(self)
            widget.setProperty("_tooltip_text", text)
        else:
            widget.setToolTip(text)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Enter:
            text = obj.property("_tooltip_text")
            if text:
                self._show_toast(obj, str(text))
        elif event.type() == QEvent.Type.Leave:
            if obj is self._current_widget:
                if self._hide_timer is not None:
                    self._hide_timer.start()
        return super().eventFilter(obj, event)

    def _show_toast(self, widget: QWidget, text: str) -> None:
        if not self._ensure_widgets():
            return
        assert self._toast is not None
        assert self._hide_timer is not None

        self._hide_timer.stop()
        self._current_widget = widget
        self._toast.label.setText(text)
        self._toast.adjustSize()

        try:
            global_pos = widget.mapToGlobal(
                widget.rect().topRight()
            )
        except Exception:
            global_pos = QCursor.pos()

        x = global_pos.x() + 12
        y = global_pos.y()

        screen = widget.screen() or widget.window().screen()
        if screen is not None:
            geo = screen.availableGeometry()
            if x + self._toast.width() > geo.right():
                try:
                    left_pos = widget.mapToGlobal(
                        widget.rect().topLeft()
                    )
                    x = left_pos.x() - self._toast.width() - 12
                except Exception:
                    x = geo.right() - self._toast.width() - 8
            if y + self._toast.height() > geo.bottom():
                y = geo.bottom() - self._toast.height() - 8
            x = max(geo.left() + 8, x)
            y = max(geo.top() + 8, y)

        self._toast.move(x, y)
        self._toast.show()


_manager = TooltipManager()


def attach_tooltip(widget: QWidget, key: str) -> None:
    text = TOOLTIPS.get(key)
    if not text:
        log.warning("Нет текста подсказки для ключа: %s", key)
        return
    _manager.install(widget, text)


def attach_tooltip_text(widget: QWidget, text: str) -> None:
    _manager.install(widget, text)


class InfoIcon(QLabel):
    """Маленькая иконка ⓘ с подсказкой."""

    def __init__(
        self, tooltip_text: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__("ⓘ", parent)
        self.setFixedSize(16, 16)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCursor(Qt.CursorShape.WhatsThisCursor)
        self.setStyleSheet(
            "QLabel {"
            "  color: #888;"
            "  font-size: 13px;"
            "  font-weight: bold;"
            "  border-radius: 8px;"
            "}"
            "QLabel:hover { color: #4a90d9; "
            "background-color: #eaf2fb; }"
        )
        _manager.install(self, tooltip_text)


def make_info_icon(key: str) -> Optional[InfoIcon]:
    text = TOOLTIPS.get(key)
    if not text:
        log.warning("Нет текста подсказки для ключа: %s", key)
        return None
    return InfoIcon(text)


def make_info_icon_text(text: str) -> InfoIcon:
    return InfoIcon(text)


def with_info(
    field: QWidget,
    key: str,
    stretch: bool = True,
    spacing: int = 6,
) -> QWidget:
    container = QWidget()
    h = QHBoxLayout(container)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(spacing)

    if stretch:
        h.addWidget(field, 1)
    else:
        h.addWidget(field)

    icon = make_info_icon(key)
    if icon is not None:
        h.addWidget(icon, 0, Qt.AlignmentFlag.AlignVCenter)

    return container


def with_info_widget(
    field: QWidget,
    tooltip_text: str,
    stretch: bool = True,
    spacing: int = 6,
) -> QWidget:
    container = QWidget()
    h = QHBoxLayout(container)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(spacing)

    if stretch:
        h.addWidget(field, 1)
    else:
        h.addWidget(field)

    icon = make_info_icon_text(tooltip_text)
    h.addWidget(icon, 0, Qt.AlignmentFlag.AlignVCenter)

    return container