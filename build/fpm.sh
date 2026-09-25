#!/bin/bash
set -e

gem install fpm --no-document

fpm -s dir -t deb -n screen-recorder \
  -v 1.0.0 \
  --description "Screen Recorder & Transcriber for Ubuntu" \
  --depends ffmpeg \
  --depends "python3 >= 3.10" \
  --depends python3-pyside6 \
  --depends python3-aiohttp \
  --depends python3-aiofiles \
  --depends python3-cryptography \
  --depends python3-keyring \
  --after-install build/deb/DEBIAN/postinst \
  --before-remove build/deb/DEBIAN/prerm \
  -C build/deb/ .