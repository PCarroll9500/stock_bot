"""
tests/test_hard_cancel_buy_only.py

Tests for the hard-cancel logic at the end of main.py's realloc phase.
Key invariant: only unfilled BUY orders are cancelled — SELL orders
(trailing stops, stop-losses) must be left alive on IBKR.
"""

from unittest.mock import MagicMock, call


def _make_trade(action: str, status: str, filled: float = 0.0) -> MagicMock:
    """Build a minimal Trade mock with the given action / order status."""
    trade = MagicMock()
    trade.order.action = action
    trade.orderStatus.status = status
    trade.orderStatus.filled = filled
    return trade


def _run_hard_cancel(trades_by_ticker: dict, ib: MagicMock) -> list[str]:
    """Replicate the hard-cancel loop from main.py and return cancelled tickers."""
    cancelled = []
    for _ticker, _trade_list in trades_by_ticker.items():
        for _t in (_trade_list or []):
            _status = getattr(_t, "orderStatus", None)
            _action = getattr(_t.order, "action", "") if getattr(_t, "order", None) else ""
            if (_action == "BUY" and _status and _status.filled == 0
                    and _status.status not in ("Filled", "Cancelled", "Inactive")):
                ib.cancelOrder(_t.order)
                cancelled.append(_ticker)
    return cancelled


class TestHardCancelBuyOnly:
    """The hard cancel must never touch SELL (stop-loss / trailing stop) orders."""

    def test_unfilled_buy_is_cancelled(self):
        ib = MagicMock()
        buy = _make_trade("BUY", "Submitted", filled=0)
        _run_hard_cancel({"AAPL": [buy]}, ib)
        ib.cancelOrder.assert_called_once_with(buy.order)

    def test_sell_trailing_stop_is_not_cancelled(self):
        ib = MagicMock()
        sell = _make_trade("SELL", "PreSubmitted", filled=0)
        _run_hard_cancel({"AAPL": [sell]}, ib)
        ib.cancelOrder.assert_not_called()

    def test_sell_stop_order_with_trigger_held_is_not_cancelled(self):
        """Trailing stop in whyHeld='trigger' (PreSubmitted) must survive."""
        ib = MagicMock()
        stop = _make_trade("SELL", "PreSubmitted", filled=0)
        _run_hard_cancel({"TSEM": [stop]}, ib)
        ib.cancelOrder.assert_not_called()

    def test_filled_buy_is_not_cancelled(self):
        ib = MagicMock()
        buy = _make_trade("BUY", "Filled", filled=100)
        _run_hard_cancel({"DXYZ": [buy]}, ib)
        ib.cancelOrder.assert_not_called()

    def test_already_cancelled_buy_is_skipped(self):
        ib = MagicMock()
        buy = _make_trade("BUY", "Cancelled", filled=0)
        _run_hard_cancel({"ARM": [buy]}, ib)
        ib.cancelOrder.assert_not_called()

    def test_mixed_orders_only_buy_cancelled(self):
        """One unfilled buy + one trailing stop sell — only the buy is cancelled."""
        ib = MagicMock()
        buy = _make_trade("BUY", "Submitted", filled=0)
        sell = _make_trade("SELL", "PreSubmitted", filled=0)
        cancelled = _run_hard_cancel({"TERN": [buy, sell]}, ib)
        assert cancelled == ["TERN"]
        ib.cancelOrder.assert_called_once_with(buy.order)

    def test_multiple_tickers_only_unfilled_buys_cancelled(self):
        """Five tickers each with a buy + trailing stop; only 2 buys are unfilled."""
        ib = MagicMock()
        tickers = ["TERN", "DXYZ", "ARM", "TSEM", "DOW"]
        trades = {}
        for i, t in enumerate(tickers):
            buy_status = "Submitted" if i < 2 else "Filled"
            buy = _make_trade("BUY", buy_status, filled=0 if i < 2 else 100)
            sell = _make_trade("SELL", "PreSubmitted", filled=0)
            trades[t] = [buy, sell]

        cancelled = _run_hard_cancel(trades, ib)
        assert cancelled == ["TERN", "DXYZ"]
        assert ib.cancelOrder.call_count == 2

    def test_pending_cancel_buy_is_cancelled(self):
        """A BUY already in PendingCancel state (not yet acked by IBKR) is still cancelled."""
        ib = MagicMock()
        buy = _make_trade("BUY", "PendingCancel", filled=0)
        cancelled = _run_hard_cancel({"AAPL": [buy]}, ib)
        assert cancelled == ["AAPL"]

    def test_inactive_buy_is_skipped(self):
        ib = MagicMock()
        buy = _make_trade("BUY", "Inactive", filled=0)
        _run_hard_cancel({"AAPL": [buy]}, ib)
        ib.cancelOrder.assert_not_called()

    def test_empty_trade_list_no_error(self):
        ib = MagicMock()
        _run_hard_cancel({"AAPL": []}, ib)
        ib.cancelOrder.assert_not_called()

    def test_none_trade_list_no_error(self):
        ib = MagicMock()
        _run_hard_cancel({"AAPL": None}, ib)
        ib.cancelOrder.assert_not_called()
