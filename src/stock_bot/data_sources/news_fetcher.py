# src/stock_bot/data_sources/news_fetcher.py

import asyncio
import logging
import re
import xml.etree.ElementTree as ET

import httpx
from bs4 import BeautifulSoup
from ib_insync import IB

logger = logging.getLogger(__name__)

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_FINVIZ_SEM  = asyncio.Semaphore(4)   # avoid Finviz rate-limit

_SCRAPER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


def _strip_html(text: str) -> str:
    return _HTML_TAG_RE.sub("", text).strip()


# ── Web scrapers ───────────────────────────────────────────────────────────────

async def _scrape_finviz(ticker: str, client: httpx.AsyncClient) -> list[dict]:
    """Scrape Finviz news table for a ticker."""
    try:
        async with _FINVIZ_SEM:
            r = await client.get(
                f"https://finviz.com/quote.ashx?t={ticker}",
                headers=_SCRAPER_HEADERS,
                timeout=10,
            )
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        table = soup.find("table", {"id": "news-table"})
        if not table:
            return []
        articles = []
        last_date = ""
        for row in table.find_all("tr"):
            tds = row.find_all("td")
            if len(tds) < 2:
                continue
            time_str = tds[0].text.strip()
            if len(time_str) > 8:
                last_date = time_str[:11]
                time_part = time_str[12:] if len(time_str) > 12 else time_str
            else:
                time_part = time_str
            a_tag = tds[1].find("a")
            if not a_tag:
                continue
            articles.append({
                "time": f"{last_date} {time_part}".strip(),
                "provider": "finviz",
                "headline": a_tag.text.strip(),
                "body": "",
            })
        return articles
    except Exception:
        logger.debug("scraper: finviz failed for %s", ticker, exc_info=True)
        return []


async def _scrape_yahoo(ticker: str, client: httpx.AsyncClient) -> list[dict]:
    """Fetch Yahoo Finance RSS feed for a ticker."""
    try:
        r = await client.get(
            f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US",
            headers=_SCRAPER_HEADERS,
            timeout=10,
        )
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.text)
        articles = []
        for item in root.findall(".//item"):
            title = item.findtext("title", "").strip()
            desc  = _strip_html(item.findtext("description", ""))[:400]
            pub   = item.findtext("pubDate", "").strip()
            if title:
                articles.append({
                    "time": pub,
                    "provider": "yahoo",
                    "headline": title,
                    "body": desc,
                })
        return articles
    except Exception:
        logger.debug("scraper: yahoo failed for %s", ticker, exc_info=True)
        return []


async def _scrape_google(ticker: str, client: httpx.AsyncClient) -> list[dict]:
    """Fetch Google News RSS for a ticker."""
    try:
        r = await client.get(
            f"https://news.google.com/rss/search?q={ticker}+stock&hl=en-US&gl=US&ceid=US:en",
            headers=_SCRAPER_HEADERS,
            timeout=10,
        )
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.text)
        articles = []
        for item in root.findall(".//item"):
            title = item.findtext("title", "").strip()
            pub   = item.findtext("pubDate", "").strip()
            desc  = _strip_html(item.findtext("description", ""))[:400]
            if title:
                articles.append({
                    "time": pub,
                    "provider": "google_news",
                    "headline": title,
                    "body": desc,
                })
        return articles
    except Exception:
        logger.debug("scraper: google failed for %s", ticker, exc_info=True)
        return []


def _merge_articles(finviz: list, yahoo: list, google: list, max_per_source: int) -> list[dict]:
    """
    Combine articles from all three sources, dedup by headline prefix,
    capping each source at max_per_source before merging.
    """
    seen: set[str] = set()
    merged: list[dict] = []
    for article in finviz[:max_per_source] + yahoo[:max_per_source] + google[:max_per_source]:
        key = article["headline"][:60].lower().strip()
        if key and key not in seen:
            seen.add(key)
            merged.append(article)
    return merged


async def _fetch_web_one(
    ticker: str,
    client: httpx.AsyncClient,
    max_per_source: int,
) -> tuple[str, list[dict]]:
    finviz, yahoo, google = await asyncio.gather(
        _scrape_finviz(ticker, client),
        _scrape_yahoo(ticker, client),
        _scrape_google(ticker, client),
    )
    articles = _merge_articles(finviz, yahoo, google, max_per_source)
    logger.info(
        "scraper: %s — %d unique articles (finviz=%d yahoo=%d google=%d)",
        ticker, len(articles),
        len(finviz[:max_per_source]),
        len(yahoo[:max_per_source]),
        len(google[:max_per_source]),
    )
    return ticker, articles


