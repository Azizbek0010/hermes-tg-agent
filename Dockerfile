FROM node:22-bookworm-slim

# opencode нужен Node (уже есть) + сам CLI ставим через npm глобально
RUN npm install -g opencode-ai@latest

# Python для bot.py / agents_watch.py + curl для health-check в entrypoint
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip curl dos2unix \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

RUN python3 -m venv /app/venv \
    && /app/venv/bin/pip install --no-cache-dir aiogram python-dotenv requests telethon

# Секреты (токены, сессии) приходят через переменные окружения Render.
# dos2unix — entrypoint.sh писался на Windows, CRLF ломает bash на Linux.
RUN dos2unix /app/entrypoint.sh && chmod +x /app/entrypoint.sh

CMD ["/app/entrypoint.sh"]
