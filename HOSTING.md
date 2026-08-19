# Развёртывание Family Activity Bot v1.4 на хостинге

## Главный принцип

База и backup должны жить на **постоянном диске/volume**, а не внутри временного контейнера.

Production paths:

```env
DATABASE_PATH=/data/family_activity.db
BACKUP_DIR=/data/backups
LOG_PATH=/data/logs/family_activity.log
```

## Docker

```bash
docker build -t family-activity-bot .
docker run -d \
  --name family-activity-bot \
  --env-file .env \
  -v family_activity_data:/data \
  --restart unless-stopped \
  family-activity-bot
```

Проверить логи:

```bash
docker logs -f family-activity-bot
```

## Обновление контейнера

1. Создать `/семья резервная_копия`.
2. Убедиться, что volume `/data` остаётся подключённым.
3. Остановить старый контейнер.
4. Собрать новый image.
5. Запустить новый контейнер с **тем же volume**.
6. Проверить `/семья база`.

## Только одна реплика

SQLite-версия рассчитана на один активный экземпляр Family Activity Bot. Не запускай две копии с одной БД и одним Discord token как «реплики».

## Backup

Локальные копии находятся в `/data/backups`. Они защищают от ошибок приложения/обновлений, но не от полной потери диска хостинга. Для настоящего production периодически копируй backup-файлы на отдельное внешнее хранилище/компьютер.

## Перед переносом

Сначала завершить Windows test-server smoke test:

- `/семья настройка` с vacation-role;
- public/staff panels;
- отпуск ролью: выдать → синхронизировать → снять;
- тестовая активность/check-in;
- weekly report;
- `/семья база`;
- restart с сохранением данных.
