Я проанализировал клиентское приложение Screen Recorder & Transcriber — десктопное PySide6-приложение для записи экрана, транскрибации, суммаризации и синхронизации с сервером. Ниже разбор архитектуры, ключевых решений и найденных проблем, в том числе в связке с сервером.

---

## 1. Общая характеристика

**Назначение:** GUI-клиент для Linux (Ubuntu), который:
- записывает экран через ffmpeg (X11 `x11grab` / Wayland `kmsgrab`+VAAPI);
- конвертирует видео в аудио;
- шлёт на внешний сервер транскрибации (Whisper API);
- суммаризует результат через сервер транскрибации или LiteLLM;
- формирует DeepSeek-промпт для скрам-митингов;
- ведёт локальную «библиотеку» с полнотекстовым поиском;
- импортирует готовые материалы;
- отправляет протоколы/summary в Bitrix24;
- **синхронизируется с `screc-server`** — публикация/скачивание записей, медиа, справочников.

**Стек:** PySide6, aiohttp, pynput, python-docx, pypdf, cryptography + keyring.

**Версия в `src/__init__.py` — 1.3.2**, при этом README заявляет 1.1.0. Расхождение.

---

## 2. Архитектура

```
main.py                    — QApplication, контроллер, asyncio-поток
config_manager.py          — config.json + Fernet-шифрование секретов
logger.py                  — логирование + GUI-хук
utils.py                   — утилиты (sanitize_filename, xrandr)

recorder.py                — запись ffmpeg
hotkeys.py                 — pynput
task_queue.py              — очередь (JSON)
processor.py               — конвейер: конвертация → транскрибация → summary → DeepSeek
transcribe_client.py       — Whisper API
litellm_client.py          — OpenAI-совместимый API
bitrix_client.py           — Bitrix24 (2-этапная загрузка на Диск)
screc_client.py            — клиент к screc-server
sync_manager.py            — бизнес-логика синхронизации

ui/                        — 15+ окон и диалогов
file_readers.py            — единый ридер .txt/.md/.docx/.pdf/.json
markdown_docx.py           — MD → DOCX
markdown_to_bitrix.py      — MD → BB-код
markdown_editor.py         — редактор MD
library_search.py          — поиск + сборка промпта
tooltips.py                — единая система подсказок
```

Разделение в целом хорошее: бизнес-логика (`processor`, `sync_manager`) отделена от UI, инфраструктура (`file_readers`, `markdown_*`) вынесена.

---

## 3. Сильные стороны

**1. Асинхронный конвейер с корректной отменой.**
`processor.process_video` построен на `asyncio.to_thread` для блокирующих операций, поддерживает `cancel_event`, аккуратно убивает ffmpeg (SIGTERM → ожидание → SIGKILL). Отмена транскрибации реализована через `asyncio.wait_for(cancel_event.wait(), timeout=interval)` — корректный паттерн.

**2. Шифрование секретов.**
`ConfigManager` использует Fernet, ключ хранится в системном keyring с fallback на файл `0600`. Есть методы `get_raw_key`/`import_key` для переноса.

**3. Санитизация имён файлов.**
`sanitize_filename` декодирует URL-encoding (до 3 раз), удаляет запрещённые символы, обрезает по символам с сохранением расширения. Это прямой ответ на проблему «слишком длинные имена → 500 на сервере».

**4. Единый ридер файлов.**
`file_readers.read_any_text` устраняет дублирование чтения `.txt/.md/.docx/.pdf/.json` между `library_search`, `processor`, `send_to_bitrix_dialog` и др.

**5. Сохранение форматирования при экспорте промпта.**
`_render_blocks_to_docx` умеет вставлять `.docx`-файлы в целевой документ с сохранением runs (bold/italic/underline/размер/шрифт), стилей абзацев, списков (в т.ч. по `numPr`, с эвристикой определения bullet vs number по `abstractNum`), таблиц. `.md` конвертируется во временный `.docx` и вставляется. Это нетривиально и сделано аккуратно.

**6. Условная загрузка медиа.**
`sync_manager.publish_session` перед загрузкой медиа делает пакетную проверку `POST /records/{id}/artifacts/check`, получает `skip`-флаги и грузит только те файлы, которых нет на сервере с таким же SHA-256. Плюс `upload_artifact_path` умеет HEAD-проверку. Это реальная экономия трафика.

