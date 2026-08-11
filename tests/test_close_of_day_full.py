"""
tests/test_close_of_day_full.py

Comprehensive tests for close_of_day.py and portfolio_writer.py covering
the full close-of-day pipeline: cancel orders → sell → P&L → NetLiquidation
→ equity curve → save portfolio.

These tests verify that the bot will reliably close positions and record
accurate P&L regardless of IBKR availability edge cases.
"""
import json
import os
import sys
import time
import math
from datetime import date as date_type
from pathlib import Path
from unittest.mock import MagicMock, patch, call, PropertyMock

import pytest

# ── Helpers to keep tests DRY ─────────────────────────────────────────────────

def _make_ib(connected=True):
    ib = MagicMock()
    ib.isConnected.return_value = connected
    ib.RequestTimeout = 30
    return ib


def _session(today: str, close_value=None, picks=None, no_picks=False):
    return {
        "date": today,
        "mode": "aggressive",
        "no_picks": no_picks,
        "picks": picks or [],
        "portfolio_open_value": 10_000.0,
        "portfolio_close_value": close_value,
        "session_return_usd": None,
        "session_return_pct": None,
        "qqq_buy_price": 400.0,
        "qqq_close_price": None,
        "qqq_day_return_pct": None,
    }


def _pick(ticker: str, buy_price: float = 100.0, shares: int = 10, hold_until=None):
    p = {
        "ticker": ticker,
        "score": 8,
        "direction": "long",
        "risk": "medium",
        "expected_gain_pct": 5.0,
        "reason": "test",
        "trend_summary": "",
        "allocation_pct": 10.0,
        "shares": shares,
        "buy_price": buy_price,
        "buy_value": round(buy_price * shares, 2),
        "close_price": None,
        "day_return_pct": None,
        "day_return_usd": None,
    }
    if hold_until:
        p["hold_until"] = hold_until
    return p


# ═══════════════════════════════════════════════════════════════════════════════
# _retry_ibkr
# ═══════════════════════════════════════════════════════════════════════════════

