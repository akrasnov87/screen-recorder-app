# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller-spec для Screen Recorder & Transcriber (Windows 10/11).

Сборка:
    pyinstaller --clean --noconfirm packaging\\windows\\ScreenRecorder.spec

Результат:
    dist\\ScreenRecorder\\ScreenRecorder.exe   ← основное приложение
    dist\\ScreenRecorder\\                      ← вся папка (datas + dll)

ВНИМАНИЕ:
  • Файл app.ico должен лежать рядом со спеком:
        packaging\\windows\\app.ico
  • Ресурсы (styles.qss, иконки svg) берутся из корня репозитория:
        resources\\...
"""

from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_submodules,
)


# Корень проекта — на два уровня выше этого spec-файла.
ROOT = Path(SPECPATH).parent.parent  # type: ignore[name-defined]

# --- Данные приложения ---
datas = [
    # Весь каталог resources/ — стили и иконки.
    (str(ROOT / "resources"), "resources"),

    # Метаданные keyring.backends (нужны для правильного выбора
    # бэкенда — Windows / macOS / Linux SecretService).
    *collect_data_files("keyring.backends"),
]

# --- Скрытые импорты ---
hiddenimports = [
    # Подмодули keyring — PyInstaller не видит их через
    # статический анализ (они загружаются через entry points).
    *collect_submodules("keyring.backends"),

    # Keyring-бэкенды конкретных ОС — на всякий случай, если
    # collect_submodules что-то пропустит.
    "keyring.backends.Windows",
    "keyring.backends.SecretService",
    "keyring.backends.kwallet",
    "keyring.backends.chainer",
    "keyring.backends.fail",
    "keyring.backends.null",

    # PySide6-модули, которые подключаются динамически.
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",

    # Опциональные зависимости.
    "docx",
    "pypdf",
]

# --- Исключения ---
excludes = [
    "tkinter",
    "matplotlib",
    "numpy",
    "pandas",
    "IPython",
    "notebook",
    "pytest",
    "unittest",
]


a = Analysis(
    [str(ROOT / "run.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ScreenRecorder",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,                  # без чёрного окна консоли
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "packaging" / "windows" / "app.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="ScreenRecorder",
)