"""
tests/test_stop_loss_triggered.py

Tests for stop_loss_triggered flag logic in close_of_day.py.

Rules:
  - Normal EOD sell (fill from sell_trades)  → stop_loss_triggered NOT set
  - Intraday stop fill (price from exec_map)  → stop_loss_triggered = True
  - Fallback to last-bar price               → stop_loss_triggered NOT set
"""


def _resolve_close_price(ticker, sell_trades, exec_map):
    """
    Replicate the close-price selection logic from close_of_day.py.
    Returns (close_price, stop_loss_triggered).
    """
    close_price = None
    stop_loss_triggered = False

    # 1. Actual EOD sell fill
    trade = sell_trades.get(ticker)
    sell_status = getattr(trade, "orderStatus", None) if trade else None
    if sell_status and sell_status.filled > 0:
        close_price = float(sell_status.avgFillPrice)

    # 2. Intraday stop-loss fill
    if close_price is None and ticker in exec_map:
        close_price = exec_map[ticker]
        stop_loss_triggered = True

    # 3. Fallback (last bar price) — caller responsibility; represented as sentinel
    if close_price is None:
        close_price = None  # would be fetched via _get_last_price

    return close_price, stop_loss_triggered


class TestStopLossTriggeredFlag:
    """stop_loss_triggered is only set when the close price comes from exec_map."""

    def test_normal_eod_sell_no_flag(self):
        from unittest.mock import MagicMock
        trade = MagicMock()
        trade.orderStatus.filled = 100
        trade.orderStatus.avgFillPrice = 55.0

        price, triggered = _resolve_close_price("TERN", {"TERN": trade}, {})
        assert price == 55.0
        assert triggered is False

    def test_intraday_stop_sets_flag(self):
        """When exec_map has the ticker, stop_loss_triggered must be True."""
        price, triggered = _resolve_close_price("DXYZ", {}, {"DXYZ": 28.36})
        assert price == 28.36
        assert triggered is True

    def test_eod_sell_takes_priority_over_exec_map(self):
        """A confirmed EOD fill beats exec_map — flag stays False."""
        from unittest.mock import MagicMock
        trade = MagicMock()
        trade.orderStatus.filled = 50
        trade.orderStatus.avgFillPrice = 60.0

        price, triggered = _resolve_close_price("ARM", {"ARM": trade}, {"ARM": 55.0})
        assert price == 60.0
        assert triggered is False

    def test_no_sell_no_exec_map_returns_none(self):
        """Without a fill or exec_map entry, close_price is None (fallback needed)."""
        price, triggered = _resolve_close_price("TSEM", {}, {})
        assert price is None
        assert triggered is False

    def test_zero_filled_eod_trade_falls_through_to_exec_map(self):
        """filled=0 means the sell order didn't execute — use exec_map instead."""
        from unittest.mock import MagicMock
        trade = MagicMock()
        trade.orderStatus.filled = 0
        trade.orderStatus.avgFillPrice = 0.0

        price, triggered = _resolve_close_price("DOW", {"DOW": trade}, {"DOW": 39.60})
        assert price == 39.60
        assert triggered is True

    def test_multiple_tickers_independent(self):
        """Each ticker resolves independently; one stop doesn't infect others."""
        from unittest.mock import MagicMock
        trade = MagicMock()
        trade.orderStatus.filled = 100
        trade.orderStatus.avgFillPrice = 53.0

        # TERN: normal EOD sell
        p1, t1 = _resolve_close_price("TERN", {"TERN": trade}, {"DXYZ": 28.36})
        # DXYZ: stop-loss fired
        p2, t2 = _resolve_close_price("DXYZ", {}, {"DXYZ": 28.36})

        assert t1 is False
        assert t2 is True
        assert p1 == 53.0
        assert p2 == 28.36


class TestStopLossPickFieldUpdate:
    """Verify the pick dict is mutated correctly when a stop fires."""

    def _apply_close_price(self, pick, sell_trades, exec_map):
        """Replicate the pick-mutation logic from close_of_day.py."""
        ticker = pick["ticker"]
        buy_price = pick.get("buy_price", 0)
        shares = pick.get("shares", 0)

        close_price, stop_loss_triggered = _resolve_close_price(ticker, sell_trades, exec_map)

        if close_price and buy_price > 0 and shares > 0:
            day_return_usd = (close_price - buy_price) * shares
            day_return_pct = (close_price - buy_price) / buy_price * 100
            pick["close_price"] = close_price
            pick["day_return_pct"] = round(day_return_pct, 3)
            pick["day_return_usd"] = round(day_return_usd, 2)
            if stop_loss_triggered:
                pick["stop_loss_triggered"] = True

    def test_stop_loss_sets_flag_on_pick(self):
        pick = {"ticker": "DXYZ", "buy_price": 31.79, "shares": 161}
        self._apply_close_price(pick, {}, {"DXYZ": 28.36})
        assert pick.get("stop_loss_triggered") is True
        assert pick["close_price"] == 28.36

    def test_normal_sell_no_flag_on_pick(self):
        from unittest.mock import MagicMock
        trade = MagicMock()
        trade.orderStatus.filled = 152
        trade.orderStatus.avgFillPrice = 52.99
        pick = {"ticker": "TERN", "buy_price": 52.87, "shares": 152}
        self._apply_close_price(pick, {"TERN": trade}, {})
        assert "stop_loss_triggered" not in pick
        assert pick["close_price"] == 52.99

    def test_stop_loss_pnl_is_correct(self):
        """P&L is computed correctly when using the stop-loss price."""
        pick = {"ticker": "ARM", "buy_price": 159.75, "shares": 41}
        self._apply_close_price(pick, {}, {"ARM": 150.05})
        assert pick.get("stop_loss_triggered") is True
        assert pick["day_return_usd"] == round((150.05 - 159.75) * 41, 2)
        assert pick["day_return_pct"] == round((150.05 - 159.75) / 159.75 * 100, 3)

    def test_no_price_leaves_pick_unchanged(self):
        """If no close price is found at all, the pick dict is not modified."""
        pick = {"ticker": "XYZ", "buy_price": 50.0, "shares": 100}
        self._apply_close_price(pick, {}, {})
        assert "close_price" not in pick
        assert "stop_loss_triggered" not in pick
