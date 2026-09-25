# Screen Recorder & Transcriber

Десктопное приложение для Ubuntu: запись экрана, конвертация в аудио и (опционально) транскрибация.
**Все файлы сохраняются локально.**

## Возможности

- Запись экрана через `ffmpeg` (X11: `x11grab` + `libx264`, Wayland: `kmsgrab` + `h264_vaapi`).
- Пауза/возобновление записи.
- Наложение водяного знака (только под X11).
- Автоматическая конвертация видео в аудио (mp3/aac/wav/opus).
- Опциональная транскрибация через внешний сервис.
- **Локальное хранение** результатов — без отправки на NAS.
- Системный трей и плавающая панель управления.
- Глобальные горячие клавиши (`Ctrl+Shift+R` — старт/пауза, `Ctrl+Shift+S` — стоп).
- Подробное логирование.

## Установка

```bash
sudo apt update
sudo apt install -y ffmpeg python3 python3-pip python3-venv python3-full x11-utils vainfo \
                    intel-media-va-driver-non-free i965-va-driver-shaders \
                    gnome-shell-extension-appindicator
cd screen-recorder-app
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Для Wayland (kmsgrab) нужно:

```bash
sudo usermod -aG video $USER
sudo usermod -aG render $USER
sudo setcap cap_sys_admin+ep $(which ffmpeg)
# logout/login
```

## Запуск

```bash
source .venv/bin/activate
python -m src.main
# или с DEBUG-логом
SCREEN_RECORDER_LOG_LEVEL=DEBUG python -m src.main
```

## Куда сохраняются файлы

По умолчанию:

```
/tmp/screen-recorder/sessions/<YYYY-MM-DD_HH-MM-SS>/
├── video.mp4              ← запись
├── video.ffmpeg.log       ← stderr ffmpeg
├── video.mp3              ← аудиодорожка
└── video.txt              ← транскрипт (если включён сервер)
```

**Важно:** `/tmp` очищается при перезагрузке. Чтобы сохранять файлы надолго:

1. Откройте настройки (левый клик по иконке в трее).
2. Вкладка **«Хранилище»**.
3. Укажите **постоянную** папку, например `/home/a-krasnov/Videos/screen-recorder`.
4. Сохранить.

## Горячие клавиши

| Действие | По умолчанию |
|---|---|
| Старт/Пауза | `Ctrl+Shift+R` |
| Стоп | `Ctrl+Shift+S` |

## Настройка

Окно настроек открывается **левым кликом** по иконке трея. Вкладки:

- **Транскрибация** — URL сервера и access key (можно оставить пустыми — тогда шаг пропускается).
- **Запись** — монитор, микрофон, водяной знак, горячие клавиши.
- **Форматы и сжатие** — кодек аудио, битрейты, уровень сжатия.
- **Хранилище** — путь к папке сессий и срок хранения.

### Файл конфигурации

```
~/.config/screen-recorder/config.json
```

### Временные файлы

```
/tmp/screen-recorder/
├── app.log              # логи приложения
└── queue.json           # очередь задач
```

## Что происходит после остановки записи

1. Файл `video.mp4` **остаётся** в `<temp_path>/sessions/<timestamp>/`.
2. Выполняется **конвертация в аудио** (`video.mp3`).
3. Если в настройках задан **URL сервера транскрибации** — выполняется транскрибация
   и результат сохраняется в `video.txt`.
4. Если URL **пустой** — шаг транскрибации пропускается, файлы остаются только `video.mp4` и `video.mp3`.
5. **Загрузка на NAS не выполняется** — приложение работает в локальном режиме.

## Тестирование

```bash
bash scripts/run_tests.sh
```

## Устранение неполадок

### 1. `externally-managed-environment` при `pip install`

Используйте venv:

```bash
sudo apt install -y python3-venv python3-full
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. `ModuleNotFoundError: No module named 'src.main'`

Запускать из корня проекта:

```bash
cd screen-recorder-app
python -m src.main
```

### 3. Пустые строки в `QComboBox`

В `resources/styles.qss` должно быть правило:

```css
QComboBox QAbstractItemView {
    background-color: palette(base);
    color: palette(text);
    border: 1px solid palette(mid);
    selection-background-color: palette(highlight);
    selection-color: palette(highlightedText);
    outline: none;
}
```

### 4. Меню в трее обрезано (GNOME)

```bash
sudo apt install -y gnome-shell-extension-appindicator
gnome-extensions enable ubuntu-appindicators@ubuntu.com
# logout/login
```

Резерв: **левый клик** по иконке открывает настройки.

### 5. Чёрный экран в записи под Wayland

Проверьте `XDG_SESSION_TYPE`. Под Wayland используется `kmsgrab` + `h264_vaapi`.
Нужны:

- `vainfo` — проверьте, что VAAPI работает;
- `/dev/dri/cardN` — доступен пользователю (группы `video`, `render`);
- `sudo setcap cap_sys_admin+ep $(which ffmpeg)`.

### 6. `Failed to open DRM device`

```bash
ls -la /dev/dri/
# должно быть cardN и renderD128

sudo usermod -aG video $USER
sudo usermod -aG render $USER
sudo setcap cap_sys_admin+ep $(which ffmpeg)
# logout/login
```

### 7. VAAPI не работает

```bash
sudo apt install -y intel-media-va-driver-non-free i965-va-driver-shaders vainfo
vainfo
# если H264 есть — работает
```

Если не работает — используйте **CPU-кодек**. В `src/recorder.py` замените в Wayland-ветке:

```python
vf = f"{crop_expr},hwdownload,format=bgr0,format=yuv420p"
cmd += ["-vf", vf, "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", "-b:v", f"{video_bitrate}k"]
```

### 8. `ffmpeg` падает, а приложение «не видит»

Лог ffmpeg пишется рядом с видео:

```bash
cat /tmp/screen-recorder/sessions/<timestamp>/video.ffmpeg.log
```

Смотрите последние строки — там причина.

### 9. Overlay-панель показывает 100% и старые логи

Метод `OverlayPanel.reset()` вызывается при старте и остановке записи (уже реализовано).

### 10. Ошибки при транскрибации

- Проверьте URL сервера во вкладке **«Транскрибация»**.
- Если URL пустой — шаг пропускается, ошибки не будет.
- Логи: `tail -f /tmp/screen-recorder/app.log | grep transcribe`.

## Лицензия

MIT