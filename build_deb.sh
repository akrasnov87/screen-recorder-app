#!/bin/bash
set -euo pipefail

APP_NAME="screen-recorder"
VERSION="1.1.0"
ARCH="all"
BUILD_DIR="build/${APP_NAME}_${VERSION}_${ARCH}"

# 1. Чистим
rm -rf build
mkdir -p "$BUILD_DIR"

# 2. Копируем DEBIAN-скрипты
cp -r packaging/DEBIAN "$BUILD_DIR/"

# 3. Копируем usr-дерево
cp -r packaging/usr "$BUILD_DIR/"

# 4. Копируем исходники в /usr/share/screen-recorder
mkdir -p "$BUILD_DIR/usr/share/screen-recorder"
cp -r src "$BUILD_DIR/usr/share/screen-recorder/"
cp run.py "$BUILD_DIR/usr/share/screen-recorder/"
cp requirements.txt "$BUILD_DIR/usr/share/screen-recorder/"

# 5. Копируем resources
if [ -d resources ]; then
    cp -r resources "$BUILD_DIR/usr/share/screen-recorder/"
fi

# 6. Иконка (если ещё не в usr/share/icons)
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

# 8. Считаем размер
SIZE=$(du -sk "$BUILD_DIR" | cut -f1)
sed -i "s/^Installed-Size:.*/Installed-Size: ${SIZE}/" \
    "$BUILD_DIR/DEBIAN/control" 2>/dev/null || true

# 9. Собираем .deb
dpkg-deb --build --root-owner-group \
    "$BUILD_DIR" "build/${APP_NAME}_${VERSION}_${ARCH}.deb"

echo ""
echo "Готово: build/${APP_NAME}_${VERSION}_${ARCH}.deb"
ls -lh "build/${APP_NAME}_${VERSION}_${ARCH}.deb"