#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
V4-SIM epsilon — Trend Quality Filter Diagnostic
=================================================

NO REAL ORDERS.
NO API KEY.
Historical Binance USDⓈ-M Futures simulation only.

Required in the same folder:
    v4_sim_beta.py

Official Strategy v1.0 remains unchanged.

Research question
-----------------
Why did some 90-day windows (e.g. choppy / whipsaw environments)
perform much worse than clean trend windows?

This test changes ONLY the NEW-ENTRY gate.
It does NOT change:
    - 4H / 1H / 15m baseline setup logic
    - 1 Unit = 80 USDT
    - pyramiding
    - structural partial TP
    - 1H structure stop
    - 4H bias exit
    - 1% hard risk
    - hedge (kept at 0% in this diagnostic)

Fixed test config:
    max_units = 7
    hedge_ratio = 0%

Predetermined quality filters
-----------------------------
BASELINE
    No extra filter.

ER_030
    4H Efficiency Ratio (8 bars / ~32h) >= 0.30

ADX_20
    4H ADX14 >= 20

EMA_GAP_030
    abs(4H EMA20 - EMA50) / 4H ATR14 >= 0.30

PERSIST_2
    Same directional 4H Bias has persisted for >= 2 closed 4H bars.

QUALITY_2OF3
    At least 2 of:
        ER8 >= 0.30
        ADX14 >= 20
        EMA gap / ATR >= 0.30

Important:
Thresholds are fixed before seeing this test's result.
The goal is diagnosis / robustness, not optimization.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

import v4_sim_beta as sim


OUTPUT_DIR = Path("backtest_output_epsilon")

TEST_CONFIG = sim.SimConfig(
    max_units=7,
    hedge_ratio=0.0,
)

VARIANTS = [
    "BASELINE",
    "ER_030",
    "ADX_20",
    "EMA_GAP_030",
    "PERSIST_2",
    "QUALITY_2OF3",
]


# ============================================================
# QUALITY METRICS
# ============================================================

def add_adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """
    Wilder-style ADX approximation using EWM(alpha=1/period).
    Uses only current/past closed bars.
    """
    out = df.copy()

    high = out["high"]
    low = out["low"]
    close = out["close"]

    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = pd.Series(
        np.where(
            (up_move > down_move) & (up_move > 0),
            up_move,
            0.0,
        ),
        index=out.index,
    )

    minus_dm = pd.Series(
        np.where(
            (down_move > up_move) & (down_move > 0),
            down_move,
            0.0,
        ),
        index=out.index,
    )

    prev_close = close.shift(1)

    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr = tr.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period,
    ).mean()

    plus_smoothed = plus_dm.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period,
    ).mean()

    minus_smoothed = minus_dm.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period,
    ).mean()

    plus_di = 100 * plus_smoothed / atr.replace(0, np.nan)
    minus_di = 100 * minus_smoothed / atr.replace(0, np.nan)

    dx = (
        100
        * (plus_di - minus_di).abs()
        / (plus_di + minus_di).replace(0, np.nan)
    )

    out["adx14"] = dx.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period,
    ).mean()

    return out


def add_efficiency_ratio(
    df: pd.DataFrame,
    period: int = 8,
) -> pd.DataFrame:
    """
    Kaufman-style Efficiency Ratio:
        abs(C_t - C_t-n) / sum(abs(diff(C)), n)

    ~1 = highly directional
    ~0 = highly noisy / back-and-forth
    """
    out = df.copy()

    direction = (
        out["close"]
        - out["close"].shift(period)
    ).abs()

    volatility = (
        out["close"]
        .diff()
        .abs()
        .rolling(
            period,
            min_periods=period,
        )
        .sum()
    )

    out[f"er{period}"] = (
        direction
        / volatility.replace(0, np.nan)
    )

    return out


