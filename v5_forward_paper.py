#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
V5 — Strategy v1.1 Forward Paper Trading
========================================

NO REAL ORDERS.
NO API KEY.
NO PRIVATE BINANCE ENDPOINTS.

This script uses only Binance USDⓈ-M Futures public market data and
simulates Strategy v1.1 forward in real time.

Required in same folder:
    v4_sim_beta.py
    v4_trend_quality_test.py
    v5_forward_paper.py

Strategy v1.1 (LOCKED)
----------------------
Universe:
    BTCUSDT / ETHUSDT

New entry:
    Original Strategy v1.0 setup
    +
    4H ADX14 >= 20

Management:
    1 Unit = 80 USDT notional
    Max 7 Units
    0% Hedge
    No special Re-entry
    1H structure invalidation exit
    Opposite 4H bias exit
    1% Trade Idea hard risk
    Structural partial TP
    Pyramiding only while core position is profitable

Paper assumptions:
    Starting equity = 500 USDT
    Leverage = 10x (reporting only)
    Taker fee = 0.05%
    Slippage = 2 bps

Forward integrity:
    - Decisions are made only from CLOSED 15m candles.
    - Signal confirmed on a closed 15m candle.
    - Action executes at the NEXT 15m candle open.
    - Persistent state survives restarts.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests

import v4_sim_beta as sim
import v4_trend_quality_test as tq


# ============================================================
# LOCKED STRATEGY / PAPER CONFIG
# ============================================================

STRATEGY_VERSION = "1.1"
FRAMEWORK_VERSION = "V5_FORWARD_PAPER"

SYMBOLS = ["BTCUSDT", "ETHUSDT"]

STARTING_EQUITY = 500.0
UNIT_NOTIONAL = 80.0
MAX_UNITS = 7
LEVERAGE = 10.0

ACCOUNT_RISK = 0.01

TAKER_FEE_RATE = 0.0005
SLIPPAGE_BPS = 2.0

ADX_THRESHOLD = 20.0

POLL_SECONDS = 60

# We need enough 15m history to build stable 4H indicators.
KLINE_LIMIT = 1000

BASE_URL = "https://fapi.binance.com"

STATE_DIR = Path("forward_paper_state")
STATE_FILE = STATE_DIR / "paper_state.json"

EVENTS_FILE = STATE_DIR / "paper_events.csv"
TRADES_FILE = STATE_DIR / "paper_trades.csv"
EQUITY_FILE = STATE_DIR / "paper_equity.csv"
STATUS_FILE = STATE_DIR / "paper_status.json"


# ============================================================
# DATA CLASSES
# ============================================================

@dataclass
class PaperUnit:
    entry_price: float
    qty: float
    entry_notional: float
    opened_at: int


@dataclass
class PaperTrade:
    trade_id: str
    symbol: str
    direction: str
    opened_at: int
    trade_start_equity: float

    units: List[PaperUnit] = field(default_factory=list)

    fees: float = 0.0
    funding: float = 0.0
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
    direction: Optional[str] = None
    reason: Optional[str] = None
    swing_time: Optional[int] = None

    # Snapshot of signal context for OPEN.
    signal_context: Optional[dict] = None


@dataclass
class PaperState:
    framework_version: str = FRAMEWORK_VERSION
    strategy_version: str = STRATEGY_VERSION

    cash: float = STARTING_EQUITY

    active: Dict[str, Optional[PaperTrade]] = field(
        default_factory=lambda: {
            "BTCUSDT": None,
            "ETHUSDT": None,
        }
    )

    pending: Dict[str, Optional[PendingAction]] = field(
        default_factory=lambda: {
            "BTCUSDT": None,
            "ETHUSDT": None,
        }
    )

    trade_counter: Dict[str, int] = field(
        default_factory=lambda: {
            "BTCUSDT": 0,
            "ETHUSDT": 0,
        }
    )

    cooldown_bars: Dict[str, int] = field(
        default_factory=lambda: {
            "BTCUSDT": 0,
            "ETHUSDT": 0,
        }
    )

    last_processed_closed_open_time: Dict[str, Optional[int]] = field(
        default_factory=lambda: {
            "BTCUSDT": None,
            "ETHUSDT": None,
        }
    )

    last_funding_time_processed: Dict[str, Optional[int]] = field(
        default_factory=lambda: {
            "BTCUSDT": None,
            "ETHUSDT": None,
        }
    )

    created_at_ms: int = field(
        default_factory=lambda: int(time.time() * 1000)
    )

    updated_at_ms: int = field(
        default_factory=lambda: int(time.time() * 1000)
    )


# ============================================================
# SERIALIZATION
# ============================================================

def unit_from_dict(d: dict) -> PaperUnit:
    return PaperUnit(**d)


def trade_from_dict(d: dict) -> PaperTrade:
    d = dict(d)
    d["units"] = [
        unit_from_dict(x)
        for x in d.get("units", [])
    ]
    return PaperTrade(**d)


def pending_from_dict(d: dict) -> PendingAction:
    return PendingAction(**d)


def state_to_dict(state: PaperState) -> dict:
    out = asdict(state)
    return out


