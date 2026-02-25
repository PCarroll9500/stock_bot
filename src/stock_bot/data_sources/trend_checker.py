# src/stock_bot/data_sources/trend_checker.py

import asyncio
import logging
from datetime import date

from ib_insync import IB, Stock, util



logger = logging.getLogger(__name__)

# Period → number of trading-day bars to look back
_PERIOD_LOOKBACKS = {
    "daily":     2,
    "weekly":    6,
    "monthly":   22,
    "quarterly": 64,
    "yearly":    253,
}


def get_trend_data(ticker: str, ib: IB) -> dict | None:
    """
    Fetch up to 10 years of daily OHLCV from IBKR for `ticker`.

    Returns a dict mapping period name → {"pct_change": float | None},
    or None if the ticker can't be qualified or no bars are returned.

    Periods: daily, weekly, monthly, quarterly, ytd, yearly, overall.
    A period's pct_change is None when there isn't enough history to compute it.
    """
    contract = Stock(ticker, "SMART", "USD")
    try:
        qualified = ib.qualifyContracts(contract)
    except Exception:
        logger.warning("trend_checker: qualifyContracts failed for %s", ticker)
        return None

    if not qualified:
        logger.warning("trend_checker: %s not found on IBKR", ticker)
        return None

    try:
        bars = ib.reqHistoricalData(
            qualified[0],
            endDateTime="",
            durationStr="10 Y",
            barSizeSetting="1 day",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
        )
    except Exception:
        logger.warning("trend_checker: reqHistoricalData failed for %s", ticker)
        return None

    if not bars:
        logger.warning("trend_checker: no bars returned for %s", ticker)
        return None

    df = util.df(bars)[["date", "close"]].set_index("date")
    n = len(df)

    result: dict[str, dict] = {}

    # Fixed-lookback periods
    for period, lookback in _PERIOD_LOOKBACKS.items():
        if n >= lookback:
            start_price = df["close"].iloc[-lookback]
            end_price = df["close"].iloc[-1]
            if start_price and start_price != 0:
                pct = (end_price - start_price) / start_price * 100.0
                result[period] = {"pct_change": pct}
            else:
                result[period] = {"pct_change": None}
        else:
            result[period] = {"pct_change": None}

    # YTD: from first bar on/after Jan 1 of current year
    ytd_start = date(date.today().year, 1, 1)
    ytd_df = df[df.index >= ytd_start]
    if not ytd_df.empty and n >= 1:
        start_price = ytd_df["close"].iloc[0]
        end_price = df["close"].iloc[-1]
        if start_price and start_price != 0:
            pct = (end_price - start_price) / start_price * 100.0
            result["ytd"] = {"pct_change": pct}
        else:
            result["ytd"] = {"pct_change": None}
    else:
        result["ytd"] = {"pct_change": None}

    # Overall: first bar → last bar
    if n >= 2:
        start_price = df["close"].iloc[0]
        end_price = df["close"].iloc[-1]
        if start_price and start_price != 0:
            pct = (end_price - start_price) / start_price * 100.0
            result["overall"] = {"pct_change": pct}
        else:
            result["overall"] = {"pct_change": None}
    else:
        result["overall"] = {"pct_change": None}

    return result


def get_trend_for_scoring(ticker: str, ib: IB) -> dict | None:
    """
    Fetch 1 year of daily bars and return pct_change for key timeframes.
    Used to give GPT trend context during catalyst scoring.

    Returns: {"daily": float|None, "weekly": float|None, "monthly": float|None,
               "quarterly": float|None, "yearly": float|None}
    Returns None on any failure (fail-open — scorer will note data unavailable).
    """
    contract = Stock(ticker, "SMART", "USD")
    try:
        qualified = ib.qualifyContracts(contract)
        if not qualified:
            return None
        bars = ib.reqHistoricalData(
            qualified[0],
            endDateTime="",
            durationStr="1 Y",
            barSizeSetting="1 day",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
        )
    except Exception:
        logger.warning("trend_checker: trend fetch failed for %s", ticker)
        return None

    if not bars or len(bars) < 2:
        return None

    closes = [b.close for b in bars]
    n = len(closes)
    last = closes[-1]

    def pct(lookback: int) -> float | None:
        if n >= lookback:
            p = closes[-lookback]
            return round((last - p) / p * 100, 1) if p else None
        return None

    return {
        "daily":     pct(2),
        "weekly":    pct(6),
        "monthly":   pct(22),
        "quarterly": pct(min(64, n)),
        "yearly":    pct(min(253, n)),
    }


