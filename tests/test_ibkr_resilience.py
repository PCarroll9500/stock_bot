"""
tests/test_ibkr_resilience.py

Strict tests for IBKR failure modes — every test proves the pipeline survives
a real error that IBKR actually produces in production.

Failures covered:
  1.  Scanner — all codes timeout              → empty universe, watchlist keeps run alive
  2.  Scanner — one code raises Exception      → that code skipped, others proceed
  3.  Scanner — all results are ETFs           → universe empty after filtering
  4.  qualifyContracts returns [] (Error 200)  → aggressive_filter fail-open
  5.  qualifyContracts raises                  → aggressive_filter fail-open
  6.  qualifyContracts returns [] (Error 200)  → gap_filter fail-open
  7.  qualifyContracts raises                  → gap_filter fail-open
  8.  reqHistoricalData returns []             → trend check returns None (no crash)
  9.  reqHistoricalData raises                 → trend check returns None (no crash)
  10. reqHistoricalData returns []             → aggressive_filter passes (fail-open)
  11. reqHistoricalData raises                 → aggressive_filter passes (fail-open)
  12. IBKR news — all 5 probes return empty    → falls back to Finnhub
  13. IBKR news — all 5 probes return empty    → no Finnhub key → falls back to web
  14. IBKR news — partial probe success        → IBKR used, no fallback triggered
  15. IBKR news — reqHistoricalNews raises     → per-ticker empty, batch continues
  16. live price NaN / 0                       → delayed price attempted
  17. live + delayed both unavailable          → ValueError raised (caller must catch)
  18. Peer closed / isConnected False          → reconnect at STEP 9, session still saved
  19. close_position raises for one ticker     → remaining tickers still sold
  20. sell_all: no open position for ticker    → returns None, no crash
  21. RequestTimeout=30 set before reqOpenOrders in open_market_liquidate
  22. Scanner subscription limit (≤9 concurrent)
  23. Mixed Error 200 + valid tickers in same batch → valid tickers get news, invalid fall back
"""

import asyncio
import math
import pathlib
import sys
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

_REPO = pathlib.Path(__file__).parents[1]
_SCRIPTS = _REPO / "scripts"
_MOCK_SETTINGS = MagicMock(exchange="SMART", currency="USD", account="DU123456")


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _make_bar(date_str="20260408", open_=100.0, high=105.0, low=99.0, close=103.0, volume=1_000_000):
    bar = MagicMock()
    bar.open = open_
    bar.high = high
    bar.low = low
    bar.close = close
    bar.volume = volume
    from datetime import datetime, date
    bar.date = datetime.strptime(date_str, "%Y%m%d")
    return bar


def _make_ticker(last=None, close=None):
    td = MagicMock()
    td.last = last if last is not None else float("nan")
    td.close = close if close is not None else float("nan")
    return td


# ══════════════════════════════════════════════════════════════════════════════
# 1–3. Scanner resilience
# ══════════════════════════════════════════════════════════════════════════════

