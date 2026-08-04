import json
import logging
import re
from datetime import datetime
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

CACHE_DIR = Path("logs/web_scraper_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

BULLISH_KEYWORDS = {"buy", "bullish", "strong", "beat", "surge", "jump", "pop", "rocket"}
BEARISH_KEYWORDS = {"sell", "bearish", "weak", "miss", "drop", "crash", "tank", "down"}


def _score_sentiment(text: str) -> str:
    """Simple sentiment classification from text."""
    text_lower = text.lower()
    bullish = len([w for w in BULLISH_KEYWORDS if w in text_lower])
    bearish = len([w for w in BEARISH_KEYWORDS if w in text_lower])

    if bullish > bearish:
        return "bullish"
    elif bearish > bullish:
        return "bearish"
    else:
        return "neutral"


def _scrape_finviz(ticker: str) -> list[dict]:
    """Scrape news from Finviz."""
    news = []
    try:
        headers = {"User-Agent": USER_AGENTS[0]}
        url = f"https://finviz.com/quote.ashx?t={ticker}"
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()

        soup = BeautifulSoup(response.content, "html.parser")
        news_table = soup.find("table", {"id": "news-table"})

        if news_table:
            rows = news_table.find_all("tr")
            for row in rows[:5]:  # Get top 5 articles
                cols = row.find_all("td")
                if len(cols) >= 2:
                    title = cols[1].text.strip()
                    link = cols[1].find("a")
                    url_article = link["href"] if link else ""

                    news.append(
                        {
                            "ticker": ticker,
                            "title": title,
                            "source": "Finviz",
                            "url": url_article,
                            "publish_date": datetime.now().date().isoformat(),
                            "sentiment": _score_sentiment(title),
                        }
                    )
    except Exception as e:
        logger.debug(f"Error scraping Finviz for {ticker}: {e}")

    return news


def _scrape_yahoo_news(ticker: str) -> list[dict]:
    """Scrape news from Yahoo Finance."""
    news = []
    try:
        headers = {"User-Agent": USER_AGENTS[1]}
        url = f"https://finance.yahoo.com/quote/{ticker}/news"
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()

        soup = BeautifulSoup(response.content, "html.parser")
        news_links = soup.find_all("a", {"href": re.compile(r"news")})

        for link in news_links[:5]:
            title = link.text.strip()
            if title and len(title) > 10:
                news.append(
                    {
                        "ticker": ticker,
                        "title": title,
                        "source": "Yahoo Finance",
                        "url": link.get("href", ""),
                        "publish_date": datetime.now().date().isoformat(),
                        "sentiment": _score_sentiment(title),
                    }
                )
    except Exception as e:
        logger.debug(f"Error scraping Yahoo News for {ticker}: {e}")

    return news


def _scrape_seeking_alpha(ticker: str) -> list[dict]:
    """Scrape articles from Seeking Alpha."""
    news = []
    try:
        headers = {"User-Agent": USER_AGENTS[2]}
        url = f"https://seekingalpha.com/symbol/{ticker}/news"
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()

        soup = BeautifulSoup(response.content, "html.parser")
        articles = soup.find_all("a", {"data-article-id": True})

        for article in articles[:5]:
            title = article.get("title", article.text.strip())
            if title and len(title) > 10:
                news.append(
                    {
                        "ticker": ticker,
                        "title": title,
                        "source": "Seeking Alpha",
                        "url": article.get("href", ""),
                        "publish_date": datetime.now().date().isoformat(),
                        "sentiment": _score_sentiment(title),
                    }
                )
    except Exception as e:
        logger.debug(f"Error scraping Seeking Alpha for {ticker}: {e}")

    return news


def _scrape_stocktwits_news(ticker: str) -> list[dict]:
    """Scrape latest posts from StockTwits."""
    news = []
    try:
        headers = {"User-Agent": USER_AGENTS[0]}
        url = f"https://stocktwits.com/symbol/{ticker}"
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()

        soup = BeautifulSoup(response.content, "html.parser")
        posts = soup.find_all("div", {"class": re.compile("post")})

        for post in posts[:5]:
            title_elem = post.find("p")
            if title_elem:
                title = title_elem.text.strip()
                if len(title) > 10:
                    news.append(
                        {
                            "ticker": ticker,
                            "title": title,
                            "source": "StockTwits",
                            "url": "",
                            "publish_date": datetime.now().date().isoformat(),
                            "sentiment": _score_sentiment(title),
                        }
                    )
    except Exception as e:
        logger.debug(f"Error scraping StockTwits for {ticker}: {e}")

    return news


def _scrape_motley_fool(ticker: str) -> list[dict]:
    """Scrape articles from Motley Fool."""
    news = []
    try:
        headers = {"User-Agent": USER_AGENTS[1]}
        url = f"https://www.fool.com/quote/{ticker}"
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()

        soup = BeautifulSoup(response.content, "html.parser")
        articles = soup.find_all("a", {"href": re.compile(r"article|quote")})

        for article in articles[:5]:
            title = article.text.strip()
            if title and len(title) > 10 and "article" in str(article.get("href", "")):
                news.append(
                    {
                        "ticker": ticker,
                        "title": title,
                        "source": "Motley Fool",
                        "url": article.get("href", ""),
                        "publish_date": datetime.now().date().isoformat(),
                        "sentiment": _score_sentiment(title),
                    }
                )
    except Exception as e:
        logger.debug(f"Error scraping Motley Fool for {ticker}: {e}")

    return news


def scrape_news(
    tickers: list[str], sources: list[str] = None, max_per_ticker: int = 20
) -> pd.DataFrame:
    """Scrape news from multiple sources.

    Returns:
        DataFrame with columns: ticker, title, source, url, publish_date, sentiment
    """
    if sources is None:
        sources = ["finviz", "yahoo", "seeking_alpha", "stocktwits", "motley_fool"]

    try:
        all_news = []
        scraper_map = {
            "finviz": _scrape_finviz,
            "yahoo": _scrape_yahoo_news,
            "seeking_alpha": _scrape_seeking_alpha,
            "stocktwits": _scrape_stocktwits_news,
            "motley_fool": _scrape_motley_fool,
        }

        for ticker in tickers[:max_per_ticker]:
            for source in sources:
                if source in scraper_map:
                    try:
                        news = scraper_map[source](ticker)
                        all_news.extend(news)
                    except Exception as e:
                        logger.debug(f"Error scraping {source} for {ticker}: {e}")

        df = pd.DataFrame(all_news)
        if len(df) > 0:
            logger.info(f"Scraped {len(df)} articles from {len(sources)} sources")
        else:
            logger.debug("No articles scraped")

        return df if len(df) > 0 else pd.DataFrame(
            columns=["ticker", "title", "source", "url", "publish_date", "sentiment"]
        )

    except Exception as e:
        logger.error(f"Error in web scraper: {e}")
        return pd.DataFrame(
            columns=["ticker", "title", "source", "url", "publish_date", "sentiment"]
        )
