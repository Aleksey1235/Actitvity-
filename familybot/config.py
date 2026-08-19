from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True, slots=True)
class Config:
    token: str
    database_path: Path
    backup_dir: Path
    backup_interval_hours: int
    backup_retention_days: int
    log_path: Path
    log_level: int
    dev_guild_id: int | None


def _positive_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}")
    return value


def load_config() -> Config:
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN is missing. Copy .env.example to .env and set the bot token."
        )

    database_path = Path(
        os.getenv("DATABASE_PATH", "data/family_activity.db")
    ).expanduser()
    backup_dir = Path(
        os.getenv("BACKUP_DIR", "data/backups")
    ).expanduser()

    log_path = Path(os.getenv("LOG_PATH", "data/logs/family_activity.log")).expanduser()
    level_name = os.getenv("LOG_LEVEL", "INFO").upper().strip()
    log_level = getattr(logging, level_name, logging.INFO)

    dev_raw = os.getenv("DEV_GUILD_ID", "").strip()
    try:
        dev_guild_id = int(dev_raw) if dev_raw else None
    except ValueError as exc:
        raise RuntimeError("DEV_GUILD_ID must be a Discord server ID") from exc

    return Config(
        token=token,
        database_path=database_path,
        backup_dir=backup_dir,
        backup_interval_hours=_positive_int("BACKUP_INTERVAL_HOURS", 6),
        backup_retention_days=_positive_int("BACKUP_RETENTION_DAYS", 30),
        log_path=log_path,
        log_level=log_level,
        dev_guild_id=dev_guild_id,
    )
