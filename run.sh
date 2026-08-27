#!/usr/bin/env bash
# run.sh — запуск моста Telegram ↔ opencode одной командой
# Уроки из гайда: старые поллеры убиваем (409 Conflict), токен только в .env
# tmux не установлен → nohup + PID-файл

set -u
cd "$(dirname "$0")"

export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:$PATH"

echo "🧹 Убиваю старые инстансы..."
[ -f bot.pid ] && kill "$(cat bot.pid)" 2>/dev/null; sleep 1
pkill -f "tg-agents-kit/bot.py" 2>/dev/null
pkill -f "tg-agents-kit.venv.*python.*bot.py" 2>/dev/null
pkill -f "opencode serve --port" 2>/dev/null
sleep 1

if [ ! -f .env ]; then
  echo "❌ Нет .env"; exit 1
fi

mkdir -p logs

# Windows venv кладёт интерпретатор в Scripts/, не в bin/ (как на Linux у оригинала кита)
PY="./venv/bin/python"
[ -f "./venv/Scripts/python.exe" ] && PY="./venv/Scripts/python.exe"

echo "🚀 Поднимаю мост..."
PYTHONUNBUFFERED=1 nohup "$PY" bot.py > logs/bot.log 2>&1 &
echo $! > bot.pid
sleep 5

if kill -0 "$(cat bot.pid)" 2>/dev/null; then
  echo "✅ Запущено (PID $(cat bot.pid))."
  echo "   Логи:      tail -f logs/bot.log"
  echo "   Остановить: kill \$(cat bot.pid)"
  tail -3 logs/bot.log
else
  echo "❌ Упал сразу, лог:"
  tail -15 logs/bot.log
  exit 1
fi
