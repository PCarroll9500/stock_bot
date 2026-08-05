# src/stock_bot/ai/catalyst_scorer.py

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from functools import lru_cache
from importlib.resources import files

import openai
from openai import OpenAI

from stock_bot.core.llm_input_logger import log_llm_input, log_llm_output

logger = logging.getLogger(__name__)

MODEL       = "gpt-5.4-mini"
TEMPERATURE = 0.3
MAX_WORKERS = 3

_ALLOC_MIN_PCT    = 5.0
_ALLOC_MAX_PCT    = 35.0
_MAX_NEWS_AGE_HRS = 72  # articles older than this are dropped before scoring

_CATALYST_TYPES = {
    "earnings_beat", "ma_acquisition", "fda_approval", "insider_buying",
    "analyst_action", "contract_win", "sector_tailwind", "short_squeeze",
    "guidance_raise", "other",
}


# ── Prompt template ────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _load_prompt_template() -> str:
    return files("stock_bot.templates").joinpath("catalyst_prompt.txt").read_text(encoding="utf-8")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _age_label(published_dt: datetime | None) -> str:
    """Return a human-readable age string like '2h ago' or '3d ago'."""
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
    """Drop articles older than _MAX_NEWS_AGE_HRS. Articles with no parsed
    timestamp are kept so we never silently discard news we can't date."""
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


def _format_news_items(articles: list[dict]) -> str:
    if not articles:
        return "(no recent news available)"
    lines = []
    for i, a in enumerate(articles, 1):
        body_snippet = a.get("body", "")[:600].strip()
        age = _age_label(a.get("published_dt"))
        lines.append(
            f"{i}. [{age}] ({a.get('provider', '')}) {a.get('headline', '')}\n"
            f"   {body_snippet}"
        )
    return "\n\n".join(lines)


def _normalize_catalyst_type(value) -> str:
    """Coerce GPT's catalyst_type response to a known value, defaulting to 'other'."""
    normalized = str(value or "other").strip().lower()
    return normalized if normalized in _CATALYST_TYPES else "other"


def catalyst_weight(catalyst_type: str, weights: dict[str, float] | None) -> float:
    """Return the conviction/allocation multiplier for a catalyst_type.

    Weights come from picker_config.json's "catalyst_type_weights", tuned from
    scripts/analyze_scoring.py's realized win-rate/return breakdown by
    catalyst_type (e.g. ma_acquisition has historically underperformed and
    analyst_action has historically outperformed). Defaults to 1.0 (neutral)
    for any catalyst_type not present in the config.
    """
    if not weights:
        return 1.0
    return float(weights.get(catalyst_type, 1.0))


def _format_earnings_signal(earnings: dict | None) -> str:
    if not earnings:
        return "No upcoming earnings data."
    earnings_date = earnings.get("earnings_date", "")
    if not earnings_date:
        return "No upcoming earnings data."
    eps_estimate = earnings.get("eps_estimate")
    time_of_day = earnings.get("time_of_day", "unknown")
    if eps_estimate:
        return f"Earnings on {earnings_date} ({time_of_day}), EPS estimate: ${eps_estimate:.2f}"
    return f"Earnings on {earnings_date} ({time_of_day}), EPS estimate: N/A"


def _format_sentiment_signal(sentiment: dict | None) -> str:
    if not sentiment:
        return "No social sentiment data available."
    reddit_mentions = sentiment.get("reddit_mentions", 0)
    reddit_sentiment = sentiment.get("reddit_sentiment", 0.0)
    stocktwits_sentiment = sentiment.get("stocktwits_sentiment", 0.0)
    lines = []
    if reddit_mentions > 0:
        lines.append(f"Reddit: {reddit_mentions} mentions, sentiment {reddit_sentiment:+.2f}")
    if stocktwits_sentiment != 0:
        lines.append(f"StockTwits: sentiment {stocktwits_sentiment:+.2f}")
    return " | ".join(lines) if lines else "Neutral social sentiment"


def _format_sec_signal(filings: list[dict] | None) -> str:
    if not filings:
        return "No recent SEC filings or insider activity."
    lines = []
    for filing in filings[:3]:
        form_type = filing.get("form_type", "")
        significance = filing.get("significance", "")
        summary = filing.get("summary", "")
        lines.append(f"- {form_type} ({significance}): {summary}")
    return "\n".join(lines) if lines else "No recent SEC filings or insider activity."


