#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
V4-SIM beta
============

Purpose
-------
Backtest Strategy v1.0 on Binance USDⓈ-M Futures public historical data.

NO API KEY.
NO LIVE ORDERS.
NO ACCOUNT WRITES.

Strategy v1.0 (frozen rules)
-------------------
Universe:
    BTCUSDT / ETHUSDT only

Timeframes:
    4H = primary trend bias
    1H = pullback / core invalidation
    15m = entry trigger / pyramid / hedge trigger

Position unit:
    1 Unit = 80 USDT notional
    Leverage = 10x (used for margin reporting only)

Initial entry:
    - 4H confirmed bias
    - 1H price in pullback/support-resistance zone
    - 15m structure flips back into 4H direction
    - execute at next 15m open

Pyramiding:
    - add +1 Unit only when trade is already favorable
    - require a newly-confirmed 15m HL (long) / LH (short)
    - 4H bias still agrees
    - never average down a losing core position

Partial profit:
    - pre-entry confirmed 1H / 4H swing level is used as structural TP
    - if target is touched and >1 core unit remains, close the newest Unit
    - initial/core unit is left to run

Hedge:
    - ONLY for profitable trend positions
    - 15m flips against the core trend
    - 4H trend remains valid and 1H has not invalidated the core
    - hedge size = configured ratio of current core notional
    - remove hedge when 15m returns to the core trend

Stop / exit:
    - 1H structure flips against the core direction => exit
    - 4H bias flips to the opposite direction => exit
    - whole Trade Idea reaches -1% of account equity at trade start => hard exit
    - end of backtest => close remaining positions

Important
---------
This beta keeps the Strategy v1.0 trading rules frozen and upgrades only
the validation framework: full-year, independent 90-day segments,
LONG/SHORT statistics, market-regime statistics, and buy-and-hold benchmarks.
It is NOT a production trading system.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests


# ============================================================
# CONFIG
# ============================================================

BASE_URL = "https://fapi.binance.com"

SYMBOLS = ["BTCUSDT", "ETHUSDT"]

BAR_INTERVAL = "15m"
BAR_MS = 15 * 60 * 1000

UNIT_NOTIONAL = 80.0
LEVERAGE = 10.0
ACCOUNT_RISK = 0.01

EMA_FAST = 20
EMA_SLOW = 50
RSI_PERIOD = 14
ATR_PERIOD = 14

SWING_LEFT = 2
SWING_RIGHT = 2

# Conservative default for simulated market orders.
# Change this later to match your actual Binance fee tier.
TAKER_FEE_RATE = 0.0005

# Simulated execution slippage for market orders.
SLIPPAGE_BPS = 2.0

# Keep a little cooldown after an exit to reduce instant churn.
COOLDOWN_BARS = 4

# Pullback-zone tolerances.
PULLBACK_EMA_ATR_BELOW = 0.35
PULLBACK_EMA_ATR_ABOVE = 0.15
PULLBACK_SWING_ATR = 0.60

# Only hedge if core has at least this many Units.
MIN_UNITS_FOR_HEDGE = 2

CACHE_DIR = Path("backtest_cache")
OUTPUT_DIR = Path("backtest_output_beta")


# ============================================================
# HELPERS
# ============================================================

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def dt_to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def floor_15m(dt: datetime) -> datetime:
    minute = (dt.minute // 15) * 15
    return dt.replace(minute=minute, second=0, microsecond=0)


def slip_price(price: float, side: str) -> float:
    """
    side = BUY or SELL.
    Slippage is applied against the trader.
    """
    slip = SLIPPAGE_BPS / 10000.0
    if side == "BUY":
        return price * (1.0 + slip)
    return price * (1.0 - slip)


def fee_for_notional(notional: float) -> float:
    return abs(notional) * TAKER_FEE_RATE


# ============================================================
# BINANCE PUBLIC DATA
# ============================================================

def fetch_klines(
    symbol: str,
    start_ms: int,
    end_ms: int,
    session: requests.Session,
) -> pd.DataFrame:
    """
    Download 15m USDⓈ-M Futures klines with pagination.
    Public endpoint; no API key.
    """
    rows: List[list] = []
    cursor = start_ms

    while cursor <= end_ms:
        params = {
            "symbol": symbol,
            "interval": BAR_INTERVAL,
            "startTime": cursor,
            "endTime": end_ms,
            "limit": 1500,
        }
        r = session.get(
            BASE_URL + "/fapi/v1/klines",
            params=params,
            timeout=20,
        )
        r.raise_for_status()
        batch = r.json()

        if not batch:
            break

        rows.extend(batch)
        last_open = int(batch[-1][0])
        next_cursor = last_open + BAR_MS

        if next_cursor <= cursor:
            break

        cursor = next_cursor
        time.sleep(0.04)

    if not rows:
        raise RuntimeError(f"{symbol}: no kline data returned")

    df = pd.DataFrame(
        rows,
        columns=[
            "open_time",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "close_time",
            "quote_volume",
            "trades",
            "taker_buy_base",
            "taker_buy_quote",
            "ignore",
        ],
    )

    df = df[
        [
            "open_time",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "close_time",
        ]
    ].copy()

    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df["open_time"] = pd.to_numeric(df["open_time"], errors="coerce").astype("int64")
    df["close_time"] = pd.to_numeric(df["close_time"], errors="coerce").astype("int64")
    df = df.drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)

    # Only fully closed bars inside requested range.
    df = df[(df["open_time"] >= start_ms) & (df["close_time"] <= end_ms)].copy()
    return df.reset_index(drop=True)


def fetch_funding(
    symbol: str,
    start_ms: int,
    end_ms: int,
    session: requests.Session,
) -> pd.DataFrame:
    """
    Download funding-rate history.
    Public endpoint; no API key.
    """
    rows: List[dict] = []
    cursor = start_ms

    while cursor <= end_ms:
        params = {
            "symbol": symbol,
            "startTime": cursor,
            "endTime": end_ms,
            "limit": 1000,
        }
        r = session.get(
            BASE_URL + "/fapi/v1/fundingRate",
            params=params,
            timeout=20,
        )
        r.raise_for_status()
        batch = r.json()

        if not batch:
            break

        rows.extend(batch)
        last_t = int(batch[-1]["fundingTime"])
        next_cursor = last_t + 1

        if next_cursor <= cursor:
            break

        cursor = next_cursor

        if len(batch) < 1000:
            break

        time.sleep(0.04)

    if not rows:
        return pd.DataFrame(columns=["funding_time", "funding_rate", "mark_price"])

    out = pd.DataFrame(rows)
    out["funding_time"] = pd.to_numeric(out["fundingTime"], errors="coerce").astype("int64")
    out["funding_rate"] = pd.to_numeric(out["fundingRate"], errors="coerce")
    if "markPrice" in out.columns:
        out["mark_price"] = pd.to_numeric(out["markPrice"], errors="coerce")
    else:
        out["mark_price"] = math.nan

    out = out[["funding_time", "funding_rate", "mark_price"]]
    out = out.drop_duplicates("funding_time").sort_values("funding_time").reset_index(drop=True)
    return out


