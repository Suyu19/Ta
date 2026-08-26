#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
V16 — Champion / Challenger Dual Forward Paper
===============================================

NO REAL ORDERS.
NO API KEY.
NO PRIVATE BINANCE ENDPOINTS.

Required in the same folder:
    v4_sim_beta.py
    v4_trend_quality_test.py
    v5_forward_paper.py
    v16_dual_forward_paper.py

CHAMPION
--------
Strategy v1.1 exactly as implemented by v5_forward_paper.py.

It CONTINUES using the existing folder:
    forward_paper_state/

Nothing is reset.

CHALLENGER
----------
Strategy v1.1
+
ONLY a Re-add into a previously reached Pyramid Layer requires:

    4H Continuation Score >= 3 / 4

First Entry:
    unchanged

First-Reach Pyramid:
    unchanged

Structural TP / exits / hard risk:
    unchanged

On the FIRST V16 run, if no Challenger state exists,
the Challenger is cloned from the Champion's CURRENT persistent state.

That means:
    same cash
    same open trades
    same pending actions
    same cooldowns
    same trade counters
    same last processed candle
    same last processed funding

Only FUTURE decisions are allowed to diverge.

IMPORTANT:
Do NOT run v5_forward_paper.py and v16_dual_forward_paper.py
at the same time. Both would write Champion state.

Continuation Score
------------------
Uses only already-CLOSED 4H context.

Lookback:
    2 completed 4H candles (~8h)

1 point each:
    1) same-direction 4H Bias persistence >= 2 closed 4H bars
    2) ADX14 now >= ADX14 two closed 4H bars ago
    3) |EMA20-EMA50| / ATR now >= two closed 4H bars ago
    4) directional 4H price progress vs two bars ago

Range:
    0..4

Re-add classification
---------------------
At an ADD signal:

    target_layer = current_units + 1

If:
    target_layer > trade.max_units_seen

then:
    FIRST_REACH
    -> always allowed, exactly like v1.1

If:
    target_layer <= trade.max_units_seen

then:
    READD
    -> Challenger requires Score >= 3

If blocked:
    the current confirmed HL/LH swing is consumed.
    A NEW swing is required for another Add attempt.

The Champion is never gated.
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import math
import shutil
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

import pandas as pd
import requests

import v5_forward_paper as base


# ============================================================
# DUAL CONFIG
# ============================================================

FRAMEWORK_VERSION = "V16_DUAL_FORWARD_PAPER"
CHAMPION_LABEL = "CHAMPION_V1_1"
CHALLENGER_LABEL = "CHALLENGER_READD_SCORE3"

SCORE_THRESHOLD = 3
SCORE_LOOKBACK_4H_BARS = 2

DUAL_DIR = Path("forward_paper_dual")
ANCHOR_FILE = DUAL_DIR / "dual_anchor.json"
DUAL_STATUS_FILE = DUAL_DIR / "dual_status.json"
DUAL_EQUITY_FILE = DUAL_DIR / "dual_equity.csv"

CHALLENGER_DIR = Path("forward_paper_state_score3")
CHALLENGER_DECISIONS_FILE = (
    CHALLENGER_DIR
    / "readd_score3_decisions.csv"
)


# ============================================================
# TRACK PATH SWITCHING
# ============================================================

@dataclass(frozen=True)
class TrackPaths:
    state_dir: Path

    @property
    def state_file(self):
        return (
            self.state_dir
            / "paper_state.json"
        )

    @property
    def events_file(self):
        return (
            self.state_dir
            / "paper_events.csv"
        )

    @property
    def trades_file(self):
        return (
            self.state_dir
            / "paper_trades.csv"
        )

    @property
    def equity_file(self):
        return (
            self.state_dir
            / "paper_equity.csv"
        )

    @property
    def status_file(self):
        return (
            self.state_dir
            / "paper_status.json"
        )


CHAMPION_PATHS = TrackPaths(
    Path("forward_paper_state")
)

CHALLENGER_PATHS = TrackPaths(
    CHALLENGER_DIR
)


