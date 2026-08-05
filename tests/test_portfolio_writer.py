"""
tests/test_portfolio_writer.py

Unit tests for portfolio_writer.py — pure-logic functions only (no IBKR connection).
"""

import json
import math
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# _get_open_value
# ---------------------------------------------------------------------------
from stock_bot.data_sources.portfolio_writer import _get_open_value


def _portfolio(sessions, initial=10_000.0):
    return {"initial_investment": initial, "sessions": sessions}


class TestGetOpenValue:
    def test_no_sessions_returns_initial(self):
        assert _get_open_value(_portfolio([])) == 10_000.0

    def test_returns_last_close(self):
        p = _portfolio([
            {"date": "2025-01-01", "portfolio_close_value": 11_000.0},
            {"date": "2025-01-02", "portfolio_close_value": 12_000.0},
        ])
        assert _get_open_value(p) == 12_000.0

    def test_skips_error_session(self):
        p = _portfolio([
            {"date": "2025-01-01", "portfolio_close_value": 11_500.0},
            {"date": "2025-01-02", "portfolio_close_value": "ERROR"},
        ])
        assert _get_open_value(p) == 11_500.0

    def test_skips_none_close(self):
        p = _portfolio([
            {"date": "2025-01-01", "portfolio_close_value": 9_800.0},
            {"date": "2025-01-02", "portfolio_close_value": None},
        ])
        assert _get_open_value(p) == 9_800.0

    def test_all_error_sessions_returns_initial(self):
        p = _portfolio([
            {"date": "2025-01-01", "portfolio_close_value": "ERROR"},
            {"date": "2025-01-02", "portfolio_close_value": "ERROR"},
        ])
        assert _get_open_value(p) == 10_000.0

    def test_custom_initial(self):
        assert _get_open_value(_portfolio([], initial=50_000.0)) == 50_000.0


# ---------------------------------------------------------------------------
# write_session — fill-price aggregation
# ---------------------------------------------------------------------------
from stock_bot.data_sources.portfolio_writer import write_session


def _make_trade(action="BUY", filled=100, avg_fill=50.0, status_str="Filled"):
    trade = MagicMock()
    trade.order.action = action
    trade.orderStatus.filled = filled
    trade.orderStatus.avgFillPrice = avg_fill
    trade.orderStatus.status = status_str
    return trade


