import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

LOG_DIR = Path("logs/llm_inputs")
LOG_DIR.mkdir(parents=True, exist_ok=True)


def log_llm_input(
    ticker: str,
    current_price: float,
    news: list[dict],
    trends: str,
    earnings: Optional[dict] = None,
    sentiment: Optional[dict] = None,
    sec_filings: Optional[list[dict]] = None,
    web_news: Optional[list[dict]] = None,
    gpt_prompt: str = "",
) -> str:
    """Log all data sent to LLM for a ticker.

    Returns:
        Path to the log file.
    """
    try:
        date_dir = LOG_DIR / datetime.now().strftime("%Y-%m-%d")
        date_dir.mkdir(parents=True, exist_ok=True)

        log_data = {
            "ticker": ticker,
            "timestamp": datetime.now().isoformat(),
            "current_price": current_price,
            "news": news or [],
            "trends": trends,
            "earnings": earnings or {},
            "sentiment": sentiment or {},
            "sec_filings": sec_filings or [],
            "web_news": web_news or [],
            "gpt_prompt_sent": gpt_prompt,
        }

        log_file = date_dir / f"{ticker}_input.json"

        with open(log_file, "w") as f:
            json.dump(log_data, f, indent=2, default=str)

        logger.debug(f"Logged LLM input for {ticker} to {log_file}")
        return str(log_file)

    except Exception as e:
        logger.error(f"Error logging LLM input for {ticker}: {e}")
        return ""


def log_llm_output(
    ticker: str,
    gpt_response: dict,
    reasoning: str = "",
) -> str:
    """Log LLM output and reasoning for a ticker.

    Returns:
        Path to the log file.
    """
    try:
        date_dir = LOG_DIR / datetime.now().strftime("%Y-%m-%d")
        date_dir.mkdir(parents=True, exist_ok=True)

        log_data = {
            "ticker": ticker,
            "timestamp": datetime.now().isoformat(),
            "gpt_response": gpt_response,
            "reasoning": reasoning,
        }

        log_file = date_dir / f"{ticker}_output.json"

        with open(log_file, "w") as f:
            json.dump(log_data, f, indent=2, default=str)

        logger.debug(f"Logged LLM output for {ticker} to {log_file}")
        return str(log_file)

    except Exception as e:
        logger.error(f"Error logging LLM output for {ticker}: {e}")
        return ""


def get_latest_run_logs(date: Optional[str] = None) -> dict:
    """Get all LLM input/output logs for a specific date.

    Args:
        date: ISO format date string (YYYY-MM-DD). Defaults to today.

    Returns:
        Dictionary mapping ticker -> {input, output}
    """
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    date_dir = LOG_DIR / date
    if not date_dir.exists():
        logger.warning(f"No logs found for date {date}")
        return {}

    logs_by_ticker = {}

    for log_file in date_dir.glob("*.json"):
        ticker = log_file.stem.split("_")[0]

        if ticker not in logs_by_ticker:
            logs_by_ticker[ticker] = {}

        try:
            with open(log_file) as f:
                data = json.load(f)

            if "gpt_response" in data:
                logs_by_ticker[ticker]["output"] = data
            else:
                logs_by_ticker[ticker]["input"] = data
        except Exception as e:
            logger.error(f"Error reading log file {log_file}: {e}")

    return logs_by_ticker
