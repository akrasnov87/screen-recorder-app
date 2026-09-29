#!/usr/bin/env python3
"""Точка входа Screen Recorder."""
import os
import sys

# Путь к встроенному venv или системным пакетам
BASE_DIR = "/usr/share/screen-recorder"
if os.path.isdir(BASE_DIR) and BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from src.main import main

if __name__ == "__main__":
    sys.exit(main())