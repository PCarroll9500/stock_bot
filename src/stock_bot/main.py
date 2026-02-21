# src/stock_bot/main.py

import json
import logging
import sys
from pathlib import Path

from stock_bot.core.logging_config import setup_logging
from stock_bot.config.settings import ib_settings
from stock_bot.brokers.ib.connect_disconnect import connect_ib, disconnect_ib
from stock_bot.data_sources.scanner import get_scanner_universe
from stock_bot.data_sources.news_fetcher import fetch_news_for_tickers
from stock_bot.data_sources.trend_checker import (
    passes_trend_filters,
    passes_gap_filter,
    passes_aggressive_filters,
    get_spy_day_return,
)
from stock_bot.ai.catalyst_scorer import score_and_rank
from stock_bot.data_sources.portfolio_writer import write_session

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
    setup_logging()
    logger = logging.getLogger(__name__)

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

    # 4. Score + rank (bullish only, effective_min_score threshold applied inside)
    picks = score_and_rank(news_by_ticker, num_stocks, excluded_set, min_score=effective_min_score)

    logger.info("Final picks (%d):", len(picks))
    for p in picks:
        logger.info("  %s (score=%d, %s) — %s", p["ticker"], p["score"], p["direction"], p["reason"])

    # TODO: execute trades via IBKR for each pick

    # 5. Record session to portfolio.json
    mode_label = "aggressive" if aggressive_mode else "conservative"
    write_session(picks, ib, mode=mode_label, spy_return=spy_return)

    disconnect_ib()
    logger.info("Disconnected from IBKR. Shutdown complete.")


if __name__ == "__main__":
    main()