**7. Аккуратная обработка «умного» скачивания.**
`_should_skip_download` сравнивает SHA-256, при отсутствии — размер. `_prune_local_artifacts` удаляет локальные текстовые артефакты, которых нет на сервере, но **не трогает медиа** — правильное решение.

**8. Индикация несохранённых изменений в редакторе ВМ.**
`YandexVMEditor` эмитит `dirty_changed` только при реальных правках (через `blockSignals` при программной установке). Заголовок вкладки получает «●».

---

## 4. Найденные проблемы

### 4.1. Критичные

**(1) `screc_client.py` и `sync_manager.py` не соответствуют реальному состоянию сервера.**

В `screc_client.py` есть предохранитель:
```python
_DELETE_ALLOWED_ON_SERVER: bool = False
```
`delete_record` и `delete_artifact` бросают `ScrecError`, не отправляя запрос. Логика: «удаление на сервере запрещено политикой». При этом `sync_manager._handle_delete` обрабатывает `action == "delete"` — то есть удаление **на сервере** клиент увидит (кто-то удалил через API напрямую), но сам инициировать не может. Это осознанное решение, но:
- в `DOCS.md` сервера `DELETE` описан как рабочий;
- сервер в `artifacts.py` **не реализует** `DELETE /records/{id}/artifacts` (только одиночный), а дока обещает;
- в `screc_client.py` в комментариях прямо помечено: «DELETE /api/v1/records/{id}/artifacts ← не реализовано».

Итого: клиент и сервер согласованы в том, что удаление не используется, но документация сервера описывает несуществующее. Нужно либо реализовать на сервере, либо убрать из доки.

**(2) `sync_manager._apply_change` при `action == "artifact_upload"` делает полный `download_record`.**

```python
if action in ("artifact_upload", "artifact_delete", ...):
    session_dir = self._find_local_session(record_id, path)
    if session_dir:
        await self.download_record(record_id, session_dir=session_dir)
```
Это перезагружает всю запись при каждом изменении артефакта. Если клиент B загрузил 268 МБ видео, клиент A при получении `artifact_upload` скачает **всю запись целиком**, включая это видео. Для дельты это дорого. Стоит либо скачивать только изменённый артефакт, либо хотя бы уважать `download_media`.

**(3) `_find_local_session` — O(N) обход всех папок при каждом изменении.**

```python
for name in entries:
    ...
    marker = os.path.join(full, _SYNC_MARKER_FILE)
    if not os.path.isfile(marker): continue
    with open(marker) as f: data = json.load(f)
    if str(data.get("record_id")) == record_id: return full
```
При `pull_changes` для каждого изменения из changelog — полный обход `sessions/`. При сотнях записей и тысячах изменений это заметно. Нужен индекс `record_id → session_dir` в `sync_state.json`.

**(4) `sync_manager.pull_configs` использует `get_summary` как транспорт для JSON.**

```python
raw = await client.get_summary(rid)
data = json.loads(raw)
```
`get_summary` возвращает `summary_bb` из `_meta.json` как `text/markdown`. Это работает, потому что `_push_config_item` кладёт `json.dumps(data)` в `summary_bb`. Но:
- это хак, завязанный на формат `summary_bb`;
- если сервер начнёт что-то делать с `summary_bb` (например, отдавать как есть, но с санитайзацией), конфиги сломаются;
- в FTS `summary_bb` индексируется — конфиги попадут в поиск.

Правильнее — отдельные эндпоинты или отдельные поля.

**(5) `screc_client.publish_record` не проверяет результат на `skipped_artifacts` для текстовых артефактов.**

`sync_manager` считает `media_uploaded`/`media_skipped` для медиа, но для текстовых артефактов `result.get("skipped_artifacts")` и `uploaded_artifacts` просто прокидываются наружу. Если сервер пропустил, скажем, `video.txt` из-за совпадения хэша, клиент об этом узнает, но никак не использует. Мелочь, но в логах было бы полезно.

### 4.2. Существенные

**(6) `main.py._build_default_meta` не выставляет `include_tags_in_prompt` в `False` по умолчанию.**

