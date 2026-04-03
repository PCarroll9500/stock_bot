# src/stock_bot/data_sources/portfolio_writer.py

import json
import logging
import math
from datetime import datetime, date as date_type
from pathlib import Path

import urllib.request
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


def get_net_liquidation(ib: IB) -> float | None:
    """Return the NetLiquidation value from the live IBKR account.

    Used after all positions are sold to get the true cash balance as
    reported by IBKR, rather than calculating from fill prices.
    Returns None if the value cannot be read.
    """
    account = ib_settings.account
    if not account:
        logger.warning("portfolio_writer: IB_ACCOUNT not configured — cannot read NetLiquidation")
        return None
    try:
        vals = {v.tag: v for v in ib.accountValues(account=account) if v.currency == "USD"}
        net_liq = vals.get("NetLiquidation")
        if net_liq is not None:
            value = float(net_liq.value)
            logger.info("portfolio_writer: NetLiquidation = $%.2f", value)
            return value
        logger.warning(
            "portfolio_writer: NetLiquidation not found — got %d account values for %s",
            len(vals), account,
        )
    except Exception:
        logger.warning("portfolio_writer: could not read NetLiquidation from IBKR", exc_info=True)
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



def _get_nasdaq_price() -> float | None:
    """Fetch the NASDAQ Composite (^IXIC) closing price from Yahoo Finance."""
    url = 'https://query1.finance.yahoo.com/v8/finance/chart/%5EIXIC?interval=1d&range=5d'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            import json as _json
            data = _json.loads(resp.read())
        result = data['chart']['result'][0]
        price = result['meta'].get('regularMarketPrice')
        if price:
            return round(float(price), 2)
        closes = result['indicators']['quote'][0].get('close', [])
        for p in reversed(closes):
            if p is not None:
                return round(float(p), 2)
    except Exception:
        logger.warning('portfolio_writer: NASDAQ price fetch failed', exc_info=True)
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
    """Return the previous session's close value, or the initial investment.

    Skips sessions where portfolio_close_value is "ERROR" (IBKR unavailable).
    """
    for session in reversed(portfolio.get("sessions", [])):
        close = session.get("portfolio_close_value")
        if close is not None:
            try:
                return float(close)
            except (TypeError, ValueError):
                continue  # skip ERROR sessions, look further back
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
    runner_ups: list[dict] | None = None,
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
        logger.info("portfolio_writer: no picks — writing no-pick stub to portfolio.json")
        portfolio = load_portfolio(test_mode=test_mode)
        today = date_type.today().isoformat()
        open_value = open_value_override if open_value_override is not None else _get_open_value(portfolio)
        stub: dict = {
            "date": today,
            "mode": mode,
            "no_picks": True,
            "picks": [],
            "runner_ups": runner_ups or [],
            "portfolio_open_value": open_value,
        }
        sessions = portfolio.get("sessions", [])
        idx = next((i for i, s in enumerate(sessions) if s.get("date") == today), None)
        if idx is not None:
            sessions[idx] = stub
        else:
            sessions.append(stub)
        portfolio["sessions"] = sessions
        save_portfolio(portfolio, test_mode=test_mode)
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

    # On first-ever session, set initial_investment from the live cash balance
    # instead of relying on the hardcoded default.
    if not portfolio.get("sessions") and open_value_override is not None:
        portfolio["initial_investment"] = round(open_value_override, 2)
        logger.info(
            "portfolio_writer: first session — setting initial_investment = $%.2f from live balance",
            open_value_override,
        )

    # QQQ as NASDAQ proxy — use pre-fetched value if provided (avoids sync IBKR call)
    if qqq_price_override is not None:
        qqq_price = qqq_price_override
        logger.info("portfolio_writer: QQQ price = %.4f (pre-fetched)", qqq_price)
    else:
        qqq_price = _get_last_price("QQQ", ib)
        logger.info("portfolio_writer: QQQ price = %s", qqq_price)

    # NASDAQ Composite (^IXIC) — fetched from Yahoo Finance for chart benchmark
    nasdaq_price = _get_nasdaq_price()
    logger.info("portfolio_writer: NASDAQ Composite = %s", nasdaq_price)

    # Build per-pick entries.
    # Priority: actual IBKR fill → estimated from market data
    pick_entries = []
    for p in picks:
        alloc_pct = p.get("allocation_pct") or round(100.0 / len(picks), 1)
        alloc_usd = alloc_pct / 100 * open_value

        # --- Try to use the actual fill from the Trade object first -----------
        # Sum all BUY fills for this ticker (handles original + realloc top-ups)
        fill_price: float | None = None
        fill_qty: int = 0
        if trades_by_ticker:
            total_filled = 0
            total_value = 0.0
            for trade in (trades_by_ticker.get(p["ticker"]) or []):
                status = getattr(trade, "orderStatus", None)
                order_action = getattr(trade.order, "action", "") if getattr(trade, "order", None) else ""
                if status and status.filled > 0 and order_action == "BUY":
                    total_filled += int(status.filled)
                    total_value += status.filled * status.avgFillPrice
            if total_filled > 0:
                fill_price = round(total_value / total_filled, 4)
                fill_qty = total_filled

        if fill_price and fill_qty > 0:
            buy_price = fill_price
            shares = fill_qty
            buy_value = round(shares * buy_price, 2)
            logger.info(
                "portfolio_writer: %s score=%d → %.1f%% — FILLED %d @ $%.4f = $%.2f",
                p["ticker"], p["score"], alloc_pct, shares, buy_price, buy_value,
            )
        elif trades_by_ticker is not None:
            # Trade data was provided but fill not yet confirmed — order may be queued.
            # Fall back to estimated price (same as no-trade path) so the session is recorded.
            # close_of_day will sell via IBKR positions and reconcile actual fill prices.
            queued_status = None
            for trade in (trades_by_ticker.get(p["ticker"]) or [])[:1]:
                st = getattr(trade, "orderStatus", None)
                if st and st.status in {"PreSubmitted", "Submitted", "ApiPending"}:
                    queued_status = st.status
            if queued_status:
                buy_price = _get_last_price(p["ticker"], ib) or 0.0
                if buy_price > 0:
                    shares = math.floor(alloc_usd / buy_price)
                    buy_value = round(shares * buy_price, 2)
                else:
                    shares = 0
                    buy_value = 0.0
                logger.warning(
                    "portfolio_writer: %s order queued (%s) -- estimated %d @ $%.4f = $%.2f",
                    p["ticker"], queued_status, shares, buy_price, buy_value,
                )
            else:
                logger.warning(
                    "portfolio_writer: %s -- no confirmed fill, skipping (not actually purchased)",
                    p["ticker"],
                )
                continue
        else:
            # No trade data provided at all — fall back to estimated price
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

        entry = {
            "ticker": p["ticker"],
            "score": p["score"],
            "direction": p["direction"],
            "risk": p.get("risk"),
            "expected_gain_pct": p.get("expected_gain_pct"),
            "reason": p["reason"],
            "trend_summary": p.get("trend_summary", ""),
            "allocation_pct": round(buy_value / open_value * 100, 1) if open_value > 0 else round(alloc_pct, 1),
            "shares": shares,
            "buy_price": buy_price or 0,
            "buy_value": buy_value,
            "close_price": None,
            "day_return_pct": None,
            "day_return_usd": None,
        }
        if p.get("hold_until"):
            entry["hold_until"] = p["hold_until"]
        pick_entries.append(entry)

    # Compute QQQ indexed to initial investment
    sessions = portfolio.get("sessions", [])
    initial_qqq = sessions[0].get("qqq_buy_price") if sessions else qqq_price
    qqq_indexed = (
        round((qqq_price / initial_qqq) * float(portfolio["initial_investment"]), 2)
        if (initial_qqq and qqq_price and initial_qqq > 0) else
        float(portfolio["initial_investment"])
    )

    # Compute NASDAQ Composite indexed to initial investment
    initial_nasdaq = sessions[0].get("nasdaq_buy_price") if sessions else nasdaq_price
    nasdaq_indexed = (
        round((nasdaq_price / initial_nasdaq) * float(portfolio["initial_investment"]), 2)
        if (initial_nasdaq and nasdaq_price and initial_nasdaq > 0) else None
    )

    cash_uninvested = round(open_value - sum(e["buy_value"] for e in pick_entries), 2)
    logger.info("portfolio_writer: uninvested cash = $%.2f (of $%.2f open)", cash_uninvested, open_value)

    session = {
        "date": today,
        "mode": mode,
        "spy_return_pct": round(spy_return, 3) if spy_return is not None else None,
        "picks": pick_entries,
        "runner_ups": runner_ups or [],
        "qqq_buy_price": qqq_price,
        "qqq_close_price": None,
        "qqq_day_return_pct": None,
        "nasdaq_buy_price": nasdaq_price,
        "nasdaq_close_price": None,
        "nasdaq_day_return_pct": None,
        "portfolio_open_value": round(open_value, 2),
        "portfolio_close_value": None,
        "session_return_pct": None,
        "session_return_usd": None,
        "cash_uninvested": cash_uninvested,
        "cash_balance_close": None,
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
    eq_point = {"date": today, "portfolio_value": round(open_value, 2), "qqq_indexed": qqq_indexed, "nasdaq_indexed": nasdaq_indexed}
    eq_idx = next((i for i, e in enumerate(equity_curve) if e.get("date") == today), None)
    if eq_idx is not None:
        equity_curve[eq_idx] = eq_point
    else:
        equity_curve.append(eq_point)
    portfolio["equity_curve"] = equity_curve

    save_portfolio(portfolio, test_mode=test_mode)
