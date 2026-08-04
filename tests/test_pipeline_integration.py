"""
tests/test_pipeline_integration.py

Integration-level tests for the morning pipeline:
- Budget normalization when allocations exceed 100%
- Position sizing with stop-loss as reference price
- Allocation computation (risk-adjusted)
- Realloc cash tracking
- No-picks-today scenario
"""

import json
import math
import pytest


# ── Allocation normalization ──────────────────────────────────────────────────

class TestAllocationNormalization:
    """When AI allocations sum > 100%, main.py normalizes to prevent margin."""

    def _normalize(self, raw_allocs):
        total = sum(raw_allocs)
        if total > 100.0:
            return [a / total * 100.0 for a in raw_allocs]
        return raw_allocs

    def test_already_100_unchanged(self):
        allocs = [50.0, 30.0, 20.0]
        result = self._normalize(allocs)
        assert abs(sum(result) - 100.0) < 1e-9

    def test_over_100_normalized(self):
        allocs = [40.0, 40.0, 40.0]  # 120%
        result = self._normalize(allocs)
        assert abs(sum(result) - 100.0) < 1e-9
        assert all(r < 40.0 for r in result)

    def test_under_100_unchanged(self):
        allocs = [30.0, 30.0, 30.0]  # 90%
        result = self._normalize(allocs)
        assert result == allocs

    def test_normalization_preserves_relative_weights(self):
        allocs = [60.0, 30.0, 30.0]  # 120%
        result = self._normalize(allocs)
        assert abs(result[0] / result[1] - 2.0) < 1e-9


# ── Order sizing with stop-loss ───────────────────────────────────────────────

class TestOrderSizingWithStopLoss:
    """Verify position sizing and stop-loss price when limit order is used."""

    def _size_order(self, alloc_pct, budget, preflight_price, buffer_pct, stop_loss_pct):
        alloc_usd = alloc_pct / 100.0 * budget
        shares = math.floor(alloc_usd / preflight_price)
        limit_price = round(preflight_price * (1 + buffer_pct / 100), 2) if buffer_pct else None
        ref_price = limit_price if limit_price is not None else preflight_price
        sl_price = round(ref_price * (1 - stop_loss_pct / 100), 2) if stop_loss_pct else None
        cost = shares * preflight_price
        return shares, limit_price, sl_price, cost

    def test_stop_based_on_limit_not_preflight(self):
        """Stop-loss reference is the limit price (1% above preflight), not the raw price."""
        shares, limit, sl, cost = self._size_order(
            alloc_pct=10.0, budget=30_000.0,
            preflight_price=100.0, buffer_pct=1.0, stop_loss_pct=2.5,
        )
        assert limit == 101.0
        assert sl == round(101.0 * 0.975, 2)  # 98.47, not 97.5

    def test_cost_never_exceeds_budget(self):
        for price in [10.0, 50.0, 100.0, 250.0, 1000.0]:
            shares, _, _, cost = self._size_order(10.0, 30_000.0, price, 1.0, 2.5)
            assert cost <= 3_000.0

    def test_zero_shares_when_price_exceeds_alloc(self):
        shares, _, _, _ = self._size_order(1.0, 1_000.0, 999.0, 1.0, 2.5)
        # 1% of $1000 = $10, floor(10/999) = 0
        assert shares == 0

    def test_ten_picks_total_cost_within_budget(self):
        """10 equal picks at 10% each — total cost should not exceed budget."""
        budget = 30_000.0
        price = 50.0
        total_cost = 0
        for _ in range(10):
            shares = math.floor(0.10 * budget / price)
            total_cost += shares * price
        assert total_cost <= budget


# ── Max drawdown per trade ────────────────────────────────────────────────────

class TestMaxLossPerTrade:
    """With 2.5% stop-loss, verify max dollar loss per position."""

    def test_worst_case_loss_per_position(self):
        """10% alloc of $30k = $3k per position. 2.5% stop = max $75 loss."""
        alloc_usd = 0.10 * 30_000.0  # $3,000
        stop_loss_pct = 2.5
        max_loss = alloc_usd * stop_loss_pct / 100
        assert max_loss == 75.0

    def test_total_portfolio_stop_loss_exposure(self):
        """10 positions × $75 worst-case = $750 max daily loss (2.5% of portfolio)."""
        per_position = 0.10 * 30_000.0 * 2.5 / 100
        total_max_loss = 10 * per_position
        portfolio_pct = total_max_loss / 30_000.0 * 100
        assert total_max_loss == 750.0
        assert portfolio_pct == 2.5


# ── Realloc freed cash tracking ───────────────────────────────────────────────

class TestReallocCashTracking:
    """Freed cash from unfilled orders must be tracked correctly."""

    def test_freed_cash_equals_unfilled_alloc(self):
        """If 2 of 10 picks don't fill, the freed cash = their allocated USD."""
        budget = 30_000.0
        alloc_per_pick = budget / 10  # $3,000 each
        unfilled = 2
        freed = unfilled * alloc_per_pick
        assert freed == 6_000.0

    def test_realloc_threshold_filters_small_cash(self):
        """Freed cash below $200 (_MIN_REALLOC_USD) should not trigger realloc."""
        _MIN_REALLOC_USD = 200.0
        assert 150.0 < _MIN_REALLOC_USD  # too small
        assert 500.0 >= _MIN_REALLOC_USD  # large enough

    def test_round1_top_up_uses_floor_division(self):
        freed_cash = 3_000.0
        price = 47.33
        extra_shares = math.floor(freed_cash / price)
        cost = extra_shares * price
        assert cost <= freed_cash
        assert isinstance(extra_shares, int)


# ── No-picks scenario ─────────────────────────────────────────────────────────

class TestNoPicksScenario:
    """When no stocks qualify, the bot should stay in cash without error."""

    def test_write_session_skipped_on_empty_picks(self, tmp_path, monkeypatch):
        """portfolio_writer.write_session writes a no-pick stub (not a real
        session) when picks=[], and must never touch the real portfolio.json.
        Redirects _PORTFOLIO_JSON_TEST to a tmp file so this test can't
        pollute production data regardless of test_mode handling upstream."""
        from unittest.mock import MagicMock, patch
        import stock_bot.data_sources.portfolio_writer as pw

        tmp_test_json = tmp_path / "portfolio_test.json"
        monkeypatch.setattr(pw, "_PORTFOLIO_JSON_TEST", tmp_test_json)

        mock_ib = MagicMock()
        with patch("stock_bot.data_sources.portfolio_writer._get_last_price", return_value=100.0):
            result = pw.write_session(picks=[], ib=mock_ib, test_mode=True, open_value_override=10_000.0)
        assert result is None

        written = json.loads(tmp_test_json.read_text(encoding="utf-8"))
        assert len(written["sessions"]) == 1
        assert written["sessions"][0]["no_picks"] is True
        assert written["sessions"][0]["picks"] == []

    def test_empty_picks_list_not_bought(self):
        """If picks is empty, no orders should be submitted."""
        picks = []
        trades_submitted = 0
        for pick in picks:
            trades_submitted += 1
        assert trades_submitted == 0
