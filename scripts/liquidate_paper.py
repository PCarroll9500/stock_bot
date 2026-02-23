"""One-shot script: sell every open position on the paper trading account."""

import sys
import time

sys.path.insert(0, "src")  # allow running from repo root without install

from ib_insync import IB, MarketOrder, Stock

from stock_bot.config.settings import ib_settings
from stock_bot.core.logging_config import setup_logging
from stock_bot.brokers.ib.connect_disconnect import connect_ib, disconnect_ib

import logging

setup_logging()
logger = logging.getLogger(__name__)


def liquidate_all() -> None:
    if ib_settings.mode != "paper":
        logger.error(
            "IB_MODE is '%s', not 'paper'. Refusing to run. "
            "Set IB_MODE=paper in your .env file.",
            ib_settings.mode,
        )
        sys.exit(1)

    ib: IB = connect_ib()

    positions = ib.positions(account=ib_settings.account)
    long_positions = [
        p for p in positions if p.contract.secType == "STK" and p.position > 0
    ]

    if not long_positions:
        logger.info("No open long positions found — nothing to sell.")
        disconnect_ib()
        return

    logger.info("Found %d position(s) to liquidate:", len(long_positions))
    for p in long_positions:
        logger.info("  %s  x%.4f shares", p.contract.symbol, p.position)

    trades = []
    for pos in long_positions:
        symbol = pos.contract.symbol
        shares = float(pos.position)

        contract = Stock(symbol, ib_settings.exchange, ib_settings.currency)
        ib.qualifyContracts(contract)

        order = MarketOrder("SELL", shares)
        trade = ib.placeOrder(contract, order)
        trades.append((symbol, shares, trade))
        logger.info("Sell order submitted: %s x%.4f | orderId=%s", symbol, shares, trade.order.orderId)

    # Give orders a moment to acknowledge
    logger.info("Waiting for order acknowledgements...")
    ib.sleep(3)

    logger.info("--- Liquidation summary ---")
    all_ok = True
    for symbol, shares, trade in trades:
        status = trade.orderStatus.status
        filled = trade.orderStatus.filled
        avg_fill = trade.orderStatus.avgFillPrice
        logger.info(
            "  %-8s x%.4f  status=%-12s  filled=%.4f  avgPrice=%.4f",
            symbol, shares, status, filled, avg_fill,
        )
        if status not in ("Filled", "Submitted", "PreSubmitted"):
            all_ok = False
            logger.warning("  ^ unexpected status for %s", symbol)

    if all_ok:
        logger.info("All sell orders submitted successfully.")
    else:
        logger.warning("Some orders may have issues — check TWS for details.")

    disconnect_ib()


if __name__ == "__main__":
    liquidate_all()