@contextmanager
def use_track_paths(
    paths: TrackPaths,
):
    original = {
        "STATE_DIR":
            base.STATE_DIR,

        "STATE_FILE":
            base.STATE_FILE,

        "EVENTS_FILE":
            base.EVENTS_FILE,

        "TRADES_FILE":
            base.TRADES_FILE,

        "EQUITY_FILE":
            base.EQUITY_FILE,

        "STATUS_FILE":
            base.STATUS_FILE,
    }

    try:
        base.STATE_DIR = (
            paths.state_dir
        )

        base.STATE_FILE = (
            paths.state_file
        )

        base.EVENTS_FILE = (
            paths.events_file
        )

        base.TRADES_FILE = (
            paths.trades_file
        )

        base.EQUITY_FILE = (
            paths.equity_file
        )

        base.STATUS_FILE = (
            paths.status_file
        )

        yield

    finally:
        for key, value in original.items():
            setattr(
                base,
                key,
                value,
            )


# ============================================================
# STATE
# ============================================================

def load_champion():
    with use_track_paths(
        CHAMPION_PATHS
    ):
        return base.load_state()


def load_or_clone_challenger(
    champion,
):
    if (
        CHALLENGER_PATHS
        .state_file
        .exists()
    ):
        with use_track_paths(
            CHALLENGER_PATHS
        ):
            state = base.load_state()

        return (
            state,
            False,
        )

    # Exact deep clone of current Champion persistent state.
    state = base.state_from_dict(
        base.state_to_dict(
            champion
        )
    )

    state.framework_version = (
        FRAMEWORK_VERSION
    )

    state.strategy_version = (
        "1.1+READD_SCORE3"
    )

    with use_track_paths(
        CHALLENGER_PATHS
    ):
        base.save_state(
            state
        )

    return (
        state,
        True,
    )


def save_both(
    champion,
    challenger,
):
    with use_track_paths(
        CHAMPION_PATHS
    ):
        base.save_state(
            champion
        )

    with use_track_paths(
        CHALLENGER_PATHS
    ):
        base.save_state(
            challenger
        )


# ============================================================
# CONTINUATION SCORE
# ============================================================

def augment_regime_metrics(
    context: pd.DataFrame,
) -> pd.DataFrame:
    """
    Adds 2-completed-4H-bar lag values using only already closed
    4H context contained in tq.merge_quality_context output.

    Robustly derives EMA-gap/ATR and Bias persistence if the
    corresponding V13 convenience columns are not present.
    """
    out = context.copy()

    required = [
        "q4_close_time",
        "q4_adx14",
        "h4_close",
    ]

    missing = [
        c
        for c in required
        if c not in out.columns
    ]

    if missing:
        raise RuntimeError(
            "Continuation Score cannot be built. "
            f"Missing context columns: {missing}"
        )

    q = (
        out
        .sort_values(
            "q4_close_time"
        )
        .drop_duplicates(
            subset=[
                "q4_close_time"
            ],
            keep="last",
        )
        .copy()
    )

    # Normalized 4H EMA separation.
    if (
        "q4_ema_gap_atr"
        not in q.columns
    ):
        ema_cols = [
            "h4_ema20",
            "h4_ema50",
            "h4_atr14",
        ]

        missing_ema = [
            c
            for c in ema_cols
            if c not in q.columns
        ]

        if missing_ema:
            raise RuntimeError(
                "Cannot derive q4_ema_gap_atr. "
                f"Missing: {missing_ema}"
            )

        atr = pd.to_numeric(
            q[
                "h4_atr14"
            ],
            errors="coerce",
        ).replace(
            0,
            pd.NA,
        )

        q[
            "q4_ema_gap_atr"
        ] = (
            (
                pd.to_numeric(
                    q[
                        "h4_ema20"
                    ],
                    errors="coerce",
                )
                - pd.to_numeric(
                    q[
                        "h4_ema50"
                    ],
                    errors="coerce",
                )
            )
            .abs()
            / atr
        )

    # Derive the exact live 4H Bias by calling the same Strategy function.
    q[
        "dual_q4_bias"
    ] = q.apply(
        lambda r:
            base.sim.four_hour_bias(
                r
            ),
        axis=1,
    )

    # Consecutive completed-4H Bias run length.
    changed = (
        q[
            "dual_q4_bias"
        ]
        != q[
            "dual_q4_bias"
        ].shift(
            1
        )
    )

    group = changed.cumsum()

    q[
        "dual_q4_bias_persistence"
    ] = (
        q.groupby(
            group
        ).cumcount()
        + 1
    )

    q[
        "dual_q4_adx_lag2"
    ] = (
        pd.to_numeric(
            q[
                "q4_adx14"
            ],
            errors="coerce",
        )
        .shift(
            SCORE_LOOKBACK_4H_BARS
        )
    )

    q[
        "dual_q4_gap_lag2"
    ] = (
        pd.to_numeric(
            q[
                "q4_ema_gap_atr"
            ],
            errors="coerce",
        )
        .shift(
            SCORE_LOOKBACK_4H_BARS
        )
    )

    q[
        "dual_h4_close_lag2"
    ] = (
        pd.to_numeric(
            q[
                "h4_close"
            ],
            errors="coerce",
        )
        .shift(
            SCORE_LOOKBACK_4H_BARS
        )
    )

    attach = q[
        [
            "q4_close_time",
            "q4_ema_gap_atr",
            "dual_q4_bias",
            "dual_q4_bias_persistence",
            "dual_q4_adx_lag2",
            "dual_q4_gap_lag2",
            "dual_h4_close_lag2",
        ]
    ].copy()

    duplicate_cols = [
        c
        for c in attach.columns
        if (
            c != "q4_close_time"
            and c in out.columns
        )
    ]

    if duplicate_cols:
        out = out.drop(
            columns=duplicate_cols
        )

    out = out.merge(
        attach,
        on="q4_close_time",
        how="left",
    )

    return (
        out
        .sort_values(
            "open_time"
        )
        .reset_index(
            drop=True
        )
    )


