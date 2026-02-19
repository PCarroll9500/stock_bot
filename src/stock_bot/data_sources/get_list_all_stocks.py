# src/stock_bot/data_sources/get_list_all_stocks.py

import io

import pandas as pd
import requests

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
NASDAQ_OTHERLISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

REQUEST_TIMEOUT = 10


def _fetch_csv(url: str) -> str:
    response = requests.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.text


def get_list_all_stocks() -> pd.DataFrame:
    """
    Get a list of all US-listed stocks from NASDAQ's symbol directories.
    Combines NASDAQ-listed (nasdaqlisted.txt) and other-exchange-listed
    (otherlisted.txt — NYSE, NYSE Arca, NYSE American, BATS) securities.

    Returns:
        pd.DataFrame: DataFrame containing `symbol`, `name`, and `exchange`.
    """
    # NASDAQ-listed stocks
    nasdaq_df = pd.read_csv(
        io.StringIO(_fetch_csv(NASDAQ_LISTED_URL)),
        sep="|",
        dtype=str,
        usecols=["Symbol", "Security Name"],
        na_values="",
    )
    nasdaq_df = nasdaq_df.rename(columns={"Symbol": "symbol", "Security Name": "name"})
    nasdaq_df["exchange"] = "NASDAQ"

    # NYSE / Amex / BATS / etc.
    other_df = pd.read_csv(
        io.StringIO(_fetch_csv(NASDAQ_OTHERLISTED_URL)),
        sep="|",
        dtype=str,
        usecols=["ACT Symbol", "Security Name", "Exchange"],
        na_values="",
    )
    other_df = other_df.rename(
        columns={
            "ACT Symbol": "symbol",
            "Security Name": "name",
            "Exchange": "exchange",
        }
    )

    df = pd.concat([nasdaq_df, other_df], ignore_index=True)

    # Both files end with a "File Creation Time: ..." footer row — drop it
    df = df[~df["symbol"].str.startswith("File Creation Time", na=True)]
    df = df.dropna(subset=["symbol"])

    return df


def main():
    df = get_list_all_stocks()
    print(df)


if __name__ == "__main__":
    main()
