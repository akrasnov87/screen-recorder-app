# Сборка .deb пакета для Screen Recorder & Transcriber на Ubuntu 26

Разберу по шагам, что нужно сделать, чтобы получить устанавливаемый `.deb` пакет.

## 1. Структура проекта

Сначала наведём порядок в проекте. Нужна примерно такая структура:

```
screen-recorder/
├── src/                        # твой код (все .py файлы)
│   ├── __init__.py
│   ├── main.py
│   ├── config_manager.py
│   └── ...
├── resources/
│   ├── icons/
│   │   ├── app.svg
│   │   ├── recording.svg
│   │   └── ...
│   └── styles.qss
├── packaging/
│   ├── DEBIAN/
│   │   ├── control
│   │   ├── postinst
│   │   ├── prerm
│   │   └── postrm
│   ├── usr/
│   │   ├── bin/
│   │   │   └── screen-recorder
│   │   ├── share/
│   │   │   ├── applications/
│   │   │   │   └── screen-recorder.desktop
│   │   │   ├── icons/
│   │   │   │   └── hicolor/
│   │   │   │       └── scalable/
│   │   │   │           └── apps/
│   │   │   │               └── screen-recorder.svg
│   │   │   └── screen-recorder/
│   │   │       └── src/...
│   └── ...
├── pyproject.toml
├── requirements.txt
├── build_deb.sh
└── README.md
```

## 2. Зависимости приложения

Сначала создай `requirements.txt`:

```txt
PySide6>=6.6
aiohttp>=3.9
keyring>=24.0
cryptography>=42.0
python-docx>=1.1
pypdf>=4.0
pynput>=1.7
```

Системные зависимости (Ubuntu 26):

```bash
sudo apt install -y \
    python3 python3-pip python3-venv \
    ffmpeg \
    x11-utils \
    libxcb-xinerama0 libxcb-cursor0 libxkbcommon-x11-0 \
    libxcb-icccm4 libxcb-keysyms1 libxcb-shape0 \
    libgl1 libegl1 \
    libpulse0 \
    fonts-dejavu
```

## 3. Сборка и установка

```bash
chmod +x build_deb.sh
./build_deb.sh

# Установка
sudo dpkg -i build/screen-recorder_1.1.0_all.deb

# Если ругается на зависимости — доустановить их
sudo apt-get install -f
```

## Важные нюансы

### Python-пакеты в venv, не в системе
Ставить `PySide6` через `pip install` в системный Python на Ubuntu нельзя (PEP 668). Поэтому в `postinst` создаётся отдельный venv в `/opt/screen-recorder/venv`.

### Первый запуск долгий
`postinst` устанавливает все pip-пакеты — это займёт 1–3 минуты. Это нормально. Можно вынести установку в отдельный `apt` хук, но для самодельного пакета сойдёт.

### Настройки и логи — вне пакета
Твой `ConfigManager` пишет в `~/.config/screen-recorder/config.json`, а лог — в `/tmp/screen-recorder/app.log`. Пакет этих путей **не трогает** — при `apt purge` пользовательские данные сохраняются. Если хочешь их удалять — добавь в `postrm` при `purge`:

```bash
rm -rf ~/.config/screen-recorder
```
но это спорно: конфиг содержит зашифрованные ключи, и пользователь может захотеть их сохранить.

### Права на запись экрана
На Ubuntu 26 с Wayland `kmsgrab` требует членства в группе `video`:

```bash
sudo usermod -aG video $USER
```

Можно добавить эту команду в `postinst`, но лучше просто упомянуть в README — иначе пользователь не поймёт, почему запись падает.

### Проверка пакета перед публикацией

```bash
# Посмотреть содержимое
dpkg-deb -c build/screen-recorder_1.1.0_all.deb

# Проверить зависимости
dpkg-deb -I build/screen-recorder_1.1.0_all.deb

# Проверить, что скрипты исполняемы
dpkg-deb -e build/screen-recorder_1.1.0_all.deb build/DEBIAN_extracted
ls -la build/DEBIAN_extracted/
```