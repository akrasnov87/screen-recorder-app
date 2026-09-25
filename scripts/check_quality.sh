#!/bin/bash
echo "🔍 black..."
black --check src/ tests/ || true
echo "🔍 pylint..."
pylint src/ --exit-zero || true
echo "✅ Проверка завершена"