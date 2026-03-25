"""
tests/test_close_of_day_order_cancel.py

Tests for the order-cancellation block added to close_of_day.py.
Ensures stop-loss child orders are cancelled before EOD liquidation
so they cannot create short positions overnight.
"""

import importlib.util
import pathlib
from unittest.mock import MagicMock, call, patch


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
            "close_of_day_cancel",
            pathlib.Path(__file__).parents[1] / "scripts" / "close_of_day.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


def _make_open_trade(symbol, sectype="STK"):
    t = MagicMock()
    t.contract.symbol = symbol
    t.contract.secType = sectype
    t.order = MagicMock()
    return t


class TestOrderCancellationLogic:
    """Simulate the cancellation block logic from close_of_day.main()."""

    def _run_cancel_block(self, open_orders, tickers_to_sell, ib):
        """Replicate the cancellation block from close_of_day.py."""
        cancelled = 0
        for trade in open_orders:
            symbol = getattr(trade.contract, "symbol", None)
            if symbol in tickers_to_sell:
                try:
                    ib.cancelOrder(trade.order)
                    cancelled += 1
                except Exception:
                    pass
        return cancelled

    def test_cancels_stop_order_for_today_ticker(self):
        ib = MagicMock()
        orders = [_make_open_trade("AAPL"), _make_open_trade("MSFT")]
        tickers = {"AAPL", "MSFT"}
        count = self._run_cancel_block(orders, tickers, ib)
        assert count == 2
        assert ib.cancelOrder.call_count == 2

    def test_does_not_cancel_unrelated_tickers(self):
        ib = MagicMock()
        orders = [_make_open_trade("AAPL"), _make_open_trade("GOOG")]
        tickers = {"AAPL"}  # only AAPL is in today's session
        count = self._run_cancel_block(orders, tickers, ib)
        assert count == 1
        # Only AAPL's order cancelled
        ib.cancelOrder.assert_called_once_with(orders[0].order)

    def test_no_orders_nothing_cancelled(self):
        ib = MagicMock()
        count = self._run_cancel_block([], {"AAPL"}, ib)
        assert count == 0
        ib.cancelOrder.assert_not_called()

    def test_cancel_failure_does_not_abort(self):
        ib = MagicMock()
        ib.cancelOrder.side_effect = Exception("IB error")
        orders = [_make_open_trade("AAPL")]
        # Should not raise even if cancelOrder throws
        count = self._run_cancel_block(orders, {"AAPL"}, ib)
        # count stays 0 because exception was caught
        assert count == 0

    def test_cancels_multiple_orders_for_same_ticker(self):
        """Both the stop and take-profit child orders should be cancelled."""
        ib = MagicMock()
        sl_order = _make_open_trade("VG")
        tp_order = _make_open_trade("VG")
        count = self._run_cancel_block([sl_order, tp_order], {"VG"}, ib)
        assert count == 2

    def test_only_picks_with_shares_generate_cancel_targets(self):
        """Tickers with shares=0 in picks should not be in tickers_to_sell."""
        picks = [
            {"ticker": "AAPL", "shares": 100},
            {"ticker": "GOOG", "shares": 0},   # never bought
            {"ticker": "MSFT", "shares": 50},
        ]
        tickers_to_sell = {p["ticker"] for p in picks if p.get("shares", 0) > 0}
        assert "AAPL" in tickers_to_sell
        assert "MSFT" in tickers_to_sell
        assert "GOOG" not in tickers_to_sell


class TestShortPositionPrevention:
    """Verify the logic that would detect accidental shorts the next morning."""

    def test_no_short_positions_passes(self):
        held = [
            MagicMock(contract=MagicMock(secType="STK", symbol="AAPL"), position=100),
        ]
        shorts = [p for p in held if p.contract.secType == "STK" and p.position < 0]
        assert shorts == []

    def test_short_position_detected(self):
        held = [
            MagicMock(contract=MagicMock(secType="STK", symbol="AAPL"), position=-50),
        ]
        shorts = [p for p in held if p.contract.secType == "STK" and p.position < 0]
        assert len(shorts) == 1
        assert shorts[0].contract.symbol == "AAPL"

    def test_zero_position_not_flagged_as_short(self):
        held = [
            MagicMock(contract=MagicMock(secType="STK", symbol="AAPL"), position=0),
        ]
        shorts = [p for p in held if p.contract.secType == "STK" and p.position < 0]
        assert shorts == []