def state_from_dict(d: dict) -> PaperState:
    state = PaperState()

    state.framework_version = d.get(
        "framework_version",
        FRAMEWORK_VERSION,
    )
    state.strategy_version = d.get(
        "strategy_version",
        STRATEGY_VERSION,
    )
    state.cash = float(
        d.get(
            "cash",
            STARTING_EQUITY,
        )
    )

    state.active = {}
    for symbol in SYMBOLS:
        item = d.get("active", {}).get(symbol)
        state.active[symbol] = (
            trade_from_dict(item)
            if item
            else None
        )

    state.pending = {}
    for symbol in SYMBOLS:
        item = d.get("pending", {}).get(symbol)
        state.pending[symbol] = (
            pending_from_dict(item)
            if item
            else None
        )

    state.trade_counter = {
        s: int(
            d.get(
                "trade_counter",
                {},
            ).get(s, 0)
        )
        for s in SYMBOLS
    }

    state.cooldown_bars = {
        s: int(
            d.get(
                "cooldown_bars",
                {},
            ).get(s, 0)
        )
        for s in SYMBOLS
    }

    state.last_processed_closed_open_time = {
        s: d.get(
            "last_processed_closed_open_time",
            {},
        ).get(s)
        for s in SYMBOLS
    }

    state.last_funding_time_processed = {
        s: d.get(
            "last_funding_time_processed",
            {},
        ).get(s)
        for s in SYMBOLS
    }

    state.created_at_ms = int(
        d.get(
            "created_at_ms",
            int(time.time() * 1000),
        )
    )

    state.updated_at_ms = int(
        d.get(
            "updated_at_ms",
            int(time.time() * 1000),
        )
    )

    return state


