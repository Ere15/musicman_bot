#!/bin/bash

BOT_DIR=$(pwd)
LAVALINK_DIR="lavalink"

# 1. Надежно экспортируем переменные из .env в окружение Bash
if [ -f "$BOT_DIR/.env" ]; then
  echo "⚙️ Загрузка переменных окружения из .env..."
  set -a
  source "$BOT_DIR/.env"
  set +a
else
  echo "❌ Ошибка: Файл .env не найден в корне проекта!"
  exit 1
fi

echo "⏳ Шаг 1: Запуск сервера Lavalink..."
cd "$LAVALINK_DIR" || { echo "❌ Ошибка: Папка Lavalink не найдена"; exit 1; }

LAVALINK_SERVER_PASSWORD="$LAVALINK_PASSWORD" java -jar Lavalink.jar > lavalink.log 2>&1 &
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

# Запуск бота (uv подхватит тот же .env)
uv run main.py

echo "🛑 Выключение бота. Останавливаем Lavalink..."
kill $LAVALINK_PID
