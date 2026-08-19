FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/data/family_activity.db \
    BACKUP_DIR=/data/backups \
    LOG_PATH=/data/logs/family_activity.log

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

RUN mkdir -p /data/backups /data/logs
VOLUME ["/data"]

CMD ["python", "main.py"]
