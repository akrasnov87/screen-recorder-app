# AGENT.md — правила работы над Screen Recorder & Transcriber

Документ для ИИ-агентов и разработчиков, которые правят этот репозиторий.
Описывает, что за проект, как он устроен и как именно менять код, чтобы изменения
не ломали архитектуру и совпадали со стилем кодовой базы.

Версия проекта — **единственный источник `src/__init__.py`** (`__version__`, сейчас `1.13.16`).
`README.md` и `BUILD.md` могут отставать от неё.

---

## 1. Что это за приложение

Desktop-приложение на **PySide6** (Ubuntu + Windows) для пост-обработки видео и документов:

- приём готовых видео/аудио: загрузка с диска, импорт материалов, запись-документ из файлов
  (Word, PDF, TXT, MD, CSV, JSON, XLSX, PPTX);
- конвертация в аудио через `ffmpeg` и транскрибация через внешний Whisper-совместимый сервис;
- суммаризация через сервер транскрибации или LiteLLM (OpenAI-совместимый API);
- промпты DeepSeek / скрам, протоколы, поручения (action items);
- локальная «библиотека» с полнотекстовым и fuzzy-поиском, сводки за день;
- интеграции: Bitrix24 (Диск, чаты, сотрудники, BB-код), TFS / Azure DevOps Server;
- синхронизация с `screc-server` (`screc_client.py` + `sync_manager.py`).

**Записи экрана больше нет.** Механизм `ScreenRecorder` / `GlobalHotkeyManager` / `OverlayPanel`
и управление ВМ Yandex **удалены**. Приложение — обработчик готовых файлов, а не рекордер.
Исторические имена (`VideoProcessor`, `TaskQueue`, название проекта) не переименовывать.

---

## 2. Структура репозитория

```
run.py                  # точка входа: корень проекта, проверка src/, вызов src.main
src/
  __init__.py           # __version__ — единый источник версии
  main.py               # QApplication, контроллер, asyncio-поток, склейка окон
  platform_utils.py     # ОС, пути (temp/config/log), имена ffmpeg/ffprobe — листовой модуль
  logger.py             # логирование + GUI-хук, faulthandler
  config_manager.py     # config.json + шифрование секретов (Fernet, keyring)
  task_queue.py         # потокобезопасная очередь задач (JSON)
  processor.py          # конвейер: конвертация → транскрибация → summary → промпт
  transcribe_client.py  # Whisper API (aiohttp)
  litellm_client.py     # OpenAI-совместимый API
  screc_client.py       # HTTP-клиент к screc-server
  sync_manager.py       # бизнес-логика синхронизации
  bitrix_client.py      # Bitrix24 (2-этапная загрузка на Диск)
  bitrix_employees_sender.py, employees_matching.py
  tfs_client.py, tfs_attachment_builder.py
  library_search.py     # поиск + сборка промпта из результатов
  daily_digest.py, relations_manager.py, tasks_manager.py, tasks_json_parser.py
  protocol_summary_helper.py, protocol_action_items_helper.py, action_items_prompt_builder.py
  file_readers.py       # единый ридер .txt/.md/.docx/.pdf/.json/.xlsx/.pptx
  markdown_docx.py      # MD → DOCX
  markdown_to_bitrix.py # MD → plain + BB-код
  docx_to_html.py, bbcode_editor.py
  video_compressor.py   # автосжатие медиа перед публикацией
  utils.py              # sanitize_filename, safe_local_path, generate_task_id, ...
  ui/                   # ~25 окон и диалогов + tooltips.py
resources/              # icons/, styles.qss
packaging/
  DEBIAN/               # control, postinst, prerm, postrm
  usr/                  # bin/screen-recorder, share/applications, share/screen-recorder
  windows/              # PyInstaller spec, Inno Setup, build_win.ps1
build_deb.sh            # сборка .deb, версия берётся из src/__init__.py
requirements.txt
BUILD.md, README.md, CRITICAL.md, SKILS.md, CHANGELOG.md
```

### Слои

- **UI** (`src/ui/*`) — только Qt-виджеты, сигналы/слоты. Никакой сетевой или файловой бизнес-логики.
- **Бизнес-логика** (`processor`, `sync_manager`, `tasks_manager`, `relations_manager`,
  `library_search`, `daily_digest`) — не импортирует `ui/`, общается через сигналы и колбэки.
