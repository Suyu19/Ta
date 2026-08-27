#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Discord bridge for Strategy v2.1 LIVE trading.

This module is selected only by trade_discord_bridge.py when TRADE_MODE=live.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import time
import traceback
from pathlib import Path
from typing import Optional

from strategy_v2_live import StrategyV2LiveEngine


def _env_bool(name: str, default: bool) -> bool:
    raw=os.getenv(name)
    if raw is None:return default
    return raw.strip().lower() not in {"0","false","no","off","disabled"}


def _fmt_u(x, signed=False):
    try:x=float(x)
    except Exception:return "—"
    return f"{x:+,.2f}U" if signed else f"{x:,.2f}U"


def _fmt_p(x):
    try:x=float(x)
    except Exception:return "—"
    return f"${x:,.2f}"


def _mark(ok):
    return "✅" if bool(ok) else "❌"


def _fmt_num(x, digits=2):
    try:
        return f"{float(x):.{digits}f}"
    except Exception:
        return "—"


class LiveStrategyV2DiscordBridge:
    def __init__(self, bot, tz, fallback_channel_id: Optional[int]=None):
        self.bot=bot
        self.tz=tz
        raw=os.getenv("TRADE_CHANNEL_ID")
        try:self.channel_id=int(raw) if raw else fallback_channel_id
        except Exception:self.channel_id=fallback_channel_id

        self.poll_seconds=max(10,int(os.getenv("TRADE_POLL_SECONDS","20")))
        self.daily_hour=int(os.getenv("TRADE_DAILY_HOUR","20"))
        self.daily_minute=int(os.getenv("TRADE_DAILY_MINUTE","0"))

        self.data_root=Path(os.getenv("TRADE_DATA_DIR","trade_runtime"))
        self.data_root.mkdir(parents=True,exist_ok=True)
        self.control_file=self.data_root/"strategy_v2_bridge_control.json"
        self.control=self._load_control()

        self.engine=None
        self._runner_task=None
        self._daily_task=None
        self._lock=asyncio.Lock()
        self._last_status=None
        self._startup_notified=False
        self._last_error_text=""
        self._last_error_notice_mono=0.0

    def _default_control(self):
        return {
            "new_risk_enabled":_env_bool("TRADE_FORWARD_ENABLED",True),
            "last_daily_summary_date":None,
        }

    def _load_control(self):
        d=self._default_control()
        if self.control_file.exists():
            try:
                x=json.loads(self.control_file.read_text(encoding="utf-8"))
                if isinstance(x,dict):d.update(x)
            except Exception:
                pass
        return d

    def _save_control(self):
        tmp=self.control_file.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self.control,ensure_ascii=False,indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.control_file)

    @property
    def new_risk_enabled(self):
        return bool(self.control.get("new_risk_enabled",True))

    def start(self):
        if self.channel_id is None:
            print("[trade-live] TRADE_CHANNEL_ID unavailable; bridge not started.",flush=True)
            return
        if self._runner_task is None or self._runner_task.done():
            self._runner_task=asyncio.create_task(self._run_loop())
        if self._daily_task is None or self._daily_task.done():
            self._daily_task=asyncio.create_task(self._daily_loop())

    async def _get_channel(self):
        if self.channel_id is None:return None
        ch=self.bot.get_channel(self.channel_id)
        if ch is None:
            try:ch=await self.bot.fetch_channel(self.channel_id)
            except Exception as exc:
                print(f"[trade-live] channel fetch failed: {exc}",flush=True)
                return None
        return ch if hasattr(ch,"send") else None

    async def _ensure_engine(self):
        if self.engine is not None:return
        confirm=os.getenv("LIVE_TRADING_CONFIRM","").strip()
        if confirm!="YES_I_WANT_REAL_ORDERS":
            raise RuntimeError(
                "TRADE_MODE=live is set, but LIVE_TRADING_CONFIRM is not "
                "exactly YES_I_WANT_REAL_ORDERS. No live engine was started."
            )
        self.engine=await asyncio.to_thread(
            StrategyV2LiveEngine,
            self.data_root/"strategy_v2_live",
        )
        print("[trade-live] Strategy v2.1 live engine loaded.",flush=True)

    async def _run_loop(self):
        await self.bot.wait_until_ready()
        try:
            await self._ensure_engine()
        except Exception as exc:
            traceback.print_exc()
            await self._notify_error(f"LIVE startup failed: {exc}",force=True)
            return

        while not self.bot.is_closed():
            started=time.monotonic()
            try:
                async with self._lock:
                    if not self.new_risk_enabled:
                        await asyncio.to_thread(self.engine.suppress_new_risk_pending)
                    status=await asyncio.to_thread(
                        self.engine.cycle,
                        new_risk_enabled=self.new_risk_enabled,
                    )
                    self._last_status=status
                await self._dispatch_events(status.get("events",[]))
                if not self._startup_notified:
                    await self._send_startup()
                    self._startup_notified=True
            except Exception as exc:
                print(f"[trade-live] cycle error: {exc}",flush=True)
                traceback.print_exc()
                await self._notify_error(str(exc))
            elapsed=time.monotonic()-started
            await asyncio.sleep(max(5,self.poll_seconds-elapsed))

    async def _dispatch_events(self,events):
        if not events:return
        ch=await self._get_channel()
        if ch is None:return
        for e in events:
            await ch.send(self._format_event(e))

    def _format_event(self,e):
        typ=e.get("type","EVENT")
        symbol=e.get("symbol","BTCUSDT")
        coin=symbol.replace("USDT","")
        direction=e.get("direction","")
        order=e.get("order_id","—")
        cid=e.get("client_order_id","—")

        if typ=="TREND_OPEN":
            return (
                f"[🟢](https://discord.com/assets/2d6d478121939bde.svg) **開倉通知｜**"
                f"{coin}｜{direction}｜Regime **{e.get('meta_state')}** "
                f"(Score {e.get('meta_score')}/6) "
                f"成交價：**{_fmt_p(e.get('price'))}**｜Qty **{e.get('qty')}** "
                f"名義：**{_fmt_u(e.get('notional'))}**｜Risk Budget **{float(e.get('risk_mult',0))*100:.0f}%** "
                f"Trade ID：`{e.get('trade_id')}` "
                f"Binance Order：`{order}`｜Client：`{cid}`"
            )
        if typ=="TREND_ADD":
            return (
                f"➕ **LIVE｜Trend Pyramid Add**\n"
                f"{coin}｜{direction}｜Units **{e.get('units')}**\n"
                f"成交：**{_fmt_p(e.get('price'))}**｜Qty **{e.get('qty')}**\n"
                f"Trade ID：`{e.get('trade_id')}`｜Order：`{order}`"
            )
        if typ=="TREND_REDUCE":
            return (
                f"➖ **LIVE｜Trend Structural TP**\n"
                f"{coin}｜{direction}｜成交 **{_fmt_p(e.get('price'))}**\n"
                f"Qty **{e.get('qty')}**｜原因 `{e.get('reason')}`\n"
                f"Trade ID：`{e.get('trade_id')}`｜Order：`{order}`"
            )
        if typ=="TREND_EXIT":
            pnl=e.get("realized_strategy_pnl")
            return (
                f"🏁 **LIVE｜Trend 平倉**\n"
                f"{coin}｜{direction}｜成交 **{_fmt_p(e.get('price'))}**\n"
                f"策略內實現盈虧：約 **{_fmt_u(pnl,True)}**\n"
                f"原因：`{e.get('reason')}`\n"
                f"Trade ID：`{e.get('trade_id')}`｜Order：`{order}`"
            )
        if typ=="TREND_READD_BLOCKED":
            return (
                f"🧱 **LIVE｜Trend Re-add 被 Meta/Continuation 擋下**\n"
                f"{coin}｜Trade `{e.get('trade_id')}`｜"
                f"Layer {e.get('target_layer')}｜Score **{e.get('score')}/4**"
            )
        if typ=="FLEX_OPEN":
            return (
                f"[🟢](https://discord.com/assets/2d6d478121939bde.svg) **開倉通知｜**"
                f"BTC｜LONG｜Regime **{e.get('meta_state')}** "
                f"(Score {e.get('meta_score')}/6) "
                f"成交價：**{_fmt_p(e.get('price'))}**｜Qty **{e.get('qty')}** "
                f"保證金基準：**{_fmt_u(e.get('margin'))}**｜Risk Budget **{float(e.get('risk_mult',0))*100:.0f}%** "
                f"Cycle ID：`{e.get('cycle_id')}`｜Binance Order：`{order}`｜Client：`{cid}`"
            )
        if typ=="FLEX_ADD":
            return (
                f"➕ **LIVE｜FLEX 加倉**\n"
                f"Entry #{e.get('entries')}｜成交 **{_fmt_p(e.get('price'))}**｜Qty **{e.get('qty')}**\n"
                f"Cycle：`{e.get('cycle_id')}`｜Order：`{order}`"
            )
        if typ=="FLEX_ADD_BLOCKED":
            return (
                f"🛡️ **LIVE｜FLEX 加倉被風控擋下**\n"
                f"Cycle：`{e.get('cycle_id')}`｜Entries {e.get('entries')}\n"
                f"估算 Liq/Cost：**{float(e.get('liq_ratio',0)):.3f}**｜"
                f"Gross Cap：**{'OK' if e.get('gross_ok') else 'BLOCK'}**"
            )
        if typ=="FLEX_HEDGE":
            return (
                f"🛡️ **LIVE｜FLEX Hedge 調整**\n"
                f"SHORT Target **{float(e.get('target_ratio',0))*100:.0f}%**｜"
                f"成交 **{_fmt_p(e.get('price'))}**｜Qty **{e.get('qty')}**\n"
                f"Cycle：`{e.get('cycle_id')}`｜Order：`{order}`"
            )
        if typ in ("FLEX_REDUCE","FLEX_EXIT"):
            return (
                f"{'➖' if typ=='FLEX_REDUCE' else '🏁'} **LIVE｜FLEX "
                f"{'部分止盈' if typ=='FLEX_REDUCE' else '平倉'}**\n"
                f"成交 **{_fmt_p(e.get('price'))}**｜Qty **{e.get('qty')}**\n"
                f"原因 `{e.get('reason')}`｜Cycle `{e.get('cycle_id')}`\n"
                f"Order：`{order}`"
            )
        return f"ℹ️ **LIVE Strategy v2.1**\n```json\n{json.dumps(e,ensure_ascii=False)[:1700]}\n```"

    async def _send_startup(self):
        ch=await self._get_channel()
        if ch is None:return
        status=self._last_status or {}
        a=status.get("account",{})
        await ch.send(
            "🚨 **Strategy v2.1 LIVE 已啟動｜真實 Binance Futures 訂單**\n"
            "Meta SAFE v1.1｜Trend Score3 + FLEX-AC\n"
            "Trend Unit：Profit-Only √ Compounding｜Hard Risk 1%\n"
            f"目前 Equity：**{_fmt_u(a.get('margin_balance'))}**｜"
            f"Portfolio DD：**{float(a.get('portfolio_dd',0))*100:.2f}%**\n"
            f"新增風險：**{'ENABLED' if self.new_risk_enabled else 'PAUSED'}**\n"
            "⚠️ 這不是 Paper；OPEN / ADD / TP / EXIT / Hedge 都會送到真實合約帳戶。"
        )

    async def _notify_error(self,text,force=False):
        now=time.monotonic()
        if (
            not force
            and text==self._last_error_text
            and now-self._last_error_notice_mono<1800
        ):
            return
        self._last_error_text=text
        self._last_error_notice_mono=now
        ch=await self._get_channel()
        if ch is not None:
            try:
                await ch.send(
                    "🚨 **Strategy v2.1 LIVE 引擎錯誤**\n"
                    f"`{text[:1500]}`\n"
                    "為避免重複/錯誤下單，請優先檢查 Railway Log 與 Binance 真實持倉。"
                )
            except Exception:
                pass

    async def pause_new_risk(self):
        await self._ensure_engine()
        async with self._lock:
            self.control["new_risk_enabled"]=False
            await asyncio.to_thread(self.engine.suppress_new_risk_pending)
            self._save_control()
        return (
            "🛑 **Strategy v2.1 LIVE 已暫停新增風險**\n"
            "不再 OPEN / ADD；現有 Trend Exit / Structural TP、"
            "FLEX TP / Recovery Hedge 仍繼續管理。"
        )

    async def resume_new_risk(self):
        await self._ensure_engine()
        async with self._lock:
            await asyncio.to_thread(self.engine.reconcile_or_halt)
            self.control["new_risk_enabled"]=True
            self._save_control()
        return "▶️ **Strategy v2.1 LIVE 已恢復新增風險。**"

    async def status_text(self):
        status=self._last_status
        if status is None and self.engine is not None and self.engine.status_file.exists():
            try:status=json.loads(self.engine.status_file.read_text(encoding="utf-8"))
            except Exception:status=None
        if status is None:
            return "📡 Strategy v2.1 LIVE 尚未產生第一份 status。"

        a=status.get("account",{})
        meta=status.get("meta",{})
        lines=[
            "🚨 **Strategy v2.1 LIVE 狀態**",
            f"新增風險：**{'ENABLED' if self.new_risk_enabled else 'PAUSED'}**",
            f"Meta：**{meta.get('state')}**｜Score **{meta.get('score')}/6**",
            f"Equity：**{_fmt_u(a.get('margin_balance'))}**｜"
            f"Wallet **{_fmt_u(a.get('wallet_balance'))}**｜"
            f"DD **{float(a.get('portfolio_dd',0))*100:.2f}%**",
            f"Initial Margin：**{_fmt_u(a.get('initial_margin'))}**｜"
            f"Util **{float(a.get('initial_margin_util',0))*100:.2f}%**",
            f"Trend Lock **{float(status.get('trend_lock',0))*100:.0f}%**｜"
            f"FLEX Lock **{float(status.get('flex_lock',0))*100:.0f}%**\n",
        ]

        # Global Meta 0/6 breakdown.  This is deliberately separate from
        # each symbol's Trend Entry 0/4 readiness score.
        mc=meta.get("conditions",{})
        if mc:
            al=mc.get("d1_h4_aligned",{})
            adx=mc.get("adx_ge_25",{})
            adxt=mc.get("adx_non_declining",{})
            sep=mc.get("ema_separation_ge_075_atr",{})
            sept=mc.get("ema_separation_non_declining",{})
            lines += [
                "",
                f"**Meta Score 明細（全域 {meta.get('score', 0)}/6）**",
                f"{_mark(al.get('ok'))} 1D/4H 同方向：{al.get('direction', '—')} "
                f"(**+{int(al.get('points', 0))}/2**)",
                f"{_mark(adx.get('ok'))} 4H ADX ≥25："
                f"{_fmt_num(adx.get('value'))}",
                f"{_mark(adxt.get('ok'))} ADX 未衰退："
                f"{_fmt_num(adxt.get('value'))} vs 2根前 {_fmt_num(adxt.get('lag2'))}",
                f"{_mark(sep.get('ok'))} EMA Separation ≥0.75 ATR："
                f"{_fmt_num(sep.get('value'), 3)} ATR",
                f"{_mark(sept.get('ok'))} Separation 未縮小："
                f"{_fmt_num(sept.get('value'), 3)} vs 2根前 {_fmt_num(sept.get('lag2'), 3)}",
            ]

        diagnostics=status.get("trend_entry_diagnostics",{})
        lines += ["", "**Trend 狀態 / 新開倉條件**"]
        for s,tr in status.get("trend_active",{}).items():
            coin=s.replace("USDT","")
            d=diagnostics.get(s,{})
            if tr is None:
                lines.append(
                    f"\n**{coin} Trend：FLAT｜Entry Score "
                    f"{d.get('score','—')}/{d.get('max_score',4)}｜"
                    f"方向 {d.get('direction','—')}**"
                )
                if not d.get("available",False):
                    lines.append(f"↳ ⚠️ {d.get('reason','診斷資料不足')}")
                    continue
                c=d.get("conditions",{})
                hb=c.get("h4_bias",{})
                pb=c.get("h1_pullback",{})
                tg=c.get("m15_fresh_trigger",{})
                ax=c.get("adx20",{})
                lines.append(
                    f"↳ {_mark(hb.get('ok'))} 4H Bias：**{hb.get('value','—')}** "
                    f"(Structure {hb.get('structure','—')})"
                )
                lines.append(
                    f"↳ {_mark(pb.get('ok'))} 1H Pullback："
                    f"Close {_fmt_p(pb.get('close'))}｜EMA20 {_fmt_p(pb.get('ema20'))}｜"
                    f"EMA50 {_fmt_p(pb.get('ema50'))}"
                )
                lines.append(
                    f"↳ {_mark(tg.get('ok'))} 15m Fresh Trigger："
                    f"{tg.get('prev_structure','—')} → {tg.get('structure','—')}｜"
                    f"Close {_fmt_p(tg.get('close'))} / EMA20 {_fmt_p(tg.get('ema20'))}"
                )
                lines.append(
                    f"↳ {_mark(ax.get('ok'))} ADX20："
                    f"{_fmt_num(ax.get('value'))} ≥ {_fmt_num(ax.get('threshold'),0)}"
                )
                g=d.get("gates",{})
                lines.append(
                    f"↳ Gate：新增風險 {_mark(g.get('new_risk_enabled'))}｜"
                    f"Cooldown {_mark(g.get('cooldown_ready'))}"
                    f"({int(g.get('cooldown_bars',0))} bars)"
                    + (f"｜Pending **{g.get('pending_kind')}**" if g.get('pending_kind') else "")
                )
            else:
                lines.append(
                    f"\n目前持倉：\n     **{coin} Trend：{tr.get('direction')} {tr.get('units')} units**｜"
                    f"開倉價 {_fmt_p(tr.get('avg_entry'))}｜盈虧： {_fmt_u(tr.get('pnl'),True)}｜"
                    f"X槓桿後持倉量 {_fmt_u(tr.get('locked_unit_notional'))}\n"
                )

        fx=status.get("flex")
        if fx is None:
            lines.append("BTC FLEX：FLAT")
        else:
            lines.append(
                f"BTC FLEX：LONG entries {fx.get('entries')}｜"
                f"開倉價 {_fmt_p(fx.get('avg_entry'))}｜盈虧： {_fmt_u(fx.get('pnl'),True)}｜"
                f"Hedge {float(fx.get('short_qty',0)):.6g} BTC"
            )
        if status.get("halted"):
            lines += ["",f"🚨 HALTED：`{status.get('halt_reason')}`"]
        return "\n".join(lines)

    async def send_test_notification(self):
        ch=await self._get_channel()
        if ch is None:return "❌ 找不到交易頻道。"
        await ch.send(
            "🧪 **Strategy v2.1 LIVE Discord 通報測試成功**\n"
            "這則測試不會送出 Binance 訂單。"
        )
        return "✅ 已送出 LIVE bridge 測試訊息。"

    async def _daily_loop(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            now=datetime.datetime.now(self.tz)
            target=now.replace(
                hour=self.daily_hour,minute=self.daily_minute,
                second=0,microsecond=0,
            )
            if now>=target:target+=datetime.timedelta(days=1)
            await asyncio.sleep(max(5,(target-now).total_seconds()))
            today=datetime.datetime.now(self.tz).date()
            if self.control.get("last_daily_summary_date")==today.isoformat():
                continue
            ch=await self._get_channel()
            if ch is None:continue
            try:
                msg=await self.status_text()
                await ch.send(
                    f"📊 **Strategy v2.1 LIVE 每日摘要｜{today:%Y/%m/%d}**\n\n"
                    +msg
                )
                self.control["last_daily_summary_date"]=today.isoformat()
                self._save_control()
            except Exception as exc:
                print(f"[trade-live] daily summary failed: {exc}",flush=True)