def fmt_trend_for_prompt(trend: dict | None) -> str:
    """Format trend dict as a compact string for GPT prompt injection."""
    if not trend:
        return "unavailable"
    labels = [("daily", "1d"), ("weekly", "1w"), ("monthly", "1m"),
              ("quarterly", "3m"), ("yearly", "1yr")]
    parts = []
    for key, label in labels:
        val = trend.get(key)
        if val is not None:
            parts.append(f"{label}: {'+' if val >= 0 else ''}{val:.1f}%")
    return "  ".join(parts) if parts else "unavailable"


def passes_trend_filters(ticker: str, ib: IB, filters_config: dict) -> bool:
    """
    Return True if `ticker` passes all configured trend thresholds.

    - Skips keys starting with '_' (comments).
    - Skips a period entirely if its min/max are both null.
    - Fail-open: returns True if get_trend_data() returns None (data unavailable).
    - Returns True if a period's pct_change is None (insufficient history).
    """
    trend_data = get_trend_data(ticker, ib)
    if trend_data is None:
        logger.info("trend_checker: no data for %s — passing (fail-open)", ticker)
        return True

    for period, bounds in filters_config.items():
        if period.startswith("_"):
            continue

        min_val = bounds.get("min")
        max_val = bounds.get("max")

        # Both null → no filter configured for this period
        if min_val is None and max_val is None:
            continue

        period_data = trend_data.get(period)
        if period_data is None:
            continue  # unknown period in config — skip

        pct = period_data.get("pct_change")
        if pct is None:
            continue  # insufficient history — skip this period

        if min_val is not None and pct < min_val:
            logger.info(
                "trend_checker: %s REJECTED — %s pct_change %.2f%% < min %.2f%%",
                ticker, period, pct, min_val,
            )
            return False

        if max_val is not None and pct > max_val:
            logger.info(
                "trend_checker: %s REJECTED — %s pct_change %.2f%% > max %.2f%%",
                ticker, period, pct, max_val,
            )
            return False

    logger.info("trend_checker: %s passed all trend filters", ticker)
    return True


def passes_gap_filter(ticker: str, ib: IB, max_gap_pct: float) -> bool:
    """
    Return False if today's open gapped up more than max_gap_pct% vs yesterday's close.

    Fail-open: returns True if data is unavailable or insufficient.
    """
    contract = Stock(ticker, "SMART", "USD")
    try:
        qualified = ib.qualifyContracts(contract)
    except Exception:
        logger.warning("gap_filter: qualifyContracts failed for %s — passing (fail-open)", ticker)
        return True

    if not qualified:
        return True

    try:
        bars = ib.reqHistoricalData(
            qualified[0],
            endDateTime="",
            durationStr="2 D",
            barSizeSetting="1 day",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
        )
    except Exception:
        logger.warning("gap_filter: reqHistoricalData failed for %s — passing (fail-open)", ticker)
        return True

    if not bars or len(bars) < 2:
        return True

    prev_close = bars[-2].close
    today_open = bars[-1].open

    if not prev_close or prev_close == 0:
        return True

    gap_pct = (today_open - prev_close) / prev_close * 100.0
    if gap_pct > max_gap_pct:
        logger.info(
            "gap_filter: %s REJECTED — open gap +%.2f%% > max %.2f%%",
            ticker, gap_pct, max_gap_pct,
        )
        return False

    logger.info("gap_filter: %s passed — open gap %.2f%%", ticker, gap_pct)
    return True


