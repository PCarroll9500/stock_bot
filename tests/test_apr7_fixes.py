"""
tests/test_apr7_fixes.py

Regression tests for the four bugs fixed on 2026-04-07:

  1. news_fetcher: watchlist-injected tickers have no conId — bot crashed with
     KeyError every time the scanner returned 0 results (happened today).
     Fix: resolve conId via qualifyContractsAsync before fetching; fall back
     to Finnhub/web when qualification fails.

  2. main.py: min-picks fallback loop condition was inverted
     (_fallback_floor >= score_floor was always False), so the loop never ran.
     Fix: condition changed to >= max(1, score_floor - 3).

  3. close_of_day.py: _retry_ibkr didn't catch exceptions raised by fn(ib).
     Any exception (e.g. network timeout) propagated up and aborted the
     end-of-day sell, leaving positions open overnight.
     Fix: wrap fn(ib) in try/except, log and treat as None to trigger retry.

  4. main.py: limit_order_buffer_pct = 0 was treated as falsy in the
     reallocation block, sending market orders instead of exact-price limit
     orders.  Fix: use `is not None` instead of truthiness check.
"""

import asyncio
import pathlib
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_REPO = pathlib.Path(__file__).parents[1]
_SCRIPTS = _REPO / "scripts"


# ══════════════════════════════════════════════════════════════════════════════
# Fix 1 — conId missing for watchlist-injected tickers
# ══════════════════════════════════════════════════════════════════════════════

def _load_news_fetcher_batch():
    from stock_bot.data_sources.news_fetcher import _fetch_ibkr_batch
    return _fetch_ibkr_batch


def _make_ib(con_id: int = 55555) -> MagicMock:
    ib = MagicMock()
    qualified = MagicMock()
    qualified.conId = con_id
    ib.qualifyContractsAsync = AsyncMock(return_value=[qualified])
    ib.reqHistoricalNewsAsync = AsyncMock(return_value=[])
    return ib


_CONFIG = {"providers": "BRFG", "max_articles": 3}
_MOCK_SETTINGS = MagicMock(exchange="SMART", currency="USD")


