import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
]

BULLISH_KEYWORDS = {"bullish", "moon", "buy", "surge", "beat", "pop", "pump", "rocket"}
BEARISH_KEYWORDS = {"bearish", "crash", "sell", "tank", "miss", "dump", "dead", "rip"}

CACHE_DIR = Path("logs/sentiment_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _get_cache_key(ticker: str, source: str) -> Path:
    return CACHE_DIR / f"{ticker}_{source}.json"


def _is_cache_valid(cache_file: Path, ttl_min: int = 30) -> bool:
    if not cache_file.exists():
        return False
    age_min = (datetime.now() - datetime.fromtimestamp(cache_file.stat().st_mtime)) / timedelta(minutes=1)
    return age_min < ttl_min


def _score_sentiment(text: str) -> float:
    """Simple sentiment score from -1.0 (bearish) to +1.0 (bullish)."""
    text_lower = text.lower()
    bullish = len([w for w in BULLISH_KEYWORDS if w in text_lower])
    bearish = len([w for w in BEARISH_KEYWORDS if w in text_lower])
    total = bullish + bearish
    if total == 0:
        return 0.0
    return (bullish - bearish) / total


def _scrape_reddit(ticker: str, cache_ttl_min: int = 30) -> dict:
    """Scrape Reddit r/wallstreetbets and r/stocks for ticker mentions."""
    cache_file = _get_cache_key(ticker, "reddit")
    if _is_cache_valid(cache_file, cache_ttl_min):
        try:
            with open(cache_file) as f:
                return json.load(f)
        except Exception as e:
            logger.debug(f"Failed to load Reddit cache for {ticker}: {e}")

    try:
        headers = {"User-Agent": USER_AGENTS[0]}
        mentions = 0
        sentiment_sum = 0.0

        for subreddit in ["wallstreetbets", "stocks"]:
            try:
                url = f"https://www.reddit.com/r/{subreddit}/search/?q={ticker}&restrict_sr=1&t=day"
                response = requests.get(url, headers=headers, timeout=10)
                response.raise_for_status()

                soup = BeautifulSoup(response.content, "html.parser")
                posts = soup.find_all("h3", limit=10)

                for post in posts:
                    mentions += 1
                    sentiment_sum += _score_sentiment(post.text)

            except Exception as e:
                logger.debug(f"Error scraping r/{subreddit} for {ticker}: {e}")

        avg_sentiment = sentiment_sum / max(mentions, 1)
        result = {
            "ticker": ticker,
            "source": "reddit",
            "mentions": mentions,
            "sentiment": avg_sentiment,
            "timestamp": datetime.now().isoformat(),
        }

        try:
            with open(cache_file, "w") as f:
                json.dump(result, f)
        except Exception as e:
            logger.debug(f"Failed to cache Reddit data: {e}")

        return result

    except Exception as e:
        logger.warning(f"Error scraping Reddit for {ticker}: {e}")
        return {
            "ticker": ticker,
            "source": "reddit",
            "mentions": 0,
            "sentiment": 0.0,
        }


def _scrape_stocktwits(ticker: str, cache_ttl_min: int = 30) -> dict:
    """Scrape StockTwits for sentiment."""
    cache_file = _get_cache_key(ticker, "stocktwits")
    if _is_cache_valid(cache_file, cache_ttl_min):
        try:
            with open(cache_file) as f:
                return json.load(f)
        except Exception as e:
            logger.debug(f"Failed to load StockTwits cache for {ticker}: {e}")

    try:
        headers = {"User-Agent": USER_AGENTS[1]}
        url = f"https://stocktwits.com/symbol/{ticker}"
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()

        soup = BeautifulSoup(response.content, "html.parser")

        # Look for sentiment indicators (usually displayed as bullish/bearish percentages)
        sentiment_elements = soup.find_all("span", {"class": re.compile("sentiment")})

        bullish_count = 0
        bearish_count = 0

        for elem in sentiment_elements[:20]:
            text = elem.text.lower()
            if "bullish" in text:
                bullish_count += 1
            elif "bearish" in text:
                bearish_count += 1

        total = bullish_count + bearish_count
        sentiment = (bullish_count - bearish_count) / total if total > 0 else 0.0

        result = {
            "ticker": ticker,
            "source": "stocktwits",
            "bullish": bullish_count,
            "bearish": bearish_count,
            "sentiment": sentiment,
            "timestamp": datetime.now().isoformat(),
        }

        try:
            with open(cache_file, "w") as f:
                json.dump(result, f)
        except Exception as e:
            logger.debug(f"Failed to cache StockTwits data: {e}")

        return result

    except Exception as e:
        logger.warning(f"Error scraping StockTwits for {ticker}: {e}")
        return {
            "ticker": ticker,
            "source": "stocktwits",
            "bullish": 0,
            "bearish": 0,
            "sentiment": 0.0,
        }


def fetch_sentiment(
    tickers: list[str], cache_ttl_min: int = 30, max_per_source: int = 10
) -> pd.DataFrame:
    """Fetch social sentiment signals for tickers.

    Returns:
        DataFrame with columns: ticker, reddit_mentions, reddit_sentiment,
                                stocktwits_sentiment, overall_sentiment
    """
    try:
        results = []

        for ticker in tickers[:max_per_source]:
            reddit_data = _scrape_reddit(ticker, cache_ttl_min)
            stocktwits_data = _scrape_stocktwits(ticker, cache_ttl_min)

            overall_sentiment = (
                reddit_data.get("sentiment", 0.0) * 0.5
                + stocktwits_data.get("sentiment", 0.0) * 0.5
            )

            results.append(
                {
                    "ticker": ticker,
                    "reddit_mentions": reddit_data.get("mentions", 0),
                    "reddit_sentiment": reddit_data.get("sentiment", 0.0),
                    "stocktwits_sentiment": stocktwits_data.get("sentiment", 0.0),
                    "overall_sentiment": overall_sentiment,
                }
            )

        df = pd.DataFrame(results)
        logger.info(f"Fetched sentiment for {len(df)} tickers")
        return df

    except Exception as e:
        logger.error(f"Error fetching sentiment: {e}")
        return pd.DataFrame(
            columns=[
                "ticker",
                "reddit_mentions",
                "reddit_sentiment",
                "stocktwits_sentiment",
                "overall_sentiment",
            ]
        )
