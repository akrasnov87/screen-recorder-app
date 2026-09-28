#!/bin/bash
# -----------------------------------------------------------------------------
# Сборка DEB-пакета для Screen Recorder & Transcriber.
#
# Требуется:
#   • Ruby + gem (для fpm)
#   • Структура build/deb/ должна содержать:
#       build/deb/opt/screen-recorder/{src,resources}
#       build/deb/usr/bin/screen-recorder
#       build/deb/usr/bin/screen-recorder-gui
#       build/deb/usr/share/applications/screen-recorder.desktop
#       build/deb/usr/share/icons/hicolor/...
#       build/deb/DEBIAN/{postinst,prerm}
# -----------------------------------------------------------------------------
set -euo pipefail

# ---- Версия пакета и архитектура --------------------------------------------
PKG_NAME="screen-recorder"
PKG_VERSION="1.1.0"
PKG_ARCH="amd64"
PKG_MAINTAINER="Your Name <email@example.com>"

# ---- Установка fpm (если ещё нет) -------------------------------------------
if ! command -v fpm &>/dev/null; then
    echo "==> Устанавливаю fpm…"
    if ! command -v gem &>/dev/null; then
        echo "Ошибка: gem не установлен. Установите ruby:"
        echo "    sudo apt install ruby ruby-dev build-essential"
        exit 1
    fi
    gem install fpm --no-document
fi

# ---- Подготовка пустой структуры (на случай, если что-то забыли) ------------
mkdir -p build/deb/DEBIAN
mkdir -p build/deb/opt/screen-recorder
mkdir -p build/deb/usr/bin
mkdir -p build/deb/usr/share/applications
mkdir -p build/deb/usr/share/icons/hicolor/256x256/apps

# ---- Копирование иконок ----------------------------------------------------
if [ -f resources/icons/app.svg ]; then
    mkdir -p build/deb/usr/share/icons/hicolor/scalable/apps
    cp resources/icons/app.svg \
       build/deb/usr/share/icons/hicolor/scalable/apps/screen-recorder.svg
fi
if [ -f resources/icons/app.png ]; then
    for size in 48 64 128 256; do
        mkdir -p "build/deb/usr/share/icons/hicolor/${size}x${size}/apps"
        cp resources/icons/app.png \
           "build/deb/usr/share/icons/hicolor/${size}x${size}/apps/screen-recorder.png"
    done
fi

# ---- Генерация control-файла ------------------------------------------------
cat > build/deb/DEBIAN/control <<EOF
Package: ${PKG_NAME}
Version: ${PKG_VERSION}
Section: utils
Priority: optional
Architecture: ${PKG_ARCH}
Maintainer: ${PKG_MAINTAINER}
Depends: ffmpeg,
         python3 (>= 3.10),
         python3-pyside6.qtwidgets,
         python3-pyside6.qtcore,
         python3-pyside6.qtgui,
         python3-aiohttp,
         python3-aiofiles,
         python3-cryptography,
         python3-keyring,
         python3-pynput,
         python3-docx,
         python3-pypdf,
         python3-rapidfuzz
Recommends: x11-utils,
            vainfo,
            intel-media-va-driver-non-free,
            gnome-shell-extension-appindicator
Homepage: https://github.com/your/repo
Description: Screen Recorder & Transcriber
 Десктопное приложение для Ubuntu: запись экрана, конвертация в аудио,
 транскрибация через внешний сервис, суммаризация через LiteLLM,
 полнотекстовый поиск по архиву и отправка протоколов в Bitrix24.
 .
 Все файлы сохраняются локально.
EOF

# ---- Финальная сборка -------------------------------------------------------
echo "==> Собираю deb-пакет ${PKG_NAME}_${PKG_VERSION}_${PKG_ARCH}.deb"

fpm -s dir -t deb \
    -n "${PKG_NAME}" \
    -v "${PKG_VERSION}" \
    -a "${PKG_ARCH}" \
    --maintainer "${PKG_MAINTAINER}" \
    --category utils \
    --depends ffmpeg \
    --depends "python3 >= 3.10" \
    --depends python3-pyside6.qtwidgets \
    --depends python3-pyside6.qtcore \
    --depends python3-pyside6.qtgui \
    --depends python3-aiohttp \
    --depends python3-aiofiles \
    --depends python3-cryptography \
    --depends python3-keyring \
    --depends python3-pynput \
    --depends python3-docx \
    --depends python3-pypdf \
    --depends python3-rapidfuzz \
    --recommends x11-utils \
    --recommends vainfo \
    --recommends intel-media-va-driver-non-free \
    --recommends gnome-shell-extension-appindicator \
    --after-install build/deb/DEBIAN/postinst \
    --before-remove build/deb/DEBIAN/prerm \
    -C build/deb/ .

echo "==> Готово: $(ls -1 ${PKG_NAME}_${PKG_VERSION}_${PKG_ARCH}.deb)"