Смотрим:
```python
meta = {
    ...
    "include_name_in_prompt": True,
    "include_project_in_prompt": True,
    "include_comment_in_prompt": True,
    "include_tags_in_prompt": True,
}
```
В `MetadataDialog._apply_initial` по умолчанию тоже `True`. Это осознанно (комментарий в коде: «По умолчанию включены все четыре пункта»). Но в `processor._build_context_header` **теги в контекст не добавляются** — параметр `include_tags` не передаётся, в функции нет ветки для тегов:
```python
def _build_context_header(metadata, include_name, include_project, include_comment):
    # include_tags отсутствует
```
То есть чекбокс «Теги» в UI есть, значение сохраняется в `session.json`, но в промпт теги **не попадают**. Нужно либо добавить в `_build_context_header`, либо убрать чекбокс.

**(7) `processor._build_deepseek_prompt` — `include_tags_in_prompt` тоже не используется.**

Аналогично: метаданные содержат `include_tags_in_prompt`, но в `_build_deepseek_prompt` контекст собирается через `_build_context_header`, который теги не обрабатывает.

**(8) `main.py._ask_metadata` в `MetadataDialog` передаёт `sessions_root`, но `MetadataDialog._on_pick_from_session` ищет `manual_protocol.*`, `protocol.*`, `deepseek_prompt.*`, `video.txt` — и берёт первый найденный. Это не фильтр по проекту.**

При выборе «Из записи…» в скрам-вкладке пользователь видит список **всех** записей, отсортированных по имени папки (то есть по дате). Проект записи показывается в label, но фильтра по проекту нет. Для пользователя с десятками проектов это неудобно.

**(9) `main.py._perform_import`: имя сессии `date_dt.strftime("%Y-%m-%d_%H-%M-%S")` без проекта.**

`_new_session_dir` использует то же самое. Две записи с одинаковым временем в разных проектах получат разные суффиксы `_1`, `_2`. Это не баг, но структура `sessions/` плоская, и при большом архиве `listdir` + сортировка по имени даёт хронологический порядок, что ок.

**(10) `processor._export_prompt_file` для `.md` конвертирует `.docx`-вложения через `_docx_to_markdown`, но не сохраняет таблицы.**

`_docx_to_markdown` обрабатывает только `doc.paragraphs`, `doc.tables` пропускаются. Для промпта это, возможно, и не критично, но для «предыдущего протокола» с таблицами — потеря.

**(11) `markdown_docx._add_inline` для ссылок делает `tail.font.color.rgb = None` — это no-op.**

```python
tail = paragraph.add_run(f" ({m.group(2)})")
tail.font.color.rgb = None  # оставляем как есть
```
`font.color.rgb = None` не «оставляет как есть», а сбрасывает в default (что, впрочем, и так default). Комментарий вводит в заблуждение. Либо убрать строку, либо сделать ссылку реальной гиперссылкой (python-docx это умеет через XML).

**(12) `markdown_editor._prefix_lines` — потенциальный баг при выделении нескольких строк.**

```python
cursor.setPosition(start)
cursor.movePosition(StartOfBlock)
cursor.beginEditBlock()
while True:
    cursor.insertText(prefix)
    end += len(prefix)
    if cursor.position() >= end:
        break
    if not cursor.movePosition(NextBlock):
        break
cursor.endEditBlock()
```
После `insertText(prefix)` курсор остаётся **после** вставленного префикса, а не в начале следующей строки. `movePosition(NextBlock)` перейдёт в начало следующего блока — это работает, но `end` инкрементируется на `len(prefix)` за итерацию, и условие `cursor.position() >= end` может сработать неверно при не-ASCII префиксах (позиции в Qt — в символах UTF-16, а `len(prefix)` — в Python-символах). Для ASCII-префиксов (`# `, `- `, `1. `) проблемы нет, но `> ` тоже ASCII. Не критично, но хрупко.

**(13) `library_search._search_in_text`: если точных совпадений нет, ищется fuzzy по **всему** тексту, и результаты сортируются по score. Но `SearchHit.match_kind` для fuzzy — «fuzzy», а score — произведение avg и coverage. При нескольких совпадениях в одной сессии все они попадут в hits, дублируя session_name.**

`_deduplicate_contexts` работает на этапе сборки промпта, но в таблице результатов будут дубли. Для UI это приемлемо (разные файлы/сниппеты), но для промпта — дубликаты схлопываются только по пересечению контекста, не по файлу. Если из одного файла 10 фрагментов с разными сниппетами, все 10 попадут в промпт.

**(14) `settings_window._test_sync_connection` создаёт `ScrecClient` и вызывает `ping()`, но не проверяет флаг `_DELETE_ALLOWED_ON_SERVER`.**

