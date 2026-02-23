# src/stock_bot/ai/catalyst_scorer.py

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from importlib.resources import files

from openai import OpenAI

logger = logging.getLogger(__name__)

MODEL       = "gpt-4o"
TEMPERATURE = 0.3
MAX_WORKERS = 10

# ── Prompt templates ──────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _load_prompt_template() -> str:
    return files("stock_bot.templates").joinpath("catalyst_prompt.txt").read_text(encoding="utf-8")


_ALLOCATION_PROMPT = """\
You are a portfolio manager allocating capital across a set of aggressive day-trading picks.

Given the picks below (each with a catalyst score, price trend, and reason), assign an \
allocation percentage to each. Allocations must sum to exactly 100%.

Rules:
- Higher allocation: strong catalyst (score 9-10) AND healthy trend (monthly/yearly positive or only mildly negative)
- Lower allocation: speculative play, poor yearly trend (worse than -25%), borderline score, or uncertain catalyst
- A pick with a terrible yearly trend (worse than -40%) should get no more than 5-8% even with a good catalyst
- Minimum per pick: 5%
- Maximum per pick: 35%
- Reflect relative conviction — a clearly superior pick should get meaningfully more capital than weaker ones

Picks:
{picks_summary}

Respond with a JSON array only — no explanation outside the JSON:
[{{"ticker": "X", "allocation_pct": 20.0}}, ...]
"""

# ── Helpers ───────────────────────────────────────────────────────────────────

def _format_news_items(articles: list[dict]) -> str:
    if not articles:
        return "(no recent news available)"
    lines = []
    for i, a in enumerate(articles, 1):
        body_snippet = a.get("body", "")[:300].strip()
        lines.append(
            f"{i}. [{a.get('time', '')}] ({a.get('provider', '')}) {a.get('headline', '')}\n"
            f"   {body_snippet}"
        )
    return "\n\n".join(lines)


def _parse_json_response(raw: str) -> dict:
    """Strip markdown fences and parse JSON."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return json.loads(raw)

# ── Per-ticker scoring ────────────────────────────────────────────────────────

def _score_ticker(
    client: OpenAI,
    ticker: str,
    articles: list[dict],
    trend_summary: str,
) -> dict:
    prompt = (
        _load_prompt_template()
        .replace("{ticker}", ticker)
        .replace("{trend_summary}", trend_summary)
        .replace("{news_items}", _format_news_items(articles))
    )
    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": prompt}],
            temperature=TEMPERATURE,
            max_tokens=200,
        )
        parsed = _parse_json_response(response.choices[0].message.content or "")
        score     = int(parsed.get("score", 0))
        direction = str(parsed.get("direction", "bearish")).lower()
        reason    = str(parsed.get("reason", ""))
        logger.info(
            "catalyst_scorer: %s scored %d (%s) | trend: %s | %s",
            ticker, score, direction, trend_summary, reason,
        )
        return {"ticker": ticker, "score": score, "direction": direction, "reason": reason}
    except Exception:
        logger.warning("catalyst_scorer: failed to score %s — defaulting to 0", ticker, exc_info=True)
        return {"ticker": ticker, "score": 0, "direction": "bearish", "reason": "scoring failed"}

# ── Portfolio allocation ──────────────────────────────────────────────────────

def _allocate_portfolio(client: OpenAI, picks: list[dict]) -> dict[str, float]:
    """
    Second GPT call: given the top N picks, assign allocation percentages summing to 100%.

    Each pick must already have 'trend_summary' set.
    Falls back to equal weighting on any failure.
    """
    lines = [
        f"- {p['ticker']}: score={p['score']}/10, "
        f"trend=[{p.get('trend_summary', 'N/A')}], "
        f"reason=\"{p['reason']}\""
        for p in picks
    ]
    prompt = _ALLOCATION_PROMPT.replace("{picks_summary}", "\n".join(lines))

    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": prompt}],
            temperature=0.2,
            max_tokens=300,
        )
        allocations = _parse_json_response(response.choices[0].message.content or "")
        result = {a["ticker"]: float(a["allocation_pct"]) for a in allocations}

        # Normalize to exactly 100% in case of rounding drift
        total = sum(result.values())
        if total > 0:
            result = {t: round(v / total * 100, 1) for t, v in result.items()}

        logger.info("catalyst_scorer: GPT allocations — %s", result)
        return result

    except Exception:
        logger.warning(
            "catalyst_scorer: allocation call failed — falling back to equal weighting",
            exc_info=True,
        )
        equal = round(100.0 / len(picks), 1)
        return {p["ticker"]: equal for p in picks}

# ── Public API ────────────────────────────────────────────────────────────────

def score_and_rank(
    news_by_ticker: dict[str, list[dict]],
    num_stocks: int,
    excluded: set[str],
    min_score: int = 7,
    trend_by_ticker: dict[str, str] | None = None,
) -> list[dict]:
    """
    Score each ticker in parallel using GPT (with trend context).
    Then run a second GPT call to assign risk-adjusted allocations.

    Args:
        news_by_ticker:   ticker → list of news articles
        num_stocks:       max picks to return
        excluded:         tickers to skip
        min_score:        minimum score threshold
        trend_by_ticker:  ticker → formatted trend string (from fmt_trend_for_prompt)

    Returns:
        list of dicts with keys: ticker, score, direction, reason, allocation_pct, trend_summary
    """
    client = OpenAI()
    trend_by_ticker = trend_by_ticker or {}

    candidates = {
        ticker: articles
        for ticker, articles in news_by_ticker.items()
        if ticker not in excluded and articles
    }

    logger.info("catalyst_scorer: scoring %d tickers with news", len(candidates))

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_ticker = {
            executor.submit(
                _score_ticker,
                client,
                ticker,
                articles,
                trend_by_ticker.get(ticker, "unavailable"),
            ): ticker
            for ticker, articles in candidates.items()
        }
        for future in as_completed(future_to_ticker):
            result = future.result()
            result["trend_summary"] = trend_by_ticker.get(result["ticker"], "unavailable")
            results.append(result)

    # Filter: bullish only, above min_score
    bullish = [r for r in results if r["direction"] == "bullish" and r["score"] >= min_score]
    rejected_bearish = [r["ticker"] for r in results if r["direction"] != "bullish"]
    rejected_score   = [r["ticker"] for r in results if r["direction"] == "bullish" and r["score"] < min_score]

    if rejected_bearish:
        logger.info("catalyst_scorer: bearish filtered: %s", ", ".join(rejected_bearish))
    if rejected_score:
        logger.info("catalyst_scorer: below min_score (%d) filtered: %s", min_score, ", ".join(rejected_score))

    bullish.sort(key=lambda x: x["score"], reverse=True)
    top = bullish[:num_stocks]

    if not top:
        logger.info("catalyst_scorer: no picks passed filters")
        return []

    # Second GPT call — risk-adjusted allocation
    logger.info("catalyst_scorer: running portfolio allocation for %d picks", len(top))
    allocations = _allocate_portfolio(client, top)
    for pick in top:
        pick["allocation_pct"] = allocations.get(pick["ticker"], round(100.0 / len(top), 1))

    logger.info("catalyst_scorer: final %d picks with allocations", len(top))
    return top
