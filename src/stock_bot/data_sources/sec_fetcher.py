import logging
import re
from datetime import datetime, timedelta

import pandas as pd
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
]


def _scrape_sec_edgar(ticker: str, form_types: list[str]) -> list[dict]:
    """Scrape SEC EDGAR for recent filings."""
    filings = []

    try:
        headers = {"User-Agent": USER_AGENTS[0]}

        # SEC EDGAR company search (get CIK)
        search_url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company={ticker}&type=&dateb=&owner=exclude&count=40"
        response = requests.get(search_url, headers=headers, timeout=10)
        response.raise_for_status()

        soup = BeautifulSoup(response.content, "html.parser")

        # Find company link to get CIK
        company_link = soup.find("a", {"href": re.compile(r"CIK=\d+")})
        if not company_link:
            logger.debug(f"Could not find company {ticker} on SEC EDGAR")
            return filings

        cik_match = re.search(r"CIK=(\d+)", company_link["href"])
        if not cik_match:
            return filings

        cik = cik_match.group(1)

        # Fetch filings for this CIK
        for form_type in form_types:
            try:
                filings_url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type={form_type}&dateb=&owner=exclude&count=10"
                response = requests.get(filings_url, headers=headers, timeout=10)
                response.raise_for_status()

                soup = BeautifulSoup(response.content, "html.parser")
                rows = soup.find_all("tr")

                for row in rows[1:]:  # Skip header
                    cols = row.find_all("td")
                    if len(cols) < 4:
                        continue

                    filing_type = cols[0].text.strip()
                    filing_date = cols[3].text.strip()

                    # Parse filing date
                    try:
                        date_obj = datetime.strptime(filing_date, "%Y-%m-%d").date()
                    except ValueError:
                        continue

                    # Check if recent (last 7 days)
                    if (datetime.now().date() - date_obj).days > 7:
                        continue

                    filing_link = cols[1].find("a")
                    accession_num = cols[2].text.strip()

                    # Extract summary from filing (simplified)
                    summary = f"{filing_type} filed"
                    if "8-K" in filing_type and "CEO" in str(row):
                        summary = "FORM 8-K: Potential CEO/officer change"
                    elif "4" in filing_type:
                        summary = "FORM 4: Insider trading activity"

                    significance = "high" if "8-K" in filing_type else "medium"

                    filings.append(
                        {
                            "ticker": ticker,
                            "form_type": filing_type,
                            "filing_date": date_obj.isoformat(),
                            "summary": summary,
                            "significance": significance,
                            "accession": accession_num,
                        }
                    )

            except Exception as e:
                logger.debug(f"Error fetching {form_type} filings for {ticker}: {e}")

    except Exception as e:
        logger.warning(f"Error scraping SEC EDGAR for {ticker}: {e}")

    return filings


def _scrape_yahoo_insider(ticker: str) -> list[dict]:
    """Scrape Yahoo Finance insider trades."""
    insider_trades = []

    try:
        headers = {"User-Agent": USER_AGENTS[1]}
        url = f"https://finance.yahoo.com/quote/{ticker}/insider-transactions"

        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()

        soup = BeautifulSoup(response.content, "html.parser")
        table = soup.find("table")

        if not table:
            return insider_trades

        rows = table.find_all("tr")[1:]  # Skip header

        for row in rows[:10]:  # Last 10 insider trades
            cols = row.find_all("td")
            if len(cols) < 5:
                continue

            try:
                trans_date = cols[0].text.strip()
                trans_type = cols[1].text.strip().upper()  # BUY/SELL
                shares = cols[3].text.strip().replace(",", "")
                price = cols[4].text.strip().replace("$", "").replace(",", "")

                # Parse date
                try:
                    date_obj = datetime.strptime(trans_date, "%m/%d/%Y").date()
                except ValueError:
                    continue

                # Check if recent
                if (datetime.now().date() - date_obj).days > 7:
                    continue

                significance = "high" if trans_type == "BUY" else "low"

                insider_trades.append(
                    {
                        "ticker": ticker,
                        "form_type": "INSIDER_TRADE",
                        "filing_date": date_obj.isoformat(),
                        "summary": f"Insider {trans_type.lower()} {shares} shares @ ${price}",
                        "significance": significance,
                        "transaction": trans_type,
                    }
                )
            except Exception as e:
                logger.debug(f"Error parsing insider trade row: {e}")

    except Exception as e:
        logger.debug(f"Error scraping Yahoo insider for {ticker}: {e}")

    return insider_trades


def fetch_sec_filings(
    tickers: list[str], days_back: int = 7, max_per_ticker: int = 10
) -> pd.DataFrame:
    """Fetch SEC filings and insider activity.

    Returns:
        DataFrame with columns: ticker, form_type, filing_date, summary, significance
    """
    try:
        all_filings = []

        form_types = ["4", "8-K", "13F"]

        for ticker in tickers[:max_per_ticker]:
            # SEC EDGAR filings
            sec_filings = _scrape_sec_edgar(ticker, form_types)
            all_filings.extend(sec_filings)

            # Yahoo insider trades
            insider_trades = _scrape_yahoo_insider(ticker)
            all_filings.extend(insider_trades)

        df = pd.DataFrame(all_filings)
        if len(df) > 0:
            logger.info(f"Fetched {len(df)} SEC filings and insider trades")
        else:
            logger.debug("No recent SEC filings found")

        return df if len(df) > 0 else pd.DataFrame(
            columns=["ticker", "form_type", "filing_date", "summary", "significance"]
        )

    except Exception as e:
        logger.error(f"Error fetching SEC filings: {e}")
        return pd.DataFrame(
            columns=["ticker", "form_type", "filing_date", "summary", "significance"]
        )
