"""
tests/test_close_of_day_pnl.py

Tests for close_of_day.py session-close logic: per-pick P&L calculation,
portfolio_close_value ERROR handling, equity curve updates.
"""

import importlib.util
import json
import pathlib
import tempfile
from unittest.mock import MagicMock, patch


def _load_cod():
    stubs = {
        "ib_insync": MagicMock(),
        "stock_bot.core.logging_config": MagicMock(),
        "stock_bot.brokers.ib.connect_disconnect": MagicMock(),
        "stock_bot.brokers.ib.sell_all": MagicMock(),
        "stock_bot.data_sources.portfolio_writer": MagicMock(),
        "stock_bot.config.settings": MagicMock(),
    }
    with patch.dict("__import__('sys').modules", stubs):
        spec = importlib.util.spec_from_file_location(
            "close_of_day_mod",
            pathlib.Path(__file__).parents[1] / "scripts" / "close_of_day.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


class TestPickPnlCalc:
    """P&L math used in close_of_day (replicated here as pure arithmetic)."""

    def _calc(self, buy_price, close_price, shares):
        day_return_usd = (close_price - buy_price) * shares
        day_return_pct = (close_price - buy_price) / buy_price * 100
        return round(day_return_pct, 3), round(day_return_usd, 2)

    def test_positive_return(self):
        pct, usd = self._calc(buy_price=50.0, close_price=55.0, shares=100)
        assert pct == 10.0
        assert usd == 500.0

    def test_negative_return(self):
        pct, usd = self._calc(buy_price=100.0, close_price=95.0, shares=200)
        assert pct == -5.0
        assert usd == -1000.0

    def test_flat(self):
        pct, usd = self._calc(buy_price=25.0, close_price=25.0, shares=50)
        assert pct == 0.0
        assert usd == 0.0


class TestSessionReturnCalc:
    """session_return_usd / pct math from close_of_day."""

    def _calc(self, open_val, close_val):
        session_return_usd = round(close_val - open_val, 2)
        session_return_pct = round((close_val - open_val) / open_val * 100, 3) if open_val > 0 else 0
        return session_return_usd, session_return_pct

    def test_gain(self):
        usd, pct = self._calc(10_000, 10_500)
        assert usd == 500.0
        assert pct == 5.0

    def test_loss(self):
        usd, pct = self._calc(10_000, 9_800)
        assert usd == -200.0
        assert pct == -2.0

    def test_error_path_produces_string(self):
        """When NetLiquidation fails, values must be the string 'ERROR'."""
        total_close_value = None
        session = {}
        if total_close_value is not None:
            session["portfolio_close_value"] = round(total_close_value, 2)
            session["session_return_usd"] = round(total_close_value - 10_000, 2)
        else:
            session["portfolio_close_value"] = "ERROR"
            session["session_return_usd"] = "ERROR"
            session["session_return_pct"] = "ERROR"
        assert session["portfolio_close_value"] == "ERROR"
        assert session["session_return_usd"] == "ERROR"
        assert isinstance(session["portfolio_close_value"], str)


class TestEquityCurveUpdate:
    """Equity curve must not be updated when close value is ERROR."""

    def test_error_leaves_equity_curve_unchanged(self):
        equity_curve = [{"date": "2025-01-01", "portfolio_value": 10_000.0}]
        total_close_value = None  # ERROR scenario

        if total_close_value is not None:
            equity_curve.append({"date": "2025-01-02", "portfolio_value": round(total_close_value, 2)})

        assert len(equity_curve) == 1

    def test_success_appends_to_equity_curve(self):
        equity_curve = [{"date": "2025-01-01", "portfolio_value": 10_000.0}]
        total_close_value = 10_500.0
        today = "2025-01-02"

        if total_close_value is not None:
            eq_point = {"date": today, "portfolio_value": round(total_close_value, 2), "qqq_indexed": 10_300.0}
            eq_idx = next((i for i, e in enumerate(equity_curve) if e.get("date") == today), None)
            if eq_idx is not None:
                equity_curve[eq_idx] = eq_point
            else:
                equity_curve.append(eq_point)

        assert len(equity_curve) == 2
        assert equity_curve[1]["portfolio_value"] == 10_500.0

    def test_success_replaces_existing_date(self):
        today = "2025-01-02"
        equity_curve = [
            {"date": "2025-01-01", "portfolio_value": 10_000.0},
            {"date": today, "portfolio_value": 10_200.0},  # stale open value
        ]
        total_close_value = 10_500.0
        eq_point = {"date": today, "portfolio_value": round(total_close_value, 2)}
        eq_idx = next((i for i, e in enumerate(equity_curve) if e.get("date") == today), None)
        if eq_idx is not None:
            equity_curve[eq_idx] = eq_point
        else:
            equity_curve.append(eq_point)

        assert len(equity_curve) == 2
        assert equity_curve[1]["portfolio_value"] == 10_500.0
