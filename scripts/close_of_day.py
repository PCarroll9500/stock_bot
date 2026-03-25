#!/usr/bin/env python3
"""
scripts/close_of_day.py

Run at market close (4:05 PM ET) via cron to record end-of-day prices,
compute session returns, and update the equity curve in portfolio.json.

Cron entry (add with: crontab -e):
  5 16 * * 1-5 cd /home/patrick/dev/github/stock_bot && .venv/bin/python scripts/close_of_day.py >> logs/close_of_day.log 2>&1
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
from stock_bot.brokers.ib.sell_all import sell_all_stock, close_position
from stock_bot.data_sources.portfolio_writer import (
    load_portfolio,
    save_portfolio,
    get_net_liquidation,
    _get_last_price,
)

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "src" / "stock_bot" / "config" / "picker_config.json"
_RETRY_INTERVAL_S = 30
_RETRY_TIMEOUT_S = 600  # 10 minutes


def _retry_ibkr(fn, label: str, ib, connect_fn, logger, *, interval=_RETRY_INTERVAL_S, timeout=_RETRY_TIMEOUT_S):
    """Call fn(ib) repeatedly until it returns a non-None value or timeout expires.

    Attempts reconnection on each retry if IBKR is disconnected.
    Logs an error and returns None if timeout is reached.
    """
    deadline = time.monotonic() + timeout
    attempt = 0
    while True:
        attempt += 1
        result = fn(ib)
        if result is not None:
            return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.error(
                "close_of_day: %s still unavailable after %d s — giving up",
                label, timeout,
            )
            return None
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

    ib = connect_ib()
    if not ib.isConnected():
        logger.error("close_of_day: failed to connect to IBKR")
        sys.exit(1)

    logger.info("close_of_day: updating session for %s", today)

    # Liquidate all open positions, collecting Trade objects for verification
    logger.info("close_of_day: liquidating all positions")
    sell_trades: dict[str, Trade] = {}
    for pick in session.get("picks", []):
        if pick.get("shares", 0) > 0:
            try:
                trade = sell_all_stock(pick["ticker"], ib)
                if trade is not None:
                    sell_trades[pick["ticker"]] = trade
                else:
                    logger.warning(
                        "close_of_day: no open position found for %s — sell skipped",
                        pick["ticker"],
                    )
            except Exception:
                logger.error("close_of_day: sell failed for %s", pick["ticker"], exc_info=True)

    # Close any orphaned positions not in today's picks (longs or shorts)
    today_tickers = {p["ticker"] for p in session.get("picks", [])}
    from stock_bot.config.settings import ib_settings as _ib_settings
    for pos in ib.positions(account=_ib_settings.account):
        ticker = pos.contract.symbol
        if pos.contract.secType != "STK" or ticker in today_tickers:
            continue
        pos_size = float(pos.position)
        if pos_size > 0:
            logger.warning("close_of_day: orphaned long — selling %s x%.0f", ticker, pos_size)
        elif pos_size < 0:
            logger.warning("close_of_day: orphaned short — covering %s x%.0f (cash-only account)", ticker, abs(pos_size))
        else:
            continue
        try:
            trade = close_position(ticker, ib)
            if trade is not None:
                sell_trades[ticker] = trade
        except Exception:
            logger.error("close_of_day: failed to close orphan %s", ticker, exc_info=True)
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

    # Verify sells — log confirmation or warning for each position
    for pick in session.get("picks", []):
        ticker = pick["ticker"]
        if pick.get("shares", 0) <= 0:
            continue
        trade = sell_trades.get(ticker)
        if trade is None:
            logger.warning("close_of_day: SELL NOT CONFIRMED — %s (no order placed)", ticker)
            continue
        status = getattr(trade, "orderStatus", None)
        if status and status.filled > 0:
            logger.info(
                "close_of_day: SOLD %s — %.0f shares @ $%.4f avg",
                ticker, status.filled, status.avgFillPrice,
            )
        else:
            logger.warning(
                "close_of_day: SELL NOT CONFIRMED — %s status=%s filled=%.0f",
                ticker,
                getattr(status, "status", "unknown") if status else "no status",
                getattr(status, "filled", 0) if status else 0,
            )

    # Record per-pick close prices for individual P&L tracking
    for pick in session.get("picks", []):
        ticker = pick["ticker"]
        buy_price = pick.get("buy_price", 0)
        shares = pick.get("shares", 0)

        # Try actual sell fill price first (no retry needed — already in memory)
        close_price: float | None = None
        trade = sell_trades.get(ticker)
        sell_status = getattr(trade, "orderStatus", None) if trade else None
        if sell_status and sell_status.filled > 0:
            close_price = float(sell_status.avgFillPrice)

        # Fall back to last bar price with retry
        if close_price is None:
            close_price = _retry_ibkr(
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
    actual_close_value = _retry_ibkr(get_net_liquidation, "NetLiquidation", ib, connect_ib, logger)
    if actual_close_value is not None:
        logger.info("close_of_day: using IBKR NetLiquidation $%.2f as portfolio_close_value", actual_close_value)
        total_close_value = actual_close_value
        session.pop("close_value_error", None)
    else:
        # Fallback: sum fill prices + uninvested cash (old behaviour)
        logger.error("close_of_day: NetLiquidation unavailable after retries — falling back to calculated close value")
        total_close_value = sum(
            p.get("close_price", p.get("buy_price", 0)) * p.get("shares", 0)
            for p in session.get("picks", [])
        )
        total_invested = sum(p.get("buy_value", 0) for p in session.get("picks", []))
        cash = max(0.0, session.get("portfolio_open_value", 0) - total_invested)
        total_close_value += cash
        session["close_value_error"] = round(total_close_value, 2)

    session["portfolio_close_value"] = round(total_close_value, 2)
    session["session_return_usd"] = round(total_close_value - session["portfolio_open_value"], 2)
    open_val = session["portfolio_open_value"]
    session["session_return_pct"] = (
        round((total_close_value - open_val) / open_val * 100, 3) if open_val > 0 else 0
    )

    # QQQ close price
    qqq_close = _retry_ibkr(
        lambda _ib: _get_last_price("QQQ", _ib),
        "QQQ close price", ib, connect_ib, logger,
    )
    qqq_buy = session.get("qqq_buy_price")
    if qqq_close and qqq_buy and qqq_buy > 0:
        session["qqq_close_price"] = qqq_close
        session["qqq_day_return_pct"] = round((qqq_close - qqq_buy) / qqq_buy * 100, 3)

    # Update equity curve with close value
    initial_investment = float(portfolio.get("initial_investment", 10_000))
    initial_qqq = sessions[0].get("qqq_buy_price") if sessions else None
    qqq_indexed = (
        round((qqq_close / initial_qqq) * initial_investment, 2)
        if (initial_qqq and qqq_close and initial_qqq > 0) else initial_investment
    )

    equity_curve = portfolio.get("equity_curve", [])
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
        "close_of_day: done — portfolio $%.2f (%+.2f%%) | QQQ %+.2f%%",
        total_close_value,
        session["session_return_pct"],
        session.get("qqq_day_return_pct") or 0,
    )


if __name__ == "__main__":
    main()