def continuation_score(
    row: pd.Series,
    direction: str,
) -> dict:
    current_adx = row.get(
        "q4_adx14"
    )

    lag_adx = row.get(
        "dual_q4_adx_lag2"
    )

    current_gap = row.get(
        "q4_ema_gap_atr"
    )

    lag_gap = row.get(
        "dual_q4_gap_lag2"
    )

    current_close = row.get(
        "h4_close"
    )

    lag_close = row.get(
        "dual_h4_close_lag2"
    )

    bias = row.get(
        "dual_q4_bias"
    )

    persistence = row.get(
        "dual_q4_bias_persistence"
    )

    required = [
        current_adx,
        lag_adx,
        current_gap,
        lag_gap,
        current_close,
        lag_close,
        persistence,
    ]

    if any(
        x is None
        or pd.isna(
            x
        )
        for x in required
    ):
        return {
            "available":
                False,

            "score":
                0,

            "bias_persistence":
                False,

            "adx_expanding":
                False,

            "ema_gap_expanding":
                False,

            "directional_progress":
                False,
        }

    bias_persistence = (
        bias
        == direction
        and int(
            persistence
        )
        >= 2
    )

    adx_expanding = (
        float(
            current_adx
        )
        >= float(
            lag_adx
        )
    )

    ema_gap_expanding = (
        float(
            current_gap
        )
        >= float(
            lag_gap
        )
    )

    if direction == "LONG":
        directional_progress = (
            float(
                current_close
            )
            > float(
                lag_close
            )
        )

    else:
        directional_progress = (
            float(
                current_close
            )
            < float(
                lag_close
            )
        )

    score = sum(
        [
            int(
                bias_persistence
            ),
            int(
                adx_expanding
            ),
            int(
                ema_gap_expanding
            ),
            int(
                directional_progress
            ),
        ]
    )

    return {
        "available":
            True,

        "score":
            score,

        "bias_persistence":
            bias_persistence,

        "adx_expanding":
            adx_expanding,

        "ema_gap_expanding":
            ema_gap_expanding,

        "directional_progress":
            directional_progress,
    }


def prepare_dual_context(
    raw15: pd.DataFrame,
    now_ms: int,
) -> Tuple[
    pd.DataFrame,
    pd.DataFrame,
]:
    context, raw = (
        base.prepare_live_context(
            raw15,
            now_ms,
        )
    )

    context = (
        augment_regime_metrics(
            context
        )
    )

    return (
        context,
        raw,
    )


# ============================================================
# CHALLENGER DECISION LOG
# ============================================================

