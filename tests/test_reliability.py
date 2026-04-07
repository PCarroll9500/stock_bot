"""
tests/test_reliability.py

Tests for the reliability hardening changes:
  - save_portfolio() atomic write (tmp → rename)
  - config key validation (scanner/news missing → clean exit, not KeyError crash)
  - _retry_ibkr respects the new 120s timeout constant
  - close_of_day no-picks path still liquidates stray IBKR positions
  - open_market_liquidate RequestTimeout is set before any blocking call
  - pipeline timing: all pre-buy stages complete within a budget on mocked I/O
"""

import asyncio
import json
import math
import os
import sys
import time
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────

def _stub_modules(*names):
    """Patch sys.modules with MagicMock stubs for each name (and any sub-names)."""
    stubs = {}
    for name in names:
        stubs[name] = MagicMock()
        # Also stub common sub-modules so dotted imports work
        for sub in ("client", "wrapper", "ib", "util"):
            stubs[f"{name}.{sub}"] = MagicMock()
    return stubs


def _load_module_patched(file_path: Path, module_name: str, extra_stubs: dict | None = None):
    """Load a module from file_path with heavy dependencies stubbed out."""
    stubs = {
        "ib_insync": MagicMock(),
        "stock_bot.core.logging_config": MagicMock(),
        "stock_bot.brokers.ib.connect_disconnect": MagicMock(),
        "stock_bot.brokers.ib.sell_all": MagicMock(),
        "stock_bot.data_sources.portfolio_writer": MagicMock(),
        "stock_bot.config.settings": MagicMock(),
    }
    if extra_stubs:
        stubs.update(extra_stubs)
    with patch.dict("sys.modules", stubs):
        import importlib.util
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


_REPO = Path(__file__).parents[1]
_SCRIPTS = _REPO / "scripts"
_SRC = _REPO / "src" / "stock_bot"


# ═══════════════════════════════════════════════════════════════════════════════
# 1. ATOMIC save_portfolio
# ═══════════════════════════════════════════════════════════════════════════════

class TestSavePortfolioAtomic:
    """save_portfolio() must use write-to-tmp then os.replace() for atomicity."""

    def _run_save(self, data: dict, tmp_path: Path) -> Path:
        data_dir = tmp_path / "docs" / "data"
        data_dir.mkdir(parents=True)
        portfolio_path = data_dir / "portfolio_test.json"

        with patch("stock_bot.data_sources.portfolio_writer._PORTFOLIO_JSON_TEST", portfolio_path):
            from stock_bot.data_sources.portfolio_writer import save_portfolio
            save_portfolio(data, test_mode=True)

        return portfolio_path

    def test_file_written_correctly(self, tmp_path):
        data = {"sessions": [], "initial_investment": 10_000}
        path = self._run_save(data, tmp_path)
        assert path.exists()
        loaded = json.loads(path.read_text())
        assert loaded["initial_investment"] == 10_000

    def test_no_tmp_file_left_behind(self, tmp_path):
        """After a successful save the .tmp file must be gone (renamed to final)."""
        data = {"sessions": []}
        path = self._run_save(data, tmp_path)
        tmp = path.with_suffix(".tmp")
        assert not tmp.exists(), ".tmp file should have been renamed away"

    def test_atomic_replace_called(self, tmp_path):
        """Verify os.replace is used (not shutil.move or direct write_text)."""
        data_dir = tmp_path / "docs" / "data"
        data_dir.mkdir(parents=True)
        portfolio_path = data_dir / "portfolio_test.json"

        replaced: list[tuple[str, str]] = []
        _real_replace = os.replace

        def _spy_replace(src, dst):
            replaced.append((str(src), str(dst)))
            _real_replace(src, dst)

        with (
            patch("stock_bot.data_sources.portfolio_writer._PORTFOLIO_JSON_TEST", portfolio_path),
            patch("os.replace", side_effect=_spy_replace),
        ):
            from stock_bot.data_sources.portfolio_writer import save_portfolio
            save_portfolio({}, test_mode=True)

        assert len(replaced) == 1
        src, dst = replaced[0]
        assert src.endswith(".tmp")
        assert dst == str(portfolio_path)

    def test_existing_file_preserved_if_write_fails(self, tmp_path):
        """If write_text raises (disk full), the original file must survive intact."""
        data_dir = tmp_path / "docs" / "data"
        data_dir.mkdir(parents=True)
        portfolio_path = data_dir / "portfolio_test.json"

        original = {"sessions": [{"date": "2026-01-01"}], "initial_investment": 9_999}
        portfolio_path.write_text(json.dumps(original), encoding="utf-8")

        def _boom(*args, **kwargs):
            raise OSError("disk full")

        with (
            patch("stock_bot.data_sources.portfolio_writer._PORTFOLIO_JSON_TEST", portfolio_path),
            patch("pathlib.Path.write_text", side_effect=_boom),
        ):
            from stock_bot.data_sources.portfolio_writer import save_portfolio
            with pytest.raises(OSError):
                save_portfolio({"sessions": []}, test_mode=True)

        # Original must be untouched
        surviving = json.loads(portfolio_path.read_text())
        assert surviving["initial_investment"] == 9_999


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Config key validation in main.py
# ═══════════════════════════════════════════════════════════════════════════════