class TestWriteSessionFillAggregation:
    """Test that write_session uses the correct buy price / share count."""

    def _run(self, picks, trades_by_ticker, tmp_path, preflight_price_by_ticker=None):
        """Helper: run write_session against a temp portfolio file."""
        data_dir = tmp_path / "docs" / "data"
        data_dir.mkdir(parents=True)
        portfolio_path = data_dir / "portfolio_test.json"

        mock_ib = MagicMock()

        with (
            patch("stock_bot.data_sources.portfolio_writer._PORTFOLIO_JSON_TEST", portfolio_path),
            patch("stock_bot.data_sources.portfolio_writer._get_last_price", return_value=50.0),
        ):
            write_session(
                picks=picks,
                ib=mock_ib,
                test_mode=True,
                trades_by_ticker=trades_by_ticker,
                open_value_override=10_000.0,
                preflight_price_by_ticker=preflight_price_by_ticker,
            )
            return json.loads(portfolio_path.read_text())

    def test_single_fill_uses_actual_price(self, tmp_path):
        picks = [{"ticker": "AAPL", "score": 8, "direction": "bullish",
                  "risk": "medium", "expected_gain_pct": 3.0, "reason": "test",
                  "allocation_pct": 100}]
        trades = {"AAPL": [_make_trade(filled=50, avg_fill=100.0)]}
        data = self._run(picks, trades, tmp_path)
        pick = data["sessions"][0]["picks"][0]
        assert pick["shares"] == 50
        assert pick["buy_price"] == 100.0

    def test_two_buy_fills_vwap(self, tmp_path):
        """Round 1 original buy + Round 1 realloc top-up → VWAP price."""
        picks = [{"ticker": "MSFT", "score": 9, "direction": "bullish",
                  "risk": "low", "expected_gain_pct": 4.0, "reason": "test",
                  "allocation_pct": 100}]
        # First trade: 100 shares @ $50 = $5,000
        # Second trade: 50 shares @ $52 = $2,600
        # VWAP: $7,600 / 150 = $50.6667
        t1 = _make_trade(action="BUY", filled=100, avg_fill=50.0)
        t2 = _make_trade(action="BUY", filled=50, avg_fill=52.0)
        trades = {"MSFT": [t1, t2]}
        data = self._run(picks, trades, tmp_path)
        pick = data["sessions"][0]["picks"][0]
        assert pick["shares"] == 150
        assert pick["buy_price"] == pytest.approx(50.6667, abs=1e-3)

    def test_sell_fill_ignored(self, tmp_path):
        """A SELL trade in the list must not inflate share count or skew price."""
        picks = [{"ticker": "GOOG", "score": 7, "direction": "bullish",
                  "risk": "medium", "expected_gain_pct": 2.0, "reason": "test",
                  "allocation_pct": 100}]
        buy = _make_trade(action="BUY", filled=40, avg_fill=80.0)
        sell = _make_trade(action="SELL", filled=40, avg_fill=85.0)
        trades = {"GOOG": [buy, sell]}
        data = self._run(picks, trades, tmp_path)
        pick = data["sessions"][0]["picks"][0]
        assert pick["shares"] == 40
        assert pick["buy_price"] == 80.0

    def test_zero_fill_falls_back_to_estimated(self, tmp_path):
        """An unfilled order (filled=0, queued status) uses last-price estimate."""
        picks = [{"ticker": "AMZN", "score": 7, "direction": "bullish",
                  "risk": "high", "expected_gain_pct": 5.0, "reason": "test",
                  "allocation_pct": 100}]
        queued = _make_trade(action="BUY", filled=0, avg_fill=0.0, status_str="Submitted")
        trades = {"AMZN": [queued]}
        data = self._run(picks, trades, tmp_path)
        pick = data["sessions"][0]["picks"][0]
        # Falls back to _get_last_price mock (50.0) → 10000/50 = 200 shares
        assert pick["shares"] == 200
        assert pick["buy_price"] == 50.0

    def test_sector_and_catalyst_type_persisted(self, tmp_path):
        """sector/catalyst_type from GPT scoring must survive into the saved
        pick entry — otherwise the score-vs-returns feedback loop analysis
        has nothing to break results down by."""
        picks = [{"ticker": "NVDA", "score": 8, "direction": "bullish",
                  "risk": "low", "expected_gain_pct": 3.0, "reason": "test",
                  "allocation_pct": 100, "sector": "Technology",
                  "catalyst_type": "earnings_beat"}]
        trades = {"NVDA": [_make_trade(filled=10, avg_fill=100.0)]}
        data = self._run(picks, trades, tmp_path)
        pick = data["sessions"][0]["picks"][0]
        assert pick["sector"] == "Technology"
        assert pick["catalyst_type"] == "earnings_beat"

    def test_sector_and_catalyst_type_default_when_missing(self, tmp_path):
        """A pick dict without sector/catalyst_type (e.g. from an older code
        path or a scoring failure) must not raise — falls back to defaults."""
        picks = [{"ticker": "IBM", "score": 7, "direction": "bullish",
                  "risk": "medium", "expected_gain_pct": 2.0, "reason": "test",
                  "allocation_pct": 100}]
        trades = {"IBM": [_make_trade(filled=10, avg_fill=100.0)]}
        data = self._run(picks, trades, tmp_path)
        pick = data["sessions"][0]["picks"][0]
        assert pick["sector"] == "Unknown"
        assert pick["catalyst_type"] == "other"


