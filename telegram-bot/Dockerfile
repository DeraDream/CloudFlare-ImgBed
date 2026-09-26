FROM python:3.12-slim

WORKDIR /app

ARG APP_VERSION=dev
ENV PYTHONUNBUFFERED=1 \
    BOT_DB_PATH=/data/bot.db \
    APP_VERSION=${APP_VERSION}

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py ./

VOLUME ["/data"]

CMD ["python", "bot.py"]
