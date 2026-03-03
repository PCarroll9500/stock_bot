# src/stock_bot/brokers/ib/sell_stocks.py

import logging
import math
from typing import Optional

from ib_insync import IB, Order, Stock, Trade

from stock_bot.config.settings import ib_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _qualify(ticker: str, ib: IB) -> Stock:
    """Create and qualify a stock contract."""
    contract = Stock(ticker, ib_settings.exchange, ib_settings.currency)
    ib.qualifyContracts(contract)
    return contract


def _last_price(contract: Stock, ib: IB) -> float:
    """Return the last-traded (or closing) price for a qualified contract.

    Raises:
        ValueError: If IBKR cannot return a usable price.
    """
    (ticker_data,) = ib.reqTickers(contract)
    price = ticker_data.last or ticker_data.close
    if price is None or math.isnan(price):
        raise ValueError(
            f"Cannot resolve a market price for {contract.symbol}. "
            "Ensure the market is open or use limit_price explicitly."
        )
    return float(price)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def sell_stock(
    ticker: str,
    ib: IB,
    *,
    shares: float,
    limit_price: Optional[float] = None,
    stop_price: Optional[float] = None,
    stop_limit_price: Optional[float] = None,
    trailing_stop_pct: Optional[float] = None,
) -> Trade:
    """Place a SELL order for the given number of shares.

    Supports fractional shares (e.g. ``shares=0.5``).

    Order types
    -----------
    Pass at most one price / stop argument to select the order type:

    * **No extra args** → Market order (MKT). Fills immediately at best price.
    * **limit_price** → Limit order (LMT). Sells only at ``limit_price`` or
      better (higher). Use this as a take-profit.
    * **stop_price** → Stop order (STP). Converts to a market order once price
      falls to ``stop_price``. Classic stop-loss.
    * **stop_price** + **stop_limit_price** → Stop-Limit order (STP LMT).
      Converts to a limit order at ``stop_limit_price`` when ``stop_price`` is
      triggered.  Avoids market-order slippage on the stop, but may not fill if
      price gaps through ``stop_limit_price``.
    * **trailing_stop_pct** → Trailing Stop (TRAIL %). The stop level rises
      automatically with the price; sells if the price falls by
      ``trailing_stop_pct`` % from its peak.

    Args:
        ticker: Ticker symbol, e.g. ``'AAPL'``.
        ib: Active IB connection.
        shares: Number of shares to sell (supports fractional).
        limit_price: Limit price for a LMT sell (take-profit style).
        stop_price: Trigger price for STP or STP LMT orders.
        stop_limit_price: Limit price used once ``stop_price`` is triggered
            (STP LMT only).  Must be <= ``stop_price`` for a sell.
        trailing_stop_pct: Trailing distance as a % of price
            (e.g. ``1.5`` → 1.5 % trailing stop).

    Returns:
        Trade object for the submitted order.

    Raises:
        ValueError: On invalid argument combinations or share count.
    """
    shares = math.floor(shares)
    if shares <= 0:
        raise ValueError(f"shares must be > 0, got {shares}.")

    # Validate mutually exclusive price arguments
    price_args = {
        "limit_price": limit_price,
        "stop_price": stop_price,
        "trailing_stop_pct": trailing_stop_pct,
    }
    provided = [k for k, v in price_args.items() if v is not None]
    # stop_limit_price is only valid alongside stop_price
    if stop_limit_price is not None and stop_price is None:
        raise ValueError("'stop_limit_price' requires 'stop_price' to be set.")
    if len(provided) > 1 and not (
        len(provided) == 2
        and "stop_price" in provided
        and stop_limit_price is not None
        and "limit_price" not in provided
        and "trailing_stop_pct" not in provided
    ):
        raise ValueError(
            "Provide at most one of: limit_price, stop_price [+ stop_limit_price], "
            "or trailing_stop_pct."
        )

    contract = _qualify(ticker, ib)

    # --- Build order -------------------------------------------------------
    if limit_price is not None:
        order = Order(
            action="SELL",
            orderType="LMT",
            totalQuantity=shares,
            lmtPrice=round(limit_price, 2),
            transmit=True,
        )
        logger.info("SELL LMT %s x%.4f @ %.2f", ticker, shares, limit_price)

    elif stop_price is not None and stop_limit_price is not None:
        order = Order(
            action="SELL",
            orderType="STP LMT",
            totalQuantity=shares,
            auxPrice=round(stop_price, 2),
            lmtPrice=round(stop_limit_price, 2),
            transmit=True,
        )
        logger.info(
            "SELL STP LMT %s x%.4f | stop=%.2f limit=%.2f",
            ticker,
            shares,
            stop_price,
            stop_limit_price,
        )

    elif stop_price is not None:
        order = Order(
            action="SELL",
            orderType="STP",
            totalQuantity=shares,
            auxPrice=round(stop_price, 2),
            transmit=True,
        )
        logger.info("SELL STP %s x%.4f @ stop=%.2f", ticker, shares, stop_price)

    elif trailing_stop_pct is not None:
        order = Order(
            action="SELL",
            orderType="TRAIL",
            trailingPercent=trailing_stop_pct,
            totalQuantity=shares,
            transmit=True,
        )
        logger.info(
            "SELL TRAIL %s x%.4f | trailing=%.2f%%", ticker, shares, trailing_stop_pct
        )

    else:
        order = Order(
            action="SELL",
            orderType="MKT",
            totalQuantity=shares,
            transmit=True,
        )
        logger.info("SELL MKT %s x%.4f", ticker, shares)

    trade = ib.placeOrder(contract, order)
    logger.info(
        "Sell order submitted — %s x%.4f | orderId=%s status=%s",
        ticker,
        shares,
        trade.order.orderId,
        trade.orderStatus.status,
    )
    return trade
