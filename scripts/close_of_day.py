#!/usr/bin/env python3
"""
scripts/close_of_day.py

Run at 2:30 PM ET via cron to lock in profits ~90 min before close.
Records close prices, computes session returns, updates portfolio.json.

Cron entry (EC2, UTC — see run_close.sh):
  30 18 * * 1-5  (18:30 UTC = 2:30 PM EDT / 1:30 PM EST)
"""

import argparse
import json
import logging
import sys
import time
from datetime import date as date_type
from pathlib import Path

from ib_insync import Trade

# Package is installed in the venv via `pip install -e .`
from stock_bot.core.logging_config import setup_logging
from stock_bot.brokers.ib.connect_disconnect import connect_ib, disconnect_ib
from stock_bot.brokers.ib.sell_all import close_position
from stock_bot.data_sources.portfolio_writer import (
    load_portfolio,
    save_portfolio,
    get_net_liquidation,
    get_live_account_value,
    _get_last_price,
    _get_nasdaq_price,
)

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "src" / "stock_bot" / "config" / "picker_config.json"
_RETRY_INTERVAL_S = 30
_RETRY_TIMEOUT_S = 600  # 10 minutes


def _retry_ibkr(fn, label: str, ib, connect_fn, logger, *, interval=_RETRY_INTERVAL_S, timeout=_RETRY_TIMEOUT_S):
    """Call fn(ib) repeatedly until it returns a non-None value or timeout expires.

    Attempts reconnection on each retry if IBKR is disconnected.
    Returns (result, ib) so callers always have the live connection reference.
    Returns (None, ib) if timeout is reached.
    """
    deadline = time.monotonic() + timeout
    attempt = 0
    while True:
        attempt += 1
        result = fn(ib)
        if result is not None:
            return result, ib
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.error(
                "close_of_day: %s still unavailable after %d s — giving up",
                label, timeout,
            )
            return None, ib
        logger.warning(
            "close_of_day: %s unavailable (attempt %d) — retrying in %d s (%.0f s remaining)",
            label, attempt, interval, remaining,
        )
        time.sleep(interval)
        if not ib.isConnected():
            try:
                ib = connect_fn()
                logger.info("close_of_day: reconnected to IBKR for retry")
            except Exception:
                logger.warning("close_of_day: reconnect failed — will retry anyway")