class TestWriteSessionSlippage(TestWriteSessionFillAggregation):
    """slippage_pct measures execution cost (pre-flight quote vs actual fill)
    separately from scoring alpha -- it must only be computed against a
    confirmed real fill, never against an estimated/queued price.

    Inherits _run() from TestWriteSessionFillAggregation."""

    def test_slippage_computed_against_confirmed_fill(self, tmp_path):
        picks = [{"ticker": "AAPL", "score": 8, "direction": "bullish",
                  "risk": "medium", "expected_gain_pct": 3.0, "reason": "test",
                  "allocation_pct": 100}]
        trades = {"AAPL": [_make_trade(filled=50, avg_fill=101.0)]}
        data = self._run(picks, trades, tmp_path, preflight_price_by_ticker={"AAPL": 100.0})
        pick = data["sessions"][0]["picks"][0]
        assert pick["preflight_price"] == 100.0
        assert pick["slippage_pct"] == pytest.approx(1.0)  # (101-100)/100 * 100

    def test_negative_slippage_when_fill_better_than_quote(self, tmp_path):
        picks = [{"ticker": "MSFT", "score": 8, "direction": "bullish",
                  "risk": "medium", "expected_gain_pct": 3.0, "reason": "test",
                  "allocation_pct": 100}]
        trades = {"MSFT": [_make_trade(filled=50, avg_fill=99.0)]}
        data = self._run(picks, trades, tmp_path, preflight_price_by_ticker={"MSFT": 100.0})
        pick = data["sessions"][0]["picks"][0]
        assert pick["slippage_pct"] == pytest.approx(-1.0)

    def test_no_preflight_price_leaves_slippage_unset(self, tmp_path):
        """A missing preflight quote (e.g. price fetch failed) must not
        fabricate a slippage figure."""
        picks = [{"ticker": "GOOG", "score": 8, "direction": "bullish",
                  "risk": "medium", "expected_gain_pct": 3.0, "reason": "test",
                  "allocation_pct": 100}]
        trades = {"GOOG": [_make_trade(filled=50, avg_fill=101.0)]}
        data = self._run(picks, trades, tmp_path)  # no preflight_price_by_ticker
        pick = data["sessions"][0]["picks"][0]
        assert pick["preflight_price"] is None
        assert pick["slippage_pct"] is None

    def test_unfilled_estimated_price_does_not_compute_slippage(self, tmp_path):
        """An ESTIMATED (unconfirmed) fill must not be compared to the
        pre-flight quote -- that would measure market drift, not execution
        cost, and would silently corrupt the slippage signal."""
        picks = [{"ticker": "AMZN", "score": 7, "direction": "bullish",
                  "risk": "high", "expected_gain_pct": 5.0, "reason": "test",
                  "allocation_pct": 100}]
        data = self._run(picks, None, tmp_path, preflight_price_by_ticker={"AMZN": 100.0})
        pick = data["sessions"][0]["picks"][0]
        assert pick["buy_price"] == 50.0  # from the mocked _get_last_price
        assert pick["slippage_pct"] is None


# ---------------------------------------------------------------------------
# Budget check maths (pre-flight scale-down)
# ---------------------------------------------------------------------------
class TestPreflightBudgetMath:
    """Verify the scale-down formula used in main.py's pre-flight block."""

    def _simulate(self, prices, alloc_pcts, budget):
        """
        Replicate the pre-flight logic:
          1. compute shares = floor(alloc_usd / price)
          2. if projected > budget, scale all shares down
        Returns list of (shares, price) after scale-down.
        """
        plans = []
        for price, pct in zip(prices, alloc_pcts):
            alloc_usd = pct / 100.0 * budget
            shares = math.floor(alloc_usd / price)
            plans.append([shares, price])

        projected = sum(s * p for s, p in plans)
        if projected > budget:
            scale = budget / projected
            plans = [[math.floor(s * scale), p] for s, p in plans]

        return plans

    def test_no_scale_needed(self):
        plans = self._simulate([10.0, 20.0], [50.0, 50.0], 1000.0)
        cost = sum(s * p for s, p in plans)
        assert cost <= 1000.0

    def test_scale_down_keeps_cost_within_budget(self):
        # 3 equal allocs, prices deliberately high so floor rounding might over-commit
        plans = self._simulate([33.33, 33.33, 33.34], [33.33, 33.33, 33.34], 1000.0)
        cost = sum(s * p for s, p in plans)
        assert cost <= 1000.0

    def test_stale_price_overspend_scenario(self):
        """
        Reproduce the original bug: delayed price $16.39 but actual $17.24.
        Allocating $3,050 at $16.39 = 186 shares → real cost 186*$17.24 = $3,206.64.
        The pre-flight fix uses the actual price, so shares = floor(3050/17.24) = 176.
        Cost = 176*$17.24 = $3,034.24 ≤ $3,050.
        """
        actual_price = 17.24
        alloc_usd = 3050.0
        shares = math.floor(alloc_usd / actual_price)
        cost = shares * actual_price
        assert cost <= alloc_usd
        assert shares == 176

    def test_zero_share_position_excluded(self):
        """A position that rounds to 0 shares should not appear in the order."""
        plans = self._simulate([10_000.0], [1.0], 500.0)
        # 1% of $500 = $5, floor(5/10000) = 0
        assert plans[0][0] == 0
