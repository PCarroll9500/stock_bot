#!/usr/bin/env python3
"""
scripts/open_market_liquidate.py

Run at market open before main.py to clear any leftover positions and
pending orders from the previous session (e.g. limit buys that filled
after the morning bot disconnected, stop-losses that never fired).

Cancels ALL open orders, then sells ALL open equity positions.
"""

import logging
import math
import sys

from stock_bot.core.logging_config import setup_logging
from stock_bot.brokers.ib.connect_disconnect import connect_ib, disconnect_ib
from stock_bot.config.settings import ib_settings

setup_logging()
logger = logging.getLogger(__name__)

FILL_WAIT_SECONDS = 30


def main() -> None:
    ib = connect_ib()
    if not ib.isConnected():
        logger.error("open_market_liquidate: failed to connect to IBKR -- aborting")
        sys.exit(1)

    # 1. Cancel all open orders
    try:
        open_orders = ib.reqOpenOrders()
        if not open_orders:
            logger.info("open_market_liquidate: no open orders to cancel")
        for trade in open_orders:
            symbol = getattr(trade.contract, "symbol", "?")
            order_type = getattr(trade.order, "orderType", "?")
            try:
                ib.cancelOrder(trade.order)
                logger.info("open_market_liquidate: cancelled %s order for %s", order_type, symbol)
            except Exception:
                logger.warning("open_market_liquidate: could not cancel order for %s", symbol, exc_info=True)
        if open_orders:
            ib.sleep(2)
    except Exception:
        logger.warning("open_market_liquidate: error fetching open orders -- continuing", exc_info=True)

    # 2. Sell all open equity positions
    from ib_insync import MarketOrder, Stock

    positions = [
        pos for pos in ib.positions(account=ib_settings.account)
        if pos.contract.secType == "STK" and float(pos.position) > 0
    ]

    if not positions:
        logger.info("open_market_liquidate: no open positions -- nothing to sell")
        disconnect_ib()
        return

    logger.info("open_market_liquidate: found %d open position(s) to sell", len(positions))
    sell_trades = []
    for pos in positions:
        symbol = pos.contract.symbol
        shares = math.floor(float(pos.position))
        if shares <= 0:
            continue
        try:
            contract = Stock(symbol, ib_settings.exchange, ib_settings.currency)
            ib.qualifyContracts(contract)
            order = MarketOrder("SELL", shares)
            trade = ib.placeOrder(contract, order)
            sell_trades.append((symbol, shares, trade))
            logger.info("open_market_liquidate: SELL %s x%d submitted", symbol, shares)
        except Exception:
            logger.error("open_market_liquidate: sell failed for %s", symbol, exc_info=True)

    # 3. Wait for fills
    logger.info("open_market_liquidate: waiting %ds for fills...", FILL_WAIT_SECONDS)
    ib.sleep(FILL_WAIT_SECONDS)

    # 4. Report results
    for symbol, shares, trade in sell_trades:
        status = getattr(trade, "orderStatus", None)
        if status and status.filled > 0:
            logger.info(
                "open_market_liquidate: SOLD %s -- %.0f shares @ $%.4f avg",
                symbol, status.filled, status.avgFillPrice,
            )
        else:
            logger.warning(
                "open_market_liquidate: SELL NOT CONFIRMED -- %s status=%s filled=%.0f",
                symbol,
                getattr(status, "status", "unknown") if status else "no status",
                getattr(status, "filled", 0) if status else 0,
            )

    disconnect_ib()
    logger.info("open_market_liquidate: done")


if __name__ == "__main__":
    main()
