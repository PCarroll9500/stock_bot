# src/stock_bot/ai/stock_picker.py

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from importlib.resources import files

from openai import OpenAI

logger = logging.getLogger(__name__)

# OpenAI settings
MODEL = "gpt-4o"
TEMPERATURE = 1.2
MAX_TOKENS = 150

# Retry / concurrency settings
MAX_RETRIES_PER_AGENT = 5
RETRY_SLEEP = 0.35
BATCH_SIZE = 6
MAX_TOTAL_ATTEMPTS = 50


@lru_cache(maxsize=1)
def _load_prompt_template() -> str:
    """Load picker_prompt.txt once and cache it."""
    return files("stock_bot.templates").joinpath("picker_prompt.txt").read_text(encoding="utf-8")


def _build_prompt(excluded_tickers: list[str]) -> str:
    exclusion_str = ", ".join(sorted(excluded_tickers)) if excluded_tickers else "None"
    return _load_prompt_template().replace("{excluded_tickers}", exclusion_str)


def _parse_pick(text: str) -> tuple[str, str] | None:
    """
    Parse GPT output into (ticker, reason).

    Handles:
      Two-line:   TICKER\nOne sentence reason.
      One-line:   TICKER: reason  /  TICKER - reason
    """
    text = text.strip()
    # Single-line with separator (TICKER: reason or TICKER - reason)
    match = re.match(r"^([A-Z$.\-]{1,12})[:\-–\s]+(.+)$", text)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    # Two-line format
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) >= 2 and re.match(r"^[A-Z]{1,5}$", lines[0]):
        return lines[0], " ".join(lines[1:])
    return None


def _is_valid_ticker(ticker: str, valid_symbols: set[str]) -> bool:
    """
    Validate ticker against the known US stock universe.

    TODO: Enhance with live yfinance validation to confirm the ticker is
          actively trading with a real price on a supported exchange
          (NASDAQ, NYSE, AMEX). See stock_bot_website/scripts/utils/
          yahoo_finance_stock_info.py for a reference implementation.
    """
    return ticker in valid_symbols


def _call_openai(client: OpenAI, prompt: str) -> str:
    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "system", "content": prompt}],
        temperature=TEMPERATURE,
        max_tokens=MAX_TOKENS,
    )
    return response.choices[0].message.content or ""


def _single_agent(
    client: OpenAI,
    excluded: frozenset[str],
    valid_symbols: set[str],
) -> tuple[str, str] | None:
    """
    One picker agent: call OpenAI, parse, validate, retry up to MAX_RETRIES_PER_AGENT.
    Returns (ticker, reason) or None if all retries are exhausted.
    """
    prompt = _build_prompt(list(excluded))
    for attempt in range(1, MAX_RETRIES_PER_AGENT + 1):
        try:
            raw = _call_openai(client, prompt)
            parsed = _parse_pick(raw)
            if parsed is None:
                logger.debug("Agent attempt %d: parse failed — %r", attempt, raw[:80])
                time.sleep(RETRY_SLEEP)
                continue
            ticker, reason = parsed
            if ticker in excluded:
                logger.debug("Agent attempt %d: %s is in exclusion list, retrying", attempt, ticker)
                time.sleep(RETRY_SLEEP)
                continue
            if not _is_valid_ticker(ticker, valid_symbols):
                logger.debug("Agent attempt %d: %s not in symbol universe, retrying", attempt, ticker)
                time.sleep(RETRY_SLEEP)
                continue
            return ticker, reason
        except Exception:
            logger.exception("Agent attempt %d: OpenAI call failed", attempt)
            time.sleep(RETRY_SLEEP)
    logger.warning("Agent exhausted all %d retries with no valid pick", MAX_RETRIES_PER_AGENT)
    return None


def get_stocks(
    num_stocks: int,
    valid_symbols: list[str],
    excluded_tickers: list[str] | None = None,
) -> list[dict]:
    """
    Pick `num_stocks` unique AI-selected stocks using parallel OpenAI agents.

    Args:
        num_stocks: How many distinct tickers to return.
        valid_symbols: Full US stock universe (from get_list_all_stocks).
        excluded_tickers: Tickers to never pick — current holdings, always-excluded
                          list, and tickers already picked this run are all merged here.

    Returns:
        List of dicts: [{"ticker": "AAPL", "reason": "..."}, ...]
    """
    client = OpenAI()  # reads OPENAI_API_KEY from environment
    symbol_set = set(valid_symbols)
    picks: list[dict] = []
    excluded: set[str] = set(excluded_tickers or [])
    total_attempts = 0

    while len(picks) < num_stocks and total_attempts < MAX_TOTAL_ATTEMPTS:
        needed = num_stocks - len(picks)
        batch_size = min(BATCH_SIZE, needed + 2)  # slight overshoot to fill gaps fast
        total_attempts += batch_size

        logger.info(
            "Picker: firing %d agents (%d/%d picks so far)",
            batch_size, len(picks), num_stocks,
        )

        # Snapshot excluded set so all agents in this batch share the same baseline.
        # Newly claimed tickers from this batch are added to `excluded` after futures resolve.
        snapshot = frozenset(excluded)

        with ThreadPoolExecutor(max_workers=batch_size) as executor:
            futures = [
                executor.submit(_single_agent, client, snapshot, symbol_set)
                for _ in range(batch_size)
            ]
            for future in as_completed(futures):
                result = future.result()
                if result is None:
                    continue
                ticker, reason = result
                if ticker in excluded:
                    continue  # another thread in this batch beat us to it
                excluded.add(ticker)
                picks.append({"ticker": ticker, "reason": reason})
                logger.info("Picker: +%s — %s", ticker, reason)
                if len(picks) >= num_stocks:
                    break

    if len(picks) < num_stocks:
        logger.warning(
            "Picker: only collected %d/%d picks after %d total agent calls",
            len(picks), num_stocks, total_attempts,
        )

    # Final safety filter — catch anything that slipped through despite the
    # per-agent checks (e.g. GPT hallucinating, race conditions, future callers
    # bypassing _single_agent).
    original_exclusions = set(excluded_tickers or [])
    safe_picks = [p for p in picks if p["ticker"] not in original_exclusions]
    slipped = [p["ticker"] for p in picks if p["ticker"] in original_exclusions]
    if slipped:
        logger.warning(
            "Picker: excluded ticker(s) slipped through and were removed: %s",
            ", ".join(slipped),
        )

    return safe_picks[:num_stocks]
