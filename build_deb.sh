#!/bin/bash
set -euo pipefail

APP_NAME="screen-recorder"
ARCH="all"

# --- Читаем версию из src/__init__.py — единый источник правды ---
if [ -f src/__init__.py ]; then
    VERSION=$(grep -oP '__version__\s*=\s*"\K[^"]+' src/__init__.py || true)
else
    echo "ОШИБКА: не найден src/__init__.py для чтения версии"
    exit 1
fi

if [ -z "${VERSION:-}" ]; then
    echo "ОШИБКА: не удалось извлечь __version__ из src/__init__.py"
    exit 1
fi

echo "Собираю ${APP_NAME} версии ${VERSION}"

BUILD_DIR="build/${APP_NAME}_${VERSION}_${ARCH}"

# 1. Чистим
rm -rf build
mkdir -p "$BUILD_DIR"

# 2. Копируем DEBIAN-скрипты
cp -r packaging/DEBIAN "$BUILD_DIR/"

# --- Нормализация control ---
CONTROL="$BUILD_DIR/DEBIAN/control"

# Убираем \r (на случай, если файл правился в Windows-редакторе)
sed -i 's/\r$//' "$CONTROL"

# Подставляем актуальную версию в control
sed -i "s/^Version:.*/Version: ${VERSION}/" "$CONTROL"

# Убеждаемся, что файл заканчивается переводом строки
if [ -s "$CONTROL" ] && [ "$(tail -c 1 "$CONTROL" | wc -l)" -eq 0 ]; then
    printf '\n' >> "$CONTROL"
fi

# Проверяем, что все строки продолжения Description начинаются
# с пробела — dpkg-deb это требует.
awk '
    /^[A-Za-z][A-Za-z0-9-]*:/ { in_desc = ($0 ~ /^Description:/); next }
    in_desc && !/^ / && !/^\t/ && NF > 0 {
        print "ОШИБКА control: строка продолжения Description должна " \
              "начинаться с пробела: " $0 > "/dev/stderr"
        exit 1
    }
' "$CONTROL"

# 3. Копируем usr-дерево
cp -r packaging/usr "$BUILD_DIR/"

# 4. Копируем исходники
mkdir -p "$BUILD_DIR/usr/share/screen-recorder"
cp -r src "$BUILD_DIR/usr/share/screen-recorder/"
cp run.py "$BUILD_DIR/usr/share/screen-recorder/" 2>/dev/null || true
cp requirements.txt "$BUILD_DIR/usr/share/screen-recorder/"

# 5. Ресурсы
if [ -d resources ]; then
    cp -r resources "$BUILD_DIR/usr/share/screen-recorder/"
fi

# 6. Иконка
mkdir -p "$BUILD_DIR/usr/share/icons/hicolor/scalable/apps"
if [ -f resources/icons/app.svg ]; then
    cp resources/icons/app.svg \
       "$BUILD_DIR/usr/share/icons/hicolor/scalable/apps/${APP_NAME}.svg"
fi

# 7. Права
chmod 755 "$BUILD_DIR/DEBIAN/postinst" \
          "$BUILD_DIR/DEBIAN/prerm" \
          "$BUILD_DIR/DEBIAN/postrm"
chmod 755 "$BUILD_DIR/usr/bin/${APP_NAME}"

# 8. Installed-Size
SIZE=$(du -sk "$BUILD_DIR" | cut -f1)
sed -i "s/^Installed-Size:.*/Installed-Size: ${SIZE}/" "$CONTROL" 2>/dev/null || true

# 9. Сборка
dpkg-deb --build --root-owner-group \
    "$BUILD_DIR" "build/${APP_NAME}_${VERSION}_${ARCH}.deb"

echo ""
echo "Готово: build/${APP_NAME}_${VERSION}_${ARCH}.deb"
ls -lh "build/${APP_NAME}_${VERSION}_${ARCH}.deb"