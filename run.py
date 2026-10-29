#!/usr/bin/env python3
"""Точка входа Screen Recorder & Transcriber.

Кроссплатформенная версия:
  • путь к src/ вычисляется относительно расположения run.py,
    а не задан жёстко (/usr/share/screen-recorder);
  • работает и из исходников (git clone), и из установленного
    .deb (Ubuntu), и на Windows 11 (python run.py);
  • не падает, если папка src/ отсутствует — показывает
    понятное сообщение.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _setup_import_paths() -> Path:
    """
    Добавляет корень проекта (папку с src/) в sys.path.

    Возвращает путь к корню проекта.

    Логика:
      1. Корень — это директория, где лежит сам run.py.
      2. Если run.py запущен через симлинк — resolve() развернёт
         его в реальный файл, и корень будет настоящим.
      3. На Ubuntu .deb установка кладёт run.py в
         /usr/share/screen-recorder/run.py, а src/ — рядом.
         Относительный путь сработает.
      4. На Windows — src/ лежит рядом с run.py.
    """
    run_py = Path(__file__).resolve()
    project_root = run_py.parent

    root_str = str(project_root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)

    return project_root


def _is_frozen() -> bool:
    """
    Определяет, работает ли приложение в замороженной сборке
    (PyInstaller / cx_Freeze / Nuitka / py2app).

    В таких сборках исходники упакованы в архив (PYZ), а не
    лежат отдельными файлами, поэтому проверка `src/` рядом
    с `run.py` неприменима — импорт всё равно сработает через
    импорт-хуки PyInstaller.
    """
    return bool(
        getattr(sys, "frozen", False)
        or hasattr(sys, "_MEIPASS")
    )


def _check_src_exists(project_root: Path) -> bool:
    """
    Проверяет, что рядом с run.py лежит папка src/.

    В замороженной сборке (PyInstaller и т.п.) модули упакованы
    в PYZ-архив, а не лежат отдельной папкой — проверка теряет
    смысл, возвращаем True.
    """
    if _is_frozen():
        return True
    src_dir = project_root / "src"
    return src_dir.is_dir()


def _print_missing_src_help(project_root: Path) -> None:
    """Печатает понятную инструкцию, если src/ не найдена."""
    import platform

    print(
        "ОШИБКА: не найдена папка src/ рядом с run.py.\n"
        f"  Ожидалось: {project_root / 'src'}\n"
        "\n"
        "Проверьте, что структура проекта такая:\n"
        f"  {project_root}\n"
        "  ├── run.py\n"
        "  ├── src/\n"
        "  │   ├── __init__.py\n"
        "  │   ├── main.py\n"
        "  │   └── ...\n"
        "  └── resources/\n"
        "\n"
        "Если приложение установлено из пакета — переустановите:\n"
        "  • Ubuntu (.deb):\n"
        "      sudo dpkg -i screen-recorder_*.deb\n"
        "      sudo apt-get install -f\n"
        "  • Windows (.exe):\n"
        "      пересоберите дистрибутив:\n"
        "      .\\packaging\\windows\\build_win.ps1 -Clean\n"
        "\n"
        f"Текущая платформа: {platform.system()}\n"
    )


def main() -> int:
    project_root = _setup_import_paths()

    if not _check_src_exists(project_root):
        _print_missing_src_help(project_root)
        return 1

    try:
        from src.main import main as app_main
    except ImportError as exc:
        print(
            f"ОШИБКА: не удалось импортировать src.main: {exc}\n"
            "\n"
            "Возможные причины:\n"
            "  • не установлены зависимости "
            "(pip install -r requirements.txt);\n"
            "  • запуск из неполной установки;\n"
            "  • ошибка синтаксиса в одном из модулей src/.\n"
        )
        return 1

    return app_main()


if __name__ == "__main__":
    sys.exit(main())