# src/stock_bot/data_sources/portfolio_writer.py

import json
import logging
import math
from datetime import datetime, date as date_type
from pathlib import Path

from ib_insync import IB, Stock

from stock_bot.config.settings import ib_settings

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parents[3] / "docs" / "data"
_PORTFOLIO_JSON      = _DATA_DIR / "portfolio.json"
_PORTFOLIO_JSON_TEST = _DATA_DIR / "portfolio_test.json"
_INITIAL_INVESTMENT  = 10_000.0


def _resolve_path(test_mode: bool) -> Path:
    return _PORTFOLIO_JSON_TEST if test_mode else _PORTFOLIO_JSON


def get_live_account_value(ib: IB) -> float | None:
    """Return the available cash balance from the live IBKR account.

    Uses CashBalance (not NetLiquidation or BuyingPower) so the bot never
    sizes orders against margin — only actual cash on hand is used.
    Returns None if the value cannot be read.
    """
    account = ib_settings.account
    if not account:
        logger.warning("portfolio_writer: IB_ACCOUNT not configured — cannot read live balance")
        return None
    try:
        vals = {v.tag: v for v in ib.accountValues(account=account) if v.currency == "USD"}
        cash = vals.get("CashBalance")
        net_liq = vals.get("NetLiquidation")
        if cash is not None:
            value = float(cash.value)
            logger.info(
                "portfolio_writer: CashBalance = $%.2f (NetLiq = $%.2f) — using cash only, no margin",
                value, float(net_liq.value) if net_liq else 0,
            )
            return value
        logger.warning(
            "portfolio_writer: CashBalance not found — got %d account values for %s",
            len(vals), account,
        )
    except Exception:
        logger.warning("portfolio_writer: could not read account value from IBKR", exc_info=True)
    return None


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


def load_portfolio(test_mode: bool = False) -> dict:
    """Load portfolio.json (or portfolio_test.json in test mode).
    Returns a fresh skeleton if the file doesn't exist."""
    path = _resolve_path(test_mode)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("portfolio_writer: could not parse %s — starting fresh", path.name)
    title = "Inf Money Stock Bot [TEST]" if test_mode else "Inf Money Stock Bot"
    return {
        "title": title,
        "initial_investment": _INITIAL_INVESTMENT,
        "start_date": date_type.today().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "sessions": [],
        "equity_curve": [],
    }


def save_portfolio(data: dict, test_mode: bool = False) -> None:
    """Write portfolio dict to JSON."""
    path = _resolve_path(test_mode)
    data["updated_at"] = datetime.now().isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    logger.info("portfolio_writer: saved → %s", path)


def _get_open_value(portfolio: dict, test_mode: bool = False) -> float:
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
    test_mode: bool = False,
    trades_by_ticker: dict | None = None,
    open_value_override: float | None = None,
    qqq_price_override: float | None = None,
) -> None:
    """
    Write today's session to portfolio.json using actual IBKR fill data when
    available, falling back to estimated prices from market data.

    Args:
        picks: Scored and ranked pick dicts from the AI scorer.
        ib: Active IB connection.
        mode: 'aggressive' or 'conservative'.
        spy_return: SPY day return % (context only).
        test_mode: If True, writes to portfolio_test.json.
        trades_by_ticker: {ticker: [Trade, ...]} — actual IBKR trade objects
            returned by buy_stock().  When a filled trade is present the actual
            avgFillPrice / filled qty are used instead of estimated prices.
        open_value_override: Actual account balance from IBKR (NetLiquidation).
            When supplied this is used as the session open value instead of
            reading the previous session's close from portfolio.json.
    """
    if not picks:
        logger.info("portfolio_writer: no picks — skipping session write")
        return

    if test_mode:
        logger.info("portfolio_writer: TEST MODE — writing to portfolio_test.json")

    portfolio = load_portfolio(test_mode=test_mode)
    today = date_type.today().isoformat()

    # Use live IBKR balance when provided; otherwise fall back to JSON history
    if open_value_override is not None:
        open_value = open_value_override
        logger.info("portfolio_writer: open_value = $%.2f (from IBKR)", open_value)
    else:
        open_value = _get_open_value(portfolio)
        logger.info("portfolio_writer: open_value = $%.2f (from portfolio.json)", open_value)

    # QQQ as NASDAQ proxy — use pre-fetched value if provided (avoids sync IBKR call)
    if qqq_price_override is not None:
        qqq_price = qqq_price_override
        logger.info("portfolio_writer: QQQ price = %.4f (pre-fetched)", qqq_price)
    else:
        qqq_price = _get_last_price("QQQ", ib)
        logger.info("portfolio_writer: QQQ price = %s", qqq_price)

    # Build per-pick entries.
    # Priority: actual IBKR fill → estimated from market data
    pick_entries = []
    for p in picks:
        alloc_pct = p.get("allocation_pct") or round(100.0 / len(picks), 1)
        alloc_usd = alloc_pct / 100 * open_value

        # --- Try to use the actual fill from the Trade object first -----------
        fill_price: float | None = None
        fill_qty: int = 0
        if trades_by_ticker:
            for trade in (trades_by_ticker.get(p["ticker"]) or [])[:1]:  # entry order only
                status = getattr(trade, "orderStatus", None)
                if status and status.filled > 0:
                    fill_price = float(status.avgFillPrice)
                    fill_qty = int(status.filled)
                    break

        if fill_price and fill_qty > 0:
            buy_price = fill_price
            shares = fill_qty
            buy_value = round(shares * buy_price, 2)
            logger.info(
                "portfolio_writer: %s score=%d → %.1f%% — FILLED %d @ $%.4f = $%.2f",
                p["ticker"], p["score"], alloc_pct, shares, buy_price, buy_value,
            )
        else:
            # Fall back to estimated price from IBKR market data
            buy_price = _get_last_price(p["ticker"], ib) or 0.0
            if buy_price > 0:
                shares = math.floor(alloc_usd / buy_price)
                buy_value = round(shares * buy_price, 2)
            else:
                shares = 0
                buy_value = 0.0
            logger.info(
                "portfolio_writer: %s score=%d → %.1f%% — ESTIMATED %d @ $%.4f = $%.2f",
                p["ticker"], p["score"], alloc_pct, shares, buy_price, buy_value,
            )

        pick_entries.append({
            "ticker": p["ticker"],
            "score": p["score"],
            "direction": p["direction"],
            "risk": p.get("risk"),
            "expected_gain_pct": p.get("expected_gain_pct"),
            "reason": p["reason"],
            "trend_summary": p.get("trend_summary", ""),
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

    save_portfolio(portfolio, test_mode=test_mode)