Если пользователь включит удаление на сервере (флаг True), клиент покажет успешное подключение, но при попытке удаления — снова ошибка. Нужно либо показывать статус «удаление запрещено» в UI, либо не полагаться на глобальный флаг.

**(15) `tray_manager._resources_dir` — путь `"..", "..", "resources"` от `src/ui/tray_manager.py` даёт `src/resources`, а не корневой `resources`.**

Считаем: `__file__` = `src/ui/tray_manager.py`. `os.path.dirname` → `src/ui`. `join(..., "..", "..", "resources")` → `src/ui/../../resources` = `resources`. Ок, это корневой `resources`. Комментарий верный.

**(16) `sync_window._load_force_overwrite_setting` и `_on_force_overwrite_toggled` используют `config_manager.set_sync_settings`.**

`set_sync_settings` **перезаписывает** `api_key` через `enc:...`. Если пользователь в окне синхронизации не трогал ключ, `get_sync_settings` вернёт расшифрованный ключ, `set_sync_settings` зашифрует его заново — ок. Но если ключ был пустой, а пользователь меняет `force_overwrite`, `set_sync_settings` запишет `api_key=""`. Это может случайно сбросить ключ, если он был задан в другом месте. Стоит в `_on_force_overwrite_toggled` использовать точечное обновление конфига, а не полный `set_sync_settings`.

### 4.3. Менее критичные

**(17) `README.md` заявляет версию 1.1.0, `__init__.py` — 1.3.2.**

**(18) `README.md` описывает функциональность, которой нет:**
- «Синхронизация с сервером» — есть в коде, но не в разделе «Возможности» (упомянута только в меню трея).
- «Экспорт/импорт настроек в JSON» — есть.
- «Версия приложения в правом нижнем углу» — есть.
- Но при этом в разделе «Структура проекта» не упомянуты `screc_client.py`, `sync_manager.py`, `sync_window.py`, `bbcode_editor.py`.

**(19) `metadata_dialog._on_accept` формирует `ordered_tags` в порядке справочника, но `_selected_tags` может содержать теги, которых нет в `_tags` (если `_get_tags_cb` вернул устаревший список).**

В конце:
```python
for name in self._selected_tags:
    if name and name not in ordered_tags:
        ordered_tags.append(name)
```
Это добавляет «хвост». Ок.

**(20) `processor._render_blocks_to_docx` использует `from docx.enum.text import WD_ALIGN_PARAGRAPH`, но в `_append_markdown_text_to_docx` импортирует его повторно.**

Мелочь, дублирование импортов.

**(21) `hotkeys._parse_hotkey` возвращает `frozenset`, содержащий и модификаторы (Key.ctrl), и символы ('r').**

```python
if p in _MODIFIER_ALIASES:
    keys.add(_MODIFIER_ALIASES[p])
else:
    keys.add(p)
```
`'r'` добавляется как строка, `Key.ctrl` — как enum. `_normalize_key` приводит правый Ctrl к `Key.ctrl`, а `'r'` — к `'r'`. `combo.issubset(pressed)` работает, если pressed содержит и то, и другое. Ок.

Но: `_on_press` добавляет `norm` в `self._pressed`, а `_on_release` удаляет. Если пользователь нажмёт `Ctrl+Shift+R`, потом отпустит `R`, но оставит `Ctrl+Shift`, и нажмёт `R` снова — сработает. Это нормальное поведение.

**(22) `hotkeys.on_start_stop` при `status["recording"]` работает напрямую с `recorder`, игнорируя `start_cb`/`stop_cb`.**

```python
if status["recording"]:
    ...
    asyncio.run_coroutine_threadsafe(self.recorder.pause_recording(), loop)
    return
if self._start_cb is not None:
    self._start_cb()
```
То есть пауза/возобновление идут напрямую, а старт — через callback (чтобы учесть настройки показа метаданных). Это осознанно, но для паузы тоже мог бы быть callback, если в будущем понадобится логика.

**(23) `screc_client.upload_artifact_path` считает sha256 **каждый раз** перед загрузкой.**

```python
sha = _sha256_file(file_path)
...
if skip_if_hash_matches and sha:
    info = await self.check_artifact(record_id, filename, sha)
```
Для 268 МБ видео это ~1–2 секунды CPU. Приемлемо, но для повторных публикаций одной и той же записи хэш можно кэшировать (mtime + size → sha). В `sync_manager.publish_session` хэши уже считаются для `check_artifacts_batch`, а потом `upload_artifact_path` считает их **снова**. Двойной проход по файлу. Стоит передавать sha в `upload_artifact_path`.

