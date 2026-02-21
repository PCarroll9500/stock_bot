#!/usr/bin/env python3
"""
scripts/close_of_day.py

Run at market close (4:05 PM ET) via cron to record end-of-day prices,
compute session returns, and update the equity curve in portfolio.json.

Cron entry (add with: crontab -e):
  5 16 * * 1-5 cd /home/patrick/dev/github/stock_bot && .venv/bin/python scripts/close_of_day.py >> logs/close_of_day.log 2>&1
"""

import logging
import sys
from datetime import date as date_type
from pathlib import Path

# Package is installed in the venv via `pip install -e .`
from stock_bot.core.logging_config import setup_logging
from stock_bot.brokers.ib.connect_disconnect import connect_ib, disconnect_ib
from stock_bot.data_sources.portfolio_writer import (
    load_portfolio,
    save_portfolio,
    _get_last_price,
)


def main() -> None:
    setup_logging()
    logger = logging.getLogger(__name__)
    today = date_type.today().isoformat()

    portfolio = load_portfolio()
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

    # Fetch close prices for every pick
    total_close_value = 0.0
    for pick in session.get("picks", []):
        close_price = _get_last_price(pick["ticker"], ib)
        buy_price = pick.get("buy_price", 0)
        shares = pick.get("shares", 0)

        if close_price and buy_price > 0 and shares > 0:
            day_return_usd = (close_price - buy_price) * shares
            day_return_pct = (close_price - buy_price) / buy_price * 100
            pick["close_price"] = close_price
            pick["day_return_pct"] = round(day_return_pct, 3)
            pick["day_return_usd"] = round(day_return_usd, 2)
            close_value = close_price * shares
            logger.info(
                "close_of_day: %s close=%.4f return=%.2f%% ($%.2f)",
                pick["ticker"], close_price, day_return_pct, day_return_usd,
            )
        else:
            close_value = pick.get("buy_value", 0)
            logger.warning("close_of_day: %s — no close price, using buy_value", pick["ticker"])

        total_close_value += close_value

    # Add back uninvested cash (from rounding when computing share counts)
    total_invested = sum(p.get("buy_value", 0) for p in session.get("picks", []))
    cash = max(0.0, session["portfolio_open_value"] - total_invested)
    total_close_value += cash

    session["portfolio_close_value"] = round(total_close_value, 2)
    session["session_return_usd"] = round(total_close_value - session["portfolio_open_value"], 2)
    open_val = session["portfolio_open_value"]
    session["session_return_pct"] = (
        round((total_close_value - open_val) / open_val * 100, 3) if open_val > 0 else 0
    )

    # QQQ close price
    qqq_close = _get_last_price("QQQ", ib)
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

    disconnect_ib()
    save_portfolio(portfolio)

    logger.info(
        "close_of_day: done — portfolio $%.2f (%+.2f%%) | QQQ %+.2f%%",
        total_close_value,
        session["session_return_pct"],
        session.get("qqq_day_return_pct") or 0,
    )


if __name__ == "__main__":
    main()
