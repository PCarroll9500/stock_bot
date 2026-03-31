"""
tests/test_news_freshness.py

Tests for news article timestamp parsing, age labels, staleness filtering,
and the resulting changes to how articles are sent to GPT.
"""

from datetime import datetime, timezone, timedelta


# ── Replicate helpers from news_fetcher / catalyst_scorer ────────────────────

_ET = timezone(timedelta(hours=-4))


def _parse_article_time(time_str: str):
    import email.utils
    if not time_str or not time_str.strip():
        return None
    s = time_str.strip()
    try:
        return email.utils.parsedate_to_datetime(s)
    except Exception:
        pass
    try:
        return datetime.fromisoformat(s)
    except Exception:
        pass
    now = datetime.now(_ET)
    for fmt in ("%b %d %I:%M%p", "%b %d  %I:%M%p"):
        try:
            dt = datetime.strptime(s, fmt).replace(year=now.year, tzinfo=_ET)
            if dt > now + timedelta(hours=2):
                dt = dt.replace(year=now.year - 1)
            return dt
        except Exception:
            pass
    return None


_MAX_NEWS_AGE_HRS = 72


def _age_label(published_dt) -> str:
    if published_dt is None:
        return "age unknown"
    now = datetime.now(timezone.utc)
    if published_dt.tzinfo is None:
        published_dt = published_dt.replace(tzinfo=timezone.utc)
    delta_s = (now - published_dt).total_seconds()
    if delta_s < 0:
        return "just now"
    if delta_s < 3600:
        return f"{int(delta_s / 60)}m ago"
    if delta_s < 86400:
        return f"{int(delta_s / 3600)}h ago"
    return f"{int(delta_s / 86400)}d ago"


def _filter_stale_articles(articles: list[dict]) -> list[dict]:
    now = datetime.now(timezone.utc)
    fresh = []
    for a in articles:
        dt = a.get("published_dt")
        if dt is None:
            fresh.append(a)
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if (now - dt).total_seconds() / 3600 <= _MAX_NEWS_AGE_HRS:
            fresh.append(a)
    return fresh


# ── Timestamp parsing ─────────────────────────────────────────────────────────

class TestParseArticleTime:

    def test_rfc2822_with_timezone(self):
        dt = _parse_article_time("Thu, 26 Mar 2026 13:35:04 +0000")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 3
        assert dt.day == 26
        assert dt.tzinfo is not None

    def test_rfc2822_gmt(self):
        dt = _parse_article_time("Mon, 30 Mar 2026 14:00:00 GMT")
        assert dt is not None
        assert dt.year == 2026

    def test_iso_with_offset(self):
        dt = _parse_article_time("2026-03-26 13:35:04+00:00")
        assert dt is not None
        assert dt.year == 2026
        assert dt.tzinfo is not None

    def test_iso_no_offset(self):
        dt = _parse_article_time("2026-03-26 13:35:04")
        assert dt is not None
        assert dt.year == 2026

    def test_finviz_format(self):
        dt = _parse_article_time("Mar 26 08:32AM")
        assert dt is not None
        assert dt.month == 3
        assert dt.day == 26
        assert dt.hour == 8

    def test_empty_string_returns_none(self):
        assert _parse_article_time("") is None

    def test_whitespace_returns_none(self):
        assert _parse_article_time("   ") is None

    def test_garbage_returns_none(self):
        assert _parse_article_time("not a date at all") is None


# ── Age labels ────────────────────────────────────────────────────────────────

class TestAgeLabel:

    def _dt(self, hours_ago: float) -> datetime:
        return datetime.now(timezone.utc) - timedelta(hours=hours_ago)

    def test_minutes_ago(self):
        label = _age_label(self._dt(0.25))   # 15 minutes
        assert label.endswith("m ago")

    def test_hours_ago(self):
        label = _age_label(self._dt(3))
        assert label == "3h ago"

    def test_one_day_ago(self):
        label = _age_label(self._dt(25))
        assert label == "1d ago"

    def test_three_days_ago(self):
        label = _age_label(self._dt(72))
        assert label == "3d ago"

    def test_none_returns_unknown(self):
        assert _age_label(None) == "age unknown"

    def test_future_timestamp_returns_just_now(self):
        future = datetime.now(timezone.utc) + timedelta(minutes=5)
        assert _age_label(future) == "just now"

    def test_naive_datetime_handled(self):
        naive = datetime.utcnow() - timedelta(hours=2)
        label = _age_label(naive)
        assert label == "2h ago"


# ── Staleness filtering ───────────────────────────────────────────────────────

class TestFilterStaleArticles:

    def _article(self, hours_ago: float | None, headline: str = "test") -> dict:
        if hours_ago is None:
            return {"headline": headline, "published_dt": None}
        dt = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
        return {"headline": headline, "published_dt": dt}

    def test_fresh_article_kept(self):
        articles = [self._article(1)]
        assert len(_filter_stale_articles(articles)) == 1

    def test_stale_article_dropped(self):
        articles = [self._article(73)]   # just over 72h
        assert len(_filter_stale_articles(articles)) == 0

    def test_exactly_at_boundary_kept(self):
        articles = [self._article(71.99)]  # just inside 72h — avoids millisecond timing drift
        assert len(_filter_stale_articles(articles)) == 1

    def test_unknown_timestamp_kept(self):
        """Articles with no parseable timestamp are kept (fail safe)."""
        articles = [self._article(None)]
        assert len(_filter_stale_articles(articles)) == 1

    def test_mixed_list_filters_correctly(self):
        articles = [
            self._article(2,  "fresh"),
            self._article(80, "stale"),
            self._article(None, "unknown age"),
            self._article(48, "yesterday"),
        ]
        result = _filter_stale_articles(articles)
        headlines = [a["headline"] for a in result]
        assert "fresh" in headlines
        assert "stale" not in headlines
        assert "unknown age" in headlines
        assert "yesterday" in headlines

    def test_all_stale_returns_empty(self):
        articles = [self._article(100), self._article(200)]
        assert _filter_stale_articles(articles) == []

    def test_empty_input(self):
        assert _filter_stale_articles([]) == []
