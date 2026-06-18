#!/bin/bash

# Сохраняем корневой путь проекта, откуда запускается скрипт
BOT_DIR=$(pwd)
LAVALINK_DIR="lavalink"

# === ДОБАВЛЕНО: Загрузка переменных из .env ===
if [ -f "$BOT_DIR/.env" ]; then
  echo "⚙️ Загрузка переменных окружения из .env..."
  # Читаем .env, игнорируя комментарии, и экспортируем переменные в ОС
  export $(grep -v '^#' "$BOT_DIR/.env" | xargs)
else
  echo "⚠️ Предупреждение: Файл .env не найден в корне проекта!"
fi
# =============================================

echo "⏳ Шаг 1: Запуск сервера Lavalink..."
cd "$LAVALINK_DIR" || { echo "❌ Ошибка: Папка Lavalink не найдена"; exit 1; }

# Теперь переменная $LAVALINK_PASSWORD видна Java-процессу
java -jar Lavalink.jar > lavalink.log 2>&1 &
LAVALINK_PID=$!

echo "⏱ Ожидание запуска Lavalink (порт 2333)..."
count=0
while ! nc -z 127.0.0.1 2333; do
  sleep 1
  count=$((count+1))
  if [ $count -gt 30 ]; then
    echo "❌ Ошибка: Lavalink не запустился за 30 секунд. Проверьте lavalink.log"
    kill $LAVALINK_PID
    exit 1
  fi
done

echo "✅ Lavalink успешно запущен!"

echo "🚀 Шаг 2: Запуск Discord бота через uv..."
cd "$BOT_DIR" || { echo "❌ Ошибка: Папка бота не найдена"; kill $LAVALINK_PID; exit 1; }

# Запуск через uv
uv run main.py

# Код ниже выполнится автоматически сразу после закрытия бота (через Ctrl+C)
echo "🛑 Выключение бота. Останавливаем Lavalink..."
kill $LAVALINK_PID
