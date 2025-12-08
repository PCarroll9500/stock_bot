# src/stock_bot/config/settings.py
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class IBSettings:
    mode: str
    host: str
    port: int
    account: str
    client_id: int
    exchange: str
    currency: str


@dataclass
class LoggingSettings:
    level: str
    file: str


def load_ib_settings() -> IBSettings:
    mode = os.getenv("IB_MODE", "paper").lower()

    if mode == "live":
        port = int(os.getenv("IB_PORT_LIVE", "4001"))
        account = os.getenv("IB_ACCOUNT_LIVE", "")
    else:
        port = int(os.getenv("IB_PORT_PAPER", "4002"))
        account = os.getenv("IB_ACCOUNT_PAPER", "")

    return IBSettings(
        mode=mode,
        host=os.getenv("IB_HOST", "127.0.0.1"),
        port=port,
        account=account,
        client_id=int(os.getenv("IB_CLIENT_ID", "1")),
        exchange=os.getenv("IB_EXCHANGE", "SMART"),
        currency=os.getenv("IB_CURRENCY", "USD"),
    )


def load_logging_settings() -> LoggingSettings:
    return LoggingSettings(
        level=os.getenv("LOG_LEVEL", "WARNING").upper(),
        file=os.getenv("LOG_FILE", "logs/stock_bot.log"),
    )


ib_settings = load_ib_settings()
logging_settings = load_logging_settings()