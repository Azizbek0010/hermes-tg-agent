#!/bin/bash
set -e

echo "[entrypoint] restoring opencode auth..."
mkdir -p "$HOME/.local/share/opencode" "$HOME/.config/opencode"
if [ -n "$OPENCODE_AUTH_B64" ]; then
  echo "$OPENCODE_AUTH_B64" | base64 -d > "$HOME/.local/share/opencode/auth.json"
fi
cp /app/opencode.container.jsonc "$HOME/.config/opencode/opencode.jsonc"

export OC_PORT="${OC_PORT:-46177}"
export OC_CWD="${OC_CWD:-/app}"

echo "[entrypoint] starting opencode serve on :$OC_PORT ..."
# </dev/null — иначе, если opencode на первом запуске ждёт ввод с stdin
# (какой-нибудь first-run prompt), процесс висит вечно без единой строки
# в логе. Это и произошло при первом деплое 2026-08-27 (зависание >1 часа).
opencode serve --port "$OC_PORT" --hostname 127.0.0.1 < /dev/null > /app/logs_serve.log 2>&1 &
SERVE_PID=$!

for i in $(seq 1 30); do
  if curl -s "http://127.0.0.1:$OC_PORT/doc" > /dev/null 2>&1; then
    echo "[entrypoint] opencode serve is up after ${i}0s"
    break
  fi
  echo "[entrypoint] waiting for opencode serve... attempt $i/30"
  if ! kill -0 $SERVE_PID 2>/dev/null; then
    echo "[entrypoint] !!! opencode serve process DIED, tail of its log:"
    tail -30 /app/logs_serve.log
    break
  fi
  sleep 2
done
echo "[entrypoint] final serve.log tail:"
tail -20 /app/logs_serve.log

echo "[entrypoint] starting bot.py..."
/app/venv/bin/python /app/bot.py > /app/logs_bot.log 2>&1 &
BOT_PID=$!

echo "[entrypoint] starting agents_watch.py..."
/app/venv/bin/python /app/agents_watch.py > /app/logs_watch.log 2>&1 &
WATCH_PID=$!

echo "[entrypoint] all started: serve=$SERVE_PID bot=$BOT_PID watch=$WATCH_PID"

# если любой процесс упадёт — контейнер должен упасть, Render перезапустит сам
wait -n $SERVE_PID $BOT_PID $WATCH_PID
echo "[entrypoint] one of the processes exited, shutting down"
exit 1