class TestRetryIbkr:
    """Unit tests for the _retry_ibkr helper in close_of_day.py."""

    def setup_method(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import importlib
        import close_of_day
        importlib.reload(close_of_day)
        self.mod = close_of_day

    def test_returns_immediately_on_first_success(self):
        ib = _make_ib()
        fn = MagicMock(return_value=42.0)
        result, ret_ib = self.mod._retry_ibkr(fn, "label", ib, MagicMock(), MagicMock(), timeout=5)
        assert result == 42.0
        fn.assert_called_once_with(ib)

    def test_retries_on_none_return(self):
        ib = _make_ib()
        fn = MagicMock(side_effect=[None, None, 99.0])
        logger = MagicMock()
        connect_fn = MagicMock(return_value=ib)
        with patch("time.sleep"):
            result, _ = self.mod._retry_ibkr(fn, "netliq", ib, connect_fn, logger, interval=1, timeout=60)
        assert result == 99.0
        assert fn.call_count == 3

    def test_exception_in_fn_is_caught_and_retried(self):
        """fn() raising an exception must be treated as None (not crash)."""
        ib = _make_ib()
        fn = MagicMock(side_effect=[RuntimeError("boom"), 55.0])
        logger = MagicMock()
        with patch("time.sleep"):
            result, _ = self.mod._retry_ibkr(fn, "netliq", ib, MagicMock(), logger, interval=1, timeout=60)
        assert result == 55.0
        # Warning must mention the exception class name
        warning_args = logger.warning.call_args_list[0][0]
        assert "RuntimeError" in warning_args[2]

    def test_returns_none_after_timeout(self):
        ib = _make_ib()
        fn = MagicMock(return_value=None)
        logger = MagicMock()
        with patch("time.sleep"), patch("time.monotonic", side_effect=[0, 0, 1, 0, 200]):
            result, _ = self.mod._retry_ibkr(fn, "netliq", ib, MagicMock(), logger, interval=1, timeout=10)
        assert result is None

    def test_reconnect_attempted_when_disconnected(self):
        ib = _make_ib(connected=False)
        ib2 = _make_ib(connected=True)
        fn = MagicMock(side_effect=[None, 77.0])
        connect_fn = MagicMock(return_value=ib2)
        logger = MagicMock()
        with patch("time.sleep"):
            result, _ = self.mod._retry_ibkr(fn, "netliq", ib, connect_fn, logger, interval=1, timeout=60)
        connect_fn.assert_called_once()
        assert result == 77.0

    def test_always_raising_fn_returns_none_after_timeout(self):
        """If fn always raises, _retry_ibkr must still return None eventually."""
        ib = _make_ib()
        fn = MagicMock(side_effect=ConnectionError("gone"))
        logger = MagicMock()
        call_count = 0
        original_mono = time.monotonic

        def fake_mono():
            nonlocal call_count
            call_count += 1
            # First call sets deadline, subsequent calls advance time past deadline
            return 0 if call_count <= 2 else 200

        with patch("time.sleep"), patch("time.monotonic", side_effect=fake_mono):
            result, _ = self.mod._retry_ibkr(fn, "netliq", ib, MagicMock(), logger, interval=1, timeout=10)
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# close_of_day early-exit paths
# ═══════════════════════════════════════════════════════════════════════════════

class TestCloseOfDayEarlyExit:

    def setup_method(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import importlib
        import close_of_day
        importlib.reload(close_of_day)
        self.mod = close_of_day

    def test_no_session_for_today_returns_without_connecting(self):
        """If no session for today exists, main() must return immediately — no IBKR connection."""
        today = date_type.today().isoformat()
        yesterday = "2020-01-01"
        portfolio = {
            "sessions": [_session(yesterday, close_value=9900.0)],
            "equity_curve": [],
            "initial_investment": 10_000.0,
        }
        with patch.object(self.mod, "load_portfolio", return_value=portfolio), \
             patch.object(self.mod, "connect_ib") as mock_connect, \
             patch("sys.argv", ["close_of_day.py"]):
            self.mod.main()
        mock_connect.assert_not_called()

    def test_already_closed_session_skips_without_selling(self):
        """If session already has portfolio_close_value, main() must skip selling."""
        today = date_type.today().isoformat()
        portfolio = {
            "sessions": [_session(today, close_value=10_500.0)],
            "equity_curve": [],
            "initial_investment": 10_000.0,
        }
        with patch.object(self.mod, "load_portfolio", return_value=portfolio), \
             patch.object(self.mod, "connect_ib") as mock_connect, \
             patch("sys.argv", ["close_of_day.py"]):
            self.mod.main()
        mock_connect.assert_not_called()

    def test_no_picks_session_still_checks_ibkr_positions(self):
        """A no-picks session must connect and check for stray positions."""
        today = date_type.today().isoformat()
        portfolio = {
            "sessions": [_session(today, no_picks=True)],
            "equity_curve": [],
            "initial_investment": 10_000.0,
        }
        ib = _make_ib()
        ib.positions.return_value = []  # no stray positions

        with patch.object(self.mod, "load_portfolio", return_value=portfolio), \
             patch.object(self.mod, "connect_ib", return_value=ib), \
             patch.object(self.mod, "disconnect_ib"), \
             patch("stock_bot.config.settings.ib_settings") as mock_settings, \
             patch("sys.argv", ["close_of_day.py"]):
            mock_settings.account = "DU123"
            self.mod.main()

        ib.positions.assert_called_once()

    def test_no_picks_session_liquidates_stray_positions(self):
        """A no-picks session with stray positions must liquidate them."""
        today = date_type.today().isoformat()
        portfolio = {
            "sessions": [_session(today, no_picks=True)],
            "equity_curve": [],
            "initial_investment": 10_000.0,
        }
        ib = _make_ib()
        stray = MagicMock()
        stray.contract.secType = "STK"
        stray.contract.symbol = "AAPL"
        stray.position = 10.0
        ib.positions.return_value = [stray]

        with patch.object(self.mod, "load_portfolio", return_value=portfolio), \
             patch.object(self.mod, "connect_ib", return_value=ib), \
             patch.object(self.mod, "disconnect_ib"), \
             patch.object(self.mod, "close_position") as mock_close, \
             patch("stock_bot.config.settings.ib_settings") as mock_settings, \
             patch("sys.argv", ["close_of_day.py"]):
            mock_settings.account = "DU123"
            self.mod.main()

        mock_close.assert_called_once_with("AAPL", ib)


# ═══════════════════════════════════════════════════════════════════════════════
# Multi-day hold logic
# ═══════════════════════════════════════════════════════════════════════════════

class TestMultiDayHolds:

    def setup_method(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import importlib
        import close_of_day
        importlib.reload(close_of_day)
        self.mod = close_of_day

    def _make_portfolio(self, today: str, hold_ticker: str, regular_ticker: str):
        """Portfolio with one held ticker and one regular ticker today."""
        future = "2099-12-31"
        return {
            "sessions": [
                {
                    "date": today,
                    "mode": "aggressive",
                    "no_picks": False,
                    "picks": [
                        _pick(hold_ticker, hold_until=future),
                        _pick(regular_ticker),
                    ],
                    "portfolio_open_value": 20_000.0,
                    "portfolio_close_value": None,
                    "session_return_usd": None,
                    "session_return_pct": None,
                    "qqq_buy_price": 400.0,
                }
            ],
            "equity_curve": [],
            "initial_investment": 20_000.0,
        }

    def test_held_ticker_not_sold(self):
        """Tickers with hold_until > today must not have close_position called."""
        today = date_type.today().isoformat()
        portfolio = self._make_portfolio(today, hold_ticker="HELD", regular_ticker="SELL")

        ib = _make_ib()
        pos_held = MagicMock()
        pos_held.contract.secType = "STK"
        pos_held.contract.symbol = "HELD"
        pos_held.position = 10.0

        pos_sell = MagicMock()
        pos_sell.contract.secType = "STK"
        pos_sell.contract.symbol = "SELL"
        pos_sell.position = 10.0

        ib.positions.return_value = [pos_held, pos_sell]
        ib.reqOpenOrders.return_value = []
        ib.reqExecutions.return_value = []

        trade = MagicMock()
        trade.orderStatus.filled = 10
        trade.orderStatus.avgFillPrice = 105.0

        sell_calls = []

        def _close(ticker, _ib):
            sell_calls.append(ticker)
            if ticker == "SELL":
                return trade
            return None

        with patch.object(self.mod, "load_portfolio", return_value=portfolio), \
             patch.object(self.mod, "save_portfolio") as mock_save, \
             patch.object(self.mod, "connect_ib", return_value=ib), \
             patch.object(self.mod, "disconnect_ib"), \
             patch.object(self.mod, "close_position", side_effect=_close), \
             patch.object(self.mod, "get_net_liquidation", return_value=20_500.0), \
             patch.object(self.mod, "get_live_account_value", return_value=5_000.0), \
             patch.object(self.mod, "_get_last_price", return_value=105.0), \
             patch.object(self.mod, "_get_nasdaq_price", return_value=None), \
             patch("stock_bot.config.settings.ib_settings") as mock_settings, \
             patch("sys.argv", ["close_of_day.py"]):
            mock_settings.account = "DU123"
            self.mod.main()

        assert "HELD" not in sell_calls
        assert "SELL" in sell_calls

    def test_held_ticker_pnl_not_recorded(self):
        """close_price must remain None for multi-day held tickers."""
        today = date_type.today().isoformat()
        portfolio = self._make_portfolio(today, hold_ticker="HOLD", regular_ticker="GO")

        ib = _make_ib()
        pos = MagicMock()
        pos.contract.secType = "STK"
        pos.contract.symbol = "GO"
        pos.position = 10.0
        ib.positions.return_value = [pos]
        ib.reqOpenOrders.return_value = []
        ib.reqExecutions.return_value = []

        trade = MagicMock()
        trade.orderStatus.filled = 10
        trade.orderStatus.avgFillPrice = 110.0

        saved_portfolio = {}

        def _save(p, **kwargs):
            saved_portfolio.update(p)

        with patch.object(self.mod, "load_portfolio", return_value=portfolio), \
             patch.object(self.mod, "save_portfolio", side_effect=_save), \
             patch.object(self.mod, "connect_ib", return_value=ib), \
             patch.object(self.mod, "disconnect_ib"), \
             patch.object(self.mod, "close_position", return_value=trade), \
             patch.object(self.mod, "get_net_liquidation", return_value=20_000.0), \
             patch.object(self.mod, "get_live_account_value", return_value=5_000.0), \
             patch.object(self.mod, "_get_last_price", return_value=110.0), \
             patch.object(self.mod, "_get_nasdaq_price", return_value=None), \
             patch("stock_bot.config.settings.ib_settings") as mock_settings, \
             patch("sys.argv", ["close_of_day.py"]):
            mock_settings.account = "DU123"
            self.mod.main()

        session = saved_portfolio["sessions"][0]
        held_pick = next(p for p in session["picks"] if p["ticker"] == "HOLD")
        assert held_pick["close_price"] is None
        assert held_pick["day_return_pct"] is None


# ═══════════════════════════════════════════════════════════════════════════════
# NetLiquidation unavailable → ERROR
# ═══════════════════════════════════════════════════════════════════════════════

class TestNetLiquidationUnavailable:

    def setup_method(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import importlib
        import close_of_day
        importlib.reload(close_of_day)
        self.mod = close_of_day

    def test_netliq_none_sets_error_string(self):
        """If NetLiquidation returns None after retries, session fields must be 'ERROR'."""
        today = date_type.today().isoformat()
        portfolio = {
            "sessions": [_session(today, picks=[_pick("AAPL")])],
            "equity_curve": [],
            "initial_investment": 10_000.0,
        }

        ib = _make_ib()
        ib.positions.return_value = []
        ib.reqOpenOrders.return_value = []
        ib.reqExecutions.return_value = []

        saved = {}

        def _retry_ibkr_patch(fn, label, ib_arg, connect_fn, logger_arg, **kwargs):
            if "NetLiquidation" in label:
                return None, ib_arg
            return 400.0, ib_arg  # QQQ price fallback

        with patch.object(self.mod, "load_portfolio", return_value=portfolio), \
             patch.object(self.mod, "save_portfolio", side_effect=lambda p, **kw: saved.update(p)), \
             patch.object(self.mod, "connect_ib", return_value=ib), \
             patch.object(self.mod, "disconnect_ib"), \
             patch.object(self.mod, "_retry_ibkr", side_effect=_retry_ibkr_patch), \
             patch.object(self.mod, "get_live_account_value", return_value=None), \
             patch.object(self.mod, "_get_last_price", return_value=None), \
             patch.object(self.mod, "_get_nasdaq_price", return_value=None), \
             patch("stock_bot.config.settings.ib_settings") as mock_settings, \
             patch("sys.argv", ["close_of_day.py"]):
            mock_settings.account = "DU123"
            self.mod.main()

        session = saved["sessions"][0]
        assert session["portfolio_close_value"] == "ERROR"
        assert session["session_return_usd"] == "ERROR"
        assert session["session_return_pct"] == "ERROR"

    def test_netliq_none_does_not_add_equity_curve_point(self):
        """When NetLiquidation fails, no equity curve point should be added."""
        today = date_type.today().isoformat()
        portfolio = {
            "sessions": [_session(today, picks=[_pick("AAPL")])],
            "equity_curve": [],
            "initial_investment": 10_000.0,
        }

        ib = _make_ib()
        ib.positions.return_value = []
        ib.reqOpenOrders.return_value = []
        ib.reqExecutions.return_value = []

        saved = {}

        def _retry_ibkr_patch(fn, label, ib_arg, connect_fn, logger_arg, **kwargs):
            return None, ib_arg  # all retries fail

        with patch.object(self.mod, "load_portfolio", return_value=portfolio), \
             patch.object(self.mod, "save_portfolio", side_effect=lambda p, **kw: saved.update(p)), \
             patch.object(self.mod, "connect_ib", return_value=ib), \
             patch.object(self.mod, "disconnect_ib"), \
             patch.object(self.mod, "_retry_ibkr", side_effect=_retry_ibkr_patch), \
             patch.object(self.mod, "get_live_account_value", return_value=None), \
             patch.object(self.mod, "_get_last_price", return_value=None), \
             patch.object(self.mod, "_get_nasdaq_price", return_value=None), \
             patch("stock_bot.config.settings.ib_settings") as mock_settings, \
             patch("sys.argv", ["close_of_day.py"]):
            mock_settings.account = "DU123"
            self.mod.main()

        assert saved.get("equity_curve", []) == []


# ═══════════════════════════════════════════════════════════════════════════════
# P&L recording — fill price vs stop-loss execution price vs fallback
# ═══════════════════════════════════════════════════════════════════════════════

class TestPnLRecording:

    def setup_method(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import importlib
        import close_of_day
        importlib.reload(close_of_day)
        self.mod = close_of_day

    def _run_with_sell(self, today, portfolio, sell_fill_price, stop_loss_ticker=None):
        """Run close_of_day with a simple sell position and a given fill price."""
        ib = _make_ib()
        pos = MagicMock()
        pos.contract.secType = "STK"
        pos.contract.symbol = "AAPL"
        pos.position = 10.0
        ib.positions.return_value = [pos]
        ib.reqOpenOrders.return_value = []

        if stop_loss_ticker:
            fill = MagicMock()
            fill.execution.side = "SLD"
            fill.execution.shares = 10.0
            fill.execution.price = sell_fill_price
            fill.contract.symbol = stop_loss_ticker
            ib.reqExecutions.return_value = [fill]
        else:
            ib.reqExecutions.return_value = []

        trade = MagicMock()
        trade.orderStatus.filled = 10
        trade.orderStatus.avgFillPrice = sell_fill_price

        saved = {}

        def _retry_ibkr_patch(fn, label, ib_arg, connect_fn, logger_arg, **kwargs):
            if "NetLiquidation" in label:
                return 10_500.0, ib_arg
            return sell_fill_price, ib_arg

        with patch.object(self.mod, "load_portfolio", return_value=portfolio), \
             patch.object(self.mod, "save_portfolio", side_effect=lambda p, **kw: saved.update(p)), \
             patch.object(self.mod, "connect_ib", return_value=ib), \
             patch.object(self.mod, "disconnect_ib"), \
             patch.object(self.mod, "close_position", return_value=trade), \
             patch.object(self.mod, "_retry_ibkr", side_effect=_retry_ibkr_patch), \
             patch.object(self.mod, "get_live_account_value", return_value=5_000.0), \
             patch.object(self.mod, "_get_last_price", return_value=sell_fill_price), \
             patch.object(self.mod, "_get_nasdaq_price", return_value=None), \
             patch("stock_bot.config.settings.ib_settings") as mock_settings, \
             patch("sys.argv", ["close_of_day.py"]):
            mock_settings.account = "DU123"
            self.mod.main()

        return saved

    def test_fill_price_used_for_pnl(self):
        """close_price must equal the actual fill price, not the buy price."""
        today = date_type.today().isoformat()
        portfolio = {
            "sessions": [_session(today, picks=[_pick("AAPL", buy_price=100.0, shares=10)])],
            "equity_curve": [],
            "initial_investment": 10_000.0,
        }
        saved = self._run_with_sell(today, portfolio, sell_fill_price=105.0)
        pick = saved["sessions"][0]["picks"][0]
        assert pick["close_price"] == 105.0
        assert abs(pick["day_return_pct"] - 5.0) < 0.01
        assert abs(pick["day_return_usd"] - 50.0) < 0.01

    def test_stop_loss_intraday_sets_flag(self):
        """If stop-loss fired intraday (no position at close), use exec_map price."""
        today = date_type.today().isoformat()
        # MSFT has shares in picks but no position at close (stop-loss fired)
        portfolio = {
            "sessions": [_session(today, picks=[_pick("MSFT", buy_price=200.0, shares=5)])],
            "equity_curve": [],
            "initial_investment": 10_000.0,
        }

        ib = _make_ib()
        ib.positions.return_value = []  # No position at close
        ib.reqOpenOrders.return_value = []

        fill = MagicMock()
        fill.execution.side = "SLD"
        fill.execution.shares = 5.0
        fill.execution.price = 190.0
        fill.contract.symbol = "MSFT"
        ib.reqExecutions.return_value = [fill]

        saved = {}

        def _retry_ibkr_patch(fn, label, ib_arg, connect_fn, logger_arg, **kwargs):
            if "NetLiquidation" in label:
                return 9_950.0, ib_arg
            return 190.0, ib_arg

        with patch.object(self.mod, "load_portfolio", return_value=portfolio), \
             patch.object(self.mod, "save_portfolio", side_effect=lambda p, **kw: saved.update(p)), \
             patch.object(self.mod, "connect_ib", return_value=ib), \
             patch.object(self.mod, "disconnect_ib"), \
             patch.object(self.mod, "close_position", return_value=None), \
             patch.object(self.mod, "_retry_ibkr", side_effect=_retry_ibkr_patch), \
             patch.object(self.mod, "get_live_account_value", return_value=9_950.0), \
             patch.object(self.mod, "_get_last_price", return_value=190.0), \
             patch.object(self.mod, "_get_nasdaq_price", return_value=None), \
             patch("stock_bot.config.settings.ib_settings") as mock_settings, \
             patch("sys.argv", ["close_of_day.py"]):
            mock_settings.account = "DU123"
            self.mod.main()

        pick = saved["sessions"][0]["picks"][0]
        assert pick.get("stop_loss_triggered") is True
        assert pick["close_price"] == 190.0
        assert pick["day_return_pct"] < 0  # bought at 200, sold at 190
        # stop_slippage_pct: worst-case floor = 200 * (1 - 3.0/100) = 194.0
        # (real picker_config.json trailing_stop_pct=3.0, risk="medium" isn't
        # a "1".."5" key so it falls back to the flat rate). Exit at 190.0 is
        # below that floor -> negative slippage.
        assert pick["stop_slippage_pct"] == pytest.approx((190.0 - 194.0) / 194.0 * 100, abs=0.01)
        assert pick["stop_slippage_pct"] < 0

    def test_session_return_computed_correctly(self):
        """session_return_usd = close_value - open_value."""
        today = date_type.today().isoformat()
        portfolio = {
            "sessions": [_session(today, picks=[_pick("AAPL", buy_price=100.0, shares=10)])],
            "equity_curve": [],
            "initial_investment": 10_000.0,
        }
        # open_value = 10_000, net_liq = 10_500 → return = +500
        ib = _make_ib()
        ib.positions.return_value = []
        ib.reqOpenOrders.return_value = []
        ib.reqExecutions.return_value = []

        saved = {}

        def _retry_patch(fn, label, ib_arg, connect_fn, logger_arg, **kwargs):
            if "NetLiquidation" in label:
                return 10_500.0, ib_arg
            return 105.0, ib_arg

        with patch.object(self.mod, "load_portfolio", return_value=portfolio), \
             patch.object(self.mod, "save_portfolio", side_effect=lambda p, **kw: saved.update(p)), \
             patch.object(self.mod, "connect_ib", return_value=ib), \
             patch.object(self.mod, "disconnect_ib"), \
             patch.object(self.mod, "close_position", return_value=None), \
             patch.object(self.mod, "_retry_ibkr", side_effect=_retry_patch), \
             patch.object(self.mod, "get_live_account_value", return_value=10_500.0), \
             patch.object(self.mod, "_get_last_price", return_value=105.0), \
             patch.object(self.mod, "_get_nasdaq_price", return_value=None), \
             patch("stock_bot.config.settings.ib_settings") as mock_settings, \
             patch("sys.argv", ["close_of_day.py"]):
            mock_settings.account = "DU123"
            self.mod.main()

        session = saved["sessions"][0]
        assert session["portfolio_close_value"] == 10_500.0
        assert session["session_return_usd"] == 500.0
        assert abs(session["session_return_pct"] - 5.0) < 0.01


# ═══════════════════════════════════════════════════════════════════════════════
# Cancel open orders before selling
# ═══════════════════════════════════════════════════════════════════════════════

class TestCancelOrdersBeforeSell:

    def setup_method(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import importlib
        import close_of_day
        importlib.reload(close_of_day)
        self.mod = close_of_day

    def test_all_open_orders_cancelled(self):
        """cancelOrder must be called for every open order before positions are liquidated."""
        today = date_type.today().isoformat()
        portfolio = {
            "sessions": [_session(today, picks=[_pick("AAPL")])],
            "equity_curve": [],
            "initial_investment": 10_000.0,
        }

        ib = _make_ib()
        orders = [MagicMock(), MagicMock(), MagicMock()]
        for i, o in enumerate(orders):
            o.contract.symbol = f"TICK{i}"
            o.order.orderType = "LMT"
        ib.reqOpenOrders.return_value = orders
        ib.positions.return_value = []
        ib.reqExecutions.return_value = []

        with patch.object(self.mod, "load_portfolio", return_value=portfolio), \
             patch.object(self.mod, "save_portfolio"), \
             patch.object(self.mod, "connect_ib", return_value=ib), \
             patch.object(self.mod, "disconnect_ib"), \
             patch.object(self.mod, "close_position", return_value=None), \
             patch.object(self.mod, "get_net_liquidation", return_value=10_000.0), \
             patch.object(self.mod, "get_live_account_value", return_value=10_000.0), \
             patch.object(self.mod, "_retry_ibkr",
                          side_effect=lambda fn, label, ib_arg, *a, **kw: (10_000.0, ib_arg)), \
             patch.object(self.mod, "_get_last_price", return_value=None), \
             patch.object(self.mod, "_get_nasdaq_price", return_value=None), \
             patch("stock_bot.config.settings.ib_settings") as mock_settings, \
             patch("sys.argv", ["close_of_day.py"]):
            mock_settings.account = "DU123"
            self.mod.main()

        assert ib.cancelOrder.call_count == 3

    def test_held_ticker_stop_order_not_cancelled(self):
        """STP/TRAIL orders for multi-day held tickers must be preserved."""
        today = date_type.today().isoformat()
        future = "2099-12-31"
        portfolio = {
            "sessions": [{
                "date": today,
                "mode": "aggressive",
                "no_picks": False,
                "picks": [_pick("HELD", hold_until=future), _pick("SELL")],
                "portfolio_open_value": 20_000.0,
                "portfolio_close_value": None,
                "session_return_usd": None,
                "session_return_pct": None,
                "qqq_buy_price": 400.0,
            }],
            "equity_curve": [],
            "initial_investment": 20_000.0,
        }

        ib = _make_ib()
        # Two open orders: a TRAIL for HELD (must keep) and a LMT for SELL (must cancel)
        held_order = MagicMock()
        held_order.contract.symbol = "HELD"
        held_order.order.orderType = "TRAIL"

        sell_order = MagicMock()
        sell_order.contract.symbol = "SELL"
        sell_order.order.orderType = "LMT"

        ib.reqOpenOrders.return_value = [held_order, sell_order]
        ib.positions.return_value = []
        ib.reqExecutions.return_value = []

        with patch.object(self.mod, "load_portfolio", return_value=portfolio), \
             patch.object(self.mod, "save_portfolio"), \
             patch.object(self.mod, "connect_ib", return_value=ib), \
             patch.object(self.mod, "disconnect_ib"), \
             patch.object(self.mod, "close_position", return_value=None), \
             patch.object(self.mod, "_retry_ibkr",
                          side_effect=lambda fn, label, ib_arg, *a, **kw: (20_000.0, ib_arg)), \
             patch.object(self.mod, "get_live_account_value", return_value=10_000.0), \
             patch.object(self.mod, "_get_last_price", return_value=None), \
             patch.object(self.mod, "_get_nasdaq_price", return_value=None), \
             patch("stock_bot.config.settings.ib_settings") as mock_settings, \
             patch("sys.argv", ["close_of_day.py"]):
            mock_settings.account = "DU123"
            self.mod.main()

        # Only the SELL LMT order should be cancelled, not the HELD TRAIL
        cancelled_orders = [call_args[0][0] for call_args in ib.cancelOrder.call_args_list]
        assert sell_order.order in cancelled_orders
        assert held_order.order not in cancelled_orders


# ═══════════════════════════════════════════════════════════════════════════════
# Equity curve
# ═══════════════════════════════════════════════════════════════════════════════

class TestEquityCurve:

    def setup_method(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import importlib
        import close_of_day
        importlib.reload(close_of_day)
        self.mod = close_of_day

    def _run_close(self, portfolio, net_liq):
        today = date_type.today().isoformat()
        ib = _make_ib()
        ib.positions.return_value = []
        ib.reqOpenOrders.return_value = []
        ib.reqExecutions.return_value = []

        saved = {}

        def _retry_patch(fn, label, ib_arg, connect_fn, logger_arg, **kwargs):
            if "NetLiquidation" in label:
                return net_liq, ib_arg
            return 410.0, ib_arg  # QQQ price

        with patch.object(self.mod, "load_portfolio", return_value=portfolio), \
             patch.object(self.mod, "save_portfolio", side_effect=lambda p, **kw: saved.update(p)), \
             patch.object(self.mod, "connect_ib", return_value=ib), \
             patch.object(self.mod, "disconnect_ib"), \
             patch.object(self.mod, "close_position", return_value=None), \
             patch.object(self.mod, "_retry_ibkr", side_effect=_retry_patch), \
             patch.object(self.mod, "get_live_account_value", return_value=net_liq), \
             patch.object(self.mod, "_get_last_price", return_value=None), \
             patch.object(self.mod, "_get_nasdaq_price", return_value=None), \
             patch("stock_bot.config.settings.ib_settings") as mock_settings, \
             patch("sys.argv", ["close_of_day.py"]):
            mock_settings.account = "DU123"
            self.mod.main()

        return saved

    def test_new_equity_point_appended(self):
        today = date_type.today().isoformat()
        portfolio = {
            "sessions": [_session(today, picks=[_pick("AAPL")])],
            "equity_curve": [{"date": "2020-01-01", "portfolio_value": 10_000.0, "qqq_indexed": 10_000.0}],
            "initial_investment": 10_000.0,
        }
        saved = self._run_close(portfolio, net_liq=10_500.0)
        dates = [e["date"] for e in saved["equity_curve"]]
        assert today in dates
        assert len(dates) == 2  # old point + new point

    def test_existing_date_replaced_not_duplicated(self):
        today = date_type.today().isoformat()
        portfolio = {
            "sessions": [_session(today, picks=[_pick("AAPL")])],
            "equity_curve": [{"date": today, "portfolio_value": 9_900.0, "qqq_indexed": 9_900.0}],
            "initial_investment": 10_000.0,
        }
        saved = self._run_close(portfolio, net_liq=10_600.0)
        today_points = [e for e in saved["equity_curve"] if e["date"] == today]
        assert len(today_points) == 1
        assert today_points[0]["portfolio_value"] == 10_600.0

    def test_qqq_indexed_computed_relative_to_first_session(self):
        """QQQ indexed = (qqq_close / initial_qqq) * initial_investment."""
        today = date_type.today().isoformat()
        # First session had qqq_buy_price=400, initial_investment=10_000
        # QQQ close=410 → indexed = (410/400)*10_000 = 10_250
        portfolio = {
            "sessions": [
                {
                    "date": "2020-01-01",
                    "qqq_buy_price": 400.0,
                    "picks": [],
                    "portfolio_open_value": 10_000.0,
                    "portfolio_close_value": 10_000.0,
                    "mode": "aggressive",
                },
                _session(today, picks=[_pick("AAPL")]),
            ],
            "equity_curve": [],
            "initial_investment": 10_000.0,
        }
        saved = self._run_close(portfolio, net_liq=10_500.0)
        today_eq = next(e for e in saved["equity_curve"] if e["date"] == today)
        expected_qqq_indexed = round((410.0 / 400.0) * 10_000.0, 2)
        assert today_eq["qqq_indexed"] == expected_qqq_indexed


# ═══════════════════════════════════════════════════════════════════════════════
# portfolio_writer — write_session
# ═══════════════════════════════════════════════════════════════════════════════

class TestWriteSession:

    def setup_method(self):
        from stock_bot.data_sources import portfolio_writer
        import importlib
        importlib.reload(portfolio_writer)
        self.mod = portfolio_writer

    def _make_ib(self, last_price=100.0):
        ib = MagicMock()
        ib.qualifyContracts.return_value = [MagicMock()]
        bar = MagicMock()
        bar.close = last_price
        ib.reqHistoricalData.return_value = [bar]
        return ib

    def test_empty_picks_writes_no_pick_stub(self):
        """write_session([]) must write a no-pick stub, not crash."""
        portfolio = {
            "sessions": [],
            "equity_curve": [],
            "initial_investment": 10_000.0,
        }
        ib = self._make_ib()
        saved = {}

        with patch.object(self.mod, "load_portfolio", return_value=portfolio), \
             patch.object(self.mod, "save_portfolio", side_effect=lambda p, **kw: saved.update(p)), \
             patch.object(self.mod, "_get_nasdaq_price", return_value=None):
            self.mod.write_session([], ib)

        today = date_type.today().isoformat()
        assert len(saved["sessions"]) == 1
        stub = saved["sessions"][0]
        assert stub["date"] == today
        assert stub["no_picks"] is True
        assert stub["picks"] == []

    def test_no_picks_stub_uses_open_value_override(self):
        """If open_value_override is provided, the stub must use it."""
        portfolio = {"sessions": [], "equity_curve": [], "initial_investment": 10_000.0}
        ib = self._make_ib()
        saved = {}

        with patch.object(self.mod, "load_portfolio", return_value=portfolio), \
             patch.object(self.mod, "save_portfolio", side_effect=lambda p, **kw: saved.update(p)), \
             patch.object(self.mod, "_get_nasdaq_price", return_value=None):
            self.mod.write_session([], ib, open_value_override=15_000.0)

        assert saved["sessions"][0]["portfolio_open_value"] == 15_000.0

    def test_existing_session_replaced_not_appended(self):
        """Running write_session twice must replace the existing session, not duplicate it."""
        today = date_type.today().isoformat()
        old_session = {"date": today, "mode": "conservative", "picks": [], "portfolio_open_value": 9_000.0}
        portfolio = {"sessions": [old_session], "equity_curve": [], "initial_investment": 10_000.0}
        ib = self._make_ib()
        saved = {}

        picks = [{"ticker": "AAPL", "score": 8, "direction": "long", "risk": "medium",
                  "expected_gain_pct": 5.0, "reason": "test", "trend_summary": ""}]

        with patch.object(self.mod, "load_portfolio", return_value=portfolio), \
             patch.object(self.mod, "save_portfolio", side_effect=lambda p, **kw: saved.update(p)), \
             patch.object(self.mod, "_get_nasdaq_price", return_value=None):
            self.mod.write_session(picks, ib, mode="aggressive")

        assert len(saved["sessions"]) == 1
        assert saved["sessions"][0]["mode"] == "aggressive"

    def test_first_session_sets_initial_investment(self):
        """On the very first session, initial_investment must be set from open_value_override."""
        portfolio = {"sessions": [], "equity_curve": [], "initial_investment": 10_000.0}
        ib = self._make_ib()
        saved = {}

        picks = [{"ticker": "AAPL", "score": 8, "direction": "long", "risk": "medium",
                  "expected_gain_pct": 5.0, "reason": "test", "trend_summary": ""}]

        with patch.object(self.mod, "load_portfolio", return_value=portfolio), \
             patch.object(self.mod, "save_portfolio", side_effect=lambda p, **kw: saved.update(p)), \
             patch.object(self.mod, "_get_nasdaq_price", return_value=None):
            self.mod.write_session(picks, ib, open_value_override=12_500.0)

        assert saved["initial_investment"] == 12_500.0

    def test_second_session_does_not_overwrite_initial_investment(self):
        """If there are existing sessions, initial_investment must NOT change."""
        today = date_type.today().isoformat()
        portfolio = {
            "sessions": [{"date": "2020-01-01", "portfolio_close_value": 10_200.0, "qqq_buy_price": None}],
            "equity_curve": [],
            "initial_investment": 10_000.0,
        }
        ib = self._make_ib()
        saved = {}

        picks = [{"ticker": "MSFT", "score": 7, "direction": "long", "risk": "low",
                  "expected_gain_pct": 3.0, "reason": "test", "trend_summary": ""}]

        with patch.object(self.mod, "load_portfolio", return_value=portfolio), \
             patch.object(self.mod, "save_portfolio", side_effect=lambda p, **kw: saved.update(p)), \
             patch.object(self.mod, "_get_nasdaq_price", return_value=None):
            self.mod.write_session(picks, ib, open_value_override=10_200.0)

        assert saved["initial_investment"] == 10_000.0  # unchanged

    def test_actual_fill_price_used_over_market_price(self):
        """When trades_by_ticker contains a filled order, use avgFillPrice not market price."""
        portfolio = {"sessions": [], "equity_curve": [], "initial_investment": 10_000.0}
        ib = self._make_ib(last_price=105.0)  # market price is 105

        trade = MagicMock()
        trade.orderStatus.filled = 10
        trade.orderStatus.avgFillPrice = 103.5
        trade.order.action = "BUY"

        picks = [{"ticker": "NVDA", "score": 9, "direction": "long", "risk": "medium",
                  "expected_gain_pct": 7.0, "reason": "test", "trend_summary": "",
                  "allocation_pct": 100.0}]

        saved = {}

        with patch.object(self.mod, "load_portfolio", return_value=portfolio), \
             patch.object(self.mod, "save_portfolio", side_effect=lambda p, **kw: saved.update(p)), \
             patch.object(self.mod, "_get_nasdaq_price", return_value=None):
            self.mod.write_session(picks, ib, trades_by_ticker={"NVDA": [trade]},
                                   open_value_override=10_000.0)

        pick_entry = saved["sessions"][0]["picks"][0]
        assert pick_entry["buy_price"] == 103.5
        assert pick_entry["shares"] == 10

    def test_equity_curve_point_not_duplicated(self):
        """write_session must replace an existing equity curve point for today, not append."""
        today = date_type.today().isoformat()
        portfolio = {
            "sessions": [],
            "equity_curve": [{"date": today, "portfolio_value": 9_000.0, "qqq_indexed": 9_000.0}],
            "initial_investment": 10_000.0,
        }
        ib = self._make_ib()
        saved = {}

        picks = [{"ticker": "TSLA", "score": 8, "direction": "long", "risk": "medium",
                  "expected_gain_pct": 5.0, "reason": "test", "trend_summary": ""}]

        with patch.object(self.mod, "load_portfolio", return_value=portfolio), \
             patch.object(self.mod, "save_portfolio", side_effect=lambda p, **kw: saved.update(p)), \
             patch.object(self.mod, "_get_nasdaq_price", return_value=None):
            self.mod.write_session(picks, ib, open_value_override=10_500.0)

        today_pts = [e for e in saved["equity_curve"] if e["date"] == today]
        assert len(today_pts) == 1
        assert today_pts[0]["portfolio_value"] == 10_500.0


# ═══════════════════════════════════════════════════════════════════════════════
# portfolio_writer — _get_open_value
# ═══════════════════════════════════════════════════════════════════════════════

class TestGetOpenValue:

    def setup_method(self):
        from stock_bot.data_sources import portfolio_writer
        self.mod = portfolio_writer

    def test_returns_last_close_value(self):
        portfolio = {
            "sessions": [
                {"portfolio_close_value": 10_200.0},
                {"portfolio_close_value": 10_500.0},
            ],
            "initial_investment": 10_000.0,
        }
        result = self.mod._get_open_value(portfolio)
        assert result == 10_500.0

    def test_skips_error_sessions(self):
        """If the most recent session has 'ERROR', skip it and use the prior session."""
        portfolio = {
            "sessions": [
                {"portfolio_close_value": 10_000.0},
                {"portfolio_close_value": "ERROR"},
            ],
            "initial_investment": 9_000.0,
        }
        result = self.mod._get_open_value(portfolio)
        assert result == 10_000.0

    def test_all_error_sessions_use_initial_investment(self):
        """If all sessions are ERROR, fall back to initial_investment."""
        portfolio = {
            "sessions": [
                {"portfolio_close_value": "ERROR"},
                {"portfolio_close_value": "ERROR"},
            ],
            "initial_investment": 7_500.0,
        }
        result = self.mod._get_open_value(portfolio)
        assert result == 7_500.0

    def test_empty_sessions_uses_initial_investment(self):
        portfolio = {"sessions": [], "initial_investment": 10_000.0}
        result = self.mod._get_open_value(portfolio)
        assert result == 10_000.0

    def test_none_close_value_skipped(self):
        """Sessions with portfolio_close_value=None (not yet closed) are skipped."""
        portfolio = {
            "sessions": [
                {"portfolio_close_value": 10_000.0},
                {"portfolio_close_value": None},
            ],
            "initial_investment": 8_000.0,
        }
        result = self.mod._get_open_value(portfolio)
        assert result == 10_000.0


# ═══════════════════════════════════════════════════════════════════════════════
# QQQ benchmark computation
# ═══════════════════════════════════════════════════════════════════════════════

class TestQQQBenchmark:

    def setup_method(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import importlib
        import close_of_day
        importlib.reload(close_of_day)
        self.mod = close_of_day

    def test_qqq_day_return_pct_computed(self):
        """qqq_day_return_pct = (close - buy) / buy * 100."""
        today = date_type.today().isoformat()
        session = _session(today, picks=[])
        session["qqq_buy_price"] = 400.0
        portfolio = {
            "sessions": [session],
            "equity_curve": [],
            "initial_investment": 10_000.0,
        }

        ib = _make_ib()
        ib.positions.return_value = []
        ib.reqOpenOrders.return_value = []
        ib.reqExecutions.return_value = []

        saved = {}

        def _retry_patch(fn, label, ib_arg, connect_fn, logger_arg, **kwargs):
            if "NetLiquidation" in label:
                return 10_200.0, ib_arg
            # QQQ close
            return 408.0, ib_arg

        with patch.object(self.mod, "load_portfolio", return_value=portfolio), \
             patch.object(self.mod, "save_portfolio", side_effect=lambda p, **kw: saved.update(p)), \
             patch.object(self.mod, "connect_ib", return_value=ib), \
             patch.object(self.mod, "disconnect_ib"), \
             patch.object(self.mod, "_retry_ibkr", side_effect=_retry_patch), \
             patch.object(self.mod, "get_live_account_value", return_value=10_200.0), \
             patch.object(self.mod, "_get_last_price", return_value=None), \
             patch.object(self.mod, "_get_nasdaq_price", return_value=None), \
             patch("stock_bot.config.settings.ib_settings") as mock_settings, \
             patch("sys.argv", ["close_of_day.py"]):
            mock_settings.account = "DU123"
            self.mod.main()

        session_out = saved["sessions"][0]
        # (408 - 400) / 400 * 100 = 2.0
        assert abs(session_out.get("qqq_day_return_pct", 0) - 2.0) < 0.01

    def test_qqq_skipped_when_no_buy_price(self):
        """If qqq_buy_price is 0 or None, qqq_day_return_pct must not be set."""
        today = date_type.today().isoformat()
        session = _session(today, picks=[])
        session["qqq_buy_price"] = None
        portfolio = {
            "sessions": [session],
            "equity_curve": [],
            "initial_investment": 10_000.0,
        }

        ib = _make_ib()
        ib.positions.return_value = []
        ib.reqOpenOrders.return_value = []
        ib.reqExecutions.return_value = []

        saved = {}

        def _retry_patch(fn, label, ib_arg, connect_fn, logger_arg, **kwargs):
            return 10_000.0, ib_arg

        with patch.object(self.mod, "load_portfolio", return_value=portfolio), \
             patch.object(self.mod, "save_portfolio", side_effect=lambda p, **kw: saved.update(p)), \
             patch.object(self.mod, "connect_ib", return_value=ib), \
             patch.object(self.mod, "disconnect_ib"), \
             patch.object(self.mod, "_retry_ibkr", side_effect=_retry_patch), \
             patch.object(self.mod, "get_live_account_value", return_value=10_000.0), \
             patch.object(self.mod, "_get_last_price", return_value=None), \
             patch.object(self.mod, "_get_nasdaq_price", return_value=None), \
             patch("stock_bot.config.settings.ib_settings") as mock_settings, \
             patch("sys.argv", ["close_of_day.py"]):
            mock_settings.account = "DU123"
            self.mod.main()

        session_out = saved["sessions"][0]
        assert session_out.get("qqq_day_return_pct") is None


# ═══════════════════════════════════════════════════════════════════════════════
# Execution map building (stop-loss fills from reqExecutions)
# ═══════════════════════════════════════════════════════════════════════════════

class TestExecMap:

    def setup_method(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import importlib
        import close_of_day
        importlib.reload(close_of_day)
        self.mod = close_of_day

    def test_weighted_avg_price_from_multiple_fills(self):
        """exec_map must compute weighted-average price from multiple partial fills."""
        today = date_type.today().isoformat()
        portfolio = {
            "sessions": [_session(today, picks=[_pick("TSLA", buy_price=200.0, shares=20)])],
            "equity_curve": [],
            "initial_investment": 10_000.0,
        }

        ib = _make_ib()
        ib.positions.return_value = []  # Stop fired — no position left
        ib.reqOpenOrders.return_value = []

        # Two partial fills: 10 @ 190, 10 @ 195 → weighted avg = 192.50
        fill1 = MagicMock()
        fill1.execution.side = "SLD"
        fill1.execution.shares = 10.0
        fill1.execution.price = 190.0
        fill1.contract.symbol = "TSLA"

        fill2 = MagicMock()
        fill2.execution.side = "SLD"
        fill2.execution.shares = 10.0
        fill2.execution.price = 195.0
        fill2.contract.symbol = "TSLA"

        ib.reqExecutions.return_value = [fill1, fill2]

        saved = {}

        def _retry_patch(fn, label, ib_arg, connect_fn, logger_arg, **kwargs):
            if "NetLiquidation" in label:
                return 9_960.0, ib_arg
            return 192.5, ib_arg

        with patch.object(self.mod, "load_portfolio", return_value=portfolio), \
             patch.object(self.mod, "save_portfolio", side_effect=lambda p, **kw: saved.update(p)), \
             patch.object(self.mod, "connect_ib", return_value=ib), \
             patch.object(self.mod, "disconnect_ib"), \
             patch.object(self.mod, "close_position", return_value=None), \
             patch.object(self.mod, "_retry_ibkr", side_effect=_retry_patch), \
             patch.object(self.mod, "get_live_account_value", return_value=9_960.0), \
             patch.object(self.mod, "_get_last_price", return_value=192.5), \
             patch.object(self.mod, "_get_nasdaq_price", return_value=None), \
             patch("stock_bot.config.settings.ib_settings") as mock_settings, \
             patch("sys.argv", ["close_of_day.py"]):
            mock_settings.account = "DU123"
            self.mod.main()

        pick = saved["sessions"][0]["picks"][0]
        assert pick["close_price"] == 192.5
        assert pick.get("stop_loss_triggered") is True

    def test_buy_fills_not_included_in_exec_map(self):
        """Only 'SLD' (sell) fills must be included in exec_map, not 'BOT' (buy) fills."""
        today = date_type.today().isoformat()
        portfolio = {
            "sessions": [_session(today, picks=[_pick("AMZN", buy_price=150.0, shares=5)])],
            "equity_curve": [],
            "initial_investment": 10_000.0,
        }

        ib = _make_ib()
        # position exists at close — regular sell will handle P&L, not exec_map
        pos = MagicMock()
        pos.contract.secType = "STK"
        pos.contract.symbol = "AMZN"
        pos.position = 5.0
        ib.positions.return_value = [pos]
        ib.reqOpenOrders.return_value = []

        buy_fill = MagicMock()
        buy_fill.execution.side = "BOT"
        buy_fill.execution.shares = 5.0
        buy_fill.execution.price = 150.0
        buy_fill.contract.symbol = "AMZN"
        ib.reqExecutions.return_value = [buy_fill]

        trade = MagicMock()
        trade.orderStatus.filled = 5
        trade.orderStatus.avgFillPrice = 160.0

        saved = {}

        def _retry_patch(fn, label, ib_arg, connect_fn, logger_arg, **kwargs):
            if "NetLiquidation" in label:
                return 10_050.0, ib_arg
            return 160.0, ib_arg

        with patch.object(self.mod, "load_portfolio", return_value=portfolio), \
             patch.object(self.mod, "save_portfolio", side_effect=lambda p, **kw: saved.update(p)), \
             patch.object(self.mod, "connect_ib", return_value=ib), \
             patch.object(self.mod, "disconnect_ib"), \
             patch.object(self.mod, "close_position", return_value=trade), \
             patch.object(self.mod, "_retry_ibkr", side_effect=_retry_patch), \
             patch.object(self.mod, "get_live_account_value", return_value=10_050.0), \
             patch.object(self.mod, "_get_last_price", return_value=160.0), \
             patch.object(self.mod, "_get_nasdaq_price", return_value=None), \
             patch("stock_bot.config.settings.ib_settings") as mock_settings, \
             patch("sys.argv", ["close_of_day.py"]):
            mock_settings.account = "DU123"
            self.mod.main()

        pick = saved["sessions"][0]["picks"][0]
        # Should use sell fill price (160), not buy fill price (150)
        assert pick["close_price"] == 160.0
        assert not pick.get("stop_loss_triggered")