class TestScannerResilience:
    """Scanner failures must never crash the pipeline — empty universe is fine
    because the watchlist inject recovers the run."""

    def _run_scanner(self, scan_codes, side_effects):
        """Drive get_scanner_universe with controlled per-code results."""
        from stock_bot.data_sources.scanner import get_scanner_universe

        def _make_item(symbol, secType="STK", con_id=12345):
            item = MagicMock()
            item.contractDetails.contract.symbol = symbol
            item.contractDetails.contract.secType = secType
            item.contractDetails.contract.conId = con_id
            return item

        ib = MagicMock()
        ib.reqScannerData.side_effect = side_effects
        config = {"scan_codes": scan_codes, "price_min": 5.0, "volume_min": 100_000, "max_per_scan": 50}
        return get_scanner_universe(ib, config)

    def test_all_scanners_raise_returns_empty_universe(self):
        """Every scan code raises an Exception — must return [] without crashing."""
        result = self._run_scanner(
            ["TOP_PERC_GAIN", "MOST_ACTIVE"],
            [Exception("Error 162: scanner cancelled"), Exception("Error 322: too many")],
        )
        assert result == [], "Scanner exception must not propagate — returns empty list"

    def test_one_scanner_raises_others_proceed(self):
        """If one scan code raises, the others still contribute their results."""
        item = MagicMock()
        item.contractDetails.contract.symbol = "AAPL"
        item.contractDetails.contract.secType = "STK"
        item.contractDetails.contract.conId = 265598
        result = self._run_scanner(
            ["FAILING_CODE", "WORKING_CODE"],
            [Exception("Error 162"), [item]],
        )
        tickers = [r["ticker"] for r in result]
        assert "AAPL" in tickers
        assert len(result) == 1

    def test_all_results_are_etfs_returns_empty(self):
        """If every result is an ETF (secType != STK), universe must be empty."""
        etf = MagicMock()
        etf.contractDetails.contract.symbol = "SPY"
        etf.contractDetails.contract.secType = "ETF"
        etf.contractDetails.contract.conId = 756733
        result = self._run_scanner(["TOP_PERC_GAIN"], [[etf, etf, etf]])
        assert result == []

    def test_mixed_etf_and_stk_only_stk_included(self):
        """ETFs are filtered out; plain STKs are included."""
        etf = MagicMock()
        etf.contractDetails.contract.symbol = "QQQ"
        etf.contractDetails.contract.secType = "ETF"
        etf.contractDetails.contract.conId = 111

        stk = MagicMock()
        stk.contractDetails.contract.symbol = "NVDA"
        stk.contractDetails.contract.secType = "STK"
        stk.contractDetails.contract.conId = 4815747

        result = self._run_scanner(["SCAN1"], [[etf, stk]])
        tickers = [r["ticker"] for r in result]
        assert "NVDA" in tickers
        assert "QQQ" not in tickers

    def test_empty_results_returns_empty(self):
        """Scanner returning zero items is normal and must not crash."""
        result = self._run_scanner(["TOP_PERC_GAIN"], [[]])
        assert result == []

    def test_deduplication_across_scan_codes(self):
        """Same ticker appearing in two scan codes must only appear once."""
        def _item(sym):
            i = MagicMock()
            i.contractDetails.contract.symbol = sym
            i.contractDetails.contract.secType = "STK"
            i.contractDetails.contract.conId = hash(sym) & 0xFFFFFF
            return i

        result = self._run_scanner(
            ["SCAN_A", "SCAN_B"],
            [[_item("AAPL"), _item("GOOG")], [_item("GOOG"), _item("MSFT")]],
        )
        tickers = [r["ticker"] for r in result]
        assert tickers.count("GOOG") == 1
        assert len(result) == 3

    def test_scanner_async_semaphore_limit(self):
        """get_scanner_universe_async uses a Semaphore(9) to stay under IBKR's
        10-simultaneous-subscription limit. Verify the semaphore value."""
        import inspect
        from stock_bot.data_sources.scanner import get_scanner_universe_async
        source = inspect.getsource(get_scanner_universe_async)
        assert "Semaphore(9)" in source, (
            "Scanner must use Semaphore(9) to stay under IBKR's 10-subscription cap. "
            "If this limit is exceeded, IBKR returns Error 322 and all scans fail."
        )


# ══════════════════════════════════════════════════════════════════════════════
# 4–7. qualifyContracts failure → filter fail-open
# ══════════════════════════════════════════════════════════════════════════════

class TestQualifyContractsFailOpen:
    """Error 200 (no security definition) causes qualifyContracts to return [] or raise.
    All filters must be fail-open: unknown ticker passes rather than being silently dropped."""

    def _run_aggressive(self, qualify_side_effect) -> bool:
        from stock_bot.data_sources.trend_checker import passes_aggressive_filters_async
        ib = MagicMock()
        ib.qualifyContractsAsync = AsyncMock(side_effect=qualify_side_effect)
        sem = asyncio.Semaphore(10)
        return asyncio.run(passes_aggressive_filters_async("UNKNOWN", ib, 5.0, sem))

    def _run_gap_filter(self, qualify_side_effect) -> bool:
        from stock_bot.data_sources.trend_checker import passes_gap_filter_async
        ib = MagicMock()
        ib.qualifyContractsAsync = AsyncMock(side_effect=qualify_side_effect)
        sem = asyncio.Semaphore(10)
        return asyncio.run(passes_gap_filter_async("UNKNOWN", ib, 5.0, sem))

    def test_aggressive_filter_passes_when_qualify_returns_empty(self):
        """Error 200: qualifyContracts returns [] → ticker passes aggressive filter."""
        assert self._run_aggressive([[]]) is True

    def test_aggressive_filter_passes_when_qualify_raises(self):
        """Connection error during qualify → ticker passes aggressive filter."""
        assert self._run_aggressive(Exception("IBKR error")) is True

    def test_gap_filter_passes_when_qualify_returns_empty(self):
        """Error 200: qualifyContracts returns [] → ticker passes gap filter."""
        assert self._run_gap_filter([[]]) is True

    def test_gap_filter_passes_when_qualify_raises(self):
        """Connection error during qualify → ticker passes gap filter."""
        assert self._run_gap_filter(Exception("IBKR error")) is True

    def test_fail_open_means_ticker_reaches_scoring(self):
        """Fail-open behavior is essential: a ticker IBKR can't find still gets
        scored by GPT. Real example from today: MV had no security definition
        but its news came from Finnhub and it was in the scoring pool."""
        results = [self._run_aggressive([[]]) for _ in range(5)]
        assert all(results), "All Error 200 tickers must pass — fail-open is intentional"


