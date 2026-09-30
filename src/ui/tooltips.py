"""Единая система подсказок для полей интерфейса.

Изменения:
  • Добавлены подсказки для синхронизации (sync_*),
    включая новые:
      – sync_send_media_to_server;
      – sync_delete_local_media_after_media_upload;
      – sync_use_hash_check.
  • Добавлены подсказки для встроенного плеера:
      – app_media_prefer_builtin_player;
      – app_media_auto_download_from_server;
      – app_media_download_timeout;
      – app_media_player_window_width;
      – app_media_player_window_height.
"""
from __future__ import annotations

from typing import Dict, Optional

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QVBoxLayout, QWidget,
)

from ..logger import get_logger

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
        "переопределить значение вручную."
    ),
    "default_chat_id": (
        "ID чата Bitrix24, куда по умолчанию отправляются "
        "протоколы и summary.\n\n"
        "Приоритеты при отправке:\n"
        "  1) этот ID чата (если задан);\n"
        "  2) чат проекта записи (столбец «Чат Bitrix24» в таблице "
        "проектов выше);\n"
        "  3) пустое поле — пользователь вводит ID вручную.\n\n"
        "Формат: chat2101 или просто 2101."
    ),
    "employees_list": (
        "Справочник сотрудников: ФИО и ID личного чата в Bitrix24."
    ),
    "employees_table": (
        "Список сотрудников. Двойной клик по ячейке — редактирование."
    ),
    "prompts_table": (
        "Библиотека готовых промптов для сервера транскрибации."
    ),
    "default_prompt": (
        "Промпт, который подставляется автоматически, если "
        "пользователь не выбрал другой в окне метаданных."
    ),
    "name_templates": (
        "Шаблоны названий для записей.\n\n"
        "Плейсхолдеры: {name} {abbr} {date} {time} {datetime}."
    ),

    # --- Транскрибация ---
    "tr_url": "Адрес сервера транскрибации.",
    "tr_key": "Access key для авторизации на сервере транскрибации.",
    "tr_connect_timeout": "Таймаут установки TCP-соединения.",
    "tr_read_timeout": "Таймаут ожидания ответа на HTTP-запрос.",
    "tr_max_wait": "Общий лимит ожидания результата транскрибации.",

    # --- Суммаризация ---
    "sum_enabled": (
        "Формировать ли краткое содержание (summary) для новых записей."
    ),
    "sum_provider": "Где формируется протокол/резюме записи.",
    "sum_litellm": "Настройки локального вызова LiteLLM.",
    "sum_litellm_url": "Базовый адрес LiteLLM-прокси.",
    "sum_litellm_key": "API-ключ для LiteLLM.",
    "sum_litellm_model": "Название модели.",
    "sum_litellm_temperature": "Температура сэмплирования (0.0–2.0).",
    "sum_litellm_max_tokens": "Максимум токенов в ответе.",
    "sum_litellm_connect_timeout": "Таймаут соединения с LiteLLM.",
    "sum_litellm_read_timeout": "Таймаут чтения от LiteLLM.",
    "sum_litellm_system": "Системное сообщение для модели.",

    # --- Глоссарий ---
    "glossary_terms": "Список терминов и аббревиатур.",
    "glossary_send_to_summarizer": (
        "Добавлять глоссарий в промпт суммаризации."
    ),
    "glossary_send_to_deepseek": (
        "Добавлять глоссарий в файл deepseek_prompt.*"
    ),

    # --- Запись ---
    "monitor": "Монитор, который будет записан.",
    "mic": "Записывать звук с микрофона.",
    "watermark": "Накладывать на видео водяной знак.",
    "hotkey_start": (
        "Глобальная горячая клавиша для старта/паузы записи."
    ),
    "hotkey_stop": "Глобальная горячая клавиша для остановки записи.",
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

    # --- App: ffmpeg / overlay ---
    "app_ffmpeg_start_check_delay": (
        "Задержка после запуска ffmpeg перед проверкой, что "
        "процесс не упал сразу."
    ),
    "app_ffmpeg_stop_timeout": (
        "Сколько секунд ждать корректного завершения ffmpeg."
    ),
    "app_ffmpeg_kill_timeout": (
        "Сколько секунд ждать после SIGKILL."
    ),
    "app_overlay_hide_delay_ms": (
        "Через сколько миллисекунд бездействия панель скроется."
    ),
    "app_overlay_log_lines": (
        "Сколько последних строк лога показывать на панели."
    ),

    # --- App: встроенный плеер и скачивание медиа ---
    "app_media_prefer_builtin_player": (
        "Использовать встроенный плеер (Qt Multimedia) для просмотра "
        "видео и прослушивания аудио из окна «Записи».\n\n"
        "Если включено — медиа открывается во встроенном окне "
        "прямо из программы. Это устраняет зависимость от системного "
        "VLC/xdg-open, который в snap-окружении может падать с "
        "ошибкой «symbol lookup error».\n\n"
        "Если выключено — файл открывается системным приложением "
        "(двойной клик в файловом менеджере)."
    ),
    "app_media_auto_download_from_server": (
        "Автоматически скачивать медиафайл с сервера, если его нет "
        "локально.\n\n"
        "Если запись опубликована на сервере, но видео/аудио было "
        "передано как артефакт (send_media_to_server) и локальный "
        "файл отсутствует — программа предложит скачать его.\n\n"
        "При выключенной опции будет показан только диалог с "
        "предложением скачать вручную."
    ),
    "app_media_download_timeout": (
        "Максимальное время ожидания скачивания медиафайла с "
        "сервера (в секундах).\n\n"
        "Для больших видеофайлов рекомендуется значение не меньше "
        "600 секунд (10 минут)."
    ),
    "app_media_player_window_width": (
        "Ширина окна встроенного плеера (в пикселях)."
    ),
    "app_media_player_window_height": (
        "Высота окна встроенного плеера (в пикселях)."
    ),

    # --- Logging ---
    "log_path": "Путь к файлу лога приложения.",
    "log_level": "Уровень детализации лога.",
    "log_max_bytes_mb": (
        "Максимальный размер файла лога в МБ перед ротацией."
    ),
    "log_backup_count": "Сколько старых файлов лога хранить.",

    # --- Settings export/import ---
    "settings_export": "Сохранить текущие настройки в файл JSON.",
    "settings_import": (
        "Загрузить настройки из ранее сохранённого файла JSON."
    ),
    "settings_version": (
        "Версия приложения Screen Recorder & Transcriber.\n\n"
        "Клик по номеру версии копирует его в буфер обмена."
    ),

    # --- Метаданные записи ---
    "meta_generate_summary": (
        "Формировать краткое содержание (summary) для этой записи."
    ),
    "meta_project": "Проект, к которому относится запись.",
    "meta_name": "Название записи.",
    "meta_comment": "Свободный комментарий к записи.",
    "meta_prompt": "Промпт для сервера транскрибации.",
    "meta_is_scrum": "Пометить запись как скрам-митинг.",
    "meta_protocol": "Файл протокола предыдущего совещания.",
    "meta_attachments": "Файлы-вложения к записи.",
    "meta_send_to_transcribe": (
        "Передать содержимое вложений на сервер транскрибации."
    ),
    "meta_send_to_deepseek": (
        "Включить содержимое вложений в промпт DeepSeek."
    ),
    "meta_context_to_prompt": "Контекст записи в промпте.",
    "meta_include_name_in_prompt": (
        "Добавлять название записи в промпт."
    ),
    "meta_include_project_in_prompt": "Добавлять проект в промпт.",
    "meta_include_comment_in_prompt": (
        "Добавлять комментарий к записи в промпт."
    ),
    "meta_include_tags_in_prompt": "Добавлять теги записи в промпт.",
    "meta_tags": (
        "Теги записи — произвольные метки для быстрого поиска."
    ),
    "meta_tags_list": (
        "Отметьте один или несколько тегов из справочника."
    ),

    # --- Библиотека ---
    "lib_intro": "Полнотекстовый поиск по всем сохранённым записям.",
    "lib_query": "Поисковый запрос.",
    "lib_search_in": "Где искать совпадения.",
    "lib_project": "Ограничение поиска одним проектом.",
    "lib_date_from": "Начало периода поиска (включительно).",
    "lib_date_to": "Конец периода поиска (включительно).",
    "lib_date_enabled": "Включить фильтр по датам.",
    "lib_fuzzy": "Порог схожести для нечёткого поиска (fuzzy).",
    "lib_results": "Результаты поиска.",
    "lib_preview": "Превью выбранного результата.",
    "lib_open_folder": "Открыть папку записи.",
    "lib_open_file": "Открыть найденный файл.",
    "lib_save_file": "Скопировать найденный файл в любое место.",
    "lib_prompt_enabled": (
        "Включить формирование промпта из результатов поиска."
    ),
    "lib_prompt_before": "Сколько символов до совпадения.",
    "lib_prompt_after": "Сколько символов после совпадения.",
    "lib_prompt_max": "Максимум совпадений в промпте.",
    "lib_prompt_dedup": "Схлопывать пересекающиеся фрагменты.",
    "lib_prompt_instruction": "Инструкция для ИИ.",
    "lib_prompt_save_docx": "Сохранить промпт в .docx.",
    "lib_prompt_save_md": "Сохранить промпт в Markdown.",
    "lib_tag": "Фильтр поиска по тегу записи.",
    "lib_tag_enabled": "Включить фильтр поиска по тегу.",

    # --- App: библиотека ---
    "app_search_default_fuzzy": (
        "Значение по умолчанию для порога нечёткого поиска."
    ),
    "app_search_default_context_chars": (
        "Сколько символов включать в промпт до и после совпадения."
    ),
    "app_search_default_max_prompt_hits": (
        "Максимум совпадений в промпте по умолчанию."
    ),
    "app_fuzzy_max_word_distance": (
        "Максимальное расстояние между словами запроса."
    ),
    "app_search_cancel_wait_ms": (
        "Сколько миллисекунд ждать завершения поиска после отмены."
    ),

    # --- Импорт ---
    "imp_intro": "Импорт готовых материалов в программу.",
    "imp_video": "Видео- или аудиофайл записи.",
    "imp_transcript": "Готовая стенограмма записи.",
    "imp_protocol": "Готовый протокол записи.",
    "imp_date": "Дата, к которой относится запись.",
    "imp_name": "Название записи.",
    "imp_project": "Проект, к которому относится запись.",
    "imp_comment": "Необязательный комментарий к записи.",
    "imp_tags": "Теги импортируемой записи.",
    "imp_tags_list": (
        "Отметьте один или несколько тегов из справочника."
    ),

    # --- ВМ Yandex ---
    "yandex_vm_root": (
        "Корневая папка с конфигурациями виртуальных машин "
        "Yandex Cloud."
    ),
    "yandex_vm_list": (
        "Список виртуальных машин — подпапки корневой папки."
    ),
    "yandex_vm_schedule": "Файл расписания работы ВМ (schedule.cron).",
    "yandex_vm_exceptions": "Файл исключений (exceptions.txt).",
    "yandex_vm_editor": (
        "Редактор файла конфигурации ВМ. Изменения на диск "
        "записываются только по кнопке «Сохранить»."
    ),

    # --- Bitrix24 ---
    "bitrix_enabled": "Включить интеграцию с Bitrix24.",
    "bitrix_webhook": "URL входящего вебхука Bitrix24.",
    "bitrix_connect_timeout": (
        "Таймаут установки TCP-соединения с порталом Bitrix24."
    ),
    "bitrix_read_timeout": "Таймаут ожидания ответа.",
    "bitrix_default_send": "Что отправляется по умолчанию.",
    "bitrix_header": "Добавлять заголовок в сообщение.",
    "bitrix_system": "Отправлять как системное сообщение.",
    "bitrix_no_preview": "Отключить предпросмотр ссылок.",
    "bitrix_files_section": "Настройки отправки файлов в Bitrix24.",
    "bitrix_file_threshold": (
        "Порог, после которого текст отправляется файлом."
    ),
    "bitrix_upload_folder": (
        "Папка на Диске Bitrix24, куда загружать файлы."
    ),
    "bitrix_max_message_chars": (
        "Максимальная длина одного сообщения Bitrix24."
    ),
    "bitrix_retry_count": "Сколько раз пытаться повторить отправку.",
    "bitrix_retry_delay": "Пауза между попытками повтора.",
    "bitrix_chat_id": "ID чата Bitrix24 вручную.",
    "bitrix_recipients_list": (
        "Список получателей для массовой рассылки."
    ),
    "bitrix_recipient_type": "Кому отправить сообщение.",
    "bitrix_recipient": "Выбор конкретного получателя.",

    # --- Очередь ---
    "auto_retry": "Автоматически повторять задачи с ошибкой.",
    "retry_interval": (
        "Как часто проверять очередь на наличие error-задач."
    ),
    "max_retries": "Сколько раз пытаться повторить одну задачу.",
    "queue_pause_when_recording": (
        "Не обрабатывать очередь задач, пока идёт запись экрана."
    ),

    # --- Скрам ---
    "scrum_template": "Шаблон промпта для DeepSeek.",
    "scrum_format": (
        "Формат, в котором сохраняется готовый промпт DeepSeek."
    ),

    # --- Форматы и сжатие ---
    "audio_format": "Формат аудио, в который конвертируется видео.",
    "audio_bitrate": "Битрейт аудио при конвертации.",
    "video_bitrate": "Битрейт видео при записи экрана.",
    "compression_level": "Уровень сжатия видео (0–9).",

    # --- Хранилище ---
    "temp_path": (
        "Корневая папка для хранения записей и очереди задач."
    ),
    "retention_hours": (
        "Сколько часов хранить завершённые записи."
    ),

    # --- Синхронизация с сервером ---
    "sync_enabled": (
        "Включить синхронизацию записей с удалённым сервером.\n\n"
        "Сервер — это screc-server (см. DOCS.md). Он хранит\n"
        "метаданные, текстовые артефакты и (опционально) медиа."
    ),
    "sync_base_url": (
        "Базовый URL сервера синхронизации.\n\n"
        "Примеры:\n"
        "  • http://localhost:8000 — локальный сервер;\n"
        "  • https://sync.company.ru — продовый.\n\n"
        "Вводите БЕЗ завершающего слэша."
    ),
    "sync_api_key": (
        "API-ключ (X-API-Key) для авторизации на сервере.\n\n"
        "Выдаётся администратором сервера. Хранится\n"
        "в зашифрованном виде в config.json (Fernet)."
    ),
    "sync_connect_timeout": (
        "Сколько секунд ждать установки TCP-соединения\n"
        "с сервером синхронизации."
    ),
    "sync_read_timeout": (
        "Сколько секунд ждать ответа на HTTP-запрос.\n\n"
        "Для больших артефактов (видео) рекомендуется\n"
        "значение не меньше 300 секунд."
    ),
    "sync_auto_upload_after_processing": (
        "Автоматически публиковать запись на сервер после\n"
        "успешного завершения обработки."
    ),
    "sync_auto_pull_enabled": (
        "Автоматически подтягивать изменения с сервера\n"
        "в фоне (дельта-синхронизация через /sync/changes)."
    ),
    "sync_auto_pull_interval": (
        "Как часто (в секундах) проверять сервер на наличие\n"
        "изменений.\n\n"
        "По умолчанию 300 секунд (5 минут)."
    ),
    "sync_page_size": (
        "Размер страницы changelog при дельта-синхронизации."
    ),
    "sync_max_artifact_mb": (
        "Максимальный размер одного артефакта в мегабайтах.\n\n"
        "Применяется и к текстовым артефактам, и к видео/аудио "
        "(если они передаются).\n\n"
        "Должен совпадать с SCREC_MAX_ARTIFACT_MB на сервере.\n"
        "Если задать больше — сервер вернёт ошибку 413."
    ),
    "sync_send_video_link": (
        "Передавать на сервер ссылку на видео (file://…).\n\n"
        "Сервер хранит ссылку в метаданных записи."
    ),
    "sync_allow_delete_local_media_after_upload": (
        "Удалять локальные аудио/видео после публикации\n"
        "(старое поведение, без учёта загрузки медиа)."
    ),
    "sync_send_media_to_server": (
        "Передавать видео и аудио на сервер как артефакты.\n\n"
        "Если включено — при публикации записи локальные "
        "медиафайлы (video.mp4, video.mp3 и т.п.) уходят на\n"
        "сервер как артефакты kind=video и kind=audio.\n\n"
        "При скачивании записи на другом устройстве они\n"
        "вернутся как video.<ext> и будут доступны локально.\n\n"
        "Ограничения:\n"
        "  • Размер каждого файла не больше «Макс. размер "
        "артефакта» и SCREC_MAX_ARTIFACT_MB на сервере.\n"
        "  • Загрузка большого видео может занять минуты.\n"
        "  • Файл хранится и локально, и на сервере, если\n"
        "    не включено удаление после загрузки."
    ),
    "sync_delete_local_media_after_media_upload": (
        "Удалять локальные видео/аудио после успешной\n"
        "загрузки на сервер.\n\n"
        "Работает только если включена передача медиа\n"
        "на сервер.\n\n"
        "ВНИМАНИЕ: после удаления ссылка file:// станет\n"
        "неактуальной, но файл можно будет скачать с сервера."
    ),
    "sync_use_hash_check": (
        "Использовать условную загрузку перед отправкой файла.\n\n"
        "Как это работает:\n"
        "  1. Клиент считает SHA-256 файла.\n"
        "  2. Спрашивает сервер через HEAD или POST /check,\n"
        "     нужно ли грузить файл.\n"
        "  3. Если файл уже есть с таким же хэшем — содержимое\n"
        "     НЕ отправляется по сети (экономия трафика).\n"
        "  4. Если файла нет или хэш разошёлся — файл\n"
        "     отправляется как обычно.\n\n"
        "На практике это избавляет от повторной передачи\n"
        "больших видеофайлов (сотни МБ) при повторной\n"
        "публикации записи.\n\n"
        "Требует сервер версии 1.1.0+ (эндпоинты HEAD\n"
        "/records/{id}/artifacts/{filename} и POST\n"
        "/records/{id}/artifacts/check).\n\n"
        "Если сервер старый — проверка просто не сработает,\n"
        "файл уйдёт по сети как обычно, ошибки не будет."
    ),
    "sync_sync_projects_and_tags": (
        "Синхронизировать справочник проектов и тегов."
    ),
    "sync_retry_count": (
        "Сколько раз повторять запрос при сетевой ошибке."
    ),
    "sync_retry_delay": (
        "Пауза между попытками (в секундах)."
    ),
    "sync_test_connection": (
        "Проверить подключение: запросить /health и /api/v1/tree."
    ),
    "sync_manual_record_id": (
        "Record ID на сервере (UUID).\n\n"
        "Можно посмотреть в окне «Синхронизация» на вкладке "
        "«С сервера» в таблице."
    ),
    "sync_auto_pull": (
        "Включить/выключить фоновое подтягивание изменений."
    ),
    "sync_status": (
        "Текущий статус подключения к серверу синхронизации."
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