- **Инфраструктура** (`file_readers`, `markdown_*`, `utils`, `platform_utils`, `logger`,
  `config_manager`) — переиспользуется везде, не должна тянуть UI.
- **Клиенты** (`*_client.py`) — только транспорт, ошибки в виде собственных исключений.

Зависимости идут сверху вниз: `ui → бизнес-логика → инфраструктура → клиенты`.
Обратный импорт — ошибка.

---

## 3. Жёсткие правила кода

### 3.1. Подсказки (обязательно)

Из `src/SKILS.md` — правило №1 в проекте:

- **любое новое поле в UI обязано иметь подсказку** (tooltip);
- использовать существующую систему `src/ui/tooltips.py`:
  `attach_tooltip(widget, key)` — по ключу из реестра, `attach_tooltip_text(widget, text)` —
  произвольный текст (длинные тексты автоматически показываются как toast);
- не писать `widget.setToolTip(...)` напрямую в новом коде — только через хелперы `tooltips.py`;
- текст подсказки — по-русски, объясняет «что делает» и «зачем», а не пересказывает подпись.

### 3.2. Константы → в настройки

- любая новая константа (таймаут, интервал, лимит, путь, порог) выносится в конфиг
  через `ConfigManager.get_defaults()` в нужной секции (`app`, `transcribe`, `sync`, ...)
  и получает подсказку в окне настроек;
- в коде допустим только fallback при чтении: `config.get("app", {}).get("key", 30)`;
- хардкод магических чисел в логике — не принимать.

### 3.3. Кроссплатформенность

- никаких `/tmp/...`, `C:\...`, `xrandr`, `dpkg` в общем коде — только через `platform_utils`
  (`default_temp_dir()`, `default_config_path()`, `ffmpeg_binary_name()`, `ffprobe_binary_name()`);
- ветвление по ОС — через `IS_LINUX` / `IS_WINDOWS` / `IS_MACOS`, а не по строкам `sys.platform`;
- `platform_utils.py` **не должен** импортировать `logger` или другие модули `src`
  (циклический импорт) — держать его «листовым».

### 3.4. Асинхронность и потоки

- блокирующие операции (ffmpeg, чтение/запись больших файлов, sync-вызовы) — только через
  `await asyncio.to_thread(...)`;
- сеть — `aiohttp`, таймауты из конфига;
- отмена — через `asyncio.Event` / `cancel_event` + `asyncio.wait_for`, как в `processor.process_video`;
- **не оставлять фоновые корутины живыми при выходе**: `main._quit()` сначала отменяет
  sync-puller и processing task, ждёт их завершения, и только потом останавливает loop.
  Любая новая фоновая задача должна быть там зарегистрирована (иначе снова segfault);
- сигналы Qt из asyncio-потока — через существующий паттерн `QObject` + `Signal`.

### 3.5. Файлы и имена

- имена файлов перед записью на диск и отправкой на сервер — только через
  `utils.sanitize_filename` (длинные и URL-encoded имена давали 500 на сервере);
- пути в новом коде — `pathlib.Path` (старый код местами на `os.path` — не переписывать массово);
- локальные данные пользователя не удалять без явного запроса.

### 3.6. Секреты

- API-ключи, токены, пароли — только через `ConfigManager` (шифруются Fernet, мастер-ключ
  в keyring с файловым fallback с правами `0600`);
- никогда не писать секреты в логи, в `config.json` открытым текстом, в тексты ошибок;
- не логировать тела запросов к Bitrix/TFS/LiteLLM с заголовком `Authorization`.

### 3.7. Логирование

- только `get_logger(__name__)`, никаких `print()` в `src/` (в `run.py` — допустимо);
- уровни по смыслу: `debug` — детали, `info` — значимые события, `warning` — восстановимые
  проблемы, `error`/`exception` — сбой;
- ленивое форматирование в логах: `log.info("... %s", value)`, а не f-строки.

### 3.8. Стиль

- `from __future__ import annotations` в начале модуля;
- type hints в публичных функциях и методах (`str | None`, `Dict[str, Any]`, `Optional[...]`);
- docstring модуля — по-русски, с блоком «Изменения:» как в `processor.py`, `main.py`,
  `platform_utils.py`;
- комментарии — по-русски, объясняют **почему**, особенно для нетривиальных мест
  (Wayland, keyring на Windows, `numPr` в docx, отмена ffmpeg);