**(24) `sync_manager.publish_session` передаёт `artifact_hashes` в `publish_record`, но `publish_record` использует только `basename` файла как ключ.**

```python
sha = artifact_hashes.get(os.path.basename(full_path), "")
```
А `small_artifact_hashes` формируется с ключом `os.path.basename(filename)`, где `filename` — относительный путь (`attachments/foo.pdf`). `basename` даст `foo.pdf`. Совпадает. Но для `video.txt` — `video.txt`. Ок.

**(25) `sync_manager.collect_artifacts` не включает в `small` файлы `video_summary.md` и `summary.md` одновременно.**

```python
("summary", "video_summary.md"),
("summary", "summary.md"),
```
`seen_kinds["summary"] = True` после первого найденного. Если есть оба файла, уйдёт только `video_summary.md`. Это осознанно (один kind — один файл на сервере), но пользователь может не понять, почему `summary.md` не отправился.

**(26) `sessions_window._on_media_download_finished` fallback-поиск локального файла:**

```python
for ext in _VIDEO_EXTS:
    candidate = os.path.join(row["dir"], f"video{ext}")
```
`_VIDEO_EXTS` включает и видео-, и аудио-расширения (`.mp3`, `.wav` и т.д.). Для `kind == "video"` это может найти аудиофайл. Стоит разделить `_VIDEO_EXTS` и `_AUDIO_EXTS` (что в других местах проекта и сделано).

**(27) `settings_window._apply_form_to_config` сохраняет `cfg["projects"]` напрямую из таблицы, но не вызывает `set_projects`. Аналогично `cfg["tags"]`.**

Это работает, но обходит нормализацию (`set_projects` чистит дубликаты и пустые имена). В форме дубликаты уже отфильтрованы вручную, но при импорте конфига из JSON может прийти что угодно.

**(28) `library_window._on_tag_enabled_toggled` включает/выключает `tag_combo`, но не сбрасывает `tag_includes_untagged`.**

Если пользователь выбрал «— без тега —», потом снял галочку «Тег:», потом снова поставил — состояние `tag_combo` сохранилось, и `tag_includes_untagged` снова True. Ок, но если пользователь выбрал конкретный тег, снял галочку, потом в коде что-то поменяло `tag_combo` — `tag_includes_untagged` останется от прошлого. В `_build_filters` это пересчитывается каждый раз, так что проблемы нет.

**(29) `processor._build_context_header` не добавляет теги, хотя `include_tags_in_prompt` есть в metadata.**

См. пункт (6). Это самая заметная функциональная дыра: UI показывает чекбокс, значение сохраняется, но в промпт не идёт.

**(30) `settings_window._on_version_click` переопределяет `mousePressEvent` у QLabel.**

```python
label.mousePressEvent = self._on_version_click  # type: ignore
```
Это работает, но `_on_version_click` не вызывает `super().mousePressEvent`, что может сломать hover-эффект из QSS (`QLabel:hover`). Мелочь.

**(31) `processor.run_forever` не логирует начало каждой итерации, только взятие задачи.**

При отладке «почему задача не берётся» придётся смотреть `_maybe_retry_failed` и `get_next_task`. Логи есть в `get_next_task` (debug), но не в цикле.

**(32) `processor.process_video` при `provider == "server"` и `generate_summary == False` не передаёт промпт на сервер, но всё равно ждёт результат транскрибации.**

```python
if provider == "server" and generate_summary:
    server_prompt = prompt
else:
    server_prompt = ""
    if provider == "server" and not generate_summary:
        log.info("[%s] Summary отключено — промпт на сервер не передаём")
```
Логика верная, но если пользователь хотел summary через сервер, а `generate_summary=False`, он получит только транскрипт. Это by design.

---

## 5. Взаимодействие клиента с сервером

### Что согласовано

