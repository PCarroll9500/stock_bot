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
    # ------------------------------------------------------------------ #
    # SETUP — parse flags, initialise logging, read picker_config.json   #
    # ------------------------------------------------------------------ #
    parser = argparse.ArgumentParser(description="Inf Money Stock Bot")
    parser.add_argument(
        "--test",
        action="store_true",
        help="Test mode: write to portfolio_test.json instead of portfolio.json",
    )
    parser.add_argument(
        "--sequential",
        action="store_true",
        help="Sequential mode: run one ticker at a time — slower but easier to debug in VSCode",
    )
    args = parser.parse_args()
    test_mode: bool = args.test
    sequential: bool = args.sequential

    setup_logging()
    logger = logging.getLogger(__name__)

    if test_mode:
        logger.info("*** TEST MODE — output goes to portfolio_test.json, real data untouched ***")
    if sequential:
        logger.info("*** SEQUENTIAL MODE — tickers processed one at a time ***")

    logger.info(
        "Starting stock bot (mode=%s, host=%s, port=%s, client_id=%s)",
        ib_settings.mode,
        ib_settings.host,
        ib_settings.port,
        ib_settings.client_id,
    )

    # Pull all from picker_config.json so the rest of the
    # function works off local variables rather than raw dict lookups.
    config = _load_picker_config()
    excluded_tickers: list[str] = config.get("always_exclude", [])
    excluded_set = set(excluded_tickers)        # grows as we add held_tickers below
    num_stocks: int = config.get("num_stocks", 10)
    min_score: int = config.get("min_score", 7)
    max_open_gap_pct: float = config.get("max_open_gap_pct", 5.0)
    trend_filters: dict | None = config.get("trend_filters") or None
    aggressive_mode: bool = config.get("aggressive_mode", False)
    fill_wait_seconds: int = config.get("fill_wait_seconds", 60)
    min_expected_gain_pct: float = config.get("min_expected_gain_pct", 0.0)
    take_profit_pct: float | None = config.get("take_profit_pct")
    stop_loss_pct: float | None = config.get("stop_loss_pct")

    logger.info(
        "Loaded config — aggressive_mode=%s, num_stocks=%d",
        aggressive_mode, num_stocks,
    )

    # ------------------------------------------------------------------ #
    # CONNECT — open async IBKR session via ib_insync                    #
    # ------------------------------------------------------------------ #
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
    # TODO: Fix, held_tickers should still be included if chosen.
    if held_tickers:
        logger.info(
            "Excluding %d already-held tickers from picks: %s",
            len(held_tickers), ", ".join(sorted(held_tickers)),
        )
        excluded_set |= held_tickers

    # ------------------------------------------------------------------ #
    # STEP 1 — SCAN                                                        #
    # Sequential: scan codes run one at a time inside get_scanner_universe_async.
    # Parallel:   all scan codes run concurrently (~15 s → ~2 s).         #
    # ------------------------------------------------------------------ #
    universe = await get_scanner_universe_async(ib, config["scanner"])

    # Remove any ticker on the exclusion list before further processing
    universe = [s for s in universe if s["ticker"] not in excluded_set]
    logger.info("Scanner universe: %d unique tickers", len(universe))

    # Semaphore for IBKR historical data pacing (parallel mode only)
    hist_sem = asyncio.Semaphore(10)

    # ------------------------------------------------------------------ #
    # STEP 2 — FILTER                                                      #
    # Narrow the universe down to stocks that meet technical criteria.     #
    # aggressive_mode and conservative_mode take different code paths.     #
    # ------------------------------------------------------------------ #
    spy_return: float | None = None
    if aggressive_mode:
        # Aggressive path: stricter combined filter + SPY market-direction check.
        effective_min_score = config.get("aggressive_min_score", 9)

        if sequential:
            # Sequential: SPY first, then each ticker one at a time
            spy_return = await get_spy_day_return_async(ib)
            filter_results = []
            for s in universe:
                result = await passes_aggressive_filters_async(s["ticker"], ib, max_open_gap_pct, hist_sem)
                filter_results.append(result)
        else:
            # Parallel: SPY check runs concurrently with the aggressive filter batch
            filter_coros = [
                passes_aggressive_filters_async(s["ticker"], ib, max_open_gap_pct, hist_sem)
                for s in universe
            ]
            spy_return, *filter_results = await asyncio.gather(
                get_spy_day_return_async(ib),
                *filter_coros,
            )

        # If SPY is notably down, raise the quality bar to avoid catching
        # falling knives in a broad market sell-off.
        if spy_return is not None:
            logger.info("SPY day return: %.2f%%", spy_return)
            if spy_return < config.get("spy_down_threshold", -1.0):
                effective_min_score = 10
                logger.info("SPY down %.2f%% — raising min_score to 10", spy_return)

        survivors = [s for s, passed in zip(universe, filter_results) if passed]
        logger.info("After aggressive filter: %d survivors", len(survivors))

    else:
        # Conservative path: apply optional trend filter first, then gap filter.
        effective_min_score = min_score

        # Trend filter — checks moving-average / momentum conditions defined in config.
        if trend_filters:
            if sequential:
                trend_filter_results = []
                for s in universe:
                    result = await passes_trend_filters_async(s["ticker"], ib, trend_filters, hist_sem)
                    trend_filter_results.append(result)
            else:
                trend_filter_results = await asyncio.gather(*[
                    passes_trend_filters_async(s["ticker"], ib, trend_filters, hist_sem)
                    for s in universe
                ])
            survivors = [s for s, passed in zip(universe, trend_filter_results) if passed]
        else:
            survivors = universe
        logger.info("After trend filter: %d survivors", len(survivors))

        # Gap filter — rejects stocks whose open gapped more than max_open_gap_pct
        # from the previous close (to avoid chasing extended moves).
        if sequential:
            gap_results = []
            for s in survivors:
                result = await passes_gap_filter_async(s["ticker"], ib, max_open_gap_pct, hist_sem)
                gap_results.append(result)
        else:
            gap_results = await asyncio.gather(*[
                passes_gap_filter_async(s["ticker"], ib, max_open_gap_pct, hist_sem)
                for s in survivors
            ])
        survivors = [s for s, passed in zip(survivors, gap_results) if passed]
        logger.info("After gap filter: %d survivors", len(survivors))

    # ------------------------------------------------------------------ #
    # STEP 3 — NEWS                                                        #
    # Sequential: fetch one ticker at a time.                              #
    # Parallel:   up to 5 tickers fetched concurrently.                   #
    # Tickers without news are silently dropped here.                      #
    # ------------------------------------------------------------------ #
    news_by_ticker = await fetch_news_for_tickers_async(survivors, ib, config["news"])

    # ------------------------------------------------------------------ #
    # STEP 4 — TREND DATA                                                  #
    # Fetch price-trend data for every ticker that has news.               #
    # The raw values are used to pre-filter before GPT (see below).        #
    # The formatted string is injected into the AI scoring prompt.         #
    # ------------------------------------------------------------------ #
    logger.info("Fetching trend data for %d tickers with news", len(news_by_ticker))
    trend_sem = asyncio.Semaphore(10)
    tickers_with_news = list(news_by_ticker.keys())

    if sequential:
        trend_results = []
        for ticker in tickers_with_news:
            result = await get_trend_for_scoring_async(ticker, ib, trend_sem)
            trend_results.append(result)
    else:
        trend_results = await asyncio.gather(*[
            get_trend_for_scoring_async(ticker, ib, trend_sem)
            for ticker in tickers_with_news
        ])

    # Keep both the raw dict (for filtering) and the formatted string (for GPT).
    raw_trend_by_ticker: dict[str, dict] = {}
    trend_by_ticker: dict[str, str] = {}
    for ticker, trend in zip(tickers_with_news, trend_results):
        if trend:
            raw_trend_by_ticker[ticker] = trend
        trend_by_ticker[ticker] = fmt_trend_for_prompt(trend)
        logger.info("trend: %s — %s", ticker, trend_by_ticker[ticker])

    # ------------------------------------------------------------------ #
    # STEP 4b — PRE-SCORE TREND FILTER                                     #
    # Drop tickers that don't meet minimum weekly/monthly/etc thresholds   #
    # before sending to OpenAI.  This saves GPT calls and quota.           #
    # Configured via "pre_score_trend_filters" in picker_config.json.      #
    # Fail-open: tickers with no trend data are kept (data was unavailable).#
    # ------------------------------------------------------------------ #
    pre_score_filters: dict = config.get("pre_score_trend_filters", {})
    if pre_score_filters:
        before_count = len(news_by_ticker)
        filtered_news: dict = {}
        for ticker, articles in news_by_ticker.items():
            raw = raw_trend_by_ticker.get(ticker) or {}
            passed = True
            for period, bounds in pre_score_filters.items():
                if period.startswith("_"):
                    continue
                min_val = bounds.get("min")
                max_val = bounds.get("max")
                if min_val is None and max_val is None:
                    continue
                val = raw.get(period)
                if val is None:
                    continue  # no data — fail-open, keep the ticker
                if min_val is not None and val < min_val:
                    logger.warning(
                        "pre_score_filter: %s REJECTED — %s %.1f%% < min %.1f%%",
                        ticker, period, val, min_val,
                    )
                    passed = False
                    break
                if max_val is not None and val > max_val:
                    logger.warning(
                        "pre_score_filter: %s REJECTED — %s %.1f%% > max %.1f%%",
                        ticker, period, val, max_val,
                    )
                    passed = False
                    break
            if passed:
                filtered_news[ticker] = articles
        news_by_ticker = filtered_news
        logger.warning(
            "pre_score_filter: %d → %d candidates after trend pre-filter",
            before_count, len(news_by_ticker),
        )

    # Cap to the most-newsworthy tickers to keep the scoring phase short.
    # More articles = richer context for GPT and a stronger catalyst signal.
    max_candidates: int = config.get("max_score_candidates", 30)
    if len(news_by_ticker) > max_candidates:
        news_by_ticker = dict(
            sorted(news_by_ticker.items(), key=lambda kv: len(kv[1]), reverse=True)[:max_candidates]
        )
        logger.warning(
            "Capped scoring candidates to top %d by article count (had more)", max_candidates
        )

    # ------------------------------------------------------------------ #
    # STEP 5 — AI SCORING                                                  #
    # Sequential: one GPT call at a time — easy to step through.          #
    # Parallel:   up to 10 concurrent GPT calls via thread-pool executor. #
    # ------------------------------------------------------------------ #
    if sequential:
        all_scored = score_candidates(news_by_ticker, excluded_set, trend_by_ticker, sequential=True)
    else:
        loop = asyncio.get_running_loop()
        all_scored = await loop.run_in_executor(
            None,
            lambda: score_candidates(news_by_ticker, excluded_set, trend_by_ticker),
        )

    # ------------------------------------------------------------------ #
    # STEP 6 — PICK SELECTION                                              #
    # Try to fill exactly num_stocks slots. If not enough candidates meet  #
    # the current threshold, lower it by 1 until reaching score_floor.    #
    # ------------------------------------------------------------------ #
    picks: list[dict] = []
    score_floor = config.get("score_floor", 4)
    threshold = effective_min_score

    while threshold >= score_floor:
        picks = filter_and_rank(all_scored, num_stocks, min_score=threshold, min_expected_gain_pct=min_expected_gain_pct)
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

    # If still short in aggressive mode, re-score the stocks that were
    # filtered out by passes_aggressive_filters (conservative pool) and
    # try to top up the slate.
    if len(picks) < num_stocks and aggressive_mode:
        logger.info(
            "Still short (%d/%d) — expanding to conservative candidate pool",
            len(picks), num_stocks,
        )
        # Identify universe members that were never sent to the AI scorer
        already_scored = {p["ticker"] for p in all_scored}
        conservative_extras = [
            s for s in universe
            if s["ticker"] not in already_scored
            and s["ticker"] not in excluded_set
        ]
        # Still enforce the gap filter on these extras
        if conservative_extras:
            if sequential:
                gap_results2 = []
                for s in conservative_extras:
                    result = await passes_gap_filter_async(s["ticker"], ib, max_open_gap_pct, hist_sem)
                    gap_results2.append(result)
            else:
                gap_results2 = await asyncio.gather(*[
                    passes_gap_filter_async(s["ticker"], ib, max_open_gap_pct, hist_sem)
                    for s in conservative_extras
                ])
            conservative_extras = [s for s, passed in zip(conservative_extras, gap_results2) if passed]

        if conservative_extras:
            # Fetch news + trend then score the conservative extras
            extra_news = await fetch_news_for_tickers_async(conservative_extras, ib, config["news"])
            extra_tickers = list(extra_news.keys())

            if sequential:
                extra_trend_results = []
                for ticker in extra_tickers:
                    result = await get_trend_for_scoring_async(ticker, ib, trend_sem)
                    extra_trend_results.append(result)
            else:
                extra_trend_results = await asyncio.gather(*[
                    get_trend_for_scoring_async(ticker, ib, trend_sem)
                    for ticker in extra_tickers
                ])

            for ticker, trend in zip(extra_tickers, extra_trend_results):
                trend_by_ticker[ticker] = fmt_trend_for_prompt(trend)

            if sequential:
                extra_scored = score_candidates(extra_news, excluded_set, trend_by_ticker, sequential=True)
            else:
                loop = asyncio.get_running_loop()
                extra_scored = await loop.run_in_executor(
                    None,
                    lambda: score_candidates(extra_news, excluded_set, trend_by_ticker),
                )

            # Merge with original scored list and re-rank at the score floor
            all_scored = all_scored + extra_scored
            picks = filter_and_rank(all_scored, num_stocks, min_score=score_floor, min_expected_gain_pct=min_expected_gain_pct)
            logger.info(
                "After conservative expansion: %d picks (min_score=%d)",
                len(picks), score_floor,
            )

    logger.warning("Final picks (%d/%d):", len(picks), num_stocks)
    for p in picks:
        logger.warning(
            "  %s score=%d risk=%d gain=%.1f%% alloc=%.1f%% — %s",
            p["ticker"], p["score"], p.get("risk", 0),
            p.get("expected_gain_pct", 0), p.get("allocation_pct", 0), p["reason"],
        )

    # ------------------------------------------------------------------ #
    # STEP 7 — ORDER EXECUTION                                             #
    # Determine capital base, size each position by allocation_pct, submit #
    # market buy orders, wait for fills, then drop any unfilled picks.    #
    # ------------------------------------------------------------------ #
    _portfolio = load_portfolio(test_mode=test_mode)

    # Prefer the live IBKR NetLiquidation value; fall back to last recorded
    # open_value in portfolio.json if the account query fails.
    live_balance = get_live_account_value(ib)
    if live_balance is not None:
        open_value = live_balance
        logger.info("Capital base: $%.2f (live from IBKR NetLiquidation)", open_value)
    else:
        open_value = _get_open_value(_portfolio)
        logger.info("Capital base: $%.2f (from portfolio.json — IBKR balance unavailable)", open_value)

    trades_by_ticker: dict[str, list] = {}
    if picks and test_mode:
        # TEST MODE — log picks but do not place any real orders
        logger.warning("*** TEST MODE — skipping order execution, no real trades placed ***")
        for pick in picks:
            alloc_pct = pick.get("allocation_pct") or round(100.0 / len(picks), 1)
            alloc_usd = alloc_pct / 100.0 * open_value
            logger.warning(
                "  [DRY RUN] WOULD BUY: %s  score=%d  $%.2f (%.1f%%)  — %s",
                pick["ticker"], pick["score"], alloc_usd, alloc_pct, pick["reason"],
            )

    elif picks:
        logger.info("Executing %d buy orders — capital base $%.2f", len(picks), open_value)
        for pick in picks:
            # Use AI-suggested allocation if present; otherwise split evenly
            alloc_pct = pick.get("allocation_pct") or round(100.0 / len(picks), 1)
            alloc_usd = alloc_pct / 100.0 * open_value
            try:
                trades = await buy_stock_async(
                    pick["ticker"], ib,
                    dollar_amount=alloc_usd,
                    take_profit_pct=take_profit_pct,
                    stop_loss_pct=stop_loss_pct,
                )
                trades_by_ticker[pick["ticker"]] = trades
                logger.info("BUY submitted: %s $%.2f (%.1f%%)", pick["ticker"], alloc_usd, alloc_pct)
            except Exception:
                logger.error("Buy order failed for %s", pick["ticker"], exc_info=True)
                trades_by_ticker[pick["ticker"]] = []

        # Wait for market-order fills
        logger.info("Waiting %d s for order fills…", fill_wait_seconds)
        await asyncio.sleep(fill_wait_seconds)

        # Verify fills — only record picks that were actually purchased
        filled_picks: list[dict] = []
        for pick in picks:
            ticker = pick["ticker"]
            confirmed = False
            for t in (trades_by_ticker.get(ticker) or [])[:1]:  # entry order only
                status = getattr(t, "orderStatus", None)
                if status:
                    logger.info(
                        "Fill check: %s status=%s filled=%.0f avgPrice=%.4f",
                        ticker, status.status, status.filled, status.avgFillPrice,
                    )
                    if status.filled > 0:
                        confirmed = True
            if confirmed:
                filled_picks.append(pick)
            else:
                logger.warning(
                    "BUY NOT CONFIRMED — %s will NOT be recorded in portfolio", ticker
                )

        unfilled = len(picks) - len(filled_picks)
        if unfilled:
            logger.warning(
                "%d of %d picks were NOT filled and will be excluded from the portfolio",
                unfilled, len(picks),
            )
        picks = filled_picks

    # ------------------------------------------------------------------ #
    # STEP 8 — BENCHMARK PRICE                                             #
    # Fetch the QQQ close asynchronously here so write_session doesn't    #
    # need to make a blocking IBKR call.                                  #
    # ------------------------------------------------------------------ #
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

    # ------------------------------------------------------------------ #
    # STEP 9 — PERSIST SESSION                                             #
    # Write the final picks + metadata to portfolio.json (or              #
    # portfolio_test.json in test mode) for the dashboard to consume.     #
    # Reconnect first — the scoring phase can be long enough that TWS     #
    # drops the idle API connection before we get here.                   #
    # ------------------------------------------------------------------ #
    if not ib.isConnected():
        logger.warning("IB disconnected after scoring — reconnecting for portfolio write")
        try:
            await connect_ib_async()
        except Exception:
            logger.warning("Reconnect failed — portfolio prices will be unavailable", exc_info=True)

    mode_label = "aggressive" if aggressive_mode else "conservative"
    write_session(
        picks, ib,
        mode=mode_label,
        spy_return=spy_return,
        test_mode=test_mode,
        trades_by_ticker=None if test_mode else trades_by_ticker,
        open_value_override=open_value,
        qqq_price_override=qqq_price,
    )

    # ------------------------------------------------------------------ #
    # SHUTDOWN                                                             #
    # ------------------------------------------------------------------ #
    disconnect_ib()
    logger.info("Disconnected from IBKR. Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