- импорты: stdlib → сторонние → `src`; внутри `src` — относительные (`from .utils import ...`);
- в `main.py` двойной импорт (пакетный и скриптовый режим, `if __package__ in (None, "")` / `else`) —
  новый модуль добавлять в **обе ветки**.

### 3.9. Формат ответа агента

Из `src/SKILS.md`: изменённые файлы выводить полностью, если это возможно; иначе — полностью
изменённый метод/функцию, а не фрагмент «до/после» без контекста.

---

## 4. Типовые сценарии изменений

### Добавить настройку
1. `ConfigManager.get_defaults()` — ключ с дефолтом в нужной секции.
2. `ui/settings_window.py` — поле в соответствующей вкладке + подсказка.
3. `config_manager.py` — загрузка/нормализация (если нужна миграция старого конфига).
4. Использование — через `config[...].get(...)` с fallback.

### Добавить поле в метаданные записи
1. `main._build_default_meta()` — значение по умолчанию.
2. `ui/metadata_dialog.py` — виджет на нужной вкладке + подсказка.
3. `processor` / `sync_manager` — если поле влияет на обработку или публикацию.
4. Прокинуть значение в `initial` при открытии диалога.

### Добавить вызов внешнего API
1. Новый или существующий `*_client.py` — aiohttp, таймауты из конфига, собственное исключение.
2. Никаких UI-импортов в клиенте.
3. Ошибки наверх — исключением; UI показывает диалог, логика решает, ретраить или нет.

### Новое окно
1. `src/ui/<name>_window.py`, класс `QWidget` / `QDialog`.
2. Связь с логикой — только через сигналы.
3. Регистрация в `main.py` (обе ветки импорта) и в меню/трее при необходимости.
4. Все поля — с подсказками.

### Экспорт и документы
- MD → DOCX: `markdown_docx.py`;
- вставка готового `.docx` с сохранением форматирования (runs, стили абзацев, списки по `numPr`,
  таблицы) — `_render_blocks_to_docx` в `library_search.py`;
- MD → Bitrix: `markdown_to_bitrix.py`;
- чтение любого файла: `file_readers.read_any_text` — не писать свой парсер `.docx`/`.pdf`.

---

## 5. Сборка и запуск

- из исходников: `python run.py` (нужен venv с `requirements.txt` и системный `ffmpeg`);
- `.deb`: `./build_deb.sh` → `build/screen-recorder_<version>_all.deb`; версия берётся из
  `src/__init__.py` — менять версию только там;
- Windows: `packaging/windows/build_win.ps1` (PyInstaller) + `installer.iss` (Inno Setup);
- при изменении зависимостей — обновить `requirements.txt` и при необходимости
  `packaging/DEBIAN/control` / `postinst`.

Пути во время работы:
- Linux: конфиг `~/.config/screen-recorder/config.json`, логи и очередь `/tmp/screen-recorder/`;
- Windows: `%APPDATA%\screen-recorder\`, `%TEMP%\screen-recorder\`.

---

## 6. Что НЕ делать

- не возвращать механизм записи экрана, хоткеи, overlay и управление ВМ Yandex;
- не переименовывать публичные классы и файлы «для красоты» — на них завязаны импорты и сборка;
- не тащить `src.ui` в бизнес-логику и клиенты;
- не писать `print()`, прямой `setToolTip()`, хардкод путей `/tmp`;
- не логировать секреты и не хранить их вне `ConfigManager`;
- не трогать `build/`, `dist/`, `.venv/` — это артефакты;
- не менять версию в README/BUILD вручную, если задача этого не требует.

---

## 7. Чек-лист перед сдачей изменения

- [ ] новые поля имеют подсказки через `tooltips.py`;
- [ ] новые константы вынесены в конфиг с дефолтом и подсказкой;
- [ ] нет платформо-специфичных путей и команд вне `platform_utils`;
- [ ] блокирующие вызовы в async-коде обёрнуты в `asyncio.to_thread`;
- [ ] новые фоновые задачи отменяются в `main._quit()`;
- [ ] нет `print()`, секретов в логах, прямых `setToolTip`;
- [ ] импорты в `main.py` добавлены в обе ветки;
- [ ] изменённые файлы/методы выведены полностью;
- [ ] версия (если релиз) обновлена только в `src/__init__.py`.