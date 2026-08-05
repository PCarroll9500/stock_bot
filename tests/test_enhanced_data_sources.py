"""
tests/test_enhanced_data_sources.py

Tests for the enhanced data source modules (earnings/sentiment/SEC/web news)
and their prompt-formatting helpers in catalyst_scorer. These modules scrape
public web pages, so the main correctness requirement is that they fail open
(return an empty, correctly-shaped DataFrame) rather than raising when a
request errors out or a page's structure doesn't match expectations.
"""

import pandas as pd
import pytest
import requests

from stock_bot.ai.catalyst_scorer import (
    _format_earnings_signal,
    _format_sentiment_signal,
    _format_sec_signal,
    _normalize_catalyst_type,
)
from stock_bot.data_sources.earnings_fetcher import fetch_earnings
from stock_bot.data_sources.sentiment_fetcher import fetch_sentiment
from stock_bot.data_sources.sec_fetcher import fetch_sec_filings
from stock_bot.data_sources.web_scraper import scrape_news


class TestFailOpen:
    """Each fetcher must return an empty, correctly-columned DataFrame
    (never raise) when the network call fails."""

    def test_fetch_earnings_fails_open(self, monkeypatch):
        def _raise(*a, **k):
            raise requests.RequestException("network down")
        monkeypatch.setattr(requests, "get", _raise)

        df = fetch_earnings(days_ahead=7)
        assert isinstance(df, pd.DataFrame)
        assert df.empty
        assert list(df.columns) == ["ticker", "earnings_date", "eps_estimate", "time_of_day"]

    def test_fetch_sentiment_fails_open(self, monkeypatch):
        def _raise(*a, **k):
            raise requests.RequestException("network down")
        monkeypatch.setattr(requests, "get", _raise)

        df = fetch_sentiment(["AAPL", "MSFT"], cache_ttl_min=0, max_per_source=2)
        assert isinstance(df, pd.DataFrame)
        # fetch_sentiment fails open per-ticker (returns zeroed rows), not empty-frame
        assert set(df.columns) >= {
            "ticker", "reddit_mentions", "reddit_sentiment",
            "stocktwits_sentiment", "overall_sentiment",
        }
        assert (df["reddit_mentions"] == 0).all()

    def test_fetch_sec_filings_fails_open(self, monkeypatch):
        def _raise(*a, **k):
            raise requests.RequestException("network down")
        monkeypatch.setattr(requests, "get", _raise)

        df = fetch_sec_filings(["AAPL"], days_back=7, max_per_ticker=1)
        assert isinstance(df, pd.DataFrame)
        assert df.empty
        assert list(df.columns) == ["ticker", "form_type", "filing_date", "summary", "significance"]

    def test_scrape_news_fails_open(self, monkeypatch):
        def _raise(*a, **k):
            raise requests.RequestException("network down")
        monkeypatch.setattr(requests, "get", _raise)

        df = scrape_news(["AAPL"], max_per_ticker=1)
        assert isinstance(df, pd.DataFrame)
        assert df.empty
        assert list(df.columns) == ["ticker", "title", "source", "url", "publish_date", "sentiment"]


class TestSignalFormatting:
    """catalyst_scorer's prompt-formatting helpers must handle missing data
    gracefully (None / empty) since most tickers won't have every signal."""

    def test_earnings_signal_none(self):
        assert _format_earnings_signal(None) == "No upcoming earnings data."

    def test_earnings_signal_no_date(self):
        assert _format_earnings_signal({}) == "No upcoming earnings data."

    def test_earnings_signal_with_estimate(self):
        result = _format_earnings_signal(
            {"earnings_date": "2026-08-05", "eps_estimate": 1.23, "time_of_day": "pre-market"}
        )
        assert "2026-08-05" in result
        assert "1.23" in result
        assert "pre-market" in result

    def test_earnings_signal_no_estimate(self):
        result = _format_earnings_signal(
            {"earnings_date": "2026-08-05", "eps_estimate": None, "time_of_day": "unknown"}
        )
        assert "N/A" in result

    def test_sentiment_signal_none(self):
        assert _format_sentiment_signal(None) == "No social sentiment data available."

    def test_sentiment_signal_empty(self):
        assert _format_sentiment_signal({}) == "No social sentiment data available."

    def test_sentiment_signal_with_data(self):
        result = _format_sentiment_signal(
            {"reddit_mentions": 5, "reddit_sentiment": 0.4, "stocktwits_sentiment": -0.2}
        )
        assert "Reddit" in result and "5 mentions" in result
        assert "StockTwits" in result

    def test_sentiment_signal_reddit_only(self):
        result = _format_sentiment_signal(
            {"reddit_mentions": 3, "reddit_sentiment": 0.1, "stocktwits_sentiment": 0.0}
        )
        assert "Reddit" in result
        assert "StockTwits" not in result

    def test_sec_signal_none(self):
        assert _format_sec_signal(None) == "No recent SEC filings or insider activity."

    def test_sec_signal_empty(self):
        assert _format_sec_signal([]) == "No recent SEC filings or insider activity."

    def test_sec_signal_with_filings(self):
        result = _format_sec_signal([
            {"form_type": "8-K", "significance": "high", "summary": "Acquisition announced"},
            {"form_type": "4", "significance": "medium", "summary": "Insider buy"},
        ])
        assert "8-K" in result
        assert "Acquisition announced" in result

    def test_sec_signal_caps_at_three(self):
        filings = [
            {"form_type": f"F{i}", "significance": "low", "summary": f"filing {i}"}
            for i in range(5)
        ]
        result = _format_sec_signal(filings)
        assert result.count("filing") == 3


class TestCatalystTypeNormalization:
    """catalyst_type feeds the score-vs-returns feedback loop analysis, so an
    unrecognized or missing value from GPT must fall back to 'other' rather
    than polluting the dataset with arbitrary free-text values."""

    def test_known_value_passes_through(self):
        assert _normalize_catalyst_type("earnings_beat") == "earnings_beat"

    def test_case_insensitive(self):
        assert _normalize_catalyst_type("Earnings_Beat") == "earnings_beat"
        assert _normalize_catalyst_type("FDA_APPROVAL") == "fda_approval"

    def test_whitespace_stripped(self):
        assert _normalize_catalyst_type("  ma_acquisition  ") == "ma_acquisition"

    def test_unknown_value_falls_back_to_other(self):
        assert _normalize_catalyst_type("stock_split") == "other"
        assert _normalize_catalyst_type("random gibberish") == "other"

    def test_none_falls_back_to_other(self):
        assert _normalize_catalyst_type(None) == "other"

    def test_empty_string_falls_back_to_other(self):
        assert _normalize_catalyst_type("") == "other"

    def test_all_documented_types_are_valid(self):
        for t in [
            "earnings_beat", "ma_acquisition", "fda_approval", "insider_buying",
            "analyst_action", "contract_win", "sector_tailwind", "short_squeeze",
            "guidance_raise", "other",
        ]:
            assert _normalize_catalyst_type(t) == t
