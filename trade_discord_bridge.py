#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Trading bridge selector.

TRADE_MODE=paper (default)
    -> existing V16 Champion / Challenger Forward Paper.

TRADE_MODE=live
    -> Strategy v2.1 Meta SAFE live Binance Futures bridge.

bot.py should always import:
    from trade_discord_bridge import TradeDiscordBridge
"""

from __future__ import annotations

import os


class TradeDiscordBridge:
    def __init__(self, bot, tz, fallback_channel_id=None):
        mode = os.getenv("TRADE_MODE", "paper").strip().lower()

        if mode == "live":
            from trade_v2_discord_bridge import LiveStrategyV2DiscordBridge

            self._impl = LiveStrategyV2DiscordBridge(
                bot,
                tz,
                fallback_channel_id=fallback_channel_id,
            )

        elif mode == "paper":
            from trade_discord_bridge_paper import TradeDiscordBridge as PaperBridge

            self._impl = PaperBridge(
                bot,
                tz,
                fallback_channel_id=fallback_channel_id,
            )

        else:
            raise RuntimeError(
                f"Unsupported TRADE_MODE={mode!r}. Use 'paper' or 'live'."
            )

    def start(self):
        return self._impl.start()

    async def status_text(self):
        return await self._impl.status_text()

    async def pause_new_risk(self):
        return await self._impl.pause_new_risk()

    async def resume_new_risk(self):
        return await self._impl.resume_new_risk()

    async def send_test_notification(self):
        return await self._impl.send_test_notification()

    @property
    def new_risk_enabled(self):
        return self._impl.new_risk_enabled
