# src/stock_bot/main.py

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from stock_bot.core.logging_config import setup_logging
from stock_bot.config.settings import ib_settings
from stock_bot.brokers.ib.connect_disconnect import connect_ib_async, disconnect_ib
from stock_bot.brokers.ib.buy_stocks import buy_stock_async
from stock_bot.data_sources.scanner import get_scanner_universe_async
from stock_bot.data_sources.news_fetcher import fetch_news_for_tickers_async
from stock_bot.data_sources.trend_checker import (
    passes_trend_filters_async,
    passes_gap_filter_async,
    passes_aggressive_filters_async,
    get_spy_day_return_async,
    get_trend_for_scoring_async,
    fmt_trend_for_prompt,
)
from stock_bot.ai.catalyst_scorer import score_candidates, filter_and_rank
from stock_bot.data_sources.portfolio_writer import (
    write_session,
    load_portfolio,
    _get_open_value,
    get_live_account_value,
)

_CONFIG_DIR = Path(__file__).parent / "config"


def _load_picker_config() -> dict:
    """Load run settings from config/picker_config.json."""
    path = _CONFIG_DIR / "picker_config.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logging.getLogger(__name__).warning(
            "Could not load picker_config.json — using defaults"
        )
        return {}


async def main():
    parser = argparse.ArgumentParser(description="Inf Money Stock Bot")
    parser.add_argument(
        "--test",
        action="store_true",
        help="Test mode: write to portfolio_test.json instead of portfolio.json",
    )
    args = parser.parse_args()
    test_mode: bool = args.test

    setup_logging()
    logger = logging.getLogger(__name__)

    if test_mode:
        logger.info("*** TEST MODE — output goes to portfolio_test.json, real data untouched ***")

    logger.info(
        "Starting stock bot (mode=%s, host=%s, port=%s, client_id=%s)",
        ib_settings.mode,
        ib_settings.host,
        ib_settings.port,
        ib_settings.client_id,
    )

    config = _load_picker_config()
    excluded_tickers: list[str] = config.get("always_exclude", [])
    excluded_set = set(excluded_tickers)
    num_stocks: int = config.get("num_stocks", 10)
    min_score: int = config.get("min_score", 7)
    max_open_gap_pct: float = config.get("max_open_gap_pct", 5.0)
    trend_filters: dict | None = config.get("trend_filters") or None
    aggressive_mode: bool = config.get("aggressive_mode", False)

    logger.info(
        "Loaded config — aggressive_mode=%s, num_stocks=%d",
        aggressive_mode, num_stocks,
    )

    ib = await connect_ib_async()

    if ib.isConnected():
        logger.info("Connected to IBKR")
    else:
        logger.error("Failed to connect to IBKR")
        disconnect_ib()
        sys.exit(1)

    # Allow IBKR a moment to push account data after connection
    await asyncio.sleep(2)

    # Exclude tickers already held in the account so we never double-buy
    held_positions = ib.positions(account=ib_settings.account)
    held_tickers = {
        p.contract.symbol
        for p in held_positions
        if p.contract.secType == "STK" and p.position != 0
    }
    if held_tickers:
        logger.info(
            "Excluding %d already-held tickers from picks: %s",
            len(held_tickers), ", ".join(sorted(held_tickers)),
        )
        excluded_set |= held_tickers

    # 1. Scan — all 15 scan codes run in parallel (~15 s → ~2 s)
    universe = await get_scanner_universe_async(ib, config["scanner"])
    universe = [s for s in universe if s["ticker"] not in excluded_set]
    logger.info("Scanner universe: %d unique tickers", len(universe))

    # Shared semaphore for IBKR historical data requests (pacing: ~50 req/10 s)
    hist_sem = asyncio.Semaphore(10)

    # 2. Filter — run all per-ticker checks in parallel
    spy_return: float | None = None
    if aggressive_mode:
        effective_min_score = config.get("aggressive_min_score", 9)

        # SPY check runs concurrently with the aggressive filter batch
        filter_coros = [
            passes_aggressive_filters_async(s["ticker"], ib, max_open_gap_pct, hist_sem)
            for s in universe
        ]
        spy_return, *filter_results = await asyncio.gather(
            get_spy_day_return_async(ib),
            *filter_coros,
        )

        if spy_return is not None:
            logger.info("SPY day return: %.2f%%", spy_return)
            if spy_return < config.get("spy_down_threshold", -1.0):
                effective_min_score = 10
                logger.info("SPY down %.2f%% — raising min_score to 10", spy_return)

        survivors = [s for s, passed in zip(universe, filter_results) if passed]
        logger.info("After aggressive filter: %d survivors", len(survivors))

    else:
        effective_min_score = min_score

        if trend_filters:
            trend_filter_results = await asyncio.gather(*[
                passes_trend_filters_async(s["ticker"], ib, trend_filters, hist_sem)
                for s in universe
            ])
            survivors = [s for s, passed in zip(universe, trend_filter_results) if passed]
        else:
            survivors = universe
        logger.info("After trend filter: %d survivors", len(survivors))

        gap_results = await asyncio.gather(*[
            passes_gap_filter_async(s["ticker"], ib, max_open_gap_pct, hist_sem)
            for s in survivors
        ])
        survivors = [s for s, passed in zip(survivors, gap_results) if passed]
        logger.info("After gap filter: %d survivors", len(survivors))

    # 3. News — all tickers fetched in parallel (up to 5 concurrent)
    news_by_ticker = await fetch_news_for_tickers_async(survivors, ib, config["news"])

    # 4. Trend data — all tickers fetched in parallel
    logger.info("Fetching trend data for %d tickers with news", len(news_by_ticker))
    trend_sem = asyncio.Semaphore(10)
    tickers_with_news = list(news_by_ticker.keys())
    trend_results = await asyncio.gather(*[
        get_trend_for_scoring_async(ticker, ib, trend_sem)
        for ticker in tickers_with_news
    ])
    trend_by_ticker: dict[str, str] = {}
    for ticker, trend in zip(tickers_with_news, trend_results):
        trend_by_ticker[ticker] = fmt_trend_for_prompt(trend)
        logger.info("trend: %s — %s", ticker, trend_by_ticker[ticker])

    # 5. Score all candidates with GPT (run in executor so event loop stays responsive)
    loop = asyncio.get_running_loop()
    all_scored = await loop.run_in_executor(
        None,
        lambda: score_candidates(news_by_ticker, excluded_set, trend_by_ticker),
    )

    # 6. Try to fill num_stocks slots — lower threshold until we hit the target
    picks: list[dict] = []
    score_floor = config.get("score_floor", 4)
    threshold = effective_min_score

    while threshold >= score_floor:
        picks = filter_and_rank(all_scored, num_stocks, min_score=threshold)
        if len(picks) >= num_stocks:
            logger.info(
                "Target of %d stocks reached at min_score=%d", num_stocks, threshold
            )
            break
        logger.info(
            "Only %d picks at min_score=%d — lowering threshold to %d",
            len(picks), threshold, threshold - 1,
        )
        threshold -= 1

    # If still short, expand to conservative candidate pool
    if len(picks) < num_stocks and aggressive_mode:
        logger.info(
            "Still short (%d/%d) — expanding to conservative candidate pool",
            len(picks), num_stocks,
        )
        already_scored = {p["ticker"] for p in all_scored}
        conservative_extras = [
            s for s in universe
            if s["ticker"] not in already_scored
            and s["ticker"] not in excluded_set
        ]
        if conservative_extras:
            gap_results2 = await asyncio.gather(*[
                passes_gap_filter_async(s["ticker"], ib, max_open_gap_pct, hist_sem)
                for s in conservative_extras
            ])
            conservative_extras = [s for s, passed in zip(conservative_extras, gap_results2) if passed]

        if conservative_extras:
            extra_news = await fetch_news_for_tickers_async(conservative_extras, ib, config["news"])
            extra_tickers = list(extra_news.keys())
            extra_trend_results = await asyncio.gather(*[
                get_trend_for_scoring_async(ticker, ib, trend_sem)
                for ticker in extra_tickers
            ])
            for ticker, trend in zip(extra_tickers, extra_trend_results):
                trend_by_ticker[ticker] = fmt_trend_for_prompt(trend)
            extra_scored = await loop.run_in_executor(
                None,
                lambda: score_candidates(extra_news, excluded_set, trend_by_ticker),
            )
            all_scored = all_scored + extra_scored
            picks = filter_and_rank(all_scored, num_stocks, min_score=score_floor)
            logger.info(
                "After conservative expansion: %d picks (min_score=%d)",
                len(picks), score_floor,
            )

    logger.info("Final picks (%d/%d):", len(picks), num_stocks)
    for p in picks:
        logger.info(
            "  %s score=%d risk=%d gain=%.1f%% alloc=%.1f%% — %s",
            p["ticker"], p["score"], p.get("risk", 0),
            p.get("expected_gain_pct", 0), p.get("allocation_pct", 0), p["reason"],
        )

    # 7. Execute buy orders via IBKR
    _portfolio = load_portfolio(test_mode=test_mode)

    live_balance = get_live_account_value(ib)
    if live_balance is not None:
        open_value = live_balance
        logger.info("Capital base: $%.2f (live from IBKR NetLiquidation)", open_value)
    else:
        open_value = _get_open_value(_portfolio)
        logger.info("Capital base: $%.2f (from portfolio.json — IBKR balance unavailable)", open_value)

    trades_by_ticker: dict[str, list] = {}
    if picks:
        logger.info("Executing %d buy orders — capital base $%.2f", len(picks), open_value)
        for pick in picks:
            alloc_pct = pick.get("allocation_pct") or round(100.0 / len(picks), 1)
            alloc_usd = alloc_pct / 100.0 * open_value
            try:
                trades = await buy_stock_async(pick["ticker"], ib, dollar_amount=alloc_usd)
                trades_by_ticker[pick["ticker"]] = trades
                logger.info("BUY submitted: %s $%.2f (%.1f%%)", pick["ticker"], alloc_usd, alloc_pct)
            except Exception:
                logger.error("Buy order failed for %s", pick["ticker"], exc_info=True)
                trades_by_ticker[pick["ticker"]] = []

        # Wait for market-order fills (normally < 1 s at open; allow 10 s)
        logger.info("Waiting 10 s for order fills…")
        await asyncio.sleep(10)

        # Log fill summary
        for ticker, trades in trades_by_ticker.items():
            for t in trades[:1]:  # entry order
                status = getattr(t, "orderStatus", None)
                if status:
                    logger.info(
                        "Fill: %s status=%s filled=%.0f avgPrice=%.4f",
                        ticker, status.status, status.filled, status.avgFillPrice,
                    )

    # 8. Pre-fetch QQQ price asynchronously (avoids sync IBKR call inside write_session)
    from ib_insync import Stock as _Stock
    qqq_price: float | None = None
    try:
        qqq_bars = await ib.reqHistoricalDataAsync(
            _Stock("QQQ", "SMART", "USD"),
            endDateTime="",
            durationStr="1 D",
            barSizeSetting="1 min",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=2,
        )
        if qqq_bars:
            qqq_price = round(float(qqq_bars[-1].close), 4)
            logger.info("QQQ close price: $%.4f", qqq_price)
    except Exception:
        logger.warning("Could not fetch QQQ price — will be null in portfolio")

    # Record session to portfolio.json (or portfolio_test.json in test mode)
    mode_label = "aggressive" if aggressive_mode else "conservative"
    write_session(
        picks, ib,
        mode=mode_label,
        spy_return=spy_return,
        test_mode=test_mode,
        trades_by_ticker=trades_by_ticker,
        open_value_override=open_value,
        qqq_price_override=qqq_price,
    )

    disconnect_ib()
    logger.info("Disconnected from IBKR. Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
