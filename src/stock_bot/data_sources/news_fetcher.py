# src/stock_bot/data_sources/news_fetcher.py

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
