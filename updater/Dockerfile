FROM docker:cli

RUN apk add --no-cache python3 git

WORKDIR /app
COPY server.py /app/server.py

EXPOSE 8081
CMD ["python3", "/app/server.py"]
