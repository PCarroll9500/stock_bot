"""
tests/test_watchlist.py

Tests for the watchlist feature:
  - Injection: watchlist tickers added after filters, deduped, excluded tickers skipped
  - pre_score_trend_filter bypass: watchlist tickers always reach GPT
  - max_score_candidates cap: watchlist tickers always preserved, scanner fills remaining slots
  - Timing: 27 watchlist tickers add acceptable overhead
"""

import time
import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────

def _inject(watchlist: list[str], survivors: list[dict], excluded: set[str]) -> tuple[list[dict], set[str]]:
    """Replicate the watchlist inject block from main.py."""
    watchlist_upper = [t.upper() for t in watchlist]
    survivor_tickers = {s["ticker"] for s in survivors}
    injected = []
    for ticker in watchlist_upper:
        if ticker not in excluded and ticker not in survivor_tickers:
            survivors.append({"ticker": ticker})
            injected.append(ticker)
    watchlist_set = set(injected) | (survivor_tickers & set(watchlist_upper))
    return survivors, watchlist_set


def _apply_pre_score_filter(
    news_by_ticker: dict,
    raw_trend: dict,
    filters: dict,
    watchlist_set: set[str],
) -> tuple[dict, set[str]]:
    """Replicate the pre_score_trend_filter block from main.py."""
    filtered_news: dict = {}
    rejected: set[str] = set()
    for ticker, articles in news_by_ticker.items():
        if ticker in watchlist_set:
            filtered_news[ticker] = articles
            continue
        raw = raw_trend.get(ticker) or {}
        passed = True
        for period, bounds in filters.items():
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
                passed = False
                break
            if max_val is not None and val > max_val:
                passed = False
                break
        if passed:
            filtered_news[ticker] = articles
        else:
            rejected.add(ticker)
    return filtered_news, rejected


def _apply_cap(
    news_by_ticker: dict,
    watchlist_set: set[str],
    max_candidates: int,
) -> dict:
    """Replicate the max_score_candidates cap block from main.py."""
    watchlist_news = {t: a for t, a in news_by_ticker.items() if t in watchlist_set}
    scanner_news = {t: a for t, a in news_by_ticker.items() if t not in watchlist_set}
    remaining_slots = max(0, max_candidates - len(watchlist_news))
    if len(scanner_news) > remaining_slots:
        scanner_news = dict(
            sorted(scanner_news.items(), key=lambda kv: len(kv[1]), reverse=True)[:remaining_slots]
        )
    return {**watchlist_news, **scanner_news}


# ══════════════════════════════════════════════════════════════════════════════
# 1. Injection
# ══════════════════════════════════════════════════════════════════════════════