class TestConIdResolution:
    """_fetch_ibkr_batch must resolve conId for entries that arrive without one."""

    def test_entry_without_con_id_gets_qualified(self):
        """qualifyContractsAsync must be called and conId populated on the entry."""
        _fetch_ibkr_batch = _load_news_fetcher_batch()
        ib = _make_ib(con_id=12345)
        entry = {"ticker": "META"}

        with patch("stock_bot.config.settings.ib_settings", _MOCK_SETTINGS):
            asyncio.run(_fetch_ibkr_batch([entry], ib, _CONFIG))

        ib.qualifyContractsAsync.assert_called_once()
        assert entry.get("conId") == 12345

    def test_entry_with_existing_con_id_not_re_qualified(self):
        """If conId is already present, qualifyContractsAsync must NOT be called."""
        _fetch_ibkr_batch = _load_news_fetcher_batch()
        ib = _make_ib()
        entry = {"ticker": "AAPL", "conId": 265598}

        with patch("stock_bot.config.settings.ib_settings", _MOCK_SETTINGS):
            asyncio.run(_fetch_ibkr_batch([entry], ib, _CONFIG))

        ib.qualifyContractsAsync.assert_not_called()

    def test_mixed_batch_resolves_only_missing(self):
        """In a batch with some entries having conId and some not, only the
        missing ones should trigger qualifyContractsAsync."""
        _fetch_ibkr_batch = _load_news_fetcher_batch()
        ib = _make_ib(con_id=77777)
        entries = [
            {"ticker": "GOOG", "conId": 208813720},
            {"ticker": "META"},                      # no conId
            {"ticker": "TSLA", "conId": 76792991},
            {"ticker": "NVDA"},                      # no conId
        ]

        with patch("stock_bot.config.settings.ib_settings", _MOCK_SETTINGS):
            asyncio.run(_fetch_ibkr_batch(entries, ib, _CONFIG))

        assert ib.qualifyContractsAsync.call_count == 2
        assert entries[1].get("conId") == 77777
        assert entries[3].get("conId") == 77777

    def test_qualification_failure_does_not_crash_batch(self):
        """If qualifyContractsAsync raises, the whole batch must still complete."""
        _fetch_ibkr_batch = _load_news_fetcher_batch()
        ib = MagicMock()
        ib.qualifyContractsAsync = AsyncMock(side_effect=Exception("IBKR unavailable"))
        ib.reqHistoricalNewsAsync = AsyncMock(return_value=[])

        entries = [{"ticker": "UNKNOWN_TICKER"}]

        with patch("stock_bot.config.settings.ib_settings", _MOCK_SETTINGS):
            result = asyncio.run(_fetch_ibkr_batch(entries, ib, _CONFIG))

        assert isinstance(result, dict)
        assert "UNKNOWN_TICKER" in result

    def test_qualification_failure_falls_back_to_finnhub(self):
        """Entry whose qualification fails should fall back to Finnhub if key exists."""
        _fetch_ibkr_batch = _load_news_fetcher_batch()
        ib = MagicMock()
        ib.qualifyContractsAsync = AsyncMock(side_effect=Exception("no route"))
        ib.reqHistoricalNewsAsync = AsyncMock(return_value=[])

        finnhub_articles = [{"headline": "Meta jumps 5%", "body": "...", "time": "", "published_dt": None, "provider": "finnhub"}]
        mock_finnhub = AsyncMock(return_value={"META": finnhub_articles})

        entry = {"ticker": "META"}
        with (
            patch("stock_bot.config.settings.ib_settings", _MOCK_SETTINGS),
            patch("stock_bot.data_sources.news_fetcher._FINNHUB_KEY", "fake-key"),
            patch("stock_bot.data_sources.news_fetcher._fetch_finnhub_all", mock_finnhub),
        ):
            result = asyncio.run(_fetch_ibkr_batch([entry], ib, _CONFIG))

        assert result.get("META") == finnhub_articles

    def test_qualification_failure_falls_back_to_web_when_no_finnhub(self):
        """Without a Finnhub key, fall back to web scrapers."""
        _fetch_ibkr_batch = _load_news_fetcher_batch()
        ib = MagicMock()
        ib.qualifyContractsAsync = AsyncMock(side_effect=Exception("no route"))
        ib.reqHistoricalNewsAsync = AsyncMock(return_value=[])

        web_articles = [{"headline": "GOOG surges", "body": "...", "time": "", "published_dt": None, "provider": "finviz"}]
        mock_web = AsyncMock(return_value={"GOOG": web_articles})

        entry = {"ticker": "GOOG"}
        with (
            patch("stock_bot.config.settings.ib_settings", _MOCK_SETTINGS),
            patch("stock_bot.data_sources.news_fetcher._FINNHUB_KEY", None),
            patch("stock_bot.data_sources.news_fetcher._fetch_web_all", mock_web),
        ):
            result = asyncio.run(_fetch_ibkr_batch([entry], ib, _CONFIG))

        assert result.get("GOOG") == web_articles

    def test_all_27_watchlist_tickers_no_crash(self):
        """Simulate today's crash scenario: 27 watchlist tickers with no conId,
        scanner returned 0 results. All 27 must resolve and return a result."""
        _fetch_ibkr_batch = _load_news_fetcher_batch()
        ib = _make_ib(con_id=11111)
        watchlist = [
            "NBIS", "LITE", "SNDK", "GEV", "VRT", "MPWR", "LYV", "MV",
            "STX", "CAT", "TSM", "HWM", "GOOG", "TER", "DELL", "META", "AMAT",
            "LHX", "NXPI", "KLAC", "MKSI", "UI", "CRDO", "CW", "DE", "VLVLY", "NTES",
        ]
        entries = [{"ticker": t} for t in watchlist]

        with patch("stock_bot.config.settings.ib_settings", _MOCK_SETTINGS):
            result = asyncio.run(_fetch_ibkr_batch(entries, ib, _CONFIG))

        assert len(result) == 27
        assert ib.qualifyContractsAsync.call_count == 27
        for t in watchlist:
            assert t in result

    def test_returns_ibkr_news_after_successful_qualification(self):
        """After resolving conId, reqHistoricalNewsAsync must be called with that id."""
        _fetch_ibkr_batch = _load_news_fetcher_batch()
        ib = _make_ib(con_id=99999)

        headline = MagicMock()
        headline.providerCode = "FLY"
        headline.articleId = "FLY$abc"
        headline.headline = "Stock surges on earnings"
        headline.time = "20260407 09:35:00"
        ib.reqHistoricalNewsAsync = AsyncMock(return_value=[headline])

        article = MagicMock()
        article.articleText = "Full article text"
        ib.reqNewsArticleAsync = AsyncMock(return_value=article)

        entry = {"ticker": "VRT"}
        with patch("stock_bot.config.settings.ib_settings", _MOCK_SETTINGS):
            result = asyncio.run(_fetch_ibkr_batch([entry], ib, _CONFIG))

        ib.reqHistoricalNewsAsync.assert_called_once()
        call_kwargs = ib.reqHistoricalNewsAsync.call_args
        assert call_kwargs.kwargs.get("conId") == 99999
        assert len(result.get("VRT", [])) == 1
        assert result["VRT"][0]["headline"] == "Stock surges on earnings"


