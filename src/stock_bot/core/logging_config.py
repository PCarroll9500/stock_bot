# src/stock_bot/core/logging_config.py

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from stock_bot.config.settings import logging_settings


def setup_logging() -> None:
    """
    Configure root logging for the entire app.
    Should be called once at startup (e.g. in main()).
    """
    logger = logging.getLogger()

    # If handlers already exist, don't add duplicates
    if logger.handlers:
        return

    level_name = logging_settings.level
    level = getattr(logging, level_name, logging.WARNING)
    logger.setLevel(level)

    # Ensure log directory exists
    log_path = Path(logging_settings.file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )

    # Console handler (stdout) - good for dev & Docker/server logs
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # Rotating file handler for history on server
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=5_000_000,  # ~5 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
