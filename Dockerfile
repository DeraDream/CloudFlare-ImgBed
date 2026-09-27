FROM node:22-alpine AS dependencies

WORKDIR /app

COPY package.json package-lock.json ./
COPY deploy/profiles ./deploy/profiles

RUN apk add --no-cache python3 make g++ && \
    npm ci --workspace=@cloudflare-imgbed/common --workspace=@cloudflare-imgbed/server --omit=dev && \
    rm -rf /root/.npm /tmp/*

FROM node:22-alpine AS runtime

RUN apk add --no-cache \
        ca-certificates curl git \
        python3 py3-pip \
        supervisor \
        docker-cli docker-cli-compose \
        libjpeg-turbo libwebp zlib && \
    apk add --no-cache --virtual .bot-build \
        build-base python3-dev jpeg-dev zlib-dev libwebp-dev && \
    python3 -m venv /opt/venv

WORKDIR /app

ENV NODE_ENV=production \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8080 \
    UPDATE_HOST=127.0.0.1 \
    UPDATE_PORT=8081 \
    IMGBED_URL=http://127.0.0.1:8080 \
    UPDATE_AGENT_URL=http://127.0.0.1:8081 \
    BOT_DB_PATH=/app/telegram-bot-data/bot.db \
    IMGBED_DB_PATH=/app/data/database.sqlite

COPY --from=dependencies /app/node_modules ./node_modules
COPY package.json ./
COPY VERSION ./VERSION
COPY deploy/profiles ./deploy/profiles
COPY frontend-dist ./frontend-dist
COPY functions ./functions
COPY database ./database
COPY deploy/server ./deploy/server

COPY telegram-bot/requirements.txt /tmp/telegram-bot-requirements.txt
RUN /opt/venv/bin/pip install --no-cache-dir -r /tmp/telegram-bot-requirements.txt && \
    rm -f /tmp/telegram-bot-requirements.txt && \
    apk del .bot-build

COPY telegram-bot ./telegram-bot
COPY updater ./updater
COPY deploy/supervisord.conf /etc/supervisord.conf

EXPOSE 8080

CMD ["/usr/bin/supervisord", "-c", "/etc/supervisord.conf"]