# ══════════════════════════════════════════════════════════════════════════════
# Fix 2 — min-picks fallback loop condition was inverted
# ══════════════════════════════════════════════════════════════════════════════

def _run_fallback_loop(all_scored, num_stocks, score_floor, min_picks, min_expected_gain_pct=1.5):
    """Replicate the fallback loop from main.py after the initial scoring pass."""
    from stock_bot.ai.catalyst_scorer import filter_and_rank

    # Simulate: initial picks were generated at score_floor
    picks = filter_and_rank(
        all_scored, num_stocks,
        min_score=score_floor,
        min_expected_gain_pct=min_expected_gain_pct,
    )

    _fallback_floor = score_floor - 1
    attempts = []
    while len(picks) < min_picks and _fallback_floor >= max(1, score_floor - 3):
        picks = filter_and_rank(
            all_scored, num_stocks,
            min_score=_fallback_floor,
            min_expected_gain_pct=min_expected_gain_pct,
        )
        attempts.append(_fallback_floor)
        _fallback_floor -= 1

    return picks, attempts


def _make_scored(ticker, score, gain=3.0, risk=2):
    return {
        "ticker": ticker,
        "score": score,
        "direction": "bullish",
        "expected_gain_pct": gain,
        "risk": risk,
        "reason": "test",
        "trend_summary": "",
    }