def add_bias_persistence(
    df4h_prepared: pd.DataFrame,
) -> pd.DataFrame:
    """
    Reconstruct the exact same directional 4H Bias concept:
      LONG:
        structure BULLISH and close > EMA20 > EMA50
      SHORT:
        structure BEARISH and close < EMA20 < EMA50
      otherwise NEUTRAL

    persistence = consecutive closed 4H bars with same LONG/SHORT bias.
    """
    out = df4h_prepared.copy()

    bias = []

    for _, row in out.iterrows():
        if (
            row["structure"] == "BULLISH"
            and row["close"] > row["ema20"] > row["ema50"]
        ):
            bias.append("LONG")

        elif (
            row["structure"] == "BEARISH"
            and row["close"] < row["ema20"] < row["ema50"]
        ):
            bias.append("SHORT")

        else:
            bias.append("NEUTRAL")

    out["quality_bias"] = bias

    persistence = []
    current = None
    count = 0

    for b in bias:
        if b in ("LONG", "SHORT") and b == current:
            count += 1
        elif b in ("LONG", "SHORT"):
            current = b
            count = 1
        else:
            current = None
            count = 0

        persistence.append(count)

    out["bias_persistence"] = persistence

    return out


def prepare_4h_quality(df4h: pd.DataFrame) -> pd.DataFrame:
    """
    sim.prepare_timeframe:
      EMA / RSI / ATR + online swing structure without future leak.
    """
    out = sim.prepare_timeframe(df4h)
    out = add_adx(out, 14)
    out = add_efficiency_ratio(out, 8)
    out = add_bias_persistence(out)

    out["ema_gap_atr"] = (
        (out["ema20"] - out["ema50"]).abs()
        / out["atr14"].replace(0, np.nan)
    )

    return out


def merge_quality_context(
    df15: pd.DataFrame,
    df1h: pd.DataFrame,
    df4h: pd.DataFrame,
) -> pd.DataFrame:
    """
    Use original beta merge for strategy inputs.
    Then attach ONLY quality metrics from already-closed 4H candles.
    """
    base = sim.merge_context(
        df15,
        df1h,
        df4h,
    ).sort_values(
        "close_time"
    ).copy()

    q4 = prepare_4h_quality(
        df4h
    ).sort_values(
        "close_time"
    ).copy()

    q4 = q4[
        [
            "close_time",
            "adx14",
            "er8",
            "ema_gap_atr",
            "quality_bias",
            "bias_persistence",
        ]
    ].copy()

    q4 = q4.rename(
        columns={
            "close_time": "q4_close_time",
            "adx14": "q4_adx14",
            "er8": "q4_er8",
            "ema_gap_atr": "q4_ema_gap_atr",
            "quality_bias": "q4_bias",
            "bias_persistence": "q4_bias_persistence",
        }
    )

    merged = pd.merge_asof(
        base,
        q4,
        left_on="close_time",
        right_on="q4_close_time",
        direction="backward",
    )

    return merged.reset_index(
        drop=True
    )


# ============================================================
# FILTERS
# ============================================================

def quality_flags(
    row: pd.Series,
    bias: str,
) -> dict:
    er = row.get("q4_er8")
    adx = row.get("q4_adx14")
    gap = row.get("q4_ema_gap_atr")
    persistence = row.get(
        "q4_bias_persistence"
    )
    q_bias = row.get("q4_bias")

    er_ok = (
        er is not None
        and not pd.isna(er)
        and float(er) >= 0.30
    )

    adx_ok = (
        adx is not None
        and not pd.isna(adx)
        and float(adx) >= 20.0
    )

    gap_ok = (
        gap is not None
        and not pd.isna(gap)
        and float(gap) >= 0.30
    )

    persistence_ok = (
        persistence is not None
        and not pd.isna(persistence)
        and int(persistence) >= 2
        and q_bias == bias
    )

    score_2of3 = (
        int(er_ok)
        + int(adx_ok)
        + int(gap_ok)
    )

    return {
        "er_ok": er_ok,
        "adx_ok": adx_ok,
        "gap_ok": gap_ok,
        "persistence_ok": persistence_ok,
        "quality_score_3": score_2of3,
    }


