# src/stock_bot/main.py

import argparse
import asyncio
import json
import logging
import math
import sys
from pathlib import Path

from stock_bot.core.logging_config import setup_logging
from stock_bot.config.settings import ib_settings
from stock_bot.brokers.ib.connect_disconnect import connect_ib_async, disconnect_ib
from stock_bot.brokers.ib.buy_stocks import buy_stock_async, get_price_async
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
from stock_bot.brokers.ib.sell_all import close_position
from stock_bot.data_sources.portfolio_writer import (
    write_session,
    load_portfolio,
    _get_open_value,
    get_live_account_value,
    get_net_liquidation,
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


async def _safe(coro, default=None):
    """Wrap a coroutine so one failure returns `default` instead of crashing a gather."""
    try:
        return await coro
    except Exception as exc:
        logger.warning("async task failed (%s: %s) -- using default", type(exc).__name__, exc)
        return default


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
    realloc_fill_wait_seconds: int = config.get("realloc_fill_wait_seconds", 20)
    min_expected_gain_pct: float = config.get("min_expected_gain_pct", 0.0)
    sector_cap: int | None = config.get("sector_cap")
    take_profit_pct: float | None = config.get("take_profit_pct")
    stop_loss_pct: float | None = config.get("stop_loss_pct")
    limit_order_buffer_pct: float | None = config.get("limit_order_buffer_pct", 0.5)
    logger.info("Limit order buffer: %.2f%% (%s mode)", limit_order_buffer_pct or 0, ib_settings.mode)

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

    # Sanity check: this is a cash-only account — shorts must never exist
    held_positions = ib.positions(account=ib_settings.account)
    short_positions = [
        p for p in held_positions
        if p.contract.secType == "STK" and p.position < 0
    ]
    if short_positions:
        logger.error(
            "CASH-ONLY VIOLATION: %d short position(s) detected — aborting session. "
            "Run liquidate_paper.py manually to resolve: %s",
            len(short_positions),
            ", ".join(f"{p.contract.symbol} x{abs(p.position):.0f}" for p in short_positions),
        )
        disconnect_ib()
        sys.exit(1)

    # Exclude tickers already held long in the account so we never double-buy
    held_tickers = {
        p.contract.symbol
        for p in held_positions
        if p.contract.secType == "STK" and p.position > 0
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
            _gr = await asyncio.gather(
                _safe(get_spy_day_return_async(ib)),
                *[_safe(c, False) for c in filter_coros],
            )
            spy_return, filter_results = _gr[0], list(_gr[1:])

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
                    _safe(passes_trend_filters_async(s["ticker"], ib, trend_filters, hist_sem), False)
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
                _safe(passes_gap_filter_async(s["ticker"], ib, max_open_gap_pct, hist_sem), False)
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
            _safe(get_trend_for_scoring_async(ticker, ib, trend_sem))
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
    pre_score_rejected: set[str] = set()  # tickers explicitly failed — never re-enter via expansion
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
            else:
                pre_score_rejected.add(ticker)
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
    if spy_return is not None:
        spy_context = f"SPY is {'up' if spy_return >= 0 else 'down'} {abs(spy_return):.2f}% today."
    else:
        spy_context = "SPY return unavailable."

    if sequential:
        all_scored = score_candidates(news_by_ticker, excluded_set, trend_by_ticker, sequential=True, spy_context=spy_context)
    else:
        loop = asyncio.get_running_loop()
        all_scored = await loop.run_in_executor(
            None,
            lambda: score_candidates(news_by_ticker, excluded_set, trend_by_ticker, spy_context=spy_context),
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
        picks = filter_and_rank(all_scored, num_stocks, min_score=threshold, min_expected_gain_pct=min_expected_gain_pct, sector_cap=sector_cap)
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
        # Identify universe members that were never sent to the AI scorer.
        # Exclude tickers that were explicitly rejected by the pre-score trend
        # filter — they failed a hard threshold and should not re-enter here.
        already_scored = {p["ticker"] for p in all_scored}
        conservative_extras = [
            s for s in universe
            if s["ticker"] not in already_scored
            and s["ticker"] not in excluded_set
            and s["ticker"] not in pre_score_rejected
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
                    _safe(passes_gap_filter_async(s["ticker"], ib, max_open_gap_pct, hist_sem), False)
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
                    _safe(get_trend_for_scoring_async(ticker, ib, trend_sem))
                    for ticker in extra_tickers
                ])

            extra_raw_trend: dict[str, dict] = {}
            for ticker, trend in zip(extra_tickers, extra_trend_results):
                if trend:
                    extra_raw_trend[ticker] = trend
                trend_by_ticker[ticker] = fmt_trend_for_prompt(trend)

            # Apply the same pre-score trend filter to conservative extras so
            # tickers with bad trends can't enter through the expansion path.
            if pre_score_filters:
                filtered_extra_news: dict = {}
                for ticker, articles in extra_news.items():
                    raw = extra_raw_trend.get(ticker) or {}
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
                            continue
                        if min_val is not None and val < min_val:
                            logger.info(
                                "conservative_expansion pre_score_filter: %s rejected — %s %.1f%% < min %.1f%%",
                                ticker, period, val, min_val,
                            )
                            passed = False
                            break
                        if max_val is not None and val > max_val:
                            logger.info(
                                "conservative_expansion pre_score_filter: %s rejected — %s %.1f%% > max %.1f%%",
                                ticker, period, val, max_val,
                            )
                            passed = False
                            break
                    if passed:
                        filtered_extra_news[ticker] = articles
                extra_news = filtered_extra_news

            if sequential:
                extra_scored = score_candidates(extra_news, excluded_set, trend_by_ticker, sequential=True, spy_context=spy_context)
            else:
                loop = asyncio.get_running_loop()
                extra_scored = await loop.run_in_executor(
                    None,
                    lambda: score_candidates(extra_news, excluded_set, trend_by_ticker, spy_context=spy_context),
                )

            # Merge with original scored list and re-rank at the score floor
            all_scored = all_scored + extra_scored
            picks = filter_and_rank(all_scored, num_stocks, min_score=score_floor, min_expected_gain_pct=min_expected_gain_pct, sector_cap=sector_cap)
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

    # cash_balance  — CashBalance from IBKR: used for order sizing only so we
    #                 never allocate against margin or unsettled positions.
    # record_open   — NetLiquidation from IBKR: the true account value recorded
    #                 as portfolio_open_value. After overnight liquidation these
    #                 are equal; keeping them separate makes the intent explicit.
    cash_balance = get_live_account_value(ib)
    record_open = get_net_liquidation(ib)

    if cash_balance is not None:
        open_value = cash_balance
        logger.info("Order sizing base: $%.2f (CashBalance from IBKR)", open_value)
    else:
        open_value = _get_open_value(_portfolio)
        logger.info("Order sizing base: $%.2f (from portfolio.json — IBKR unavailable)", open_value)

    if record_open is not None:
        logger.info("Portfolio open value: $%.2f (NetLiquidation from IBKR)", record_open)
    else:
        record_open = open_value
        logger.warning("Portfolio open value: falling back to CashBalance $%.2f (NetLiquidation unavailable)", record_open)

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

        # Normalize allocations so they never exceed 100% of cash (prevents margin use)
        raw_allocs = [p.get("allocation_pct") or round(100.0 / len(picks), 1) for p in picks]
        total_alloc = sum(raw_allocs)
        if total_alloc > 100.0:
            logger.warning(
                "AI allocations sum to %.1f%% — normalizing to 100%% to avoid margin", total_alloc
            )
            raw_allocs = [a / total_alloc * 100.0 for a in raw_allocs]

        # Pre-flight: fetch all prices in parallel then calculate exact share
        # counts so total committed cash never exceeds the available balance.
        alloc_pairs = list(zip(picks, raw_allocs))
        price_results = await asyncio.gather(
            *[get_price_async(pick["ticker"], ib) for pick, _ in alloc_pairs],
            return_exceptions=True,
        )
        order_plans: list[tuple] = []  # (pick, alloc_pct, shares, est_price)
        for (pick, alloc_pct), result in zip(alloc_pairs, price_results):
            if isinstance(result, Exception):
                logger.error("Pre-flight price fetch failed for %s — skipping: %s", pick["ticker"], result)
                continue
            alloc_usd = alloc_pct / 100.0 * open_value
            shares = math.floor(alloc_usd / result)
            order_plans.append((pick, alloc_pct, shares, result))

        projected_cost = sum(shares * price for _, _, shares, price in order_plans)
        if projected_cost > open_value:
            scale = open_value / projected_cost
            logger.warning(
                "Pre-flight: projected cost $%.2f exceeds budget $%.2f — scaling all positions by %.4f",
                projected_cost, open_value, scale,
            )
            order_plans = [
                (pick, alloc_pct, math.floor(shares * scale), price)
                for pick, alloc_pct, shares, price in order_plans
            ]
            projected_cost = sum(shares * price for _, _, shares, price in order_plans)
        logger.info(
            "Pre-flight complete: %d positions, projected cost $%.2f of $%.2f budget",
            len(order_plans), projected_cost, open_value,
        )

        for pick, alloc_pct, shares, preflight_price in order_plans:
            if shares <= 0:
                logger.warning("Skipping %s — 0 shares after budget check", pick["ticker"])
                continue
            limit_price: float | None = None
            if limit_order_buffer_pct is not None:
                limit_price = round(preflight_price * (1 + limit_order_buffer_pct / 100), 2)
            try:
                trades = await buy_stock_async(
                    pick["ticker"], ib,
                    shares=shares,
                    limit_price=limit_price,
                    take_profit_pct=take_profit_pct,
                    stop_loss_pct=stop_loss_pct,
                )
                trades_by_ticker[pick["ticker"]] = trades
                order_type = f"LMT ${limit_price:.2f}" if limit_price else "MKT"
                logger.info("BUY submitted: %s x%d %s (%.1f%%)", pick["ticker"], shares, order_type, alloc_pct)
            except Exception:
                logger.error("Buy order failed for %s", pick["ticker"], exc_info=True)
                trades_by_ticker[pick["ticker"]] = []

        # Wait for market-order fills
        logger.info("Waiting %d s for order fills…", fill_wait_seconds)
        await asyncio.sleep(fill_wait_seconds)

        # Verify fills — accept filled orders AND orders queued for market open
        # (PreSubmitted/Submitted = accepted by IBKR, will fill at next open)
        _QUEUED_STATUSES = {"PreSubmitted", "Submitted", "ApiPending"}
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
                    elif status.status in _QUEUED_STATUSES:
                        confirmed = True
                        logger.warning(
                            "Fill check: %s order queued (%s) -- recording with estimated price",
                            ticker, status.status,
                        )
            if confirmed:
                filled_picks.append(pick)
            else:
                logger.warning(
                    "BUY NOT CONFIRMED -- %s will NOT be recorded in portfolio", ticker
                )

        unfilled = len(picks) - len(filled_picks)
        if unfilled:
            logger.warning(
                "%d of %d picks were NOT filled and will be excluded from the portfolio",
                unfilled, len(picks),
            )

        # ── Reallocation: redeploy capital from unfilled limit orders ──────
        # Cancel unfilled orders and calculate freed cash. One pass only:
        # first try topping up already-filled positions, then one reserve
        # candidate if meaningful cash still remains.
        _MIN_REALLOC_USD = 200.0
        unfilled_plans = [
            (pick, shares, preflight_price)
            for pick, _alloc_pct, shares, preflight_price in order_plans
            if pick not in filled_picks
        ]
        freed_cash = sum(shares * price for _, shares, price in unfilled_plans)

        if unfilled_plans and freed_cash >= _MIN_REALLOC_USD:
            # Cancel the unfilled limit orders so IBKR doesn't fill them later
            for pick, _, _ in unfilled_plans:
                for t in (trades_by_ticker.get(pick["ticker"]) or [])[:1]:
                    try:
                        ib.cancelOrder(t.order)
                    except Exception:
                        logger.warning("Could not cancel order for %s", pick["ticker"])

            logger.info(
                "Reallocation: freed $%.2f from %d unfilled order(s) — redistributing",
                freed_cash, len(unfilled_plans),
            )

            # Round 1: top up existing filled picks proportionally
            if filled_picks:
                filled_tickers = {p["ticker"] for p in filled_picks}
                filled_plans = [
                    (pick, shares, price)
                    for pick, _alloc_pct, shares, price in order_plans
                    if pick["ticker"] in filled_tickers
                ]
                total_filled_cost = sum(s * p for _, s, p in filled_plans) or 1.0
                realloc_trades: dict[str, list] = {}
                cash_used = 0.0

                # Fetch fresh prices in parallel for realloc sizing/limits
                r1_tickers = [fp["ticker"] for fp, _, _ in filled_plans]
                r1_prices_raw = await asyncio.gather(
                    *[get_price_async(t, ib) for t in r1_tickers],
                    return_exceptions=True,
                )
                r1_prices = {
                    t: p for t, p in zip(r1_tickers, r1_prices_raw)
                    if not isinstance(p, Exception)
                }

                for fp, fp_shares, fp_price in filled_plans:
                    fresh_price = r1_prices.get(fp["ticker"], fp_price)
                    weight = (fp_shares * fp_price) / total_filled_cost
                    extra_usd = freed_cash * weight
                    extra_shares = math.floor(extra_usd / fresh_price)
                    if extra_shares <= 0:
                        continue
                    extra_limit = round(fresh_price * (1 + limit_order_buffer_pct / 100), 2) if limit_order_buffer_pct else None
                    try:
                        t = await buy_stock_async(fp["ticker"], ib, shares=extra_shares, limit_price=extra_limit, stop_loss_pct=stop_loss_pct)
                        realloc_trades[fp["ticker"]] = t
                        cash_used += extra_shares * fresh_price
                        order_type = f"LMT ${extra_limit:.2f}" if extra_limit else "MKT"
                        logger.info("Realloc round 1: %s +%d shares %s ($%.2f)", fp["ticker"], extra_shares, order_type, extra_shares * fresh_price)
                    except Exception:
                        logger.error("Realloc round 1: buy failed for %s", fp["ticker"], exc_info=True)
                freed_cash -= cash_used

                # Wait for round 1 fills then append confirmed trades to
                # trades_by_ticker so write_session includes the extra shares
                await asyncio.sleep(realloc_fill_wait_seconds)
                for ticker, r1_trade_list in realloc_trades.items():
                    for t in (r1_trade_list or [])[:1]:
                        status = getattr(t, "orderStatus", None)
                        if status and status.filled > 0:
                            trades_by_ticker.setdefault(ticker, []).append(t)
                            logger.info(
                                "Realloc round 1: %s confirmed +%.0f @ $%.4f",
                                ticker, status.filled, status.avgFillPrice,
                            )
                        else:
                            logger.warning("Realloc round 1: %s did not fill — cancelling", ticker)
                            try:
                                ib.cancelOrder(t.order)
                            except Exception:
                                pass

            # Round 2: one reserve candidate if meaningful cash remains
            if freed_cash >= _MIN_REALLOC_USD:
                filled_tickers = {p["ticker"] for p in filled_picks}
                original_tickers = {p["ticker"] for p in picks}
                reserve_candidates = [
                    s for s in all_scored
                    if s["ticker"] not in original_tickers
                    and s["ticker"] not in filled_tickers
                    and s.get("score", 0) >= score_floor
                    and s.get("direction") == "bullish"
                ]
                if reserve_candidates:
                    reserve = reserve_candidates[0]
                    try:
                        res_price = await get_price_async(reserve["ticker"], ib)
                        res_shares = math.floor(freed_cash / res_price)
                        if res_shares > 0:
                            res_limit = round(res_price * (1 + limit_order_buffer_pct / 100), 2) if limit_order_buffer_pct else None
                            res_trades = await buy_stock_async(reserve["ticker"], ib, shares=res_shares, limit_price=res_limit, stop_loss_pct=stop_loss_pct)
                            trades_by_ticker[reserve["ticker"]] = res_trades
                            order_type = f"LMT ${res_limit:.2f}" if res_limit else "MKT"
                            logger.info(
                                "Realloc round 2: reserve pick %s x%d %s (score=%d, $%.2f)",
                                reserve["ticker"], res_shares, order_type, reserve.get("score", 0), res_shares * res_price,
                            )
                            # Wait briefly for fill then verify
                            await asyncio.sleep(realloc_fill_wait_seconds)
                            for t in (res_trades or [])[:1]:
                                status = getattr(t, "orderStatus", None)
                                if status and status.filled > 0:
                                    reserve["shares"] = int(status.filled)
                                    reserve["buy_price"] = round(status.avgFillPrice, 4)
                                    reserve["buy_value"] = round(status.filled * status.avgFillPrice, 2)
                                    filled_picks.append(reserve)
                                    logger.info(
                                        "Realloc round 2: %s filled %.0f @ $%.4f",
                                        reserve["ticker"], status.filled, status.avgFillPrice,
                                    )
                                else:
                                    logger.warning("Realloc round 2: %s did not fill — cancelling", reserve["ticker"])
                                    try:
                                        ib.cancelOrder(res_trades[0].order)
                                    except Exception:
                                        pass
                    except Exception:
                        logger.error("Realloc round 2: failed for %s", reserve["ticker"], exc_info=True)
                else:
                    logger.info("Realloc round 2: no eligible reserve candidates")

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
        open_value_override=record_open,
        qqq_price_override=qqq_price,
    )

    # ------------------------------------------------------------------ #
    # SHUTDOWN                                                             #
    # ------------------------------------------------------------------ #
    disconnect_ib()
    logger.info("Disconnected from IBKR. Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
