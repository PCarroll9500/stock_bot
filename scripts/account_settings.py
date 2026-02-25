#!/usr/bin/env python3
"""
account_settings.py — show account balances and explain what must be
changed manually in TWS / the IBKR portal.

Usage:
    python scripts/account_settings.py

What this script does:
  - Connects to IBKR and prints cash, net liquidation, and buying power.

What must be done MANUALLY (not accessible via API):
  1. Reset paper account to $10,000:
       TWS → top menu → Account → Paper Trading Account → Reset Paper Account
       (or IBKR Client Portal → Settings → Paper Trading → Reset)

  2. Switch from margin to cash account:
       IBKR Client Portal → Settings → Account Type
       Change from "Reg T Margin" to "Cash"
       Note: paper accounts are margin by default; the bot is already
       configured to use CashBalance (not BuyingPower) for order sizing,
       so margin will never be touched in practice.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ib_insync import IB
from stock_bot.config.settings import ib_settings


def main():
    ib = IB()
    print(f"Connecting to {ib_settings.host}:{ib_settings.port} …")
    ib.connect(ib_settings.host, ib_settings.port, clientId=ib_settings.client_id + 10, timeout=10)

    if not ib.isConnected():
        print("ERROR: could not connect to IBKR")
        sys.exit(1)

    ib.sleep(1)
    account = ib_settings.account
    vals = {v.tag: v.value for v in ib.accountValues(account=account) if v.currency == "USD"}

    cash       = float(vals.get("CashBalance",    0))
    net_liq    = float(vals.get("NetLiquidation", 0))
    buying_pwr = float(vals.get("BuyingPower",    0))
    margin_used = net_liq - cash  # roughly how much is on margin

    print()
    print("=" * 56)
    print(f"  Account: {account}")
    print("=" * 56)
    print(f"  Cash Balance     : ${cash:>12,.2f}   ← bot uses THIS")
    print(f"  Net Liquidation  : ${net_liq:>12,.2f}")
    print(f"  Buying Power     : ${buying_pwr:>12,.2f}   ← margin available (ignored)")
    print(f"  Margin in use    : ${margin_used:>12,.2f}")
    print("=" * 56)

    if abs(cash - 10_000) > 1:
        diff = cash - 10_000
        print(f"\n  ⚠  Cash is ${diff:+,.2f} vs $10,000 target.")
        print("     To reset paper account to exactly $10,000:")
        print("       TWS → Account menu → Paper Trading Account → Reset Paper Account")
        print("       — OR —")
        print("       IBKR Portal → Settings → Paper Trading → Reset\n")
    else:
        print(f"\n  ✓  Cash is at ${cash:,.2f} — close to $10,000 target.\n")

    if buying_pwr > cash * 1.05:
        print("  ⚠  Margin is enabled on this account (buying power > cash).")
        print("     To disable margin (optional — bot already ignores it):")
        print("       IBKR Client Portal → Settings → Account Type → Cash")
        print("     The bot is already configured to only use CashBalance")
        print("     for order sizing, so margin will never be touched.\n")
    else:
        print("  ✓  Buying power ≈ cash — margin does not appear to be active.\n")

    ib.disconnect()


if __name__ == "__main__":
    main()