| Действие | Клиент | Сервер |
|---|---|---|
| `POST /records` с `payload` + `files` + `sha256` | `publish_record` | `create_or_update_record` |
| `GET /records/{id}` | `get_record` | `get_record` |
| `PATCH /records/{id}` | `patch_record` | `patch_record` |
| `HEAD /records/{id}/artifacts/{filename}` | `check_artifact` | `head_artifact` |
| `POST /records/{id}/artifacts/check` | `check_artifacts_batch` | `check_artifacts_batch` |
| `POST /records/{id}/artifacts` | `upload_artifact_path` | `upload_artifact` |
| `GET /records/{id}/artifacts/{filename}` | `download_artifact` | `download_artifact` |
| `GET /sync/changes` | `get_changes` | `sync_changes` |
| `GET /sync/snapshot` | `get_snapshot` | `sync_snapshot` |
| `GET /tree`, `/tree/{project}`, ... | `get_tree`, `get_years`, ... | `list_projects`, ... |

### Что не согласовано

| Действие | Клиент | Сервер |
|---|---|---|
| `DELETE /records/{id}` | `delete_record` заблокирован флагом | `soft_delete` реализован |
| `DELETE /records/{id}/artifacts/{filename}` | `delete_artifact` заблокирован | `delete_artifact` реализован (физически) |
| `DELETE /records/{id}/artifacts` | не реализовано | не реализовано (есть в доке) |
| `GET /records/{id}/summary` | используется как транспорт JSON для конфигов | отдаёт `summary_bb` как markdown |
| `GET /records/{id}/transcript` | не используется клиентом | реализовано |

**Вывод:** клиент и сервер согласованы в том, что удаление не используется. Документация сервера (`DOCS.md`) описывает `DELETE` и `keep_kinds`, которых нет. Это нужно либо реализовать, либо убрать из доки. Хак с `summary_bb` как транспортом для конфигов работает, но хрупок.

### Расхождения в моделях

| Поле | Клиент | Сервер |
|---|---|---|
| `include_tags_in_prompt` | есть в `session.json`, в UI | есть в `RecordPayload`, но в `_build_context_header` не используется |
| `video_url` / `video_size` / `video_mime` | передаётся в payload | сохраняется в `_links.json` |
| `source` | `record` / `upload` / `import` | строка, без валидации |
| `tags` | список строк или `{name, color}` | список строк |
| `attachments` | список абсолютных путей | не передаётся как поле, только файлы |
| `name_template` / `name_abbr` | передаются | сохраняются |

---

## 6. Производительность

- **`_find_local_session`** — O(N) обход всех папок при каждом изменении. Главное узкое место при дельта-синхронизации.
- **`_apply_change` при `artifact_upload`** — полный `download_record`. Дорого.
- **`_sha256_file`** — вызывается дважды для каждого медиафайла (в `publish_session` и в `upload_artifact_path`). Для больших видео — двойная нагрузка на диск.
- **`library_search.search`** — полное сканирование всех сессий. Фильтры по проекту/тегу/датам сужают, но по умолчанию сканируется всё. Для тысяч записей — медленно. Прогресс-бар есть, отмена есть.
- **`processor._read_attachments_text`** — читает все вложения в память (до `max_file_read_chars` = 5 МБ). Для нескольких `.pdf` может быть тяжело.
- **`settings_window._reload_tags`** — объединяет справочник и теги из сессий, сканируя `sessions/`. При большом архиве — медленно, но это действие по кнопке.

---

## 7. Безопасность

**Хорошо:**
- Fernet-шифрование секретов.
- Keyring с fallback на файл `0600`.
- `sanitize_filename` защищает от path traversal и слишком длинных имён.
- `_safe_artifact_path` на сервере + `assert_inside`.
- API-ключ не логируется.

**Проблемы:**
- `_DELETE_ALLOWED_ON_SERVER = False` — предохранитель, но если кто-то его включит, удаление станет возможным без дополнительного подтверждения в UI.
- `send_to_bitrix_dialog` хранит вебхук в `QLineEdit` с `EchoMode.Password`, но при экспорте настроек (`_export_settings`) вебхук и все ключи уходят в JSON **в открытом виде**. В диалоге есть предупреждение, но это всё равно риск.
- `config_manager.encrypt`/`decrypt` используют один Fernet-ключ на всё. При компрометации ключа все секреты раскрыты.
- В `screc_client` при ошибке HEAD-проверки (`check_artifact`) логируется предупреждение и продолжается обычная загрузка. Это правильно для совместимости, но если сервер **всегда** возвращает 500 на HEAD, клиент будет грузить всё подряд, теряя экономию. Стоит запоминать неудачу и не повторять HEAD для следующих файлов в рамках одной сессии.

---

## 8. Расхождения кода и документации (клиент)

