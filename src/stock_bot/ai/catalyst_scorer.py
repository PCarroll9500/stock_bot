# src/stock_bot/ai/catalyst_scorer.py

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from importlib.resources import files

import openai
from openai import OpenAI

logger = logging.getLogger(__name__)

MODEL       = "gpt-4o"
TEMPERATURE = 0.3
MAX_WORKERS = 3

_ALLOC_MIN_PCT = 5.0
_ALLOC_MAX_PCT = 35.0


# ── Prompt template ────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _load_prompt_template() -> str:
    return files("stock_bot.templates").joinpath("catalyst_prompt.txt").read_text(encoding="utf-8")


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
    _default = {
        "ticker": ticker,
        "score": 0,
        "direction": "bearish",
        "risk": 5,
        "expected_gain_pct": 0.0,
        "reason": "scoring failed",
    }
    for attempt in range(4):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "system", "content": prompt}],
                temperature=TEMPERATURE,
                max_tokens=200,
            )
            parsed = _parse_json_response(response.choices[0].message.content or "")
            score         = int(parsed.get("score", 0))
            direction     = str(parsed.get("direction", "bearish")).lower()
            risk          = max(1, min(5, int(parsed.get("risk", 3))))
            expected_gain = float(parsed.get("expected_gain_pct", 0.0))
            reason        = str(parsed.get("reason", ""))
            logger.info(
                "catalyst_scorer: %s score=%d dir=%s risk=%d gain=%.1f%% | %s",
                ticker, score, direction, risk, expected_gain, reason,
            )
            return {
                "ticker": ticker,
                "score": score,
                "direction": direction,
                "risk": risk,
                "expected_gain_pct": expected_gain,
                "reason": reason,
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

def _compute_allocations(picks: list[dict]) -> dict[str, float]:
    """
    Allocate capital proportionally to expected value: score * expected_gain_pct / risk.

    Uses iterative redistribution so every pick stays within
    [_ALLOC_MIN_PCT, _ALLOC_MAX_PCT] and the total is exactly 100%.

    Algorithm: each pass computes proportional allocations for unconstrained
    picks. Any pick hitting min or max is fixed and excluded from subsequent
    passes so budget is redistributed to remaining free picks.
    """
    convictions: dict[str, float] = {
        p["ticker"]: p["score"] * max(p.get("expected_gain_pct", 1.0), 0.5) / max(p["risk"], 1)
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
) -> list[dict]:
    """
    Score every ticker in news_by_ticker with GPT in parallel.

    Returns the full raw list (unfiltered, unsorted) so the caller can
    apply different thresholds without re-calling GPT.

    Each result dict has keys: ticker, score, direction, risk,
    expected_gain_pct, reason, trend_summary.
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
    with ThreadPoolExecutor(max_workers=1 if sequential else MAX_WORKERS) as executor:
        futures = {
            executor.submit(
                _score_ticker,
                client,
                ticker,
                articles,
                trend_by_ticker.get(ticker, "unavailable"),
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
) -> list[dict]:
    """
    Filter scored results to bullish picks above min_score, sort by score,
    take top num_stocks, then compute risk-adjusted allocations.

    Returns list of pick dicts with allocation_pct added.
    """
    bullish = [r for r in scored if r["direction"] == "bullish" and r["score"] >= min_score]
    rejected_bearish = [r["ticker"] for r in scored if r["direction"] != "bullish"]
    rejected_score   = [r["ticker"] for r in scored if r["direction"] == "bullish" and r["score"] < min_score]

    if rejected_bearish:
        logger.info("catalyst_scorer: bearish/neutral filtered: %s", ", ".join(rejected_bearish))
    if rejected_score:
        logger.info(
            "catalyst_scorer: below min_score (%d) filtered: %s",
            min_score, ", ".join(rejected_score),
        )

    bullish.sort(key=lambda x: x["score"], reverse=True)
    top = bullish[:num_stocks]

    if not top:
        return []

    allocations = _compute_allocations(top)
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