def get_spy_day_return(ib: IB) -> float | None:
    """
    Return SPY's percentage change today vs yesterday's close.
    Returns None if data is unavailable.
    """
    contract = Stock("SPY", "SMART", "USD")
    try:
        bars = ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr="2 D",
            barSizeSetting="1 day",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
        )
    except Exception:
        logger.warning("SPY context check failed — skipping")
        return None

    if not bars or len(bars) < 2:
        return None

    prev_close = bars[-2].close
    today_close = bars[-1].close
    if not prev_close or prev_close == 0:
        return None

    return (today_close - prev_close) / prev_close * 100.0


def passes_aggressive_filters(ticker: str, ib: IB, max_gap_pct: float) -> bool:
    """
    Aggressive mode filter — requires green on the day and rejects fading gaps.

    Uses 2 days of 1-min bars to compute:
      - prev_close: last bar of yesterday
      - today_open: first bar of today
      - current:    most recent bar

    Rejects if:
      - Fading gap: opened >max_gap_pct% up but current < today_open
      - Red on day: current <= prev_close

    Continuing gaps (large gap still holding/expanding) are allowed — that's momentum.
    Fail-open on data errors.
    """
    contract = Stock(ticker, "SMART", "USD")
    try:
        qualified = ib.qualifyContracts(contract)
    except Exception:
        logger.warning("aggressive_filter: qualifyContracts failed for %s — passing (fail-open)", ticker)
        return True

    if not qualified:
        return True

    try:
        bars = ib.reqHistoricalData(
            qualified[0],
            endDateTime="",
            durationStr="2 D",
            barSizeSetting="1 min",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=2,
        )
    except Exception:
        logger.warning("aggressive_filter: reqHistoricalData failed for %s — passing (fail-open)", ticker)
        return True

    if not bars or len(bars) < 2:
        return True

    from datetime import date as date_type
    today = date_type.today()

    yesterday_bars = [b for b in bars if b.date.date() < today]
    today_bars = [b for b in bars if b.date.date() == today]

    if not yesterday_bars or not today_bars:
        logger.info("aggressive_filter: %s — insufficient intraday data, passing", ticker)
        return True

    prev_close = yesterday_bars[-1].close
    today_open = today_bars[0].open
    current = today_bars[-1].close

    if not prev_close or prev_close == 0:
        return True

    gap_pct = (today_open - prev_close) / prev_close * 100.0

    # Reject fading gaps
    if gap_pct > max_gap_pct and current < today_open:
        logger.info(
            "aggressive_filter: %s REJECTED — fading gap (opened +%.2f%%, now below open)",
            ticker, gap_pct,
        )
        return False

    # Reject red on the day
    if current <= prev_close:
        logger.info(
            "aggressive_filter: %s REJECTED — red on day (current %.2f <= prev_close %.2f)",
            ticker, current, prev_close,
        )
        return False

    logger.info(
        "aggressive_filter: %s passed — gap %.2f%%, intraday +%.2f%%",
        ticker, gap_pct, (current - prev_close) / prev_close * 100.0,
    )
    return True


# ---------------------------------------------------------------------------
# Async counterparts — use these in async main() for parallelism
# ---------------------------------------------------------------------------

async def get_spy_day_return_async(ib: IB) -> float | None:
    """Async version of get_spy_day_return."""
    contract = Stock("SPY", "SMART", "USD")
    try:
        bars = await ib.reqHistoricalDataAsync(
            contract,
            endDateTime="",
            durationStr="2 D",
            barSizeSetting="1 day",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
        )
    except Exception:
        logger.warning("SPY context check failed — skipping")
        return None

    if not bars or len(bars) < 2:
        return None

    prev_close = bars[-2].close
    today_close = bars[-1].close
    if not prev_close or prev_close == 0:
        return None
    return (today_close - prev_close) / prev_close * 100.0


