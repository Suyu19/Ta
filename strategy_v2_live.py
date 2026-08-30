#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Strategy v2.2 live engine — Max8 + G65 + BTC Range Alpha
=====================================================

Meta SAFE v1.1 + three virtual sleeves on ONE real Binance USDⓈ-M Futures wallet.

Trend child
-----------
- BTCUSDT / ETHUSDT
- Strategy v1.1 entry + ADX20
- Re-add into previously reached Pyramid layer requires Continuation Score >=3
- reference Unit = 140U notional at a 500U starting wallet
- Profit-Only sqrt compounding on NEW Trend trades only:
  Unit = base Unit * sqrt(max(1, current equity / starting equity))
- Unit is locked for the whole Trade, including all Pyramid Adds
- Max 8 units
- Structural TP
- 1H invalidation / 4H bias flip
- hard idea risk = 1% of trade-start strategy equity, scaled by locked Meta budget

FLEX child
----------
- BTCUSDT LONG inventory
- flexible pullback/stabilization entry
- 10x
- initial 12U margin / add 8U margin at a 500U starting wallet
- size scale = initial-capital scale * sqrt(current equity / starting equity)
- single-entry displayed ROE TP +7.5%
- 2~3 entries +5%
- 4+ entries: +5% close 30%, +10% close 35% of remaining, +15% close all
- Recovery dynamic SHORT hedge 15% -> 30% -> 50%
- no conventional stop loss

Range Alpha child
-----------------
- BTCUSDT LONG only
- New entries only while Meta == RANGE
- Quality Gate: low directional efficiency + adequate two-way volatility
- 48h range-low setup + 15m stabilization + 5m taker-buy confirmation
- Maker-first post-only entry, valid for 1 hour
- Fixed qty: BTC <100,000 => 0.003 BTC; >=100,000 => 0.002 BTC
- Maker TP +0.30%, hard stop -1.75%, max hold 8h
- No averaging/adds
- Can coexist with FLEX LONG and with Trend after entry
- Portfolio G65 does NOT scale Range Alpha; the global 50% margin guard still applies

Meta SAFE v1.1
--------------
TREND_BULL: Trend 100 / FLEX 0
TREND_BEAR: Trend 100 / FLEX 0
TRANS_BULL: Trend 50 / FLEX 50
TRANS_BEAR: Trend 70 / FLEX 0 / cash 30
RANGE:      Trend 40 / FLEX 60

Portfolio drawdown governor, applied ONLY when a sleeve opens:
DD <15%   x1.00
>=15%     x0.65

Important live differences from historical simulator
-----------------------------------------------------
- Trend/FLEX use actual MARKET fills; Range Alpha uses actual GTX post-only
  LIMIT entry/TP plus MARKET stop/time exits. Fees and exchange filters are live.
- Trend structural TP is acted on when the live poll observes price at/beyond
  the TP level. It cannot retroactively fill an intrabar spike that occurred
  between polls.
- Actual account equity/margin controls Meta DD and global new-risk guard.
- Strategy v2.2 requires a dedicated Futures account/wallet. Do not mix
  unrelated manual futures positions while this engine is active.