def quality_pass(
    row: pd.Series,
    bias: str,
    variant: str,
) -> bool:
    if variant == "BASELINE":
        return True

    flags = quality_flags(
        row,
        bias,
    )

    if variant == "ER_030":
        return flags["er_ok"]

    if variant == "ADX_20":
        return flags["adx_ok"]

    if variant == "EMA_GAP_030":
        return flags["gap_ok"]

    if variant == "PERSIST_2":
        return flags["persistence_ok"]

    if variant == "QUALITY_2OF3":
        return (
            flags["quality_score_3"]
            >= 2
        )

    raise ValueError(
        f"Unknown variant: {variant}"
    )


# ============================================================
# DATA
# ============================================================

def prepare_data(
    days: int,
    refresh: bool,
):
    validation_end = (
        sim.floor_15m(
            sim.utc_now()
        )
        - timedelta(
            milliseconds=1
        )
    )

    validation_start = (
        validation_end
        - timedelta(
            days=days
        )
    )

    download_start = (
        validation_start
        - timedelta(
            days=45
        )
    )

    download_start_ms = (
        sim.dt_to_ms(
            download_start
        )
    )

    validation_end_ms = (
        sim.dt_to_ms(
            validation_end
        )
    )

    full_datasets: Dict[
        str,
        pd.DataFrame
    ] = {}

    funding_maps: Dict[
        str,
        Dict[
            int,
            Tuple[
                float,
                float
            ]
        ]
    ] = {}

    for symbol in sim.SYMBOLS:

        print(
            f"[{symbol}] download/cache..."
        )

        df15, funding = (
            sim.load_or_fetch_symbol(
                symbol,
                download_start_ms,
                validation_end_ms,
                refresh=refresh,
            )
        )

        df1h = (
            sim.aggregate_from_15m(
                df15,
                1,
            )
        )

        df4h = (
            sim.aggregate_from_15m(
                df15,
                4,
            )
        )

        merged = (
            merge_quality_context(
                df15,
                df1h,
                df4h,
            )
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
            & merged[
                "q4_er8"
            ].notna()
            & merged[
                "q4_ema_gap_atr"
            ].notna()
        ].copy()

        full_datasets[
            symbol
        ] = (
            merged.reset_index(
                drop=True
            )
        )

        funding_maps[
            symbol
        ] = (
            sim.funding_events_map(
                funding
            )
        )

        print(
            f"  usable candles="
            f"{len(merged):,}, "
            f"funding={len(funding):,}"
        )

    return (
        validation_start,
        validation_end,
        full_datasets,
        funding_maps,
    )


def window_data(
    full_datasets,
    full_funding_maps,
    start_dt,
    end_dt,
):
    start_ms = (
        sim.dt_to_ms(
            start_dt
        )
    )

    end_ms = (
        sim.dt_to_ms(
            end_dt
        )
    )

    datasets = {
        symbol:
            sim.filter_dataset_window(
                df,
                start_ms,
                end_ms,
            )
        for symbol, df
        in full_datasets.items()
    }

    funds = {
        symbol:
            sim.filter_funding_window(
                fmap,
                start_ms,
                end_ms,
            )
        for symbol, fmap
        in full_funding_maps.items()
    }

    return (
        datasets,
        funds,
    )


# ============================================================
# FILTERED BACKTEST
# ============================================================

def run_variant(
    variant: str,
    datasets,
    funds,
    starting_equity: float,
):
    original_entry = (
        sim.entry_trigger
    )

    def filtered_entry(
        row,
        prev,
        bias,
    ):
        if not original_entry(
            row,
            prev,
            bias,
        ):
            return False

        return quality_pass(
            row,
            bias,
            variant,
        )

    sim.entry_trigger = (
        filtered_entry
    )

    try:
        result = (
            sim.run_backtest(
                datasets=datasets,
                funding_maps=funds,
                config=TEST_CONFIG,
                starting_equity=
                    starting_equity,
            )
        )
    finally:
        sim.entry_trigger = (
            original_entry
        )

    return result


