# src/stock_bot/brokers/ib/buy_stocks.py

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

    Tries live data first; falls back to delayed-frozen (type 4) which
    requires no additional market-data subscription.

    Raises:
        ValueError: If IBKR cannot return a usable price.
    """
    def _usable(val) -> bool:
        return val is not None and not math.isnan(val) and val > 0

    # --- attempt 1: live data (type 1) ---
    ib.reqMarketDataType(1)
    (td,) = ib.reqTickers(contract)
    price = td.last if _usable(td.last) else td.close
    if _usable(price):
        return float(price)

    # --- attempt 2: delayed data (type 4) — populates same last/close fields ---
    logger.debug("Live price unavailable for %s — trying delayed", contract.symbol)
    ib.reqMarketDataType(4)
    (td,) = ib.reqTickers(contract)
    price = td.last if _usable(td.last) else td.close
    ib.reqMarketDataType(1)  # reset for subsequent calls
    if _usable(price):
        logger.info("Using delayed price for %s: %.4f", contract.symbol, price)
        return float(price)

    raise ValueError(
        f"Cannot resolve a market price for {contract.symbol}. "
        "Ensure the market is open or use limit_price explicitly."
    )


def _entry_order(action: str, shares: float, limit_price: Optional[float]) -> Order:
    """Build a MKT or LMT entry order (transmit=False by default)."""
    if limit_price is not None:
        return Order(
            action=action,
            orderType="LMT",
            totalQuantity=shares,
            lmtPrice=round(limit_price, 2),
            transmit=False,
        )
    return Order(
        action=action,
        orderType="MKT",
        totalQuantity=shares,
        transmit=False,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def buy_stock(
    ticker: str,
    ib: IB,
    *,
    shares: Optional[float] = None,
    dollar_amount: Optional[float] = None,
    limit_price: Optional[float] = None,
    stop_loss_pct: Optional[float] = None,
    take_profit_pct: Optional[float] = None,
    trailing_stop_pct: Optional[float] = None,
) -> list[Trade]:
    """Place a BUY order with optional risk-management exit orders.

    Exactly one of ``shares`` or ``dollar_amount`` must be supplied.
    Fractional shares are supported (e.g. ``shares=0.5``).

    Entry order type
    ----------------
    * LMT when ``limit_price`` is given, MKT otherwise.

    Risk management (attach at most one group)
    ------------------------------------------
    * ``stop_loss_pct`` **and** ``take_profit_pct`` → bracket order
      (OCO take-profit limit + stop-loss stop attached to the entry).
    * ``stop_loss_pct`` only → stop-loss stop order attached to entry.
    * ``take_profit_pct`` only → take-profit limit order attached to entry.
    * ``trailing_stop_pct`` → trailing-stop order attached to entry.
      Cannot be combined with stop_loss_pct / take_profit_pct.

    The reference price used to calculate stop / take-profit levels is
    ``limit_price`` when provided, otherwise the current last-traded price
    is fetched from IBKR.

    Args:
        ticker: Ticker symbol, e.g. ``'AAPL'``.
        ib: Active IB connection.
        shares: Exact number of shares (supports fractional, e.g. ``1.5``).
        dollar_amount: Dollar value to invest; share count is derived from the
            current market price.
        limit_price: Limit price for a LMT entry order.
        stop_loss_pct: Stop-loss distance as a % below the reference price
            (e.g. ``2.0`` → stop 2 % below entry).
        take_profit_pct: Take-profit distance as a % above the reference price
            (e.g. ``5.0`` → limit sell 5 % above entry).
        trailing_stop_pct: Trailing-stop distance as a % of price
            (e.g. ``1.5`` → 1.5 % trailing stop).  Mutually exclusive with
            ``stop_loss_pct`` / ``take_profit_pct``.

    Returns:
        List of Trade objects — entry first, followed by any attached exit
        orders.

    Raises:
        ValueError: On invalid argument combinations or when a market price
            cannot be resolved.
    """
    # --- Validate arguments ------------------------------------------------
    if (shares is None) == (dollar_amount is None):
        raise ValueError("Provide exactly one of 'shares' or 'dollar_amount'.")
    if trailing_stop_pct is not None and (
        stop_loss_pct is not None or take_profit_pct is not None
    ):
        raise ValueError(
            "'trailing_stop_pct' cannot be combined with "
            "'stop_loss_pct' or 'take_profit_pct'."
        )

    contract = _qualify(ticker, ib)

    # --- Resolve share quantity --------------------------------------------
    if dollar_amount is not None:
        price = _last_price(contract, ib)
        shares = dollar_amount / price
        logger.info(
            "Dollar amount $%.2f at $%.4f/share → %.4f shares of %s",
            dollar_amount,
            price,
            shares,
            ticker,
        )

    if shares <= 0:
        raise ValueError(f"Share count must be > 0, got {shares:.4f}.")

    # --- Resolve reference price for risk orders ---------------------------
    needs_ref = stop_loss_pct or take_profit_pct or trailing_stop_pct
    if needs_ref:
        ref_price = limit_price if limit_price is not None else _last_price(contract, ib)
    else:
        ref_price = None  # not needed

    # --- Build and place orders --------------------------------------------
    trades: list[Trade] = []
    has_bracket = stop_loss_pct is not None and take_profit_pct is not None

    if has_bracket:
        # Bracket: entry + OCO take-profit limit + stop-loss stop
        tp_price = round(ref_price * (1 + take_profit_pct / 100), 2)
        sl_price = round(ref_price * (1 - stop_loss_pct / 100), 2)

        # Parent entry order
        parent = _entry_order("BUY", shares, limit_price)
        parent_trade = ib.placeOrder(contract, parent)
        parent_id = parent_trade.order.orderId
        trades.append(parent_trade)

        # Take-profit LMT sell
        tp_order = Order(
            action="SELL",
            orderType="LMT",
            totalQuantity=shares,
            lmtPrice=tp_price,
            parentId=parent_id,
            transmit=False,
        )
        trades.append(ib.placeOrder(contract, tp_order))

        # Stop-loss STP sell (last child → transmit=True releases all)
        sl_order = Order(
            action="SELL",
            orderType="STP",
            totalQuantity=shares,
            auxPrice=sl_price,
            parentId=parent_id,
            transmit=True,
        )
        trades.append(ib.placeOrder(contract, sl_order))

        logger.info(
            "Bracket BUY %s x%.4f | entry=%s ref=%.2f TP=%.2f SL=%.2f",
            ticker,
            shares,
            f"LMT {limit_price:.2f}" if limit_price else "MKT",
            ref_price,
            tp_price,
            sl_price,
        )

    elif trailing_stop_pct is not None:
        parent = _entry_order("BUY", shares, limit_price)
        parent_trade = ib.placeOrder(contract, parent)
        trades.append(parent_trade)

        trail_order = Order(
            action="SELL",
            orderType="TRAIL",
            trailingPercent=trailing_stop_pct,
            totalQuantity=shares,
            parentId=parent_trade.order.orderId,
            transmit=True,
        )
        trades.append(ib.placeOrder(contract, trail_order))
        logger.info(
            "BUY %s x%.4f %s | trailing stop %.2f%%",
            ticker,
            shares,
            f"LMT {limit_price:.2f}" if limit_price else "MKT",
            trailing_stop_pct,
        )

    elif stop_loss_pct is not None:
        sl_price = round(ref_price * (1 - stop_loss_pct / 100), 2)
        parent = _entry_order("BUY", shares, limit_price)
        parent_trade = ib.placeOrder(contract, parent)
        trades.append(parent_trade)

        sl_order = Order(
            action="SELL",
            orderType="STP",
            totalQuantity=shares,
            auxPrice=sl_price,
            parentId=parent_trade.order.orderId,
            transmit=True,
        )
        trades.append(ib.placeOrder(contract, sl_order))
        logger.info(
            "BUY %s x%.4f %s | stop-loss at %.2f (%.1f%% below %.2f)",
            ticker,
            shares,
            f"LMT {limit_price:.2f}" if limit_price else "MKT",
            sl_price,
            stop_loss_pct,
            ref_price,
        )

    elif take_profit_pct is not None:
        tp_price = round(ref_price * (1 + take_profit_pct / 100), 2)
        parent = _entry_order("BUY", shares, limit_price)
        parent_trade = ib.placeOrder(contract, parent)
        trades.append(parent_trade)

        tp_order = Order(
            action="SELL",
            orderType="LMT",
            totalQuantity=shares,
            lmtPrice=tp_price,
            parentId=parent_trade.order.orderId,
            transmit=True,
        )
        trades.append(ib.placeOrder(contract, tp_order))
        logger.info(
            "BUY %s x%.4f %s | take-profit at %.2f (%.1f%% above %.2f)",
            ticker,
            shares,
            f"LMT {limit_price:.2f}" if limit_price else "MKT",
            tp_price,
            take_profit_pct,
            ref_price,
        )

    else:
        # Plain entry — transmit immediately
        plain = _entry_order("BUY", shares, limit_price)
        plain.transmit = True
        trade = ib.placeOrder(contract, plain)
        trades.append(trade)
        logger.info(
            "Buy order submitted — %s x%.4f %s | orderId=%s status=%s",
            ticker,
            shares,
            f"LMT {limit_price:.2f}" if limit_price else "MKT",
            trade.order.orderId,
            trade.orderStatus.status,
        )

    return trades


# ---------------------------------------------------------------------------
# Async counterparts — required when buy_stock is called from async main()
# ---------------------------------------------------------------------------

async def _qualify_async(ticker: str, ib: IB) -> Stock:
    """Create and qualify a stock contract asynchronously."""
    contract = Stock(ticker, ib_settings.exchange, ib_settings.currency)
    await ib.qualifyContractsAsync(contract)
    return contract


async def _last_price_async(contract: Stock, ib: IB) -> float:
    """Async version of _last_price.

    ib_insync's Ticker stores both live and delayed prices in the same
    `last`/`close` fields — there are no separate `delayedLast` attributes.
    When reqMarketDataType(4) is active, IBKR sends delayed ticks into those
    same fields.
    """
    def _usable(val) -> bool:
        return val is not None and not math.isnan(val) and val > 0

    # Attempt 1: live data
    ib.reqMarketDataType(1)
    (td,) = await ib.reqTickersAsync(contract)
    price = td.last if _usable(td.last) else td.close
    if _usable(price):
        return float(price)

    # Attempt 2: delayed data (populates same last/close fields)
    logger.debug("Live price unavailable for %s — trying delayed", contract.symbol)
    ib.reqMarketDataType(4)
    (td,) = await ib.reqTickersAsync(contract)
    price = td.last if _usable(td.last) else td.close
    ib.reqMarketDataType(1)  # reset
    if _usable(price):
        logger.info("Using delayed price for %s: %.4f", contract.symbol, price)
        return float(price)

    raise ValueError(
        f"Cannot resolve a market price for {contract.symbol}. "
        "Ensure the market is open or use limit_price explicitly."
    )


async def buy_stock_async(
    ticker: str,
    ib: IB,
    *,
    shares: Optional[float] = None,
    dollar_amount: Optional[float] = None,
    limit_price: Optional[float] = None,
    stop_loss_pct: Optional[float] = None,
    take_profit_pct: Optional[float] = None,
    trailing_stop_pct: Optional[float] = None,
) -> list[Trade]:
    """Async version of buy_stock — use this when calling from async main()."""
    if (shares is None) == (dollar_amount is None):
        raise ValueError("Provide exactly one of 'shares' or 'dollar_amount'.")
    if trailing_stop_pct is not None and (
        stop_loss_pct is not None or take_profit_pct is not None
    ):
        raise ValueError(
            "'trailing_stop_pct' cannot be combined with "
            "'stop_loss_pct' or 'take_profit_pct'."
        )

    contract = await _qualify_async(ticker, ib)

    if dollar_amount is not None:
        price = await _last_price_async(contract, ib)
        shares = dollar_amount / price
        logger.info(
            "Dollar amount $%.2f at $%.4f/share → %.4f shares of %s",
            dollar_amount, price, shares, ticker,
        )

    if shares <= 0:
        raise ValueError(f"Share count must be > 0, got {shares:.4f}.")

    needs_ref = stop_loss_pct or take_profit_pct or trailing_stop_pct
    if needs_ref:
        ref_price = limit_price if limit_price is not None else await _last_price_async(contract, ib)
    else:
        ref_price = None

    trades: list[Trade] = []
    has_bracket = stop_loss_pct is not None and take_profit_pct is not None

    if has_bracket:
        tp_price = round(ref_price * (1 + take_profit_pct / 100), 2)
        sl_price = round(ref_price * (1 - stop_loss_pct / 100), 2)
        parent = _entry_order("BUY", shares, limit_price)
        parent_trade = ib.placeOrder(contract, parent)
        parent_id = parent_trade.order.orderId
        trades.append(parent_trade)
        tp_order = Order(
            action="SELL", orderType="LMT", totalQuantity=shares,
            lmtPrice=tp_price, parentId=parent_id, transmit=False,
        )
        trades.append(ib.placeOrder(contract, tp_order))
        sl_order = Order(
            action="SELL", orderType="STP", totalQuantity=shares,
            auxPrice=sl_price, parentId=parent_id, transmit=True,
        )
        trades.append(ib.placeOrder(contract, sl_order))
        logger.info(
            "Bracket BUY %s x%.4f | entry=%s ref=%.2f TP=%.2f SL=%.2f",
            ticker, shares,
            f"LMT {limit_price:.2f}" if limit_price else "MKT",
            ref_price, tp_price, sl_price,
        )

    elif trailing_stop_pct is not None:
        parent = _entry_order("BUY", shares, limit_price)
        parent_trade = ib.placeOrder(contract, parent)
        trades.append(parent_trade)
        trail_order = Order(
            action="SELL", orderType="TRAIL", trailingPercent=trailing_stop_pct,
            totalQuantity=shares, parentId=parent_trade.order.orderId, transmit=True,
        )
        trades.append(ib.placeOrder(contract, trail_order))
        logger.info(
            "BUY %s x%.4f %s | trailing stop %.2f%%",
            ticker, shares,
            f"LMT {limit_price:.2f}" if limit_price else "MKT",
            trailing_stop_pct,
        )

    elif stop_loss_pct is not None:
        sl_price = round(ref_price * (1 - stop_loss_pct / 100), 2)
        parent = _entry_order("BUY", shares, limit_price)
        parent_trade = ib.placeOrder(contract, parent)
        trades.append(parent_trade)
        sl_order = Order(
            action="SELL", orderType="STP", totalQuantity=shares,
            auxPrice=sl_price, parentId=parent_trade.order.orderId, transmit=True,
        )
        trades.append(ib.placeOrder(contract, sl_order))
        logger.info(
            "BUY %s x%.4f %s | stop-loss at %.2f (%.1f%% below %.2f)",
            ticker, shares,
            f"LMT {limit_price:.2f}" if limit_price else "MKT",
            sl_price, stop_loss_pct, ref_price,
        )

    elif take_profit_pct is not None:
        tp_price = round(ref_price * (1 + take_profit_pct / 100), 2)
        parent = _entry_order("BUY", shares, limit_price)
        parent_trade = ib.placeOrder(contract, parent)
        trades.append(parent_trade)
        tp_order = Order(
            action="SELL", orderType="LMT", totalQuantity=shares,
            lmtPrice=tp_price, parentId=parent_trade.order.orderId, transmit=True,
        )
        trades.append(ib.placeOrder(contract, tp_order))
        logger.info(
            "BUY %s x%.4f %s | take-profit at %.2f (%.1f%% above %.2f)",
            ticker, shares,
            f"LMT {limit_price:.2f}" if limit_price else "MKT",
            tp_price, take_profit_pct, ref_price,
        )

    else:
        plain = _entry_order("BUY", shares, limit_price)
        plain.transmit = True
        trade = ib.placeOrder(contract, plain)
        trades.append(trade)
        logger.info(
            "Buy order submitted — %s x%.4f %s | orderId=%s status=%s",
            ticker, shares,
            f"LMT {limit_price:.2f}" if limit_price else "MKT",
            trade.order.orderId, trade.orderStatus.status,
        )

    return trades