"""

from __future__ import annotations

import json
import math
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import numpy as np

import v5_forward_paper as base
import v16_dual_forward_paper as v16
from binance_futures_live import (
    BinanceFuturesLive,
    BinanceLiveError,
    BinanceReconciliationError,
    FillResult,
)


STRATEGY_VERSION = "2.2-MAX8-G65-RANGE-ALPHA-LIVE1"
SYMBOLS = ("BTCUSDT", "ETHUSDT")
TREND_REF_UNIT_500 = 140.0
TREND_MAX_UNITS = 8

# BTC Range Alpha — frozen research specification.
RANGE_ALPHA_SYMBOL = "BTCUSDT"
RANGE_ALPHA_PRICE_THRESHOLD = 100_000.0
RANGE_ALPHA_QTY_BELOW_100K = 0.003
RANGE_ALPHA_QTY_AT_OR_ABOVE_100K = 0.002
RANGE_ALPHA_ENTRY_OFFSET = 0.0020       # maker BUY 20 bps below signal close
RANGE_ALPHA_TP = 0.0030                 # +0.30% maker TP
RANGE_ALPHA_STOP = 0.0175               # -1.75% hard stop
RANGE_ALPHA_PENDING_MS = 60 * 60 * 1000
RANGE_ALPHA_MAX_HOLD_MS = 8 * 60 * 60 * 1000
RANGE_ALPHA_RANGE_POS_MAX = 0.175
RANGE_ALPHA_TAKER_BUY_MIN = 0.525
RANGE_ALPHA_ADX_MAX = 28.0
RANGE_ALPHA_SEP_ATR_MAX = 1.0
RANGE_ALPHA_EFF24_MAX = 0.12
RANGE_ALPHA_RV24_MIN = 0.014
RANGE_ALPHA_VOLRATIO_12_48_MAX = 1.20
RANGE_ALPHA_CROSS24_MIN = 12
RANGE_ALPHA_MIN_RANGE_AGE_H = 4.0
FLEX_LEVERAGE = 10.0
FLEX_INITIAL_MARGIN_500 = 12.0
FLEX_ADD_MARGIN_500 = 8.0
FLEX_SIZE_MULT = 1.7
FLEX_GROSS_CAP = 1.2
FLEX_MMR_RESEARCH = 0.004

META_WEIGHTS = {
    "TREND_BULL": (1.00, 0.00),
    "TREND_BEAR": (1.00, 0.00),
    "TRANS_BULL": (0.50, 0.50),
    "TRANS_BEAR": (0.70, 0.00),
    "RANGE":      (0.40, 0.60),
}


# ============================================================
# Serializable state
# ============================================================

@dataclass
class TrendUnit:
    entry_price: float
    qty: float
    entry_notional: float
    opened_at: int


@dataclass
class TrendTrade:
    trade_id: str
    symbol: str
    direction: str
    opened_at: int
    trade_start_equity: float
    meta_risk_mult: float
    # Locked once when this Trend Trade opens. Existing v2.0 state
    # files are migrated from their first filled Unit.
    locked_unit_notional: Optional[float] = None
    units: List[TrendUnit] = field(default_factory=list)
    fees: float = 0.0
    realized_core: float = 0.0
    max_units_seen: int = 0
    last_add_swing_time: Optional[int] = None
    tp_levels: List[float] = field(default_factory=list)
    tp_hit: List[bool] = field(default_factory=list)
    max_pnl_seen: float = -1e18
    min_pnl_seen: float = 1e18


@dataclass
class PendingAction:
    kind: str
    symbol: Optional[str] = None
    direction: Optional[str] = None
    reason: Optional[str] = None
    swing_time: Optional[int] = None
    signal_context: Optional[dict] = None
    meta_state: Optional[str] = None
    portfolio_dd: Optional[float] = None
    target_hedge: Optional[float] = None


@dataclass
class FlexCycle:
    cycle_id: str
    opened_at: int
    start_equity: float
    meta_risk_mult: float
    init_margin: float
    add_margin: float
    long_qty: float = 0.0
    long_avg: float = 0.0
    long_margin: float = 0.0
    short_qty: float = 0.0
    short_avg: float = 0.0
    fees: float = 0.0
    realized_pnl: float = 0.0
    entry_actions: int = 0
    max_entries_seen: int = 0
    max_entries_before_partial: int = 0
    tp_stage: int = 0
    last_ref_price: Optional[float] = None
    recovery: bool = False
    hedge_anchor: Optional[float] = None
    add_blocked: int = 0


@dataclass
class RangePendingEntry:
    cycle_id: str
    signal_open_time: int
    signal_close_time: int
    created_at_ms: int
    expires_at_ms: int
    qty: float
    signal_price: float
    limit_price: float
    client_order_id: str
    order_id: Optional[int] = None
    meta_state: str = "RANGE"
    quality: dict = field(default_factory=dict)
    # A GTX LIMIT can partially fill before our cancellation reaches Binance.
    # Track that real exchange quantity while the remainder is still pending so
    # aggregate position reconciliation remains correct across polls/restarts.
    partial_qty: float = 0.0


@dataclass
class RangeCycle:
    cycle_id: str
    opened_at: int
    initial_qty: float
    qty: float
    avg_entry: float
    entry_fee: float
    tp_price: float
    stop_price: float
    expires_at_ms: int
    entry_order_id: int
    entry_client_order_id: str
    tp_order_id: Optional[int] = None
    tp_client_order_id: Optional[str] = None
    tp_trade_ids_accounted: List[int] = field(default_factory=list)
    tp_filled_qty_accounted: float = 0.0
    tp_fill_quote_accounted: float = 0.0
    tp_commission_accounted: float = 0.0
    fees: float = 0.0
    realized_pnl: float = 0.0


@dataclass
class V2State:
    strategy_version: str = STRATEGY_VERSION
    created_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    updated_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))

    starting_equity: Optional[float] = None
    peak_equity: Optional[float] = None
    capital_scale: float = 1.0

    trend_lock: float = 0.0
    flex_lock: float = 0.0

    trend_active: Dict[str, Optional[TrendTrade]] = field(
        default_factory=lambda: {s: None for s in SYMBOLS}
    )
    trend_pending: Dict[str, Optional[PendingAction]] = field(
        default_factory=lambda: {s: None for s in SYMBOLS}
    )
    trend_cooldown: Dict[str, int] = field(
        default_factory=lambda: {s: 0 for s in SYMBOLS}
    )
    trend_counter: Dict[str, int] = field(
        default_factory=lambda: {s: 0 for s in SYMBOLS}
    )
    last_processed_closed_open_time: Dict[str, Optional[int]] = field(
        default_factory=lambda: {s: None for s in SYMBOLS}
    )

    flex_cycle: Optional[FlexCycle] = None
    flex_pending: Optional[PendingAction] = None
    flex_cooldown_until_ms: int = 0
    flex_last_processed_closed_open_time: Optional[int] = None

    range_pending: Optional[RangePendingEntry] = None
    range_cycle: Optional[RangeCycle] = None
    range_last_processed_closed_open_time: Optional[int] = None
    range_counter: int = 0

    order_seq: int = 0
    inflight: Optional[dict] = None

    halted: bool = False
    halt_reason: Optional[str] = None


def _trend_unit_from(d):
    return TrendUnit(**d)


def _trend_trade_from(d):
    if not d:
        return None
    x = dict(d)
    units = [_trend_unit_from(u) for u in x.get("units", [])]
    x["units"] = units

    # v2.0 live state did not store a locked Unit size.  During the v2.1
    # migration, preserve an already-open trade by adopting its first actual
    # Binance fill notional.  This prevents a deployment from resizing an
    # existing ETH/BTC campaign mid-trade.
    if not x.get("locked_unit_notional"):
        x["locked_unit_notional"] = (
            float(units[0].entry_notional) if units else None
        )

    return TrendTrade(**x)


def _pending_from(d):
    return PendingAction(**d) if d else None


def _flex_from(d):
    return FlexCycle(**d) if d else None


def _range_pending_from(d):
    return RangePendingEntry(**d) if d else None


def _range_cycle_from(d):
    return RangeCycle(**d) if d else None


def state_from_dict(d: dict) -> V2State:
    st = V2State()
    for key in (
        "strategy_version", "created_at_ms", "updated_at_ms",
        "starting_equity", "peak_equity", "capital_scale",
        "trend_lock", "flex_lock", "flex_cooldown_until_ms",
        "flex_last_processed_closed_open_time",
        "range_last_processed_closed_open_time", "range_counter",
        "order_seq", "inflight", "halted", "halt_reason",
    ):
        if key in d:
            setattr(st, key, d[key])

    st.trend_active = {
        s: _trend_trade_from(d.get("trend_active", {}).get(s))
        for s in SYMBOLS
    }
    st.trend_pending = {
        s: _pending_from(d.get("trend_pending", {}).get(s))
        for s in SYMBOLS
    }
    st.trend_cooldown = {
        s: int(d.get("trend_cooldown", {}).get(s, 0))
        for s in SYMBOLS
    }
    st.trend_counter = {
        s: int(d.get("trend_counter", {}).get(s, 0))
        for s in SYMBOLS
    }
    st.last_processed_closed_open_time = {
        s: d.get("last_processed_closed_open_time", {}).get(s)
        for s in SYMBOLS
    }
    st.flex_cycle = _flex_from(d.get("flex_cycle"))
    st.flex_pending = _pending_from(d.get("flex_pending"))
    st.range_pending = _range_pending_from(d.get("range_pending"))
    st.range_cycle = _range_cycle_from(d.get("range_cycle"))

    # State schema is backward-compatible; mark the migrated state as the current strategy version.
    st.strategy_version = STRATEGY_VERSION
    return st


# ============================================================
# Indicators for Meta / FLEX
# ============================================================

def _numeric_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    for c in ("open", "high", "low", "close", "volume"):
        if c in x:
            x[c] = pd.to_numeric(x[c], errors="coerce")
    x["open_time"] = pd.to_numeric(x["open_time"], errors="coerce").astype("int64")
    x["close_time"] = pd.to_numeric(x["close_time"], errors="coerce").astype("int64")
    return x


def _klines_to_df(raw) -> pd.DataFrame:
    df = pd.DataFrame(
        raw,
        columns=[
            "open_time","open","high","low","close","volume","close_time",
            "quote_volume","trades","taker_buy_base","taker_buy_quote","ignore",
        ],
    )
    return _numeric_ohlc(
        df[["open_time","open","high","low","close","volume","close_time"]]
    )


def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev).abs(),
            (df["low"] - prev).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False, min_periods=n).mean()


def _adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    atr = _atr(df, n).replace(0, pd.NA)
    plus_di = 100 * plus_dm.ewm(alpha=1/n, adjust=False, min_periods=n).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=1/n, adjust=False, min_periods=n).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, pd.NA)
    return dx.ewm(alpha=1/n, adjust=False, min_periods=n).mean()


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/n, adjust=False, min_periods=n).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/n, adjust=False, min_periods=n).mean()
    rs = gain / loss.replace(0, pd.NA)
    return 100 - (100 / (1 + rs))


def _add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    x["ema20"] = x["close"].ewm(span=20, adjust=False, min_periods=20).mean()
    x["ema50"] = x["close"].ewm(span=50, adjust=False, min_periods=50).mean()
    x["atr14"] = _atr(x, 14)
    x["adx14"] = _adx(x, 14)
    return x


def _dir(close, e20, e50) -> int:
    if any(pd.isna(v) for v in (close, e20, e50)):
        return 0
    if close > e20 > e50:
        return 1
    if close < e20 < e50:
        return -1
    return 0


def meta_state_from_frames(d1: pd.DataFrame, h4: pd.DataFrame) -> dict:
    d = _add_indicators(d1).dropna(subset=["ema50"]).copy()
    h = _add_indicators(h4).dropna(subset=["ema50","adx14","atr14"]).copy()
    if len(d) < 2 or len(h) < 5:
        return {"state":"RANGE","score":0,"aligned_dir":0}

    dsmall = d[["close_time","close","ema20","ema50"]].rename(
        columns={
            "close_time":"d_close_time",
            "close":"d_close",
            "ema20":"d_ema20",
            "ema50":"d_ema50",
        }
    ).sort_values("d_close_time")

    hs = h[[
        "close_time","close","ema20","ema50","atr14","adx14"
    ]].copy().sort_values("close_time")
    hs["sep_atr"] = (hs["ema20"] - hs["ema50"]).abs() / hs["atr14"].replace(0,pd.NA)
    hs["adx_lag2"] = hs["adx14"].shift(2)
    hs["sep_lag2"] = hs["sep_atr"].shift(2)

    merged = pd.merge_asof(
        hs,
        dsmall,
        left_on="close_time",
        right_on="d_close_time",
        direction="backward",
    )
    merged["d_dir"] = [
        _dir(c,a,b) for c,a,b in zip(merged.d_close, merged.d_ema20, merged.d_ema50)
    ]
    merged["h_dir"] = [
        _dir(c,a,b) for c,a,b in zip(merged.close, merged.ema20, merged.ema50)
    ]
    merged["aligned_dir"] = [
        hd if hd != 0 and hd == dd else 0
        for hd,dd in zip(merged.h_dir, merged.d_dir)
    ]

    merged["score"] = 0
    merged["score"] += (merged["aligned_dir"] != 0).astype(int) * 2
    merged["score"] += (merged["adx14"] >= 25).astype(int)
    merged["score"] += (merged["adx14"] >= merged["adx_lag2"]).astype(int)
    merged["score"] += (merged["sep_atr"] >= 0.75).astype(int)
    merged["score"] += (merged["sep_atr"] >= merged["sep_lag2"]).astype(int)

    active = False
    trend_dir = 0
    enter_count = 0
    exit_count = 0
    candidate_dir = 0
    state = "RANGE"
    last_emitted_state = None
    state_since_close_time_ms = None

    for _, r in merged.iterrows():
        aligned = int(r["aligned_dir"])
        score = int(r["score"])
        if not active:
            if aligned != 0 and score >= 5:
                if aligned == candidate_dir:
                    enter_count += 1
                else:
                    candidate_dir = aligned
                    enter_count = 1
                if enter_count >= 2:
                    active = True
                    trend_dir = aligned
                    exit_count = 0
            else:
                candidate_dir = 0
                enter_count = 0
        else:
            bad = (aligned != trend_dir) or (score <= 3)
            exit_count = exit_count + 1 if bad else 0
            if exit_count >= 2:
                active = False
                trend_dir = 0
                exit_count = 0
                candidate_dir = 0
                enter_count = 0

        if active:
            state = "TREND_BULL" if trend_dir == 1 else "TREND_BEAR"
        elif aligned == 1 and score >= 4:
            state = "TRANS_BULL"
        elif aligned == -1 and score >= 4:
            state = "TRANS_BEAR"
        else:
            state = "RANGE"

        if state != last_emitted_state:
            last_emitted_state = state
            state_since_close_time_ms = int(r["close_time"])

    last = merged.iloc[-1]
    aligned_ok = int(last["aligned_dir"]) != 0
    adx_ok = bool(last["adx14"] >= 25)
    adx_trend_ok = bool(
        not pd.isna(last["adx_lag2"])
        and last["adx14"] >= last["adx_lag2"]
    )
    sep_ok = bool(last["sep_atr"] >= 0.75)
    sep_trend_ok = bool(
        not pd.isna(last["sep_lag2"])
        and last["sep_atr"] >= last["sep_lag2"]
    )

    return {
        "state": state,
        "state_since_ms": state_since_close_time_ms,
        "score": int(last["score"]),
        "aligned_dir": int(last["aligned_dir"]),
        "conditions": {
            "d1_h4_aligned": {
                "ok": aligned_ok,
                "points": 2 if aligned_ok else 0,
                "direction": (
                    "BULL" if int(last["aligned_dir"]) == 1
                    else "BEAR" if int(last["aligned_dir"]) == -1
                    else "NONE"
                ),
            },
            "adx_ge_25": {
                "ok": adx_ok,
                "value": float(last["adx14"]),
                "threshold": 25.0,
            },
            "adx_non_declining": {
                "ok": adx_trend_ok,
                "value": float(last["adx14"]),
                "lag2": (
                    float(last["adx_lag2"])
                    if not pd.isna(last["adx_lag2"]) else None
                ),
            },
            "ema_separation_ge_075_atr": {
                "ok": sep_ok,
                "value": float(last["sep_atr"]),
                "threshold": 0.75,
            },
            "ema_separation_non_declining": {
                "ok": sep_trend_ok,
                "value": float(last["sep_atr"]),
                "lag2": (
                    float(last["sep_lag2"])
                    if not pd.isna(last["sep_lag2"]) else None
                ),
            },
        },
        "d1": {
            "close": float(last["d_close"]),
            "ema20": float(last["d_ema20"]),
            "ema50": float(last["d_ema50"]),
        },
        "h4": {
            "close": float(last["close"]),
            "ema20": float(last["ema20"]),
            "ema50": float(last["ema50"]),
            "atr14": float(last["atr14"]),
            "adx14": float(last["adx14"]),
            "adx_lag2": (
                float(last["adx_lag2"]) if not pd.isna(last["adx_lag2"]) else None
            ),
        },
    }


# ============================================================
# Strategy math
# ============================================================

def trend_qty(tr: TrendTrade) -> float:
    return sum(u.qty for u in tr.units)


def trend_avg(tr: TrendTrade) -> float:
    q = trend_qty(tr)
    if q <= 0:
        return math.nan
    return sum(u.entry_price*u.qty for u in tr.units) / q


def trend_unrealized(tr: TrendTrade, price: float) -> float:
    if tr.direction == "LONG":
        return sum((price-u.entry_price)*u.qty for u in tr.units)
    return sum((u.entry_price-price)*u.qty for u in tr.units)


def trend_pnl(tr: TrendTrade, price: float) -> float:
    return tr.realized_core - tr.fees + trend_unrealized(tr, price)


def flex_unrealized(fc: FlexCycle, price: float) -> float:
    return (
        fc.long_qty*(price-fc.long_avg)
        + fc.short_qty*(fc.short_avg-price)
    )


def flex_pnl(fc: FlexCycle, price: float) -> float:
    return fc.realized_pnl - fc.fees + flex_unrealized(fc, price)


def liq_price_long_cross(
    collateral_ex_flex_unrealized: float,
    long_qty: float,
    long_avg: float,
    short_qty: float,
    short_avg: float,
    mmr: float = FLEX_MMR_RESEARCH,
) -> float:
    if long_qty <= 0:
        return 0.0
    numerator = long_qty*long_avg - short_qty*short_avg - collateral_ex_flex_unrealized
    denominator = (long_qty-short_qty) - mmr*(long_qty+short_qty)
    if denominator <= 0:
        return 0.0
    return max(0.0, numerator/denominator)


# ============================================================
# Live engine
# ============================================================

class StrategyV2LiveEngine:
    def __init__(self, data_dir: Path | str):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.data_dir / "strategy_v2_live_state.json"
        self.status_file = self.data_dir / "strategy_v2_live_status.json"
        self.events_file = self.data_dir / "strategy_v2_live_events.jsonl"

        self.client = BinanceFuturesLive()
        self.is_fresh = not self.state_file.exists()
        self.state = self._load_state()

        self._last_daily4h_fetch_mono = 0.0
        self._cached_d1 = None
        self._cached_h4 = None
        self._last_account = None

        self._bootstrap()

    # --------------------------------------------------------
    # persistence / bootstrap
    # --------------------------------------------------------

    def _load_state(self) -> V2State:
        if not self.state_file.exists():
            return V2State()
        return state_from_dict(json.loads(self.state_file.read_text(encoding="utf-8")))

    @staticmethod
    def _json_default(obj):
        try:
            if hasattr(obj, "item"):
                return obj.item()
        except Exception:
            pass
        if isinstance(obj, pd.Timestamp):
            return obj.isoformat()
        raise TypeError(f"Not JSON serializable: {type(obj).__name__}")

    def save(self):
        self.state.updated_at_ms = int(time.time()*1000)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                asdict(self.state),
                ensure_ascii=False,
                indent=2,
                default=self._json_default,
            ),
            encoding="utf-8",
        )
        tmp.replace(self.state_file)

    def _event(self, event: dict):
        event = dict(event)
        event.setdefault("time_ms", int(time.time()*1000))
        event.setdefault("strategy", STRATEGY_VERSION)
        with self.events_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, default=self._json_default) + "\n")
        return event

    def _record_event(self, events, event: dict):
        e = self._event(event)
        if events is not None:
            events.append(e)
        return e

    def _known_range_client_ids(self) -> set[str]:
        out: set[str] = set()
        rp = self.state.range_pending
        if rp is not None and rp.client_order_id:
            out.add(rp.client_order_id)
        rc = self.state.range_cycle
        if rc is not None and rc.tp_client_order_id:
            out.add(rc.tp_client_order_id)
        return out

    def _bootstrap(self):
        allow_other = os.getenv(
            "LIVE_ALLOW_OTHER_FUTURES_POSITIONS", "false"
        ).strip().lower() in {"1","true","yes","on"}

        # Read-only safety checks happen BEFORE changing leverage/margin mode.
        if not self.client.position_mode():
            raise BinanceLiveError(
                "Strategy v2.2 requires Binance Futures Hedge Mode."
            )
        if self.client.multi_assets_mode():
            raise BinanceLiveError(
                "Strategy v2.2 requires Single-Asset Mode."
            )
        acct_perm = self.client.account_v2()
        if not self.client._as_bool(acct_perm.get("canTrade")):
            raise BinanceLiveError("Binance Futures account reports canTrade=false.")

        acct = self.client.account()
        positions = self.client.positions()
        orders = self.client.open_orders()

        if not allow_other:
            unrelated = [
                p for p in positions
                if p.get("symbol") not in SYMBOLS
                and abs(float(p.get("positionAmt",0))) > 0
            ]
            known_order_ids = self._known_range_client_ids()
            unexpected_orders = [
                o for o in orders
                if (o.get("clientOrderId") or "") not in known_order_ids
            ]
            if unrelated or unexpected_orders:
                raise BinanceLiveError(
                    "Strategy v2.2 expects a dedicated Futures account. "
                    "Unrelated positions or unknown open orders were found. "
                    "Only persistent Range Alpha orders recorded in strategy state "
                    "are allowed. Close/reconcile unknown orders first; "
                    "LIVE_ALLOW_OTHER_FUTURES_POSITIONS=true weakens this protection "
                    "and is not recommended."
                )

        if self.is_fresh:
            btc_eth_dirty = [
                p for p in positions
                if p.get("symbol") in SYMBOLS
                and abs(float(p.get("positionAmt",0))) > 0
            ]
            if btc_eth_dirty:
                raise BinanceLiveError(
                    "Fresh Strategy v2.2 LIVE state requires BTCUSDT/ETHUSDT "
                    "to be flat. The bot will not adopt pre-existing manual positions."
                )

        # Only after the account passes read-only safety checks do we normalize
        # BTC/ETH leverage and CROSS mode.
        acct = self.client.validate_environment(SYMBOLS, require_clean=False)

        if self.is_fresh:
            eq = float(acct.get("totalMarginBalance", 0.0))
            if eq <= 0:
                raise BinanceLiveError("Futures margin balance must be > 0.")
            self.state.starting_equity = eq
            self.state.peak_equity = eq
            self.state.capital_scale = eq / 500.0
            self.save()
            self._event({
                "type":"BOOTSTRAP",
                "equity":eq,
                "capital_scale":self.state.capital_scale,
            })
        else:
            if self.state.starting_equity is None:
                eq = float(acct.get("totalMarginBalance",0.0))
                self.state.starting_equity = eq
                self.state.peak_equity = max(eq, self.state.peak_equity or eq)
                self.state.capital_scale = eq/500.0
                self.save()

        if self.state.inflight:
            self._recover_inflight()

        # A Range Alpha LIMIT/TP may have filled while Railway was restarting.
        # Synchronize those persistent orders BEFORE comparing exchange position
        # quantities to the virtual sleeves.
        self._sync_range_orders(events=None, bootstrap=True)
        self.reconcile_or_halt()

    def _recover_inflight(self):
        inf = dict(self.state.inflight)
        symbol = inf["symbol"]
        cid = inf["client_order_id"]
        try:
            raw = self.client.query_order(symbol, client_order_id=cid)
        except Exception as exc:
            self.state.halted = True
            self.state.halt_reason = (
                "Unresolved inflight order after restart. "
                f"clientOrderId={cid}; query failed: {exc}"
            )
            self.save()
            raise BinanceReconciliationError(self.state.halt_reason)

        fill = self.client._normalize_fill(
            raw,
            symbol,
            inf["side"],
            inf["position_side"],
            float(inf["qty"]),
            cid,
        )
        if fill.executed_qty <= 0:
            self.state.halted = True
            self.state.halt_reason = (
                f"Inflight order {cid} exists but has no fill; manual review required."
            )
            self.save()
            raise BinanceReconciliationError(self.state.halt_reason)

        self._apply_fill(inf, fill)
        self.state.inflight = None
        self.save()
        self._event({
            "type":"RECOVERED_INFLIGHT",
            "client_order_id":cid,
            "order_id":fill.order_id,
            "executed_qty":fill.executed_qty,
            "avg_price":fill.avg_price,
        })

    # --------------------------------------------------------
    # Exchange reconciliation
    # --------------------------------------------------------

    def expected_positions(self) -> dict[tuple[str,str], float]:
        out = {(s,ps):0.0 for s in SYMBOLS for ps in ("LONG","SHORT")}
        for s,tr in self.state.trend_active.items():
            if tr is None:
                continue
            out[(s,tr.direction)] += trend_qty(tr)
        fc = self.state.flex_cycle
        if fc is not None:
            out[("BTCUSDT","LONG")] += fc.long_qty
            out[("BTCUSDT","SHORT")] += fc.short_qty
        rc = self.state.range_cycle
        if rc is not None:
            out[("BTCUSDT","LONG")] += rc.qty
        rp = self.state.range_pending
        if rp is not None and float(getattr(rp, "partial_qty", 0.0) or 0.0) > 0:
            out[("BTCUSDT","LONG")] += float(rp.partial_qty)
        return out

    def reconcile_or_halt(self):
        expected = self.expected_positions()
        real = self.client.position_map()
        problems = []

        for key, exp in expected.items():
            symbol, ps = key
            rp = real.get(key)
            actual = abs(float(rp.get("positionAmt",0.0))) if rp else 0.0
            step = self.client.symbol_filter(symbol).qty_step
            tol = max(step*1.5, max(exp, actual)*0.003)
            if abs(actual-exp) > tol:
                problems.append(
                    f"{symbol}:{ps} expected={exp:.12g}, actual={actual:.12g}"
                )

        if problems:
            self.state.halted = True
            self.state.halt_reason = (
                "Exchange/state reconciliation mismatch: " + "; ".join(problems)
            )
            self.save()
            raise BinanceReconciliationError(self.state.halt_reason)

        if self.state.halted and self.state.halt_reason and \
           self.state.halt_reason.startswith("Exchange/state reconciliation"):
            self.state.halted = False
            self.state.halt_reason = None
            self.save()

    # --------------------------------------------------------
    # Market data
    # --------------------------------------------------------

    def _fetch_higher_timeframes(self, now_ms: int):
        now_mono = time.monotonic()
        if (
            self._cached_d1 is not None
            and self._cached_h4 is not None
            and now_mono-self._last_daily4h_fetch_mono < 300
        ):
            return self._cached_d1, self._cached_h4

        d1 = _klines_to_df(
            self.client.public_get(
                "/fapi/v1/klines",
                {"symbol":"BTCUSDT","interval":"1d","limit":120},
            )
        )
        h4 = _klines_to_df(
            self.client.public_get(
                "/fapi/v1/klines",
                {"symbol":"BTCUSDT","interval":"4h","limit":220},
            )
        )
        d1 = d1[d1.close_time <= now_ms].reset_index(drop=True)
        h4 = h4[h4.close_time <= now_ms].reset_index(drop=True)
        self._cached_d1, self._cached_h4 = d1, h4
        self._last_daily4h_fetch_mono = now_mono
        return d1, h4

    def _market_context(self):
        now_ms = self.client.timestamp_ms()
        raw15 = {}
        contexts = {}
        forming = {}
        prices = {}

        for s in SYMBOLS:
            raw = base.fetch_live_15m(s)
            ctx, r = v16.prepare_dual_context(raw, now_ms)
            raw15[s] = r
            contexts[s] = ctx
            fbar = base.current_forming_bar(r, now_ms)
            forming[s] = fbar
            prices[s] = float(
                fbar["close"] if fbar is not None else ctx.iloc[-1]["close"]
            )

        d1,h4 = self._fetch_higher_timeframes(now_ms)
        meta = meta_state_from_frames(d1,h4)

        btc_closed = raw15["BTCUSDT"][
            raw15["BTCUSDT"]["close_time"] <= now_ms
        ].copy().reset_index(drop=True)
        btc_closed["rsi14"] = _rsi(btc_closed["close"],14)

        return {
            "now_ms":now_ms,
            "raw15":raw15,
            "contexts":contexts,
            "forming":forming,
            "prices":prices,
            "d1":_add_indicators(d1),
            "h4":_add_indicators(h4),
            "btc15":btc_closed,
            "meta":meta,
        }

    # --------------------------------------------------------
    # Meta / sizing
    # --------------------------------------------------------

    def _account_snapshot(self):
        snap = self.client.margin_snapshot()
        self._last_account = snap
        eq = snap["margin_balance"]
        if self.state.peak_equity is None:
            self.state.peak_equity = eq
        self.state.peak_equity = max(float(self.state.peak_equity), eq)
        dd = max(0.0, 1.0-eq/float(self.state.peak_equity))
        snap["portfolio_dd"] = dd
        return snap

    @staticmethod
    def governor(dd: float) -> float:
        # v2.2 G65: keep full sleeve allocation below 15% portfolio DD;
        # once DD reaches 15%, cut NEW sleeve risk to 65%.
        if dd >= .15: return .65
        return 1.0

    def desired_lock(self, meta_state: str, sleeve: str, dd: float) -> float:
        wt,wf = META_WEIGHTS.get(meta_state,(0.0,0.0))
        desired = wt if sleeve=="trend" else wf
        desired *= self.governor(dd)
        other = self.state.flex_lock if sleeve=="trend" else self.state.trend_lock
        return max(0.0,min(desired,1.0-other))

    def trend_unit_notional(
        self,
        risk_mult: float,
        current_equity: Optional[float] = None,
    ) -> float:
        """Profit-Only sqrt Trend sizing for a NEW Trend Trade.

        The base reference is still 140U per 500U of starting capital.
        Equity below the Strategy-v2 starting equity never shrinks the base
        Unit.  Only accumulated profit increases future NEW-Trade Units.

        The returned value must be stored on TrendTrade and reused unchanged
        by every Pyramid Add belonging to that Trade.
        """
        starting = max(float(self.state.starting_equity or 500.0), 1e-9)
        equity = float(current_equity if current_equity is not None else starting)
        growth = math.sqrt(max(1.0, equity / starting))
        base_unit = TREND_REF_UNIT_500 * float(self.state.capital_scale)
        return base_unit * growth * float(risk_mult)

    def flex_sizes(self, equity: float, risk_mult: float) -> tuple[float,float]:
        base_scale = self.state.capital_scale
        growth = math.sqrt(
            max(equity,1e-9) / max(self.state.starting_equity or equity,1e-9)
        )
        return (
            FLEX_INITIAL_MARGIN_500*base_scale*growth*FLEX_SIZE_MULT*risk_mult,
            FLEX_ADD_MARGIN_500*base_scale*growth*FLEX_SIZE_MULT*risk_mult,
        )

    # --------------------------------------------------------
    # Order submission / crash-safe journal
    # --------------------------------------------------------

    def _new_client_id(self, kind: str) -> str:
        self.state.order_seq += 1
        short = {
            "trend_open":"to","trend_add":"ta","trend_reduce":"tr","trend_exit":"tx",
            "flex_open":"fo","flex_add":"fa","flex_close":"fc","flex_hedge":"fh",
            "range_entry":"re","range_tp":"rt","range_exit":"rx",
        }.get(kind,"x")
        # <= 36 chars, valid Binance charset
        return f"s2-{self.state.order_seq:08d}-{short}-{uuid.uuid4().hex[:8]}"

    def _submit(self, intent: dict) -> FillResult:
        cid = self._new_client_id(intent["kind"])
        intent = dict(intent)
        intent["client_order_id"] = cid
        self.state.inflight = intent
        self.save()

        fill = self.client.market_order(
            symbol=intent["symbol"],
            side=intent["side"],
            position_side=intent["position_side"],
            qty=float(intent["qty"]),
            client_order_id=cid,
            reducing=bool(intent["reducing"]),
            reference_price=float(intent.get("reference_price") or 0) or None,
        )

        self._apply_fill(intent, fill)
        self.state.inflight = None
        self.save()
        return fill

    def _apply_fill(self, intent: dict, fill: FillResult):
        kind = intent["kind"]
        s = intent["symbol"]
        qty = fill.executed_qty
        px = fill.avg_price
        fee = fill.commission

        if px <= 0 or qty <= 0:
            raise BinanceLiveError(
                f"Cannot apply fill {fill.client_order_id}: qty={qty}, avg={px}"
            )

        if kind in ("trend_open","trend_add"):
            tr = self.state.trend_active[s]
            if kind=="trend_open" and tr is None:
                c = intent["trade_create"]
                tr = TrendTrade(**c)
                tr.units = []
                self.state.trend_active[s] = tr
            if tr is None:
                raise BinanceLiveError("Trend fill has no active TrendTrade.")
            tr.units.append(
                TrendUnit(
                    entry_price=px,
                    qty=qty,
                    entry_notional=fill.quote_qty or qty*px,
                    opened_at=int(intent["opened_at"]),
                )
            )
            if not tr.locked_unit_notional:
                tr.locked_unit_notional = float(tr.units[0].entry_notional)
            tr.fees += fee
            tr.max_units_seen=max(tr.max_units_seen,len(tr.units))
            if intent.get("swing_time") is not None:
                tr.last_add_swing_time=int(intent["swing_time"])

        elif kind=="trend_reduce":
            tr=self.state.trend_active[s]
            if tr is None:
                raise BinanceLiveError("Trend reduce fill has no trade.")
            unit_index=int(intent["unit_index"])
            if unit_index<0:
                unit_index=len(tr.units)+unit_index
            unit=tr.units[unit_index]
            close_qty=min(qty,unit.qty)
            pnl=(
                (px-unit.entry_price)*close_qty
                if tr.direction=="LONG"
                else (unit.entry_price-px)*close_qty
            )
            tr.realized_core+=pnl
            tr.fees+=fee
            unit.qty-=close_qty
            if unit.qty <= self.client.symbol_filter(s).qty_step/2:
                tr.units.pop(unit_index)

        elif kind=="trend_exit":
            tr=self.state.trend_active[s]
            if tr is None:
                raise BinanceLiveError("Trend exit fill has no trade.")
            remaining=qty
            # Any exchange-side aggregate close is allocated FIFO across this
            # virtual Trend trade's own units.
            for unit in list(tr.units):
                if remaining<=0:break
                q=min(unit.qty,remaining)
                pnl=(
                    (px-unit.entry_price)*q
                    if tr.direction=="LONG"
                    else (unit.entry_price-px)*q
                )
                tr.realized_core+=pnl
                unit.qty-=q
                remaining-=q
            tr.units=[u for u in tr.units if u.qty>self.client.symbol_filter(s).qty_step/2]
            tr.fees+=fee
            if not tr.units:
                self.state.trend_active[s]=None
                self.state.trend_cooldown[s]=base.sim.COOLDOWN_BARS

        elif kind in ("flex_open","flex_add"):
            fc=self.state.flex_cycle
            if kind=="flex_open" and fc is None:
                fc=FlexCycle(**intent["cycle_create"])
                self.state.flex_cycle=fc
            if fc is None:
                raise BinanceLiveError("Flex open/add fill has no cycle.")
            nq=fc.long_qty+qty
            fc.long_avg=(
                (fc.long_avg*fc.long_qty+px*qty)/nq if nq else 0.0
            )
            fc.long_qty=nq
            margin=(fill.quote_qty or qty*px)/FLEX_LEVERAGE
            fc.long_margin+=margin
            fc.fees+=fee
            fc.entry_actions+=1
            fc.max_entries_seen=max(fc.max_entries_seen,fc.entry_actions)
            fc.max_entries_before_partial=max(
                fc.max_entries_before_partial,fc.entry_actions
            )
            fc.last_ref_price=float(intent.get("reference_price") or px)

        elif kind=="flex_close":
            fc=self.state.flex_cycle
            if fc is None:
                raise BinanceLiveError("Flex close fill has no cycle.")
            q=min(qty,fc.long_qty)
            pnl=q*(px-fc.long_avg)
            oldq=fc.long_qty
            fc.realized_pnl+=pnl
            fc.fees+=fee
            fc.long_qty-=q
            if oldq>0:
                fc.long_margin*=max(0.0,fc.long_qty/oldq)
            if fc.long_qty <= self.client.symbol_filter("BTCUSDT").qty_step/2:
                fc.long_qty=0;fc.long_avg=0;fc.long_margin=0

        elif kind=="flex_hedge":
            fc=self.state.flex_cycle
            if fc is None:
                raise BinanceLiveError("Flex hedge fill has no cycle.")
            action=intent["hedge_action"]
            if action=="increase":
                nq=fc.short_qty+qty
                fc.short_avg=(
                    (fc.short_avg*fc.short_qty+px*qty)/nq if nq else 0.0
                )
                fc.short_qty=nq
                fc.fees+=fee
            else:
                q=min(qty,fc.short_qty)
                fc.realized_pnl += q*(fc.short_avg-px)
                fc.short_qty-=q
                fc.fees+=fee
                if fc.short_qty <= self.client.symbol_filter("BTCUSDT").qty_step/2:
                    fc.short_qty=0;fc.short_avg=0

        elif kind=="range_exit":
            rc=self.state.range_cycle
            if rc is None:
                raise BinanceLiveError("Range Alpha exit fill has no active cycle.")
            q=min(qty,rc.qty)
            rc.realized_pnl += q*(px-rc.avg_entry)
            rc.fees += fee
            rc.qty -= q
            step=self.client.symbol_filter(RANGE_ALPHA_SYMBOL).qty_step
            if rc.qty <= step/2:
                rc.qty=0.0
                self.state.range_cycle=None

    # --------------------------------------------------------
    # Trend order helpers
    # --------------------------------------------------------

    def _open_trend(self, s, direction, row, meta, account, execution_ref_price, events):
        if self.state.trend_active[s] is not None:
            return
        if not any(self.state.trend_active.values()):
            self.state.trend_lock = self.desired_lock(
                meta["state"],"trend",account["portfolio_dd"]
            )
        if self.state.trend_lock <= 0:
            return

        notional=self.trend_unit_notional(
            self.state.trend_lock, account["margin_balance"]
        )
        signal_price=float(row["close"])
        price=float(execution_ref_price)
        qty=self.client.qty_for_notional(s,notional,price)
        if qty<=0:
            return

        self.state.trend_counter[s]+=1
        trade_id=f"{s[:3]}-V2-{self.state.trend_counter[s]:05d}"
        swing=row.get(
            "last_swing_low_time" if direction=="LONG" else "last_swing_high_time"
        )
        swing=int(swing) if swing is not None and not pd.isna(swing) else None
        tps=base.initial_tp_levels(row,direction,signal_price)

        create={
            "trade_id":trade_id,
            "symbol":s,
            "direction":direction,
            "opened_at":self.client.timestamp_ms(),
            "trade_start_equity":account["margin_balance"],
            "meta_risk_mult":self.state.trend_lock,
            "locked_unit_notional":notional,
            "fees":0.0,"realized_core":0.0,"max_units_seen":0,
            "last_add_swing_time":swing,
            "tp_levels":[float(x) for x in tps],
            "tp_hit":[False]*len(tps),
            "max_pnl_seen":-1e18,"min_pnl_seen":1e18,
        }

        fill=self._submit({
            "kind":"trend_open",
            "symbol":s,
            "side":"BUY" if direction=="LONG" else "SELL",
            "position_side":direction,
            "qty":qty,
            "reducing":False,
            "reference_price":price,
            "opened_at":self.client.timestamp_ms(),
            "trade_create":create,
            "swing_time":swing,
        })
        events.append(self._event({
            "type":"TREND_OPEN","symbol":s,"direction":direction,
            "meta_state":meta["state"],"meta_score":meta["score"],
            "risk_mult":self.state.trend_lock,
            "locked_unit_notional":notional,
            "notional":fill.quote_qty or fill.executed_qty*fill.avg_price,
            "qty":fill.executed_qty,"price":fill.avg_price,
            "order_id":fill.order_id,"client_order_id":fill.client_order_id,
            "trade_id":trade_id,
        }))

    def _add_trend(self,s,tr,swing_time,price,events):
        if tr.locked_unit_notional:
            notional = float(tr.locked_unit_notional)
        elif tr.units:
            # Backward-compatibility safety for an already-open v2.0 Trade.
            notional = float(tr.units[0].entry_notional)
            tr.locked_unit_notional = notional
        else:
            # Defensive fallback only; normally every active Trade has Unit 1.
            notional = self.trend_unit_notional(
                tr.meta_risk_mult, tr.trade_start_equity
            )
            tr.locked_unit_notional = notional

        qty=self.client.qty_for_notional(s,notional,price)
        if qty<=0:return
        fill=self._submit({
            "kind":"trend_add","symbol":s,
            "side":"BUY" if tr.direction=="LONG" else "SELL",
            "position_side":tr.direction,"qty":qty,"reducing":False,
            "reference_price":price,"opened_at":self.client.timestamp_ms(),
            "swing_time":swing_time,
        })
        # --- Strategy v2.x Stale Structural TP Guard ---
        # A newly-added Pyramid Unit must never be immediately trimmed by
        # an old Structural TP that is already behind that new Unit.
        #
        # LONG  -> keep only unhit TP > newest Unit fill price
        # SHORT -> keep only unhit TP < newest Unit fill price
        #
        # IMPORTANT: do NOT manufacture new TP levels here.
        newest_unit_entry = float(fill.avg_price)

        old_tp_levels = list(tr.tp_levels)
        old_tp_hit = list(tr.tp_hit)
        kept_tp_levels = []

        for tp_idx, tp_level in enumerate(old_tp_levels):
            already_hit = (
                tp_idx < len(old_tp_hit)
                and bool(old_tp_hit[tp_idx])
            )
            if already_hit:
                continue

            tp_level = float(tp_level)

            if tr.direction == "LONG":
                if tp_level > newest_unit_entry:
                    kept_tp_levels.append(tp_level)
            else:
                if tp_level < newest_unit_entry:
                    kept_tp_levels.append(tp_level)

        tr.tp_levels = sorted(
            set(kept_tp_levels),
            reverse=(tr.direction == "SHORT"),
        )
        tr.tp_hit = [False] * len(tr.tp_levels)

        # _submit() already persisted the fill. Persist the TP cleanup too,
        # so a Railway restart cannot restore stale pre-ADD TP levels.
        self.save()
        events.append(self._event({
            "type":"TREND_ADD","symbol":s,"direction":tr.direction,
            "trade_id":tr.trade_id,"qty":fill.executed_qty,"price":fill.avg_price,
            "notional":fill.quote_qty or fill.executed_qty*fill.avg_price,
            "locked_unit_notional":notional,
            "units":len(tr.units),"order_id":fill.order_id,
            "client_order_id":fill.client_order_id,
        }))

    def _reduce_trend_unit(self,s,tr,idx,reason,price,events):
        if len(tr.units)<=1:return
        unit=tr.units[idx]
        fill=self._submit({
            "kind":"trend_reduce","symbol":s,
            "side":"SELL" if tr.direction=="LONG" else "BUY",
            "position_side":tr.direction,"qty":unit.qty,"reducing":True,
            "reference_price":price,"unit_index":idx,
        })
        events.append(self._event({
            "type":"TREND_REDUCE","symbol":s,"direction":tr.direction,
            "trade_id":tr.trade_id,"reason":reason,
            "qty":fill.executed_qty,"price":fill.avg_price,
            "order_id":fill.order_id,"client_order_id":fill.client_order_id,
        }))

    def _exit_trend(self,s,tr,reason,price,events):
        q=trend_qty(tr)
        if q<=0:return
        fill=self._submit({
            "kind":"trend_exit","symbol":s,
            "side":"SELL" if tr.direction=="LONG" else "BUY",
            "position_side":tr.direction,"qty":q,"reducing":True,
            "reference_price":price,
        })
        net=tr.realized_core-tr.fees
        events.append(self._event({
            "type":"TREND_EXIT","symbol":s,"direction":tr.direction,
            "trade_id":tr.trade_id,"reason":reason,
            "qty":fill.executed_qty,"price":fill.avg_price,
            "realized_strategy_pnl":net,
            "order_id":fill.order_id,"client_order_id":fill.client_order_id,
        }))
        if not any(self.state.trend_active.values()):
            self.state.trend_lock=0.0

    # --------------------------------------------------------
    # FLEX conditions / order helpers
    # --------------------------------------------------------

    def _flex_context(self, market):
        d1=market["d1"].dropna(subset=["ema50","atr14"])
        h4=market["h4"].dropna(subset=["ema50","atr14","adx14"])
        b=market["btc15"].dropna(subset=["rsi14"])
        if len(d1)<2 or len(h4)<3 or len(b)<2:
            return None
        dr=d1.iloc[-1];hr=h4.iloc[-1];pr=h4.iloc[-3]
        r=b.iloc[-1];prev=b.iloc[-2]
        strong_bear=(
            dr["close"] < dr["ema50"]
            and hr["close"] < hr["ema50"]
            and hr["adx14"] >=25
            and hr["adx14"] >= pr["adx14"]
        )
        return {
            "d1":dr,"h4":hr,"h4_lag2":pr,"r":r,"prev":prev,
            "strong_bear":bool(strong_bear),
        }

    def _flex_entry_allowed(self, market) -> bool:
        c=self._flex_context(market)
        if c is None or c["strong_bear"]:
            return False
        d,h,r,p=c["d1"],c["h4"],c["r"],c["prev"]
        if (r["close"]-d["ema20"])/d["atr14"] > 1.0:
            return False
        if r["close"] < d["ema50"]-0.5*d["atr14"]:
            return False
        touched=r["low"] <= h["ema20"]+0.15*h["atr14"]
        near=(
            r["close"] <= h["ema20"]+0.25*h["atr14"]
            and r["close"] >= h["ema50"]-0.5*h["atr14"]
        )
        if not (touched and near):
            return False
        if not (30 <= r["rsi14"] <= 55):
            return False
        return bool(r["close"]>p["close"] and r["rsi14"]>=p["rsi14"])

    def _flex_add_confirm(self, market, threshold: float) -> bool:
        c=self._flex_context(market)
        if c is None:return False
        r,p=c["r"],c["prev"]
        touched=r["low"]<=threshold
        stabilization=(r["close"]>p["close"]) or (r["rsi14"]>=p["rsi14"])
        return bool(touched and stabilization)

    def _open_flex(self,market,account,events):
        meta=market["meta"]
        lock=self.desired_lock(meta["state"],"flex",account["portfolio_dd"])
        if lock<=0:return
        im,am=self.flex_sizes(account["margin_balance"],lock)
        price=market["prices"]["BTCUSDT"]
        notional=im*FLEX_LEVERAGE
        qty=self.client.qty_for_notional("BTCUSDT",notional,price)
        if qty<=0:return
        cid=f"FX-{int(time.time())}"
        create={
            "cycle_id":cid,"opened_at":self.client.timestamp_ms(),
            "start_equity":account["margin_balance"],"meta_risk_mult":lock,
            "init_margin":im,"add_margin":am,
        }
        self.state.flex_lock=lock
        fill=self._submit({
            "kind":"flex_open","symbol":"BTCUSDT",
            "side":"BUY","position_side":"LONG","qty":qty,"reducing":False,
            "reference_price":price,"cycle_create":create,
        })
        events.append(self._event({
            "type":"FLEX_OPEN","symbol":"BTCUSDT","direction":"LONG",
            "cycle_id":cid,"meta_state":meta["state"],"meta_score":meta["score"],
            "risk_mult":lock,"margin":im,
            "qty":fill.executed_qty,"price":fill.avg_price,
            "order_id":fill.order_id,"client_order_id":fill.client_order_id,
        }))

    def _add_flex(self,market,account,events):
        fc=self.state.flex_cycle
        if fc is None:return
        price=market["prices"]["BTCUSDT"]
        notional=fc.add_margin*FLEX_LEVERAGE
        qty=self.client.qty_for_notional("BTCUSDT",notional,price)
        if qty<=0:return

        # Prospective virtual basket.
        px=price
        nq=fc.long_qty+qty
        navg=(fc.long_avg*fc.long_qty+px*qty)/nq
        newgross=nq*price
        collateral_ex_flex = account["margin_balance"] - flex_unrealized(fc,price)
        lp=liq_price_long_cross(
            collateral_ex_flex,nq,navg,fc.short_qty,fc.short_avg
        )
        ratio=lp/navg if navg else 0.0
        gross_ok=newgross <= fc.start_equity*FLEX_GROSS_CAP

        if not (ratio<=0.20 and gross_ok):
            fc.add_blocked+=1
            if not fc.recovery:
                fc.recovery=True;fc.hedge_anchor=price
            events.append(self._event({
                "type":"FLEX_ADD_BLOCKED","cycle_id":fc.cycle_id,
                "liq_ratio":ratio,"gross_ok":gross_ok,
                "price":price,"entries":fc.entry_actions,
            }))
            return

        fill=self._submit({
            "kind":"flex_add","symbol":"BTCUSDT",
            "side":"BUY","position_side":"LONG","qty":qty,"reducing":False,
            "reference_price":price,
        })
        events.append(self._event({
            "type":"FLEX_ADD","cycle_id":fc.cycle_id,
            "qty":fill.executed_qty,"price":fill.avg_price,
            "entries":fc.entry_actions,
            "order_id":fill.order_id,"client_order_id":fill.client_order_id,
        }))

    def _close_flex_fraction(self,fraction,reason,price,events):
        fc=self.state.flex_cycle
        if fc is None or fc.long_qty<=0:return
        q=self.client.round_qty("BTCUSDT",fc.long_qty*fraction)
        if q<=0:return
        fill=self._submit({
            "kind":"flex_close","symbol":"BTCUSDT",
            "side":"SELL","position_side":"LONG","qty":q,"reducing":True,
            "reference_price":price,
        })
        events.append(self._event({
            "type":"FLEX_REDUCE" if fraction<0.999 else "FLEX_EXIT",
            "cycle_id":fc.cycle_id,"reason":reason,
            "fraction":fraction,"qty":fill.executed_qty,"price":fill.avg_price,
            "strategy_realized_pnl":fc.realized_pnl-fc.fees,
            "order_id":fill.order_id,"client_order_id":fill.client_order_id,
        }))
        if fc.long_qty<=0:
            # Close hedge too before cycle ends.
            if fc.short_qty>0:
                self._set_flex_hedge(0.0,price,events)
            self.state.flex_cycle=None
            self.state.flex_lock=0.0
            self.state.flex_cooldown_until_ms=self.client.timestamp_ms()+4*3600*1000

    def _set_flex_hedge(self,target_ratio,price,events):
        fc=self.state.flex_cycle
        if fc is None:return
        if fc.long_qty<=0 and target_ratio>0:return
        target_qty=(
            self.client.round_qty("BTCUSDT",fc.long_qty*target_ratio)
            if target_ratio>0 else 0.0
        )
        current=fc.short_qty
        diff=target_qty-current
        step=self.client.symbol_filter("BTCUSDT").qty_step
        if abs(diff)<step:return

        if diff>0:
            fill=self._submit({
                "kind":"flex_hedge","symbol":"BTCUSDT","side":"SELL",
                "position_side":"SHORT","qty":diff,"reducing":False,
                "reference_price":price,"hedge_action":"increase",
            })
        else:
            fill=self._submit({
                "kind":"flex_hedge","symbol":"BTCUSDT","side":"BUY",
                "position_side":"SHORT","qty":-diff,"reducing":True,
                "reference_price":price,"hedge_action":"decrease",
            })
        events.append(self._event({
            "type":"FLEX_HEDGE","cycle_id":fc.cycle_id,
            "target_ratio":target_ratio,
            "qty":fill.executed_qty,"price":fill.avg_price,
            "order_id":fill.order_id,"client_order_id":fill.client_order_id,
        }))

    # --------------------------------------------------------
    # BTC Range Alpha — Quality-Gated maker mean reversion
    # --------------------------------------------------------

    def _range_quality_features(self, market) -> dict:
        btc = market.get("btc15")
        meta = market.get("meta", {})
        out = {
            "available": False,
            "pass": False,
            "meta_range": meta.get("state") == "RANGE",
        }
        if btc is None or len(btc) < 220:
            out["reason"] = "insufficient_15m_history"
            return out

        x = btc.copy().reset_index(drop=True)
        c = pd.to_numeric(x["close"], errors="coerce")
        h = pd.to_numeric(x["high"], errors="coerce")
        l = pd.to_numeric(x["low"], errors="coerce")
        if c.tail(220).isna().any():
            out["reason"] = "nan_15m_history"
            return out

        lr = np.log(c / c.shift(1))
        rv12 = float(lr.rolling(48).std().iloc[-1] * math.sqrt(96))
        rv24 = float(lr.rolling(96).std().iloc[-1] * math.sqrt(96))
        rv48 = float(lr.rolling(192).std().iloc[-1] * math.sqrt(96))
        volratio = rv12 / rv48 if rv48 > 0 else math.inf

        path24 = float(c.diff().abs().iloc[-96:].sum())
        eff24 = (
            abs(float(c.iloc[-1]) - float(c.iloc[-97])) / path24
            if path24 > 0 else 0.0
        )

        ema20 = c.ewm(span=20, adjust=False).mean()
        above = (c > ema20).astype(int)
        crosses = (above != above.shift(1)).astype(float)
        cross24 = float(crosses.iloc[-96:].sum())

        hi48 = float(h.iloc[-192:].max())
        lo48 = float(l.iloc[-192:].min())
        pos48 = (
            (float(c.iloc[-1]) - lo48) / (hi48 - lo48)
            if hi48 > lo48 else 0.5
        )

        adx = float(meta.get("h4", {}).get("adx14") or math.nan)
        sep = float(
            meta.get("conditions", {})
            .get("ema_separation_ge_075_atr", {})
            .get("value")
            or math.nan
        )
        since = meta.get("state_since_ms")
        age_h = (
            max(0.0, (float(market["now_ms"]) - float(since)) / 3_600_000.0)
            if since is not None and meta.get("state") == "RANGE" else 0.0
        )

        checks = {
            "meta_range": meta.get("state") == "RANGE",
            "range_age_ge_4h": age_h >= RANGE_ALPHA_MIN_RANGE_AGE_H,
            "adx_le_28": not math.isnan(adx) and adx <= RANGE_ALPHA_ADX_MAX,
            "sep_atr_le_1": not math.isnan(sep) and sep <= RANGE_ALPHA_SEP_ATR_MAX,
            "eff24_le_012": eff24 <= RANGE_ALPHA_EFF24_MAX,
            "rv24_ge_014": not math.isnan(rv24) and rv24 >= RANGE_ALPHA_RV24_MIN,
            "volratio_le_120": not math.isnan(volratio) and volratio <= RANGE_ALPHA_VOLRATIO_12_48_MAX,
            "cross24_ge_12": cross24 >= RANGE_ALPHA_CROSS24_MIN,
        }
        out.update({
            "available": True,
            "pass": all(checks.values()),
            "checks": checks,
            "range_age_h": age_h,
            "adx14": adx,
            "sep_atr": sep,
            "eff24": eff24,
            "rv12": rv12,
            "rv24": rv24,
            "rv48": rv48,
            "volratio_12_48": volratio,
            "ema20_crosses_24h": cross24,
            "range_pos_48h": pos48,
            "range_low_48h": lo48,
            "range_high_48h": hi48,
        })
        return out

    def _range_micro_context(self, signal_open_time: int, now_ms: int) -> dict:
        raw = self.client.public_get(
            "/fapi/v1/klines",
            {"symbol": RANGE_ALPHA_SYMBOL, "interval": "5m", "limit": 30},
        )
        cols = [
            "open_time","open","high","low","close","volume","close_time",
            "quote_volume","trades","taker_buy_base","taker_buy_quote","ignore",
        ]
        d = pd.DataFrame(raw, columns=cols)
        if d.empty:
            return {"available": False, "pass": False, "reason": "no_5m_data"}
        for col in ("open_time","close_time"):
            d[col] = pd.to_numeric(d[col], errors="coerce").astype("int64")
        for col in ("volume","taker_buy_base"):
            d[col] = pd.to_numeric(d[col], errors="coerce")
        d = d[d["close_time"] <= int(now_ms)].copy().reset_index(drop=True)

        cur = d[(d.open_time >= signal_open_time) & (d.open_time < signal_open_time + 900_000)]
        prev = d[(d.open_time >= signal_open_time - 900_000) & (d.open_time < signal_open_time)]
        if len(cur) < 3 or len(prev) < 3:
            return {"available": False, "pass": False, "reason": "incomplete_5m_buckets"}
        cur = cur.tail(3)
        prev = prev.tail(3)
        cur_vol = float(cur.volume.sum())
        prev_vol = float(prev.volume.sum())
        last_vol = float(cur.iloc[-1].volume)
        if min(cur_vol, prev_vol, last_vol) <= 0:
            return {"available": False, "pass": False, "reason": "zero_volume"}

        buy15 = float(cur.taker_buy_base.sum()) / cur_vol
        prev_buy15 = float(prev.taker_buy_base.sum()) / prev_vol
        last5 = float(cur.iloc[-1].taker_buy_base) / last_vol
        passed = (
            last5 >= RANGE_ALPHA_TAKER_BUY_MIN
            and buy15 >= prev_buy15
        )
        return {
            "available": True,
            "pass": passed,
            "last5_buy_ratio": last5,
            "buy_ratio_15m": buy15,
            "prev_buy_ratio_15m": prev_buy15,
            "last5_buy_ge_0525": last5 >= RANGE_ALPHA_TAKER_BUY_MIN,
            "buy_pressure_non_declining": buy15 >= prev_buy15,
        }

    def _range_entry_diagnostic(self, market, *, include_micro: bool = False) -> dict:
        q = self._range_quality_features(market)
        btc = market.get("btc15")
        if btc is None or len(btc) < 2:
            return {"available": False, "eligible_now": False, "quality": q}
        row = btc.iloc[-1]
        prev = btc.iloc[-2]
        signal_open = int(row["open_time"])
        signal_close = int(row["close_time"])
        signal_price = float(row["close"])
        edge_ok = bool(q.get("available") and q.get("range_pos_48h", 1.0) <= RANGE_ALPHA_RANGE_POS_MAX)
        stabilize = signal_price > float(prev["close"])
        micro = None
        if include_micro and q.get("pass") and edge_ok and stabilize:
            micro = self._range_micro_context(signal_open, int(market["now_ms"]))
        micro_ok = bool(micro and micro.get("pass")) if include_micro else None
        # Research entries occurred when Trend was not already running. Once a
        # Range Alpha order/cycle exists, a later Trend is explicitly allowed
        # to coexist; this gate applies only to creating a fresh Range order.
        trend_clear_for_new_entry = (
            not any(self.state.trend_active.values())
            and not any(p is not None for p in self.state.trend_pending.values())
        )
        eligible = bool(
            q.get("pass") and edge_ok and stabilize
            and (micro_ok if include_micro else True)
            and trend_clear_for_new_entry
            and self.state.range_pending is None
            and self.state.range_cycle is None
        )
        return {
            "available": True,
            "eligible_now": eligible,
            "signal_open_time": signal_open,
            "signal_close_time": signal_close,
            "signal_price": signal_price,
            "quality": q,
            "edge_ok": edge_ok,
            "stabilize": stabilize,
            "micro": micro,
            "trend_clear_for_new_entry": trend_clear_for_new_entry,
        }

    def _range_qty_for_market_price(self, market_price: float) -> float:
        raw = (
            RANGE_ALPHA_QTY_BELOW_100K
            if float(market_price) < RANGE_ALPHA_PRICE_THRESHOLD
            else RANGE_ALPHA_QTY_AT_OR_ABOVE_100K
        )
        return self.client.round_limit_qty(RANGE_ALPHA_SYMBOL, raw)

    def _place_range_entry(self, market, diagnostic, events):
        if self.state.range_pending is not None or self.state.range_cycle is not None:
            return
        if market["meta"].get("state") != "RANGE":
            return
        if any(self.state.trend_active.values()) or any(
            p is not None for p in self.state.trend_pending.values()
        ):
            return
        live_price = float(market["prices"][RANGE_ALPHA_SYMBOL])
        qty = self._range_qty_for_market_price(live_price)
        if qty <= 0:
            self._record_event(events, {
                "type": "RANGE_ENTRY_BLOCKED", "symbol": RANGE_ALPHA_SYMBOL,
                "reason": "QTY_BELOW_EXCHANGE_MINIMUM", "market_price": live_price,
            })
            return
        signal_price = float(diagnostic["signal_price"])
        limit_price = self.client.round_price(
            RANGE_ALPHA_SYMBOL,
            signal_price * (1.0 - RANGE_ALPHA_ENTRY_OFFSET),
            side="BUY",
        )
        f = self.client.symbol_filter(RANGE_ALPHA_SYMBOL)
        if qty * limit_price < f.min_notional:
            self._record_event(events, {
                "type": "RANGE_ENTRY_BLOCKED", "symbol": RANGE_ALPHA_SYMBOL,
                "reason": "MIN_NOTIONAL", "qty": qty, "limit_price": limit_price,
                "min_notional": f.min_notional,
            })
            return

        self.state.range_counter += 1
        cycle_id = f"BTC-RA-{self.state.range_counter:05d}"
        cid = self._new_client_id("range_entry")
        now = self.client.timestamp_ms()
        rp = RangePendingEntry(
            cycle_id=cycle_id,
            signal_open_time=int(diagnostic["signal_open_time"]),
            signal_close_time=int(diagnostic["signal_close_time"]),
            created_at_ms=now,
            expires_at_ms=now + RANGE_ALPHA_PENDING_MS,
            qty=qty,
            signal_price=signal_price,
            limit_price=limit_price,
            client_order_id=cid,
            meta_state="RANGE",
            quality=dict(diagnostic.get("quality") or {}),
        )
        self.state.range_pending = rp
        self.save()
        try:
            raw = self.client.limit_order(
                symbol=RANGE_ALPHA_SYMBOL,
                side="BUY",
                position_side="LONG",
                qty=qty,
                price=limit_price,
                client_order_id=cid,
                reducing=False,
                post_only=True,
            )
        except BinanceLiveError as exc:
            # A GTX order can be rejected if it would immediately cross.  That
            # means no maker entry exists; safely abandon this signal.
            if "code=-5022" in str(exc) or "Post Only" in str(exc):
                self.state.range_pending = None
                self.save()
                self._record_event(events, {
                    "type": "RANGE_ENTRY_CANCEL", "symbol": RANGE_ALPHA_SYMBOL,
                    "cycle_id": cycle_id, "reason": "POST_ONLY_REJECTED",
                    "detail": str(exc)[:500],
                })
                return
            # Ambiguous network failures keep the persistent client id so a
            # restart can query exactly this order rather than duplicating it.
            raise
        rp.order_id = int(raw.get("orderId", 0) or 0) or None
        self.save()
        self._record_event(events, {
            "type": "RANGE_ENTRY_PENDING", "symbol": RANGE_ALPHA_SYMBOL,
            "cycle_id": cycle_id, "qty": qty, "limit_price": limit_price,
            "signal_price": signal_price, "expires_at_ms": rp.expires_at_ms,
            "order_id": rp.order_id, "client_order_id": cid,
        })
        # Normally GTX remains NEW, but synchronize if it filled exceptionally fast.
        self._sync_range_orders(events=events, bootstrap=False)

    def _activate_range_entry(self, pending: RangePendingEntry, raw: dict, events):
        fill = self.client._normalize_fill(
            raw, RANGE_ALPHA_SYMBOL, "BUY", "LONG", pending.qty, pending.client_order_id
        )
        if fill.executed_qty <= 0:
            return False
        trades = self.client.user_trades_for_order(RANGE_ALPHA_SYMBOL, fill.order_id) if fill.order_id else []
        opened_at = min(
            [int(x.get("time", self.client.timestamp_ms())) for x in trades]
            or [self.client.timestamp_ms()]
        )
        entry = float(fill.avg_price)
        qty = self.client.round_limit_qty(RANGE_ALPHA_SYMBOL, fill.executed_qty)
        if qty <= 0:
            raise BinanceReconciliationError(
                f"Range Alpha fill {fill.client_order_id} is below usable qty after rounding."
            )
        tp = self.client.round_price(
            RANGE_ALPHA_SYMBOL, entry * (1.0 + RANGE_ALPHA_TP), side="SELL"
        )
        stop = entry * (1.0 - RANGE_ALPHA_STOP)
        rc = RangeCycle(
            cycle_id=pending.cycle_id,
            opened_at=opened_at,
            initial_qty=qty,
            qty=qty,
            avg_entry=entry,
            entry_fee=float(fill.commission),
            tp_price=tp,
            stop_price=stop,
            expires_at_ms=opened_at + RANGE_ALPHA_MAX_HOLD_MS,
            entry_order_id=fill.order_id,
            entry_client_order_id=fill.client_order_id,
            fees=float(fill.commission),
        )
        self.state.range_pending = None
        self.state.range_cycle = rc
        self.save()
        self._record_event(events, {
            "type": "RANGE_OPEN", "symbol": RANGE_ALPHA_SYMBOL,
            "direction": "LONG", "cycle_id": rc.cycle_id,
            "qty": qty, "price": entry,
            "notional": fill.quote_qty or qty * entry,
            "tp_price": tp, "stop_price": stop,
            "max_hold_hours": RANGE_ALPHA_MAX_HOLD_MS / 3_600_000,
            "order_id": fill.order_id, "client_order_id": fill.client_order_id,
        })
        self._ensure_range_tp(events)
        return True

    def _update_range_pending_partial(self, rp: RangePendingEntry, raw: dict) -> float:
        executed = abs(float((raw or {}).get("executedQty", 0.0) or 0.0))
        qty = self.client.round_limit_qty(RANGE_ALPHA_SYMBOL, executed) if executed > 0 else 0.0
        if abs(float(getattr(rp, "partial_qty", 0.0) or 0.0) - qty) > 1e-12:
            rp.partial_qty = qty
            self.save()
        return qty

    def _cancel_range_pending(self, reason: str, events=None):
        rp = self.state.range_pending
        if rp is None:
            return

        # A partial GTX fill creates a real BTC LONG immediately.  We only
        # convert it into an active RangeCycle after Binance confirms that the
        # remainder can no longer fill (CANCELED/EXPIRED/REJECTED/FILLED).
        # Until then partial_qty is included in expected_positions().
        cancel_raw = None
        try:
            cancel_raw = self.client.cancel_order(
                RANGE_ALPHA_SYMBOL,
                order_id=rp.order_id,
                client_order_id=None if rp.order_id is not None else rp.client_order_id,
            )
        except Exception:
            cancel_raw = None

        final_raw = None
        try:
            final_raw = self.client.query_order(
                RANGE_ALPHA_SYMBOL, client_order_id=rp.client_order_id
            )
        except Exception:
            final_raw = cancel_raw

        if not final_raw:
            raise BinanceReconciliationError(
                f"Cannot confirm final state of Range entry order {rp.client_order_id}; "
                "keeping it pending and retrying next poll."
            )

        status = str(final_raw.get("status", "")).upper()
        executed = self._update_range_pending_partial(rp, final_raw)
        terminal = status in {"CANCELED", "EXPIRED", "REJECTED", "FILLED"}
        if not terminal:
            raise BinanceLiveError(
                f"Range entry cancel not yet terminal: {rp.client_order_id} "
                f"status={status or 'UNKNOWN'} executedQty={executed:.12g}"
            )

        if executed > 0:
            # Cancellation raced a partial/full fill.  The actual filled clip
            # becomes a normal Range Cycle and is NOT discarded merely because
            # Meta changed or the pending timeout elapsed.
            self._activate_range_entry(rp, final_raw, events)
            self._record_event(events, {
                "type": "RANGE_ENTRY_PARTIAL_ACCEPTED", "symbol": RANGE_ALPHA_SYMBOL,
                "cycle_id": rp.cycle_id, "reason": reason, "executed_qty": executed,
                "final_order_status": status,
            })
            return

        self.state.range_pending = None
        self.save()
        self._record_event(events, {
            "type": "RANGE_ENTRY_CANCEL", "symbol": RANGE_ALPHA_SYMBOL,
            "cycle_id": rp.cycle_id, "reason": reason,
            "order_id": rp.order_id, "client_order_id": rp.client_order_id,
            "final_order_status": status,
        })

    def _try_cancel_range_pending(self, reason: str, events=None) -> bool:
        try:
            self._cancel_range_pending(reason, events)
            return True
        except Exception as exc:
            # Do not stop management of already-open Trend/FLEX/Range positions
            # just because a LIMIT cancellation request had a transient failure.
            # Keep the persistent pending id and retry next poll.
            self._record_event(events, {
                "type": "RANGE_ENTRY_CANCEL_ERROR",
                "symbol": RANGE_ALPHA_SYMBOL,
                "cycle_id": (self.state.range_pending.cycle_id if self.state.range_pending else None),
                "reason": reason, "detail": str(exc)[:700],
            })
            return False

    def _ensure_range_tp(self, events=None):
        rc = self.state.range_cycle
        if rc is None or rc.qty <= 0 or rc.tp_client_order_id:
            return
        # If price has already crossed the TP before the resting maker order is
        # installed, do not chase a GTX rejection indefinitely: realize it by MARKET.
        live_price = self.client.ticker_price(RANGE_ALPHA_SYMBOL)
        if live_price >= rc.tp_price:
            self._close_range_market("TP_MARKET_FALLBACK", live_price, events)
            return
        cid = self._new_client_id("range_tp")
        rc.tp_client_order_id = cid
        self.save()
        try:
            raw = self.client.limit_order(
                symbol=RANGE_ALPHA_SYMBOL,
                side="SELL",
                position_side="LONG",
                qty=rc.qty,
                price=rc.tp_price,
                client_order_id=cid,
                reducing=True,
                post_only=True,
            )
        except BinanceLiveError as exc:
            rc.tp_client_order_id = None
            rc.tp_order_id = None
            self.save()
            live_price = self.client.ticker_price(RANGE_ALPHA_SYMBOL)
            if ("code=-5022" in str(exc) or "Post Only" in str(exc)) and live_price >= rc.tp_price:
                self._close_range_market("TP_MARKET_FALLBACK", live_price, events)
                return
            self._record_event(events, {
                "type": "RANGE_TP_ORDER_ERROR", "symbol": RANGE_ALPHA_SYMBOL,
                "cycle_id": rc.cycle_id, "detail": str(exc)[:700],
            })
            return
        rc.tp_order_id = int(raw.get("orderId", 0) or 0) or None
        self.save()
        self._record_event(events, {
            "type": "RANGE_TP_PLACED", "symbol": RANGE_ALPHA_SYMBOL,
            "cycle_id": rc.cycle_id, "qty": rc.qty, "tp_price": rc.tp_price,
            "order_id": rc.tp_order_id, "client_order_id": cid,
        })

    def _apply_range_tp_totals(
        self,
        rc: RangeCycle,
        *,
        total_qty: float,
        total_quote: float,
        total_commission: float,
        events=None,
        source: str = "userTrades",
    ) -> float:
        dq = max(0.0, float(total_qty) - float(rc.tp_filled_qty_accounted))
        dquote = float(total_quote) - float(rc.tp_fill_quote_accounted)
        dcomm = float(total_commission) - float(rc.tp_commission_accounted)
        # dquote - dq*entry includes both the PnL on newly-accounted quantity
        # and any later correction to a prior fallback price estimate.
        rc.realized_pnl += dquote - dq * rc.avg_entry
        rc.fees += dcomm
        rc.qty = max(0.0, rc.qty - dq)
        rc.tp_filled_qty_accounted = float(total_qty)
        rc.tp_fill_quote_accounted = float(total_quote)
        rc.tp_commission_accounted = float(total_commission)
        if dq > 0 or abs(dquote) > 1e-10 or abs(dcomm) > 1e-10:
            self.save()
        if dq > 0:
            self._record_event(events, {
                "type": "RANGE_TP_FILL", "symbol": RANGE_ALPHA_SYMBOL,
                "cycle_id": rc.cycle_id, "qty": dq,
                "price": dquote / dq if dq > 0 else rc.tp_price,
                "remaining_qty": max(0.0, rc.qty), "source": source,
            })
        return dq

    def _account_range_tp_trades(self, rc: RangeCycle, events=None) -> float:
        if not rc.tp_order_id:
            return 0.0
        try:
            trades = self.client.user_trades_for_order(RANGE_ALPHA_SYMBOL, rc.tp_order_id)
        except Exception:
            trades = []
        if not trades:
            return 0.0
        total_qty = sum(abs(float(x.get("qty", 0.0) or 0.0)) for x in trades)
        total_quote = sum(
            abs(float(x.get("qty", 0.0) or 0.0)) * float(x.get("price", 0.0) or 0.0)
            for x in trades
        )
        total_commission = sum(float(x.get("commission", 0.0) or 0.0) for x in trades)
        rc.tp_trade_ids_accounted = [int(x.get("id", -1)) for x in trades]
        return self._apply_range_tp_totals(
            rc,
            total_qty=total_qty,
            total_quote=total_quote,
            total_commission=total_commission,
            events=events,
            source="userTrades",
        )

    def _sync_range_tp_order(self, events=None, *, rearm: bool = True, raw_override=None):
        rc = self.state.range_cycle
        if rc is None or not rc.tp_client_order_id:
            return
        raw = raw_override
        if raw is None:
            try:
                raw = self.client.query_order(
                    RANGE_ALPHA_SYMBOL, client_order_id=rc.tp_client_order_id
                )
            except Exception:
                # Leave the known order in state. A later poll/restart can query it;
                # blindly clearing it risks double-closing FLEX/Range aggregate LONG.
                return

        self._account_range_tp_trades(rc, events)
        # Binance order status can arrive before userTrades. Preserve quantity
        # reconciliation by conservatively accounting any missing executedQty at
        # the actual LIMIT price and estimated maker fee; later userTrades totals
        # can correct quote/commission without double-counting quantity.
        executed = abs(float(raw.get("executedQty", 0.0) or 0.0))
        if executed > rc.tp_filled_qty_accounted + 1e-12:
            px = float(raw.get("avgPrice", 0.0) or 0.0) or rc.tp_price
            total_quote = rc.tp_fill_quote_accounted + (
                executed - rc.tp_filled_qty_accounted
            ) * px
            total_comm = rc.tp_commission_accounted + (
                executed - rc.tp_filled_qty_accounted
            ) * px * 0.0002
            self._apply_range_tp_totals(
                rc,
                total_qty=executed,
                total_quote=total_quote,
                total_commission=total_comm,
                events=events,
                source="order_status_fallback",
            )

        step = self.client.symbol_filter(RANGE_ALPHA_SYMBOL).qty_step
        if rc.qty <= step / 2:
            net = rc.realized_pnl - rc.fees
            cid = rc.cycle_id
            self.state.range_cycle = None
            self.save()
            self._record_event(events, {
                "type": "RANGE_EXIT", "symbol": RANGE_ALPHA_SYMBOL,
                "cycle_id": cid, "reason": "TP_LIMIT",
                "realized_strategy_pnl": net,
            })
            return
        status = str(raw.get("status", "")).upper()
        if status in {"CANCELED", "EXPIRED", "REJECTED"}:
            rc.tp_order_id = None
            rc.tp_client_order_id = None
            self.save()
            if rearm:
                self._ensure_range_tp(events)

    def _sync_range_orders(self, events=None, *, bootstrap: bool = False):
        rp = self.state.range_pending
        if rp is not None:
            try:
                raw = self.client.query_order(
                    RANGE_ALPHA_SYMBOL, client_order_id=rp.client_order_id
                )
            except Exception:
                raw = None
            if raw is not None:
                status = str(raw.get("status", "")).upper()
                executed = float(raw.get("executedQty", 0.0) or 0.0)
                if status == "PARTIALLY_FILLED":
                    # Record the real partial exchange position first so
                    # reconciliation remains correct even if cancellation is
                    # temporarily unavailable. Then try to cancel the remainder.
                    self._update_range_pending_partial(rp, raw)
                    self._try_cancel_range_pending("PARTIAL_FILL_ACCEPTED", events)
                elif status == "FILLED":
                    self._update_range_pending_partial(rp, raw)
                    self._activate_range_entry(rp, raw, events)
                elif status in {"CANCELED", "EXPIRED", "REJECTED"}:
                    self._update_range_pending_partial(rp, raw)
                    if executed > 0:
                        self._activate_range_entry(rp, raw, events)
                    else:
                        self.state.range_pending = None
                        self.save()
                        self._record_event(events, {
                            "type": "RANGE_ENTRY_CANCEL", "symbol": RANGE_ALPHA_SYMBOL,
                            "cycle_id": rp.cycle_id, "reason": status,
                            "order_id": rp.order_id,
                            "client_order_id": rp.client_order_id,
                        })

        if self.state.range_cycle is not None:
            self._sync_range_tp_order(events, rearm=True)
            if self.state.range_cycle is not None:
                self._ensure_range_tp(events)

    def _cancel_range_tp(self, events=None):
        rc = self.state.range_cycle
        if rc is None or not rc.tp_client_order_id:
            return
        cid = rc.tp_client_order_id
        oid = rc.tp_order_id
        cancel_raw = None
        try:
            cancel_raw = self.client.cancel_order(
                RANGE_ALPHA_SYMBOL,
                order_id=oid,
                client_order_id=None if oid is not None else cid,
            )
        except Exception:
            cancel_raw = None
        final_raw = None
        try:
            final_raw = self.client.query_order(
                RANGE_ALPHA_SYMBOL, client_order_id=cid
            )
        except Exception:
            final_raw = cancel_raw
        if not final_raw:
            raise BinanceReconciliationError(
                f"Cannot confirm final state of Range TP order {cid}; "
                "refusing MARKET exit to avoid closing FLEX/Trend quantity by mistake."
            )
        self._sync_range_tp_order(events, rearm=False, raw_override=final_raw)
        rc = self.state.range_cycle
        if rc is not None and rc.tp_client_order_id == cid:
            rc.tp_client_order_id = None
            rc.tp_order_id = None
            self.save()

    def _close_range_market(self, reason: str, reference_price: float, events=None):
        rc = self.state.range_cycle
        if rc is None:
            return
        cycle_id = rc.cycle_id
        self._cancel_range_tp(events)
        rc = self.state.range_cycle
        if rc is None or rc.qty <= 0:
            return
        requested = rc.qty
        fill = self._submit({
            "kind": "range_exit", "symbol": RANGE_ALPHA_SYMBOL,
            "side": "SELL", "position_side": "LONG",
            "qty": requested, "reducing": True,
            "reference_price": float(reference_price),
        })
        net = rc.realized_pnl - rc.fees
        self._record_event(events, {
            "type": "RANGE_EXIT", "symbol": RANGE_ALPHA_SYMBOL,
            "cycle_id": cycle_id, "reason": reason,
            "qty": fill.executed_qty, "price": fill.avg_price,
            "realized_strategy_pnl": net,
            "order_id": fill.order_id, "client_order_id": fill.client_order_id,
        })

    # --------------------------------------------------------
    # Main cycle
    # --------------------------------------------------------

    def suppress_new_risk_pending(self):
        for s,p in self.state.trend_pending.items():
            if p is not None and p.kind in ("OPEN","ADD"):
                if p.kind=="ADD" and p.swing_time is not None:
                    tr=self.state.trend_active[s]
                    if tr is not None:
                        tr.last_add_swing_time=max(
                            tr.last_add_swing_time or 0,int(p.swing_time)
                        )
                self.state.trend_pending[s]=None
        if self.state.flex_pending and self.state.flex_pending.kind in ("OPEN","ADD"):
            self.state.flex_pending=None
        if self.state.range_pending is not None:
            self._try_cancel_range_pending("NEW_RISK_PAUSED", events=None)
        self.save()

    def cycle(self, *, new_risk_enabled: bool = True) -> dict:
        if self.state.halted:
            # A pure reconciliation halt may be cleared after the operator
            # fixes the real exchange position. Unresolved inflight/other
            # halts remain sticky and require manual review.
            if (self.state.halt_reason or "").startswith(
                "Exchange/state reconciliation mismatch"
            ):
                self.reconcile_or_halt()
            else:
                raise BinanceLiveError(
                    f"Strategy v2.2 is HALTED: {self.state.halt_reason}"
                )

        events=[]
        # Persistent maker orders may fill between 20-second polls.  Account
        # their fills before the aggregate exchange-position reconciliation.
        self._sync_range_orders(events=events, bootstrap=False)
        self.reconcile_or_halt()
        market=self._market_context()
        account=self._account_snapshot()

        # Pending Range Alpha is NEW risk.  It is cancelled when disabled,
        # expired, or Meta has left RANGE.  An already-filled Range Cycle is
        # deliberately allowed to finish even if Meta later becomes TRANS/TREND.
        rp=self.state.range_pending
        if rp is not None:
            if not new_risk_enabled:
                self._try_cancel_range_pending("NEW_RISK_PAUSED", events)
            elif market["meta"].get("state") != "RANGE":
                self._try_cancel_range_pending("META_LEFT_RANGE", events)
            elif self.client.timestamp_ms() >= rp.expires_at_ms:
                self._try_cancel_range_pending("ENTRY_TIMEOUT_1H", events)

        # Range Alpha TP rests at Binance. Hard stop/time exit are managed by
        # the live poll and close only this virtual sleeve's quantity.
        rc=self.state.range_cycle
        if rc is not None:
            price=float(market["prices"][RANGE_ALPHA_SYMBOL])
            if price <= rc.stop_price:
                self._close_range_market("HARD_STOP_1P75", price, events)
            elif self.client.timestamp_ms() >= rc.expires_at_ms:
                self._close_range_market("TIME_EXIT_8H", price, events)
            elif self.state.range_cycle is not None:
                self._ensure_range_tp(events)

        # ------------------
        # execute queued trend at live market
        # ------------------
        for s in SYMBOLS:
            p=self.state.trend_pending[s]
            self.state.trend_pending[s]=None
            if p is None:continue
            row=market["contexts"][s].iloc[-1]
            price=market["prices"][s]
            tr=self.state.trend_active[s]

            if p.kind=="OPEN" and new_risk_enabled and tr is None:
                self._open_trend(s,p.direction,pd.Series(p.signal_context),market["meta"],account,price,events)
            elif p.kind=="ADD" and new_risk_enabled and tr is not None:
                self._add_trend(s,tr,p.swing_time,price,events)
            elif p.kind=="EXIT" and tr is not None:
                self._exit_trend(s,tr,p.reason or "EXIT",price,events)

        if not any(self.state.trend_active.values()):
            self.state.trend_lock=0.0

        # ------------------
        # execute queued FLEX new risk
        # ------------------
        fp=self.state.flex_pending
        self.state.flex_pending=None
        if fp is not None:
            if fp.kind=="OPEN" and new_risk_enabled and self.state.flex_cycle is None:
                self._open_flex(market,account,events)
            elif fp.kind=="ADD" and new_risk_enabled and self.state.flex_cycle is not None:
                self._add_flex(market,account,events)

        # ------------------
        # live risk-management checks every poll
        # ------------------
        # Trend structural TP: only if currently observed at/beyond target.
        for s,tr in list(self.state.trend_active.items()):
            if tr is None or len(tr.units)<=1:continue
            price=market["prices"][s]
            for idx,level in enumerate(tr.tp_levels):
                if idx>=len(tr.tp_hit) or tr.tp_hit[idx]:
                    continue
                hit=price>=level if tr.direction=="LONG" else price<=level
                if hit and len(tr.units)>1:
                    self._reduce_trend_unit(
                        s,tr,-1,"STRUCTURAL_TP_LIVE",price,events
                    )
                    tr.tp_hit[idx]=True
                    break

        # FLEX live TP / Recovery hedge
        fc=self.state.flex_cycle
        if fc is not None and fc.long_qty>0:
            price=market["prices"]["BTCUSDT"]
            roe=(
                fc.long_qty*(price-fc.long_avg)/fc.long_margin
                if fc.long_margin>0 else -999
            )
            deep=fc.max_entries_before_partial>=4
            if not deep:
                target=.075 if fc.max_entries_before_partial==1 else .05
                if roe>=target:
                    self._close_flex_fraction(1.0,f"ROE_{target:.3f}",price,events)
            else:
                if fc.tp_stage==0 and roe>=.05:
                    self._close_flex_fraction(.30,"ROE_5_PARTIAL",price,events)
                    if self.state.flex_cycle:
                        self.state.flex_cycle.tp_stage=1
                        self.state.flex_cycle.last_ref_price=price
                fc=self.state.flex_cycle
                if fc is not None:
                    roe=fc.long_qty*(price-fc.long_avg)/fc.long_margin if fc.long_margin>0 else -999
                    if fc.tp_stage==1 and roe>=.10:
                        self._close_flex_fraction(.35,"ROE_10_PARTIAL",price,events)
                        if self.state.flex_cycle:
                            self.state.flex_cycle.tp_stage=2
                            self.state.flex_cycle.last_ref_price=price
                fc=self.state.flex_cycle
                if fc is not None:
                    roe=fc.long_qty*(price-fc.long_avg)/fc.long_margin if fc.long_margin>0 else -999
                    if fc.tp_stage==2 and roe>=.15:
                        self._close_flex_fraction(1.0,"ROE_15_EXIT",price,events)

        fc=self.state.flex_cycle
        fctx=self._flex_context(market)
        if fc is not None and fctx is not None:
            price=market["prices"]["BTCUSDT"]
            sb=fctx["strong_bear"]
            if fc.entry_actions>=4 and sb and not fc.recovery:
                fc.recovery=True;fc.hedge_anchor=price

            if fc.recovery:
                clear=(
                    not sb
                    and fctx["h4"]["close"]>=fctx["h4"]["ema20"]
                    and fctx["d1"]["close"]>=fctx["d1"]["ema20"]
                )
                if clear:
                    target=0.0
                else:
                    drop=(price/fc.hedge_anchor-1.0) if fc.hedge_anchor else 0.0
                    if sb and drop<=-.05:target=.50
                    elif sb and drop<=-.025:target=.30
                    else:target=.15
                current=fc.short_qty/fc.long_qty if fc.long_qty else 0
                if abs(target-current)>=.05:
                    self._set_flex_hedge(target,price,events)
                if clear and self.state.flex_cycle and self.state.flex_cycle.short_qty<=0:
                    self.state.flex_cycle.recovery=False
                    self.state.flex_cycle.hedge_anchor=None

        # ------------------
        # process exactly one newly closed 15m signal bar per symbol
        # ------------------
        for s in SYMBOLS:
            ctx=market["contexts"][s]
            row=ctx.iloc[-1];prev=ctx.iloc[-2] if len(ctx)>=2 else None
            cot=int(row["open_time"])
            last=self.state.last_processed_closed_open_time[s]
            if last is not None and cot<=last:
                continue
            self.state.last_processed_closed_open_time[s]=cot
            if self.state.trend_cooldown[s]>0:
                self.state.trend_cooldown[s]-=1

            tr=self.state.trend_active[s]
            bias=base.sim.four_hour_bias(row)
            close=float(row["close"])

            if tr is not None:
                pnl=trend_pnl(tr,close)
                tr.max_pnl_seen=max(tr.max_pnl_seen,pnl)
                tr.min_pnl_seen=min(tr.min_pnl_seen,pnl)

                hard=-base.ACCOUNT_RISK*tr.trade_start_equity*tr.meta_risk_mult
                if pnl<=hard:
                    self.state.trend_pending[s]=PendingAction(
                        kind="EXIT",symbol=s,reason="HARD_RISK_1PCT_META"
                    )
                    continue
                opposite=(
                    (tr.direction=="LONG" and bias=="SHORT")
                    or (tr.direction=="SHORT" and bias=="LONG")
                )
                if opposite:
                    self.state.trend_pending[s]=PendingAction(
                        kind="EXIT",symbol=s,reason="4H_BIAS_FLIP"
                    )
                    continue
                if base.sim.one_hour_invalidated(row,tr.direction):
                    self.state.trend_pending[s]=PendingAction(
                        kind="EXIT",symbol=s,reason="1H_STRUCTURE_INVALIDATED"
                    )
                    continue

                if (
                    new_risk_enabled
                    and len(tr.units)<TREND_MAX_UNITS
                    and trend_unrealized(tr,close)>0
                    and bias==tr.direction
                    and base.sim.aligned_15m(row,tr.direction)
                ):
                    swing=row.get(
                        "last_swing_low_time" if tr.direction=="LONG"
                        else "last_swing_high_time"
                    )
                    if swing is not None and not pd.isna(swing):
                        swing=int(swing)
                        if tr.last_add_swing_time is None or swing>tr.last_add_swing_time:
                            target_layer=len(tr.units)+1
                            readd=target_layer<=tr.max_units_seen
                            flags=v16.continuation_score(row,tr.direction)
                            allowed=(not readd) or (
                                flags["available"] and flags["score"]>=3
                            )
                            if allowed:
                                self.state.trend_pending[s]=PendingAction(
                                    kind="ADD",symbol=s,swing_time=swing
                                )
                            else:
                                tr.last_add_swing_time=swing
                                events.append(self._event({
                                    "type":"TREND_READD_BLOCKED","symbol":s,
                                    "trade_id":tr.trade_id,
                                    "score":flags["score"],
                                    "target_layer":target_layer,
                                }))
            else:
                if (
                    new_risk_enabled
                    and self.state.trend_cooldown[s]<=0
                    and bias in ("LONG","SHORT")
                    and base.candidate_entry_allowed(row,prev,bias)
                ):
                    # Store the complete row because entry execution needs TP/swing context.
                    self.state.trend_pending[s]=PendingAction(
                        kind="OPEN",symbol=s,direction=bias,
                        signal_context=row.to_dict(),
                        meta_state=market["meta"]["state"],
                        portfolio_dd=account["portfolio_dd"],
                    )

        # FLEX signal comes only from BTC and only once per NEW closed 15m bar.
        btc_cot=int(market["contexts"]["BTCUSDT"].iloc[-1]["open_time"])
        flex_new_bar=(
            self.state.flex_last_processed_closed_open_time is None
            or btc_cot > self.state.flex_last_processed_closed_open_time
        )
        if flex_new_bar:
            self.state.flex_last_processed_closed_open_time=btc_cot
            fc=self.state.flex_cycle
            if fc is None:
                if (
                    new_risk_enabled
                    and self.client.timestamp_ms()>=self.state.flex_cooldown_until_ms
                    and self._flex_entry_allowed(market)
                    and self.state.flex_pending is None
                ):
                    self.state.flex_pending=PendingAction(
                        kind="OPEN",symbol="BTCUSDT",
                        meta_state=market["meta"]["state"],
                        portfolio_dd=account["portfolio_dd"],
                    )
            elif new_risk_enabled and not fc.recovery and self.state.flex_pending is None:
                if fc.entry_actions==1:
                    threshold=fc.long_avg*.98
                elif fc.entry_actions==2:
                    threshold=fc.long_avg*.95
                else:
                    threshold=(fc.last_ref_price or fc.long_avg)*.95
                if (fc.entry_actions<8 or fc.tp_stage>0) and self._flex_add_confirm(market,threshold):
                    self.state.flex_pending=PendingAction(kind="ADD",symbol="BTCUSDT")

        # Range Alpha evaluates exactly one newly closed BTC 15m bar.
        # Unlike FLEX it places its maker entry immediately after that close.
        btc15=market["btc15"]
        if btc15 is not None and len(btc15)>=2:
            ra_open=int(btc15.iloc[-1]["open_time"])
            ra_last=self.state.range_last_processed_closed_open_time
            if ra_last is None or ra_open>ra_last:
                if self.state.range_cycle is not None or self.state.range_pending is not None:
                    self.state.range_last_processed_closed_open_time=ra_open
                elif not new_risk_enabled or market["meta"].get("state")!="RANGE":
                    self.state.range_last_processed_closed_open_time=ra_open
                else:
                    diag=self._range_entry_diagnostic(market,include_micro=True)
                    micro=diag.get("micro")
                    # If the only missing item is freshly-closed 5m data, retry
                    # this same 15m signal on the next 20-second poll instead of
                    # permanently discarding it because of Binance data latency.
                    retry_micro=bool(
                        diag.get("quality",{}).get("pass")
                        and diag.get("edge_ok")
                        and diag.get("stabilize")
                        and micro is not None
                        and not micro.get("available",False)
                    )
                    if not retry_micro:
                        self.state.range_last_processed_closed_open_time=ra_open
                        if diag.get("eligible_now"):
                            self._place_range_entry(market,diag,events)

        if not new_risk_enabled:
            self.suppress_new_risk_pending()

        # A maker fill can occur while the rest of this cycle is being processed.
        # Synchronize once more before final position reconciliation.
        self._sync_range_orders(events=events, bootstrap=False)

        # Final real-account snapshot and reconciliation after fills.
        account=self._account_snapshot()
        self.reconcile_or_halt()
        self.save()

        status=self._build_status(market,account,events,new_risk_enabled)
        tmp=self.status_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(status,ensure_ascii=False,indent=2,default=self._json_default),encoding="utf-8")
        tmp.replace(self.status_file)
        return status

    # --------------------------------------------------------
    # Status / entry diagnostics
    # --------------------------------------------------------

    def _trend_entry_diagnostic(self, market, symbol: str, new_risk_enabled: bool) -> dict:
        """
        Explain the exact four core conditions used by Strategy v1.1 for a
        NEW Trend entry.  This is intentionally separate from the global
        Meta 0~6 score.

        Entry Score (0~4):
          1) valid 4H directional bias
          2) 1H pullback
          3) fresh 15m trigger
          4) 4H ADX14 >= locked ADX threshold (20)

        Cooldown / new-risk / pending state are execution gates and are shown
        separately instead of being counted in the 0~4 score.
        """
        ctx = market["contexts"].get(symbol)
        if ctx is None or len(ctx) < 2:
            return {
                "available": False,
                "score": 0,
                "max_score": 4,
                "reason": "insufficient completed 15m context",
            }

        row = ctx.iloc[-1]
        prev = ctx.iloc[-2]
        bias = base.sim.four_hour_bias(row)
        bias_ok = bias in ("LONG", "SHORT")

        pullback_ok = bool(
            bias_ok and base.sim.one_hour_pullback(row, bias)
        )
        trigger_ok = bool(
            bias_ok and base.sim.entry_trigger(row, prev, bias)
        )

        adx_raw = row.get("q4_adx14")
        adx = (
            float(adx_raw)
            if adx_raw is not None and not pd.isna(adx_raw)
            else None
        )
        adx_threshold = float(getattr(base, "ADX_THRESHOLD", 20.0))
        adx_ok = bool(adx is not None and adx >= adx_threshold)

        score = int(bias_ok) + int(pullback_ok) + int(trigger_ok) + int(adx_ok)
        cooldown = int(self.state.trend_cooldown.get(symbol, 0))
        pending = self.state.trend_pending.get(symbol)
        pending_kind = pending.kind if pending is not None else None

        return {
            "available": True,
            "score": score,
            "max_score": 4,
            "direction": bias,
            "entry_allowed_core": bool(score == 4),
            "eligible_now": bool(
                score == 4
                and new_risk_enabled
                and cooldown <= 0
                and self.state.trend_active.get(symbol) is None
            ),
            "gates": {
                "new_risk_enabled": bool(new_risk_enabled),
                "cooldown_ready": cooldown <= 0,
                "cooldown_bars": cooldown,
                "pending_kind": pending_kind,
            },
            "conditions": {
                "h4_bias": {
                    "ok": bias_ok,
                    "value": bias,
                    "structure": row.get("h4_structure"),
                    "close": (float(row["h4_close"]) if not pd.isna(row.get("h4_close")) else None),
                    "ema20": (float(row["h4_ema20"]) if not pd.isna(row.get("h4_ema20")) else None),
                    "ema50": (float(row["h4_ema50"]) if not pd.isna(row.get("h4_ema50")) else None),
                },
                "h1_pullback": {
                    "ok": pullback_ok,
                    "close": (float(row["h1_close"]) if not pd.isna(row.get("h1_close")) else None),
                    "ema20": (float(row["h1_ema20"]) if not pd.isna(row.get("h1_ema20")) else None),
                    "ema50": (float(row["h1_ema50"]) if not pd.isna(row.get("h1_ema50")) else None),
                    "atr14": (float(row["h1_atr14"]) if not pd.isna(row.get("h1_atr14")) else None),
                },
                "m15_fresh_trigger": {
                    "ok": trigger_ok,
                    "structure": row.get("structure"),
                    "prev_structure": prev.get("structure"),
                    "close": (float(row["close"]) if not pd.isna(row.get("close")) else None),
                    "ema20": (float(row["ema20"]) if not pd.isna(row.get("ema20")) else None),
                },
                "adx20": {
                    "ok": adx_ok,
                    "value": adx,
                    "threshold": adx_threshold,
                },
            },
        }

    def _build_status(self,market,account,events,new_risk_enabled):
        active={}
        for s,tr in self.state.trend_active.items():
            if tr is None:
                active[s]=None
            else:
                p=market["prices"][s]
                active[s]={
                    "trade_id":tr.trade_id,
                    "direction":tr.direction,
                    "units":len(tr.units),
                    "qty":trend_qty(tr),
                    "avg_entry":trend_avg(tr),
                    "pnl":trend_pnl(tr,p),
                    "risk_mult":tr.meta_risk_mult,
                    "locked_unit_notional":tr.locked_unit_notional,
                }
        fc=self.state.flex_cycle
        flex=None
        if fc is not None:
            p=market["prices"]["BTCUSDT"]
            flex={
                "cycle_id":fc.cycle_id,
                "entries":fc.entry_actions,
                "long_qty":fc.long_qty,
                "short_qty":fc.short_qty,
                "avg_entry":fc.long_avg,
                "pnl":flex_pnl(fc,p),
                "recovery":fc.recovery,
                "tp_stage":fc.tp_stage,
                "risk_mult":fc.meta_risk_mult,
            }

        rc=self.state.range_cycle
        rp=self.state.range_pending
        range_alpha=None
        if rc is not None:
            p=float(market["prices"][RANGE_ALPHA_SYMBOL])
            range_alpha={
                "state":"ACTIVE",
                "cycle_id":rc.cycle_id,
                "qty":rc.qty,
                "initial_qty":rc.initial_qty,
                "avg_entry":rc.avg_entry,
                "pnl":rc.realized_pnl + rc.qty*(p-rc.avg_entry) - rc.fees,
                "tp_price":rc.tp_price,
                "stop_price":rc.stop_price,
                "expires_at_ms":rc.expires_at_ms,
                "tp_order_id":rc.tp_order_id,
                "tp_client_order_id":rc.tp_client_order_id,
            }
        elif rp is not None:
            range_alpha={
                "state":"PENDING_ENTRY",
                "cycle_id":rp.cycle_id,
                "qty":rp.qty,
                "partial_qty":float(getattr(rp,"partial_qty",0.0) or 0.0),
                "limit_price":rp.limit_price,
                "signal_price":rp.signal_price,
                "expires_at_ms":rp.expires_at_ms,
                "order_id":rp.order_id,
                "client_order_id":rp.client_order_id,
            }
        range_diag=self._range_entry_diagnostic(market,include_micro=False)
        range_diag["new_risk_enabled"]=bool(new_risk_enabled)
        range_diag["fixed_qty"]=(
            RANGE_ALPHA_QTY_BELOW_100K
            if float(market["prices"][RANGE_ALPHA_SYMBOL]) < RANGE_ALPHA_PRICE_THRESHOLD
            else RANGE_ALPHA_QTY_AT_OR_ABOVE_100K
        )
        return {
            "strategy":STRATEGY_VERSION,
            "mode":"LIVE",
            "updated_at_ms":self.client.timestamp_ms(),
            "new_risk_enabled":new_risk_enabled,
            "meta":market["meta"],
            "account":account,
            "starting_equity":self.state.starting_equity,
            "peak_equity":self.state.peak_equity,
            "capital_scale":self.state.capital_scale,
            "trend_lock":self.state.trend_lock,
            "flex_lock":self.state.flex_lock,
            "trend_active":active,
            "trend_entry_diagnostics": {
                s: self._trend_entry_diagnostic(
                    market, s, new_risk_enabled
                )
                for s in SYMBOLS
            },
            "flex":flex,
            "range_alpha":range_alpha,
            "range_alpha_diagnostic":range_diag,
            "pending":{
                "trend":{
                    s:(asdict(p) if p else None)
                    for s,p in self.state.trend_pending.items()
                },
                "flex":asdict(self.state.flex_pending) if self.state.flex_pending else None,
                "range":asdict(self.state.range_pending) if self.state.range_pending else None,
            },
            "events":events,
            "halted":self.state.halted,
            "halt_reason":self.state.halt_reason,
        }