async def passes_aggressive_filters_async(
    ticker: str, ib: IB, max_gap_pct: float, sem: asyncio.Semaphore,
) -> bool:
    """Async version of passes_aggressive_filters. Pass a shared Semaphore to rate-limit."""
    contract = Stock(ticker, "SMART", "USD")
    async with sem:
        try:
            qualified = await ib.qualifyContractsAsync(contract)
        except Exception:
            logger.warning("aggressive_filter: qualifyContracts failed for %s — passing (fail-open)", ticker)
            return True

        if not qualified:
            return True

        try:
            bars = await ib.reqHistoricalDataAsync(
                qualified[0],
                endDateTime="",
                durationStr="2 D",
                barSizeSetting="1 min",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=2,
            )
        except Exception:
            logger.warning("aggressive_filter: reqHistoricalData failed for %s — passing (fail-open)", ticker)
            return True

    if not bars or len(bars) < 2:
        return True

    from datetime import date as date_type
    today = date_type.today()

    yesterday_bars = [b for b in bars if b.date.date() < today]
    today_bars = [b for b in bars if b.date.date() == today]

    if not yesterday_bars or not today_bars:
        logger.info("aggressive_filter: %s — insufficient intraday data, passing", ticker)
        return True

    prev_close = yesterday_bars[-1].close
    today_open = today_bars[0].open
    current = today_bars[-1].close

    if not prev_close or prev_close == 0:
        return True

    gap_pct = (today_open - prev_close) / prev_close * 100.0

    if gap_pct > max_gap_pct and current < today_open:
        logger.info(
            "aggressive_filter: %s REJECTED — fading gap (opened +%.2f%%, now below open)",
            ticker, gap_pct,
        )
        return False

    if current <= prev_close:
        logger.info(
            "aggressive_filter: %s REJECTED — red on day (current %.2f <= prev_close %.2f)",
            ticker, current, prev_close,
        )
        return False

    logger.info(
        "aggressive_filter: %s passed — gap %.2f%%, intraday +%.2f%%",
        ticker, gap_pct, (current - prev_close) / prev_close * 100.0,
    )
    return True


async def passes_gap_filter_async(
    ticker: str, ib: IB, max_gap_pct: float, sem: asyncio.Semaphore,
) -> bool:
    """Async version of passes_gap_filter."""
    contract = Stock(ticker, "SMART", "USD")
    async with sem:
        try:
            qualified = await ib.qualifyContractsAsync(contract)
        except Exception:
            logger.warning("gap_filter: qualifyContracts failed for %s — passing (fail-open)", ticker)
            return True

        if not qualified:
            return True

        try:
            bars = await ib.reqHistoricalDataAsync(
                qualified[0],
                endDateTime="",
                durationStr="2 D",
                barSizeSetting="1 day",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=1,
            )
        except Exception:
            logger.warning("gap_filter: reqHistoricalData failed for %s — passing (fail-open)", ticker)
            return True

    if not bars or len(bars) < 2:
        return True

    prev_close = bars[-2].close
    today_open = bars[-1].open

    if not prev_close or prev_close == 0:
        return True

    gap_pct = (today_open - prev_close) / prev_close * 100.0
    if gap_pct > max_gap_pct:
        logger.info(
            "gap_filter: %s REJECTED — open gap +%.2f%% > max %.2f%%",
            ticker, gap_pct, max_gap_pct,
        )
        return False

    logger.info("gap_filter: %s passed — open gap %.2f%%", ticker, gap_pct)
    return True


