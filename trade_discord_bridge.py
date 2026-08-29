#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Strategy v2.2 LIVE bridge compatibility wrapper.

bot.py imports:
    from trade_discord_bridge import TradeDiscordBridge

The real LIVE implementation remains in:
    trade_v2_discord_bridge.py
"""

from trade_v2_discord_bridge import LiveStrategyV2DiscordBridge


class TradeDiscordBridge(LiveStrategyV2DiscordBridge):
    """Compatibility alias used by bot.py."""
    pass