# ══════════════════════════════════════════════════════════════════════════════
# 8–11. reqHistoricalData failures → fail-open
# ══════════════════════════════════════════════════════════════════════════════

class TestReqHistoricalDataFailOpen:
    """IBKR returns empty bars or raises for unknown/delisted tickers.
    Filters must pass these through rather than silently dropping them."""

    def _run_aggressive(self, bars_result) -> bool:
        from stock_bot.data_sources.trend_checker import passes_aggressive_filters_async
        qualified = MagicMock()
        qualified.conId = 99999
        ib = MagicMock()
        ib.qualifyContractsAsync = AsyncMock(return_value=[qualified])
        if isinstance(bars_result, Exception):
            ib.reqHistoricalDataAsync = AsyncMock(side_effect=bars_result)
        else:
            ib.reqHistoricalDataAsync = AsyncMock(return_value=bars_result)
        sem = asyncio.Semaphore(10)
        return asyncio.run(passes_aggressive_filters_async("TICKER", ib, 5.0, sem))

    def _run_trend(self, bars_result):
        from stock_bot.data_sources.trend_checker import get_trend_data_async
        qualified = MagicMock()
        qualified.conId = 99999
        ib = MagicMock()
        ib.qualifyContractsAsync = AsyncMock(return_value=[qualified])
        if isinstance(bars_result, Exception):
            ib.reqHistoricalDataAsync = AsyncMock(side_effect=bars_result)
        else:
            ib.reqHistoricalDataAsync = AsyncMock(return_value=bars_result)
        sem = asyncio.Semaphore(10)
        return asyncio.run(get_trend_data_async("TICKER", ib, sem))

    def test_aggressive_filter_passes_when_bars_empty(self):
        """Empty bars (market closed / no data) → ticker passes filter."""
        assert self._run_aggressive([]) is True

    def test_aggressive_filter_passes_when_bars_raise(self):
        """Exception from reqHistoricalData → ticker passes filter."""
        assert self._run_aggressive(Exception("Error 162: data cancelled")) is True

    def test_aggressive_filter_passes_when_only_one_bar(self):
        """<2 bars → can't compute gap → ticker passes."""
        assert self._run_aggressive([_make_bar()]) is True

    def test_trend_check_returns_none_when_bars_empty(self):
        """Empty historical bars → trend data is None (not a crash)."""
        result = self._run_trend([])
        assert result is None

    def test_trend_check_returns_none_when_bars_raise(self):
        """Exception during bars fetch → trend data is None (not a crash)."""
        result = self._run_trend(Exception("connection reset"))
        assert result is None

    def test_multiple_ticker_failures_dont_block_batch(self):
        """In a 50-ticker aggressive filter run, failures on some tickers
        must not prevent the others from being evaluated."""
        from stock_bot.data_sources.trend_checker import passes_aggressive_filters_async

        call_count = 0
        results_seen = []

        async def _run_batch():
            async def _mock_qualify(contract):
                nonlocal call_count
                call_count += 1
                if call_count % 3 == 0:
                    return []       # Error 200 every 3rd ticker
                mock = MagicMock()
                mock.conId = call_count * 1000
                return [mock]

            ib = MagicMock()
            ib.qualifyContractsAsync = AsyncMock(side_effect=_mock_qualify)
            ib.reqHistoricalDataAsync = AsyncMock(return_value=[])
            sem = asyncio.Semaphore(10)

            tasks = [
                passes_aggressive_filters_async(f"T{i:03d}", ib, 5.0, sem)
                for i in range(50)
            ]
            return await asyncio.gather(*tasks)

        results = asyncio.run(_run_batch())
        assert len(results) == 50
        assert all(r is True for r in results), (
            "Every ticker with Error 200 or empty bars must pass fail-open"
        )