# ============================================================
# BASELINE ENTRY DIAGNOSTIC
# ============================================================

def iso_to_ms(
    value: str,
) -> int:
    return int(
        datetime
        .fromisoformat(
            value
        )
        .timestamp()
        * 1000
    )


def signal_row_for_trade(
    df: pd.DataFrame,
    opened_at_ms: int,
) -> Optional[pd.Series]:
    """
    Trade executes at 15m open t.
    Entry signal was confirmed on previous 15m close.
    """
    values = (
        df["open_time"]
        .astype("int64")
        .to_numpy()
    )

    pos = values.searchsorted(
        opened_at_ms,
        side="left",
    )

    signal_idx = (
        int(pos)
        - 1
    )

    if (
        signal_idx < 0
        or signal_idx
        >= len(df)
    ):
        return None

    return df.iloc[
        signal_idx
    ]


def segment_label(
    timestamp_ms: int,
    segments,
    segment_days: int,
) -> str:
    dt = datetime.fromtimestamp(
        timestamp_ms / 1000,
        tz=timezone.utc,
    )

    for (
        name,
        start,
        end,
    ) in segments:

        if (
            start
            <= dt
            <= end
        ):
            actual_days = (
                (
                    end
                    - start
                )
                .total_seconds()
                / 86400
            )

            kind = (
                "FULL"
                if actual_days
                >= segment_days - 1
                else "PARTIAL"
            )

            return (
                f"{name}_{kind}"
            )

    return "UNKNOWN"


def baseline_trade_quality(
    trades: List[dict],
    datasets,
    validation_start,
    validation_end,
    segment_days,
) -> List[dict]:
    segments = (
        sim.build_non_overlapping_segments(
            validation_start,
            validation_end,
            segment_days,
        )
    )

    rows = []

    for trade in trades:
        symbol = trade[
            "symbol"
        ]

        opened_ms = (
            iso_to_ms(
                trade[
                    "opened_at"
                ]
            )
        )

        signal = (
            signal_row_for_trade(
                datasets[
                    symbol
                ],
                opened_ms,
            )
        )

        if signal is None:
            continue

        bias = trade[
            "direction"
        ]

        flags = quality_flags(
            signal,
            bias,
        )

        rows.append(
            {
                "trade_id":
                    trade[
                        "trade_id"
                    ],

                "symbol":
                    symbol,

                "direction":
                    bias,

                "segment":
                    segment_label(
                        opened_ms,
                        segments,
                        segment_days,
                    ),

                "opened_at":
                    trade[
                        "opened_at"
                    ],

                "closed_at":
                    trade[
                        "closed_at"
                    ],

                "exit_reason":
                    trade[
                        "exit_reason"
                    ],

                "net_pnl":
                    trade[
                        "net_pnl"
                    ],

                "winner":
                    trade[
                        "net_pnl"
                    ] > 0,

                "max_units":
                    trade[
                        "max_units"
                    ],

                "q4_er8":
                    signal.get(
                        "q4_er8"
                    ),

                "q4_adx14":
                    signal.get(
                        "q4_adx14"
                    ),

                "q4_ema_gap_atr":
                    signal.get(
                        "q4_ema_gap_atr"
                    ),

                "q4_bias":
                    signal.get(
                        "q4_bias"
                    ),

                "q4_bias_persistence":
                    signal.get(
                        "q4_bias_persistence"
                    ),

                "pass_er_030":
                    flags[
                        "er_ok"
                    ],

                "pass_adx_20":
                    flags[
                        "adx_ok"
                    ],

                "pass_ema_gap_030":
                    flags[
                        "gap_ok"
                    ],

                "pass_persist_2":
                    flags[
                        "persistence_ok"
                    ],

                "quality_score_3":
                    flags[
                        "quality_score_3"
                    ],

                "pass_quality_2of3":
                    (
                        flags[
                            "quality_score_3"
                        ]
                        >= 2
                    ),
            }
        )

    return rows


