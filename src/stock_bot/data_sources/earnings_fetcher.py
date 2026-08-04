import logging
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
]


def fetch_earnings(days_ahead: int = 7, max_retries: int = 3) -> pd.DataFrame:
    """Scrape upcoming earnings calendar from Yahoo Finance.

    Returns:
        DataFrame with columns: ticker, earnings_date, eps_estimate, time_of_day
    """
    try:
        today = datetime.now().date()
        end_date = today + timedelta(days=days_ahead)

        # Yahoo Finance earnings calendar URL
        url = f"https://finance.yahoo.com/calendar/earnings?from={today}&to={end_date}"

        for attempt in range(max_retries):
            try:
                headers = {"User-Agent": USER_AGENTS[attempt % len(USER_AGENTS)]}
                response = requests.get(url, headers=headers, timeout=10)
                response.raise_for_status()
                break
            except (requests.RequestException, requests.Timeout) as e:
                if attempt == max_retries - 1:
                    raise
                logger.warning(f"Earnings fetch attempt {attempt + 1} failed, retrying...")

        soup = BeautifulSoup(response.content, "html.parser")
        earnings_data = []

        # Parse earnings table
        table = soup.find("table", {"class": "W(100%)"})
        if not table:
            logger.warning("Could not find earnings table on Yahoo Finance")
            return pd.DataFrame(
                columns=["ticker", "earnings_date", "eps_estimate", "time_of_day"]
            )

        rows = table.find_all("tr")[1:]  # Skip header
        for row in rows[:50]:  # Limit to 50 entries
            try:
                cols = row.find_all("td")
                if len(cols) < 3:
                    continue

                ticker = cols[0].text.strip().upper()
                earnings_date_text = cols[1].text.strip()
                eps_text = cols[2].text.strip()

                # Parse date (e.g., "May 1, 2026")
                try:
                    earnings_date = datetime.strptime(
                        earnings_date_text, "%b %d, %Y"
                    ).date()
                except ValueError:
                    logger.debug(f"Could not parse earnings date: {earnings_date_text}")
                    continue

                # Parse EPS estimate
                eps_estimate = None
                if eps_text and eps_text != "N/A" and eps_text != "-":
                    try:
                        eps_estimate = float(eps_text)
                    except ValueError:
                        pass

                # Determine time of day (usually "Before Open" or "After Close")
                time_of_day = "unknown"
                if len(cols) > 3:
                    time_text = cols[3].text.strip().lower()
                    if "before" in time_text:
                        time_of_day = "pre-market"
                    elif "after" in time_text:
                        time_of_day = "after-hours"

                earnings_data.append(
                    {
                        "ticker": ticker,
                        "earnings_date": earnings_date.isoformat(),
                        "eps_estimate": eps_estimate,
                        "time_of_day": time_of_day,
                    }
                )
            except Exception as e:
                logger.debug(f"Error parsing earnings row: {e}")
                continue

        df = pd.DataFrame(earnings_data)
        logger.info(f"Fetched {len(df)} upcoming earnings in next {days_ahead} days")
        return df

    except Exception as e:
        logger.error(f"Error fetching earnings calendar: {e}")
        return pd.DataFrame(
            columns=["ticker", "earnings_date", "eps_estimate", "time_of_day"]
        )