def _parse_json_response(raw: str) -> dict:
    """Strip markdown fences and parse JSON."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        raw = raw.removeprefix("json")
        raw = raw.strip()
    return json.loads(raw)


# ── Per-ticker scoring ────────────────────────────────────────────────────────

def _score_ticker(
    client: OpenAI,
    ticker: str,
    articles: list[dict],
    trend_summary: str,
    spy_context: str = "unavailable",
    volume_context: str = "unavailable",
    earnings: dict | None = None,
    sentiment: dict | None = None,
    sec_filings: list[dict] | None = None,
) -> dict:
    today_date = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
    prompt = (
        _load_prompt_template()
        .replace("{ticker}", ticker)
        .replace("{trend_summary}", trend_summary)
        .replace("{news_items}", _format_news_items(articles))
        .replace("{spy_context}", spy_context)
        .replace("{today_date}", today_date)
        .replace("{volume_context}", volume_context)
        .replace("{earnings_signal}", _format_earnings_signal(earnings))
        .replace("{sentiment_signal}", _format_sentiment_signal(sentiment))
        .replace("{sec_signal}", _format_sec_signal(sec_filings))
    )
    log_llm_input(
        ticker, 0.0, articles, trend_summary,
        earnings=earnings, sentiment=sentiment, sec_filings=sec_filings,
        gpt_prompt=prompt,
    )
    _default = {
        "ticker": ticker,
        "score": 0,
        "direction": "bearish",
        "risk": 5,
        "expected_gain_pct": 0.0,
        "reason": "scoring failed",
        "sector": "Unknown",
        "catalyst_type": "other",
    }
    for attempt in range(4):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "system", "content": prompt}],
                temperature=TEMPERATURE,
                max_completion_tokens=250,
            )
            parsed = _parse_json_response(response.choices[0].message.content or "")
            score         = int(parsed.get("score", 0))
            direction     = str(parsed.get("direction", "bearish")).lower()
            risk          = max(1, min(5, int(parsed.get("risk", 3))))
            expected_gain = float(parsed.get("expected_gain_pct", 0.0))
            reason        = str(parsed.get("reason", ""))
            sector        = str(parsed.get("sector", "Unknown"))
            catalyst_type = _normalize_catalyst_type(parsed.get("catalyst_type"))
            logger.info(
                "catalyst_scorer: %s score=%d dir=%s risk=%d gain=%.1f%% sector=%s catalyst=%s | %s",
                ticker, score, direction, risk, expected_gain, sector, catalyst_type, reason,
            )
            log_llm_output(ticker, parsed, reasoning=reason)
            return {
                "ticker": ticker,
                "score": score,
                "direction": direction,
                "risk": risk,
                "expected_gain_pct": expected_gain,
                "reason": reason,
                "sector": sector,
                "catalyst_type": catalyst_type,
            }
        except openai.RateLimitError:
            if attempt < 3:
                wait = 5 * (2 ** attempt)  # 5s, 10s, 20s
                logger.warning(
                    "catalyst_scorer: rate limit hit for %s — retrying in %ds (attempt %d/4)",
                    ticker, wait, attempt + 1,
                )
                time.sleep(wait)
            else:
                logger.warning("catalyst_scorer: rate limit — giving up on %s", ticker)
                return _default
        except Exception:
            logger.warning("catalyst_scorer: failed to score %s — defaulting to 0", ticker, exc_info=True)
            return _default


# ── Math-based allocation ─────────────────────────────────────────────────────

def _compute_allocations(
    picks: list[dict],
    catalyst_weights: dict[str, float] | None = None,
) -> dict[str, float]:
    """
    Allocate capital proportionally to expected value: score * expected_gain_pct / risk,
    scaled by the pick's catalyst_type weight (see catalyst_weight()).

    Uses iterative redistribution so every pick stays within
    [_ALLOC_MIN_PCT, _ALLOC_MAX_PCT] and the total is exactly 100%.

    Algorithm: each pass computes proportional allocations for unconstrained
    picks. Any pick hitting min or max is fixed and excluded from subsequent
    passes so budget is redistributed to remaining free picks.
    """
    convictions: dict[str, float] = {
        p["ticker"]: (
            p["score"] * max(p.get("expected_gain_pct", 1.0), 0.5) / max(p["risk"], 1)
            * catalyst_weight(p.get("catalyst_type", "other"), catalyst_weights)
        )
        for p in picks
    }
    tickers = list(convictions)
    fixed: dict[str, float] = {}

    for _ in range(len(tickers) + 1):
        free = [t for t in tickers if t not in fixed]
        if not free:
            break

        remaining = 100.0 - sum(fixed.values())
        free_total = sum(convictions[t] for t in free) or 1.0
        tentative = {t: convictions[t] / free_total * remaining for t in free}

        newly_fixed = {
            t: _ALLOC_MAX_PCT for t, v in tentative.items() if v > _ALLOC_MAX_PCT
        } | {
            t: _ALLOC_MIN_PCT for t, v in tentative.items() if v < _ALLOC_MIN_PCT
        }

        if not newly_fixed:
            fixed.update(tentative)
            break
        fixed.update(newly_fixed)

    result = {t: round(fixed.get(t, 100.0 / len(tickers)), 1) for t in tickers}
    logger.info(
        "catalyst_scorer: allocations — %s",
        {t: f"{v}%" for t, v in sorted(result.items(), key=lambda x: -x[1])},
    )
    return result


# ── Public API ────────────────────────────────────────────────────────────────

def score_candidates(
    news_by_ticker: dict[str, list[dict]],
    excluded: set[str],
    trend_by_ticker: dict[str, str] | None = None,
    sequential: bool = False,
    spy_context: str = "unavailable",
    volume_by_ticker: dict[str, str] | None = None,
    earnings_by_ticker: dict[str, dict] | None = None,
    sentiment_by_ticker: dict[str, dict] | None = None,
    sec_by_ticker: dict[str, list[dict]] | None = None,
) -> list[dict]:
    """
    Score every ticker in news_by_ticker with GPT in parallel.

    Returns the full raw list (unfiltered, unsorted) so the caller can
    apply different thresholds without re-calling GPT.

    Each result dict has keys: ticker, score, direction, risk,
    expected_gain_pct, reason, trend_summary.

    earnings_by_ticker / sentiment_by_ticker / sec_by_ticker are optional —
    tickers missing from these dicts just get the "no data available" prompt text.
    """
    client = OpenAI()
    trend_by_ticker = trend_by_ticker or {}
    volume_by_ticker = volume_by_ticker or {}
    earnings_by_ticker = earnings_by_ticker or {}
    sentiment_by_ticker = sentiment_by_ticker or {}
    sec_by_ticker = sec_by_ticker or {}

    candidates: dict[str, list[dict]] = {}
    stale_skipped: list[str] = []
    for ticker, articles in news_by_ticker.items():
        if ticker in excluded or not articles:
            continue
        fresh = _filter_stale_articles(articles)
        if not fresh:
            stale_skipped.append(ticker)
            continue
        candidates[ticker] = fresh
    if stale_skipped:
        logger.info(
            "catalyst_scorer: skipped %d ticker(s) — all news older than %dh: %s",
            len(stale_skipped), _MAX_NEWS_AGE_HRS, ", ".join(stale_skipped),
        )

    logger.info("catalyst_scorer: scoring %d tickers with news", len(candidates))

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=1 if sequential else MAX_WORKERS) as executor:
        futures = {
            executor.submit(
                _score_ticker,
                client,
                ticker,
                articles,
                trend_by_ticker.get(ticker, "unavailable"),
                spy_context,
                volume_by_ticker.get(ticker, "unavailable"),
                earnings_by_ticker.get(ticker),
                sentiment_by_ticker.get(ticker),
                sec_by_ticker.get(ticker, []),
            ): ticker
            for ticker, articles in candidates.items()
        }
        for future in as_completed(futures):
            result = future.result()
            result["trend_summary"] = trend_by_ticker.get(result["ticker"], "unavailable")
            results.append(result)

    return results


def filter_and_rank(
    scored: list[dict],
    num_stocks: int,
    min_score: int,
    min_expected_gain_pct: float = 0.0,
    sector_cap: int | None = None,
    catalyst_weights: dict[str, float] | None = None,
) -> list[dict]:
    """
    Filter scored results to bullish picks above min_score, sort by score,
    take top num_stocks, then compute risk-adjusted allocations.

    catalyst_weights (from picker_config.json "catalyst_type_weights") scales
    both ranking and allocation by historical performance per catalyst_type —
    e.g. discounting ma_acquisition picks, which have historically had a much
    lower win rate than the rest of the pool. See catalyst_weight().

    Returns list of pick dicts with allocation_pct added.
    """
    bullish = [
        r for r in scored
        if r["direction"] == "bullish"
        and r["score"] >= min_score
        and r.get("expected_gain_pct", 0.0) >= min_expected_gain_pct
        and not (r["risk"] >= 4 and r["score"] < 8)
    ]
    rejected_bearish = [r["ticker"] for r in scored if r["direction"] != "bullish"]
    rejected_score   = [r["ticker"] for r in scored if r["direction"] == "bullish" and r["score"] < min_score]
    rejected_gain    = [
        r["ticker"] for r in scored
        if r["direction"] == "bullish"
        and r["score"] >= min_score
        and r.get("expected_gain_pct", 0.0) < min_expected_gain_pct
    ]
    rejected_risk    = [
        r["ticker"] for r in scored
        if r["direction"] == "bullish"
        and r["score"] >= min_score
        and r.get("expected_gain_pct", 0.0) >= min_expected_gain_pct
        and r["risk"] >= 4 and r["score"] < 8
    ]

    if rejected_bearish:
        logger.info("catalyst_scorer: bearish/neutral filtered: %s", ", ".join(rejected_bearish))
    if rejected_score:
        logger.info(
            "catalyst_scorer: below min_score (%d) filtered: %s",
            min_score, ", ".join(rejected_score),
        )
    if rejected_gain:
        logger.info(
            "catalyst_scorer: below min_expected_gain_pct (%.1f%%) filtered: %s",
            min_expected_gain_pct, ", ".join(rejected_gain),
        )
    if rejected_risk:
        logger.info("catalyst_scorer: high-risk/low-score filtered: %s", ", ".join(rejected_risk))

    # Rank by conviction = score × expected_gain / risk × catalyst_type weight
    # (same formula as allocation). This ensures the stocks we select are
    # consistent with how we'd allocate capital.
    def conviction(r: dict) -> float:
        base = r["score"] * max(r.get("expected_gain_pct", 1.0), 0.5) / max(r["risk"], 1)
        return base * catalyst_weight(r.get("catalyst_type", "other"), catalyst_weights)

    bullish.sort(key=conviction, reverse=True)

    # Apply sector cap: take picks in order, skip any sector that already
    # has sector_cap picks. This preserves conviction ordering.
    if sector_cap is not None:
        sector_counts: dict[str, int] = {}
        capped: list[dict] = []
        for r in bullish:
            sec = r.get("sector", "Unknown")
            if sector_counts.get(sec, 0) >= sector_cap:
                logger.info(
                    "catalyst_scorer: %s dropped — sector '%s' already has %d picks (cap=%d)",
                    r["ticker"], sec, sector_counts[sec], sector_cap,
                )
                continue
            sector_counts[sec] = sector_counts.get(sec, 0) + 1
            capped.append(r)
            if len(capped) == num_stocks:
                break
        top = capped
    else:
        top = bullish[:num_stocks]

    if not top:
        return []

    allocations = _compute_allocations(top, catalyst_weights)
    for pick in top:
        pick["allocation_pct"] = allocations.get(pick["ticker"], round(100.0 / len(top), 1))

    return top


def score_and_rank(
    news_by_ticker: dict[str, list[dict]],
    num_stocks: int,
    excluded: set[str],
    min_score: int = 7,
    trend_by_ticker: dict[str, str] | None = None,
) -> list[dict]:
    """Convenience wrapper: score_candidates + filter_and_rank in one call."""
    scored = score_candidates(news_by_ticker, excluded, trend_by_ticker)
    return filter_and_rank(scored, num_stocks, min_score)
