"""
tests/test_trailing_stop_limit.py

Covers _trail_sell_order() in buy_stocks.py: the TRAIL LIMIT order built when
trailing_stop_limit_offset_pct is configured, versus the plain TRAIL order
built when it isn't.

Background: a plain TRAIL order becomes a market order the instant it
triggers. Live data on 2026-08-06 showed trailing-stop exits filling well
past their configured trail width (CW: 2-3% trail, -5.56% actual exit) because
of that market-order conversion during a fast decline. TRAIL LIMIT caps that
by pinning the resulting sell to a limit price a configurable offset below
the trigger.
"""

import json
from pathlib import Path

from stock_bot.brokers.ib.buy_stocks import _trail_sell_order

CONFIG_PATH = Path(__file__).parents[1] / "src" / "stock_bot" / "config" / "picker_config.json"


def _config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


class TestTrailSellOrderPlain:
    def test_no_offset_builds_plain_trail(self):
        order = _trail_sell_order(100, 2.5, 50.0)
        assert order.orderType == "TRAIL"
        assert order.trailingPercent == 2.5
        assert order.action == "SELL"
        assert order.totalQuantity == 100

    def test_plain_trail_type_unchanged_from_pre_fix_behavior(self):
        """No offset configured -> falls back to the original plain TRAIL
        (market-on-trigger) order, so existing behavior is preserved when
        trailing_stop_limit_offset_pct is left null."""
        order = _trail_sell_order(100, 2.5, 50.0)
        assert order.orderType == "TRAIL"


class TestTrailSellOrderLimit:
    def test_offset_builds_trail_limit(self):
        order = _trail_sell_order(100, 2.5, 50.0, limit_offset_pct=1.0)
        assert order.orderType == "TRAIL LIMIT"
        assert order.trailingPercent == 2.5

    def test_trail_stop_price_below_ref(self):
        order = _trail_sell_order(100, 2.5, 50.0, limit_offset_pct=1.0)
        assert order.trailStopPrice == round(50.0 * (1 - 2.5 / 100), 2)
        assert order.trailStopPrice < 50.0

    def test_limit_price_below_trail_stop_price(self):
        """The whole point: limit sits below the trigger so the fill can't
        chase an unbounded distance down in a fast decline."""
        order = _trail_sell_order(100, 2.5, 50.0, limit_offset_pct=1.0)
        assert order.lmtPrice < order.trailStopPrice

    def test_offset_dollars_matches_pct_of_ref_price(self):
        order = _trail_sell_order(100, 2.5, 200.0, limit_offset_pct=1.0)
        assert order.lmtPriceOffset == round(200.0 * 1.0 / 100, 2)

    def test_tiny_offset_still_positive(self):
        """Guards against a zero-dollar limit offset on very low-priced
        stocks, which would make the limit order un-fillable in practice."""
        order = _trail_sell_order(100, 2.5, 1.0, limit_offset_pct=0.001)
        assert order.lmtPriceOffset > 0

    def test_parent_id_attached_when_given(self):
        order = _trail_sell_order(100, 2.5, 50.0, limit_offset_pct=1.0, parent_id=42)
        assert order.parentId == 42

    def test_transmit_default_true(self):
        order = _trail_sell_order(100, 2.5, 50.0)
        assert order.transmit is True

    def test_transmit_false_when_requested(self):
        order = _trail_sell_order(100, 2.5, 50.0, transmit=False)
        assert order.transmit is False


class TestConfigValue:
    def test_trailing_stop_limit_offset_pct_present(self):
        assert _config()["trailing_stop_limit_offset_pct"] == 1.0

    def test_offset_smaller_than_tightest_trail(self):
        """The limit offset should be tighter than the loosest trail width so
        it meaningfully caps slippage rather than just widening the trail."""
        cfg = _config()
        offset = cfg["trailing_stop_limit_offset_pct"]
        tightest_trail = min(
            v for k, v in cfg["trailing_stop_by_risk"].items() if k != "enabled"
        )
        assert offset <= tightest_trail