def load_or_fetch_symbol(
    symbol: str,
    start_ms: int,
    end_ms: int,
    refresh: bool = False,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = f"{start_ms}_{end_ms}"
    kline_path = CACHE_DIR / f"{symbol}_15m_{stamp}.csv"
    funding_path = CACHE_DIR / f"{symbol}_funding_{stamp}.csv"

    if not refresh and kline_path.exists():
        klines = pd.read_csv(kline_path)
    else:
        with requests.Session() as s:
            klines = fetch_klines(symbol, start_ms, end_ms, s)
        klines.to_csv(kline_path, index=False)

    if not refresh and funding_path.exists():
        funding = pd.read_csv(funding_path)
    else:
        with requests.Session() as s:
            funding = fetch_funding(symbol, start_ms, end_ms, s)
        funding.to_csv(funding_path, index=False)

    return klines, funding


# ============================================================
# INDICATORS + STRUCTURE (NO FUTURE LEAK)
# ============================================================

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    out["ema20"] = out["close"].ewm(span=EMA_FAST, adjust=False).mean()
    out["ema50"] = out["close"].ewm(span=EMA_SLOW, adjust=False).mean()

    delta = out["close"].diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)

    avg_gain = gain.ewm(alpha=1 / RSI_PERIOD, adjust=False, min_periods=RSI_PERIOD).mean()
    avg_loss = loss.ewm(alpha=1 / RSI_PERIOD, adjust=False, min_periods=RSI_PERIOD).mean()

    rs = avg_gain / avg_loss.replace(0, math.nan)
    out["rsi14"] = 100 - (100 / (1 + rs))
    out.loc[(avg_loss == 0) & (avg_gain > 0), "rsi14"] = 100.0
    out.loc[(avg_loss == 0) & (avg_gain == 0), "rsi14"] = 50.0

    prev_close = out["close"].shift(1)
    tr = pd.concat(
        [
            out["high"] - out["low"],
            (out["high"] - prev_close).abs(),
            (out["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    out["atr14"] = tr.ewm(
        alpha=1 / ATR_PERIOD,
        adjust=False,
        min_periods=ATR_PERIOD,
    ).mean()

    return out


def add_online_structure(
    df: pd.DataFrame,
    left: int = SWING_LEFT,
    right: int = SWING_RIGHT,
) -> pd.DataFrame:
    """
    A pivot at i is only confirmed at i + right.
    Therefore the current row never uses unknown future bars.
    """
    out = df.copy()

    structures: List[str] = []
    high_structures: List[Optional[str]] = []
    low_structures: List[Optional[str]] = []

    last_sh_price: List[Optional[float]] = []
    last_sh_time: List[Optional[int]] = []
    prev_sh_price_col: List[Optional[float]] = []

    last_sl_price: List[Optional[float]] = []
    last_sl_time: List[Optional[int]] = []
    prev_sl_price_col: List[Optional[float]] = []

    swing_highs: List[Tuple[int, float]] = []
    swing_lows: List[Tuple[int, float]] = []

    highs = out["high"].tolist()
    lows = out["low"].tolist()
    times = out["open_time"].astype("int64").tolist()

    for j in range(len(out)):
        pivot_i = j - right

        if pivot_i >= left and pivot_i + right <= j:
            hp = highs[pivot_i]
            lp = lows[pivot_i]

            left_highs = highs[pivot_i - left : pivot_i]
            right_highs = highs[pivot_i + 1 : pivot_i + 1 + right]
            left_lows = lows[pivot_i - left : pivot_i]
            right_lows = lows[pivot_i + 1 : pivot_i + 1 + right]

            if (
                len(left_highs) == left
                and len(right_highs) == right
                and all(hp > x for x in left_highs + right_highs)
            ):
                swing_highs.append((times[pivot_i], hp))

            if (
                len(left_lows) == left
                and len(right_lows) == right
                and all(lp < x for x in left_lows + right_lows)
            ):
                swing_lows.append((times[pivot_i], lp))

        hs = None
        ls = None
        structure = "UNKNOWN"

        if len(swing_highs) >= 2:
            prev_h = swing_highs[-2][1]
            latest_h = swing_highs[-1][1]
            hs = "HH" if latest_h > prev_h else ("LH" if latest_h < prev_h else "EH")

        if len(swing_lows) >= 2:
            prev_l = swing_lows[-2][1]
            latest_l = swing_lows[-1][1]
            ls = "HL" if latest_l > prev_l else ("LL" if latest_l < prev_l else "EL")

        if hs == "HH" and ls == "HL":
            structure = "BULLISH"
        elif hs == "LH" and ls == "LL":
            structure = "BEARISH"
        elif hs is not None and ls is not None:
            structure = "MIXED"

        structures.append(structure)
        high_structures.append(hs)
        low_structures.append(ls)

        last_sh_price.append(swing_highs[-1][1] if swing_highs else math.nan)
        last_sh_time.append(swing_highs[-1][0] if swing_highs else math.nan)
        prev_sh_price_col.append(swing_highs[-2][1] if len(swing_highs) >= 2 else math.nan)

        last_sl_price.append(swing_lows[-1][1] if swing_lows else math.nan)
        last_sl_time.append(swing_lows[-1][0] if swing_lows else math.nan)
        prev_sl_price_col.append(swing_lows[-2][1] if len(swing_lows) >= 2 else math.nan)

    out["structure"] = structures
    out["high_structure"] = high_structures
    out["low_structure"] = low_structures

    out["last_swing_high"] = last_sh_price
    out["last_swing_high_time"] = last_sh_time
    out["prev_swing_high"] = prev_sh_price_col

    out["last_swing_low"] = last_sl_price
    out["last_swing_low_time"] = last_sl_time
    out["prev_swing_low"] = prev_sl_price_col

    return out


def aggregate_from_15m(df15: pd.DataFrame, hours: int) -> pd.DataFrame:
    """
    Aggregate from the same 15m data so all timeframes are consistent.
    Only complete 1H/4H groups are kept.
    """
    expected = hours * 4
    bucket_ms = hours * 60 * 60 * 1000

    work = df15.copy()
    work["bucket"] = (work["open_time"] // bucket_ms) * bucket_ms

    agg = (
        work.groupby("bucket", as_index=False)
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
            count=("open_time", "count"),
        )
    )

    agg = agg[agg["count"] == expected].copy()
    agg.rename(columns={"bucket": "open_time"}, inplace=True)
    agg["close_time"] = agg["open_time"] + bucket_ms - 1
    agg.drop(columns=["count"], inplace=True)
    return agg.reset_index(drop=True)


def prepare_timeframe(df: pd.DataFrame) -> pd.DataFrame:
    return add_online_structure(add_indicators(df))


def merge_context(df15: pd.DataFrame, df1h: pd.DataFrame, df4h: pd.DataFrame) -> pd.DataFrame:
    """
    At each 15m close, attach only 1H/4H candles whose close_time is already known.
    """
    base = prepare_timeframe(df15).sort_values("close_time").copy()

    one = prepare_timeframe(df1h).sort_values("close_time").copy()
    four = prepare_timeframe(df4h).sort_values("close_time").copy()

    keep_cols = [
        "close_time",
        "open_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "ema20",
        "ema50",
        "rsi14",
        "atr14",
        "structure",
        "high_structure",
        "low_structure",
        "last_swing_high",
        "last_swing_high_time",
        "prev_swing_high",
        "last_swing_low",
        "last_swing_low_time",
        "prev_swing_low",
    ]

    one = one[keep_cols].add_prefix("h1_")
    four = four[keep_cols].add_prefix("h4_")

    base = pd.merge_asof(
        base,
        one,
        left_on="close_time",
        right_on="h1_close_time",
        direction="backward",
    )

    base = pd.merge_asof(
        base,
        four,
        left_on="close_time",
        right_on="h4_close_time",
        direction="backward",
    )

    return base.reset_index(drop=True)


# ============================================================
# STRATEGY RULES
# ============================================================

def four_hour_bias(row: pd.Series) -> str:
    required = [
        row.get("h4_structure"),
        row.get("h4_close"),
        row.get("h4_ema20"),
        row.get("h4_ema50"),
    ]
    if any(pd.isna(x) for x in required):
        return "NEUTRAL"

    if (
        row["h4_structure"] == "BULLISH"
        and row["h4_close"] > row["h4_ema20"] > row["h4_ema50"]
    ):
        return "LONG"

    if (
        row["h4_structure"] == "BEARISH"
        and row["h4_close"] < row["h4_ema20"] < row["h4_ema50"]
    ):
        return "SHORT"

    return "NEUTRAL"


def one_hour_pullback(row: pd.Series, bias: str) -> bool:
    required = [
        row.get("h1_close"),
        row.get("h1_ema20"),
        row.get("h1_ema50"),
        row.get("h1_atr14"),
    ]
    if any(pd.isna(x) for x in required):
        return False

    close = float(row["h1_close"])
    ema20 = float(row["h1_ema20"])
    ema50 = float(row["h1_ema50"])
    atr = float(row["h1_atr14"])

    if atr <= 0:
        return False

    zone_low = min(ema20, ema50) - PULLBACK_EMA_ATR_BELOW * atr
    zone_high = max(ema20, ema50) + PULLBACK_EMA_ATR_ABOVE * atr
    near_ema_zone = zone_low <= close <= zone_high

    if bias == "LONG":
        swing = row.get("h1_last_swing_low")
        near_structure = (
            not pd.isna(swing)
            and close >= float(swing) - 0.10 * atr
            and close <= float(swing) + PULLBACK_SWING_ATR * atr
        )
        # Do not call it a pullback if 1H has already clearly broken below
        # the latest structural low by a large margin.
        not_destroyed = pd.isna(swing) or close >= float(swing) - 0.35 * atr
        return bool((near_ema_zone or near_structure) and not_destroyed)

    if bias == "SHORT":
        swing = row.get("h1_last_swing_high")
        near_structure = (
            not pd.isna(swing)
            and close <= float(swing) + 0.10 * atr
            and close >= float(swing) - PULLBACK_SWING_ATR * atr
        )
        not_destroyed = pd.isna(swing) or close <= float(swing) + 0.35 * atr
        return bool((near_ema_zone or near_structure) and not_destroyed)

    return False


def entry_trigger(row: pd.Series, prev: Optional[pd.Series], bias: str) -> bool:
    if prev is None:
        return False

    if bias == "LONG":
        return bool(
            row["structure"] == "BULLISH"
            and prev["structure"] != "BULLISH"
            and row["close"] > row["ema20"]
        )

    if bias == "SHORT":
        return bool(
            row["structure"] == "BEARISH"
            and prev["structure"] != "BEARISH"
            and row["close"] < row["ema20"]
        )

    return False


def one_hour_invalidated(row: pd.Series, direction: str) -> bool:
    s = row.get("h1_structure")
    if direction == "LONG":
        return s == "BEARISH"
    return s == "BULLISH"


def opposite_15m(row: pd.Series, direction: str) -> bool:
    if direction == "LONG":
        return row["structure"] == "BEARISH"
    return row["structure"] == "BULLISH"


def aligned_15m(row: pd.Series, direction: str) -> bool:
    if direction == "LONG":
        return row["structure"] == "BULLISH"
    return row["structure"] == "BEARISH"


def classify_market_regime(row: pd.Series) -> str:
    """
    Regime label used ONLY for performance attribution.
    It does not change entries/exits in Strategy v1.0.

    BULL_TREND:
        4H structure bullish + price > EMA20 > EMA50
    BEAR_TREND:
        4H structure bearish + price < EMA20 < EMA50
    RANGE:
        4H EMA20/EMA50 compressed relative to 4H ATR,
        while structure is not cleanly directional
    TRANSITION:
        everything else / conflicting state
    """
    required = [
        row.get("h4_close"),
        row.get("h4_ema20"),
        row.get("h4_ema50"),
        row.get("h4_atr14"),
    ]
    if any(pd.isna(x) for x in required):
        return "UNKNOWN"

    close = float(row["h4_close"])
    ema20 = float(row["h4_ema20"])
    ema50 = float(row["h4_ema50"])
    atr = float(row["h4_atr14"])
    structure = row.get("h4_structure", "UNKNOWN")

    if structure == "BULLISH" and close > ema20 > ema50:
        return "BULL_TREND"

    if structure == "BEARISH" and close < ema20 < ema50:
        return "BEAR_TREND"

    if atr > 0:
        ema_gap_atr = abs(ema20 - ema50) / atr
        if ema_gap_atr <= 0.45 and structure in ("MIXED", "UNKNOWN"):
            return "RANGE"

    return "TRANSITION"


# ============================================================
# SIMULATION STATE
# ============================================================

@dataclass
class Unit:
    entry_price: float
    qty: float
    entry_notional: float
    opened_at: int


@dataclass
class Hedge:
    direction: str
    entry_price: float
    qty: float
    opened_at: int


@dataclass
class TradeState:
    trade_id: str
    symbol: str
    direction: str
    opened_at: int
    trade_start_equity: float
    entry_regime: str = "UNKNOWN"
    units: List[Unit] = field(default_factory=list)
    hedge: Optional[Hedge] = None
    fees: float = 0.0
    funding: float = 0.0
    realized_core: float = 0.0
    realized_hedge: float = 0.0
    max_units_seen: int = 0
    last_add_swing_time: Optional[int] = None
    tp_levels: List[float] = field(default_factory=list)
    tp_hit: List[bool] = field(default_factory=list)
    max_pnl_seen: float = -1e18
    min_pnl_seen: float = 1e18


@dataclass(frozen=True)
class SimConfig:
    max_units: int
    hedge_ratio: float


# ============================================================
# PNL / POSITION MATH
# ============================================================

def core_qty(trade: TradeState) -> float:
    return sum(u.qty for u in trade.units)


def avg_entry(trade: TradeState) -> float:
    q = core_qty(trade)
    if q <= 0:
        return math.nan
    total_entry_cost = sum(u.qty * u.entry_price for u in trade.units)
    return total_entry_cost / q


def core_unrealized(trade: TradeState, price: float) -> float:
    if trade.direction == "LONG":
        return sum((price - u.entry_price) * u.qty for u in trade.units)
    return sum((u.entry_price - price) * u.qty for u in trade.units)


def hedge_unrealized(trade: TradeState, price: float) -> float:
    h = trade.hedge
    if h is None:
        return 0.0

    if h.direction == "LONG":
        return (price - h.entry_price) * h.qty
    return (h.entry_price - price) * h.qty


def trade_total_pnl(trade: TradeState, price: float) -> float:
    return (
        trade.realized_core
        + trade.realized_hedge
        + trade.funding
        - trade.fees
        + core_unrealized(trade, price)
        + hedge_unrealized(trade, price)
    )


def current_core_notional(trade: TradeState, price: float) -> float:
    return core_qty(trade) * price


def current_hedge_notional(trade: TradeState, price: float) -> float:
    if trade.hedge is None:
        return 0.0
    return trade.hedge.qty * price


# ============================================================
# EXECUTION
# ============================================================

def open_unit(
    trade: TradeState,
    raw_price: float,
    timestamp: int,
    events: List[dict],
    action: str,
):
    side = "BUY" if trade.direction == "LONG" else "SELL"
    px = slip_price(raw_price, side)
    qty = UNIT_NOTIONAL / px
    trade.units.append(
        Unit(
            entry_price=px,
            qty=qty,
            entry_notional=UNIT_NOTIONAL,
            opened_at=timestamp,
        )
    )
    trade.fees += fee_for_notional(UNIT_NOTIONAL)
    trade.max_units_seen = max(trade.max_units_seen, len(trade.units))

    events.append(
        {
            "time": ms_to_iso(timestamp),
            "trade_id": trade.trade_id,
            "symbol": trade.symbol,
            "action": action,
            "direction": trade.direction,
            "price": px,
            "unit_notional": UNIT_NOTIONAL,
            "core_units_after": len(trade.units),
            "avg_entry_after": avg_entry(trade),
        }
    )


def close_one_newest_unit(
    trade: TradeState,
    raw_price: float,
    timestamp: int,
    events: List[dict],
    reason: str,
):
    if len(trade.units) <= 1:
        return

    u = trade.units.pop()
    side = "SELL" if trade.direction == "LONG" else "BUY"
    px = slip_price(raw_price, side)
    close_notional = u.qty * px

    pnl = (
        (px - u.entry_price) * u.qty
        if trade.direction == "LONG"
        else (u.entry_price - px) * u.qty
    )

    trade.realized_core += pnl
    trade.fees += fee_for_notional(close_notional)

    events.append(
        {
            "time": ms_to_iso(timestamp),
            "trade_id": trade.trade_id,
            "symbol": trade.symbol,
            "action": "REDUCE_1_UNIT",
            "direction": trade.direction,
            "reason": reason,
            "price": px,
            "realized_pnl": pnl,
            "core_units_after": len(trade.units),
            "avg_entry_after": avg_entry(trade),
        }
    )


def open_hedge(
    trade: TradeState,
    raw_price: float,
    timestamp: int,
    ratio: float,
    events: List[dict],
):
    if trade.hedge is not None or ratio <= 0:
        return

    core_notional = current_core_notional(trade, raw_price)
    hedge_notional = core_notional * ratio
    if hedge_notional <= 0:
        return

    hedge_direction = "SHORT" if trade.direction == "LONG" else "LONG"
    side = "SELL" if hedge_direction == "SHORT" else "BUY"
    px = slip_price(raw_price, side)
    qty = hedge_notional / px

    trade.hedge = Hedge(
        direction=hedge_direction,
        entry_price=px,
        qty=qty,
        opened_at=timestamp,
    )
    trade.fees += fee_for_notional(hedge_notional)

    events.append(
        {
            "time": ms_to_iso(timestamp),
            "trade_id": trade.trade_id,
            "symbol": trade.symbol,
            "action": "HEDGE_OPEN",
            "direction": hedge_direction,
            "price": px,
            "hedge_notional": hedge_notional,
            "hedge_ratio": ratio,
        }
    )


def close_hedge(
    trade: TradeState,
    raw_price: float,
    timestamp: int,
    events: List[dict],
    reason: str,
):
    h = trade.hedge
    if h is None:
        return

    side = "SELL" if h.direction == "LONG" else "BUY"
    px = slip_price(raw_price, side)
    close_notional = h.qty * px

    pnl = (
        (px - h.entry_price) * h.qty
        if h.direction == "LONG"
        else (h.entry_price - px) * h.qty
    )

    trade.realized_hedge += pnl
    trade.fees += fee_for_notional(close_notional)

    events.append(
        {
            "time": ms_to_iso(timestamp),
            "trade_id": trade.trade_id,
            "symbol": trade.symbol,
            "action": "HEDGE_CLOSE",
            "direction": h.direction,
            "reason": reason,
            "price": px,
            "realized_pnl": pnl,
        }
    )

    trade.hedge = None


def close_trade(
    trade: TradeState,
    raw_price: float,
    timestamp: int,
    events: List[dict],
    reason: str,
) -> dict:
    close_hedge(trade, raw_price, timestamp, events, "CORE_EXIT")

    side = "SELL" if trade.direction == "LONG" else "BUY"
    px = slip_price(raw_price, side)

    while trade.units:
        u = trade.units.pop()
        close_notional = u.qty * px

        pnl = (
            (px - u.entry_price) * u.qty
            if trade.direction == "LONG"
            else (u.entry_price - px) * u.qty
        )

        trade.realized_core += pnl
        trade.fees += fee_for_notional(close_notional)

    net = trade.realized_core + trade.realized_hedge + trade.funding - trade.fees

    events.append(
        {
            "time": ms_to_iso(timestamp),
            "trade_id": trade.trade_id,
            "symbol": trade.symbol,
            "action": "EXIT",
            "direction": trade.direction,
            "reason": reason,
            "price": px,
            "net_trade_pnl": net,
        }
    )

    return {
        "trade_id": trade.trade_id,
        "symbol": trade.symbol,
        "direction": trade.direction,
        "entry_regime": trade.entry_regime,
        "opened_at": ms_to_iso(trade.opened_at),
        "closed_at": ms_to_iso(timestamp),
        "exit_reason": reason,
        "max_units": trade.max_units_seen,
        "realized_core": trade.realized_core,
        "realized_hedge": trade.realized_hedge,
        "funding": trade.funding,
        "fees": trade.fees,
        "net_pnl": net,
        "max_trade_pnl_seen": trade.max_pnl_seen,
        "min_trade_pnl_seen": trade.min_pnl_seen,
        "trade_start_equity": trade.trade_start_equity,
        "return_on_start_equity_pct": (
            net / trade.trade_start_equity * 100
            if trade.trade_start_equity > 0
            else math.nan
        ),
    }


# ============================================================
# FUNDING
# ============================================================

def funding_events_map(funding: pd.DataFrame) -> Dict[int, Tuple[float, float]]:
    out: Dict[int, Tuple[float, float]] = {}
    if funding.empty:
        return out

    for _, r in funding.iterrows():
        t = int(r["funding_time"])
        rate = float(r["funding_rate"])
        mark = float(r["mark_price"]) if not pd.isna(r["mark_price"]) else math.nan
        out[t] = (rate, mark)

    return out


def apply_funding(
    trade: TradeState,
    rate: float,
    mark: float,
):
    if math.isnan(mark) or mark <= 0:
        return

    # Positive funding: longs pay, shorts receive.
    core_notional = core_qty(trade) * mark
    if trade.direction == "LONG":
        core_funding = -core_notional * rate
    else:
        core_funding = core_notional * rate

    hedge_funding = 0.0
    if trade.hedge is not None:
        hedge_notional = trade.hedge.qty * mark
        if trade.hedge.direction == "LONG":
            hedge_funding = -hedge_notional * rate
        else:
            hedge_funding = hedge_notional * rate

    trade.funding += core_funding + hedge_funding


# ============================================================
# STRUCTURAL TP LEVELS
# ============================================================

def initial_tp_levels(row: pd.Series, direction: str, entry_price: float) -> List[float]:
    candidates = []

    if direction == "LONG":
        for key in ["h1_last_swing_high", "h4_last_swing_high"]:
            v = row.get(key)
            if not pd.isna(v) and float(v) > entry_price:
                candidates.append(float(v))
        return sorted(set(candidates))

    for key in ["h1_last_swing_low", "h4_last_swing_low"]:
        v = row.get(key)
        if not pd.isna(v) and float(v) < entry_price:
            candidates.append(float(v))
    return sorted(set(candidates), reverse=True)


# ============================================================
# BACKTEST
# ============================================================

def run_backtest(
    datasets: Dict[str, pd.DataFrame],
    funding_maps: Dict[str, Dict[int, Tuple[float, float]]],
    config: SimConfig,
    starting_equity: float,
) -> Tuple[dict, List[dict], List[dict], List[dict]]:
    """
    Combined BTC + ETH account simulation.
    Both symbols share the same cash/equity pool.
    """
    cash = starting_equity
    active: Dict[str, TradeState] = {}
    cooldown: Dict[str, int] = {s: 0 for s in datasets}
    pending: Dict[str, Optional[dict]] = {s: None for s in datasets}

    trades: List[dict] = []
    events: List[dict] = []
    equity_curve: List[dict] = []

    trade_counter: Dict[str, int] = {s: 0 for s in datasets}

    # Use common timestamps. BTC/ETH Binance futures usually line up exactly.
    common = None
    for df in datasets.values():
        ts = set(df["open_time"].astype("int64").tolist())
        common = ts if common is None else common.intersection(ts)

    timeline = sorted(common or [])
    row_lookup = {
        symbol: df.set_index("open_time", drop=False)
        for symbol, df in datasets.items()
    }

    prev_rows: Dict[str, Optional[pd.Series]] = {s: None for s in datasets}

    last_equity = starting_equity
    peak_equity = starting_equity
    max_drawdown = 0.0

    for t in timeline:
        # -------------------------
        # 1) Execute actions queued from prior candle close
        # -------------------------
        for symbol, df_lookup in row_lookup.items():
            row = df_lookup.loc[t]
            raw_open = float(row["open"])

            action = pending.get(symbol)
            pending[symbol] = None

            if action is not None:
                kind = action["kind"]
                trade = active.get(symbol)

                if kind == "OPEN" and trade is None and cooldown[symbol] <= 0:
                    direction = action["direction"]
                    trade_counter[symbol] += 1
                    prefix = "BTC" if symbol.startswith("BTC") else "ETH"
                    trade_id = f"{prefix}-{trade_counter[symbol]:04d}"

                    source_row = action["signal_row"]

                    trade = TradeState(
                        trade_id=trade_id,
                        symbol=symbol,
                        direction=direction,
                        opened_at=t,
                        trade_start_equity=last_equity,
                        entry_regime=classify_market_regime(source_row),
                    )
                    active[symbol] = trade
                    open_unit(trade, raw_open, t, events, "OPEN_1_UNIT")
                    if direction == "LONG":
                        swing_t = source_row.get("last_swing_low_time")
                    else:
                        swing_t = source_row.get("last_swing_high_time")
                    trade.last_add_swing_time = (
                        int(swing_t) if not pd.isna(swing_t) else None
                    )

                    trade.tp_levels = initial_tp_levels(
                        source_row,
                        direction,
                        avg_entry(trade),
                    )
                    trade.tp_hit = [False] * len(trade.tp_levels)

                elif kind == "ADD" and trade is not None:
                    if len(trade.units) < config.max_units:
                        open_unit(trade, raw_open, t, events, "PYRAMID_ADD")
                        swing_t = action.get("swing_time")
                        if swing_t is not None:
                            trade.last_add_swing_time = int(swing_t)

                elif kind == "HEDGE_OPEN" and trade is not None:
                    open_hedge(
                        trade,
                        raw_open,
                        t,
                        config.hedge_ratio,
                        events,
                    )

                elif kind == "HEDGE_CLOSE" and trade is not None:
                    close_hedge(
                        trade,
                        raw_open,
                        t,
                        events,
                        action.get("reason", "TREND_RESUMED"),
                    )

                elif kind == "EXIT" and trade is not None:
                    record = close_trade(
                        trade,
                        raw_open,
                        t,
                        events,
                        action.get("reason", "EXIT"),
                    )
                    cash += record["net_pnl"]
                    trades.append(record)
                    active.pop(symbol, None)
                    cooldown[symbol] = COOLDOWN_BARS

        # -------------------------
        # 2) Funding inside this 15m bar
        # -------------------------
        bar_end = t + BAR_MS - 1

        for symbol, trade in list(active.items()):
            fmap = funding_maps.get(symbol, {})
            for ft, (rate, mark) in fmap.items():
                if t <= ft <= bar_end:
                    apply_funding(trade, rate, mark)

        # -------------------------
        # 3) Intrabar structural TP touches
        # -------------------------
        for symbol, trade in list(active.items()):
            row = row_lookup[symbol].loc[t]
            high = float(row["high"])
            low = float(row["low"])

            for idx, level in enumerate(trade.tp_levels):
                if trade.tp_hit[idx]:
                    continue
                if len(trade.units) <= 1:
                    break

                hit = (
                    high >= level
                    if trade.direction == "LONG"
                    else low <= level
                )

                if hit:
                    close_one_newest_unit(
                        trade,
                        level,
                        t,
                        events,
                        "STRUCTURAL_TP",
                    )
                    trade.tp_hit[idx] = True

        # -------------------------
        # 4) End-of-bar equity, hard-risk check, signals
        # -------------------------
        unrealized_total = 0.0

        for symbol, df_lookup in row_lookup.items():
            row = df_lookup.loc[t]
            close = float(row["close"])
            prev = prev_rows[symbol]

            if cooldown[symbol] > 0:
                cooldown[symbol] -= 1

            trade = active.get(symbol)
            bias = four_hour_bias(row)

            if trade is not None:
                trade_pnl = trade_total_pnl(trade, close)
                trade.max_pnl_seen = max(trade.max_pnl_seen, trade_pnl)
                trade.min_pnl_seen = min(trade.min_pnl_seen, trade_pnl)

                unrealized_total += core_unrealized(trade, close) + hedge_unrealized(trade, close)

                # Hard risk: whole Trade Idea max loss = 1% of account equity at trade start.
                hard_loss = -ACCOUNT_RISK * trade.trade_start_equity

                if trade_pnl <= hard_loss:
                    pending[symbol] = {
                        "kind": "EXIT",
                        "reason": "HARD_RISK_1PCT",
                    }
                    prev_rows[symbol] = row
                    continue

                # Opposite 4H bias = full exit.
                opposite_bias = (
                    (trade.direction == "LONG" and bias == "SHORT")
                    or (trade.direction == "SHORT" and bias == "LONG")
                )

                if opposite_bias:
                    pending[symbol] = {
                        "kind": "EXIT",
                        "reason": "4H_BIAS_FLIP",
                    }
                    prev_rows[symbol] = row
                    continue

                # 1H core invalidation = full exit.
                if one_hour_invalidated(row, trade.direction):
                    pending[symbol] = {
                        "kind": "EXIT",
                        "reason": "1H_STRUCTURE_INVALIDATED",
                    }
                    prev_rows[symbol] = row
                    continue

                # Hedge / unhedge logic.
                is_opp = opposite_15m(row, trade.direction)
                prev_is_opp = (
                    False
                    if prev is None
                    else opposite_15m(prev, trade.direction)
                )

                if (
                    trade.hedge is None
                    and config.hedge_ratio > 0
                    and len(trade.units) >= MIN_UNITS_FOR_HEDGE
                    and trade_pnl > 0
                    and is_opp
                    and not prev_is_opp
                    and bias
                    == ("LONG" if trade.direction == "LONG" else "SHORT")
                    and not one_hour_invalidated(row, trade.direction)
                ):
                    pending[symbol] = {"kind": "HEDGE_OPEN"}

                elif (
                    trade.hedge is not None
                    and aligned_15m(row, trade.direction)
                ):
                    pending[symbol] = {
                        "kind": "HEDGE_CLOSE",
                        "reason": "15M_TREND_RESUMED",
                    }

                # Pyramid add:
                # - only if currently favorable
                # - 4H still agrees
                # - 15m structure aligned
                # - new HL / LH has been confirmed since last add
                if (
                    pending[symbol] is None
                    and len(trade.units) < config.max_units
                    and core_unrealized(trade, close) > 0
                    and bias
                    == ("LONG" if trade.direction == "LONG" else "SHORT")
                    and aligned_15m(row, trade.direction)
                ):
                    if trade.direction == "LONG":
                        swing_t = row.get("last_swing_low_time")
                    else:
                        swing_t = row.get("last_swing_high_time")

                    if not pd.isna(swing_t):
                        swing_t = int(swing_t)
                        if (
                            trade.last_add_swing_time is None
                            or swing_t > trade.last_add_swing_time
                        ):
                            pending[symbol] = {
                                "kind": "ADD",
                                "swing_time": swing_t,
                            }

            else:
                # New trade only when flat and cooldown finished.
                if cooldown[symbol] <= 0:
                    if (
                        bias in ("LONG", "SHORT")
                        and one_hour_pullback(row, bias)
                        and entry_trigger(row, prev, bias)
                    ):
                        pending[symbol] = {
                            "kind": "OPEN",
                            "direction": bias,
                            "signal_row": row.copy(),
                        }

            prev_rows[symbol] = row

        # Account equity = cash + active trade total PnL
        active_total = 0.0
        for symbol, trade in active.items():
            close = float(row_lookup[symbol].loc[t]["close"])
            active_total += trade_total_pnl(trade, close)

        last_equity = cash + active_total
        peak_equity = max(peak_equity, last_equity)
        dd = (
            (peak_equity - last_equity) / peak_equity
            if peak_equity > 0
            else 0.0
        )
        max_drawdown = max(max_drawdown, dd)

        equity_curve.append(
            {
                "time": ms_to_iso(bar_end),
                "equity": last_equity,
                "cash": cash,
                "active_trades": len(active),
            }
        )

    # Close remaining positions at final close.
    if timeline:
        final_t = timeline[-1]
        for symbol, trade in list(active.items()):
            final_close = float(row_lookup[symbol].loc[final_t]["close"])
            record = close_trade(
                trade,
                final_close,
                final_t + BAR_MS - 1,
                events,
                "END_OF_TEST",
            )
            cash += record["net_pnl"]
            trades.append(record)
            active.pop(symbol, None)

    final_equity = cash
    net_profit = final_equity - starting_equity

    wins = [t for t in trades if t["net_pnl"] > 0]
    losses = [t for t in trades if t["net_pnl"] < 0]
    gross_profit = sum(t["net_pnl"] for t in wins)
    gross_loss = -sum(t["net_pnl"] for t in losses)

    profit_factor = (
        gross_profit / gross_loss
        if gross_loss > 0
        else (math.inf if gross_profit > 0 else math.nan)
    )

    summary = {
        "max_units": config.max_units,
        "hedge_ratio": config.hedge_ratio,
        "starting_equity": starting_equity,
        "final_equity": final_equity,
        "net_profit": net_profit,
        "return_pct": net_profit / starting_equity * 100,
        "max_drawdown_pct": max_drawdown * 100,
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": (len(wins) / len(trades) * 100) if trades else 0.0,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": profit_factor,
        "avg_trade_pnl": (
            sum(t["net_pnl"] for t in trades) / len(trades)
            if trades
            else 0.0
        ),
        "avg_win": (
            sum(t["net_pnl"] for t in wins) / len(wins)
            if wins
            else 0.0
        ),
        "avg_loss": (
            sum(t["net_pnl"] for t in losses) / len(losses)
            if losses
            else 0.0
        ),
        "total_fees": sum(t["fees"] for t in trades),
        "total_funding": sum(t["funding"] for t in trades),
        "total_hedge_pnl": sum(t["realized_hedge"] for t in trades),
    }

    return summary, trades, events, equity_curve


# ============================================================
# OUTPUT
# ============================================================

def write_csv(path: Path, rows: List[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return

    keys = []
    seen = set()
    for row in rows:
        for k in row.keys():
            if k not in seen:
                seen.add(k)
                keys.append(k)

    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for row in rows:
            w.writerow(row)


def safe_json_value(x):
    if isinstance(x, float) and math.isinf(x):
        return "Infinity"
    if isinstance(x, float) and math.isnan(x):
        return None
    return x


# ============================================================
# BETA VALIDATION HELPERS
# ============================================================

def summarize_trade_group(trades: List[dict]) -> dict:
    if not trades:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate_pct": 0.0,
            "net_pnl": 0.0,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
            "profit_factor": None,
            "avg_trade_pnl": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "fees": 0.0,
            "funding": 0.0,
            "hedge_pnl": 0.0,
        }

    wins = [t for t in trades if t["net_pnl"] > 0]
    losses = [t for t in trades if t["net_pnl"] < 0]

    gross_profit = sum(t["net_pnl"] for t in wins)
    gross_loss = -sum(t["net_pnl"] for t in losses)

    pf = None
    if gross_loss > 0:
        pf = gross_profit / gross_loss
    elif gross_profit > 0:
        pf = "Infinity"

    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": len(wins) / len(trades) * 100,
        "net_pnl": sum(t["net_pnl"] for t in trades),
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": pf,
        "avg_trade_pnl": sum(t["net_pnl"] for t in trades) / len(trades),
        "avg_win": (
            sum(t["net_pnl"] for t in wins) / len(wins)
            if wins
            else 0.0
        ),
        "avg_loss": (
            sum(t["net_pnl"] for t in losses) / len(losses)
            if losses
            else 0.0
        ),
        "fees": sum(t["fees"] for t in trades),
        "funding": sum(t["funding"] for t in trades),
        "hedge_pnl": sum(t["realized_hedge"] for t in trades),
    }


def trade_breakdowns(trades: List[dict]) -> Tuple[List[dict], List[dict]]:
    by_side: List[dict] = []
    for side in ("LONG", "SHORT"):
        group = [t for t in trades if t["direction"] == side]
        row = {"direction": side}
        row.update(summarize_trade_group(group))
        by_side.append(row)

    regimes = ["BULL_TREND", "BEAR_TREND", "RANGE", "TRANSITION", "UNKNOWN"]
    by_regime: List[dict] = []
    for regime in regimes:
        group = [t for t in trades if t.get("entry_regime", "UNKNOWN") == regime]
        row = {"entry_regime": regime}
        row.update(summarize_trade_group(group))
        by_regime.append(row)

    return by_side, by_regime


def filter_dataset_window(df: pd.DataFrame, start_ms: int, end_ms: int) -> pd.DataFrame:
    return df[
        (df["open_time"] >= start_ms)
        & (df["open_time"] <= end_ms)
    ].copy().reset_index(drop=True)


def filter_funding_window(
    fmap: Dict[int, Tuple[float, float]],
    start_ms: int,
    end_ms: int,
) -> Dict[int, Tuple[float, float]]:
    return {
        t: value
        for t, value in fmap.items()
        if start_ms <= t <= end_ms + BAR_MS
    }


def benchmark_returns(
    raw_15m: Dict[str, pd.DataFrame],
    start_ms: int,
    end_ms: int,
    starting_equity: float,
) -> List[dict]:
    """
    Simple price-return benchmarks.
    These are NOT leveraged and do not include fees.
    Equal-weight benchmark starts 50/50 BTC/ETH.
    """
    rows = []
    returns = []

    for symbol in SYMBOLS:
        df = raw_15m[symbol]
        sub = df[
            (df["open_time"] >= start_ms)
            & (df["open_time"] <= end_ms)
        ].copy()

        if sub.empty:
            continue

        start_price = float(sub.iloc[0]["open"])
        end_price = float(sub.iloc[-1]["close"])
        ret = end_price / start_price - 1.0
        returns.append(ret)

        rows.append(
            {
                "benchmark": f"{symbol}_BUY_HOLD",
                "start_price": start_price,
                "end_price": end_price,
                "return_pct": ret * 100,
                "ending_value_from_500_style_equity": starting_equity * (1 + ret),
            }
        )

    if returns:
        eq_ret = sum(returns) / len(returns)
        rows.append(
            {
                "benchmark": "BTC_ETH_50_50_BUY_HOLD",
                "start_price": None,
                "end_price": None,
                "return_pct": eq_ret * 100,
                "ending_value_from_500_style_equity": starting_equity * (1 + eq_ret),
            }
        )

    return rows


def build_non_overlapping_segments(
    full_start: datetime,
    full_end: datetime,
    segment_days: int,
) -> List[Tuple[str, datetime, datetime]]:
    """
    Build non-overlapping segments from oldest to newest.
    The last segment may be shorter than segment_days.
    """
    segments = []
    cursor = full_start
    idx = 1

    while cursor < full_end:
        seg_end = min(
            cursor + timedelta(days=segment_days) - timedelta(milliseconds=1),
            full_end,
        )
        segments.append((f"S{idx:02d}", cursor, seg_end))
        cursor = seg_end + timedelta(milliseconds=1)
        idx += 1

    return segments


def run_window_suite(
    window_name: str,
    start_dt: datetime,
    end_dt: datetime,
    full_datasets: Dict[str, pd.DataFrame],
    full_funding_maps: Dict[str, Dict[int, Tuple[float, float]]],
    raw_15m: Dict[str, pd.DataFrame],
    configs: List[SimConfig],
    starting_equity: float,
    save_detailed_runs: bool,
) -> Tuple[List[dict], List[dict], List[dict], List[dict]]:
    start_ms = dt_to_ms(start_dt)
    end_ms = dt_to_ms(end_dt)

    datasets = {
        symbol: filter_dataset_window(df, start_ms, end_ms)
        for symbol, df in full_datasets.items()
    }

    funding_maps = {
        symbol: filter_funding_window(fmap, start_ms, end_ms)
        for symbol, fmap in full_funding_maps.items()
    }

    if any(df.empty for df in datasets.values()):
        print(f"[WARN] {window_name}: no usable rows for one or more symbols")
        return [], [], [], []

    summary_rows: List[dict] = []
    side_rows: List[dict] = []
    regime_rows: List[dict] = []

    benchmark_rows = benchmark_returns(
        raw_15m,
        start_ms,
        end_ms,
        starting_equity,
    )
    for row in benchmark_rows:
        row["window"] = window_name
        row["period_start"] = start_dt.isoformat()
        row["period_end"] = end_dt.isoformat()

    print(f"\n{'=' * 72}")
    print(f"WINDOW {window_name}")
    print(f"{start_dt.isoformat()} -> {end_dt.isoformat()}")
    print(f"{'=' * 72}")

    for cfg in configs:
        summary, trades, events, equity_curve = run_backtest(
            datasets=datasets,
            funding_maps=funding_maps,
            config=cfg,
            starting_equity=starting_equity,
        )

        row = {
            "window": window_name,
            "period_start": start_dt.isoformat(),
            "period_end": end_dt.isoformat(),
            **summary,
        }
        summary_rows.append(row)

        by_side, by_regime = trade_breakdowns(trades)

        run_name = f"u{cfg.max_units}_h{int(cfg.hedge_ratio*100):02d}"

        for r in by_side:
            r.update(
                {
                    "window": window_name,
                    "run": run_name,
                    "max_units": cfg.max_units,
                    "hedge_ratio": cfg.hedge_ratio,
                }
            )
            side_rows.append(r)

        for r in by_regime:
            r.update(
                {
                    "window": window_name,
                    "run": run_name,
                    "max_units": cfg.max_units,
                    "hedge_ratio": cfg.hedge_ratio,
                }
            )
            regime_rows.append(r)

        print(
            f"  {run_name}: "
            f"Return {summary['return_pct']:+.2f}% | "
            f"MaxDD {summary['max_drawdown_pct']:.2f}% | "
            f"Trades {summary['trades']} | "
            f"Win {summary['win_rate_pct']:.1f}% | "
            f"PF {summary['profit_factor']}"
        )

        if save_detailed_runs:
            detail_dir = OUTPUT_DIR / "full_year_details"
            write_csv(detail_dir / f"trades_{run_name}.csv", trades)
            write_csv(detail_dir / f"events_{run_name}.csv", events)
            write_csv(detail_dir / f"equity_{run_name}.csv", equity_curve)

    return summary_rows, side_rows, regime_rows, benchmark_rows


def parse_args():
    p = argparse.ArgumentParser(
        description="V4-SIM beta multi-regime Binance Futures validation"
    )
    p.add_argument(
        "--days",
        type=int,
        default=365,
        help="Full validation history, default 365 days",
    )
    p.add_argument(
        "--segment-days",
        type=int,
        default=90,
        help="Independent non-overlapping segment length, default 90",
    )
    p.add_argument(
        "--equity",
        type=float,
        default=500.0,
        help="Starting equity for every independent window, default 500 USDT",
    )
    p.add_argument(
        "--refresh",
        action="store_true",
        help="Ignore local cache and re-download Binance public data",
    )
    return p.parse_args()


def main():
    args = parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Extra warmup is downloaded so the first validation window has indicator context.
    validation_end = floor_15m(utc_now()) - timedelta(milliseconds=1)
    validation_start = validation_end - timedelta(days=args.days)
    download_start = validation_start - timedelta(days=30)

    download_start_ms = dt_to_ms(download_start)
    validation_end_ms = dt_to_ms(validation_end)

    print("=" * 72)
    print("V4-SIM beta — Multi-Regime Validation")
    print("NO REAL ORDERS / NO API KEY")
    print("Strategy v1.0 rules are FROZEN.")
    print(f"Validation: {validation_start.isoformat()} -> {validation_end.isoformat()}")
    print(f"Warmup download begins: {download_start.isoformat()}")
    print(f"Starting equity per independent window: {args.equity:.2f} USDT")
    print("=" * 72)

    full_datasets: Dict[str, pd.DataFrame] = {}
    raw_15m: Dict[str, pd.DataFrame] = {}
    full_funding_maps: Dict[str, Dict[int, Tuple[float, float]]] = {}

    for symbol in SYMBOLS:
        print(f"\n[{symbol}] Download/cache data...")
        df15, funding = load_or_fetch_symbol(
            symbol,
            download_start_ms,
            validation_end_ms,
            refresh=args.refresh,
        )

        raw_15m[symbol] = df15.copy()

        df1h = aggregate_from_15m(df15, 1)
        df4h = aggregate_from_15m(df15, 4)
        merged = merge_context(df15, df1h, df4h)

        merged = merged[
            merged["h4_ema50"].notna()
            & merged["h1_ema50"].notna()
            & merged["ema50"].notna()
        ].copy()

        # Keep warmup context in dataframe. Window filtering happens later.
        full_datasets[symbol] = merged.reset_index(drop=True)
        full_funding_maps[symbol] = funding_events_map(funding)

        print(f"  15m raw candles: {len(df15):,}")
        print(f"  usable candles : {len(merged):,}")
        print(f"  funding rows   : {len(funding):,}")

    configs = [
        SimConfig(max_units=units, hedge_ratio=hedge)
        for units in (3, 5, 7)
        for hedge in (0.0, 0.25, 0.50)
    ]

    all_summary: List[dict] = []
    all_side: List[dict] = []
    all_regime: List[dict] = []
    all_benchmarks: List[dict] = []

    # 1) Full 365-day-style validation window.
    rows = run_window_suite(
        window_name=f"FULL_{args.days}D",
        start_dt=validation_start,
        end_dt=validation_end,
        full_datasets=full_datasets,
        full_funding_maps=full_funding_maps,
        raw_15m=raw_15m,
        configs=configs,
        starting_equity=args.equity,
        save_detailed_runs=True,
    )
    all_summary.extend(rows[0])
    all_side.extend(rows[1])
    all_regime.extend(rows[2])
    all_benchmarks.extend(rows[3])

    # 2) Independent non-overlapping segments.
    segments = build_non_overlapping_segments(
        validation_start,
        validation_end,
        args.segment_days,
    )

    for seg_name, seg_start, seg_end in segments:
        rows = run_window_suite(
            window_name=f"{seg_name}_{args.segment_days}D",
            start_dt=seg_start,
            end_dt=seg_end,
            full_datasets=full_datasets,
            full_funding_maps=full_funding_maps,
            raw_15m=raw_15m,
            configs=configs,
            starting_equity=args.equity,
            save_detailed_runs=False,
        )
        all_summary.extend(rows[0])
        all_side.extend(rows[1])
        all_regime.extend(rows[2])
        all_benchmarks.extend(rows[3])

    # Cross-window robustness by configuration.
    robustness_rows: List[dict] = []

    segment_summary = [
        r for r in all_summary
        if r["window"].startswith("S")
    ]

    for cfg in configs:
        rows = [
            r for r in segment_summary
            if r["max_units"] == cfg.max_units
            and r["hedge_ratio"] == cfg.hedge_ratio
        ]

        if not rows:
            continue

        returns = [r["return_pct"] for r in rows]
        dds = [r["max_drawdown_pct"] for r in rows]
        pfs = [
            float(r["profit_factor"])
            for r in rows
            if isinstance(r["profit_factor"], (int, float))
            and not math.isnan(float(r["profit_factor"]))
        ]

        robustness_rows.append(
            {
                "max_units": cfg.max_units,
                "hedge_ratio": cfg.hedge_ratio,
                "segments": len(rows),
                "profitable_segments": sum(1 for x in returns if x > 0),
                "losing_segments": sum(1 for x in returns if x < 0),
                "avg_segment_return_pct": sum(returns) / len(returns),
                "median_segment_return_pct": float(pd.Series(returns).median()),
                "worst_segment_return_pct": min(returns),
                "best_segment_return_pct": max(returns),
                "avg_segment_max_drawdown_pct": sum(dds) / len(dds),
                "worst_segment_max_drawdown_pct": max(dds),
                "avg_segment_profit_factor": (
                    sum(pfs) / len(pfs)
                    if pfs
                    else None
                ),
            }
        )

    write_csv(OUTPUT_DIR / "validation_summary.csv", all_summary)
    write_csv(OUTPUT_DIR / "side_breakdown.csv", all_side)
    write_csv(OUTPUT_DIR / "regime_breakdown.csv", all_regime)
    write_csv(OUTPUT_DIR / "benchmarks.csv", all_benchmarks)
    write_csv(OUTPUT_DIR / "robustness_by_config.csv", robustness_rows)

    report = {
        "strategy": "Strategy v1.0 — rules frozen",
        "validation_framework": "V4-SIM beta Multi-Regime Validation",
        "warning": (
            "This is historical validation, not proof of future profitability. "
            "Do not choose parameters only because they ranked first in this sample."
        ),
        "validation_start": validation_start.isoformat(),
        "validation_end": validation_end.isoformat(),
        "full_days": args.days,
        "segment_days": args.segment_days,
        "symbols": SYMBOLS,
        "starting_equity_per_window": args.equity,
        "unit_notional": UNIT_NOTIONAL,
        "leverage": LEVERAGE,
        "account_hard_risk_per_trade": ACCOUNT_RISK,
        "taker_fee_rate_assumption": TAKER_FEE_RATE,
        "slippage_bps_assumption": SLIPPAGE_BPS,
        "outputs": {
            "validation_summary": "validation_summary.csv",
            "side_breakdown": "side_breakdown.csv",
            "regime_breakdown": "regime_breakdown.csv",
            "benchmarks": "benchmarks.csv",
            "robustness_by_config": "robustness_by_config.csv",
            "full_year_details": "full_year_details/",
        },
        "robustness_by_config": [
            {k: safe_json_value(v) for k, v in row.items()}
            for row in robustness_rows
        ],
    }

    (OUTPUT_DIR / "validation_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("\n" + "=" * 72)
    print("V4-SIM beta DONE")
    print(f"Output folder: {OUTPUT_DIR}")
    print("Upload these first:")
    print(f"  {OUTPUT_DIR / 'validation_summary.csv'}")
    print(f"  {OUTPUT_DIR / 'robustness_by_config.csv'}")
    print(f"  {OUTPUT_DIR / 'side_breakdown.csv'}")
    print(f"  {OUTPUT_DIR / 'regime_breakdown.csv'}")
    print(f"  {OUTPUT_DIR / 'benchmarks.csv'}")
    print(f"  {OUTPUT_DIR / 'validation_report.json'}")
    print("=" * 72)


if __name__ == "__main__":
    main()
