"""
tests/test_backtest_prompt.py

Unit tests for scripts/backtest_prompt.py -- the offline harness that
replays logged LLM inputs through the current prompt/config. All tests use
a stubbed score_candidates_fn (or --dry-run) so no OpenAI API key or network
access is required.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from backtest_prompt import (
    _parse_article_dates,
    iter_logged_inputs,
    load_original_output,
    load_realized_outcomes,
    print_report,
    rescore_one,
    run_backtest,
)


def _write_input(log_dir: Path, date: str, ticker: str, **overrides) -> None:
    date_dir = log_dir / date
    date_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "ticker": ticker,
        "current_price": 10.0,
        "news": [{"headline": "test", "provider": "x", "published_dt": "2026-07-01T08:00:00+00:00", "body": ""}],
        "trends": "1d: +2.0%",
        "earnings": {},
        "sentiment": {},
        "sec_filings": [],
        "web_news": [],
        "gpt_prompt_sent": "prompt text",
    }
    data.update(overrides)
    (date_dir / f"{ticker}_input.json").write_text(json.dumps(data), encoding="utf-8")


def _write_output(log_dir: Path, date: str, ticker: str, gpt_response: dict) -> None:
    date_dir = log_dir / date
    date_dir.mkdir(parents=True, exist_ok=True)
    data = {"ticker": ticker, "gpt_response": gpt_response, "reasoning": gpt_response.get("reason", "")}
    (date_dir / f"{ticker}_output.json").write_text(json.dumps(data), encoding="utf-8")


class TestParseArticleDates:
    def test_parses_iso_string_to_datetime(self):
        articles = [{"headline": "x", "published_dt": "2026-07-01T08:00:00+00:00"}]
        parsed = _parse_article_dates(articles)
        assert isinstance(parsed[0]["published_dt"], datetime)

    def test_naive_string_gets_utc_tzinfo(self):
        articles = [{"headline": "x", "published_dt": "2026-07-01T08:00:00"}]
        parsed = _parse_article_dates(articles)
        assert parsed[0]["published_dt"].tzinfo == timezone.utc

    def test_missing_value_stays_none(self):
        articles = [{"headline": "x", "published_dt": None}]
        parsed = _parse_article_dates(articles)
        assert parsed[0]["published_dt"] is None

    def test_unparseable_value_falls_back_to_none(self):
        articles = [{"headline": "x", "published_dt": "not-a-date"}]
        parsed = _parse_article_dates(articles)
        assert parsed[0]["published_dt"] is None

    def test_original_dict_not_mutated(self):
        original = {"headline": "x", "published_dt": "2026-07-01T08:00:00+00:00"}
        articles = [original]
        _parse_article_dates(articles)
        assert isinstance(original["published_dt"], str)  # unchanged


class TestIterLoggedInputs:
    def test_discovers_all_tickers_and_dates(self, tmp_path):
        log_dir = tmp_path / "llm_inputs"
        _write_input(log_dir, "2026-07-01", "AAA")
        _write_input(log_dir, "2026-07-02", "BBB")
        results = iter_logged_inputs(log_dir)
        assert [(d, t) for d, t, _ in results] == [("2026-07-01", "AAA"), ("2026-07-02", "BBB")]

    def test_filters_by_start_date(self, tmp_path):
        log_dir = tmp_path / "llm_inputs"
        _write_input(log_dir, "2026-07-01", "AAA")
        _write_input(log_dir, "2026-07-02", "BBB")
        results = iter_logged_inputs(log_dir, start_date="2026-07-02")
        assert [t for _, t, _ in results] == ["BBB"]

    def test_filters_by_end_date(self, tmp_path):
        log_dir = tmp_path / "llm_inputs"
        _write_input(log_dir, "2026-07-01", "AAA")
        _write_input(log_dir, "2026-07-02", "BBB")
        results = iter_logged_inputs(log_dir, end_date="2026-07-01")
        assert [t for _, t, _ in results] == ["AAA"]

    def test_missing_log_dir_returns_empty(self, tmp_path):
        assert iter_logged_inputs(tmp_path / "does_not_exist") == []

    def test_corrupt_json_is_skipped(self, tmp_path):
        log_dir = tmp_path / "llm_inputs"
        date_dir = log_dir / "2026-07-01"
        date_dir.mkdir(parents=True)
        (date_dir / "BAD_input.json").write_text("{not json", encoding="utf-8")
        _write_input(log_dir, "2026-07-01", "GOOD")
        results = iter_logged_inputs(log_dir)
        assert [t for _, t, _ in results] == ["GOOD"]

    def test_news_dates_are_parsed(self, tmp_path):
        log_dir = tmp_path / "llm_inputs"
        _write_input(log_dir, "2026-07-01", "AAA")
        _, _, input_dict = iter_logged_inputs(log_dir)[0]
        assert isinstance(input_dict["news"][0]["published_dt"], datetime)


class TestLoadOriginalOutput:
    def test_loads_gpt_response(self, tmp_path):
        log_dir = tmp_path / "llm_inputs"
        _write_output(log_dir, "2026-07-01", "AAA", {"score": 8, "direction": "bullish"})
        result = load_original_output(log_dir, "2026-07-01", "AAA")
        assert result == {"score": 8, "direction": "bullish"}

    def test_missing_file_returns_none(self, tmp_path):
        log_dir = tmp_path / "llm_inputs"
        assert load_original_output(log_dir, "2026-07-01", "NOPE") is None


class TestLoadRealizedOutcomes:
    def test_loads_day_return_pct(self, tmp_path):
        path = tmp_path / "portfolio.json"
        path.write_text(json.dumps({"sessions": [
            {"date": "2026-07-01", "picks": [{"ticker": "AAA", "day_return_pct": 4.1}]},
        ]}), encoding="utf-8")
        outcomes = load_realized_outcomes([path])
        assert outcomes[("2026-07-01", "AAA")] == 4.1

    def test_missing_file_is_skipped(self, tmp_path):
        assert load_realized_outcomes([tmp_path / "nope.json"]) == {}

    def test_open_session_has_none_return(self, tmp_path):
        path = tmp_path / "portfolio.json"
        path.write_text(json.dumps({"sessions": [
            {"date": "2026-07-01", "picks": [{"ticker": "AAA", "day_return_pct": None}]},
        ]}), encoding="utf-8")
        outcomes = load_realized_outcomes([path])
        assert outcomes[("2026-07-01", "AAA")] is None


class TestRescoreOne:
    def test_calls_injected_scoring_fn_and_returns_first_result(self):
        def fake_score_candidates(news_by_ticker, **kwargs):
            ticker = next(iter(news_by_ticker))
            return [{"ticker": ticker, "score": 9, "direction": "bullish"}]

        result = rescore_one("AAA", {"news": [], "trends": "x"}, score_candidates_fn=fake_score_candidates)
        assert result == {"ticker": "AAA", "score": 9, "direction": "bullish"}

    def test_empty_results_returns_empty_dict(self):
        result = rescore_one("AAA", {"news": [], "trends": "x"}, score_candidates_fn=lambda *a, **k: [])
        assert result == {}


class TestRunBacktestDryRun:
    """--dry-run must never call the scoring function -- pure plumbing check."""

    def test_dry_run_leaves_new_score_none(self, tmp_path):
        log_dir = tmp_path / "llm_inputs"
        _write_input(log_dir, "2026-07-01", "AAA")
        _write_output(log_dir, "2026-07-01", "AAA", {"score": 7, "direction": "bullish"})
        rows = run_backtest(log_dir, None, None, [], dry_run=True)
        assert len(rows) == 1
        assert rows[0]["original_score"] == 7
        assert rows[0]["new_score"] is None

    def test_dry_run_never_invokes_score_candidates_fn(self, tmp_path):
        log_dir = tmp_path / "llm_inputs"
        _write_input(log_dir, "2026-07-01", "AAA")

        def _boom(*a, **k):
            raise AssertionError("dry-run must not call the scoring function")

        rows = run_backtest(log_dir, None, None, [], dry_run=True, score_candidates_fn=_boom)
        assert len(rows) == 1


class TestRunBacktestReplay:
    def test_joins_original_new_and_realized(self, tmp_path):
        log_dir = tmp_path / "llm_inputs"
        _write_input(log_dir, "2026-07-01", "AAA")
        _write_output(log_dir, "2026-07-01", "AAA", {"score": 6, "direction": "bearish", "reason": "x"})

        portfolio_path = tmp_path / "portfolio.json"
        portfolio_path.write_text(json.dumps({"sessions": [
            {"date": "2026-07-01", "picks": [{"ticker": "AAA", "day_return_pct": 5.0}]},
        ]}), encoding="utf-8")

        def fake_score_candidates(news_by_ticker, **kwargs):
            ticker = next(iter(news_by_ticker))
            return [{"ticker": ticker, "score": 9, "direction": "bullish"}]

        rows = run_backtest(
            log_dir, None, None, [portfolio_path], dry_run=False,
            score_candidates_fn=fake_score_candidates,
        )
        assert len(rows) == 1
        row = rows[0]
        assert row["original_score"] == 6
        assert row["new_score"] == 9
        assert row["score_delta"] == 3
        assert row["direction_flipped"] is True
        assert row["realized_return_pct"] == 5.0

    def test_no_flip_when_direction_matches(self, tmp_path):
        log_dir = tmp_path / "llm_inputs"
        _write_input(log_dir, "2026-07-01", "AAA")
        _write_output(log_dir, "2026-07-01", "AAA", {"score": 8, "direction": "bullish"})

        def fake_score_candidates(news_by_ticker, **kwargs):
            ticker = next(iter(news_by_ticker))
            return [{"ticker": ticker, "score": 9, "direction": "bullish"}]

        rows = run_backtest(log_dir, None, None, [], dry_run=False, score_candidates_fn=fake_score_candidates)
        assert rows[0]["direction_flipped"] is False

    def test_missing_original_output_still_replays(self, tmp_path):
        """A logged input without a matching *_output.json (e.g. scoring
        crashed that day) must not prevent replay -- original_score is None."""
        log_dir = tmp_path / "llm_inputs"
        _write_input(log_dir, "2026-07-01", "AAA")

        def fake_score_candidates(news_by_ticker, **kwargs):
            ticker = next(iter(news_by_ticker))
            return [{"ticker": ticker, "score": 9, "direction": "bullish"}]

        rows = run_backtest(log_dir, None, None, [], dry_run=False, score_candidates_fn=fake_score_candidates)
        assert rows[0]["original_score"] is None
        assert "score_delta" not in rows[0]  # can't compute a delta without an original


class TestPrintReport:
    def test_empty_rows_does_not_raise(self, capsys):
        print_report([])
        assert "No logged inputs" in capsys.readouterr().out

    def test_reports_flips_and_average_delta(self, capsys):
        rows = [
            {"date": "2026-07-01", "ticker": "AAA", "original_score": 6, "original_direction": "bearish",
             "new_score": 9, "new_direction": "bullish", "realized_return_pct": 5.0,
             "score_delta": 3, "direction_flipped": True},
            {"date": "2026-07-02", "ticker": "BBB", "original_score": 8, "original_direction": "bullish",
             "new_score": 8, "new_direction": "bullish", "realized_return_pct": None,
             "score_delta": 0, "direction_flipped": False},
        ]
        print_report(rows)
        out = capsys.readouterr().out
        assert "1 direction flip" in out
        assert "AAA" in out
        assert "Average score delta" in out
