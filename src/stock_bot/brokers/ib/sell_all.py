# src/stock_bot/brokers/ib/sell_all.py

import logging
import math
from typing import Optional

from ib_insync import IB, MarketOrder, Order, Stock, Trade

from stock_bot.config.settings import ib_settings
from stock_bot.brokers.ib.sell_stocks import sell_stock

logger = logging.getLogger(__name__)


def close_position(ticker: str, ib: IB) -> Optional[Trade]:
    """Close a long position in ``ticker``.

    Long (position > 0) → SELL market order
    Flat (position == 0) → returns None, no order placed
    Short (position < 0) → logs an error and returns None (shorts must not exist)
    """
    positions = ib.positions(account=ib_settings.account)
    for pos in positions:
        if pos.contract.symbol == ticker and pos.contract.secType == "STK":
            size = float(pos.position)
            if size < 0:
                logger.error(
                    "Short position detected for %s (%.0f shares) — this should never happen "
                    "in a cash-only account. Manual intervention required.",
                    ticker, size,
                )
                return None
            shares = math.floor(size)
            if shares == 0:
                logger.info("Position for %s is already flat — no order placed", ticker)
                return None
            logger.info("Closing long position: SELL %s x%d shares", ticker, shares)
            contract = Stock(ticker, ib_settings.exchange, ib_settings.currency)
            ib.qualifyContracts(contract)
            order = MarketOrder("SELL", shares)
            return ib.placeOrder(contract, order)
    logger.warning("No open position found for %s — no order placed", ticker)
    return None


def sell_all_stock(
    ticker: str,
    ib: IB,
    *,
    limit_price: Optional[float] = None,
) -> Optional[Trade]:
    """Sell the entire open position in ``ticker``.

    Looks up the current account position and places a SELL order for all
    shares held.  Returns ``None`` without placing any order when no open
    position exists or the position is flat / short.

    Order type
    ----------
    * **No limit_price** → Market order (MKT). Fastest exit, fills at best
      available price.
    * **limit_price** → Limit order (LMT). Will only fill at ``limit_price``
      or better; use when you want a minimum exit price.

    Args:
        ticker: Ticker symbol, e.g. ``'AAPL'``.
        ib: Active IB connection.
        limit_price: Optional minimum sell price (LMT order).  When omitted a
            market order is used.

    Returns:
        Trade object if a sell order was placed, ``None`` if no position was
        found.
    """
    logger.info("Fetching positions to sell all %s", ticker)
    positions = ib.positions(account=ib_settings.account)

    for pos in positions:
        if pos.contract.symbol == ticker and pos.contract.secType == "STK":
            shares = math.floor(float(pos.position))
            if shares <= 0:
                logger.warning(
                    "Position for %s is %.4f (non-positive) — no order placed",
                    ticker,
                    shares,
                )
                return None

            logger.info("Closing entire position: %s x%.4f shares", ticker, shares)
            return sell_stock(ticker, ib, shares=shares, limit_price=limit_price)

    logger.warning("No open position found for %s — no order placed", ticker)
    return None