| README | Код |
|---|---|
| Версия 1.1.0 | `__init__.py` — 1.3.2 |
| «Синхронизация с сервером» не в «Возможностях» | `sync_window.py`, `sync_manager.py`, `screc_client.py` есть |
| Структура проекта без `screc_client`, `sync_manager`, `sync_window` | Эти файлы есть |
| «Все файлы сохраняются локально» | Есть публикация на сервер (опционально) |
| `bbcode_editor.py` в структуре | Есть, но помечен как «для обратной совместимости» |
| «Экспорт/импорт настроек в JSON» | Есть, с резервной копией |
| «Версия приложения в правом нижнем углу» | Есть |

README отстал от кода. Нужно обновить.

---

## 9. Рекомендации по приоритетам

**Высокий:**
1. Добавить теги в `_build_context_header` (или убрать чекбокс из UI). Сейчас чекбокс «Теги» в промпте — мёртвый.
2. Индексировать `record_id → session_dir` в `sync_state.json`, убрать O(N) `_find_local_session`.
3. `_apply_change` для `artifact_upload` — скачивать только изменённый артефакт, не всю запись.
4. Передавать уже посчитанный sha в `upload_artifact_path`, не считать дважды.
5. Привести README в соответствие с кодом (версия, структура, возможности).

**Средний:**
6. Убрать из `DOCS.md` сервера нереализованные `DELETE /records/{id}/artifacts` и `keep_kinds`, либо реализовать.
7. Заменить хак с `summary_bb` для конфигов на нормальный транспорт (отдельные эндпоинты или поля).
8. В `_on_force_overwrite_toggled` и подобных — точечное обновление `cfg["sync"]`, а не полный `set_sync_settings`.
9. `sessions_window._on_media_download_finished` — разделить `_VIDEO_EXTS` и `_AUDIO_EXTS`.
10. `markdown_docx._add_inline` — сделать ссылки реальными гиперссылками или убрать `tail.font.color.rgb = None`.
11. Кэшировать sha256 по (mtime, size) для повторных публикаций.

**Низкий:**
12. `processor._render_blocks_to_docx` — убрать дублирующиеся импорты.
13. `library_search` — схлопывать дубликаты по файлу на этапе формирования hits, а не только в промпте.
14. `settings_window._apply_form_to_config` — использовать `set_projects`/`set_tags` для нормализации.
15. `metadata_dialog._on_pick_from_session` — фильтровать по проекту текущей записи.
16. `_docx_to_markdown` — сохранять таблицы.
17. `tray_manager` — логировать, какая иконка не найдена, с путём.

---

## 10. Итоговая оценка

**Архитектура:** 8/10. Чёткое разделение UI / бизнес-логики / инфраструктуры, асинхронный конвейер с корректной отменой, аккуратная работа с секретами.

**Код:** 7/10. Читаемый, с docstring'ами, но есть функциональные дыры (теги в промпте), двойной подсчёт sha, O(N) поиск сессии, хак с `summary_bb`.

**Синхронизация с сервером:** 7/10. Условная загрузка и «умное» скачивание сделаны хорошо, но `_apply_change` для артефактов грузит всю запись, а удаление заблокировано без UI-индикации.

**Документация:** 5/10. README отстал от кода, версия не совпадает, структура проекта неполная. `DOCS.md` сервера описывает нереализованное.

**Безопасность:** 7/10. Fernet, keyring, sanitize — хорошо. Экспорт настроек в открытом виде — риск.

**Готовность к проду:** 7/10. Для одного пользователя с сотнями записей — работает. При тысячах — упрётся в `_find_local_session` и полное сканирование. Функциональные дыры (теги в промпте) и расхождения с докой нужно закрыть.

---

## 11. Что можно сделать дальше

Если хотите, могу:

1. **Показать патч для `_build_context_header`**, чтобы теги попадали в промпт (с учётом `include_tags_in_prompt`).
2. **Показать патч для `sync_manager`** — индекс `record_id → session_dir` и точечное скачивание артефактов.
3. **Показать патч для `screc_client`** — передача sha в `upload_artifact_path`, чтобы не считать дважды.
4. **Написать `SKILS.md`-совместимые изменения** для любого из пунктов (с полными файлами и подсказками для новых полей).
5. **Сделать ревью `requirements.txt`** и `build_deb.sh`, если пришлёте.

Скажите, с чего начать.