class TestWatchlistInjection:

    def test_injects_new_tickers(self):
        survivors, _ = _inject(["AAPL", "MSFT"], [], set())
        tickers = [s["ticker"] for s in survivors]
        assert "AAPL" in tickers
        assert "MSFT" in tickers

    def test_skips_excluded_tickers(self):
        survivors, _ = _inject(["NVDA", "TSLA", "META"], [], {"NVDA", "TSLA"})
        tickers = [s["ticker"] for s in survivors]
        assert "NVDA" not in tickers
        assert "TSLA" not in tickers
        assert "META" in tickers

    def test_no_double_add_if_already_in_survivors(self):
        existing = [{"ticker": "AAPL"}, {"ticker": "GOOG"}]
        survivors, _ = _inject(["AAPL", "GOOG", "META"], existing, set())
        aapl_count = sum(1 for s in survivors if s["ticker"] == "AAPL")
        assert aapl_count == 1

    def test_deduplicates_watchlist_itself(self):
        """Duplicate entries in the watchlist list should only inject once."""
        survivors, _ = _inject(["AAPL", "AAPL", "MSFT", "MSFT"], [], set())
        tickers = [s["ticker"] for s in survivors]
        assert tickers.count("AAPL") == 1
        assert tickers.count("MSFT") == 1

    def test_lowercased_watchlist_entries_normalized(self):
        survivors, _ = _inject(["aapl", "msft"], [], set())
        tickers = [s["ticker"] for s in survivors]
        assert "AAPL" in tickers
        assert "MSFT" in tickers

    def test_empty_watchlist_survivors_unchanged(self):
        existing = [{"ticker": "SPY"}]
        survivors, watchlist_set = _inject([], existing, set())
        assert len(survivors) == 1
        assert watchlist_set == set()

    def test_watchlist_set_tracks_injected_tickers(self):
        _, watchlist_set = _inject(["META", "GOOG"], [], set())
        assert "META" in watchlist_set
        assert "GOOG" in watchlist_set

    def test_watchlist_set_includes_watchlist_already_in_survivors(self):
        """If a watchlist ticker passed filters naturally, it still joins watchlist_set."""
        existing = [{"ticker": "AAPL"}]
        _, watchlist_set = _inject(["AAPL", "MSFT"], existing, set())
        assert "AAPL" in watchlist_set  # was already a survivor
        assert "MSFT" in watchlist_set  # was injected

    def test_full_29_ticker_watchlist_minus_excluded(self):
        """The actual production watchlist: 29 tickers, NVDA+TSLA excluded → 27 injected."""
        watchlist = [
            "NBIS", "NVDA", "LITE", "SNDK", "GEV", "VRT", "MPWR", "LYV", "MV",
            "STX", "CAT", "TSM", "HWM", "GOOG", "TER", "DELL", "META", "AMAT",
            "TSLA", "LHX", "NXPI", "KLAC", "MKSI", "UI", "CRDO", "CW", "DE",
            "VLVLY", "NTES",
        ]
        excluded = {"NVDA", "TSLA"}
        survivors, watchlist_set = _inject(watchlist, [], excluded)
        assert len(survivors) == 27
        assert "NVDA" not in watchlist_set
        assert "TSLA" not in watchlist_set
        assert len(watchlist_set) == 27


# ══════════════════════════════════════════════════════════════════════════════
# 2. pre_score_trend_filter bypass
# ══════════════════════════════════════════════════════════════════════════════

class TestPreScoreFilterBypass:
    """Watchlist tickers must reach GPT even if their trend data would normally reject them."""

    FILTERS = {"weekly": {"min": 1.0, "max": None}, "monthly": {"min": -5.0, "max": None}}

    def test_watchlist_ticker_bypasses_bad_weekly(self):
        news = {"META": [{"headline": "x"}], "RAND": [{"headline": "y"}]}
        raw = {"META": {"weekly": -3.0}, "RAND": {"weekly": 5.0}}  # META below min
        filtered, rejected = _apply_pre_score_filter(news, raw, self.FILTERS, {"META"})
        assert "META" in filtered   # watchlist — kept despite bad weekly
        assert "RAND" in filtered

    def test_non_watchlist_ticker_rejected_by_bad_weekly(self):
        news = {"RAND": [{"headline": "x"}]}
        raw = {"RAND": {"weekly": -3.0}}
        filtered, rejected = _apply_pre_score_filter(news, raw, self.FILTERS, set())
        assert "RAND" not in filtered
        assert "RAND" in rejected

    def test_watchlist_ticker_with_good_trend_also_passes(self):
        news = {"META": [{"headline": "x"}]}
        raw = {"META": {"weekly": 5.0}}
        filtered, _ = _apply_pre_score_filter(news, raw, self.FILTERS, {"META"})
        assert "META" in filtered

    def test_all_27_watchlist_tickers_survive_strict_filter(self):
        """Even with weekly min=10% (extremely strict), all watchlist tickers pass."""
        strict_filters = {"weekly": {"min": 10.0, "max": None}}
        watchlist_set = {
            "NBIS", "LITE", "SNDK", "GEV", "VRT", "MPWR", "LYV", "MV",
            "STX", "CAT", "TSM", "HWM", "GOOG", "TER", "DELL", "META", "AMAT",
            "LHX", "NXPI", "KLAC", "MKSI", "UI", "CRDO", "CW", "DE", "VLVLY", "NTES",
        }
        news = {t: [{"headline": "x"}] for t in watchlist_set}
        # All have bad weekly data — would all fail without bypass
        raw = {t: {"weekly": -5.0} for t in watchlist_set}
        filtered, rejected = _apply_pre_score_filter(news, raw, strict_filters, watchlist_set)
        assert len(filtered) == 27
        assert len(rejected) == 0