async def get_trend_data_async(ticker: str, ib: IB, sem: asyncio.Semaphore) -> dict | None:
    """Async version of get_trend_data (10Y daily bars)."""
    contract = Stock(ticker, "SMART", "USD")
    async with sem:
        try:
            qualified = await ib.qualifyContractsAsync(contract)
        except Exception:
            logger.warning("trend_checker: qualifyContracts failed for %s", ticker)
            return None

        if not qualified:
            logger.warning("trend_checker: %s not found on IBKR", ticker)
            return None

        try:
            bars = await ib.reqHistoricalDataAsync(
                qualified[0],
                endDateTime="",
                durationStr="10 Y",
                barSizeSetting="1 day",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=1,
            )
        except Exception:
            logger.warning("trend_checker: reqHistoricalData failed for %s", ticker)
            return None

    if not bars:
        logger.warning("trend_checker: no bars returned for %s", ticker)
        return None

    df = util.df(bars)[["date", "close"]].set_index("date")
    n = len(df)
    result: dict[str, dict] = {}

    for period, lookback in _PERIOD_LOOKBACKS.items():
        if n >= lookback:
            start_price = df["close"].iloc[-lookback]
            end_price = df["close"].iloc[-1]
            if start_price and start_price != 0:
                pct = (end_price - start_price) / start_price * 100.0
                result[period] = {"pct_change": pct}
            else:
                result[period] = {"pct_change": None}
        else:
            result[period] = {"pct_change": None}

    ytd_start = date(date.today().year, 1, 1)
    ytd_df = df[df.index >= ytd_start]
    if not ytd_df.empty and n >= 1:
        start_price = ytd_df["close"].iloc[0]
        end_price = df["close"].iloc[-1]
        if start_price and start_price != 0:
            result["ytd"] = {"pct_change": (end_price - start_price) / start_price * 100.0}
        else:
            result["ytd"] = {"pct_change": None}
    else:
        result["ytd"] = {"pct_change": None}

    if n >= 2:
        start_price = df["close"].iloc[0]
        end_price = df["close"].iloc[-1]
        if start_price and start_price != 0:
            result["overall"] = {"pct_change": (end_price - start_price) / start_price * 100.0}
        else:
            result["overall"] = {"pct_change": None}
    else:
        result["overall"] = {"pct_change": None}

    return result


async def passes_trend_filters_async(
    ticker: str, ib: IB, filters_config: dict, sem: asyncio.Semaphore,
) -> bool:
    """Async version of passes_trend_filters."""
    trend_data = await get_trend_data_async(ticker, ib, sem)
    if trend_data is None:
        logger.info("trend_checker: no data for %s — passing (fail-open)", ticker)
        return True

    for period, bounds in filters_config.items():
        if period.startswith("_"):
            continue
        min_val = bounds.get("min")
        max_val = bounds.get("max")
        if min_val is None and max_val is None:
            continue
        period_data = trend_data.get(period)
        if period_data is None:
            continue
        pct = period_data.get("pct_change")
        if pct is None:
            continue
        if min_val is not None and pct < min_val:
            logger.info(
                "trend_checker: %s REJECTED — %s pct_change %.2f%% < min %.2f%%",
                ticker, period, pct, min_val,
            )
            return False
        if max_val is not None and pct > max_val:
            logger.info(
                "trend_checker: %s REJECTED — %s pct_change %.2f%% > max %.2f%%",
                ticker, period, pct, max_val,
            )
            return False

    logger.info("trend_checker: %s passed all trend filters", ticker)
    return True


async def get_trend_for_scoring_async(
    ticker: str, ib: IB, sem: asyncio.Semaphore,
) -> dict | None:
    """Async version of get_trend_for_scoring (1Y daily bars)."""
    contract = Stock(ticker, "SMART", "USD")
    async with sem:
        try:
            qualified = await ib.qualifyContractsAsync(contract)
            if not qualified:
                return None
            bars = await ib.reqHistoricalDataAsync(
                qualified[0],
                endDateTime="",
                durationStr="1 Y",
                barSizeSetting="1 day",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=1,
            )
        except Exception:
            logger.warning("trend_checker: trend fetch failed for %s", ticker)
            return None

    if not bars or len(bars) < 2:
        return None

    closes = [b.close for b in bars]
    n = len(closes)
    last = closes[-1]

    def pct(lookback: int) -> float | None:
        if n >= lookback:
            p = closes[-lookback]
            return round((last - p) / p * 100, 1) if p else None
        return None

    return {
        "daily":     pct(2),
        "weekly":    pct(6),
        "monthly":   pct(22),
        "quarterly": pct(min(64, n)),
        "yearly":    pct(min(253, n)),
    }
