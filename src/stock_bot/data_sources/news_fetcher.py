# src/stock_bot/data_sources/news_fetcher.py

import asyncio
import logging
import re

from ib_insync import IB

logger = logging.getLogger(__name__)

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return _HTML_TAG_RE.sub("", text).strip()


def fetch_news_for_tickers(
    tickers: list[dict],
    ib: IB,
    config: dict,
) -> dict[str, list[dict]]:
    """
    For each ticker, fetch up to max_articles recent headlines + full article body.

    Args:
        tickers: [{"ticker": str, "conId": int}, ...]
        ib: Connected IB instance.
        config: {"providers": "FLY+BRFG+DJ-N", "max_articles": 5}

    Returns:
        {ticker: [{"time": str, "provider": str, "headline": str, "body": str}]}
        Tickers with zero articles are included as empty lists.
    """
    provider_codes: str = config.get("providers", "FLY+BRFG+DJ-N")
    max_articles: int = config.get("max_articles", 5)

    news_by_ticker: dict[str, list[dict]] = {}

    for entry in tickers:
        ticker = entry["ticker"]
        con_id = entry["conId"]
        articles: list[dict] = []

        try:
            headlines = ib.reqHistoricalNews(
                conId=con_id,
                providerCodes=provider_codes,
                startDateTime="",
                endDateTime="",
                totalResults=max_articles,
            )
        except Exception:
            logger.warning("news_fetcher: reqHistoricalNews failed for %s", ticker, exc_info=True)
            news_by_ticker[ticker] = []
            continue

        if headlines is None:
            logger.warning("news_fetcher: reqHistoricalNews timed out for %s", ticker)
            news_by_ticker[ticker] = []
            continue

        for hl in headlines:
            try:
                article = ib.reqNewsArticle(
                    providerCode=hl.providerCode,
                    articleId=hl.articleId,
                )
                body = _strip_html(article.articleText) if article and article.articleText else ""
            except Exception:
                logger.debug("news_fetcher: could not fetch article body for %s/%s", ticker, hl.articleId)
                body = ""

            articles.append({
                "time": str(hl.time),
                "provider": hl.providerCode,
                "headline": hl.headline,
                "body": body,
            })

        logger.info("news_fetcher: %s — %d article(s)", ticker, len(articles))
        news_by_ticker[ticker] = articles

    return news_by_ticker


async def fetch_news_for_tickers_async(
    tickers: list[dict],
    ib: IB,
    config: dict,
) -> dict[str, list[dict]]:
    """
    Async parallel version of fetch_news_for_tickers.

    Fetches news for up to 5 tickers concurrently. Within each ticker, article
    bodies are fetched in parallel. Typically 5–10x faster than sequential.
    """
    provider_codes: str = config.get("providers", "FLY+BRFG+DJ-N")
    max_articles: int = config.get("max_articles", 5)
    sem = asyncio.Semaphore(5)  # max 5 tickers in-flight at once

    async def _fetch_article(ticker: str, hl) -> dict:
        try:
            article = await ib.reqNewsArticleAsync(
                providerCode=hl.providerCode,
                articleId=hl.articleId,
            )
            body = _strip_html(article.articleText) if article and article.articleText else ""
        except Exception:
            logger.debug("news_fetcher: could not fetch article body for %s/%s", ticker, hl.articleId)
            body = ""
        return {
            "time": str(hl.time),
            "provider": hl.providerCode,
            "headline": hl.headline,
            "body": body,
        }

    async def _fetch_one(entry: dict) -> tuple[str, list[dict]]:
        ticker = entry["ticker"]
        con_id = entry["conId"]

        async with sem:
            try:
                headlines = await ib.reqHistoricalNewsAsync(
                    conId=con_id,
                    providerCodes=provider_codes,
                    startDateTime="",
                    endDateTime="",
                    totalResults=max_articles,
                )
            except Exception:
                logger.warning("news_fetcher: reqHistoricalNews failed for %s", ticker, exc_info=True)
                return ticker, []

            if headlines is None:
                logger.warning("news_fetcher: reqHistoricalNews timed out for %s", ticker)
                return ticker, []

            # Fetch all article bodies for this ticker in parallel
            articles = await asyncio.gather(*[_fetch_article(ticker, hl) for hl in headlines])

        logger.info("news_fetcher: %s — %d article(s)", ticker, len(articles))
        return ticker, list(articles)

    results = await asyncio.gather(*[_fetch_one(e) for e in tickers])
    return dict(results)
