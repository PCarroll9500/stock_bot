#!/usr/bin/env python3
"""
show_positions.py — print current IBKR positions and account cash.

Usage:
    python scripts/show_positions.py
"""

import sys
from pathlib import Path

# Allow running from repo root without installing the package
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ib_insync import IB, Stock
from stock_bot.config.settings import ib_settings


def main():
    ib = IB()
    print(f"Connecting to {ib_settings.host}:{ib_settings.port} (account: {ib_settings.account}) …")
    ib.connect(ib_settings.host, ib_settings.port, clientId=ib_settings.client_id + 10, timeout=10)

    if not ib.isConnected():
        print("ERROR: could not connect to IBKR")
        sys.exit(1)

    ib.sleep(1)

    # ── Cash / account summary ──────────────────────────────────────────────
    account = ib_settings.account
    vals = {v.tag: v for v in ib.accountValues(account=account)}

    net_liq    = float(vals["NetLiquidation"].value)   if "NetLiquidation"   in vals else None
    cash       = float(vals["CashBalance"].value)       if "CashBalance"       in vals else None
    buying_pwr = float(vals["BuyingPower"].value)       if "BuyingPower"       in vals else None
    unrealized = float(vals["UnrealizedPnL"].value)     if "UnrealizedPnL"     in vals else None
    realized   = float(vals["RealizedPnL"].value)       if "RealizedPnL"       in vals else None

    print()
    print("=" * 52)
    print(f"  Account: {account}")
    print("=" * 52)
    print(f"  Net Liquidation : ${net_liq:>12,.2f}" if net_liq    is not None else "  Net Liquidation : —")
    print(f"  Cash Balance    : ${cash:>12,.2f}"    if cash       is not None else "  Cash Balance    : —")
    print(f"  Buying Power    : ${buying_pwr:>12,.2f}" if buying_pwr is not None else "  Buying Power    : —")
    print(f"  Unrealized P&L  : ${unrealized:>+12,.2f}" if unrealized is not None else "  Unrealized P&L  : —")
    print(f"  Realized P&L    : ${realized:>+12,.2f}"   if realized   is not None else "  Realized P&L    : —")
    print("=" * 52)

    # ── Open positions ───────────────────────────────────────────────────────
    positions = [p for p in ib.positions(account=account) if p.position != 0]

    if not positions:
        print("\n  No open positions.\n")
    else:
        print(f"\n  Open Positions ({len(positions)}):\n")
        print(f"  {'Ticker':<8}  {'Type':<5}  {'Qty':>8}  {'Avg Cost':>10}  {'Mkt Value':>12}  {'Unreal P&L':>12}")
        print(f"  {'-'*8}  {'-'*5}  {'-'*8}  {'-'*10}  {'-'*12}  {'-'*12}")

        total_market_value = 0.0
        total_unrealized   = 0.0

        for p in sorted(positions, key=lambda x: x.contract.symbol):
            symbol    = p.contract.symbol
            sec_type  = p.contract.secType
            qty       = p.position
            avg_cost  = p.avgCost
            mkt_val   = p.marketValue if hasattr(p, "marketValue") and p.marketValue else avg_cost * qty
            unreal    = p.unrealizedPNL if hasattr(p, "unrealizedPNL") and p.unrealizedPNL is not None else 0.0

            total_market_value += mkt_val or 0
            total_unrealized   += unreal  or 0

            print(
                f"  {symbol:<8}  {sec_type:<5}  {qty:>8.0f}  "
                f"${avg_cost:>9,.4f}  ${mkt_val:>11,.2f}  ${unreal:>+11,.2f}"
            )

        print(f"  {'-'*8}  {'-'*5}  {'-'*8}  {'-'*10}  {'-'*12}  {'-'*12}")
        print(f"  {'TOTAL':<8}  {'':5}  {'':8}  {'':10}  ${total_market_value:>11,.2f}  ${total_unrealized:>+11,.2f}")
        print()

    ib.disconnect()


if __name__ == "__main__":
    main()
