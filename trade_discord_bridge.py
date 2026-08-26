#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Discord bridge for V16 Champion / Challenger Forward Paper.

NO REAL ORDERS.
NO BINANCE API KEY.

Responsibilities
----------------
- Runs v16_dual_forward_paper continuously inside the Discord bot process.
- Uses a persistent data root controlled by TRADE_DATA_DIR.
- Sends immediate Discord notifications for:
    OPEN_1_UNIT
    PYRAMID_ADD
    REDUCE_1_UNIT
    EXIT
    Challenger PYRAMID_BLOCKED
- Sends a daily 20:00 Asia/Taipei trading summary.
- Supports pausing NEW RISK while still managing existing positions.

"Pause trading" semantics
-------------------------
Pause does NOT freeze open positions.

While paused:
    - no new OPEN
    - no new ADD
    - existing Structural TP still works
    - existing 1H/4H exits still work
    - 1% Hard Risk still works
    - Funding still updates

This is deliberately safer than freezing the entire engine.
"""

from __future__ import annotations

import asyncio
import csv
import datetime
import json
import os
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import discord

import v16_dual_forward_paper as engine


IMPORTANT_ACTIONS = {
    "OPEN_1_UNIT",
    "PYRAMID_ADD",
    "REDUCE_1_UNIT",
    "EXIT",
    "PYRAMID_BLOCKED",
}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
        "disabled",
    }


def _to_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _to_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except Exception:
        return None


def _fmt_usdt(value: Any, signed: bool = False) -> str:
    x = _to_float(value)
    if x is None:
        return "—"
    if signed:
        return f"{x:+,.4f}U"
    return f"{x:,.4f}U"


def _fmt_price(value: Any) -> str:
    x = _to_float(value)
    if x is None:
        return "—"
    if x >= 10000:
        return f"${x:,.2f}"
    if x >= 100:
        return f"${x:,.2f}"
    return f"${x:,.4f}"


def _safe_iso(value: str) -> Optional[datetime.datetime]:
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )
    except Exception:
        return None


class TradeDiscordBridge:
    def __init__(
        self,
        bot,
        tz,
        fallback_channel_id: Optional[int] = None,
    ):
        self.bot = bot
        self.tz = tz

        configured_channel = os.getenv("TRADE_CHANNEL_ID")
        if configured_channel:
            try:
                self.channel_id = int(configured_channel)
            except Exception:
                self.channel_id = fallback_channel_id
        else:
            self.channel_id = fallback_channel_id

        self.poll_seconds = max(
            10,
            int(os.getenv("TRADE_POLL_SECONDS", "20")),
        )

        self.daily_hour = int(
            os.getenv("TRADE_DAILY_HOUR", "20")
        )

        self.daily_minute = int(
            os.getenv("TRADE_DAILY_MINUTE", "0")
        )

        self.data_root = Path(
            os.getenv("TRADE_DATA_DIR", "trade_runtime")
        )

        self.data_root.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.control_file = (
            self.data_root
            / "trade_bridge_state.json"
        )

        self._configure_engine_paths()

        self.control = self._load_control()

        self.champion = None
        self.challenger = None

        self._runner_task = None
        self._daily_task = None
        self._lock = asyncio.Lock()

        self._last_status = None
        self._startup_notified = False
        self._last_error_notice_monotonic = 0.0
        self._last_error_text = ""

    # ========================================================
    # ENGINE PATHS / PERSISTENCE
    # ========================================================

    def _configure_engine_paths(self):
        """
        Redirect all V16 persistent state into TRADE_DATA_DIR.

        On a cloud host, mount a persistent volume and set for example:
            TRADE_DATA_DIR=/data/trading
        """
        root = self.data_root

        engine.DUAL_DIR = (
            root
            / "forward_paper_dual"
        )

        engine.ANCHOR_FILE = (
            engine.DUAL_DIR
            / "dual_anchor.json"
        )

        engine.DUAL_STATUS_FILE = (
            engine.DUAL_DIR
            / "dual_status.json"
        )

        engine.DUAL_EQUITY_FILE = (
            engine.DUAL_DIR
            / "dual_equity.csv"
        )

        engine.CHALLENGER_DIR = (
            root
            / "forward_paper_state_score3"
        )

        engine.CHALLENGER_DECISIONS_FILE = (
            engine.CHALLENGER_DIR
            / "readd_score3_decisions.csv"
        )

        engine.CHAMPION_PATHS = (
            engine.TrackPaths(
                root
                / "forward_paper_state"
            )
        )

        engine.CHALLENGER_PATHS = (
            engine.TrackPaths(
                engine.CHALLENGER_DIR
            )
        )

    def _default_control(self) -> dict:
        return {
            "new_risk_enabled":
                _env_bool(
                    "TRADE_FORWARD_ENABLED",
                    True,
                ),

            "cursor_initialized":
                False,

            "champion_event_rows":
                0,

            "challenger_event_rows":
                0,

            "last_daily_summary_date":
                None,
        }

    def _load_control(self) -> dict:
        default = self._default_control()

        if not self.control_file.exists():
            return default

        try:
            data = json.loads(
                self.control_file.read_text(
                    encoding="utf-8"
                )
            )

            if not isinstance(data, dict):
                return default

            merged = dict(default)
            merged.update(data)
            return merged

        except Exception as exc:
            print(
                f"[trade] load control failed: {exc}",
                flush=True,
            )
            return default

    def _save_control(self):
        self.control_file.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        tmp = self.control_file.with_suffix(
            ".tmp"
        )

        tmp.write_text(
            json.dumps(
                self.control,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        tmp.replace(
            self.control_file
        )

    # ========================================================
    # START / LOOP
    # ========================================================

    def start(self):
        if self.channel_id is None:
            print(
                "[trade] TRADE_CHANNEL_ID 未設定，"
                "且沒有 fallback channel；交易橋接未啟動。",
                flush=True,
            )
            return

        if (
            self._runner_task is None
            or self._runner_task.done()
        ):
            self._runner_task = (
                asyncio.create_task(
                    self._run_loop()
                )
            )

        if (
            self._daily_task is None
            or self._daily_task.done()
        ):
            self._daily_task = (
                asyncio.create_task(
                    self._daily_summary_loop()
                )
            )

    async def _ensure_engine_loaded(self):
        if (
            self.champion is not None
            and self.challenger is not None
        ):
            return

        # File I/O only, but keep it off the event loop anyway.
        champion = await asyncio.to_thread(
            engine.load_champion
        )

        challenger, cloned = (
            await asyncio.to_thread(
                engine.load_or_clone_challenger,
                champion,
            )
        )

        self.champion = champion
        self.challenger = challenger

        # Initialize cursors BEFORE the first cloud cycle so old
        # migrated history is not replayed to Discord.
        if not self.control.get(
            "cursor_initialized",
            False,
        ):
            self.control[
                "champion_event_rows"
            ] = self._count_csv_rows(
                engine.CHAMPION_PATHS.events_file
            )

            self.control[
                "challenger_event_rows"
            ] = self._count_csv_rows(
                engine.CHALLENGER_PATHS.events_file
            )

            self.control[
                "cursor_initialized"
            ] = True

            self._save_control()

        print(
            "[trade] engine loaded | "
            f"cloned_challenger={cloned} | "
            f"data_root={self.data_root.resolve()}",
            flush=True,
        )

    async def _run_loop(self):
        await self.bot.wait_until_ready()

        try:
            await self._ensure_engine_loaded()
        except Exception as exc:
            await self._notify_engine_error(
                f"啟動失敗：{exc}"
            )
            traceback.print_exc()
            return

        while not self.bot.is_closed():
            cycle_started = time.monotonic()

            try:
                async with self._lock:

                    # If user paused NEW RISK, remove any OPEN/ADD
                    # queued before the pause command.
                    if not self.control[
                        "new_risk_enabled"
                    ]:
                        self._suppress_new_risk_pending()

                    status = await asyncio.to_thread(
                        engine.run_dual_cycle,
                        self.champion,
                        self.challenger,
                        False,
                    )

                    # The just-processed closed bar can have queued
                    # a new OPEN/ADD. Consume/cancel it while paused.
                    if not self.control[
                        "new_risk_enabled"
                    ]:
                        self._suppress_new_risk_pending()

                        await asyncio.to_thread(
                            engine.save_both,
                            self.champion,
                            self.challenger,
                        )

                    new_events, next_cursors = (
                        self._collect_new_events()
                    )

                    self._last_status = status

                # Discord network sends happen outside the state lock.
                dispatch_ok = await self._dispatch_events(
                    new_events
                )

                if dispatch_ok:
                    self.control.update(
                        next_cursors
                    )
                    self._save_control()

                if not self._startup_notified:
                    await self._send_startup_notice()
                    self._startup_notified = True

            except Exception as exc:
                print(
                    f"[trade] cycle error: {exc}",
                    flush=True,
                )
                traceback.print_exc()

                await self._notify_engine_error(
                    str(exc)
                )

            elapsed = (
                time.monotonic()
                - cycle_started
            )

            await asyncio.sleep(
                max(
                    5,
                    self.poll_seconds
                    - elapsed,
                )
            )

    # ========================================================
    # SAFE PAUSE / RESUME
    # ========================================================

    def _suppress_state_pending_new_risk(
        self,
        state,
    ):
        for symbol in engine.base.SYMBOLS:
            pending = state.pending.get(
                symbol
            )

            if pending is None:
                continue

            if pending.kind not in {
                "OPEN",
                "ADD",
            }:
                continue

            # ADD signal must be consumed so it cannot be resurrected
            # after the operator resumes trading.
            if (
                pending.kind == "ADD"
                and pending.swing_time is not None
            ):
                trade = state.active.get(
                    symbol
                )

                if trade is not None:
                    if (
                        trade.last_add_swing_time is None
                        or pending.swing_time
                        > trade.last_add_swing_time
                    ):
                        trade.last_add_swing_time = (
                            pending.swing_time
                        )

            state.pending[
                symbol
            ] = None

    def _suppress_new_risk_pending(self):
        if self.champion is None:
            return

        self._suppress_state_pending_new_risk(
            self.champion
        )

        self._suppress_state_pending_new_risk(
            self.challenger
        )

    async def pause_new_risk(self) -> str:
        await self._ensure_engine_loaded()

        async with self._lock:
            self.control[
                "new_risk_enabled"
            ] = False

            self._suppress_new_risk_pending()

            await asyncio.to_thread(
                engine.save_both,
                self.champion,
                self.challenger,
            )

            self._save_control()

        return (
            "🛑 **Forward Paper 已暫停新增風險**\n"
            "不再開新倉、不再加倉；"
            "現有持倉仍會繼續執行 Structural TP、"
            "1H/4H Exit、1% Hard Risk 與 Funding。"
        )

    async def resume_new_risk(self) -> str:
        await self._ensure_engine_loaded()

        async with self._lock:
            self.control[
                "new_risk_enabled"
            ] = True
            self._save_control()

        return (
            "▶️ **Forward Paper 已恢復新增風險**\n"
            "Champion 與 Challenger 會從下一個新訊號開始"
            "正常開倉／加倉。"
        )

    @property
    def new_risk_enabled(self) -> bool:
        return bool(
            self.control.get(
                "new_risk_enabled",
                True,
            )
        )

    # ========================================================
    # EVENT CURSOR
    # ========================================================

    @staticmethod
    def _read_csv(path: Path) -> List[dict]:
        if not path.exists():
            return []

        try:
            with path.open(
                "r",
                encoding="utf-8-sig",
                newline="",
            ) as f:
                return list(
                    csv.DictReader(f)
                )
        except Exception as exc:
            print(
                f"[trade] csv read failed {path}: {exc}",
                flush=True,
            )
            return []

    @classmethod
    def _count_csv_rows(
        cls,
        path: Path,
    ) -> int:
        return len(
            cls._read_csv(path)
        )

    def _new_rows_for_track(
        self,
        track: str,
    ) -> Tuple[
        List[dict],
        int,
    ]:
        if track == "CHAMPION":
            path = (
                engine
                .CHAMPION_PATHS
                .events_file
            )

            cursor_key = (
                "champion_event_rows"
            )

        else:
            path = (
                engine
                .CHALLENGER_PATHS
                .events_file
            )

            cursor_key = (
                "challenger_event_rows"
            )

        rows = self._read_csv(
            path
        )

        cursor = int(
            self.control.get(
                cursor_key,
                0,
            )
        )

        # If a file was manually truncated/reset, do not replay
        # whatever remains as "new".
        if cursor > len(rows):
            return (
                [],
                len(rows),
            )

        return (
            rows[
                cursor:
            ],
            len(rows),
        )

    def _collect_new_events(self):
        champion_rows, champion_next = (
            self._new_rows_for_track(
                "CHAMPION"
            )
        )

        challenger_rows, challenger_next = (
            self._new_rows_for_track(
                "CHALLENGER"
            )
        )

        items = []

        for row in champion_rows:
            if row.get(
                "action"
            ) in IMPORTANT_ACTIONS:
                items.append(
                    {
                        "track":
                            "CHAMPION",
                        "row":
                            row,
                    }
                )

        for row in challenger_rows:
            if row.get(
                "action"
            ) in IMPORTANT_ACTIONS:
                items.append(
                    {
                        "track":
                            "CHALLENGER",
                        "row":
                            row,
                    }
                )

        items.sort(
            key=lambda x: (
                _to_int(
                    x[
                        "row"
                    ].get(
                        "time_ms"
                    )
                )
                or 0,
                0
                if x[
                    "track"
                ]
                == "CHAMPION"
                else 1,
            )
        )

        return (
            items,
            {
                "champion_event_rows":
                    champion_next,

                "challenger_event_rows":
                    challenger_next,
            },
        )

    # ========================================================
    # EVENT DEDUP / FORMATTING
    # ========================================================

    @staticmethod
    def _round_key(value: Any):
        x = _to_float(value)
        if x is None:
            return None
        return round(
            x,
            6,
        )

    def _event_match_key(
        self,
        row: dict,
    ):
        action = row.get(
            "action"
        )

        # Challenger block is intentionally unique.
        if action == "PYRAMID_BLOCKED":
            return (
                "PYRAMID_BLOCKED",
                row.get(
                    "trade_id"
                ),
                row.get(
                    "time_ms"
                ),
            )

        return (
            action,
            row.get(
                "symbol"
            ),
            row.get(
                "direction"
            ),
            row.get(
                "time_ms"
            ),
            self._round_key(
                row.get(
                    "price"
                )
            ),
            row.get(
                "core_units_after"
            ),
            self._round_key(
                row.get(
                    "avg_entry_after"
                )
            ),
            row.get(
                "reason"
            ),
        )

    def _pair_identical_track_events(
        self,
        items: List[dict],
    ):
        used = set()
        groups = []

        for i, item in enumerate(
            items
        ):
            if i in used:
                continue

            row = item[
                "row"
            ]

            track = item[
                "track"
            ]

            key = self._event_match_key(
                row
            )

            tracks = [
                track
            ]

            members = [
                item
            ]

            if row.get(
                "action"
            ) != "PYRAMID_BLOCKED":

                for j in range(
                    i + 1,
                    len(items),
                ):
                    if j in used:
                        continue

                    other = items[
                        j
                    ]

                    if other[
                        "track"
                    ] == track:
                        continue

                    if (
                        self._event_match_key(
                            other[
                                "row"
                            ]
                        )
                        == key
                    ):
                        used.add(
                            j
                        )

                        tracks.append(
                            other[
                                "track"
                            ]
                        )

                        members.append(
                            other
                        )

                        break

            used.add(
                i
            )

            groups.append(
                {
                    "tracks":
                        tracks,

                    "members":
                        members,

                    "row":
                        row,
                }
            )

        return groups

    @staticmethod
    def _track_label(
        tracks: Iterable[str],
    ) -> str:
        s = set(
            tracks
        )

        if s == {
            "CHAMPION",
            "CHALLENGER",
        }:
            return (
                "🏆 Champion + 🧪 Challenger"
            )

        if "CHAMPION" in s:
            return "🏆 Champion"

        return "🧪 Challenger"

    def _track_paths(
        self,
        track: str,
    ):
        if track == "CHAMPION":
            return engine.CHAMPION_PATHS

        return engine.CHALLENGER_PATHS

    def _find_trade_record(
        self,
        track: str,
        trade_id: str,
    ) -> Optional[dict]:
        if not trade_id:
            return None

        rows = self._read_csv(
            self._track_paths(
                track
            ).trades_file
        )

        for row in reversed(
            rows
        ):
            if row.get(
                "trade_id"
            ) == trade_id:
                return row

        return None

    def _find_initial_entry_price(
        self,
        track: str,
        trade_id: str,
    ) -> Optional[float]:
        if not trade_id:
            return None

        rows = self._read_csv(
            self._track_paths(
                track
            ).events_file
        )

        for row in rows:
            if (
                row.get(
                    "trade_id"
                )
                == trade_id
                and row.get(
                    "action"
                )
                == "OPEN_1_UNIT"
            ):
                return _to_float(
                    row.get(
                        "price"
                    )
                )

        return None

    def _format_event_group(
        self,
        group: dict,
    ) -> str:
        row = group[
            "row"
        ]

        tracks = group[
            "tracks"
        ]

        label = self._track_label(
            tracks
        )

        action = row.get(
            "action"
        )

        symbol = (
            row.get(
                "symbol"
            )
            or "?"
        )

        coin = symbol.replace(
            "USDT",
            "",
        )

        direction = (
            row.get(
                "direction"
            )
            or "?"
        )

        direction_zh = (
            "做多"
            if direction
            == "LONG"
            else (
                "做空"
                if direction
                == "SHORT"
                else direction
            )
        )

        units_after = (
            _to_int(
                row.get(
                    "core_units_after"
                )
            )
            or 0
        )

        total_notional = (
            units_after
            * float(
                engine.base.UNIT_NOTIONAL
            )
        )

        if action == "OPEN_1_UNIT":
            return (
                f"📥 **PAPER 開倉｜{label}**\n"
                f"幣種：**{coin}**｜方向：**{direction_zh}**\n"
                f"開倉價：**{_fmt_price(row.get('price'))}**\n"
                f"本次名義金額：**{engine.base.UNIT_NOTIONAL:.0f}U** "
                f"（10x 約 {engine.base.UNIT_NOTIONAL / engine.base.LEVERAGE:.1f}U 保證金）\n"
                f"目前倉位：**{units_after} Unit / {total_notional:.0f}U**\n"
                f"平均開倉價：**{_fmt_price(row.get('avg_entry_after'))}**\n"
                f"Trade ID：`{row.get('trade_id')}`"
            )

        if action == "PYRAMID_ADD":
            return (
                f"➕ **PAPER 加倉｜{label}**\n"
                f"幣種：**{coin}**｜方向：**{direction_zh}**\n"
                f"加倉：**+{engine.base.UNIT_NOTIONAL:.0f}U**\n"
                f"加倉價：**{_fmt_price(row.get('price'))}**\n"
                f"加倉後平均價：**{_fmt_price(row.get('avg_entry_after'))}**\n"
                f"加倉後總倉位：**{units_after} Unit / {total_notional:.0f}U**\n"
                f"Trade ID：`{row.get('trade_id')}`"
            )

        if action == "REDUCE_1_UNIT":
            pnl = _to_float(
                row.get(
                    "realized_pnl"
                )
            )

            pnl_emoji = (
                "🟢"
                if (
                    pnl is not None
                    and pnl >= 0
                )
                else "🔴"
            )

            return (
                f"➖ **PAPER 減倉｜{label}**\n"
                f"幣種：**{coin}**｜方向：**{direction_zh}**\n"
                f"減倉：**-1 Unit（原始名義 {engine.base.UNIT_NOTIONAL:.0f}U）**\n"
                f"減倉價：**{_fmt_price(row.get('price'))}**\n"
                f"{pnl_emoji} 本次 Unit 實現 PnL：**{_fmt_usdt(pnl, signed=True)}**\n"
                f"剩餘倉位：**{units_after} Unit / {total_notional:.0f}U**\n"
                f"剩餘平均價：**{_fmt_price(row.get('avg_entry_after'))}**\n"
                f"原因：`{row.get('reason') or 'STRUCTURAL_TP'}`"
            )

        if action == "EXIT":
            representative_track = (
                "CHAMPION"
                if "CHAMPION" in tracks
                else "CHALLENGER"
            )

            trade_id = row.get(
                "trade_id"
            )

            record = self._find_trade_record(
                representative_track,
                trade_id,
            )

            initial_entry = (
                self._find_initial_entry_price(
                    representative_track,
                    trade_id,
                )
            )

            net = _to_float(
                row.get(
                    "net_trade_pnl"
                )
            )

            if (
                net is None
                and record is not None
            ):
                net = _to_float(
                    record.get(
                        "net_pnl"
                    )
                )

            pnl_emoji = (
                "🟢"
                if (
                    net is not None
                    and net >= 0
                )
                else "🔴"
            )

            max_units = (
                _to_int(
                    record.get(
                        "max_units"
                    )
                )
                if record
                else None
            )

            max_notional = (
                max_units
                * float(
                    engine.base.UNIT_NOTIONAL
                )
                if max_units
                is not None
                else None
            )

            fees = (
                _to_float(
                    record.get(
                        "fees"
                    )
                )
                if record
                else None
            )

            funding = (
                _to_float(
                    record.get(
                        "funding"
                    )
                )
                if record
                else None
            )

            reason = (
                record.get(
                    "exit_reason"
                )
                if record
                else row.get(
                    "reason"
                )
            )

            return (
                f"🏁 **PAPER 平倉｜{label}**\n"
                f"幣種：**{coin}**｜方向：**{direction_zh}**\n"
                f"{pnl_emoji} 最終 PnL：**{_fmt_usdt(net, signed=True)}**\n"
                f"初始開倉價：**{_fmt_price(initial_entry)}**\n"
                f"最終平倉價：**{_fmt_price(row.get('price'))}**\n"
                f"最大同時倉位：**"
                f"{max_units if max_units is not None else '—'} Unit"
                f"{f' / {max_notional:.0f}U' if max_notional is not None else ''}**\n"
                f"手續費：**{_fmt_usdt(fees)}**｜"
                f"Funding：**{_fmt_usdt(funding, signed=True)}**\n"
                f"原因：`{reason or 'EXIT'}`\n"
                f"Trade ID：`{trade_id}`"
            )

        if action == "PYRAMID_BLOCKED":
            reason = (
                row.get(
                    "reason"
                )
                or ""
            )

            return (
                f"🧪⚖️ **策略分岔：Challenger 拒絕 Re-add**\n"
                f"幣種：**{coin}**｜方向：**{direction_zh}**\n"
                f"當下價格：**{_fmt_price(row.get('price'))}**\n"
                f"目前倉位：**{units_after} Unit / {total_notional:.0f}U**\n"
                f"判斷：`{reason}`\n"
                f"這個 HL/LH 已消耗；Challenger 必須等下一個新 swing 才能再申請補倉。\n"
                f"Trade ID：`{row.get('trade_id')}`"
            )

        return (
            f"ℹ️ {label} {action} "
            f"{symbol} {direction}"
        )

    async def _dispatch_events(
        self,
        items: List[dict],
    ) -> bool:
        if not items:
            return True

        channel = await self._get_channel()

        if channel is None:
            return False

        groups = (
            self._pair_identical_track_events(
                items
            )
        )

        try:
            for group in groups:
                msg = self._format_event_group(
                    group
                )

                await channel.send(
                    msg
                )

            return True

        except Exception as exc:
            print(
                f"[trade] Discord event send failed: {exc}",
                flush=True,
            )
            return False

    # ========================================================
    # CHANNEL / ERROR / STARTUP
    # ========================================================

    async def _get_channel(self):
        if self.channel_id is None:
            return None

        channel = self.bot.get_channel(
            self.channel_id
        )

        if channel is None:
            try:
                channel = await self.bot.fetch_channel(
                    self.channel_id
                )
            except Exception as exc:
                print(
                    f"[trade] fetch channel failed: {exc}",
                    flush=True,
                )
                return None

        if not hasattr(
            channel,
            "send",
        ):
            print(
                "[trade] TRADE_CHANNEL_ID 不可傳送訊息",
                flush=True,
            )
            return None

        return channel

    async def _send_startup_notice(self):
        channel = await self._get_channel()

        if channel is None:
            return

        mode = (
            "🟢 新增風險已啟用"
            if self.new_risk_enabled
            else "🟠 新增風險目前暫停"
        )

        await channel.send(
            "☁️ **Champion / Challenger Forward Paper 已在雲端啟動**\n"
            f"🏆 Champion：Strategy v1.1\n"
            f"🧪 Challenger：v1.1 + Re-add Continuation Score ≥ 3/4\n"
            f"{mode}\n"
            f"輪詢：每 {self.poll_seconds} 秒｜"
            f"每日交易摘要：{self.daily_hour:02d}:{self.daily_minute:02d}（台灣時間）\n"
            "⚠️ 目前仍是 **PAPER ONLY**，不會送出 Binance 真實訂單。"
        )

    async def _notify_engine_error(
        self,
        text: str,
    ):
        now = time.monotonic()

        # Avoid spamming Discord every 20 seconds.
        same_error = (
            text
            == self._last_error_text
        )

        if (
            same_error
            and now
            - self._last_error_notice_monotonic
            < 1800
        ):
            return

        self._last_error_text = text
        self._last_error_notice_monotonic = now

        channel = await self._get_channel()

        if channel is None:
            return

        try:
            await channel.send(
                "⚠️ **Forward Paper 引擎錯誤**\n"
                f"`{text[:1500]}`\n"
                "系統會持續重試；若錯誤持續，請檢查雲端 Log。"
            )
        except Exception:
            pass

    async def send_test_notification(self) -> str:
        channel = await self._get_channel()

        if channel is None:
            return (
                "❌ 找不到交易通報頻道，請檢查 TRADE_CHANNEL_ID。"
            )

        await channel.send(
            "🧪 **交易通報測試成功**\n"
            "雲端 Discord Bridge 可以正常在此頻道發送訊息。"
        )

        return (
            "✅ 已送出測試訊息到交易通報頻道。"
        )

    # ========================================================
    # STATUS
    # ========================================================

    def _load_dual_status_file(self) -> Optional[dict]:
        path = (
            engine.DUAL_STATUS_FILE
        )

        if not path.exists():
            return None

        try:
            return json.loads(
                path.read_text(
                    encoding="utf-8"
                )
            )
        except Exception:
            return None

    @staticmethod
    def _position_text(
        item: Optional[dict],
    ) -> str:
        if item is None:
            return "FLAT"

        return (
            f"{item.get('direction')} "
            f"{item.get('units')}U "
            f"PnL={_fmt_usdt(item.get('trade_pnl'), signed=True)}"
        )

    async def status_text(self) -> str:
        status = (
            self._last_status
            or self._load_dual_status_file()
        )

        mode = (
            "🟢 新增風險：啟用"
            if self.new_risk_enabled
            else "🟠 新增風險：暫停"
        )

        if status is None:
            return (
                "📡 **Forward Paper 狀態**\n"
                f"{mode}\n"
                "尚未產生第一份 dual_status；請稍候下一個輪詢週期。"
            )

        champion = status.get(
            "champion",
            {},
        )

        challenger = status.get(
            "challenger",
            {},
        )

        comparison = status.get(
            "comparison",
            {},
        )

        lines = [
            "📡 **Forward Paper 狀態**",
            mode,
            "",
            (
                "🏆 Champion v1.1："
                f"Equity **{_fmt_usdt(champion.get('equity'))}**｜"
                f"Anchor 後 **{_to_float(champion.get('return_since_anchor_pct')) or 0:+.4f}%**"
            ),
            (
                "🧪 Challenger Score3："
                f"Equity **{_fmt_usdt(challenger.get('equity'))}**｜"
                f"Anchor 後 **{_to_float(challenger.get('return_since_anchor_pct')) or 0:+.4f}%**"
            ),
            (
                "⚖️ Edge Q-C："
                f"**{_fmt_usdt(comparison.get('challenger_minus_champion_usdt'), signed=True)}**"
            ),
            "",
        ]

        for symbol in engine.base.SYMBOLS:
            coin = symbol.replace(
                "USDT",
                "",
            )

            lines.append(
                f"**{coin}**｜"
                f"Champion {self._position_text(champion.get('active', {}).get(symbol))}｜"
                f"Challenger {self._position_text(challenger.get('active', {}).get(symbol))}"
            )

        return "\n".join(
            lines
        )

    # ========================================================
    # DAILY 20:00 SUMMARY
    # ========================================================

    def _closed_pnl_for_date(
        self,
        path: Path,
        target_date: datetime.date,
    ) -> Tuple[
        float,
        int,
    ]:
        total = 0.0
        count = 0

        for row in self._read_csv(
            path
        ):
            closed = _safe_iso(
                row.get(
                    "closed_at",
                    "",
                )
            )

            if closed is None:
                continue

            if closed.tzinfo is None:
                closed = closed.replace(
                    tzinfo=datetime.timezone.utc
                )

            local = closed.astimezone(
                self.tz
            )

            if local.date() != target_date:
                continue

            pnl = _to_float(
                row.get(
                    "net_pnl"
                )
            )

            if pnl is not None:
                total += pnl
                count += 1

        return (
            total,
            count,
        )

    def _daily_equity_delta(
        self,
        target_date: datetime.date,
        column: str,
    ) -> Optional[float]:
        rows = self._read_csv(
            engine.DUAL_EQUITY_FILE
        )

        day_rows = []

        for row in rows:
            dt = _safe_iso(
                row.get(
                    "time",
                    "",
                )
            )

            if dt is None:
                continue

            if dt.tzinfo is None:
                dt = dt.replace(
                    tzinfo=datetime.timezone.utc
                )

            local = dt.astimezone(
                self.tz
            )

            if local.date() == target_date:
                value = _to_float(
                    row.get(
                        column
                    )
                )

                if value is not None:
                    day_rows.append(
                        value
                    )

        if len(
            day_rows
        ) < 2:
            return 0.0 if day_rows else None

        return (
            day_rows[
                -1
            ]
            - day_rows[
                0
            ]
        )

    async def _send_daily_summary(
        self,
        target_date: datetime.date,
    ):
        channel = await self._get_channel()

        if channel is None:
            return False

        status = (
            self._last_status
            or self._load_dual_status_file()
        )

        if status is None:
            return False

        champion_pnl, champion_count = (
            self._closed_pnl_for_date(
                engine
                .CHAMPION_PATHS
                .trades_file,
                target_date,
            )
        )

        challenger_pnl, challenger_count = (
            self._closed_pnl_for_date(
                engine
                .CHALLENGER_PATHS
                .trades_file,
                target_date,
            )
        )

        champion_delta = (
            self._daily_equity_delta(
                target_date,
                "champion_equity",
            )
        )

        challenger_delta = (
            self._daily_equity_delta(
                target_date,
                "challenger_equity",
            )
        )

        champion = status.get(
            "champion",
            {},
        )

        challenger = status.get(
            "challenger",
            {},
        )

        comparison = status.get(
            "comparison",
            {},
        )

        mode = (
            "🟢 Enabled"
            if self.new_risk_enabled
            else "🟠 Paused"
        )

        msg = (
            f"📊 **Forward Paper 每日交易摘要｜{target_date:%Y/%m/%d} 20:00**\n"
            f"交易模式：**{mode}**\n\n"
            f"🏆 **Champion v1.1**\n"
            f"今日 Equity 變化：**{_fmt_usdt(champion_delta, signed=True)}**\n"
            f"今日已平倉：**{_fmt_usdt(champion_pnl, signed=True)}**（{champion_count} 筆）\n"
            f"目前 Equity：**{_fmt_usdt(champion.get('equity'))}**\n\n"
            f"🧪 **Challenger Score3**\n"
            f"今日 Equity 變化：**{_fmt_usdt(challenger_delta, signed=True)}**\n"
            f"今日已平倉：**{_fmt_usdt(challenger_pnl, signed=True)}**（{challenger_count} 筆）\n"
            f"目前 Equity：**{_fmt_usdt(challenger.get('equity'))}**\n\n"
            f"⚖️ Challenger - Champion："
            f"**{_fmt_usdt(comparison.get('challenger_minus_champion_usdt'), signed=True)}**"
        )

        await channel.send(
            msg
        )

        return True

    async def _daily_summary_loop(self):
        await self.bot.wait_until_ready()

        while not self.bot.is_closed():
            now = datetime.datetime.now(
                self.tz
            )

            today = now.date()

            last_raw = self.control.get(
                "last_daily_summary_date"
            )

            target_today = now.replace(
                hour=self.daily_hour,
                minute=self.daily_minute,
                second=0,
                microsecond=0,
            )

            # If bot starts/restarts after 20:00 and today's report
            # was not sent, send it shortly after startup.
            if (
                now >= target_today
                and last_raw
                != today.isoformat()
            ):
                try:
                    ok = await self._send_daily_summary(
                        today
                    )

                    if ok:
                        self.control[
                            "last_daily_summary_date"
                        ] = today.isoformat()

                        self._save_control()

                except Exception as exc:
                    print(
                        f"[trade] daily summary failed: {exc}",
                        flush=True,
                    )

                await asyncio.sleep(
                    60
                )
                continue

            if now < target_today:
                next_target = (
                    target_today
                )
            else:
                next_target = (
                    target_today
                    + datetime.timedelta(
                        days=1
                    )
                )

            wait_seconds = max(
                5,
                (
                    next_target
                    - now
                ).total_seconds(),
            )

            await asyncio.sleep(
                wait_seconds
            )