def append_challenger_decision(
    row: dict,
):
    CHALLENGER_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    fields = [
        "time",
        "time_ms",
        "trade_id",
        "symbol",
        "direction",
        "add_type",
        "target_layer",
        "allowed",
        "continuation_score",
        "bias_persistence",
        "adx_expanding",
        "ema_gap_expanding",
        "directional_progress",
        "current_units",
        "max_units_seen",
        "trade_total_pnl",
        "swing_time",
    ]

    exists = (
        CHALLENGER_DECISIONS_FILE
        .exists()
    )

    normalized = {
        key:
            row.get(
                key
            )
        for key in fields
    }

    with (
        CHALLENGER_DECISIONS_FILE
        .open(
            "a",
            newline="",
            encoding="utf-8-sig",
        )
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
# CHALLENGER CLOSED-BAR LOGIC
# ============================================================

def process_challenger_closed_bar(
    state,
    symbol: str,
    context: pd.DataFrame,
    forming: Optional[
        pd.Series
    ],
    prices: Dict[
        str,
        float,
    ],
):
    """
    Exact v1.1 logic except:
        ONLY a Re-add into a previously reached Layer
        requires Continuation Score >= 3.
    """
    row, prev = (
        base.latest_closed_and_prev(
            context
        )
    )

    closed_open_time = int(
        row[
            "open_time"
        ]
    )

    last_processed = (
        state
        .last_processed_closed_open_time[
            symbol
        ]
    )

    if (
        last_processed
        is not None
        and closed_open_time
        <= last_processed
    ):
        return False

    state.last_processed_closed_open_time[
        symbol
    ] = (
        closed_open_time
    )

    if (
        state.cooldown_bars[
            symbol
        ]
        > 0
    ):
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

    bias = base.sim.four_hour_bias(
        row
    )

    # --------------------------------------------------------
    # Active trade — exact v1.1 exits / TP
    # --------------------------------------------------------

    if trade is not None:

        trade_pnl = (
            base.total_trade_pnl(
                trade,
                close,
            )
        )

        trade.max_pnl_seen = max(
            trade.max_pnl_seen,
            trade_pnl,
        )

        trade.min_pnl_seen = min(
            trade.min_pnl_seen,
            trade_pnl,
        )

        hard_loss = (
            -base.ACCOUNT_RISK
            * trade.trade_start_equity
        )

        if (
            trade_pnl
            <= hard_loss
        ):
            state.pending[
                symbol
            ] = (
                base.PendingAction(
                    kind="EXIT",
                    reason=
                        "HARD_RISK_1PCT",
                )
            )

            return True

        opposite_bias = (
            (
                trade.direction
                == "LONG"
                and bias
                == "SHORT"
            )
            or
            (
                trade.direction
                == "SHORT"
                and bias
                == "LONG"
            )
        )

        if opposite_bias:
            state.pending[
                symbol
            ] = (
                base.PendingAction(
                    kind="EXIT",
                    reason=
                        "4H_BIAS_FLIP",
                )
            )

            return True

        if (
            base.sim
            .one_hour_invalidated(
                row,
                trade.direction,
            )
        ):
            state.pending[
                symbol
            ] = (
                base.PendingAction(
                    kind="EXIT",
                    reason=
                        "1H_STRUCTURE_INVALIDATED",
                )
            )

            return True

        # Structural TP — unchanged.
        if (
            len(
                trade.units
            )
            > 1
        ):
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

            for (
                idx,
                level,
            ) in enumerate(
                trade.tp_levels
            ):

                if trade.tp_hit[
                    idx
                ]:
                    continue

                hit = (
                    high
                    >= level
                    if trade.direction
                    == "LONG"
                    else low
                    <= level
                )

                if hit:
                    with use_track_paths(
                        CHALLENGER_PATHS
                    ):
                        base.reduce_newest_unit(
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

                    break

        # ----------------------------------------------------
        # Pyramid
        # ----------------------------------------------------

        if (
            state.pending[
                symbol
            ] is None
            and len(
                trade.units
            )
            < base.MAX_UNITS
            and base.core_unrealized(
                trade,
                close,
            )
            > 0
            and bias
            == (
                "LONG"
                if trade.direction
                == "LONG"
                else "SHORT"
            )
            and base.sim.aligned_15m(
                row,
                trade.direction,
            )
        ):

            if (
                trade.direction
                == "LONG"
            ):
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
                    target_layer = (
                        len(
                            trade.units
                        )
                        + 1
                    )

                    # max_units_seen records whether this layer
                    # was ever reached before this prospective ADD.
                    is_readd = (
                        target_layer
                        <= trade.max_units_seen
                    )

                    if not is_readd:
                        # FIRST REACH is sacred / unchanged.
                        state.pending[
                            symbol
                        ] = (
                            base.PendingAction(
                                kind="ADD",
                                swing_time=
                                    swing_t,
                            )
                        )

                        append_challenger_decision(
                            {
                                "time":
                                    base.iso_ms(
                                        int(
                                            row[
                                                "close_time"
                                            ]
                                        )
                                    ),

                                "time_ms":
                                    int(
                                        row[
                                            "close_time"
                                        ]
                                    ),

                                "trade_id":
                                    trade.trade_id,

                                "symbol":
                                    symbol,

                                "direction":
                                    trade.direction,

                                "add_type":
                                    "FIRST_REACH",

                                "target_layer":
                                    target_layer,

                                "allowed":
                                    True,

                                "continuation_score":
                                    None,

                                "current_units":
                                    len(
                                        trade.units
                                    ),

                                "max_units_seen":
                                    trade.max_units_seen,

                                "trade_total_pnl":
                                    trade_pnl,

                                "swing_time":
                                    swing_t,
                            }
                        )

                    else:
                        flags = (
                            continuation_score(
                                row,
                                trade.direction,
                            )
                        )

                        allowed = (
                            flags[
                                "available"
                            ]
                            and flags[
                                "score"
                            ]
                            >= SCORE_THRESHOLD
                        )

                        append_challenger_decision(
                            {
                                "time":
                                    base.iso_ms(
                                        int(
                                            row[
                                                "close_time"
                                            ]
                                        )
                                    ),

                                "time_ms":
                                    int(
                                        row[
                                            "close_time"
                                        ]
                                    ),

                                "trade_id":
                                    trade.trade_id,

                                "symbol":
                                    symbol,

                                "direction":
                                    trade.direction,

                                "add_type":
                                    "READD",

                                "target_layer":
                                    target_layer,

                                "allowed":
                                    allowed,

                                "continuation_score":
                                    flags[
                                        "score"
                                    ],

                                "bias_persistence":
                                    flags[
                                        "bias_persistence"
                                    ],

                                "adx_expanding":
                                    flags[
                                        "adx_expanding"
                                    ],

                                "ema_gap_expanding":
                                    flags[
                                        "ema_gap_expanding"
                                    ],

                                "directional_progress":
                                    flags[
                                        "directional_progress"
                                    ],

                                "current_units":
                                    len(
                                        trade.units
                                    ),

                                "max_units_seen":
                                    trade.max_units_seen,

                                "trade_total_pnl":
                                    trade_pnl,

                                "swing_time":
                                    swing_t,
                            }
                        )

                        if allowed:
                            state.pending[
                                symbol
                            ] = (
                                base.PendingAction(
                                    kind="ADD",
                                    swing_time=
                                        swing_t,
                                )
                            )

                        else:
                            # Consume this confirmed swing.
                            trade.last_add_swing_time = (
                                swing_t
                            )

                            reason = (
                                "READD_SCORE3_BLOCK "
                                f"score={flags['score']} "
                                f"bias={int(flags['bias_persistence'])} "
                                f"adx={int(flags['adx_expanding'])} "
                                f"gap={int(flags['ema_gap_expanding'])} "
                                f"progress={int(flags['directional_progress'])}"
                            )

                            with use_track_paths(
                                CHALLENGER_PATHS
                            ):
                                base.log_event(
                                    state,
                                    trade,
                                    symbol,
                                    "PYRAMID_BLOCKED",
                                    int(
                                        row[
                                            "close_time"
                                        ]
                                    ),
                                    price=close,
                                    reason=reason,
                                    prices=prices,
                                )

    # --------------------------------------------------------
    # Flat — Entry is EXACT v1.1
    # --------------------------------------------------------

    else:

        if (
            state.cooldown_bars[
                symbol
            ]
            <= 0
        ):

            if bias in (
                "LONG",
                "SHORT",
            ):

                if (
                    base
                    .candidate_entry_allowed(
                        row,
                        prev,
                        bias,
                    )
                ):
                    prospective_entry = (
                        close
                    )

                    tps = (
                        base.initial_tp_levels(
                            row,
                            bias,
                            prospective_entry,
                        )
                    )

                    if bias == "LONG":
                        swing_t = row.get(
                            "last_swing_low_time"
                        )
                    else:
                        swing_t = row.get(
                            "last_swing_high_time"
                        )

                    ctx = (
                        base.signal_context(
                            row
                        )
                    )

                    ctx[
                        "entry_swing_time"
                    ] = (
                        int(
                            swing_t
                        )
                        if (
                            swing_t
                            is not None
                            and not pd.isna(
                                swing_t
                            )
                        )
                        else None
                    )

                    ctx[
                        "tp_levels"
                    ] = [
                        float(
                            x
                        )
                        for x in tps
                    ]

                    state.pending[
                        symbol
                    ] = (
                        base.PendingAction(
                            kind="OPEN",
                            direction=bias,
                            signal_context=ctx,
                        )
                    )

    return True


# ============================================================
# ANCHOR + COMPARISON OUTPUT
# ============================================================

def active_snapshot(
    state,
    prices,
):
    out = {}

    for symbol in base.SYMBOLS:
        trade = state.active[
            symbol
        ]

        if trade is None:
            out[
                symbol
            ] = None

        else:
            out[
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

                "max_units_seen":
                    trade.max_units_seen,

                "avg_entry":
                    base.avg_entry(
                        trade
                    ),

                "trade_pnl":
                    base.total_trade_pnl(
                        trade,
                        prices[
                            symbol
                        ],
                    ),
            }

    return out


def load_anchor():
    if not ANCHOR_FILE.exists():
        return None

    return json.loads(
        ANCHOR_FILE.read_text(
            encoding="utf-8"
        )
    )


def create_anchor_if_needed(
    champion,
    challenger,
    prices,
    now_ms,
):
    DUAL_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    anchor = load_anchor()

    if anchor is not None:
        return anchor

    champion_equity = (
        base.total_equity(
            champion,
            prices,
        )
    )

    challenger_equity = (
        base.total_equity(
            challenger,
            prices,
        )
    )

    anchor = {
        "framework":
            FRAMEWORK_VERSION,

        "anchor_time":
            base.iso_ms(
                now_ms
            ),

        "anchor_time_ms":
            now_ms,

        "champion_equity":
            champion_equity,

        "challenger_equity":
            challenger_equity,

        "champion_cash":
            champion.cash,

        "challenger_cash":
            challenger.cash,

        "prices":
            prices,

        "champion_active":
            active_snapshot(
                champion,
                prices,
            ),

        "challenger_active":
            active_snapshot(
                challenger,
                prices,
            ),

        "note":
            (
                "Challenger cloned from Champion before "
                "prospective divergence. Returns in dual_status "
                "are measured from these anchor equities."
            ),
    }

    ANCHOR_FILE.write_text(
        json.dumps(
            anchor,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return anchor


def append_dual_equity(
    row: dict,
):
    DUAL_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    fields = [
        "time",
        "time_ms",
        "champion_equity",
        "challenger_equity",
        "challenger_minus_champion_usdt",
        "champion_return_since_anchor_pct",
        "challenger_return_since_anchor_pct",
        "challenger_minus_champion_return_pct",
        "champion_cash",
        "challenger_cash",
        "champion_active_trades",
        "challenger_active_trades",
    ]

    exists = (
        DUAL_EQUITY_FILE
        .exists()
    )

    normalized = {
        key:
            row.get(
                key
            )
        for key in fields
    }

    with DUAL_EQUITY_FILE.open(
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


def write_dual_status(
    champion,
    challenger,
    prices,
    contexts,
    anchor,
    now_ms,
    log_equity=False,
):
    DUAL_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    ce = base.total_equity(
        champion,
        prices,
    )

    qe = base.total_equity(
        challenger,
        prices,
    )

    ca = float(
        anchor[
            "champion_equity"
        ]
    )

    qa = float(
        anchor[
            "challenger_equity"
        ]
    )

    cr = (
        (
            ce
            / ca
        )
        - 1.0
    ) * 100

    qr = (
        (
            qe
            / qa
        )
        - 1.0
    ) * 100

    market = {}

    for symbol, context in contexts.items():
        row = context.iloc[
            -1
        ]

        flags_long = continuation_score(
            row,
            "LONG",
        )

        flags_short = continuation_score(
            row,
            "SHORT",
        )

        market[
            symbol
        ] = {
            "price":
                prices[
                    symbol
                ],

            "4h_bias":
                base.sim.four_hour_bias(
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

            "continuation_score_long":
                flags_long,

            "continuation_score_short":
                flags_short,
        }

    status = {
        "framework":
            FRAMEWORK_VERSION,

        "mode":
            "FORWARD_PAPER_ONLY",

        "real_order_capability":
            False,

        "updated_at":
            base.iso_ms(
                now_ms
            ),

        "anchor":
            anchor,

        "champion": {
            "label":
                CHAMPION_LABEL,

            "strategy":
                "v1.1",

            "equity":
                ce,

            "cash":
                champion.cash,

            "return_since_anchor_pct":
                cr,

            "active":
                active_snapshot(
                    champion,
                    prices,
                ),
        },

        "challenger": {
            "label":
                CHALLENGER_LABEL,

            "strategy":
                (
                    "v1.1 + only Re-add "
                    "requires Continuation Score >=3"
                ),

            "equity":
                qe,

            "cash":
                challenger.cash,

            "return_since_anchor_pct":
                qr,

            "active":
                active_snapshot(
                    challenger,
                    prices,
                ),
        },

        "comparison": {
            "challenger_minus_champion_usdt":
                qe - ce,

            "challenger_minus_champion_return_pct":
                qr - cr,
        },

        "market":
            market,
    }

    DUAL_STATUS_FILE.write_text(
        json.dumps(
            status,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    if log_equity:

        append_dual_equity(
            {
                "time":
                    base.iso_ms(
                        now_ms
                    ),

                "time_ms":
                    now_ms,

                "champion_equity":
                    ce,

                "challenger_equity":
                    qe,

                "challenger_minus_champion_usdt":
                    qe - ce,

                "champion_return_since_anchor_pct":
                    cr,

                "challenger_return_since_anchor_pct":
                    qr,

                "challenger_minus_champion_return_pct":
                    qr - cr,

                "champion_cash":
                    champion.cash,

                "challenger_cash":
                    challenger.cash,

                "champion_active_trades":
                    sum(
                        1
                        for x
                        in champion.active.values()
                        if x is not None
                    ),

                "challenger_active_trades":
                    sum(
                        1
                        for x
                        in challenger.active.values()
                        if x is not None
                    ),
            }
        )

    return status


# ============================================================
# ONE DUAL CYCLE
# ============================================================

def run_dual_cycle(
    champion,
    challenger,
    verbose=True,
):
    now_ms = (
        base.server_time_ms()
    )

    contexts = {}
    forming = {}
    prices = {}

    # Fetch each market ONCE. Both tracks see identical data.
    for symbol in base.SYMBOLS:

        raw15 = (
            base.fetch_live_15m(
                symbol
            )
        )

        context, raw = (
            prepare_dual_context(
                raw15,
                now_ms,
            )
        )

        contexts[
            symbol
        ] = context

        fbar = (
            base.current_forming_bar(
                raw,
                now_ms,
            )
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

    anchor = create_anchor_if_needed(
        champion,
        challenger,
        prices,
        now_ms,
    )

    # --------------------------------------------------------
    # Execute actions queued by the PRIOR closed bar.
    # Both tracks use the same forming-bar open.
    # --------------------------------------------------------

    for symbol in base.SYMBOLS:

        with use_track_paths(
            CHAMPION_PATHS
        ):
            base.execute_pending_if_due(
                champion,
                symbol,
                forming[
                    symbol
                ],
                prices,
            )

        with use_track_paths(
            CHALLENGER_PATHS
        ):
            base.execute_pending_if_due(
                challenger,
                symbol,
                forming[
                    symbol
                ],
                prices,
            )

    # --------------------------------------------------------
    # Funding
    # --------------------------------------------------------

    for symbol in base.SYMBOLS:

        with use_track_paths(
            CHAMPION_PATHS
        ):
            base.apply_new_funding(
                champion,
                symbol,
            )

        with use_track_paths(
            CHALLENGER_PATHS
        ):
            base.apply_new_funding(
                challenger,
                symbol,
            )

    # --------------------------------------------------------
    # Closed-bar decisions
    # --------------------------------------------------------

    champion_processed = False
    challenger_processed = False

    for symbol in base.SYMBOLS:

        with use_track_paths(
            CHAMPION_PATHS
        ):
            processed = (
                base.process_new_closed_bar(
                    champion,
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

        champion_processed = (
            champion_processed
            or processed
        )

        processed = (
            process_challenger_closed_bar(
                challenger,
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

        challenger_processed = (
            challenger_processed
            or processed
        )

    # --------------------------------------------------------
    # Persist
    # --------------------------------------------------------

    save_both(
        champion,
        challenger,
    )

    with use_track_paths(
        CHAMPION_PATHS
    ):
        base.write_status(
            champion,
            prices,
            contexts,
        )

        if champion_processed:
            base.log_equity(
                champion,
                prices,
            )

    with use_track_paths(
        CHALLENGER_PATHS
    ):
        base.write_status(
            challenger,
            prices,
            contexts,
        )

        if challenger_processed:
            base.log_equity(
                challenger,
                prices,
            )

    dual_status = write_dual_status(
        champion,
        challenger,
        prices,
        contexts,
        anchor,
        now_ms,
        log_equity=(
            champion_processed
            or challenger_processed
        ),
    )

    if verbose:
        print_dual_status(
            dual_status
        )

    return dual_status


# ============================================================
# CONSOLE
# ============================================================

def format_position(
    item,
):
    if item is None:
        return "FLAT"

    return (
        f"{item['direction']} "
        f"{item['units']}U "
        f"PnL={item['trade_pnl']:+.4f}"
    )


def print_dual_status(
    status,
):
    print()
    print(
        "=" * 88
    )

    print(
        status[
            "updated_at"
        ]
    )

    c = status[
        "champion"
    ]

    q = status[
        "challenger"
    ]

    d = status[
        "comparison"
    ]

    print(
        "CHAMPION v1.1"
    )

    print(
        f"  Equity       : "
        f"{c['equity']:.4f} USDT"
    )

    print(
        f"  Since Anchor : "
        f"{c['return_since_anchor_pct']:+.4f}%"
    )

    print(
        "CHALLENGER Re-add Score>=3"
    )

    print(
        f"  Equity       : "
        f"{q['equity']:.4f} USDT"
    )

    print(
        f"  Since Anchor : "
        f"{q['return_since_anchor_pct']:+.4f}%"
    )

    print(
        f"EDGE (Q-C)     : "
        f"{d['challenger_minus_champion_usdt']:+.4f} USDT "
        f"/ "
        f"{d['challenger_minus_champion_return_pct']:+.4f}%"
    )

    for symbol in base.SYMBOLS:

        print()
        print(
            symbol
        )

        market = status[
            "market"
        ][
            symbol
        ]

        print(
            f"  Price       : "
            f"{market['price']}"
        )

        print(
            f"  4H Bias     : "
            f"{market['4h_bias']}"
        )

        print(
            f"  4H ADX14    : "
            f"{market['4h_adx14']:.2f}"
        )

        direction = market[
            "4h_bias"
        ]

        if direction in (
            "LONG",
            "SHORT",
        ):
            key = (
                "continuation_score_long"
                if direction
                == "LONG"
                else "continuation_score_short"
            )

            score = market[
                key
            ][
                "score"
            ]

            print(
                f"  Cont Score  : "
                f"{score}/4 "
                f"(for {direction})"
            )

        print(
            f"  Champion    : "
            f"{format_position(c['active'][symbol])}"
        )

        print(
            f"  Challenger  : "
            f"{format_position(q['active'][symbol])}"
        )

    print(
        "=" * 88
    )


# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Champion v1.1 vs Challenger "
            "Re-add Continuation Score>=3 "
            "Dual Forward Paper"
        )
    )

    parser.add_argument(
        "--once",
        action="store_true",
        help=(
            "Run one dual live evaluation "
            "cycle and exit."
        ),
    )

    parser.add_argument(
        "--poll",
        type=int,
        default=base.POLL_SECONDS,
        help=(
            "Polling seconds in continuous mode."
        ),
    )

    args = parser.parse_args()

    champion = (
        load_champion()
    )

    challenger, cloned = (
        load_or_clone_challenger(
            champion
        )
    )

    print(
        "=" * 88
    )

    print(
        "V16 Champion / Challenger Dual Forward Paper"
    )

    print(
        "NO REAL ORDERS / NO API KEY"
    )

    print(
        f"Champion state : "
        f"{CHAMPION_PATHS.state_dir.resolve()}"
    )

    print(
        f"Challenger     : "
        f"{CHALLENGER_PATHS.state_dir.resolve()}"
    )

    print(
        f"Dual comparison: "
        f"{DUAL_DIR.resolve()}"
    )

    if cloned:
        print()
        print(
            "CHALLENGER INITIALIZED BY EXACTLY "
            "CLONING CURRENT CHAMPION STATE."
        )

        print(
            "No Champion state was reset."
        )

    print()
    print(
        "IMPORTANT: Do NOT run the old "
        "v5_forward_paper.py simultaneously."
    )

    print(
        "=" * 88
    )

    if args.once:
        run_dual_cycle(
            champion,
            challenger,
            verbose=True,
        )

        return

    print(
        "Continuous dual mode started."
    )

    print(
        "Press Ctrl+C to stop safely."
    )

    try:
        while True:
            try:
                run_dual_cycle(
                    champion,
                    challenger,
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
        save_both(
            champion,
            challenger,
        )

        print()
        print(
            "Stopped. Both paper states saved."
        )


if __name__ == "__main__":
    main()