def quality_diagnostic_summary(
    rows: List[dict],
) -> List[dict]:
    if not rows:
        return []

    df = pd.DataFrame(
        rows
    )

    groups = [
        (
            "ALL",
            df,
        ),
        (
            "ALL_WINNERS",
            df[
                df[
                    "winner"
                ]
            ],
        ),
        (
            "ALL_LOSERS",
            df[
                ~df[
                    "winner"
                ]
            ],
        ),
    ]

    for segment, sub in (
        df.groupby(
            "segment"
        )
    ):
        groups.append(
            (
                f"{segment}_ALL",
                sub,
            )
        )

        groups.append(
            (
                f"{segment}_WINNERS",
                sub[
                    sub[
                        "winner"
                    ]
                ],
            )
        )

        groups.append(
            (
                f"{segment}_LOSERS",
                sub[
                    ~sub[
                        "winner"
                    ]
                ],
            )
        )

    for direction, sub in (
        df.groupby(
            "direction"
        )
    ):
        groups.append(
            (
                f"{direction}_ALL",
                sub,
            )
        )

        groups.append(
            (
                f"{direction}_WINNERS",
                sub[
                    sub[
                        "winner"
                    ]
                ],
            )
        )

        groups.append(
            (
                f"{direction}_LOSERS",
                sub[
                    ~sub[
                        "winner"
                    ]
                ],
            )
        )

    output = []

    for name, sub in groups:

        if sub.empty:
            continue

        output.append(
            {
                "group":
                    name,

                "trades":
                    len(
                        sub
                    ),

                "net_pnl":
                    float(
                        sub[
                            "net_pnl"
                        ].sum()
                    ),

                "win_rate_pct":
                    float(
                        sub[
                            "winner"
                        ].mean()
                        * 100
                    ),

                "median_er8":
                    float(
                        sub[
                            "q4_er8"
                        ].median()
                    ),

                "median_adx14":
                    float(
                        sub[
                            "q4_adx14"
                        ].median()
                    ),

                "median_ema_gap_atr":
                    float(
                        sub[
                            "q4_ema_gap_atr"
                        ].median()
                    ),

                "median_bias_persistence":
                    float(
                        sub[
                            "q4_bias_persistence"
                        ].median()
                    ),

                "pct_pass_er_030":
                    float(
                        sub[
                            "pass_er_030"
                        ].mean()
                        * 100
                    ),

                "pct_pass_adx_20":
                    float(
                        sub[
                            "pass_adx_20"
                        ].mean()
                        * 100
                    ),

                "pct_pass_ema_gap_030":
                    float(
                        sub[
                            "pass_ema_gap_030"
                        ].mean()
                        * 100
                    ),

                "pct_pass_persist_2":
                    float(
                        sub[
                            "pass_persist_2"
                        ].mean()
                        * 100
                    ),

                "pct_pass_quality_2of3":
                    float(
                        sub[
                            "pass_quality_2of3"
                        ].mean()
                        * 100
                    ),
            }
        )

    return output


# ============================================================
# MAIN
# ============================================================

