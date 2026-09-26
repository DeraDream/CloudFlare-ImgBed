FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    BOT_DB_PATH=/data/bot.db

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py ./

VOLUME ["/data"]

CMD ["python", "bot.py"]