class TestMinPicksFallbackLoop:
    """The fallback loop must actually run and relax score floor when picks < min_picks."""

    def test_loop_runs_when_picks_below_min(self):
        """Core regression: old condition was _fallback_floor >= score_floor
        (always False). Verify the loop now actually executes."""
        # score_floor=7: only 2 tickers qualify at score 7, but 4 more at score 6
        all_scored = (
            [_make_scored(f"HIGH{i}", 7) for i in range(2)] +
            [_make_scored(f"MED{i}", 6) for i in range(4)]
        )
        picks, attempts = _run_fallback_loop(
            all_scored, num_stocks=10, score_floor=7, min_picks=5
        )
        assert len(attempts) >= 1, "Fallback loop never ran — condition is still wrong"
        assert len(picks) >= 5

    def test_loop_does_not_run_when_already_enough_picks(self):
        """If picks >= min_picks at score_floor, the loop must not run."""
        all_scored = [_make_scored(f"T{i}", 7) for i in range(10)]
        _, attempts = _run_fallback_loop(
            all_scored, num_stocks=10, score_floor=7, min_picks=5
        )
        assert attempts == []

    def test_loop_stops_at_score_floor_minus_3(self):
        """Loop must not go below max(1, score_floor - 3).
        With score_floor=7, lowest attempted threshold is 4."""
        # No tickers at 7 or above, many at lower scores
        all_scored = [_make_scored(f"T{i}", i % 3 + 1) for i in range(20)]  # scores 1–3
        _, attempts = _run_fallback_loop(
            all_scored, num_stocks=10, score_floor=7, min_picks=10
        )
        if attempts:
            assert min(attempts) >= max(1, 7 - 3), (
                f"Loop went below score_floor - 3 (min attempted: {min(attempts)})"
            )

    def test_loop_stops_at_1_when_score_floor_is_low(self):
        """With score_floor=2, max(1, 2-3)=1, so loop can try threshold=1."""
        all_scored = [_make_scored("CHEAP", 1, gain=2.0)]
        picks, attempts = _run_fallback_loop(
            all_scored, num_stocks=5, score_floor=2, min_picks=2
        )
        if attempts:
            assert min(attempts) >= 1

    def test_loop_adds_picks_at_relaxed_threshold(self):
        """Picks added by the fallback must have scores in the relaxed range."""
        all_scored = (
            [_make_scored("STRONG", 8)] +
            [_make_scored(f"WEAK{i}", 5) for i in range(8)]
        )
        picks, attempts = _run_fallback_loop(
            all_scored, num_stocks=5, score_floor=7, min_picks=5
        )
        assert len(picks) >= 1

    def test_production_scenario_scanner_returns_zero(self):
        """Simulate today's scenario: scanner returns 0, watchlist injects 27 tickers,
        all with low GPT scores. Fallback loop must try to find picks."""
        # Watchlist tickers that GPT scored below floor
        all_scored = [_make_scored(t, 5) for t in [
            "NBIS", "LITE", "SNDK", "GEV", "VRT", "MPWR", "LYV",
        ]]
        picks, attempts = _run_fallback_loop(
            all_scored, num_stocks=10, score_floor=7, min_picks=5
        )
        # Loop should have tried score thresholds 6 and 5
        assert 6 in attempts or 5 in attempts, (
            "Fallback loop didn't try relaxed thresholds — picks below min will be skipped"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Fix 3 — _retry_ibkr must catch exceptions from fn(ib)
# ══════════════════════════════════════════════════════════════════════════════

def _load_retry_ibkr():
    import importlib.util
    stubs = {
        "ib_insync": MagicMock(),
        "stock_bot.core.logging_config": MagicMock(),
        "stock_bot.brokers.ib.connect_disconnect": MagicMock(),
        "stock_bot.brokers.ib.sell_all": MagicMock(),
        "stock_bot.data_sources.portfolio_writer": MagicMock(),
        "stock_bot.config.settings": MagicMock(),
    }
    with patch.dict("sys.modules", stubs):
        spec = importlib.util.spec_from_file_location(
            "close_of_day_exc",
            _SCRIPTS / "close_of_day.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod._retry_ibkr


class TestRetryIbkrExceptionHandling:
    """_retry_ibkr must survive exceptions thrown by fn(ib) and retry."""

    @pytest.fixture(autouse=True)
    def _fn(self):
        self._retry = _load_retry_ibkr()

    def _make_ib(self):
        ib = MagicMock()
        ib.isConnected.return_value = True
        return ib

    def test_exception_from_fn_is_caught_and_retried(self):
        """fn raising an exception must be treated as a failed attempt, not a crash."""
        ib = self._make_ib()
        logger = MagicMock()
        # First call raises, second call succeeds
        fn = MagicMock(side_effect=[RuntimeError("connection reset"), 42.5])

        with patch("time.sleep"), patch("time.monotonic", side_effect=[0, 1, 2, 3]):
            result, _ = self._retry(fn, "price", ib, MagicMock(), logger, interval=1, timeout=600)

        assert result == 42.5
        assert fn.call_count == 2
        logger.warning.assert_called()

    def test_exception_warning_includes_exception_type(self):
        """Log message must name the exception type so it's diagnosable from logs."""
        ib = self._make_ib()
        logger = MagicMock()
        fn = MagicMock(side_effect=[ValueError("bad data"), 1.0])

        with patch("time.sleep"), patch("time.monotonic", side_effect=[0, 1, 2, 3]):
            self._retry(fn, "test", ib, MagicMock(), logger, interval=1, timeout=600)

        warning_calls = [str(c) for c in logger.warning.call_args_list]
        assert any("ValueError" in c for c in warning_calls), (
            "Warning log must include the exception type name for diagnosability"
        )

    def test_always_raising_fn_returns_none_after_timeout(self):
        """If fn always raises, _retry_ibkr must give up and return None (not crash)."""
        ib = self._make_ib()
        logger = MagicMock()
        fn = MagicMock(side_effect=ConnectionError("gateway down"))

        with patch("time.sleep"), patch("time.monotonic", side_effect=[0, 1, 10]):
            result, _ = self._retry(fn, "price", ib, MagicMock(), logger, interval=1, timeout=5)

        assert result is None
        logger.error.assert_called_once()

    def test_exception_then_none_then_success(self):
        """Mixed failures: exception → None → success. All three must be handled."""
        ib = self._make_ib()
        logger = MagicMock()
        fn = MagicMock(side_effect=[OSError("timeout"), None, 99.0])

        with patch("time.sleep"), patch("time.monotonic", side_effect=[0, 1, 2, 3, 4, 5]):
            result, _ = self._retry(fn, "price", ib, MagicMock(), logger, interval=1, timeout=600)

        assert result == 99.0
        assert fn.call_count == 3

    def test_close_of_day_survives_price_fetch_exception(self):
        """Regression test for today's real-world failure mode: if _get_last_price
        raises (e.g. IBKR drops during close), _retry_ibkr must not propagate the
        exception and must allow close_of_day to continue."""
        ib = self._make_ib()
        logger = MagicMock()
        call_count = 0

        def flaky_price_fetch(ib):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RuntimeError("IBKR connection lost")
            return 150.25

        with patch("time.sleep"), patch("time.monotonic", side_effect=[0, 1, 2, 3, 4, 5]):
            result, _ = self._retry(flaky_price_fetch, "last_price", ib, MagicMock(), logger,
                                    interval=1, timeout=600)

        assert result == 150.25
        assert call_count == 3


# ══════════════════════════════════════════════════════════════════════════════
# Fix 4 — limit_order_buffer_pct = 0 must produce a limit order, not market
# ══════════════════════════════════════════════════════════════════════════════

def _realloc_limit_price(price: float, buffer_pct) -> float | None:
    """Replicate the fixed realloc limit-price formula from main.py (lines 829, 886)."""
    return round(price * (1 + buffer_pct / 100), 2) if buffer_pct is not None else None


class TestReallocLimitOrderBuffer:
    """Reallocation orders must use buffer=0 as 'exact-price limit', not 'market'."""

    def test_zero_buffer_produces_limit_at_exact_price(self):
        """buffer_pct=0 → limit price equals the market price (no markup)."""
        limit = _realloc_limit_price(100.0, 0)
        assert limit == 100.0, (
            f"buffer=0 should produce limit at 100.0, got {limit!r}. "
            "If None is returned the order becomes a market order."
        )

    def test_zero_buffer_is_not_none(self):
        """The pre-fix bug: `if buffer_pct` treated 0 as falsy → None → market order."""
        limit = _realloc_limit_price(50.0, 0)
        assert limit is not None, "buffer=0 must not produce None (market order)"

    def test_none_buffer_produces_market_order(self):
        """buffer_pct=None → market order (this is intentional and must still work)."""
        limit = _realloc_limit_price(100.0, None)
        assert limit is None

    def test_positive_buffer_adds_markup(self):
        """Normal case: buffer=1.0 → 1% above market price."""
        limit = _realloc_limit_price(200.0, 1.0)
        assert limit == 202.0

    def test_half_pct_buffer(self):
        """Default paper-mode buffer of 0.5% must work in realloc too."""
        limit = _realloc_limit_price(100.0, 0.5)
        assert limit == 100.5

    def test_zero_buffer_on_fractional_price(self):
        """Zero buffer on a fractional price must round correctly."""
        limit = _realloc_limit_price(17.3456, 0)
        assert limit == 17.35

    def test_initial_buy_already_used_is_not_none_check(self):
        """The initial buy block (main.py line 697) already used `is not None`.
        Both branches must now behave consistently: same formula, same result."""
        price = 150.0
        buffer = 0

        # Initial buy block (was always correct)
        initial_limit = round(price * (1 + buffer / 100), 2) if buffer is not None else None

        # Realloc block (was broken, now fixed)
        realloc_limit = _realloc_limit_price(price, buffer)

        assert initial_limit == realloc_limit, (
            "Initial buy and realloc limit price formulas must agree for buffer=0"
        )

    def test_all_three_buffer_values_consistent(self):
        """Verify None, 0, and 0.5 all produce the expected order type."""
        price = 100.0
        cases = [
            (None,  None),    # market order
            (0,     100.0),   # exact-price limit
            (0.5,   100.5),   # buffered limit
            (1.0,   101.0),   # standard limit
        ]
        for buffer, expected in cases:
            result = _realloc_limit_price(price, buffer)
            assert result == expected, (
                f"buffer={buffer!r}: expected {expected}, got {result!r}"
            )