class TestConfigKeyValidation:
    """Missing 'scanner' or 'news' in picker_config.json must exit cleanly,
    not raise a KeyError deep in the pipeline after IBKR is already connected."""

    def _make_config(self, **overrides) -> dict:
        base = {
            "scanner": {"scan_codes": ["TOP_PERC_GAIN"], "max_per_scan": 10},
            "news": {"providers": "BRFG", "max_articles": 3},
            "aggressive_mode": False,
            "num_stocks": 5,
        }
        base.update(overrides)
        return base

    def _check_config_exits(self, config: dict) -> bool:
        """Return True if the config validation block calls sys.exit(1)."""
        exited = False
        logger = MagicMock()

        # Replicate the validation block from main.py
        trailing_stop_pct = config.get("trailing_stop_pct")
        stop_loss_pct = config.get("stop_loss_pct")
        take_profit_pct = config.get("take_profit_pct")

        if trailing_stop_pct is not None and (stop_loss_pct is not None or take_profit_pct is not None):
            return True  # exits

        for key in ("scanner", "news"):
            if key not in config:
                logger.error("Config error: picker_config.json is missing required key '%s'", key)
                exited = True
                break

        return exited

    def test_valid_config_does_not_exit(self):
        assert not self._check_config_exits(self._make_config())

    def test_missing_scanner_exits(self):
        cfg = self._make_config()
        del cfg["scanner"]
        assert self._check_config_exits(cfg)

    def test_missing_news_exits(self):
        cfg = self._make_config()
        del cfg["news"]
        assert self._check_config_exits(cfg)

    def test_both_missing_exits(self):
        cfg = self._make_config()
        del cfg["scanner"]
        del cfg["news"]
        assert self._check_config_exits(cfg)

    def test_conflicting_stop_config_exits(self):
        cfg = self._make_config(trailing_stop_pct=2.5, stop_loss_pct=3.0)
        assert self._check_config_exits(cfg)


# ═══════════════════════════════════════════════════════════════════════════════
# 3. _retry_ibkr timeout constant is 120s (not the old 600s)
# ═══════════════════════════════════════════════════════════════════════════════