async def _fetch_web_all(
    tickers: list[dict],
    max_per_source: int,
) -> dict[str, list[dict]]:
    """Run all three scrapers in parallel for every ticker."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=12) as client:
        tasks = [_fetch_web_one(e["ticker"], client, max_per_source) for e in tickers]
        results = await asyncio.gather(*tasks)
    return dict(results)


# ── IBKR fetcher ───────────────────────────────────────────────────────────────

async def _fetch_ibkr_batch(
    tickers: list[dict],
    ib: IB,
    config: dict,
) -> dict[str, list[dict]]:
    """Fetch news from IBKR for a batch of tickers (up to 5 concurrent)."""
    if not tickers:
        return {}

    provider_codes: str = config.get("providers", "FLY+BRFG+DJ-N")
    max_articles: int   = config.get("max_articles", 5)
    sem = asyncio.Semaphore(5)

    async def _fetch_article(ticker: str, hl) -> dict:
        try:
            article = await asyncio.wait_for(
                ib.reqNewsArticleAsync(
                    providerCode=hl.providerCode,
                    articleId=hl.articleId,
                ),
                timeout=15,
            )
            body = _strip_html(article.articleText) if article and article.articleText else ""
        except asyncio.TimeoutError:
            body = ""
        except Exception:
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
                headlines = await asyncio.wait_for(
                    ib.reqHistoricalNewsAsync(
                        conId=con_id,
                        providerCodes=provider_codes,
                        startDateTime="",
                        endDateTime="",
                        totalResults=max_articles,
                    ),
                    timeout=30,
                )
            except asyncio.TimeoutError:
                logger.warning("news_fetcher: reqHistoricalNews timed out for %s (>30s)", ticker)
                return ticker, []
            except Exception:
                logger.warning("news_fetcher: reqHistoricalNews failed for %s", ticker, exc_info=True)
                return ticker, []

            if headlines is None:
                logger.warning("news_fetcher: reqHistoricalNews timed out for %s", ticker)
                return ticker, []

            articles = await asyncio.gather(*[_fetch_article(ticker, hl) for hl in headlines])

        logger.info("news_fetcher: %s — %d article(s) from IBKR", ticker, len(articles))
        return ticker, list(articles)

    results = await asyncio.gather(*[_fetch_one(e) for e in tickers])
    return dict(results)


# ── Public API ─────────────────────────────────────────────────────────────────

_PROBE_SIZE = 5   # Tickers to probe before declaring IBKR news service down


def fetch_news_for_tickers(
    tickers: list[dict],
    ib: IB,
    config: dict,
) -> dict[str, list[dict]]:
    """Synchronous wrapper — kept for backwards compatibility."""
    return asyncio.get_event_loop().run_until_complete(
        fetch_news_for_tickers_async(tickers, ib, config)
    )


async def fetch_news_for_tickers_async(
    tickers: list[dict],
    ib: IB,
    config: dict,
) -> dict[str, list[dict]]:
    """
    Fetch news for every ticker.

    Strategy:
      1. Probe IBKR with the first _PROBE_SIZE tickers.
      2. If ALL probes time out -> IBKR news service is down.
         Fall back: scrape Finviz + Yahoo Finance + Google News
         in parallel for ALL tickers and merge results.
      3. If IBKR is healthy, continue fetching remaining tickers via IBKR.
    """
    if not tickers:
        return {}

    max_articles: int = config.get("max_articles", 5)

    probe = tickers[:_PROBE_SIZE]
    rest  = tickers[_PROBE_SIZE:]

    logger.info("news: probing IBKR with %d tickers...", len(probe))
    probe_results = await _fetch_ibkr_batch(probe, ib, config)

    timeouts = sum(1 for v in probe_results.values() if not v)

    if timeouts == len(probe):
        logger.warning(
            "news: IBKR probe — all %d timed out -> falling back to web scrapers (finviz+yahoo+google)",
            len(probe),
        )
        return await _fetch_web_all(tickers, max_articles)

    logger.info(
        "news: IBKR probe — %d/%d succeeded -> continuing with IBKR",
        len(probe) - timeouts, len(probe),
    )
    if rest:
        rest_results = await _fetch_ibkr_batch(rest, ib, config)
        return {**probe_results, **rest_results}

    return probe_results
