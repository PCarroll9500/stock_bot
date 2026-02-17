# src/stock_bot/data_sources/get_list_all_stocks.py

import pandas as pd

NASDAQ_OTHERLISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"


def get_list_all_stocks() -> pd.DataFrame:
    """
    Get a list of all stocks from NASDAQ Other Listed Securities.

    Returns:
        pd.DataFrame: DataFrame containing `symbol`, `name`, and `exchange`.
    """
    df = pd.read_csv(
        NASDAQ_OTHERLISTED_URL,
        sep="|",
        dtype=str,
        usecols=["ACT Symbol", "Security Name", "Exchange"],
        na_values="",
    )

    df = df.rename(
        columns={
            "ACT Symbol": "symbol",
            "Security Name": "name",
            "Exchange": "exchange",
        }
    )

    return df   


def main():
    df = get_list_all_stocks()
    print(df)


if __name__ == "__main__":
    main()