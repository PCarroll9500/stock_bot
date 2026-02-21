# src/stock_bot/data_sources/portfolio_writer.py

import json
import logging
import math
from datetime import datetime, date as date_type
from pathlib import Path

from ib_insync import IB, Stock

logger = logging.getLogger(__name__)

# Resolve portfolio.json path relative to this file:
# portfolio_writer.py -> data_sources/ -> stock_bot/ -> src/ -> repo_root/ -> website/data/
_PORTFOLIO_JSON = Path(__file__).resolve().parents[3] / "website" / "data" / "portfolio.json"
_INITIAL_INVESTMENT = 10_000.0


def _get_last_price(ticker: str, ib: IB) -> float | None:
    """Fetch the most recent traded price from IBKR (last 1-min bar)."""
    contract = Stock(ticker, "SMART", "USD")
    try:
        qualified = ib.qualifyContracts(contract)
        if not qualified:
            return None
        bars = ib.reqHistoricalData(
            qualified[0],
            endDateTime="",
            durationStr="1 D",
            barSizeSetting="1 min",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=2,
        )
        if bars:
            return round(float(bars[-1].close), 4)
    except Exception:
        logger.warning("portfolio_writer: price fetch failed for %s", ticker)
    return None


def load_portfolio() -> dict:
    """Load portfolio.json, or return a fresh skeleton if it doesn't exist."""
    if _PORTFOLIO_JSON.exists():
        try:
            return json.loads(_PORTFOLIO_JSON.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("portfolio_writer: could not parse portfolio.json — starting fresh")
    return {
        "title": "Inf Money Stock Bot",
        "initial_investment": _INITIAL_INVESTMENT,
        "start_date": date_type.today().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "sessions": [],
        "equity_curve": [],
    }


def save_portfolio(data: dict) -> None:
    """Write portfolio dict to JSON."""
    data["updated_at"] = datetime.now().isoformat()
    _PORTFOLIO_JSON.parent.mkdir(parents=True, exist_ok=True)
    _PORTFOLIO_JSON.write_text(json.dumps(data, indent=2), encoding="utf-8")
    logger.info("portfolio_writer: saved → %s", _PORTFOLIO_JSON)


def _get_open_value(portfolio: dict) -> float:
    """Return the previous session's close value, or the initial investment."""
    for session in reversed(portfolio.get("sessions", [])):
        close = session.get("portfolio_close_value")
        if close is not None:
            return float(close)
    return float(portfolio.get("initial_investment", _INITIAL_INVESTMENT))


def write_session(
    picks: list[dict],
    ib: IB,
    mode: str = "aggressive",
    spy_return: float | None = None,
) -> None:
    """
    Compute score-weighted allocations for each pick, fetch buy prices from IBKR,
    and append (or replace) today's session in portfolio.json.

    Allocation formula: each stock's weight = its score / sum(all scores).
    Higher-scoring picks receive proportionally more capital.
    """
    if not picks:
        logger.info("portfolio_writer: no picks — skipping session write")
        return

    portfolio = load_portfolio()
    today = date_type.today().isoformat()
    open_value = _get_open_value(portfolio)
    total_score = sum(p["score"] for p in picks)

    # QQQ as NASDAQ proxy
    qqq_price = _get_last_price("QQQ", ib)
    logger.info("portfolio_writer: QQQ price = %s", qqq_price)

    # Build per-pick entries with allocations and share counts
    pick_entries = []
    for p in picks:
        alloc_pct = (p["score"] / total_score) * 100 if total_score else 0
        alloc_usd = (p["score"] / total_score) * open_value if total_score else 0

        buy_price = _get_last_price(p["ticker"], ib)
        if buy_price and buy_price > 0:
            shares = math.floor(alloc_usd / buy_price)
            buy_value = round(shares * buy_price, 2)
        else:
            shares = 0
            buy_value = 0.0

        logger.info(
            "portfolio_writer: %s score=%d → %.1f%% ($%.2f, %d shares @ $%.4f)",
            p["ticker"], p["score"], alloc_pct, buy_value, shares, buy_price or 0,
        )
        pick_entries.append({
            "ticker": p["ticker"],
            "score": p["score"],
            "direction": p["direction"],
            "reason": p["reason"],
            "allocation_pct": round(alloc_pct, 1),
            "shares": shares,
            "buy_price": buy_price or 0,
            "buy_value": buy_value,
            "close_price": None,
            "day_return_pct": None,
            "day_return_usd": None,
        })

    # Compute QQQ indexed to initial investment
    sessions = portfolio.get("sessions", [])
    initial_qqq = sessions[0].get("qqq_buy_price") if sessions else qqq_price
    qqq_indexed = (
        round((qqq_price / initial_qqq) * float(portfolio["initial_investment"]), 2)
        if (initial_qqq and qqq_price and initial_qqq > 0) else
        float(portfolio["initial_investment"])
    )

    session = {
        "date": today,
        "mode": mode,
        "spy_return_pct": round(spy_return, 3) if spy_return is not None else None,
        "picks": pick_entries,
        "qqq_buy_price": qqq_price,
        "qqq_close_price": None,
        "qqq_day_return_pct": None,
        "portfolio_open_value": round(open_value, 2),
        "portfolio_close_value": None,
        "session_return_pct": None,
        "session_return_usd": None,
    }

    # Replace today's session if it already exists (re-run scenario)
    idx = next((i for i, s in enumerate(sessions) if s.get("date") == today), None)
    if idx is not None:
        sessions[idx] = session
        logger.info("portfolio_writer: replaced existing session for %s", today)
    else:
        sessions.append(session)
    portfolio["sessions"] = sessions

    # Equity curve — record open value for today
    equity_curve = portfolio.get("equity_curve", [])
    eq_point = {"date": today, "portfolio_value": round(open_value, 2), "qqq_indexed": qqq_indexed}
    eq_idx = next((i for i, e in enumerate(equity_curve) if e.get("date") == today), None)
    if eq_idx is not None:
        equity_curve[eq_idx] = eq_point
    else:
        equity_curve.append(eq_point)
    portfolio["equity_curve"] = equity_curve

    save_portfolio(portfolio)
