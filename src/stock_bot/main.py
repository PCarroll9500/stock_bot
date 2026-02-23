# src/stock_bot/main.py

import argparse
import json
import logging
import sys
from pathlib import Path

from stock_bot.core.logging_config import setup_logging
from stock_bot.config.settings import ib_settings
from stock_bot.brokers.ib.connect_disconnect import connect_ib, disconnect_ib
from stock_bot.brokers.ib.buy_stocks import buy_stock
from stock_bot.data_sources.scanner import get_scanner_universe
from stock_bot.data_sources.news_fetcher import fetch_news_for_tickers
from stock_bot.data_sources.trend_checker import (
    passes_trend_filters,
    passes_gap_filter,
    passes_aggressive_filters,
    get_spy_day_return,
    get_trend_for_scoring,
    fmt_trend_for_prompt,
)
from stock_bot.ai.catalyst_scorer import score_candidates, filter_and_rank
from stock_bot.data_sources.portfolio_writer import (
    write_session,
    load_portfolio,
    _get_open_value,
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


def main():
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

    ib = connect_ib()

    if ib.isConnected():
        logger.info("Connected to IBKR")
    else:
        logger.error("Failed to connect to IBKR")
        disconnect_ib()
        sys.exit(1)

    # TODO: merge current IBKR holdings into excluded_set so the picker
    #       never re-buys a stock already held in the account.
    #       e.g. held = [p.contract.symbol for p in ib.positions()]
    #            excluded_set |= set(held)

    # 1. Scan
    universe = get_scanner_universe(ib, config["scanner"])
    universe = [s for s in universe if s["ticker"] not in excluded_set]
    logger.info("Scanner universe: %d unique tickers", len(universe))

    # 2. Filter
    spy_return: float | None = None
    if aggressive_mode:
        effective_min_score = config.get("aggressive_min_score", 9)

        # SPY context check — down market raises the bar
        spy_return = get_spy_day_return(ib)
        if spy_return is not None:
            logger.info("SPY day return: %.2f%%", spy_return)
            if spy_return < config.get("spy_down_threshold", -1.0):
                effective_min_score = 10
                logger.info("SPY down %.2f%% — raising min_score to 10", spy_return)

        # Skip trend filter entirely — historical trend is irrelevant for binary events
        survivors = universe
        logger.info("Aggressive mode: skipping trend filter, %d candidates", len(survivors))

        # Aggressive momentum filter: reject fading gaps and red-on-day stocks
        survivors = [s for s in survivors if passes_aggressive_filters(s["ticker"], ib, max_open_gap_pct)]
        logger.info("After aggressive filter: %d survivors", len(survivors))

    else:
        effective_min_score = min_score

        # Conservative: trend filter then simple gap filter
        if trend_filters:
            survivors = [s for s in universe if passes_trend_filters(s["ticker"], ib, trend_filters)]
        else:
            survivors = universe
        logger.info("After trend filter: %d survivors", len(survivors))

        survivors = [s for s in survivors if passes_gap_filter(s["ticker"], ib, max_open_gap_pct)]
        logger.info("After gap filter: %d survivors", len(survivors))

    # 3. News
    news_by_ticker = fetch_news_for_tickers(survivors, ib, config["news"])

    # 4. Trend data for tickers that have news (used by GPT for context + scoring adjustment)
    logger.info("Fetching trend data for %d tickers with news", len(news_by_ticker))
    trend_by_ticker: dict[str, str] = {}
    for ticker in news_by_ticker:
        trend = get_trend_for_scoring(ticker, ib)
        trend_by_ticker[ticker] = fmt_trend_for_prompt(trend)
        logger.info("trend: %s — %s", ticker, trend_by_ticker[ticker])

    # 5. Score all candidates once with GPT (no repeated API calls on retry)
    all_scored = score_candidates(news_by_ticker, excluded_set, trend_by_ticker)

    # 6. Try to fill num_stocks slots — lower aggression threshold until we hit the target
    picks: list[dict] = []
    score_floor = config.get("score_floor", 4)  # never go below this
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

    # If still short after threshold relaxation, expand the candidate pool to
    # the broader conservative universe (gap filter only, no aggressive filter)
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
            and passes_gap_filter(s["ticker"], ib, max_open_gap_pct)
        ]
        if conservative_extras:
            extra_news = fetch_news_for_tickers(conservative_extras, ib, config["news"])
            for ticker in extra_news:
                trend = get_trend_for_scoring(ticker, ib)
                trend_by_ticker[ticker] = fmt_trend_for_prompt(trend)
            extra_scored = score_candidates(extra_news, excluded_set, trend_by_ticker)
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
    if picks:
        _portfolio = load_portfolio(test_mode=test_mode)
        open_value = _get_open_value(_portfolio)
        logger.info("Executing %d buy orders — portfolio value $%.2f", len(picks), open_value)
        for pick in picks:
            alloc_pct = pick.get("allocation_pct") or round(100.0 / len(picks), 1)
            alloc_usd = alloc_pct / 100.0 * open_value
            try:
                buy_stock(pick["ticker"], ib, dollar_amount=alloc_usd)
                logger.info("BUY submitted: %s $%.2f (%.1f%%)", pick["ticker"], alloc_usd, alloc_pct)
            except Exception:
                logger.error("Buy order failed for %s", pick["ticker"], exc_info=True)

    # 8. Record session to portfolio.json (or portfolio_test.json in test mode)
    mode_label = "aggressive" if aggressive_mode else "conservative"
    write_session(picks, ib, mode=mode_label, spy_return=spy_return, test_mode=test_mode)

    disconnect_ib()
    logger.info("Disconnected from IBKR. Shutdown complete.")


if __name__ == "__main__":
    main()