def _load_config() -> dict:
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main() -> None:
    parser = argparse.ArgumentParser(description="Close of day price updater")
    parser.add_argument(
        "--test",
        action="store_true",
        help="Test mode: read/write portfolio_test.json instead of portfolio.json",
    )
    args = parser.parse_args()
    test_mode: bool = args.test

    setup_logging()
    logger = logging.getLogger(__name__)

    config = _load_config()
    sell_wait_seconds: int = config.get("sell_wait_seconds", 45)

    if test_mode:
        logger.info("*** TEST MODE — using portfolio_test.json ***")

    today = date_type.today().isoformat()

    portfolio = load_portfolio(test_mode=test_mode)
    sessions = portfolio.get("sessions", [])
    session = next((s for s in sessions if s.get("date") == today), None)

    if not session:
        logger.warning("close_of_day: no session found for %s — nothing to close", today)
        return

    if session.get("portfolio_close_value") is not None:
        logger.info("close_of_day: session for %s already closed — skipping", today)
        return

    if session.get("no_picks"):
        logger.info("close_of_day: no-picks day — skipping liquidation, close value left as null")
        return

    # Picks with hold_until > today are multi-day holds — skip selling them.
    # Check ALL sessions so holds from previous days are respected.
    held_tickers: set[str] = set()
    for _s in portfolio.get("sessions", []):
        for _p in _s.get("picks", []):
            _hold = _p.get("hold_until")
            if _hold and _hold > today:
                held_tickers.add(_p["ticker"])
    if held_tickers:
        logger.info("close_of_day: multi-day holds (skipping sell today): %s", sorted(held_tickers))

    ib = connect_ib()
    if not ib.isConnected():
        logger.error("close_of_day: failed to connect to IBKR")
        sys.exit(1)

    logger.info("close_of_day: updating session for %s", today)

    # Cancel ALL open orders (stop-losses, limit buys, child orders) before
    # selling so nothing fires after positions are flat.
    try:
        open_orders = ib.reqOpenOrders()
        cancelled = 0
        for trade in open_orders:
            symbol = getattr(trade.contract, "symbol", "?")
            # Keep stop/trail orders alive for multi-day holds
            if symbol in held_tickers and getattr(trade.order, "orderType", "") in ("STP", "TRAIL"):
                logger.info("close_of_day: keeping %s order for multi-day hold %s", trade.order.orderType, symbol)
                continue
            try:
                ib.cancelOrder(trade.order)
                cancelled += 1
                logger.info("close_of_day: cancelling open order for %s (%s)", symbol, trade.order.orderType)
            except Exception:
                logger.warning("close_of_day: could not cancel order for %s", symbol, exc_info=True)
        if cancelled:
            logger.info("close_of_day: cancelled %d open orders before liquidation", cancelled)
            ib.sleep(2)  # brief pause for cancellations to propagate
        else:
            logger.info("close_of_day: no open orders to cancel")
    except Exception:
        logger.warning("close_of_day: failed to fetch/cancel open orders -- proceeding anyway", exc_info=True)

    # Liquidate ALL actual IBKR positions -- source of truth is the account,
    # not portfolio.json (which may be stale for late-filling limit orders).
    from stock_bot.config.settings import ib_settings as _ib_settings
    logger.info("close_of_day: liquidating all positions")
    sell_trades: dict[str, Trade] = {}
    live_positions = [
        pos for pos in ib.positions(account=_ib_settings.account)
        if pos.contract.secType == "STK" and float(pos.position) > 0
    ]
    if not live_positions:
        logger.info("close_of_day: no open positions to sell")
    for pos in live_positions:
        ticker = pos.contract.symbol
        shares = float(pos.position)
        if ticker in held_tickers:
            logger.info("close_of_day: skipping sell for multi-day hold %s x%.0f", ticker, shares)
            continue
        logger.info("close_of_day: selling %s x%.0f shares", ticker, shares)
        try:
            trade = close_position(ticker, ib)
            if trade is not None:
                sell_trades[ticker] = trade
            else:
                logger.warning("close_of_day: close_position returned None for %s -- already flat?", ticker)
        except Exception:
            logger.error("close_of_day: sell failed for %s", ticker, exc_info=True)
    logger.info("close_of_day: waiting %d s for sell orders to fill…", sell_wait_seconds)
    try:
        ib.sleep(sell_wait_seconds)  # allow market orders to fill
    except Exception as e:
        logger.warning("close_of_day: lost IB connection during wait (%s) — attempting reconnect", e)
        try:
            ib = connect_ib()
            logger.info("close_of_day: reconnected to IBKR")
        except Exception:
            logger.warning("close_of_day: reconnect failed — proceeding with cached order status")

    # Verify sells -- log confirmation for every sell order placed
    for ticker, trade in sell_trades.items():
        status = getattr(trade, "orderStatus", None)
        if status and status.filled > 0:
            logger.info(
                "close_of_day: SOLD %s -- %.0f shares @ $%.4f avg",
                ticker, status.filled, status.avgFillPrice,
            )
        else:
            logger.warning(
                "close_of_day: SELL NOT CONFIRMED -- %s status=%s filled=%.0f",
                ticker,
                getattr(status, "status", "unknown") if status else "no status",
                getattr(status, "filled", 0) if status else 0,
            )
    # Note any picks whose stop-loss fired intraday (no position at close)
    sold_tickers = set(sell_trades.keys())
    for pick in session.get("picks", []):
        if pick.get("shares", 0) > 0 and pick["ticker"] not in sold_tickers:
            logger.info("close_of_day: %s -- stop-loss likely fired intraday", pick["ticker"])

    # Build a map of intraday sell prices from IBKR execution history.
    # Used as a fallback for picks whose stop-loss fired intraday.
    exec_map: dict[str, float] = {}
    try:
        executions = ib.reqExecutions()
        _fills_by_ticker: dict[str, list[tuple[float, float]]] = {}
        for _fill in executions:
            if _fill.execution.side == "SLD":
                _sym = _fill.contract.symbol
                _fills_by_ticker.setdefault(_sym, []).append(
                    (_fill.execution.shares, _fill.execution.price)
                )
        for _sym, _fills in _fills_by_ticker.items():
            _total = sum(s for s, _ in _fills)
            if _total > 0:
                exec_map[_sym] = round(sum(s * p for s, p in _fills) / _total, 4)
        if exec_map:
            logger.info("close_of_day: execution map built for %d ticker(s): %s",
                        len(exec_map), list(exec_map.keys()))
    except Exception:
        logger.warning("close_of_day: could not fetch executions for stop-loss price lookup", exc_info=True)

    # Record per-pick close prices for individual P&L tracking
    for pick in session.get("picks", []):
        ticker = pick["ticker"]
        if ticker in held_tickers:
            logger.info("close_of_day: skipping P&L for multi-day hold %s", ticker)
            continue
        buy_price = pick.get("buy_price", 0)
        shares = pick.get("shares", 0)

        # Try actual sell fill price first (no retry needed — already in memory)
        close_price: float | None = None
        trade = sell_trades.get(ticker)
        sell_status = getattr(trade, "orderStatus", None) if trade else None
        if sell_status and sell_status.filled > 0:
            close_price = float(sell_status.avgFillPrice)

        # Try intraday execution price (stop-loss fills)
        if close_price is None and ticker in exec_map:
            close_price = exec_map[ticker]
            pick["stop_loss_triggered"] = True
            logger.info("close_of_day: %s stop-loss triggered intraday @ %.4f", ticker, close_price)

        # Fall back to last bar price with retry
        if close_price is None:
            close_price, ib = _retry_ibkr(
                lambda _ib: _get_last_price(ticker, _ib),
                f"last price for {ticker}", ib, connect_ib, logger,
            )

        if close_price and buy_price > 0 and shares > 0:
            day_return_usd = (close_price - buy_price) * shares
            day_return_pct = (close_price - buy_price) / buy_price * 100
            pick["close_price"] = close_price
            pick["day_return_pct"] = round(day_return_pct, 3)
            pick["day_return_usd"] = round(day_return_usd, 2)
            logger.info(
                "close_of_day: %s close=%.4f return=%.2f%% ($%.2f)",
                ticker, close_price, day_return_pct, day_return_usd,
            )
        else:
            logger.warning("close_of_day: %s — no close price, per-pick P&L not recorded", ticker)

    # Use actual IBKR account value as portfolio_close_value — the ground truth
    # after all positions are liquidated, rather than summing estimated fill prices.
    actual_close_value, ib = _retry_ibkr(get_net_liquidation, "NetLiquidation", ib, connect_ib, logger)
    if actual_close_value is not None:
        logger.info("close_of_day: using IBKR NetLiquidation $%.2f as portfolio_close_value", actual_close_value)
        total_close_value = actual_close_value
        session.pop("close_value_error", None)
    else:
        # NetLiquidation failed — mark as ERROR, do not store a calculated guess
        logger.error("close_of_day: NetLiquidation unavailable after retries — portfolio_close_value set to ERROR")
        total_close_value = None

    # Record actual CashBalance from IBKR — ground truth for uninvested cash
    cash_close = get_live_account_value(ib)
    if cash_close is not None:
        session["cash_balance_close"] = round(cash_close, 2)
        logger.info("close_of_day: CashBalance at close = $%.2f", cash_close)
    else:
        logger.warning("close_of_day: CashBalance unavailable at close")

    if total_close_value is not None:
        session["portfolio_close_value"] = round(total_close_value, 2)
        session["session_return_usd"] = round(total_close_value - session["portfolio_open_value"], 2)
        open_val = session["portfolio_open_value"]
        session["session_return_pct"] = (
            round((total_close_value - open_val) / open_val * 100, 3) if open_val > 0 else 0
        )
    else:
        session["portfolio_close_value"] = "ERROR"
        session["session_return_usd"] = "ERROR"
        session["session_return_pct"] = "ERROR"

    # QQQ close price
    qqq_close, ib = _retry_ibkr(
        lambda _ib: _get_last_price("QQQ", _ib),
        "QQQ close price", ib, connect_ib, logger,
    )
    qqq_buy = session.get("qqq_buy_price")
    if qqq_close and qqq_buy and qqq_buy > 0:
        session["qqq_close_price"] = qqq_close
        session["qqq_day_return_pct"] = round((qqq_close - qqq_buy) / qqq_buy * 100, 3)

    nasdaq_close = _get_nasdaq_price()
    nasdaq_buy = session.get("nasdaq_buy_price")
    if nasdaq_close and nasdaq_buy and nasdaq_buy > 0:
        session["nasdaq_close_price"] = nasdaq_close
        session["nasdaq_day_return_pct"] = round((nasdaq_close - nasdaq_buy) / nasdaq_buy * 100, 3)
        logger.info("close_of_day: NASDAQ %.4f -> %.4f (%.3f%%)",
                    nasdaq_buy, nasdaq_close, session["nasdaq_day_return_pct"])

    # Update equity curve with close value
    initial_investment = float(portfolio.get("initial_investment", 10_000))
    initial_qqq = sessions[0].get("qqq_buy_price") if sessions else None
    qqq_indexed = (
        round((qqq_close / initial_qqq) * initial_investment, 2)
        if (initial_qqq and qqq_close and initial_qqq > 0) else initial_investment
    )

    equity_curve = portfolio.get("equity_curve", [])
    if total_close_value is not None:
        eq_point = {
            "date": today,
            "portfolio_value": round(total_close_value, 2),
            "qqq_indexed": qqq_indexed,
        }
        eq_idx = next((i for i, e in enumerate(equity_curve) if e.get("date") == today), None)
        if eq_idx is not None:
            equity_curve[eq_idx] = eq_point
        else:
            equity_curve.append(eq_point)
        portfolio["equity_curve"] = equity_curve

    try:
        disconnect_ib()
    except Exception:
        pass
    save_portfolio(portfolio, test_mode=test_mode)

    logger.info(
        "close_of_day: done — portfolio %s (%s%%) | QQQ %+.2f%%",
        f"${total_close_value:.2f}" if total_close_value is not None else "ERROR",
        session["session_return_pct"],
        session.get("qqq_day_return_pct") or 0,
    )


if __name__ == "__main__":
    main()