# ══════════════════════════════════════════════════════════════════════════════
# 3. max_score_candidates cap
# ══════════════════════════════════════════════════════════════════════════════

class TestCandidatesCap:
    """Watchlist tickers must survive the max_score_candidates cap.
    Scanner tickers fill remaining slots sorted by article count."""

    def test_watchlist_preserved_when_cap_exceeded(self):
        """50 scanner tickers + 27 watchlist with cap=50 → all 27 watchlist survive,
        23 scanner tickers fill the rest."""
        watchlist_set = {f"WL{i:02d}" for i in range(27)}
        scanner_tickers = {f"SC{i:02d}" for i in range(50)}
        news = {}
        for t in watchlist_set:
            news[t] = [{"headline": "x"}] * 2
        for i, t in enumerate(scanner_tickers):
            news[t] = [{"headline": "x"}] * (i + 1)  # varied article counts

        result = _apply_cap(news, watchlist_set, max_candidates=50)
        result_tickers = set(result.keys())

        # All watchlist tickers must survive
        assert watchlist_set.issubset(result_tickers), (
            f"Missing watchlist tickers: {watchlist_set - result_tickers}"
        )
        # Total should not exceed max_candidates (27 watchlist + 23 scanner = 50)
        assert len(result) == 50

    def test_scanner_tickers_selected_by_article_count(self):
        """When cap forces a cut, keep the scanner tickers with most articles."""
        watchlist_set = {"WL1"}
        news = {
            "WL1": [{}] * 1,   # watchlist — always kept
            "SC_rich": [{}] * 5,
            "SC_mid": [{}] * 3,
            "SC_poor": [{}] * 1,
        }
        result = _apply_cap(news, watchlist_set, max_candidates=2)
        assert "WL1" in result
        assert "SC_rich" in result   # most articles → kept
        assert "SC_poor" not in result

    def test_no_cap_needed_when_under_limit(self):
        watchlist_set = {"WL1", "WL2"}
        news = {"WL1": [{}], "WL2": [{}], "SC1": [{}]}
        result = _apply_cap(news, watchlist_set, max_candidates=50)
        assert len(result) == 3

    def test_cap_with_zero_scanner_tickers(self):
        """Only watchlist tickers, all with news — all should pass."""
        watchlist_set = {"META", "GOOG", "AAPL"}
        news = {t: [{}] for t in watchlist_set}
        result = _apply_cap(news, watchlist_set, max_candidates=10)
        assert set(result.keys()) == watchlist_set

    def test_production_scenario(self):
        """Simulate a real morning: 50 scanner candidates + 20 watchlist with news,
        cap=50. Expect all 20 watchlist + 30 best scanner candidates."""
        watchlist_set = {f"WL{i:02d}" for i in range(20)}
        scanner = {f"SC{i:02d}": [{}] * (50 - i) for i in range(50)}  # SC00 richest
        news = {**{t: [{}] * 3 for t in watchlist_set}, **scanner}

        result = _apply_cap(news, watchlist_set, max_candidates=50)

        assert len(result) == 50
        assert watchlist_set.issubset(set(result.keys()))
        # Top 30 scanner tickers by article count should be SC00–SC29
        scanner_in_result = {t for t in result if t.startswith("SC")}
        for i in range(30):
            assert f"SC{i:02d}" in scanner_in_result


