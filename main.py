from __future__ import annotations

import logging
import logging.handlers

from familybot.bot import FamilyBot
from familybot.config import load_config


def configure_logging(level: int, log_path) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    formatter = logging.Formatter(
        "[{asctime}] [{levelname:<8}] {name}: {message}",
        "%Y-%m-%d %H:%M:%S",
        style="{",
    )
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
        encoding="utf-8",
        maxBytes=8 * 1024 * 1024,
        backupCount=3,
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)


def main() -> None:
    config = load_config()
    configure_logging(config.log_level, config.log_path)
    bot = FamilyBot(config)
    bot.run(config.token, log_handler=None)


if __name__ == "__main__":
    main()