def main():
    parser = (
        argparse.ArgumentParser(
            description=(
                "V4-SIM epsilon "
                "trend quality diagnostic"
            )
        )
    )

    parser.add_argument(
        "--days",
        type=int,
        default=365,
    )

    parser.add_argument(
        "--segment-days",
        type=int,
        default=90,
    )

    parser.add_argument(
        "--equity",
        type=float,
        default=500.0,
    )

    parser.add_argument(
        "--refresh",
        action="store_true",
    )

    args = (
        parser.parse_args()
    )

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    (
        validation_start,
        validation_end,
        full_datasets,
        funding_maps,
    ) = prepare_data(
        args.days,
        args.refresh,
    )

    full_data, full_funds = (
        window_data(
            full_datasets,
            funding_maps,
            validation_start,
            validation_end,
        )
    )

    print()
    print("=" * 82)
    print(
        "V4-SIM epsilon — Trend Quality Filter Diagnostic"
    )
    print(
        "Official Strategy v1.0 remains unchanged."
    )
    print(
        "Only NEW ENTRY is filtered."
    )
    print(
        "Test config: 7 Units / 0% Hedge"
    )
    print("=" * 82)

    full_rows = []
    segment_rows = []

    baseline_trades = None
    baseline_events = None
    baseline_equity = None

    for variant in VARIANTS:

        (
            summary,
            trades,
            events,
            equity_curve,
        ) = run_variant(
            variant,
            full_data,
            full_funds,
            args.equity,
        )

        full_rows.append(
            {
                "variant":
                    variant,
                **summary,
            }
        )

        print(
            f"{variant:<18} "
            f"Return={summary['return_pct']:+7.2f}% | "
            f"DD={summary['max_drawdown_pct']:6.2f}% | "
            f"PF={summary['profit_factor']!s:<7} | "
            f"Trades={summary['trades']:3d} | "
            f"Win={summary['win_rate_pct']:5.1f}%"
        )

        if variant == "BASELINE":
            baseline_trades = trades
            baseline_events = events
            baseline_equity = equity_curve

    # Independent 90d segments.
    segments = (
        sim.build_non_overlapping_segments(
            validation_start,
            validation_end,
            args.segment_days,
        )
    )

    for (
        seg_name,
        seg_start,
        seg_end,
    ) in segments:

        actual_days = (
            (
                seg_end
                - seg_start
            )
            .total_seconds()
            / 86400
        )

        segment_kind = (
            "FULL"
            if actual_days
            >= args.segment_days - 1
            else "PARTIAL"
        )

        seg_data, seg_funds = (
            window_data(
                full_datasets,
                funding_maps,
                seg_start,
                seg_end,
            )
        )

        if any(
            df.empty
            for df
            in seg_data.values()
        ):
            continue

        for variant in VARIANTS:

            (
                summary,
                _,
                _,
                _,
            ) = run_variant(
                variant,
                seg_data,
                seg_funds,
                args.equity,
            )

            segment_rows.append(
                {
                    "segment":
                        seg_name,

                    "segment_kind":
                        segment_kind,

                    "period_start":
                        seg_start.isoformat(),

                    "period_end":
                        seg_end.isoformat(),

                    "variant":
                        variant,

                    **summary,
                }
            )

    sim.write_csv(
        OUTPUT_DIR
        / "trend_quality_ab_full_year.csv",
        full_rows,
    )

    sim.write_csv(
        OUTPUT_DIR
        / "trend_quality_ab_segments.csv",
        segment_rows,
    )

    # Robustness on FULL segments only.
    robustness = []

    full_segment_rows = [
        x
        for x in segment_rows
        if x[
            "segment_kind"
        ] == "FULL"
    ]

    for variant in VARIANTS:

        rows = [
            x
            for x in full_segment_rows
            if x[
                "variant"
            ] == variant
        ]

        if not rows:
            continue

        returns = [
            x[
                "return_pct"
            ]
            for x in rows
        ]

        dds = [
            x[
                "max_drawdown_pct"
            ]
            for x in rows
        ]

        pfs = [
            float(
                x[
                    "profit_factor"
                ]
            )
            for x in rows
            if isinstance(
                x[
                    "profit_factor"
                ],
                (
                    int,
                    float,
                ),
            )
            and not math.isnan(
                float(
                    x[
                        "profit_factor"
                    ]
                )
            )
        ]

        robustness.append(
            {
                "variant":
                    variant,

                "full_segments":
                    len(
                        rows
                    ),

                "profitable_segments":
                    sum(
                        1
                        for x in returns
                        if x > 0
                    ),

                "losing_segments":
                    sum(
                        1
                        for x in returns
                        if x < 0
                    ),

                "avg_segment_return_pct":
                    sum(
                        returns
                    )
                    / len(
                        returns
                    ),

                "median_segment_return_pct":
                    float(
                        pd.Series(
                            returns
                        ).median()
                    ),

                "worst_segment_return_pct":
                    min(
                        returns
                    ),

                "best_segment_return_pct":
                    max(
                        returns
                    ),

                "avg_segment_max_drawdown_pct":
                    sum(
                        dds
                    )
                    / len(
                        dds
                    ),

                "worst_segment_max_drawdown_pct":
                    max(
                        dds
                    ),

                "avg_segment_profit_factor":
                    (
                        sum(
                            pfs
                        )
                        / len(
                            pfs
                        )
                        if pfs
                        else None
                    ),
            }
        )

    sim.write_csv(
        OUTPUT_DIR
        / "trend_quality_robustness.csv",
        robustness,
    )

    # Diagnostic on official baseline entries.
    baseline_quality_rows = []

    if baseline_trades is not None:

        baseline_quality_rows = (
            baseline_trade_quality(
                baseline_trades,
                full_data,
                validation_start,
                validation_end,
                args.segment_days,
            )
        )

        sim.write_csv(
            OUTPUT_DIR
            / "baseline_trade_quality.csv",
            baseline_quality_rows,
        )

        sim.write_csv(
            OUTPUT_DIR
            / "baseline_quality_summary.csv",
            quality_diagnostic_summary(
                baseline_quality_rows
            ),
        )

        sim.write_csv(
            OUTPUT_DIR
            / "baseline_events.csv",
            baseline_events,
        )

        sim.write_csv(
            OUTPUT_DIR
            / "baseline_equity.csv",
            baseline_equity,
        )

    report = {
        "framework":
            (
                "V4-SIM epsilon "
                "Trend Quality Filter Diagnostic"
            ),

        "official_strategy":
            (
                "Strategy v1.0 remains unchanged"
            ),

        "test_scope":
            (
                "Only new-entry filtering; "
                "all management rules unchanged"
            ),

        "test_config": {
            "max_units":
                7,

            "hedge_ratio":
                0.0,

            "days":
                args.days,

            "segment_days":
                args.segment_days,

            "starting_equity":
                args.equity,
        },

        "predetermined_variants": {
            "BASELINE":
                "No extra filter",

            "ER_030":
                "4H ER8 >= 0.30",

            "ADX_20":
                "4H ADX14 >= 20",

            "EMA_GAP_030":
                (
                    "|EMA20-EMA50| / "
                    "ATR14 >= 0.30"
                ),

            "PERSIST_2":
                (
                    "Same directional 4H "
                    "bias for >=2 closed bars"
                ),

            "QUALITY_2OF3":
                (
                    "At least 2 of "
                    "ER>=0.30, ADX>=20, "
                    "EMA gap/ATR>=0.30"
                ),
        },

        "outputs": {
            "full_year_ab":
                "trend_quality_ab_full_year.csv",

            "segments_ab":
                "trend_quality_ab_segments.csv",

            "robustness":
                "trend_quality_robustness.csv",

            "baseline_trade_quality":
                "baseline_trade_quality.csv",

            "baseline_quality_summary":
                "baseline_quality_summary.csv",
        },

        "warning":
            (
                "Do not optimize thresholds "
                "from this one sample. "
                "First determine whether any "
                "quality concept improves "
                "multiple full 90-day windows."
            ),
    }

    (
        OUTPUT_DIR
        / "trend_quality_report.json"
    ).write_text(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print()
    print("=" * 82)
    print("DONE")
    print(
        f"Output: {OUTPUT_DIR}"
    )
    print()
    print("Upload these first:")
    print(
        "  trend_quality_ab_full_year.csv"
    )
    print(
        "  trend_quality_ab_segments.csv"
    )
    print(
        "  trend_quality_robustness.csv"
    )
    print(
        "  baseline_quality_summary.csv"
    )
    print(
        "  baseline_trade_quality.csv"
    )
    print(
        "  trend_quality_report.json"
    )
    print("=" * 82)


if __name__ == "__main__":
    main()