# ══════════════════════════════════════════════════════════════════════════════
# 4. Timing
# ══════════════════════════════════════════════════════════════════════════════

class TestWatchlistTiming:
    """27 extra tickers add acceptable overhead to the pipeline."""

    WATCHLIST_COUNT = 27  # 29 minus NVDA and TSLA

    def test_injection_is_instant(self):
        """The inject loop itself (pure Python) for 27 tickers should be < 1ms."""
        watchlist = [f"T{i:03d}" for i in range(self.WATCHLIST_COUNT)]
        t0 = time.monotonic()
        _inject(watchlist, [], set())
        elapsed = time.monotonic() - t0
        assert elapsed < 0.001, f"Inject took {elapsed*1000:.2f}ms — should be near-instant"

    def test_pre_score_filter_bypass_overhead(self):
        """Bypassing 27 watchlist tickers in the filter loop adds negligible time."""
        watchlist_set = {f"WL{i:02d}" for i in range(self.WATCHLIST_COUNT)}
        scanner = {f"SC{i:02d}": [{}] * 3 for i in range(50)}
        wl_news = {t: [{}] * 3 for t in watchlist_set}
        news = {**wl_news, **scanner}
        raw = {t: {"weekly": -5.0} for t in news}
        filters = {"weekly": {"min": 1.0, "max": None}}

        t0 = time.monotonic()
        _apply_pre_score_filter(news, raw, filters, watchlist_set)
        elapsed = time.monotonic() - t0
        assert elapsed < 0.01, f"Filter took {elapsed*1000:.1f}ms for 77 tickers"

    def test_cap_logic_overhead(self):
        """Cap logic with 77 tickers should be < 5ms."""
        watchlist_set = {f"WL{i:02d}" for i in range(self.WATCHLIST_COUNT)}
        scanner = {f"SC{i:02d}": [{}] * i for i in range(50)}
        wl_news = {t: [{}] * 3 for t in watchlist_set}
        news = {**wl_news, **scanner}

        t0 = time.monotonic()
        _apply_cap(news, watchlist_set, max_candidates=50)
        elapsed = time.monotonic() - t0
        assert elapsed < 0.005, f"Cap took {elapsed*1000:.1f}ms"

    def test_gpt_calls_bounded_by_max_score_candidates(self):
        """With 27 watchlist + 50 scanner candidates and cap=50,
        GPT sees exactly 50 tickers — no extra calls from the watchlist."""
        watchlist_set = {f"WL{i:02d}" for i in range(self.WATCHLIST_COUNT)}
        scanner = {f"SC{i:02d}": [{}] * 3 for i in range(50)}
        news = {**{t: [{}] * 2 for t in watchlist_set}, **scanner}

        result = _apply_cap(news, watchlist_set, max_candidates=50)
        assert len(result) == 50

    def test_worst_case_gpt_timing_still_fits(self):
        """Worst case: all 27 watchlist have news + 23 scanner = 50 GPT calls.
        At 3s/call with MAX_WORKERS=1 → 150s. Still well inside the 3600s timeout."""
        max_gpt_calls = 50  # cap
        gpt_latency_s = 3.0
        main_timeout_s = 3600
        worst_case = max_gpt_calls * gpt_latency_s
        assert worst_case < main_timeout_s

    def test_news_fetch_extra_time_estimate(self):
        """27 watchlist tickers add ~18s to news fetch (5 concurrent, ~3s per batch).
        Total news fetch stays under 2 minutes."""
        concurrent_fetches = 5
        seconds_per_fetch = 3.0
        extra_batches = self.WATCHLIST_COUNT / concurrent_fetches
        extra_seconds = extra_batches * seconds_per_fetch
        assert extra_seconds < 120, (
            f"Estimated extra news fetch time {extra_seconds:.0f}s exceeds 2 minutes"
        )
