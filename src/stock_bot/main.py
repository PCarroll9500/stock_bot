# src/stock_bot/main.py

import json
import logging
import sys
from pathlib import Path

from stock_bot.core.logging_config import setup_logging
from stock_bot.config.settings import ib_settings
from stock_bot.brokers.ib.connect_disconnect import connect_ib, disconnect_ib
from stock_bot.data_sources.get_list_all_stocks import get_list_all_stocks
from stock_bot.data_sources.trend_checker import passes_trend_filters
from stock_bot.ai.stock_picker import get_stocks

_CONFIG_DIR = Path(__file__).parent / "config"

MAX_REPLACEMENT_ATTEMPTS = 30


def _load_picker_config() -> dict:
    """Load run settings from config/picker_config.json."""
    path = _CONFIG_DIR / "picker_config.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logging.getLogger(__name__).warning(
            "Could not load picker_config.json — using defaults"
        )
        return {}


def main():
    setup_logging()
    logger = logging.getLogger(__name__)

    logger.info(
        "Starting stock bot (mode=%s, host=%s, port=%s, client_id=%s)",
        ib_settings.mode,
        ib_settings.host,
        ib_settings.port,
        ib_settings.client_id,
    )

    config = _load_picker_config()
    excluded_tickers = config.get("always_exclude", [])
    num_stocks = config.get("num_stocks", 10)
    trend_filters = config.get("trend_filters") or None

    logger.info("Loaded %d statically excluded tickers", len(excluded_tickers))

    try:
        valid_symbols = get_list_all_stocks()["symbol"].tolist()
        logger.info("Retrieved %d valid stock symbols", len(valid_symbols))
    except Exception:
        logger.exception("Error retrieving stock symbols")
        valid_symbols = []

    ib = connect_ib()

    if ib.isConnected():
        logger.info("Connected to IBKR")
    else:
        logger.error("Failed to connect to IBKR")
        disconnect_ib()
        sys.exit(1)

    # TODO: merge current IBKR holdings into excluded_tickers so the picker
    #       never re-buys a stock already held in the account.
    #       e.g. held = [p.contract.symbol for p in ib.positions()]
    #            excluded_tickers = list(set(excluded_tickers) | set(held))

    # Initial pick
    picks = get_stocks(num_stocks, valid_symbols, excluded_tickers)

    # Trend-check each pick; replace failures until num_stocks are validated
    # or the replacement attempt cap is hit (prevents infinite loops if the
    # market is so filtered that no stocks pass).
    if trend_filters:
        session_excluded = set(excluded_tickers)
        validated = []
        pending = list(picks)
        replacement_attempts = 0

        while pending and replacement_attempts < MAX_REPLACEMENT_ATTEMPTS:
            pick = pending.pop(0)
            if passes_trend_filters(pick["ticker"], ib, trend_filters):
                validated.append(pick)
            else:
                logger.info("Trend filter rejected %s — fetching replacement", pick["ticker"])
                session_excluded.add(pick["ticker"])
                replacement_attempts += 1
                replacement = get_stocks(1, valid_symbols, list(session_excluded))
                if replacement:
                    pending.append(replacement[0])  # replacement also goes through trend check

        if len(validated) < num_stocks:
            logger.warning(
                "Could only validate %d/%d picks after trend filtering",
                len(validated), num_stocks,
            )

        picks = validated

    logger.info("Final picks (%d):", len(picks))
    for pick in picks:
        logger.info("  %s — %s", pick["ticker"], pick["reason"])

    # TODO: execute trades via IBKR for each pick

    disconnect_ib()
    logger.info("Disconnected from IBKR. Shutdown complete.")


if __name__ == "__main__":
    main()
