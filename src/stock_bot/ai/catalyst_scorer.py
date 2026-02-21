# src/stock_bot/ai/catalyst_scorer.py

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from importlib.resources import files

from openai import OpenAI

logger = logging.getLogger(__name__)

MODEL = "gpt-4o"
TEMPERATURE = 0.3
MAX_TOKENS = 150
MAX_WORKERS = 10


@lru_cache(maxsize=1)
def _load_prompt_template() -> str:
    return files("stock_bot.templates").joinpath("catalyst_prompt.txt").read_text(encoding="utf-8")


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


def _score_ticker(client: OpenAI, ticker: str, articles: list[dict]) -> dict:
    prompt = (
        _load_prompt_template()
        .replace("{ticker}", ticker)
        .replace("{news_items}", _format_news_items(articles))
    )
    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": prompt}],
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS,
        )
        raw = (response.choices[0].message.content or "").strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        parsed = json.loads(raw)
        score = int(parsed.get("score", 0))
        direction = str(parsed.get("direction", "bearish")).lower()
        reason = str(parsed.get("reason", ""))
        logger.info("catalyst_scorer: %s scored %d (%s) — %s", ticker, score, direction, reason)
        return {"ticker": ticker, "score": score, "direction": direction, "reason": reason}
    except Exception:
        logger.warning("catalyst_scorer: failed to score %s — defaulting to 0", ticker, exc_info=True)
        return {"ticker": ticker, "score": 0, "direction": "bearish", "reason": "scoring failed"}


def score_and_rank(
    news_by_ticker: dict[str, list[dict]],
    num_stocks: int,
    excluded: set[str],
    min_score: int = 7,
) -> list[dict]:
    """
    Score each ticker in parallel using GPT.

    Returns top num_stocks picks sorted descending by score.
    Filters out: tickers in excluded, tickers with no news, bearish direction, score < min_score.

    Returns:
        [{"ticker": str, "score": int, "direction": str, "reason": str}]
    """
    client = OpenAI()

    candidates = {
        ticker: articles
        for ticker, articles in news_by_ticker.items()
        if ticker not in excluded and articles
    }

    logger.info("catalyst_scorer: scoring %d tickers with news", len(candidates))

    results: list[dict] = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_ticker = {
            executor.submit(_score_ticker, client, ticker, articles): ticker
            for ticker, articles in candidates.items()
        }
        for future in as_completed(future_to_ticker):
            result = future.result()
            results.append(result)

    # Filter: bullish only, above min_score
    bullish = [r for r in results if r["direction"] == "bullish" and r["score"] >= min_score]
    rejected_bearish = [r["ticker"] for r in results if r["direction"] != "bullish"]
    rejected_score = [r["ticker"] for r in results if r["direction"] == "bullish" and r["score"] < min_score]

    if rejected_bearish:
        logger.info("catalyst_scorer: bearish filtered out: %s", ", ".join(rejected_bearish))
    if rejected_score:
        logger.info("catalyst_scorer: below min_score (%d) filtered out: %s", min_score, ", ".join(rejected_score))

    bullish.sort(key=lambda x: x["score"], reverse=True)
    top = bullish[:num_stocks]

    logger.info("catalyst_scorer: top %d picks selected", len(top))
    return top