def save_state(state: PaperState):
    STATE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    state.updated_at_ms = int(
        time.time() * 1000
    )

    tmp = STATE_FILE.with_suffix(
        ".tmp"
    )

    tmp.write_text(
        json.dumps(
            state_to_dict(state),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    tmp.replace(
        STATE_FILE
    )


def load_state() -> PaperState:
    STATE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    if not STATE_FILE.exists():
        state = PaperState()
        save_state(state)
        return state

    data = json.loads(
        STATE_FILE.read_text(
            encoding="utf-8"
        )
    )

    return state_from_dict(
        data
    )


# ============================================================
# CSV LOG
# ============================================================

def append_csv(
    path: Path,
    row: dict,
):
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    exists = path.exists()

    # Existing header may be narrower than new rows.
    # For this forward version, use a stable superset per file.
    if path == EVENTS_FILE:
        fields = [
            "time",
            "time_ms",
            "trade_id",
            "symbol",
            "action",
            "direction",
            "reason",
            "price",
            "unit_notional",
            "core_units_after",
            "avg_entry_after",
            "realized_pnl",
            "net_trade_pnl",
            "equity",
            "signal_4h_bias",
            "signal_4h_adx14",
            "signal_1h_structure",
            "signal_15m_structure",
        ]
    elif path == TRADES_FILE:
        fields = [
            "trade_id",
            "symbol",
            "direction",
            "opened_at",
            "closed_at",
            "exit_reason",
            "max_units",
            "realized_core",
            "funding",
            "fees",
            "net_pnl",
            "trade_start_equity",
            "return_on_start_equity_pct",
            "max_trade_pnl_seen",
            "min_trade_pnl_seen",
        ]
    else:
        fields = [
            "time",
            "time_ms",
            "equity",
            "cash",
            "active_trades",
            "btc_unrealized",
            "eth_unrealized",
        ]

    normalized = {
        key: row.get(key)
        for key in fields
    }

    with path.open(
        "a",
        newline="",
        encoding="utf-8-sig",
    ) as f:
        writer = csv.DictWriter(
            f,
            fieldnames=fields,
        )

        if not exists:
            writer.writeheader()

        writer.writerow(
            normalized
        )


# ============================================================
# PUBLIC BINANCE
# ============================================================

session = requests.Session()

# Binance rate-limit safety.
#
# Forward Paper only consumes PUBLIC market data.  It does not use
# signed endpoints, therefore exact Binance server-time synchronization
# is unnecessary.  Railway/container clocks are NTP-synchronized well
# enough for deciding whether a 15m candle has closed.
MAX_PUBLIC_GET_RETRIES = 4
DEFAULT_429_BACKOFF_SECONDS = 60

# Funding normally changes only around scheduled funding timestamps.
# Polling it every 20/60 seconds wastes IP request budget.  Delaying the
# accounting by a few minutes does not change which historical funding
# event belongs to a trade because apply_new_funding() checks event time.
FUNDING_POLL_INTERVAL_SECONDS = 300
_last_funding_poll_monotonic = {
    symbol: 0.0
    for symbol in SYMBOLS
}


def _retry_after_seconds(
    response: requests.Response,
    attempt: int,
) -> int:
    raw = response.headers.get(
        "Retry-After"
    )

    if raw:
        try:
            return max(
                1,
                int(
                    float(
                        raw
                    )
                ),
            )
        except (
            TypeError,
            ValueError,
        ):
            pass

    # Conservative fallback if Binance/proxy omitted Retry-After.
    return min(
        300,
        DEFAULT_429_BACKOFF_SECONDS
        * (
            2 ** attempt
        ),
    )


def public_get(
    path: str,
    params: Optional[dict] = None,
):
    last_error = None

    for attempt in range(
        MAX_PUBLIC_GET_RETRIES
    ):
        response = session.get(
            BASE_URL + path,
            params=params,
            timeout=15,
        )

        if response.status_code in (
            418,
            429,
        ):
            wait_seconds = (
                _retry_after_seconds(
                    response,
                    attempt,
                )
            )

            used_weight = (
                response.headers.get(
                    "X-MBX-USED-WEIGHT-1M"
                )
                or response.headers.get(
                    "x-mbx-used-weight-1m"
                )
            )

            print(
                "[binance] rate limited | "
                f"status={response.status_code} | "
                f"path={path} | "
                f"used_weight_1m={used_weight} | "
                f"retry_after={wait_seconds}s",
                flush=True,
            )

            last_error = (
                requests.HTTPError(
                    (
                        f"{response.status_code} "
                        f"rate limit for {path}"
                    ),
                    response=response,
                )
            )

            time.sleep(
                wait_seconds
            )

            continue

        response.raise_for_status()

        return response.json()

    if last_error is not None:
        raise last_error

    raise RuntimeError(
        f"public_get failed unexpectedly: {path}"
    )


def server_time_ms() -> int:
    # Do NOT call /fapi/v1/time every cycle.
    # No signed request in Forward Paper needs server timestamp.
    return int(
        time.time()
        * 1000
    )


def fetch_live_15m(
    symbol: str,
) -> pd.DataFrame:
    raw = public_get(
        "/fapi/v1/klines",
        {
            "symbol": symbol,
            "interval": "15m",
            "limit": KLINE_LIMIT,
        },
    )

    df = pd.DataFrame(
        raw,
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

    for c in [
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]:
        df[c] = pd.to_numeric(
            df[c],
            errors="coerce",
        )

    df["open_time"] = pd.to_numeric(
        df["open_time"],
        errors="coerce",
    ).astype("int64")

    df["close_time"] = pd.to_numeric(
        df["close_time"],
        errors="coerce",
    ).astype("int64")

    return df[
        [
            "open_time",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "close_time",
        ]
    ].sort_values(
        "open_time"
    ).reset_index(
        drop=True
    )


def fetch_recent_funding(
    symbol: str,
    start_ms: Optional[int],
) -> List[dict]:
    now_mono = time.monotonic()

    last_poll = (
        _last_funding_poll_monotonic.get(
            symbol,
            0.0,
        )
    )

    if (
        now_mono
        - last_poll
        < FUNDING_POLL_INTERVAL_SECONDS
    ):
        return []

    params = {
        "symbol": symbol,
        "limit": 100,
    }

    if start_ms is not None:
        params[
            "startTime"
        ] = (
            int(start_ms)
            + 1
        )

    data = public_get(
        "/fapi/v1/fundingRate",
        params,
    )

    _last_funding_poll_monotonic[
        symbol
    ] = now_mono

    return data


# ============================================================
# MARKET CONTEXT
# ============================================================

def prepare_live_context(
    df15_raw: pd.DataFrame,
    now_ms: int,
) -> Tuple[
    pd.DataFrame,
    pd.DataFrame,
]:
    """
    Returns:
        full context built from CLOSED 15m bars
        raw dataframe including current forming bar
    """

    closed = df15_raw[
        df15_raw[
            "close_time"
        ] <= now_ms
    ].copy()

    if len(closed) < 400:
        raise RuntimeError(
            "Not enough closed 15m history."
        )

    df1h = sim.aggregate_from_15m(
        closed,
        1,
    )

    df4h = sim.aggregate_from_15m(
        closed,
        4,
    )

    merged = tq.merge_quality_context(
        closed,
        df1h,
        df4h,
    )

    merged = merged[
        merged[
            "h4_ema50"
        ].notna()
        & merged[
            "h1_ema50"
        ].notna()
        & merged[
            "ema50"
        ].notna()
        & merged[
            "q4_adx14"
        ].notna()
    ].copy()

    return (
        merged.reset_index(
            drop=True
        ),
        df15_raw,
    )


def latest_closed_and_prev(
    context: pd.DataFrame,
) -> Tuple[
    pd.Series,
    Optional[pd.Series],
]:
    if context.empty:
        raise RuntimeError(
            "No usable closed context."
        )

    latest = context.iloc[-1]
    prev = (
        context.iloc[-2]
        if len(context) >= 2
        else None
    )

    return latest, prev


def current_forming_bar(
    raw15: pd.DataFrame,
    now_ms: int,
) -> Optional[pd.Series]:
    forming = raw15[
        raw15[
            "close_time"
        ] > now_ms
    ]

    if forming.empty:
        return None

    return forming.iloc[-1]


# ============================================================
# PAPER MATH
# ============================================================

def slip_price(
    price: float,
    side: str,
) -> float:
    slip = (
        SLIPPAGE_BPS
        / 10000.0
    )

    if side == "BUY":
        return price * (
            1.0 + slip
        )

    return price * (
        1.0 - slip
    )


def fee_for_notional(
    notional: float,
) -> float:
    return (
        abs(notional)
        * TAKER_FEE_RATE
    )


def core_qty(
    trade: PaperTrade,
) -> float:
    return sum(
        u.qty
        for u in trade.units
    )


def avg_entry(
    trade: PaperTrade,
) -> float:
    qty = core_qty(
        trade
    )

    if qty <= 0:
        return math.nan

    return (
        sum(
            u.entry_price
            * u.qty
            for u in trade.units
        )
        / qty
    )


def core_unrealized(
    trade: PaperTrade,
    price: float,
) -> float:
    if trade.direction == "LONG":
        return sum(
            (
                price
                - u.entry_price
            )
            * u.qty
            for u in trade.units
        )

    return sum(
        (
            u.entry_price
            - price
        )
        * u.qty
        for u in trade.units
    )


def total_trade_pnl(
    trade: PaperTrade,
    price: float,
) -> float:
    return (
        trade.realized_core
        + trade.funding
        - trade.fees
        + core_unrealized(
            trade,
            price,
        )
    )


def total_equity(
    state: PaperState,
    prices: Dict[str, float],
) -> float:
    active_pnl = 0.0

    for symbol in SYMBOLS:
        trade = state.active[
            symbol
        ]

        if trade is None:
            continue

        price = prices.get(
            symbol
        )

        if price is None:
            continue

        active_pnl += (
            total_trade_pnl(
                trade,
                price,
            )
        )

    return (
        state.cash
        + active_pnl
    )


# ============================================================
# EVENT HELPERS
# ============================================================

def iso_ms(
    ms: int,
) -> str:
    return datetime.fromtimestamp(
        ms / 1000,
        tz=timezone.utc,
    ).isoformat()


def signal_context(
    row: pd.Series,
) -> dict:
    return {
        "signal_4h_bias":
            sim.four_hour_bias(
                row
            ),

        "signal_4h_adx14":
            (
                float(
                    row.get(
                        "q4_adx14"
                    )
                )
                if (
                    row.get(
                        "q4_adx14"
                    )
                    is not None
                    and not pd.isna(
                        row.get(
                            "q4_adx14"
                        )
                    )
                )
                else None
            ),

        "signal_1h_structure":
            row.get(
                "h1_structure"
            ),

        "signal_15m_structure":
            row.get(
                "structure"
            ),
    }


def log_event(
    state: PaperState,
    trade: Optional[PaperTrade],
    symbol: str,
    action: str,
    timestamp: int,
    price: Optional[float] = None,
    direction: Optional[str] = None,
    reason: Optional[str] = None,
    unit_notional: Optional[float] = None,
    realized_pnl: Optional[float] = None,
    net_trade_pnl: Optional[float] = None,
    context: Optional[dict] = None,
    prices: Optional[Dict[str, float]] = None,
):
    row = {
        "time":
            iso_ms(
                timestamp
            ),

        "time_ms":
            timestamp,

        "trade_id":
            (
                trade.trade_id
                if trade
                else None
            ),

        "symbol":
            symbol,

        "action":
            action,

        "direction":
            (
                direction
                or (
                    trade.direction
                    if trade
                    else None
                )
            ),

        "reason":
            reason,

        "price":
            price,

        "unit_notional":
            unit_notional,

        "core_units_after":
            (
                len(
                    trade.units
                )
                if trade
                else 0
            ),

        "avg_entry_after":
            (
                avg_entry(
                    trade
                )
                if (
                    trade
                    and trade.units
                )
                else None
            ),

        "realized_pnl":
            realized_pnl,

        "net_trade_pnl":
            net_trade_pnl,

        "equity":
            (
                total_equity(
                    state,
                    prices,
                )
                if prices
                else None
            ),
    }

    if context:
        row.update(
            context
        )

    append_csv(
        EVENTS_FILE,
        row,
    )


# ============================================================
# PAPER EXECUTION
# ============================================================

def open_unit(
    state: PaperState,
    trade: PaperTrade,
    raw_price: float,
    timestamp: int,
    action: str,
    prices: Dict[str, float],
    context: Optional[dict] = None,
):
    side = (
        "BUY"
        if trade.direction
        == "LONG"
        else "SELL"
    )

    px = slip_price(
        raw_price,
        side,
    )

    qty = (
        UNIT_NOTIONAL
        / px
    )

    trade.units.append(
        PaperUnit(
            entry_price=px,
            qty=qty,
            entry_notional=
                UNIT_NOTIONAL,
            opened_at=
                timestamp,
        )
    )

    trade.fees += (
        fee_for_notional(
            UNIT_NOTIONAL
        )
    )

    trade.max_units_seen = max(
        trade.max_units_seen,
        len(
            trade.units
        ),
    )

    log_event(
        state,
        trade,
        trade.symbol,
        action,
        timestamp,
        price=px,
        unit_notional=
            UNIT_NOTIONAL,
        context=context,
        prices=prices,
    )


def reduce_newest_unit(
    state: PaperState,
    trade: PaperTrade,
    raw_price: float,
    timestamp: int,
    reason: str,
    prices: Dict[str, float],
):
    if len(
        trade.units
    ) <= 1:
        return

    unit = trade.units.pop()

    side = (
        "SELL"
        if trade.direction
        == "LONG"
        else "BUY"
    )

    px = slip_price(
        raw_price,
        side,
    )

    close_notional = (
        unit.qty
        * px
    )

    pnl = (
        (
            px
            - unit.entry_price
        )
        * unit.qty
        if trade.direction
        == "LONG"
        else
        (
            unit.entry_price
            - px
        )
        * unit.qty
    )

    trade.realized_core += pnl

    trade.fees += (
        fee_for_notional(
            close_notional
        )
    )

    log_event(
        state,
        trade,
        trade.symbol,
        "REDUCE_1_UNIT",
        timestamp,
        price=px,
        reason=reason,
        realized_pnl=pnl,
        prices=prices,
    )


def close_trade(
    state: PaperState,
    trade: PaperTrade,
    raw_price: float,
    timestamp: int,
    reason: str,
    prices: Dict[str, float],
):
    side = (
        "SELL"
        if trade.direction
        == "LONG"
        else "BUY"
    )

    px = slip_price(
        raw_price,
        side,
    )

    while trade.units:
        unit = trade.units.pop()

        close_notional = (
            unit.qty
            * px
        )

        pnl = (
            (
                px
                - unit.entry_price
            )
            * unit.qty
            if trade.direction
            == "LONG"
            else
            (
                unit.entry_price
                - px
            )
            * unit.qty
        )

        trade.realized_core += pnl

        trade.fees += (
            fee_for_notional(
                close_notional
            )
        )

    net = (
        trade.realized_core
        + trade.funding
        - trade.fees
    )

    state.cash += net

    log_event(
        state,
        trade,
        trade.symbol,
        "EXIT",
        timestamp,
        price=px,
        reason=reason,
        net_trade_pnl=net,
        prices=prices,
    )

    record = {
        "trade_id":
            trade.trade_id,

        "symbol":
            trade.symbol,

        "direction":
            trade.direction,

        "opened_at":
            iso_ms(
                trade.opened_at
            ),

        "closed_at":
            iso_ms(
                timestamp
            ),

        "exit_reason":
            reason,

        "max_units":
            trade.max_units_seen,

        "realized_core":
            trade.realized_core,

        "funding":
            trade.funding,

        "fees":
            trade.fees,

        "net_pnl":
            net,

        "trade_start_equity":
            trade.trade_start_equity,

        "return_on_start_equity_pct":
            (
                net
                / trade.trade_start_equity
                * 100
                if trade.trade_start_equity
                > 0
                else None
            ),

        "max_trade_pnl_seen":
            trade.max_pnl_seen,

        "min_trade_pnl_seen":
            trade.min_pnl_seen,
    }

    append_csv(
        TRADES_FILE,
        record,
    )

    state.active[
        trade.symbol
    ] = None

    state.cooldown_bars[
        trade.symbol
    ] = sim.COOLDOWN_BARS


# ============================================================
# FUNDING
# ============================================================

def apply_new_funding(
    state: PaperState,
    symbol: str,
):
    trade = state.active[
        symbol
    ]

    # Still advance processed funding history even if flat,
    # so old funding isn't applied after a later entry.
    last_processed = (
        state.last_funding_time_processed[
            symbol
        ]
    )

    rows = fetch_recent_funding(
        symbol,
        last_processed,
    )

    if not rows:
        return

    latest_seen = (
        last_processed
        or 0
    )

    for item in rows:
        funding_time = int(
            item[
                "fundingTime"
            ]
        )

        if (
            last_processed
            is not None
            and funding_time
            <= last_processed
        ):
            continue

        latest_seen = max(
            latest_seen,
            funding_time,
        )

        if trade is None:
            continue

        # Only charge funding if the trade existed at funding time.
        if trade.opened_at > funding_time:
            continue

        rate = float(
            item[
                "fundingRate"
            ]
        )

        mark = (
            float(
                item[
                    "markPrice"
                ]
            )
            if item.get(
                "markPrice"
            )
            else None
        )

        if (
            mark is None
            or mark <= 0
        ):
            continue

        notional = (
            core_qty(
                trade
            )
            * mark
        )

        funding_pnl = (
            -notional
            * rate
            if trade.direction
            == "LONG"
            else notional
            * rate
        )

        trade.funding += (
            funding_pnl
        )

        append_csv(
            EVENTS_FILE,
            {
                "time":
                    iso_ms(
                        funding_time
                    ),

                "time_ms":
                    funding_time,

                "trade_id":
                    trade.trade_id,

                "symbol":
                    symbol,

                "action":
                    "FUNDING",

                "direction":
                    trade.direction,

                "reason":
                    None,

                "price":
                    mark,

                "realized_pnl":
                    funding_pnl,
            },
        )

    if latest_seen > 0:
        state.last_funding_time_processed[
            symbol
        ] = latest_seen


# ============================================================
# STRATEGY SIGNALS
# ============================================================

def candidate_entry_allowed(
    row: pd.Series,
    prev: Optional[pd.Series],
    bias: str,
) -> bool:
    """
    Strategy v1.1:
        baseline entry
        + locked ADX20 new-entry filter
    """
    if not sim.one_hour_pullback(
        row,
        bias,
    ):
        return False

    if not sim.entry_trigger(
        row,
        prev,
        bias,
    ):
        return False

    adx = row.get(
        "q4_adx14"
    )

    if (
        adx is None
        or pd.isna(
            adx
        )
    ):
        return False

    return (
        float(adx)
        >= ADX_THRESHOLD
    )


def initial_tp_levels(
    row: pd.Series,
    direction: str,
    entry_price: float,
) -> List[float]:
    return sim.initial_tp_levels(
        row,
        direction,
        entry_price,
    )


# ============================================================
# EXECUTE QUEUED ACTION AT CURRENT FORMING BAR OPEN
# ============================================================

def execute_pending_if_due(
    state: PaperState,
    symbol: str,
    forming: Optional[pd.Series],
    prices: Dict[str, float],
):
    action = state.pending[
        symbol
    ]

    if action is None:
        return

    if forming is None:
        return

    raw_open = float(
        forming[
            "open"
        ]
    )

    timestamp = int(
        forming[
            "open_time"
        ]
    )

    trade = state.active[
        symbol
    ]

    if action.kind == "OPEN":
        if trade is not None:
            state.pending[
                symbol
            ] = None
            return

        if state.cooldown_bars[
            symbol
        ] > 0:
            state.pending[
                symbol
            ] = None
            return

        direction = action.direction

        state.trade_counter[
            symbol
        ] += 1

        prefix = (
            "BTC"
            if symbol.startswith(
                "BTC"
            )
            else "ETH"
        )

        trade_id = (
            f"{prefix}-FP-"
            f"{state.trade_counter[symbol]:05d}"
        )

        equity = total_equity(
            state,
            prices,
        )

        trade = PaperTrade(
            trade_id=trade_id,
            symbol=symbol,
            direction=direction,
            opened_at=timestamp,
            trade_start_equity=
                equity,
        )

        state.active[
            symbol
        ] = trade

        open_unit(
            state,
            trade,
            raw_open,
            timestamp,
            "OPEN_1_UNIT",
            prices,
            context=(
                action.signal_context
                or {}
            ),
        )

        # Reconstruct values needed by normal management.
        context = (
            action.signal_context
            or {}
        )

        swing_time = context.get(
            "entry_swing_time"
        )

        trade.last_add_swing_time = (
            int(
                swing_time
            )
            if swing_time
            is not None
            else None
        )

        trade.tp_levels = [
            float(x)
            for x in context.get(
                "tp_levels",
                [],
            )
        ]

        trade.tp_hit = [
            False
        ] * len(
            trade.tp_levels
        )

    elif (
        action.kind == "ADD"
        and trade is not None
    ):
        if len(
            trade.units
        ) < MAX_UNITS:
            open_unit(
                state,
                trade,
                raw_open,
                timestamp,
                "PYRAMID_ADD",
                prices,
            )

            if action.swing_time is not None:
                trade.last_add_swing_time = int(
                    action.swing_time
                )

    elif (
        action.kind == "EXIT"
        and trade is not None
    ):
        close_trade(
            state,
            trade,
            raw_open,
            timestamp,
            action.reason or "EXIT",
            prices,
        )

    state.pending[
        symbol
    ] = None


# ============================================================
# PROCESS ONE NEW CLOSED 15M BAR
# ============================================================

def process_new_closed_bar(
    state: PaperState,
    symbol: str,
    context: pd.DataFrame,
    forming: Optional[pd.Series],
    prices: Dict[str, float],
):
    row, prev = latest_closed_and_prev(
        context
    )

    closed_open_time = int(
        row[
            "open_time"
        ]
    )

    last_processed = (
        state.last_processed_closed_open_time[
            symbol
        ]
    )

    if (
        last_processed is not None
        and closed_open_time
        <= last_processed
    ):
        return False

    # We process only one newly closed latest bar.
    state.last_processed_closed_open_time[
        symbol
    ] = closed_open_time

    if state.cooldown_bars[
        symbol
    ] > 0:
        state.cooldown_bars[
            symbol
        ] -= 1

    trade = state.active[
        symbol
    ]

    close = float(
        row[
            "close"
        ]
    )

    bias = sim.four_hour_bias(
        row
    )

    # --------------------------------------------------------
    # Active trade management
    # --------------------------------------------------------

    if trade is not None:

        trade_pnl = total_trade_pnl(
            trade,
            close,
        )

        trade.max_pnl_seen = max(
            trade.max_pnl_seen,
            trade_pnl,
        )

        trade.min_pnl_seen = min(
            trade.min_pnl_seen,
            trade_pnl,
        )

        # Hard 1% Trade Idea stop.
        hard_loss = (
            -ACCOUNT_RISK
            * trade.trade_start_equity
        )

        if trade_pnl <= hard_loss:
            state.pending[
                symbol
            ] = PendingAction(
                kind="EXIT",
                reason="HARD_RISK_1PCT",
            )
            return True

        opposite_bias = (
            (
                trade.direction
                == "LONG"
                and bias == "SHORT"
            )
            or
            (
                trade.direction
                == "SHORT"
                and bias == "LONG"
            )
        )

        if opposite_bias:
            state.pending[
                symbol
            ] = PendingAction(
                kind="EXIT",
                reason="4H_BIAS_FLIP",
            )
            return True

        if sim.one_hour_invalidated(
            row,
            trade.direction,
        ):
            state.pending[
                symbol
            ] = PendingAction(
                kind="EXIT",
                reason=
                    "1H_STRUCTURE_INVALIDATED",
            )
            return True

        # Structural TP is checked on the just-closed candle's high/low.
        if len(
            trade.units
        ) > 1:
            high = float(
                row[
                    "high"
                ]
            )
            low = float(
                row[
                    "low"
                ]
            )

            for idx, level in enumerate(
                trade.tp_levels
            ):
                if trade.tp_hit[
                    idx
                ]:
                    continue

                hit = (
                    high >= level
                    if trade.direction
                    == "LONG"
                    else low <= level
                )

                if hit:
                    reduce_newest_unit(
                        state,
                        trade,
                        level,
                        int(
                            row[
                                "close_time"
                            ]
                        ),
                        "STRUCTURAL_TP",
                        prices,
                    )

                    trade.tp_hit[
                        idx
                    ] = True

                    # At most one unit reduction per closed bar.
                    break

        # Pyramid only when current core is favorable.
        if (
            state.pending[
                symbol
            ] is None
            and len(
                trade.units
            ) < MAX_UNITS
            and core_unrealized(
                trade,
                close,
            ) > 0
            and bias
            == (
                "LONG"
                if trade.direction
                == "LONG"
                else "SHORT"
            )
            and sim.aligned_15m(
                row,
                trade.direction,
            )
        ):
            if trade.direction == "LONG":
                swing_t = row.get(
                    "last_swing_low_time"
                )
            else:
                swing_t = row.get(
                    "last_swing_high_time"
                )

            if (
                swing_t is not None
                and not pd.isna(
                    swing_t
                )
            ):
                swing_t = int(
                    swing_t
                )

                if (
                    trade.last_add_swing_time
                    is None
                    or swing_t
                    > trade.last_add_swing_time
                ):
                    state.pending[
                        symbol
                    ] = PendingAction(
                        kind="ADD",
                        swing_time=
                            swing_t,
                    )

    # --------------------------------------------------------
    # Flat: Strategy v1.1 NEW ENTRY
    # --------------------------------------------------------

    else:
        if state.cooldown_bars[
            symbol
        ] <= 0:
            if bias in (
                "LONG",
                "SHORT",
            ):
                if candidate_entry_allowed(
                    row,
                    prev,
                    bias,
                ):
                    # Pre-compute original structural TP levels from this
                    # closed signal bar. Entry executes next bar open.
                    prospective_entry = close

                    tps = initial_tp_levels(
                        row,
                        bias,
                        prospective_entry,
                    )

                    if bias == "LONG":
                        swing_t = row.get(
                            "last_swing_low_time"
                        )
                    else:
                        swing_t = row.get(
                            "last_swing_high_time"
                        )

                    ctx = signal_context(
                        row
                    )

                    ctx[
                        "entry_swing_time"
                    ] = (
                        int(
                            swing_t
                        )
                        if (
                            swing_t is not None
                            and not pd.isna(
                                swing_t
                            )
                        )
                        else None
                    )

                    ctx[
                        "tp_levels"
                    ] = [
                        float(x)
                        for x in tps
                    ]

                    state.pending[
                        symbol
                    ] = PendingAction(
                        kind="OPEN",
                        direction=bias,
                        signal_context=ctx,
                    )

    return True


# ============================================================
# STATUS / EQUITY
# ============================================================

def write_status(
    state: PaperState,
    prices: Dict[str, float],
    contexts: Dict[str, pd.DataFrame],
):
    equity = total_equity(
        state,
        prices,
    )

    active_summary = {}

    for symbol in SYMBOLS:
        trade = state.active[
            symbol
        ]

        if trade is None:
            active_summary[
                symbol
            ] = None
            continue

        price = prices.get(
            symbol
        )

        active_summary[
            symbol
        ] = {
            "trade_id":
                trade.trade_id,

            "direction":
                trade.direction,

            "units":
                len(
                    trade.units
                ),

            "notional_estimate":
                (
                    core_qty(
                        trade
                    )
                    * price
                    if price
                    is not None
                    else None
                ),

            "avg_entry":
                avg_entry(
                    trade
                ),

            "mark_price":
                price,

            "trade_total_pnl":
                (
                    total_trade_pnl(
                        trade,
                        price,
                    )
                    if price
                    is not None
                    else None
                ),

            "fees":
                trade.fees,

            "funding":
                trade.funding,

            "tp_levels":
                trade.tp_levels,

            "tp_hit":
                trade.tp_hit,
        }

    market_summary = {}

    for symbol, context in contexts.items():
        if context.empty:
            continue

        row = context.iloc[-1]

        market_summary[
            symbol
        ] = {
            "last_closed_15m":
                iso_ms(
                    int(
                        row[
                            "close_time"
                        ]
                    )
                ),

            "4h_bias":
                sim.four_hour_bias(
                    row
                ),

            "4h_adx14":
                (
                    float(
                        row[
                            "q4_adx14"
                        ]
                    )
                    if not pd.isna(
                        row[
                            "q4_adx14"
                        ]
                    )
                    else None
                ),

            "1h_structure":
                row.get(
                    "h1_structure"
                ),

            "15m_structure":
                row.get(
                    "structure"
                ),

            "pending_action":
                (
                    asdict(
                        state.pending[
                            symbol
                        ]
                    )
                    if state.pending[
                        symbol
                    ]
                    is not None
                    else None
                ),
        }

    status = {
        "framework":
            FRAMEWORK_VERSION,

        "strategy":
            STRATEGY_VERSION,

        "mode":
            "FORWARD_PAPER_ONLY",

        "real_order_capability":
            False,

        "updated_at":
            iso_ms(
                int(
                    time.time()
                    * 1000
                )
            ),

        "cash":
            state.cash,

        "equity":
            equity,

        "paper_return_pct":
            (
                (
                    equity
                    / STARTING_EQUITY
                    - 1.0
                )
                * 100
            ),

        "prices":
            prices,

        "active":
            active_summary,

        "market":
            market_summary,
    }

    STATUS_FILE.write_text(
        json.dumps(
            status,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def log_equity(
    state: PaperState,
    prices: Dict[str, float],
):
    btc = state.active[
        "BTCUSDT"
    ]

    eth = state.active[
        "ETHUSDT"
    ]

    btc_u = (
        total_trade_pnl(
            btc,
            prices[
                "BTCUSDT"
            ],
        )
        if (
            btc is not None
            and "BTCUSDT"
            in prices
        )
        else 0.0
    )

    eth_u = (
        total_trade_pnl(
            eth,
            prices[
                "ETHUSDT"
            ],
        )
        if (
            eth is not None
            and "ETHUSDT"
            in prices
        )
        else 0.0
    )

    now = int(
        time.time()
        * 1000
    )

    append_csv(
        EQUITY_FILE,
        {
            "time":
                iso_ms(
                    now
                ),

            "time_ms":
                now,

            "equity":
                total_equity(
                    state,
                    prices,
                ),

            "cash":
                state.cash,

            "active_trades":
                sum(
                    1
                    for x in state.active.values()
                    if x is not None
                ),

            "btc_unrealized":
                btc_u,

            "eth_unrealized":
                eth_u,
        },
    )


# ============================================================
# ONE CYCLE
# ============================================================

def run_cycle(
    state: PaperState,
    verbose: bool = True,
):
    now_ms = server_time_ms()

    raw_data = {}
    contexts = {}
    forming = {}
    prices = {}

    # Fetch and prepare all symbols first.
    for symbol in SYMBOLS:

        raw15 = fetch_live_15m(
            symbol
        )

        context, raw = (
            prepare_live_context(
                raw15,
                now_ms,
            )
        )

        contexts[
            symbol
        ] = context

        raw_data[
            symbol
        ] = raw

        fbar = current_forming_bar(
            raw,
            now_ms,
        )

        forming[
            symbol
        ] = fbar

        if fbar is not None:
            prices[
                symbol
            ] = float(
                fbar[
                    "close"
                ]
            )
        else:
            prices[
                symbol
            ] = float(
                context.iloc[
                    -1
                ][
                    "close"
                ]
            )

    # Execute any action queued by prior closed bar at current forming bar open.
    for symbol in SYMBOLS:
        execute_pending_if_due(
            state,
            symbol,
            forming[
                symbol
            ],
            prices,
        )

    # Funding before new decisions.
    for symbol in SYMBOLS:
        apply_new_funding(
            state,
            symbol,
        )

    # Process newest newly-closed bar.
    processed_any = False

    for symbol in SYMBOLS:
        processed = (
            process_new_closed_bar(
                state,
                symbol,
                contexts[
                    symbol
                ],
                forming[
                    symbol
                ],
                prices,
            )
        )

        processed_any = (
            processed_any
            or processed
        )

    save_state(
        state
    )

    write_status(
        state,
        prices,
        contexts,
    )

    if processed_any:
        log_equity(
            state,
            prices,
        )

    if verbose:
        equity = total_equity(
            state,
            prices,
        )

        print()
        print(
            "=" * 76
        )

        print(
            f"{iso_ms(now_ms)}"
        )

        print(
            f"Paper Equity : "
            f"{equity:.4f} USDT"
        )

        print(
            f"Cash         : "
            f"{state.cash:.4f} USDT"
        )

        print(
            f"Return       : "
            f"{(equity / STARTING_EQUITY - 1) * 100:+.3f}%"
        )

        for symbol in SYMBOLS:

            row = contexts[
                symbol
            ].iloc[
                -1
            ]

            trade = state.active[
                symbol
            ]

            pending = state.pending[
                symbol
            ]

            print()

            print(
                f"{symbol}"
            )

            print(
                f"  Price     : "
                f"{prices[symbol]}"
            )

            print(
                f"  4H Bias   : "
                f"{sim.four_hour_bias(row)}"
            )

            print(
                f"  4H ADX14  : "
                f"{float(row['q4_adx14']):.2f}"
            )

            print(
                f"  1H Struct : "
                f"{row.get('h1_structure')}"
            )

            print(
                f" 15m Struct : "
                f"{row.get('structure')}"
            )

            if trade is None:
                print(
                    "  Position   : FLAT"
                )
            else:
                pnl = total_trade_pnl(
                    trade,
                    prices[
                        symbol
                    ],
                )

                print(
                    f"  Position   : "
                    f"{trade.direction} "
                    f"{len(trade.units)}U"
                )

                print(
                    f"  Avg Entry  : "
                    f"{avg_entry(trade)}"
                )

                print(
                    f"  Trade PnL  : "
                    f"{pnl:+.4f} USDT"
                )

            if pending is not None:
                print(
                    f"  Pending    : "
                    f"{pending.kind} "
                    f"{pending.direction or ''} "
                    f"{pending.reason or ''}"
                )

        print(
            "=" * 76
        )


# ============================================================
# RESET
# ============================================================

def reset_state():
    if STATE_DIR.exists():
        for p in STATE_DIR.iterdir():
            if p.is_file():
                p.unlink()

    state = PaperState()
    save_state(
        state
    )

    print(
        "Forward paper state reset."
    )

    print(
        f"Starting equity: "
        f"{STARTING_EQUITY:.2f} USDT"
    )


# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Strategy v1.1 "
            "Forward Paper Trading"
        )
    )

    parser.add_argument(
        "--once",
        action="store_true",
        help=(
            "Run one live evaluation cycle "
            "and exit."
        ),
    )

    parser.add_argument(
        "--reset",
        action="store_true",
        help=(
            "Delete paper state/logs and "
            "restart from 500 USDT."
        ),
    )

    parser.add_argument(
        "--poll",
        type=int,
        default=POLL_SECONDS,
        help=(
            "Polling seconds in continuous "
            "mode. Default 20."
        ),
    )

    args = parser.parse_args()

    if args.reset:
        reset_state()
        return

    state = load_state()

    print(
        "=" * 76
    )

    print(
        "Strategy v1.1 Forward Paper Trading"
    )

    print(
        "NO REAL ORDERS / NO API KEY"
    )

    print(
        f"Starting reference equity: "
        f"{STARTING_EQUITY:.2f} USDT"
    )

    print(
        f"State folder: "
        f"{STATE_DIR.resolve()}"
    )

    print(
        "=" * 76
    )

    if args.once:
        run_cycle(
            state,
            verbose=True,
        )
        return

    print(
        "Continuous mode started."
    )

    print(
        "Press Ctrl+C to stop safely."
    )

    try:
        while True:
            try:
                run_cycle(
                    state,
                    verbose=True,
                )

            except requests.RequestException as e:
                print(
                    f"Network/API error: {e}"
                )

            except Exception as e:
                print(
                    f"Cycle error: {e}"
                )

            time.sleep(
                max(
                    5,
                    args.poll,
                )
            )

    except KeyboardInterrupt:
        save_state(
            state
        )

        print()
        print(
            "Stopped. Paper state saved."
        )


if __name__ == "__main__":
    main()
