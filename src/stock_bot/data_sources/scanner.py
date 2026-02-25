# src/stock_bot/data_sources/scanner.py

import asyncio
import logging

from ib_insync import IB, ScannerSubscription

logger = logging.getLogger(__name__)


def get_scanner_universe(ib: IB, config: dict) -> list[dict]:
    """
    Run each scan_code from config, collect results, deduplicate.

    Returns list of {"ticker": str, "conId": int}.
    Filters: abovePrice=price_min, aboveVolume=volume_min, numberOfRows=max_per_scan.
    Skips scan codes that return errors gracefully.
    """
    scan_codes: list[str] = config.get("scan_codes", [])
    price_min: float = config.get("price_min", 5.0)
    volume_min: int = int(config.get("volume_min", 500000))
    max_per_scan: int = config.get("max_per_scan", 50)
    market_cap_max_b: float | None = config.get("market_cap_max_b")  # billions; IBKR uses millions

    seen_tickers: set[str] = set()
    universe: list[dict] = []

    for scan_code in scan_codes:
        sub = ScannerSubscription(
            instrument="STK",
            locationCode="STK.US.MAJOR",
            scanCode=scan_code,
            abovePrice=price_min,
            aboveVolume=volume_min,
            numberOfRows=max_per_scan,
        )
        if market_cap_max_b is not None:
            sub.marketCapBelow = market_cap_max_b * 1000  # convert billions → millions
        try:
            results = ib.reqScannerData(sub)
            added = 0
            for item in results:
                contract = item.contractDetails.contract
                ticker = contract.symbol
                con_id = contract.conId
                if ticker not in seen_tickers:
                    seen_tickers.add(ticker)
                    universe.append({"ticker": ticker, "conId": con_id})
                    added += 1
            logger.info("Scanner %s: %d results, %d new tickers", scan_code, len(results), added)
        except Exception:
            logger.warning("Scanner %s: skipping — scan returned an error", scan_code, exc_info=True)

    logger.info("Scanner universe total: %d unique tickers", len(universe))
    return universe


async def get_scanner_universe_async(ib: IB, config: dict) -> list[dict]:
    """
    Run all scan_codes concurrently, collect results, deduplicate.

    Returns list of {"ticker": str, "conId": int}.
    Runs all scan codes in parallel — typically cuts scan time from ~15 s to ~2 s.
    """
    scan_codes: list[str] = config.get("scan_codes", [])
    price_min: float = config.get("price_min", 5.0)
    volume_min: int = int(config.get("volume_min", 500000))
    max_per_scan: int = config.get("max_per_scan", 50)
    market_cap_max_b: float | None = config.get("market_cap_max_b")

    async def _scan_one(scan_code: str) -> tuple[str, list[dict]]:
        sub = ScannerSubscription(
            instrument="STK",
            locationCode="STK.US.MAJOR",
            scanCode=scan_code,
            abovePrice=price_min,
            aboveVolume=volume_min,
            numberOfRows=max_per_scan,
        )
        if market_cap_max_b is not None:
            sub.marketCapBelow = market_cap_max_b * 1000
        try:
            results = await ib.reqScannerDataAsync(sub)
            items = []
            for item in results:
                contract = item.contractDetails.contract
                items.append({"ticker": contract.symbol, "conId": contract.conId})
            logger.info("Scanner %s: %d results", scan_code, len(results))
            return scan_code, items
        except Exception:
            logger.warning("Scanner %s: skipping — scan returned an error", scan_code, exc_info=True)
            return scan_code, []

    all_results = await asyncio.gather(*[_scan_one(sc) for sc in scan_codes])

    seen_tickers: set[str] = set()
    universe: list[dict] = []
    for _scan_code, items in all_results:
        added = 0
        for item in items:
            if item["ticker"] not in seen_tickers:
                seen_tickers.add(item["ticker"])
                universe.append(item)
                added += 1
        logger.info("Scanner %s: %d new after dedup", _scan_code, added)

    logger.info("Scanner universe total: %d unique tickers", len(universe))
    return universe