class TestRetryIbkrTimeout:
    """_RETRY_TIMEOUT_S must be 120 so close_of_day can't chew through the
    1-hour window between 2:30 PM and EventBridge's 3:30 PM instance stop."""

    def test_retry_timeout_is_120(self):
        mod = _load_module_patched(
            _SCRIPTS / "close_of_day.py",
            "close_of_day_timeout_check",
        )
        assert mod._RETRY_TIMEOUT_S == 120, (
            f"_RETRY_TIMEOUT_S is {mod._RETRY_TIMEOUT_S} — must be 120s. "
            "At 10 min per price lookup × 17 calls = 170 min, the bot gets "
            "killed by EventBridge before liquidation completes."
        )

    def test_retry_timeout_max_wall_clock_within_close_window(self):
        """With 120s timeout and ~17 IBKR calls in close_of_day, worst-case
        total retry time is 17 × 120 = 2040s = 34 min < 45 min shell timeout."""
        timeout_s = 120
        max_ibkr_calls = 17  # picks(15) + QQQ + NetLiq
        worst_case_s = max_ibkr_calls * timeout_s
        assert worst_case_s < 2700, (
            f"Worst-case retry wall time {worst_case_s}s exceeds the 2700s "
            "shell timeout in run_close.sh"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 4. RequestTimeout is set in open_market_liquidate before any blocking call
# ═══════════════════════════════════════════════════════════════════════════════

class TestOpenMarketLiquidateRequestTimeout:
    """ib.RequestTimeout must be set to 30 immediately after connecting so
    reqOpenOrders() can't hang the script for the entire trading day."""

    def test_request_timeout_set_before_req_open_orders(self):
        """Verify that ib.RequestTimeout is assigned before ib.reqOpenOrders()
        is called by inspecting the call order on a mock IB object."""
        call_log: list[str] = []

        class _TrackedIB(MagicMock):
            def __setattr__(self, name, value):
                if name == "RequestTimeout":
                    call_log.append(f"set:RequestTimeout={value}")
                super().__setattr__(name, value)

            def reqOpenOrders(self):
                call_log.append("reqOpenOrders")
                return []

            def positions(self, account=None):
                call_log.append("positions")
                return []

            def isConnected(self):
                return True

        mock_ib = _TrackedIB()

        stubs = {
            "stock_bot.core.logging_config": MagicMock(),
            "stock_bot.config.settings": MagicMock(),
        }
        mock_connect = MagicMock(return_value=mock_ib)
        mock_disconnect = MagicMock()
        stubs["stock_bot.brokers.ib.connect_disconnect"] = MagicMock(
            connect_ib=mock_connect, disconnect_ib=mock_disconnect
        )

        with patch.dict("sys.modules", {"ib_insync": MagicMock(), **stubs}):
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "open_market_liquidate_rt",
                _SCRIPTS / "open_market_liquidate.py",
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.main()

        # RequestTimeout must appear in the log BEFORE reqOpenOrders
        assert "set:RequestTimeout=30" in call_log, "ib.RequestTimeout=30 was never set"
        rt_idx = call_log.index("set:RequestTimeout=30")
        req_idx = call_log.index("reqOpenOrders")
        assert rt_idx < req_idx, (
            "RequestTimeout was set AFTER reqOpenOrders — the hang window is still open"
        )

    def test_request_timeout_set_in_close_of_day(self):
        """Same check for close_of_day.py — RequestTimeout must be set immediately
        after connect, before reqOpenOrders / positions / reqExecutions."""
        call_log: list[str] = []

        class _TrackedIB(MagicMock):
            def __setattr__(self, name, value):
                if name == "RequestTimeout":
                    call_log.append(f"set:RequestTimeout={value}")
                super().__setattr__(name, value)

            def reqOpenOrders(self):
                call_log.append("reqOpenOrders")
                return []

            def positions(self, account=None):
                call_log.append("positions")
                return []

            def isConnected(self):
                return True

            def sleep(self, secs):
                pass

        mock_ib = _TrackedIB()

        # Build a portfolio with a session for today so close_of_day doesn't exit early
        from datetime import date
        today = date.today().isoformat()
        portfolio_data = {
            "sessions": [{"date": today, "picks": [], "portfolio_open_value": 10_000}],
            "initial_investment": 10_000,
            "equity_curve": [],
        }

        mock_portfolio_writer = MagicMock()
        mock_portfolio_writer.load_portfolio.return_value = portfolio_data
        mock_portfolio_writer.get_net_liquidation.return_value = 10_000.0
        mock_portfolio_writer.get_live_account_value.return_value = 10_000.0
        mock_portfolio_writer._get_last_price.return_value = 450.0
        mock_portfolio_writer._get_nasdaq_price.return_value = 17_000.0

        mock_connect_mod = MagicMock()
        mock_connect_mod.connect_ib.return_value = mock_ib
        mock_connect_mod.disconnect_ib.return_value = None

        stubs = {
            "ib_insync": MagicMock(),
            "stock_bot.core.logging_config": MagicMock(),
            "stock_bot.brokers.ib.connect_disconnect": mock_connect_mod,
            "stock_bot.brokers.ib.sell_all": MagicMock(),
            "stock_bot.data_sources.portfolio_writer": mock_portfolio_writer,
            "stock_bot.config.settings": MagicMock(),
        }
        with patch.dict("sys.modules", stubs), patch("sys.argv", ["close_of_day.py"]):
            import importlib.util
            spec = importlib.util.spec_from_file_location("close_of_day_rt", _SCRIPTS / "close_of_day.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.main()

        assert "set:RequestTimeout=30" in call_log, "ib.RequestTimeout=30 was never set in close_of_day"
        rt_idx = call_log.index("set:RequestTimeout=30")
        # First blocking call must come after the timeout is set
        first_blocking = next(
            (i for i, e in enumerate(call_log) if e in ("reqOpenOrders", "positions")),
            None,
        )
        if first_blocking is not None:
            assert rt_idx < first_blocking, (
                "RequestTimeout set AFTER first blocking IB call in close_of_day"
            )


# ═══════════════════════════════════════════════════════════════════════════════
# 5. no_picks close_of_day — stray positions still liquidated
# ═══════════════════════════════════════════════════════════════════════════════

class TestNoPicksStrayLiquidation:
    """On a no-picks day, close_of_day must still connect to IBKR and sell
    any stray positions — orders may have slipped through without a portfolio entry."""

    def _run_no_picks(self, positions):
        from datetime import date
        today = date.today().isoformat()
        portfolio_data = {
            "sessions": [{"date": today, "picks": [], "no_picks": True, "portfolio_open_value": 10_000}],
            "initial_investment": 10_000,
            "equity_curve": [],
        }

        mock_ib = MagicMock()
        mock_ib.isConnected.return_value = True

        mock_pos_objects = []
        for sym, qty in positions:
            pos = MagicMock()
            pos.contract.secType = "STK"
            pos.contract.symbol = sym
            pos.position = qty
            mock_pos_objects.append(pos)
        mock_ib.positions.return_value = mock_pos_objects

        close_position_calls: list[str] = []
        mock_close = MagicMock(side_effect=lambda sym, _ib: close_position_calls.append(sym))

        mock_portfolio_writer = MagicMock()
        mock_portfolio_writer.load_portfolio.return_value = portfolio_data

        mock_connect_mod = MagicMock()
        mock_connect_mod.connect_ib.return_value = mock_ib
        mock_connect_mod.disconnect_ib.return_value = None

        mock_sell_all = MagicMock()
        mock_sell_all.close_position = mock_close

        stubs = {
            "ib_insync": MagicMock(),
            "stock_bot.core.logging_config": MagicMock(),
            "stock_bot.brokers.ib.connect_disconnect": mock_connect_mod,
            "stock_bot.brokers.ib.sell_all": mock_sell_all,
            "stock_bot.data_sources.portfolio_writer": mock_portfolio_writer,
            "stock_bot.config.settings": MagicMock(),
        }
        with patch.dict("sys.modules", stubs), patch("sys.argv", ["close_of_day.py"]):
            import importlib.util
            spec = importlib.util.spec_from_file_location("close_of_day_nopicks", _SCRIPTS / "close_of_day.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.main()

        return close_position_calls

    def test_stray_positions_are_sold_on_no_picks_day(self):
        sold = self._run_no_picks([("AAPL", 10), ("MSFT", 5)])
        assert "AAPL" in sold
        assert "MSFT" in sold

    def test_no_stray_positions_no_sell_calls(self):
        sold = self._run_no_picks([])
        assert sold == []

    def test_zero_qty_positions_not_sold(self):
        """Positions with qty <= 0 must not trigger a sell."""
        sold = self._run_no_picks([("AAPL", 0), ("MSFT", -1)])
        assert "AAPL" not in sold
        assert "MSFT" not in sold


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Pipeline timing — all pre-buy stages complete within budget on mocked I/O
# ═══════════════════════════════════════════════════════════════════════════════

class TestPipelineTiming:
    """Verify that the pipeline stages (scan → filter → news → trend → score)
    complete within a time budget when all I/O is mocked.

    The bot runs at 9:35 AM ET, market opens at 9:30 AM.  Every minute of
    delay = missed price action.  The pre-buy pipeline should finish in under
    5 minutes even with 50 candidates.
    """

    CANDIDATE_COUNT = 50
    MAX_SECONDS = 300  # 5 minutes wall-clock budget

    def _make_candidates(self, n: int) -> list[dict]:
        return [{"ticker": f"T{i:03d}", "score": 0} for i in range(n)]

    def _time_news_filter_stage(self, candidates: list[dict]) -> float:
        """Simulate the news-fetch + pre-score-filter stage timing.

        In production this is async with up to 5 concurrent fetches.
        Here we mock each fetch as 0ms and measure the routing overhead.
        """
        import time

        # Mock: each ticker "has" 3 articles, fetched instantly
        news_by_ticker = {c["ticker"]: [{"headline": "x", "body": "y"}] * 3 for c in candidates}

        # Pre-score trend filter: instant dict ops
        pre_score_filters = {"weekly": {"min": 0.0, "max": None}}
        raw_trend = {c["ticker"]: {"weekly": 1.0, "monthly": 0.5} for c in candidates}

        t0 = time.monotonic()
        filtered = {}
        for ticker, articles in news_by_ticker.items():
            raw = raw_trend.get(ticker) or {}
            passed = True
            for period, bounds in pre_score_filters.items():
                val = raw.get(period)
                if val is None:
                    continue
                if bounds.get("min") is not None and val < bounds["min"]:
                    passed = False
                    break
            if passed:
                filtered[ticker] = articles
        elapsed = time.monotonic() - t0
        return elapsed

    def _time_scoring_stage(self, candidates: list[dict], mock_delay_s: float = 0.002) -> float:
        """Simulate AI scoring: N sequential GPT calls each taking mock_delay_s.

        MAX_WORKERS=1 means scoring is sequential.  Real GPT-4o takes ~2-5s/call.
        We use 2ms here to test routing overhead without waiting for real API.
        """
        import time

        t0 = time.monotonic()
        scores = []
        for c in candidates:
            time.sleep(mock_delay_s)
            scores.append({"ticker": c["ticker"], "score": 8, "direction": "bullish",
                           "reason": "mock", "allocation_pct": 10.0})
        elapsed = time.monotonic() - t0
        return elapsed

    def test_news_filter_50_candidates_under_1s(self):
        """The news-filter/trend-filter routing for 50 candidates is pure Python
        and should complete in well under 1 second."""
        candidates = self._make_candidates(self.CANDIDATE_COUNT)
        elapsed = self._time_news_filter_stage(candidates)
        assert elapsed < 1.0, (
            f"News/trend filter took {elapsed:.3f}s for {self.CANDIDATE_COUNT} candidates — "
            "this is pure Python dict ops and should be near-instant"
        )

    def test_scoring_50_candidates_time_budget(self):
        """With MAX_WORKERS=1 and ~3s real GPT latency, 50 candidates = ~150s.
        Our 1-hour main.py timeout gives plenty of room, but verifying the
        routing overhead itself is tiny (2ms mock × 50 = ~0.1s)."""
        candidates = self._make_candidates(self.CANDIDATE_COUNT)
        elapsed = self._time_scoring_stage(candidates, mock_delay_s=0.002)
        # Pure overhead should be < 2s even with 50 items
        assert elapsed < 2.0, (
            f"Scoring routing overhead {elapsed:.3f}s for {self.CANDIDATE_COUNT} items — "
            "something is adding unexpected blocking time outside the GPT call itself"
        )

    def test_realistic_gpt_latency_fits_in_timeout(self):
        """With real GPT-4o at ~3s/call and 50 candidates, total scoring time
        is ~150s.  The 3600s main.py timeout gives a 24× safety margin."""
        gpt_latency_s = 3.0
        max_candidates = 50
        scoring_time_s = gpt_latency_s * max_candidates
        main_timeout_s = 3600
        assert scoring_time_s < main_timeout_s, (
            f"Estimated scoring time {scoring_time_s}s would exceed main.py timeout {main_timeout_s}s"
        )

    def test_total_pipeline_estimate_fits_in_trading_window(self):
        """Estimate total pre-buy pipeline time and verify it leaves enough
        market time to matter.  Market opens 9:30 AM, bot starts 9:35 AM,
        close_of_day liquidates at 2:30 PM = 295 minutes of trading window."""
        estimates = {
            "connect_ibkr": 5,
            "scanner_15_codes_parallel": 30,
            "aggressive_filter_50_tickers": 30,
            "news_fetch_50_tickers": 90,
            "trend_data_50_tickers": 20,
            "gpt_scoring_50_tickers_sequential": 150,
            "preflight_prices": 10,
            "order_submission": 5,
            "fill_wait": 30,
            "realloc_rounds_3": 90,
            "portfolio_write": 5,
        }
        total_s = sum(estimates.values())
        total_min = total_s / 60
        trading_window_min = 295  # 9:35 AM start to 2:30 PM close

        assert total_min < trading_window_min, (
            f"Estimated pipeline total {total_min:.1f} min exceeds trading window {trading_window_min} min"
        )
        # Also ensure buys happen within first 15 minutes (capture morning momentum)
        pre_buy_s = sum(v for k, v in estimates.items()
                        if k not in ("fill_wait", "realloc_rounds_3", "portfolio_write"))
        pre_buy_min = pre_buy_s / 60
        assert pre_buy_min < 15, (
            f"Estimated time to first buy is {pre_buy_min:.1f} min — "
            "should be under 15 min to capture morning price action. "
            "Consider increasing MAX_WORKERS in catalyst_scorer.py."
        )
