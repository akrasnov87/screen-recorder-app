"""Единая система подсказок для полей интерфейса.

Режимы:
  • Короткая подсказка (< TOAST_THRESHOLD) — стандартный QToolTip.
  • Длинная подсказка (>= TOAST_THRESHOLD) — собственный TooltipToast.

Подсказку можно повесить:
  • на сам виджет — attach_tooltip(widget, key);
  • на иконку ⓘ — make_info_icon(key).

Изменения:
  • Добавлены подсказки для новых параметров config["app"]:
    ffmpeg_start_check_delay, ffmpeg_stop_timeout, ffmpeg_kill_timeout,
    overlay_hide_delay_ms, overlay_log_lines,
    log_max_bytes_mb, log_backup_count,
    bitrix_max_message_chars, bitrix_retry_count, bitrix_retry_delay,
    queue_pause_when_recording,
    search_default_fuzzy/context_chars/max_prompt_hits,
    fuzzy_max_word_distance, search_cancel_wait_ms.
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
        "  • Чат Bitrix24 — ID чата для отправки протоколов и summary. "
        "Используется кнопками в меню «Bitrix24» окна «Записи».\n\n"
        "Можно добавлять, удалять и менять порядок проектов.\n"
        "Порядок влияет на то, какой проект выбран по умолчанию "
        "при старте записи (первый в списке)."
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
        "Выдаётся администратором сервера. Хранится в открытом виде "
        "в config.json — при необходимости используйте отдельный "
        "сервисный ключ."
    ),
    "tr_connect_timeout": (
        "Сколько секунд ждать установки TCP-соединения с сервером.\n\n"
        "Если сервер не отвечает за это время — попытка считается "
        "проваленной. Для локальной сети достаточно 5–15 секунд, "
        "для внешней — 15–30."
    ),
    "tr_read_timeout": (
        "Сколько секунд ждать ответа на каждый отдельный "
        "HTTP-запрос.\n\n"
        "Не путать с общим временем ожидания транскрибации "
        "(параметр «Максимум ожидания»).\n\n"
        "Для медленных серверов можно увеличить до 300."
    ),
    "tr_max_wait": (
        "Общий лимит ожидания результата транскрибации.\n\n"
        "Даже если сервер отвечает на опросы статуса, но задача "
        "не завершается — по истечении этого времени ожидание "
        "прекращается, задача помечается как error.\n\n"
        "По умолчанию 7200 секунд (2 часа)."
    ),

    # --- Суммаризация ---
    "sum_enabled": (
        "Формировать ли краткое содержание (summary) для новых "
        "записей.\n\n"
        "Это значение по умолчанию. В карточке каждой записи есть "
        "свой чекбокс «Формировать summary», который может "
        "переопределить глобальную настройку.\n\n"
        "По умолчанию отключено — summary не формируется."
    ),
    "sum_provider": (
        "Где формируется протокол/резюме записи.\n\n"
        "• «Сервер транскрибации» — промпт уходит на сервер вместе "
        "с медиафайлом, сервер сам формирует расшифровку и резюме.\n\n"
        "• «LiteLLM» — на сервере выполняется только расшифровка "
        "(Whisper), а резюме формируется локально в клиенте через "
        "OpenAI-совместимый API LiteLLM."
    ),
    "sum_litellm": (
        "Настройки локального вызова LiteLLM (или другого "
        "OpenAI-совместимого API)."
    ),
    "sum_litellm_url": (
        "Базовый адрес LiteLLM-прокси.\n\n"
        "Например: http://localhost:4000 или "
        "http://litellm.company:4000.\n\n"
        "Путь /v1/chat/completions или /v1/models добавляется "
        "автоматически."
    ),
    "sum_litellm_key": (
        "API-ключ для LiteLLM.\n\n"
        "Если прокси не требует авторизации — оставьте пустым."
    ),
    "sum_litellm_model": (
        "Название модели, зарегистрированное в LiteLLM.\n\n"
        "Например: gpt-4o-mini, claude-3-5-sonnet, llama3.1:70b."
    ),
    "sum_litellm_temperature": (
        "Температура сэмплирования (0.0–2.0).\n\n"
        "Для протоколов и других структурированных документов "
        "рекомендуется 0.1–0.3."
    ),
    "sum_litellm_max_tokens": (
        "Максимальная длина ответа модели в токенах.\n\n"
        "2200 токенов ≈ 1500–1700 слов."
    ),
    "sum_litellm_connect_timeout": (
        "Сколько секунд ждать установки соединения с LiteLLM."
    ),
    "sum_litellm_read_timeout": (
        "Сколько секунд ждать ответа от LiteLLM.\n\n"
        "Большие модели могут генерировать ответ десятки секунд — "
        "по умолчанию 300."
    ),
    "sum_litellm_system": (
        "Системное сообщение для модели.\n\n"
        "Задаёт общий стиль ответа: язык, отсутствие вступлений/"
        "заключений, формат."
    ),

    # --- Глоссарий ---
    "glossary_terms": (
        "Список терминов и аббревиатур, которые будут переданы ИИ.\n\n"
        "Каждая запись — пара «Термин» — «Пояснение»."
    ),
    "glossary_send_to_summarizer": (
        "Добавлять глоссарий в промпт суммаризации.\n\n"
        "Перед отправкой на сервер транскрибации или в LiteLLM "
        "к промпту дописывается блок «ГЛОССАРИЙ»."
    ),
    "glossary_send_to_deepseek": (
        "Добавлять глоссарий в файл deepseek_prompt.*\n\n"
        "В файл добавляется отдельный блок «ГЛОССАРИЙ» перед "
        "стенограммой."
    ),

    # --- Запись ---
    "monitor": (
        "Монитор, который будет записан.\n\n"
        "Список формируется автоматически через xrandr.\n\n"
        "Для Wayland запись идёт через kmsgrab — реально "
        "записывается весь DRM-узел, а выбор влияет только на "
        "область обрезки."
    ),
    "mic": (
        "Записывать звук с микрофона (default source PulseAudio).\n\n"
        "Если снять галочку — будет записано только видео без звука."
    ),
    "watermark": (
        "Накладывать на видео водяной знак с названием записи "
        "и датой/временем.\n\n"
        "Работает только в X11-режиме (libx264 + drawtext)."
    ),
    "hotkey_start": (
        "Глобальная горячая клавиша для старта / паузы / "
        "возобновления записи.\n\n"
        "Формат: Ctrl+Shift+R, Alt+F9 и т.п.\n\n"
        "Если запись идёт — клавиша ставит на паузу. "
        "Если на паузе — возобновляет. Если записи нет — начинает."
    ),
    "hotkey_stop": (
        "Глобальная горячая клавиша для остановки записи.\n\n"
        "Формат: Ctrl+Shift+S, Alt+F10 и т.п.\n\n"
        "Работает только когда запись активна."
    ),
    "metadata_on_start": (
        "Показывать окно ввода метаданных при старте записи.\n\n"
        "Если выключено — запись начнётся сразу с метаданными "
        "по умолчанию."
    ),
    "metadata_on_stop": (
        "Показывать окно метаданных повторно при остановке записи.\n\n"
        "Позволяет уточнить/исправить данные после завершения."
    ),
    "overlay_panel": (
        "Показывать плавающую панель управления поверх всех окон.\n\n"
        "На панели есть кнопки Старт/Пауза/Стоп, прогресс-бар "
        "и лог."
    ),
    "start_notification": (
        "Показывать системное уведомление о начале записи."
    ),

    # --- App: ffmpeg ---
    "app_ffmpeg_start_check_delay": (
        "Задержка (в секундах) после запуска ffmpeg перед проверкой, "
        "что процесс не упал сразу.\n\n"
        "Слишком маленькое значение может пропустить мгновенный "
        "краш — программа будет считать, что запись идёт, а на "
        "деле файл не создаётся.\n\n"
        "Слишком большое — замедлит старт записи.\n\n"
        "По умолчанию 0.3 сек — оптимально для большинства систем."
    ),
    "app_ffmpeg_stop_timeout": (
        "Сколько секунд ждать корректного завершения ffmpeg после "
        "сигнала остановки (SIGINT).\n\n"
        "Если процесс не успел завершиться — он будет убит "
        "принудительно (SIGKILL), что может привести к "
        "повреждению последних секунд видео.\n\n"
        "Для медленных дисков и больших файлов можно увеличить "
        "до 20–30 секунд.\n\n"
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
        "автоматически скроется.\n\n"
        "0 — панель не будет скрываться автоматически.\n\n"
        "По умолчанию 5000 мс (5 секунд)."
    ),
    "app_overlay_log_lines": (
        "Сколько последних строк лога показывать на плавающей "
        "панели.\n\n"
        "По умолчанию 5 строк."
    ),

    # --- Logging ---
    "log_path": (
        "Путь к файлу лога приложения.\n\n"
        "Изменение пути вступит в силу только после перезапуска "
        "приложения."
    ),
    "log_level": (
        "Уровень детализации лога.\n\n"
        "DEBUG — максимум информации (рекомендуется при отладке);\n"
        "INFO — обычная работа;\n"
        "WARNING — только предупреждения и ошибки;\n"
        "ERROR — только ошибки;\n"
        "CRITICAL — только критические сбои.\n\n"
        "Изменение вступит в силу после перезапуска."
    ),
    "log_max_bytes_mb": (
        "Максимальный размер файла лога в мегабайтах, после "
        "которого он ротируется.\n\n"
        "Старые файлы сохраняются как app.log.1, app.log.2 и т.д.\n\n"
        "Изменение вступит в силу только после перезапуска "
        "приложения.\n\n"
        "По умолчанию 10 МБ."
    ),
    "log_backup_count": (
        "Сколько старых файлов лога хранить.\n\n"
        "Файлы старше указанного количества будут автоматически "
        "удаляться при ротации.\n\n"
        "Изменение вступит в силу только после перезапуска "
        "приложения.\n\n"
        "По умолчанию 5 архивов."
    ),

    # --- Settings export/import ---
    "settings_export": (
        "Сохранить текущие настройки в файл JSON.\n\n"
        "Полезно для резервной копии и переноса на другую машину.\n\n"
        "<b>Внимание:</b> файл содержит чувствительные данные — "
        "webhook Bitrix24, access key транскрибации, API-ключ "
        "LiteLLM. Храните его в безопасном месте."
    ),
    "settings_import": (
        "Загрузить настройки из ранее сохранённого файла JSON.\n\n"
        "Перед импортом создаётся резервная копия "
        "<code>config.json.&lt;timestamp&gt;.bak</code>.\n\n"
        "После импорта рекомендуется перезапустить приложение."
    ),

    # --- Метаданные записи ---
    "meta_generate_summary": (
        "Формировать краткое содержание (summary) для этой записи.\n\n"
        "Переопределяет глобальную настройку "
        "(Настройки → Суммаризация).\n\n"
        "По умолчанию флаг снят — summary не формируется."
    ),
    "meta_project": (
        "Проект, к которому относится запись.\n\n"
        "Список проектов настраивается в Настройки → "
        "Проекты и чаты Bitrix24."
    ),
    "meta_name": (
        "Название записи.\n\n"
        "Можно ввести вручную или выбрать шаблон из выпадающего "
        "списка.\n\n"
        "Плейсхолдеры: {name} / {название}, {abbr} / {сокр}, "
        "{date} / {дата}, {time} / {время}, {datetime}."
    ),
    "meta_comment": (
        "Свободный комментарий к записи.\n\n"
        "Сохраняется в session.json. Не отправляется на сервер "
        "транскрибации и не попадает в промпт DeepSeek "
        "автоматически."
    ),
    "meta_prompt": (
        "Промпт для сервера транскрибации.\n\n"
        "Сервер применяет его к расшифровке.\n\n"
        "Можно выбрать из библиотеки или отредактировать вручную."
    ),
    "meta_is_scrum": (
        "Пометить запись как скрам-митинг.\n\n"
        "Если включено — после транскрибации автоматически "
        "собирается файл deepseek_prompt.* с готовым промптом "
        "для DeepSeek."
    ),
    "meta_protocol": (
        "Файл протокола предыдущего совещания.\n\n"
        "Будет добавлен в промпт DeepSeek отдельным блоком.\n\n"
        "Поддерживаются .docx, .txt, .md."
    ),
    "meta_attachments": (
        "Файлы-вложения к записи.\n\n"
        "Можно передать их текст на сервер транскрибации "
        "и/или включить в промпт DeepSeek."
    ),
    "meta_send_to_transcribe": (
        "Передать содержимое вложений на сервер транскрибации.\n\n"
        "Текст вложений будет дописан в конец промпта транскрибации."
    ),
    "meta_send_to_deepseek": (
        "Включить содержимое вложений в промпт DeepSeek.\n\n"
        "Текст вложений будет добавлен в файл deepseek_prompt.* "
        "отдельным блоком «ВЛОЖЕНИЯ»."
    ),
    "meta_context_to_prompt": (
        "Контекст записи в промпте.\n\n"
        "Позволяет передать ИИ информацию из карточки записи — "
        "название, проект и комментарий."
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

    # --- Библиотека ---
    "lib_intro": (
        "Полнотекстовый поиск по всем сохранённым записям.\n\n"
        "Ищет по файлам в папке sessions/ — без базы данных."
    ),
    "lib_query": (
        "Поисковый запрос.\n\n"
        "Можно вводить одно слово, фразу или несколько слов."
    ),
    "lib_search_in": (
        "Где искать совпадения.\n\n"
        "  • «Стенограммах» — video.txt;\n"
        "  • «Протоколах» — protocol.*, deepseek_prompt.*;\n"
        "  • «Summary» — session.json → summary_bb;\n"
        "  • «Вложениях» — attachments/*."
    ),
    "lib_project": (
        "Ограничение поиска одним проектом.\n\n"
        "Значение «— все —» отключает фильтр."
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
        "Порог схожести для нечёткого поиска (fuzzy).\n\n"
        "82% — рекомендуемое значение."
    ),
    "lib_results": (
        "Результаты поиска.\n\n"
        "Правый клик по строке открывает контекстное меню."
    ),
    "lib_preview": (
        "Превью выбранного результата."
    ),
    "lib_open_folder": (
        "Открыть папку записи в системном файловом менеджере."
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
        "Сколько символов включать в промпт ПЕРЕД найденным "
        "совпадением."
    ),
    "lib_prompt_after": (
        "Сколько символов включать в промпт ПОСЛЕ найденного "
        "совпадения."
    ),
    "lib_prompt_max": (
        "Максимальное количество совпадений, попадающих в промпт."
    ),
    "lib_prompt_dedup": (
        "Схлопывать пересекающиеся фрагменты."
    ),
    "lib_prompt_instruction": (
        "Инструкция для ИИ — что делать с найденными фрагментами."
    ),
    "lib_prompt_save_docx": (
        "Сохранить сформированный промпт в документ .docx."
    ),
    "lib_prompt_save_md": (
        "Сохранить сформированный промпт в формате Markdown."
    ),

    # --- App: библиотека (поиск) ---
    "app_search_default_fuzzy": (
        "Значение по умолчанию для порога нечёткого поиска "
        "в «Библиотеке».\n\n"
        "Можно менять в диалоге поиска для каждого запроса. "
        "82 % — рекомендуемое значение."
    ),
    "app_search_default_context_chars": (
        "Сколько символов включать в промпт до и после найденного "
        "совпадения по умолчанию.\n\n"
        "Можно менять в диалоге «Библиотека» для каждого поиска."
    ),
    "app_search_default_max_prompt_hits": (
        "Максимум совпадений в промпте по умолчанию.\n\n"
        "Можно менять в диалоге «Библиотека» для каждого поиска."
    ),
    "app_fuzzy_max_word_distance": (
        "Максимальное расстояние (в символах) между словами "
        "запроса в тексте, при котором они считаются одним "
        "совпадением.\n\n"
        "Большее значение — слова могут быть в разных абзацах.\n\n"
        "По умолчанию 200 символов."
    ),
    "app_search_cancel_wait_ms": (
        "Сколько миллисекунд ждать завершения поиска после "
        "запроса отмены, прежде чем вернуть управление.\n\n"
        "Если поиск завис на медленном файле — можно уменьшить "
        "значение, чтобы окно реагировало быстрее."
    ),

    # --- Импорт ---
    "imp_intro": (
        "Импорт готовых материалов в программу."
    ),
    "imp_video": (
        "Видео- или аудиофайл записи."
    ),
    "imp_transcript": (
        "Готовая стенограмма записи."
    ),
    "imp_protocol": (
        "Готовый протокол записи."
    ),
    "imp_date": (
        "Дата, к которой относится запись."
    ),
    "imp_name": (
        "Название записи."
    ),
    "imp_project": (
        "Проект, к которому относится запись."
    ),
    "imp_comment": (
        "Необязательный комментарий к записи."
    ),

    # --- Bitrix24 ---
    "bitrix_enabled": (
        "Включить интеграцию с Bitrix24.\n\n"
        "После включения в окне «Записи» появится меню «Bitrix24»."
    ),
    "bitrix_webhook": (
        "URL входящего вебхука Bitrix24.\n\n"
        "Создаётся в Bitrix24: Приложения → Разработчикам → Вебхуки."
    ),
    "bitrix_connect_timeout": (
        "Таймаут установки TCP-соединения с порталом Bitrix24."
    ),
    "bitrix_read_timeout": (
        "Таймаут ожидания ответа на HTTP-запрос."
    ),
    "bitrix_default_send": (
        "Что предлагается отправить по умолчанию при открытии "
        "диалога отправки."
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
        "Порог, после которого текст автоматически отправляется "
        "файлом.\n\n"
        "Работает в режиме «Автоматически»."
    ),
    "bitrix_upload_folder": (
        "Папка на Диске Bitrix24, куда загружать файлы.\n\n"
        "0 (по умолчанию) — «Папка чата (авто)»."
    ),
    "bitrix_max_message_chars": (
        "Максимальная длина одного сообщения Bitrix24.\n\n"
        "Реальный лимит портала ~20 000 символов. По умолчанию "
        "15 000 — оставляем запас, чтобы избежать отказов "
        "на длинных протоколах.\n\n"
        "Если протокол длиннее — он будет автоматически обрезан "
        "с пометкой в конце."
    ),
    "bitrix_retry_count": (
        "Сколько раз пытаться повторить отправку в Bitrix24 "
        "при сетевой ошибке или 5xx-ответе портала.\n\n"
        "Каждая попытка выполняется с паузой, заданной в поле "
        "«Пауза между повторами».\n\n"
        "0 — отключить повторы."
    ),
    "bitrix_retry_delay": (
        "Пауза в секундах между попытками повтора отправки.\n\n"
        "Рекомендуется 2–5 секунд для нестабильных каналов."
    ),
    "bitrix_chat_id": (
        "ID чата Bitrix24 вручную.\n\n"
        "Используется только в режиме «Вручную».\n\n"
        "Формат: chat2101 или 2101, либо ID пользователя (123)."
    ),
    "bitrix_recipients_list": (
        "Список получателей для массовой рассылки.\n\n"
        "Галочками можно отметить сразу несколько чатов — "
        "протокол и/или summary уйдут каждому из них.\n\n"
        "Если у проекта/сотрудника не задан ID чата, элемент "
        "показан серым и его нельзя отметить."
    ),
    "bitrix_recipient_type": (
        "Кому отправить сообщение (устаревший параметр)."
    ),
    "bitrix_recipient": (
        "Выбор конкретного получателя из справочника "
        "(устаревший параметр)."
    ),

    # --- Очередь ---
    "auto_retry": (
        "Автоматически повторять задачи, завершившиеся с ошибкой."
    ),
    "retry_interval": (
        "Как часто проверять очередь на наличие error-задач."
    ),
    "max_retries": (
        "Сколько раз пытаться повторить одну задачу."
    ),
    "queue_pause_when_recording": (
        "Не обрабатывать очередь задач, пока идёт запись экрана.\n\n"
        "Если включено — воркер обработки будет ждать завершения "
        "записи, прежде чем брать следующую задачу. Это снижает "
        "нагрузку на CPU во время записи.\n\n"
        "Если выключено — обработка и запись могут идти "
        "одновременно, что может привести к пропуску кадров на "
        "слабых машинах."
    ),

    # --- Скрам ---
    "scrum_template": (
        "Шаблон промпта для DeepSeek, который будет подставлен "
        "в начало файла deepseek_prompt.*."
    ),
    "scrum_format": (
        "Формат, в котором сохраняется готовый промпт DeepSeek.\n\n"
        "docx — для Word;\n"
        "md — для Markdown-редакторов;\n"
        "txt — универсальный."
    ),

    # --- Форматы и сжатие ---
    "audio_format": (
        "Формат аудио, в который конвертируется видео перед "
        "отправкой на транскрибацию."
    ),
    "audio_bitrate": (
        "Битрейт аудио при конвертации.\n\n"
        "192 kbps — оптимально для речи."
    ),
    "video_bitrate": (
        "Битрейт видео при записи экрана.\n\n"
        "4000 kbps — Full HD, 8000–15000 kbps — 4K."
    ),
    "compression_level": (
        "Уровень сжатия видео (0–9).\n\n"
        "Для real-time записи рекомендуется 4–6."
    ),

    # --- Хранилище ---
    "temp_path": (
        "Корневая папка для хранения записей и очереди задач.\n\n"
        "По умолчанию /tmp/screen-recorder."
    ),
    "retention_hours": (
        "Сколько часов хранить завершённые записи.\n\n"
        "ВНИМАНИЕ: автоочистка пока не реализована."
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
        log.debug("TooltipManager: QWidget-ы созданы лениво")
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
        log.debug("Показ toast-подсказки: %d символов", len(text))
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