#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Read-only Strategy v2.0 LIVE preflight.
NO ORDERS are sent.

Checks:
- API authentication
- Futures canTrade
- Hedge Mode
- Single-Asset Mode
- BTC/ETH exchange filters
- current positions/open orders
- wallet/margin snapshot
"""

from binance_futures_live import BinanceFuturesLive

def main():
    c=BinanceFuturesLive()
    print("="*72)
    print("Strategy v2.2 Binance LIVE preflight — READ ONLY")
    print("="*72)

    print("Hedge Mode      :", c.position_mode())
    print("Multi-Assets    :", c.multi_assets_mode())

    acct=c.account()
    print("canTrade        :", acct.get("canTrade"))
    snap=c.margin_snapshot()
    print("Margin Balance  :", snap["margin_balance"], "USDT")
    print("Wallet Balance  :", snap["wallet_balance"], "USDT")
    print("Available       :", snap["available_balance"], "USDT")
    print("Initial Margin  :", snap["initial_margin"], "USDT")
    print("Maint Margin    :", snap["maint_margin"], "USDT")
    print("IM Utilization  :", f"{snap['initial_margin_util']*100:.2f}%")

    print("\nBTC/ETH filters:")
    for s in ("BTCUSDT","ETHUSDT"):
        print(" ", c.symbol_filter(s))

    print("\nNon-zero positions:")
    dirty=False
    for p in c.positions():
        if abs(float(p.get("positionAmt",0)))>0:
            dirty=True
            print(
                f"  {p.get('symbol')} {p.get('positionSide')} "
                f"qty={p.get('positionAmt')} entry={p.get('entryPrice')} "
                f"liq={p.get('liquidationPrice')}"
            )
    if not dirty:
        print("  none")

    print("\nOpen orders:")
    orders=c.open_orders()
    if not orders:
        print("  none")
    else:
        for o in orders:
            print(
                f"  {o.get('symbol')} {o.get('side')} {o.get('positionSide')} "
                f"{o.get('type')} qty={o.get('origQty')} "
                f"client={o.get('clientOrderId')}"
            )

    print("\nNO ORDERS WERE SENT.")
    print("="*72)

if __name__=="__main__":
    main()
