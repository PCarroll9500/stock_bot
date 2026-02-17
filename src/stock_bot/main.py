# src/stock_bot/main.py

import logging

from stock_bot.core.logging_config import setup_logging
from stock_bot.config.settings import ib_settings
from stock_bot.brokers.ib.connect_disconnect import connect_ib, disconnect_ib
from stock_bot.data_sources.get_list_all_stocks import get_list_all_stocks

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

        # Get list of all stocks available on the NASDAQ
    try:
        symbols_data = get_list_all_stocks()
        # Defensive: ensure we can index "symbol"
        valid_symbols = symbols_data.get("symbol") if isinstance(symbols_data, dict) else symbols_data["symbol"]
        logger.info("Retrieved %d valid stock symbols from NASDAQ", len(valid_symbols))
    except Exception as e:
        # Log full exception and continue with empty list to avoid NameError later
        logger.exception("Error retrieving stock symbols")
        valid_symbols = []

    ib = connect_ib()

    if ib.isConnected():
        logger.info("Connected to IBKR")
    else:
        logger.error("Failed to connect to IBKR")

    # TODO: run strategies, loops, etc.

    disconnect_ib()
    logger.info("Disconnected from IBKR. Shutdown complete.")


if __name__ == "__main__":
    main()
