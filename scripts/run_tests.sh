#!/bin/bash
set -e

cd "$(dirname "$0")/.."

if [ ! -d ".venv" ]; then
    echo "📦 Создание venv..."
    python3 -m venv .venv
fi

source .venv/bin/activate

echo "📦 Установка зависимостей..."
pip install --upgrade pip
pip install -r requirements-dev.txt

echo "🧪 Unit-тесты..."
pytest tests/unit -v --cov=src --cov-report=term

echo "🔗 Интеграционные тесты..."
pytest tests/integration -v --cov=src --cov-append

echo "🎨 GUI-тесты (offscreen)..."
QT_QPA_PLATFORM=offscreen pytest tests/gui -v --cov=src --cov-append

echo "🔐 Тесты безопасности..."
pytest tests/security -v --cov=src --cov-append

echo "📊 Генерация отчёта..."
pytest --cov=src --cov-report=html --cov-report=xml

echo "✅ Готово. Отчёт: ./htmlcov/index.html"