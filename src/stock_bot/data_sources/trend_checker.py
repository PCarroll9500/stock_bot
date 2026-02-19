# src/stock_bot/data_sources/trend_checker.py

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
