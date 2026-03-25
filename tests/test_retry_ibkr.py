"""
tests/test_retry_ibkr.py

Unit tests for close_of_day._retry_ibkr — retry/reconnect logic.
"""

import sys
import time
import types
from unittest.mock import MagicMock, call, patch

import pytest

# We import the function directly without running the module's top-level code
# by importing after patching the ib_insync dependency.
import importlib


def _load_retry_ibkr():
    """Import _retry_ibkr from close_of_day, patching heavy dependencies."""
    # Stub out modules that require ib_insync installed to import
    stubs = {
        "ib_insync": MagicMock(),
        "stock_bot.core.logging_config": MagicMock(),
        "stock_bot.brokers.ib.connect_disconnect": MagicMock(),
        "stock_bot.brokers.ib.sell_all": MagicMock(),
        "stock_bot.data_sources.portfolio_writer": MagicMock(),
        "stock_bot.config.settings": MagicMock(),
    }
    with patch.dict("sys.modules", stubs):
        import importlib.util, pathlib
        spec = importlib.util.spec_from_file_location(
            "close_of_day",
            pathlib.Path(__file__).parents[1] / "scripts" / "close_of_day.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod._retry_ibkr


class TestRetryIbkr:
    @pytest.fixture(autouse=True)
    def _fn(self):
        self._retry_ibkr = _load_retry_ibkr()

    def _make_ib(self, connected=True):
        ib = MagicMock()
        ib.isConnected.return_value = connected
        return ib

    def test_returns_immediately_on_first_success(self):
        fn = MagicMock(return_value=42.0)
        ib = self._make_ib()
        logger = MagicMock()
        result, returned_ib = self._retry_ibkr(fn, "test", ib, MagicMock(), logger)
        assert result == 42.0
        assert returned_ib is ib
        fn.assert_called_once_with(ib)

    def test_retries_until_success(self):
        ib = self._make_ib()
        logger = MagicMock()
        # Fail twice, succeed on third call
        fn = MagicMock(side_effect=[None, None, 99.5])
        with patch("time.sleep"), patch("time.monotonic", side_effect=[0, 1, 2, 3, 4, 5]):
            result, _ = self._retry_ibkr(fn, "test", ib, MagicMock(), logger, interval=1, timeout=600)
        assert result == 99.5
        assert fn.call_count == 3

    def test_returns_none_after_timeout(self):
        ib = self._make_ib()
        logger = MagicMock()
        fn = MagicMock(return_value=None)
        # monotonic: initial deadline=0+5=5; first call remaining=5-1=4>0; second remaining=5-10=-5≤0
        with patch("time.sleep"), patch("time.monotonic", side_effect=[0, 1, 10]):
            result, _ = self._retry_ibkr(fn, "test", ib, MagicMock(), logger, interval=1, timeout=5)
        assert result is None
        logger.error.assert_called_once()

    def test_reconnects_when_disconnected(self):
        ib_disconnected = self._make_ib(connected=False)
        new_ib = self._make_ib(connected=True)
        connect_fn = MagicMock(return_value=new_ib)
        logger = MagicMock()
        # First call returns None (triggers retry), second call succeeds with new_ib
        fn = MagicMock(side_effect=[None, 77.0])
        with patch("time.sleep"), patch("time.monotonic", side_effect=[0, 1, 2, 3, 4]):
            result, returned_ib = self._retry_ibkr(
                fn, "test", ib_disconnected, connect_fn, logger, interval=1, timeout=600
            )
        assert result == 77.0
        connect_fn.assert_called_once()
        # After reconnect the new ib should be used
        assert returned_ib is new_ib

    def test_returns_updated_ib_after_reconnect(self):
        """Callers must unpack (result, ib) — verify the returned ib is the new one."""
        old_ib = self._make_ib(connected=False)
        fresh_ib = self._make_ib(connected=True)
        connect_fn = MagicMock(return_value=fresh_ib)
        logger = MagicMock()
        fn = MagicMock(side_effect=[None, 55.5])
        with patch("time.sleep"), patch("time.monotonic", side_effect=[0, 1, 2, 3]):
            _, returned_ib = self._retry_ibkr(
                fn, "test", old_ib, connect_fn, logger, interval=1, timeout=600
            )
        assert returned_ib is fresh_ib

    def test_continues_if_reconnect_fails(self):
        """If reconnect throws, retrying should still proceed."""
        ib = self._make_ib(connected=False)
        connect_fn = MagicMock(side_effect=ConnectionError("no gateway"))
        logger = MagicMock()
        fn = MagicMock(side_effect=[None, 33.0])
        with patch("time.sleep"), patch("time.monotonic", side_effect=[0, 1, 2, 3]):
            result, _ = self._retry_ibkr(
                fn, "test", ib, connect_fn, logger, interval=1, timeout=600
            )
        assert result == 33.0
        logger.warning.assert_any_call("close_of_day: reconnect failed — will retry anyway")