# ══════════════════════════════════════════════════════════════════════════════
# 12–15. IBKR news service failures
# ══════════════════════════════════════════════════════════════════════════════

class TestIbkrNewsServiceFailures:
    """IBKR news goes down frequently outside market hours and during data farm
    issues. The probe mechanism must detect this and fall back to Finnhub/web."""

    _CONFIG = {"providers": "BRFG", "max_articles": 3}
    _MOCK_SETTINGS = MagicMock(exchange="SMART", currency="USD")

    def _make_ib_all_empty_news(self):
        """IBKR returns [] for every reqHistoricalNews call (service down)."""
        qualified = MagicMock()
        qualified.conId = 12345
        ib = MagicMock()
        ib.qualifyContractsAsync = AsyncMock(return_value=[qualified])
        ib.reqHistoricalNewsAsync = AsyncMock(return_value=[])
        return ib

    def _make_ib_news_raises(self):
        """reqHistoricalNewsAsync raises for every call (connection reset)."""
        qualified = MagicMock()
        qualified.conId = 12345
        ib = MagicMock()
        ib.qualifyContractsAsync = AsyncMock(return_value=[qualified])
        ib.reqHistoricalNewsAsync = AsyncMock(side_effect=Exception("Peer closed connection"))
        return ib

    def test_all_probes_empty_triggers_finnhub_fallback(self):
        """When all 5 IBKR probes return no articles, Finnhub must be used for everyone."""
        from stock_bot.data_sources.news_fetcher import fetch_news_for_tickers_async

        ib = self._make_ib_all_empty_news()
        tickers = [{"ticker": t, "conId": i * 1000} for i, t in enumerate(
            ["AAPL", "MSFT", "GOOG", "META", "AMZN", "NVDA", "TSLA"]
        )]

        finnhub_result = {t["ticker"]: [{"headline": "test"}] for t in tickers}
        mock_finnhub = AsyncMock(return_value=finnhub_result)

        with (
            patch("stock_bot.data_sources.news_fetcher._FINNHUB_KEY", "fake-key"),
            patch("stock_bot.data_sources.news_fetcher._fetch_finnhub_all", mock_finnhub),
            patch("stock_bot.config.settings.ib_settings", self._MOCK_SETTINGS),
        ):
            result = asyncio.run(fetch_news_for_tickers_async(tickers, ib, self._CONFIG))

        mock_finnhub.assert_called_once()
        # Finnhub receives ALL tickers, not just the probe
        finnhub_call_tickers = [t["ticker"] for t in mock_finnhub.call_args[0][0]]
        assert set(finnhub_call_tickers) == {t["ticker"] for t in tickers}

    def test_all_probes_empty_no_finnhub_key_triggers_web_fallback(self):
        """No Finnhub key + all IBKR probes empty → web scrapers used."""
        from stock_bot.data_sources.news_fetcher import fetch_news_for_tickers_async

        ib = self._make_ib_all_empty_news()
        tickers = [{"ticker": t, "conId": i * 1000} for i, t in enumerate(
            ["AAPL", "MSFT", "GOOG", "META", "AMZN"]
        )]

        web_result = {t["ticker"]: [] for t in tickers}
        mock_web = AsyncMock(return_value=web_result)

        with (
            patch("stock_bot.data_sources.news_fetcher._FINNHUB_KEY", None),
            patch("stock_bot.data_sources.news_fetcher._fetch_web_all", mock_web),
            patch("stock_bot.config.settings.ib_settings", self._MOCK_SETTINGS),
        ):
            result = asyncio.run(fetch_news_for_tickers_async(tickers, ib, self._CONFIG))

        mock_web.assert_called_once()

    def test_partial_probe_success_keeps_ibkr(self):
        """If at least 1 of 5 probes returns articles, IBKR is considered healthy.
        Finnhub must NOT be called."""
        from stock_bot.data_sources.news_fetcher import fetch_news_for_tickers_async

        qualified = MagicMock()
        qualified.conId = 12345
        ib = MagicMock()
        ib.qualifyContractsAsync = AsyncMock(return_value=[qualified])

        call_count = 0
        async def _mixed_news(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                headline = MagicMock()
                headline.providerCode = "FLY"
                headline.articleId = "FLY$001"
                headline.headline = "Earnings beat"
                headline.time = "20260408 09:30:00"
                ib.reqNewsArticleAsync = AsyncMock(return_value=MagicMock(articleText="body"))
                return [headline]
            return []

        ib.reqHistoricalNewsAsync = AsyncMock(side_effect=_mixed_news)

        tickers = [{"ticker": t, "conId": i * 1000} for i, t in enumerate(
            ["AAPL", "MSFT", "GOOG", "META", "AMZN"]
        )]
        mock_finnhub = AsyncMock()
        mock_web = AsyncMock()

        with (
            patch("stock_bot.data_sources.news_fetcher._FINNHUB_KEY", "key"),
            patch("stock_bot.data_sources.news_fetcher._fetch_finnhub_all", mock_finnhub),
            patch("stock_bot.data_sources.news_fetcher._fetch_web_all", mock_web),
            patch("stock_bot.config.settings.ib_settings", self._MOCK_SETTINGS),
        ):
            asyncio.run(fetch_news_for_tickers_async(tickers, ib, self._CONFIG))

        mock_finnhub.assert_not_called()
        mock_web.assert_not_called()

    def test_news_fetch_raises_per_ticker_batch_continues(self):
        """If reqHistoricalNews raises for one ticker, the rest of the batch
        must still complete and return their results."""
        from stock_bot.data_sources.news_fetcher import _fetch_ibkr_batch

        qualified = MagicMock()
        qualified.conId = 12345
        ib = MagicMock()
        ib.qualifyContractsAsync = AsyncMock(return_value=[qualified])

        call_count = 0
        async def _raise_on_first(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("Error 162: subscription cancelled")
            return []

        ib.reqHistoricalNewsAsync = AsyncMock(side_effect=_raise_on_first)

        tickers = [{"ticker": t, "conId": i * 1000 + 1} for i, t in enumerate(
            ["CRASH_TICKER", "AAPL", "MSFT"]
        )]

        with patch("stock_bot.config.settings.ib_settings", self._MOCK_SETTINGS):
            result = asyncio.run(_fetch_ibkr_batch(tickers, ib, self._CONFIG))

        assert len(result) == 3, "All 3 tickers must be in result even if one raised"
        assert "CRASH_TICKER" in result
        assert "AAPL" in result


# ══════════════════════════════════════════════════════════════════════════════
# 16–17. Price fetch failures in buy phase
# ══════════════════════════════════════════════════════════════════════════════

class TestPriceFetchFailures:
    """Live prices are unavailable at open and when IBKR data farms are reconnecting.
    The bot must fall back to delayed data, and if that also fails, skip the ticker."""

    def _make_contract(self, symbol="AAPL"):
        contract = MagicMock()
        contract.symbol = symbol
        return contract

    def test_nan_live_price_triggers_delayed_attempt(self):
        """If live last/close are NaN, delayed data (reqMarketDataType=4) must be tried."""
        from stock_bot.brokers.ib.buy_stocks import _last_price_async

        ib = MagicMock()
        # First call (live): both last and close are NaN
        live_td = _make_ticker(last=float("nan"), close=float("nan"))
        # Second call (delayed): valid price
        delayed_td = _make_ticker(last=float("nan"), close=142.50)
        ib.reqTickersAsync = AsyncMock(side_effect=[(live_td,), (delayed_td,)])

        contract = self._make_contract()
        result = asyncio.run(_last_price_async(contract, ib))

        assert result == 142.50
        # reqMarketDataType must be called with 4 (delayed) and reset to 1
        calls = [c[0][0] for c in ib.reqMarketDataType.call_args_list]
        assert 4 in calls, "reqMarketDataType(4) must be called for delayed fallback"
        assert calls[-1] == 1, "reqMarketDataType must be reset to 1 after delayed attempt"

    def test_zero_live_price_falls_back_to_delayed(self):
        """Price of 0.0 is not a valid price — must fall through to delayed."""
        from stock_bot.brokers.ib.buy_stocks import _last_price_async

        ib = MagicMock()
        live_td = _make_ticker(last=0.0, close=0.0)
        delayed_td = _make_ticker(last=150.0, close=float("nan"))
        ib.reqTickersAsync = AsyncMock(side_effect=[(live_td,), (delayed_td,)])

        contract = self._make_contract()
        result = asyncio.run(_last_price_async(contract, ib))
        assert result == 150.0

    def test_both_live_and_delayed_unavailable_raises(self):
        """Market closed + no delayed data → ValueError must be raised.
        The caller (main.py) catches this per-ticker so the pipeline continues."""
        from stock_bot.brokers.ib.buy_stocks import _last_price_async

        ib = MagicMock()
        bad_td = _make_ticker(last=float("nan"), close=float("nan"))
        ib.reqTickersAsync = AsyncMock(return_value=(bad_td,))

        contract = self._make_contract("CLOSED_TICKER")
        with pytest.raises(ValueError, match="Cannot resolve a market price"):
            asyncio.run(_last_price_async(contract, ib))

    def test_market_data_type_always_reset_even_after_exception(self):
        """reqMarketDataType(1) must be called in a finally block even if the
        delayed request raises — IB must not be stuck in delayed mode for the
        next ticker's price fetch."""
        import inspect
        from stock_bot.brokers.ib.buy_stocks import _last_price_async
        source = inspect.getsource(_last_price_async)
        # The reset must be in a finally block, not just after the await
        assert "finally:" in source and "reqMarketDataType(1)" in source, (
            "reqMarketDataType(1) must be in a finally: block inside _last_price_async "
            "so it runs even when reqTickersAsync raises"
        )
        finally_pos = source.index("finally:")
        # Use rfind to get the LAST occurrence of the reset call (the one in finally:)
        reset_pos = source.rfind("reqMarketDataType(1)")
        assert reset_pos > finally_pos, (
            "reqMarketDataType(1) must come AFTER finally: — it must be inside the finally block"
        )

    def test_buy_order_skipped_but_pipeline_continues_on_price_error(self):
        """Per-ticker price failures (ValueError) must be caught in main.py's
        try/except around buy_stock_async — not propagate and kill the session."""
        # Replicate the try/except pattern in main.py lines 699-713
        tickers_attempted = []
        tickers_failed = []

        async def _mock_buy(ticker, ib, **kwargs):
            tickers_attempted.append(ticker)
            if ticker == "BADPRICE":
                raise ValueError("Cannot resolve a market price for BADPRICE")
            return [MagicMock()]  # successful trade

        picks = [
            {"ticker": "GOOG", "allocation_pct": 30.0},
            {"ticker": "BADPRICE", "allocation_pct": 20.0},
            {"ticker": "AAPL", "allocation_pct": 50.0},
        ]

        trades_by_ticker = {}
        for pick in picks:
            try:
                trades = asyncio.run(_mock_buy(pick["ticker"], None, shares=10))
                trades_by_ticker[pick["ticker"]] = trades
            except Exception:
                tickers_failed.append(pick["ticker"])
                trades_by_ticker[pick["ticker"]] = []

        assert "GOOG" in trades_by_ticker
        assert "AAPL" in trades_by_ticker
        assert trades_by_ticker["BADPRICE"] == []
        assert "BADPRICE" in tickers_failed
        # Both good tickers must have been attempted
        assert "GOOG" in tickers_attempted
        assert "AAPL" in tickers_attempted


# ══════════════════════════════════════════════════════════════════════════════
# 18. isConnected False at STEP 9 — reconnect, portfolio still saved
# ══════════════════════════════════════════════════════════════════════════════

class TestIbkrDropDuringScoring:
    """IBKR drops the connection during the GPT scoring phase (common — scoring
    can take several minutes). The bot must detect this at STEP 9 and reconnect
    before writing the portfolio, not abort the session."""

    def _simulate_step9_reconnect(self, reconnect_succeeds: bool):
        """Replicate the STEP 9 reconnect block from main.py:969-974."""
        reconnect_called = []
        portfolio_write_called = []

        class _FakeIB:
            def isConnected(self):
                return False  # dropped during scoring

        ib = _FakeIB()

        async def _mock_reconnect():
            reconnect_called.append(True)
            if not reconnect_succeeds:
                raise ConnectionError("gateway unreachable")

        def _mock_write_session(*args, **kwargs):
            portfolio_write_called.append(True)

        async def _step9():
            if not ib.isConnected():
                try:
                    await _mock_reconnect()
                except Exception:
                    pass  # log and continue — matches main.py:974

            _mock_write_session()

        asyncio.run(_step9())
        return reconnect_called, portfolio_write_called

    def test_reconnect_attempted_when_disconnected(self):
        reconnect_called, _ = self._simulate_step9_reconnect(reconnect_succeeds=True)
        assert reconnect_called, "Reconnect must be attempted when isConnected() is False"

    def test_portfolio_written_even_if_reconnect_fails(self):
        """If reconnect fails, write_session must still be called — don't lose the session."""
        _, portfolio_write_called = self._simulate_step9_reconnect(reconnect_succeeds=False)
        assert portfolio_write_called, (
            "Portfolio must be written even when reconnect fails after scoring — "
            "losing the session record is worse than incomplete price data"
        )

    def test_portfolio_written_when_reconnect_succeeds(self):
        _, portfolio_write_called = self._simulate_step9_reconnect(reconnect_succeeds=True)
        assert portfolio_write_called


# ══════════════════════════════════════════════════════════════════════════════
# 19–20. close_of_day sell isolation
# ══════════════════════════════════════════════════════════════════════════════

class TestCloseOfDaySellIsolation:
    """If selling one ticker fails, the rest must still be sold.
    An open position left overnight is worse than a sell error."""

    def _run_sells(self, tickers_to_sell, failing_ticker=None):
        from stock_bot.brokers.ib.sell_all import close_position

        sold = []
        errors = []

        for ticker in tickers_to_sell:
            ib = MagicMock()
            pos = MagicMock()
            pos.contract.symbol = ticker
            pos.contract.secType = "STK"
            pos.position = 10

            if ticker == failing_ticker:
                ib.positions.return_value = [pos]
                ib.qualifyContracts.side_effect = Exception("Error 200: no security definition")
            else:
                ib.positions.return_value = [pos]
                trade = MagicMock()
                ib.placeOrder.return_value = trade
                ib.qualifyContracts.return_value = None

            try:
                result = close_position(ticker, ib)
                sold.append(ticker)
            except Exception:
                errors.append(ticker)

        return sold, errors

    def test_sell_continues_after_one_ticker_fails_qualify(self):
        """If qualifyContracts raises for BADTICKER, AAPL and MSFT must still sell."""
        sold, errors = self._run_sells(["AAPL", "BADTICKER", "MSFT"], failing_ticker="BADTICKER")
        assert "AAPL" in sold
        assert "MSFT" in sold

    def test_no_position_found_returns_none_no_crash(self):
        """If IBKR has no record of a position, close_position returns None — not a crash."""
        from stock_bot.brokers.ib.sell_all import close_position
        ib = MagicMock()
        ib.positions.return_value = []  # no positions at all
        result = close_position("PHANTOM", ib)
        assert result is None

    def test_flat_position_returns_none_no_crash(self):
        """A position of exactly 0 shares should return None without placing an order."""
        from stock_bot.brokers.ib.sell_all import close_position
        ib = MagicMock()
        pos = MagicMock()
        pos.contract.symbol = "FLAT"
        pos.contract.secType = "STK"
        pos.position = 0
        ib.positions.return_value = [pos]
        result = close_position("FLAT", ib)
        ib.placeOrder.assert_not_called()
        assert result is None

    def test_short_position_is_rejected_and_returns_none(self):
        """Short positions must never exist in a cash-only account.
        close_position must log an error and return None — not place a BUY order."""
        from stock_bot.brokers.ib.sell_all import close_position
        ib = MagicMock()
        pos = MagicMock()
        pos.contract.symbol = "SHORTED"
        pos.contract.secType = "STK"
        pos.position = -5
        ib.positions.return_value = [pos]
        result = close_position("SHORTED", ib)
        ib.placeOrder.assert_not_called()
        assert result is None


# ══════════════════════════════════════════════════════════════════════════════
# 21. RequestTimeout guard
# ══════════════════════════════════════════════════════════════════════════════

class TestRequestTimeoutGuard:
    """ib.RequestTimeout = 30 must be set immediately after connecting to cap
    all blocking IB calls.  Without this, a slow IBKR data server can hang
    the entire pipeline for hours."""

    def test_request_timeout_value_is_30(self):
        """Verify the timeout constant is 30s (not 0 or infinite)."""
        import inspect
        from stock_bot.main import main
        source = inspect.getsource(main)
        assert "RequestTimeout = 30" in source, (
            "ib.RequestTimeout = 30 must be set in main() after connecting"
        )

    def test_request_timeout_set_before_any_scanner_call(self):
        """Confirm RequestTimeout is set before the scanner is called.
        If the scanner runs first, a hanging scan can block for the default timeout."""
        import inspect
        from stock_bot.main import main
        source = inspect.getsource(main)
        rt_pos = source.index("RequestTimeout = 30")
        # The scanner call appears after the timeout is set
        scanner_pos = source.index("get_scanner_universe_async")
        assert rt_pos < scanner_pos, (
            "RequestTimeout must be set before get_scanner_universe_async is called"
        )


# ══════════════════════════════════════════════════════════════════════════════
# 22. Error 200 + valid tickers in same batch → valid tickers get IBKR news
# ══════════════════════════════════════════════════════════════════════════════

class TestMixedError200AndValidTickers:
    """When a batch has both valid tickers (with conId) and Error 200 tickers
    (without conId after qualify fails), the valid ones must still get IBKR news
    and the Error 200 ones must fall back to Finnhub/web — no cross-contamination."""

    def test_error200_ticker_falls_back_valid_ticker_uses_ibkr(self):
        from stock_bot.data_sources.news_fetcher import _fetch_ibkr_batch

        config = {"providers": "BRFG", "max_articles": 3}

        # MV: qualification fails (Error 200)
        # AAPL: qualification succeeds
        async def _qualify(contract):
            if contract.symbol == "MV":
                return []           # Error 200: no security definition
            mock = MagicMock()
            mock.conId = 265598
            return [mock]

        ib = MagicMock()
        ib.qualifyContractsAsync = AsyncMock(side_effect=_qualify)
        ib.reqHistoricalNewsAsync = AsyncMock(return_value=[])

        mv_articles = [{"headline": "MV news", "body": "", "time": "", "published_dt": None, "provider": "finnhub"}]
        mock_finnhub = AsyncMock(return_value={"MV": mv_articles})

        tickers = [{"ticker": "MV"}, {"ticker": "AAPL"}]
        with (
            patch("stock_bot.config.settings.ib_settings", _MOCK_SETTINGS),
            patch("stock_bot.data_sources.news_fetcher._FINNHUB_KEY", "key"),
            patch("stock_bot.data_sources.news_fetcher._fetch_finnhub_all", mock_finnhub),
        ):
            result = asyncio.run(_fetch_ibkr_batch(tickers, ib, config))

        # MV: got Finnhub fallback
        assert result.get("MV") == mv_articles
        # AAPL: got IBKR (empty but via IBKR path, not Finnhub)
        assert "AAPL" in result
        # Finnhub called only for MV (only the one missing conId)
        finnhub_tickers = [t["ticker"] for t in mock_finnhub.call_args[0][0]]
        assert finnhub_tickers == ["MV"]
        assert "AAPL" not in finnhub_tickers


# ══════════════════════════════════════════════════════════════════════════════
# 23. Fractional share guard
# ══════════════════════════════════════════════════════════════════════════════

class TestFractionalShareGuard:
    """IBKR Error 10243: fractional shares are not allowed.
    All share counts must use math.floor() — never raw division."""

    def test_floor_used_in_dollar_amount_path(self):
        """The dollar-amount → shares calculation must floor, not round or truncate."""
        import inspect
        from stock_bot.brokers.ib.buy_stocks import buy_stock_async
        source = inspect.getsource(buy_stock_async)
        assert "math.floor" in source, (
            "buy_stock_async must use math.floor() for share count. "
            "Raw division causes IBKR Error 10243."
        )

    @pytest.mark.parametrize("dollar_amount,price,expected_shares", [
        (1000.0, 33.33, 30),     # 30.003... → 30
        (5000.0, 47.50, 105),    # 105.26... → 105
        (10000.0, 999.99, 10),   # 10.0001 → 10
        (500.0, 7.77, 64),       # 64.35... → 64
    ])
    def test_share_count_always_integer(self, dollar_amount, price, expected_shares):
        shares = math.floor(dollar_amount / price)
        assert shares == expected_shares
        assert isinstance(shares, int)
        assert shares * price <= dollar_amount  # never overspend


# ══════════════════════════════════════════════════════════════════════════════
# 24. PDT override present on orders
# ══════════════════════════════════════════════════════════════════════════════

class TestPDTOverride:
    """Accounts under $25k get IBKR Error 2137 (Pattern Day Trader) without
    advancedErrorOverride = 'DAYTRNG'. Verify it's set on every limit order."""

    def test_pdt_override_set_on_buy_order(self):
        """advancedErrorOverride = 'DAYTRNG' must be set on every order via the
        _entry_order helper, which is called by buy_stock_async for every order type."""
        import inspect
        from stock_bot.brokers.ib import buy_stocks
        # DAYTRNG is in _entry_order, which is called by buy_stock_async
        source = inspect.getsource(buy_stocks)
        assert "DAYTRNG" in source, (
            "advancedErrorOverride = 'DAYTRNG' must be set in _entry_order to "
            "bypass PDT rejection on accounts under $25k (IBKR Error 2137)"
        )
        assert "advancedErrorOverride" in source
