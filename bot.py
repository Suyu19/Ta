import discord
from discord.ext import commands, tasks
import asyncio
import datetime
import os
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
import yt_dlp
import base64
import tempfile
import aiohttp
import math
from typing import Optional
import json
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

print("BOOT VERSION: 2026-03-21-crypto-alert-daily-summary-1", flush=True)

# =========================
# 基本設定
# =========================

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

CHANNEL_ID_STR = os.getenv("CHANNEL_ID")
SEND_HOUR = int(os.getenv("SEND_HOUR", "20"))
SEND_MINUTE = int(os.getenv("SEND_MINUTE", "0"))

SLEEP_CHANNEL_ID_STR = os.getenv("SLEEP_CHANNEL_ID")
CRYPTO_ALERT_CHANNEL_ID_STR = os.getenv("CRYPTO_ALERT_CHANNEL_ID")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN 環境變數沒有設定！")

if CHANNEL_ID_STR is None:
    raise RuntimeError("CHANNEL_ID 環境變數沒有設定！")
CHANNEL_ID = int(CHANNEL_ID_STR)

if SLEEP_CHANNEL_ID_STR is None:
    raise RuntimeError("SLEEP_CHANNEL_ID 環境變數沒有設定！（睡覺提醒用的新文字頻道 ID）")
SLEEP_CHANNEL_ID = int(SLEEP_CHANNEL_ID_STR)

if CRYPTO_ALERT_CHANNEL_ID_STR is None:
    raise RuntimeError("CRYPTO_ALERT_CHANNEL_ID 環境變數沒有設定！（加密貨幣提醒用的新文字頻道 ID）")
CRYPTO_ALERT_CHANNEL_ID = int(CRYPTO_ALERT_CHANNEL_ID_STR)

DAILY_SUMMARY_HOUR = 19
DAILY_SUMMARY_MINUTE = 0

NEWS_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
]

NEWS_KEYWORDS = [
    "bitcoin", "btc", "ethereum", "eth", "bnb", "binance",
    "etf", "sec", "fed", "hack", "approval", "regulation",
    "stablecoin", "solana", "defi"
]

last_daily_summary_date: datetime.date | None = None

TZ = ZoneInfo("Asia/Taipei")

# 期中考期間
EXAM_START = datetime.date(2026, 4, 20)
EXAM_END = datetime.date(2026, 4, 24)

# Intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# 音樂狀態
music_queue = []
is_playing = False

task_started = False

# =========================
# Crypto Alert 設定
# =========================

price_history: dict[str, list[tuple[datetime.datetime, float]]] = {
    "BTC": [],
    "ETH": [],
    "BNB": [],
}

last_price_bucket = {
    "BTC": None,   # 每 1000
    "ETH": None,   # 每 100
}

last_percent_alert_at: dict[str, dict[str, datetime.datetime | None]] = {
    "BTC": {"15m_up": None, "15m_down": None, "1h_up": None, "1h_down": None},
    "ETH": {"15m_up": None, "15m_down": None, "1h_up": None, "1h_down": None},
    "BNB": {"15m_up": None, "15m_down": None, "1h_up": None, "1h_down": None},
}

# =========================
# Sleep Check 狀態（不落地保存）
# =========================

sleep_today: datetime.date | None = None
sleep_message_id: int | None = None
sleep_responded_users: set[int] = set()


def _sleep_label_time(dt: datetime.datetime) -> str:
    return f"{dt.month}月{dt.day}日的凌晨 2:00"


def _allowed_mentions_all():
    return discord.AllowedMentions(everyone=True, users=True, roles=False)


# =========================
# Crypto 工具函式
# =========================

def fmt_price(symbol: str, price: float) -> str:
    if symbol == "BTC":
        return f"${price:,.0f}"
    return f"${price:,.2f}"


def fmt_price_compact(symbol: str, price: float) -> str:
    if symbol == "BTC":
        return f"${price:,.0f}"
    return f"${price:,.0f}" if price >= 100 else f"${price:,.2f}"


def pct_change(old: float, new: float) -> float:
    if old == 0:
        return 0.0
    return (new - old) / old * 100.0


def get_bucket(price: float, step: int) -> int:
    return math.floor(price / step)


def should_send_cooldown(last_time: datetime.datetime | None, now: datetime.datetime, minutes: int) -> bool:
    if last_time is None:
        return True
    return (now - last_time).total_seconds() >= minutes * 60


async def fetch_crypto_prices():
    base_url = "https://data-api.binance.vision/api/v3/ticker/price"
    symbol_map = {
        "BTC": "BTCUSDT",
        "ETH": "ETHUSDT",
        "BNB": "BNBUSDT",
    }

    timeout = aiohttp.ClientTimeout(total=15)
    results = {}

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for coin, pair in symbol_map.items():
            async with session.get(base_url, params={"symbol": pair}) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"Binance API 錯誤：{resp.status} {text[:200]}")
                data = await resp.json()
                results[coin] = float(data["price"])

    return results


async def fetch_24h_ticker_stats():
    url = "https://data-api.binance.vision/api/v3/ticker/24hr"
    symbol_map = {
        "BTC": "BTCUSDT",
        "ETH": "ETHUSDT",
        "BNB": "BNBUSDT",
    }

    timeout = aiohttp.ClientTimeout(total=15)
    result = {}

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for key, symbol in symbol_map.items():
            async with session.get(url, params={"symbol": symbol}) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"Binance 24hr API 錯誤：{resp.status} {text[:200]}")
                item = await resp.json()

            result[key] = {
                "lastPrice": float(item["lastPrice"]),
                "priceChangePercent": float(item["priceChangePercent"]),
                "highPrice": float(item["highPrice"]),
                "lowPrice": float(item["lowPrice"]),
            }

    return result


def format_daily_summary_line(symbol: str, stats: dict) -> str:
    last_price = fmt_price_compact(symbol, stats["lastPrice"])
    pct = stats["priceChangePercent"]
    pct_str = f"+{pct:.1f}%" if pct >= 0 else f"{pct:.1f}%"
    high_price = fmt_price_compact(symbol, stats["highPrice"]).replace("$", "")
    low_price = fmt_price_compact(symbol, stats["lowPrice"]).replace("$", "")

    return f"{symbol}：{last_price}（24h {pct_str}，高：{high_price} / 低：{low_price}）"


async def fetch_rss_articles(feed_url: str):
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(feed_url) as resp:
            if resp.status != 200:
                return []
            text = await resp.text()

    try:
        root = ET.fromstring(text)
    except Exception:
        return []

    articles = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date_text = (item.findtext("pubDate") or "").strip()
        description = (item.findtext("description") or "").strip()

        if not title or not link:
            continue

        pub_dt = None
        if pub_date_text:
            try:
                pub_dt = parsedate_to_datetime(pub_date_text)
                if pub_dt.tzinfo is None:
                    pub_dt = pub_dt.replace(tzinfo=datetime.timezone.utc)
            except Exception:
                pub_dt = None

        articles.append({
            "title": title,
            "link": link,
            "description": description,
            "published_at": pub_dt,
        })

    return articles


def score_article(article: dict) -> int:
    text = f"{article['title']} {article['description']}".lower()
    score = 0

    for kw in NEWS_KEYWORDS:
        if kw in text:
            score += 2

    title_lower = article["title"].lower()
    for strong_kw in ["bitcoin", "btc", "ethereum", "eth", "binance", "etf", "sec", "hack"]:
        if strong_kw in title_lower:
            score += 2

    return score


async def get_top_crypto_news(limit: int = 2):
    all_articles = []
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now_utc - datetime.timedelta(hours=24)

    for feed_url in NEWS_FEEDS:
        articles = await fetch_rss_articles(feed_url)
        for article in articles:
            pub_dt = article["published_at"]
            if pub_dt is not None and pub_dt < cutoff:
                continue
            all_articles.append(article)

    dedup = {}
    for article in all_articles:
        key = article["title"].strip().lower()
        if key not in dedup:
            dedup[key] = article

    scored = list(dedup.values())
    scored.sort(
        key=lambda a: (
            score_article(a),
            a["published_at"].timestamp() if a["published_at"] else 0
        ),
        reverse=True
    )

    return scored[:limit]


async def build_daily_summary_message(now_dt: datetime.datetime) -> str:
    stats = await fetch_24h_ticker_stats()
    news_items = await get_top_crypto_news(limit=2)

    lines = [
        f"📊 每日幣圈摘要（{now_dt.month:02d}/{now_dt.day:02d} {now_dt.hour:02d}:{now_dt.minute:02d}）",
        "",
        format_daily_summary_line("BTC", stats["BTC"]),
        format_daily_summary_line("ETH", stats["ETH"]),
        format_daily_summary_line("BNB", stats["BNB"]),
    ]

    if news_items:
        lines.append("")
        lines.append("📰 今日重點新聞")
        for idx, article in enumerate(news_items, start=1):
            lines.append(f"{idx}. {article['title']}")
            lines.append(article["link"])

    return "\n".join(lines)


async def check_percent_alerts(channel: discord.TextChannel, symbol: str, now: datetime.datetime, current_price: float):
    history = price_history[symbol]
    if not history:
        return

    price_15m = None
    target_15m = now - datetime.timedelta(minutes=15)
    for ts, price in history:
        if ts <= target_15m:
            price_15m = price
        else:
            break

    price_1h = None
    target_1h = now - datetime.timedelta(hours=1)
    for ts, price in history:
        if ts <= target_1h:
            price_1h = price
        else:
            break

    if price_15m is not None:
        change_15m = pct_change(price_15m, current_price)

        if change_15m >= 1.0:
            key = "15m_up"
            if should_send_cooldown(last_percent_alert_at[symbol][key], now, 15):
                await channel.send(
                    f"🚨 {symbol} 劇烈波動提醒\n"
                    f"目前價格：{fmt_price(symbol, current_price)}\n"
                    f"15 分鐘內上漲：+{change_15m:.2f}%"
                )
                last_percent_alert_at[symbol][key] = now

        elif change_15m <= -1.0:
            key = "15m_down"
            if should_send_cooldown(last_percent_alert_at[symbol][key], now, 15):
                await channel.send(
                    f"🚨 {symbol} 劇烈波動提醒\n"
                    f"目前價格：{fmt_price(symbol, current_price)}\n"
                    f"15 分鐘內下跌：{change_15m:.2f}%"
                )
                last_percent_alert_at[symbol][key] = now

    if price_1h is not None:
        change_1h = pct_change(price_1h, current_price)

        if change_1h >= 2.0:
            key = "1h_up"
            if should_send_cooldown(last_percent_alert_at[symbol][key], now, 30):
                await channel.send(
                    f"🚨 {symbol} 劇烈波動提醒\n"
                    f"目前價格：{fmt_price(symbol, current_price)}\n"
                    f"1 小時內上漲：+{change_1h:.2f}%"
                )
                last_percent_alert_at[symbol][key] = now

        elif change_1h <= -2.0:
            key = "1h_down"
            if should_send_cooldown(last_percent_alert_at[symbol][key], now, 30):
                await channel.send(
                    f"🚨 {symbol} 劇烈波動提醒\n"
                    f"目前價格：{fmt_price(symbol, current_price)}\n"
                    f"1 小時內下跌：{change_1h:.2f}%"
                )
                last_percent_alert_at[symbol][key] = now


async def check_breakout_alerts(channel: discord.TextChannel, symbol: str, current_price: float):
    if symbol == "BTC":
        step = 1000
    elif symbol == "ETH":
        step = 100
    else:
        return

    current_bucket = get_bucket(current_price, step)
    previous_bucket = last_price_bucket[symbol]

    if previous_bucket is None:
        last_price_bucket[symbol] = current_bucket
        return

    if current_bucket > previous_bucket:
        crossed_price = current_bucket * step
        await channel.send(f"{symbol}突破 {crossed_price}！📈")
    elif current_bucket < previous_bucket:
        crossed_price = previous_bucket * step
        await channel.send(f"{symbol}跌破{crossed_price}！📉")

    last_price_bucket[symbol] = current_bucket


@tasks.loop(minutes=2)
async def crypto_price_watch_task():
    await bot.wait_until_ready()

    channel = bot.get_channel(CRYPTO_ALERT_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(CRYPTO_ALERT_CHANNEL_ID)
        except Exception as e:
            print(f"[crypto] 無法取得提醒頻道：{e}", flush=True)
            return

    if not isinstance(channel, discord.TextChannel):
        print("[crypto] CRYPTO_ALERT_CHANNEL_ID 不是文字頻道", flush=True)
        return

    now = datetime.datetime.now(TZ)

    try:
        prices = await fetch_crypto_prices()
    except Exception as e:
        print(f"[crypto] 抓價格失敗：{e}", flush=True)
        return

    for symbol, current_price in prices.items():
        history = price_history[symbol]
        history.append((now, current_price))

        cutoff = now - datetime.timedelta(hours=2)
        while history and history[0][0] < cutoff:
            history.pop(0)

        await check_percent_alerts(channel, symbol, now, current_price)
        await check_breakout_alerts(channel, symbol, current_price)


@crypto_price_watch_task.before_loop
async def before_crypto_price_watch_task():
    await bot.wait_until_ready()


# =========================
# FFmpeg / yt-dlp 設定
# =========================

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


def ensure_cookies_file() -> Optional[str]:
    b64 = os.getenv("YT_COOKIES_B64")
    if not b64:
        print("[yt] YT_COOKIES_B64 not set", flush=True)
        return None

    path = os.path.join(tempfile.gettempdir(), "yt_cookies.txt")
    try:
        with open(path, "wb") as f:
            f.write(base64.b64decode(b64))
        print(f"[yt] cookies written: {path} ({os.path.getsize(path)} bytes)", flush=True)
        return path
    except Exception as e:
        print(f"[yt] cookies decode/write failed: {e}", flush=True)
        return None


def build_ytdlp_options():
    cookies_path = ensure_cookies_file()

    opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "nocheckcertificate": True,
        "cachedir": False,
        "force_ipv4": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web"],
            }
        },
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        },
    }

    if cookies_path:
        opts["cookiefile"] = cookies_path
    else:
        print("[yt] WARNING: cookiefile not available -> likely to get 'not a bot' error", flush=True)

    return opts


async def get_stream_info(url: str):
    loop = asyncio.get_running_loop()
    ydl_opts = build_ytdlp_options()

    def _extract():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if "entries" in info:
                info = info["entries"][0]
            return {
                "title": info.get("title", "未知音樂"),
                "stream_url": info["url"],
            }

    return await loop.run_in_executor(None, _extract)


# =========================
# Sleep Check UI（按鈕 + Modal）
# =========================

class NotSleepModal(discord.ui.Modal, title="還沒睡（告訴我為什麼！）"):
    reason = discord.ui.TextInput(
        label="原因（必填）",
        placeholder="例如：在趕報告 / 打遊戲停不下來 / 失眠…",
        required=True,
        min_length=1,
        max_length=200,
    )

    def __init__(self, channel: discord.TextChannel):
        super().__init__(timeout=180)
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        global sleep_responded_users

        user_id = interaction.user.id
        if user_id in sleep_responded_users:
            await interaction.response.send_message("你今天已回報過了，不能修改喔！", ephemeral=True)
            return

        sleep_responded_users.add(user_id)

        reason_text = str(self.reason.value).strip()
        await interaction.response.send_message("已記錄 ✅", ephemeral=True)

        await self.channel.send(
            f"❌ {interaction.user.mention} 還沒睡\n原因：{reason_text}",
            allowed_mentions=_allowed_mentions_all(),
        )


class SleepCheckView(discord.ui.View):
    def __init__(self, channel: discord.TextChannel):
        super().__init__(timeout=None)
        self.channel = channel

    @discord.ui.button(label="✅ 我睡了", style=discord.ButtonStyle.success)
    async def slept(self, interaction: discord.Interaction, button: discord.ui.Button):
        global sleep_responded_users

        user_id = interaction.user.id
        if user_id in sleep_responded_users:
            await interaction.response.send_message("你今天已回報過了，不能修改喔！", ephemeral=True)
            return

        sleep_responded_users.add(user_id)
        await interaction.response.send_message("已記錄 ✅", ephemeral=True)

        await self.channel.send(
            f"✅ {interaction.user.mention} 我睡了",
            allowed_mentions=_allowed_mentions_all(),
        )

    @discord.ui.button(label="❌ 還沒睡（告訴我為什麼！）", style=discord.ButtonStyle.danger)
    async def not_slept(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id
        if user_id in sleep_responded_users:
            await interaction.response.send_message("你今天已回報過了，不能修改喔！", ephemeral=True)
            return

        await interaction.response.send_modal(NotSleepModal(self.channel))


# =========================
# Sleep Check 排程：02:00 發 + 02:30 檢查 tag
# =========================

async def run_sleep_check_now(channel: discord.TextChannel):
    global sleep_today, sleep_responded_users

    guild = channel.guild

    members: list[discord.Member] = []
    try:
        members = [m for m in guild.members]
        if len(members) == 0:
            async for m in guild.fetch_members(limit=None):
                members.append(m)
    except Exception as e:
        print(f"[sleep] 取得成員名單失敗：{e}", flush=True)

    targets = []
    for m in members:
        if m.bot:
            continue
        if m.id in sleep_responded_users:
            continue
        targets.append(m)

    if not targets:
        await channel.send("🎉 檢查結果：大家都回報了！晚安～", allowed_mentions=_allowed_mentions_all())
        return

    await channel.send(
        "@everyone ⏰ 測試檢查：還沒回報的人請按上方按鈕回報～",
        allowed_mentions=_allowed_mentions_all(),
    )

    chunk = []
    current_len = 0
    for m in targets:
        mention = m.mention
        add_len = len(mention) + 1
        if current_len + add_len > 1800:
            await channel.send(
                "還沒回報的人： " + " ".join(chunk),
                allowed_mentions=_allowed_mentions_all(),
            )
            chunk = []
            current_len = 0
        chunk.append(mention)
        current_len += add_len

    if chunk:
        await channel.send(
            "還沒回報的人： " + " ".join(chunk),
            allowed_mentions=_allowed_mentions_all(),
        )


async def sleep_check_task():
    global sleep_today, sleep_message_id, sleep_responded_users

    await bot.wait_until_ready()

    channel = bot.get_channel(SLEEP_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(SLEEP_CHANNEL_ID)
        except Exception as e:
            print(f"[sleep] 無法取得 SLEEP_CHANNEL_ID 頻道：{e}", flush=True)
            return

    if not isinstance(channel, discord.TextChannel):
        print("[sleep] SLEEP_CHANNEL_ID 不是文字頻道，請確認設定", flush=True)
        return

    print("[sleep] Sleep check task started (TZ=Asia/Taipei)", flush=True)

    while not bot.is_closed():
        now = datetime.datetime.now(TZ)

        send_dt = now.replace(hour=2, minute=0, second=0, microsecond=0)
        check_dt = now.replace(hour=2, minute=30, second=0, microsecond=0)

        if now >= check_dt:
            send_dt = send_dt + datetime.timedelta(days=1)
            check_dt = check_dt + datetime.timedelta(days=1)
        elif now >= send_dt:
            pass

        if sleep_today != send_dt.date():
            wait_send = (send_dt - datetime.datetime.now(TZ)).total_seconds()
            if wait_send > 0:
                await asyncio.sleep(wait_send)

            now2 = datetime.datetime.now(TZ)
            today = now2.date()

            if sleep_today != today:
                sleep_today = today
                sleep_responded_users = set()

            sleep_message_id = None

            label_time = _sleep_label_time(now2)
            content = (
                f"🌙 現在是 **{label_time}**，該睡覺囉！\n"
                f"請在下方回報：你有沒有乖乖睡覺？"
            )

            msg = await channel.send(
                content,
                view=SleepCheckView(channel),
                allowed_mentions=_allowed_mentions_all()
            )
            sleep_message_id = msg.id

        wait_check = (check_dt - datetime.datetime.now(TZ)).total_seconds()
        if wait_check > 0:
            await asyncio.sleep(wait_check)

        now3 = datetime.datetime.now(TZ)
        if sleep_today != now3.date():
            continue

        guild = channel.guild

        members: list[discord.Member] = []
        try:
            members = [m for m in guild.members]
            if len(members) == 0:
                async for m in guild.fetch_members(limit=None):
                    members.append(m)
        except Exception as e:
            print(f"[sleep] 取得成員名單失敗：{e}", flush=True)

        targets = []
        for m in members:
            if m.bot:
                continue
            if m.id in sleep_responded_users:
                continue
            targets.append(m)

        if not targets:
            await channel.send("🎉 02:30 檢查：大家都回報了！晚安～", allowed_mentions=_allowed_mentions_all())
        else:
            await channel.send(
                "@everyone ⏰ 02:30 了！還沒回報的人請趕快按上方按鈕回報～",
                allowed_mentions=_allowed_mentions_all(),
            )

            chunk = []
            current_len = 0
            for m in targets:
                mention = m.mention
                add_len = len(mention) + 1
                if current_len + add_len > 1800:
                    await channel.send(
                        "還沒回報的人： " + " ".join(chunk),
                        allowed_mentions=_allowed_mentions_all(),
                    )
                    chunk = []
                    current_len = 0
                chunk.append(mention)
                current_len += add_len

            if chunk:
                await channel.send(
                    "還沒回報的人： " + " ".join(chunk),
                    allowed_mentions=_allowed_mentions_all(),
                )


# =========================
# 播放下一首（核心）
# =========================

async def play_next(ctx):
    global is_playing

    if len(music_queue) == 0:
        is_playing = False
        return

    is_playing = True
    item = music_queue.pop(0)
    voice_client = ctx.voice_client

    if voice_client is None:
        is_playing = False
        return

    try:
        if item["type"] == "yt":
            info = await get_stream_info(item["url"])
            title = info["title"]
            stream_url = info["stream_url"]
            source = discord.FFmpegPCMAudio(stream_url, **FFMPEG_OPTIONS)

        elif item["type"] == "file":
            title = item.get("title", "本地音檔")
            source = discord.FFmpegPCMAudio(item["path"])

        else:
            raise RuntimeError("未知的 queue 類型")

    except Exception as e:
        await ctx.send(f"❌ 取得音訊失敗：`{e}`\n（可能是 YouTube 驗證或雲端 IP 被擋）")
        asyncio.create_task(play_next(ctx))
        return

    def after_playing(error):
        if error:
            print(f"播放發生錯誤：{error}", flush=True)

        if item["type"] == "file":
            try:
                p = item["path"]
                if os.path.exists(p):
                    os.remove(p)
            except Exception as ex:
                print(f"刪除暫存檔失敗：{ex}", flush=True)

        asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)

    voice_client.play(source, after=after_playing)
    await ctx.send(f"▶ 正在播放：**{title}**")


# =========================
# 倒數排程
# =========================

async def countdown_task():
    await bot.wait_until_ready()
    channel = bot.get_channel(CHANNEL_ID)

    if channel is None:
        print("找不到頻道，請確認 CHANNEL_ID 是否正確！", flush=True)
        return

    print("倒數排程啟動…（時區：Asia/Taipei）", flush=True)

    while not bot.is_closed():
        now = datetime.datetime.now(TZ)
        today_send = now.replace(hour=SEND_HOUR, minute=SEND_MINUTE, second=0, microsecond=0)

        if now >= today_send:
            next_send = today_send + datetime.timedelta(days=1)
        else:
            next_send = today_send

        wait_seconds = (next_send - now).total_seconds()
        print(f"下一次發訊息時間（Asia/Taipei）：{next_send}（等待 {wait_seconds:.0f} 秒）", flush=True)
        await asyncio.sleep(wait_seconds)

        now = datetime.datetime.now(TZ)
        today = now.date()

        if today == EXAM_START:
            msg = "(4/20) 今天是期中考第一天！Fight！！💪📚"
        elif EXAM_START < today < EXAM_END:
            msg = f"({today.month}/{today.day}) 期中考進行中！加油！！🔥"
        elif today == EXAM_END:
            msg = "(4/24) 今天是期中考最後一天！撐住！！🎯"
        elif today > EXAM_END:
            days_after = (today - EXAM_END).days
            msg = f"📘 期中考已經結束 {days_after} 天，辛苦了～🎉"
        else:
            diff = (EXAM_START - today).days
            msg = f"📘 期中考倒數：還剩 **{diff} 天**！（考試第一天：4/20）"

        await channel.send(msg)


async def daily_crypto_summary_task():
    global last_daily_summary_date

    await bot.wait_until_ready()

    channel = bot.get_channel(CRYPTO_ALERT_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(CRYPTO_ALERT_CHANNEL_ID)
        except Exception as e:
            print(f"[daily-summary] 無法取得提醒頻道：{e}", flush=True)
            return

    if not isinstance(channel, discord.TextChannel):
        print("[daily-summary] CRYPTO_ALERT_CHANNEL_ID 不是文字頻道", flush=True)
        return

    print("[daily-summary] 每日幣圈摘要排程啟動", flush=True)

    while not bot.is_closed():
        now = datetime.datetime.now(TZ)
        target = now.replace(hour=DAILY_SUMMARY_HOUR, minute=DAILY_SUMMARY_MINUTE, second=0, microsecond=0)

        if now >= target:
            target = target + datetime.timedelta(days=1)

        wait_seconds = (target - now).total_seconds()
        await asyncio.sleep(wait_seconds)

        now2 = datetime.datetime.now(TZ)
        today = now2.date()

        if last_daily_summary_date == today:
            continue

        try:
            msg = await build_daily_summary_message(now2)
        except Exception as e:
            print(f"[daily-summary] 建立摘要失敗：{e}", flush=True)
            continue

        await channel.send(msg)
        last_daily_summary_date = today


@bot.event
async def on_ready():
    global task_started
    print(f"Bot 已登入：{bot.user}", flush=True)
    if not task_started:
        asyncio.create_task(countdown_task())
        asyncio.create_task(sleep_check_task())
        asyncio.create_task(daily_crypto_summary_task())
        crypto_price_watch_task.start()
        task_started = True


# =========================
# 指令：exam / help / sleeptest / sleepcheck / price / dailytest
# =========================

@bot.command(name="sleep")
async def early_sleep(ctx: commands.Context):
    global sleep_today, sleep_responded_users

    now = datetime.datetime.now(TZ)
    today = now.date()

    if sleep_today != today:
        sleep_today = today
        sleep_responded_users = set()

    user_id = ctx.author.id
    if user_id in sleep_responded_users:
        await ctx.send("你今天已回報過了，不能修改喔！")
        return

    sleep_responded_users.add(user_id)

    channel = bot.get_channel(SLEEP_CHANNEL_ID)
    if channel is None:
        channel = await bot.fetch_channel(SLEEP_CHANNEL_ID)

    await channel.send(
        f"✅ {ctx.author.mention} 我睡了（提前回報：{now.hour:02d}:{now.minute:02d}）",
        allowed_mentions=_allowed_mentions_all(),
    )


@bot.command(name="nosleep")
async def early_no_sleep(ctx: commands.Context, *, reason: str = ""):
    global sleep_today, sleep_responded_users

    reason = reason.strip()
    if not reason:
        await ctx.send("❌ 你要說明原因喔！用法：`!nosleep 原因...`")
        return

    now = datetime.datetime.now(TZ)
    today = now.date()

    if sleep_today != today:
        sleep_today = today
        sleep_responded_users = set()

    user_id = ctx.author.id
    if user_id in sleep_responded_users:
        await ctx.send("你今天已回報過了，不能修改喔！")
        return

    sleep_responded_users.add(user_id)

    channel = bot.get_channel(SLEEP_CHANNEL_ID)
    if channel is None:
        channel = await bot.fetch_channel(SLEEP_CHANNEL_ID)

    await channel.send(
        f"❌ {ctx.author.mention} 還沒睡（提前回報：{now.hour:02d}:{now.minute:02d}）\n原因：{reason[:200]}",
        allowed_mentions=_allowed_mentions_all(),
    )


@bot.command(name="exam")
async def exam_countdown(ctx: commands.Context):
    today = datetime.datetime.now(TZ).date()

    if today < EXAM_START:
        days = (EXAM_START - today).days
        msg = f"📘 距離期中考第一天（4/20）還有 **{days} 天**！"
    elif today == EXAM_START:
        msg = "📘 今天是期中考第一天（4/20）！Fight！！🔥"
    elif EXAM_START < today < EXAM_END:
        day_no = (today - EXAM_START).days + 1
        left = (EXAM_END - today).days
        msg = (
            f"📘 期中考進行中（第 **{day_no} 天**）！\n"
            f"⏳ 距離最後一天（4/24）還有 **{left} 天**"
        )
    elif today == EXAM_END:
        msg = "📘 今天是期中考最後一天（4/24） 解脫了！"
    else:
        days_after = (today - EXAM_END).days
        msg = f"🎉 期中考已結束 **{days_after} 天**，辛苦了～"

    await ctx.send(msg)


@bot.command(name="price")
async def price_now(ctx: commands.Context):
    try:
        prices = await fetch_crypto_prices()
    except Exception as e:
        await ctx.send(f"❌ 抓價格失敗：{e}")
        return

    msg = (
        f"BTC：{fmt_price('BTC', prices['BTC'])}\n"
        f"ETH：{fmt_price('ETH', prices['ETH'])}\n"
        f"BNB：{fmt_price('BNB', prices['BNB'])}"
    )
    await ctx.send(msg)


@bot.command(name="dailytest")
@commands.has_permissions(administrator=True)
async def daily_test(ctx: commands.Context):
    now = datetime.datetime.now(TZ)
    try:
        msg = await build_daily_summary_message(now)
    except Exception as e:
        await ctx.send(f"❌ 測試每日摘要失敗：{e}")
        return

    await ctx.send(msg)


@bot.command(name="help")
async def custom_help(ctx: commands.Context):
    msg = (
        "!後：\n"
        "  help  顯示所有可用功能指令\n"
        "  exam  顯示期中考倒數\n"
        "  price  顯示 BTC / ETH / BNB 目前價格\n"
        "  dailytest  測試每日幣圈摘要（管理員）\n\n"
        "  join   加入語音頻道陪你\n"
        "  bye   離開語音頻道\n\n"
        "  clear （數字） 清除當前頻道最近 X 則訊息\n\n"
        "  play  播放這則訊息附帶的 mp3 檔\n"
        "  yt      後接網址播放音樂\n"
        "  skip  跳到清單下一首\n"
        "  stop  停止所有音樂播放\n\n"
        "  sleep 提前回報要睡覺\n"
        "  nosleep 提前回報不睡覺(空格原因直接打)\n"
        "  sleeptest   立刻發出睡覺回報按鈕（測試）\n"
        "  sleepcheck  立刻做一次未回報檢查（測試）\n\n"
        "【自動提醒】\n"
        "  BTC / ETH / BNB：15 分鐘內漲跌超過 1% 提醒\n"
        "  BTC / ETH / BNB：1 小時內漲跌超過 2% 提醒\n"
        "  BTC：每跨 1000 美元提醒\n"
        "  ETH：每跨 100 美元提醒\n"
        "  每天 19:00 自動發送每日幣圈摘要與 2 則重點新聞\n"
    )
    await ctx.send(msg)


@bot.command(name="sleeptest")
@commands.has_permissions(administrator=True)
async def sleep_test(ctx: commands.Context):
    global sleep_today, sleep_message_id, sleep_responded_users

    channel = bot.get_channel(SLEEP_CHANNEL_ID)
    if channel is None:
        channel = await bot.fetch_channel(SLEEP_CHANNEL_ID)

    if not isinstance(channel, discord.TextChannel):
        await ctx.send("❌ SLEEP_CHANNEL_ID 不是文字頻道，請檢查設定。")
        return

    now = datetime.datetime.now(TZ)
    sleep_today = now.date()
    sleep_message_id = None
    sleep_responded_users = set()

    content = (
        f"🧪（測試）🌙 現在是 **{now.month}月{now.day}日的凌晨 2:00**，該睡覺囉！\n"
        f"請在下方回報：你有沒有乖乖睡覺？"
    )
    msg = await channel.send(content, view=SleepCheckView(channel), allowed_mentions=_allowed_mentions_all())
    sleep_message_id = msg.id

    await ctx.send("✅ 已在睡覺頻道發出測試訊息（含按鈕）。")


@bot.command(name="sleepcheck")
@commands.has_permissions(administrator=True)
async def sleep_check_now(ctx: commands.Context):
    channel = bot.get_channel(SLEEP_CHANNEL_ID)
    if channel is None:
        channel = await bot.fetch_channel(SLEEP_CHANNEL_ID)

    if not isinstance(channel, discord.TextChannel):
        await ctx.send("❌ SLEEP_CHANNEL_ID 不是文字頻道，請檢查設定。")
        return

    await run_sleep_check_now(channel)
    await ctx.send("✅ 已執行一次測試檢查（請看睡覺頻道）。")


# =========================
# join / bye
# =========================

@bot.command(name="join")
async def join_voice(ctx: commands.Context):
    voice_state = ctx.author.voice
    if voice_state is None or voice_state.channel is None:
        await ctx.send("要先進入一個語音頻道，我才能跟上去唷！")
        return

    channel = voice_state.channel

    if ctx.voice_client is not None:
        if ctx.voice_client.channel.id == channel.id:
            await ctx.send("我已經在這個語音頻道裡啦！")
            return
        await ctx.voice_client.move_to(channel)
        await ctx.send(f"跟隨你到：{channel.name} 頻道囉~")
    else:
        await channel.connect()
        await ctx.send(f"我已經加入：{channel.name} 頻道陪你囉~")


@bot.command(name="bye")
async def leave_voice(ctx: commands.Context):
    voice_client = ctx.voice_client
    if voice_client is None:
        await ctx.send("我現在沒有在任何語音頻道裡唷！")
        return
    await voice_client.disconnect()
    await ctx.send("下次歡迎再來找我唷~")


# =========================
# clear
# =========================

@bot.command(name="clear")
@commands.has_permissions(manage_messages=True)
async def clear_messages(ctx: commands.Context, amount: int):
    if amount <= 0:
        await ctx.send("請輸入大於 0 的數量喔！")
        return

    deleted = await ctx.channel.purge(limit=amount + 1)
    count = len(deleted) - 1
    msg = await ctx.send(f"🧹 已清除 {count} 則訊息")
    await asyncio.sleep(3)
    await msg.delete()


@clear_messages.error
async def clear_messages_error(ctx: commands.Context, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("你沒有**管理訊息**的權限，不能使用這個指令！")
    else:
        print(f"clear 指令錯誤：{error}", flush=True)


# =========================
# play：播放上傳 mp3（改成進 queue）
# =========================

@bot.command(name="play")
async def play_audio(ctx: commands.Context):
    voice_state = ctx.author.voice
    if voice_state is None or voice_state.channel is None:
        await ctx.send("你要先進入一個語音頻道，我才能幫你播音樂唷！")
        return

    voice_client = ctx.voice_client
    channel = voice_state.channel

    if voice_client is None:
        await channel.connect()
        await ctx.send(f"我已經加入：{channel.name} 頻道囉，準備幫你播音樂～")
    else:
        if voice_client.channel.id != channel.id:
            await voice_client.move_to(channel)
            await ctx.send(f"我換到：{channel.name} 頻道囉～")

    if not ctx.message.attachments:
        await ctx.send("請把 mp3 檔案當作**附件**一起傳給我，再使用 `!play` 喔～")
        return

    attachment = ctx.message.attachments[0]
    if not attachment.filename.lower().endswith(".mp3"):
        await ctx.send("目前我只支援 `.mp3` 檔案喔 QQ")
        return

    temp_filename = f"temp_{attachment.id}.mp3"
    await attachment.save(temp_filename)

    music_queue.append({"type": "file", "path": temp_filename, "title": attachment.filename})
    await ctx.send(f"🎵 已加入播放清單：**{attachment.filename}**")

    if not is_playing:
        await play_next(ctx)


# =========================
# yt：播放 YouTube（queue 存 url）
# =========================

@bot.command(name="yt")
async def play_youtube(ctx: commands.Context, url: str):
    global is_playing

    voice_state = ctx.author.voice
    if voice_state is None or voice_state.channel is None:
        await ctx.send("你要先進入語音頻道喔！")
        return

    voice_client = ctx.voice_client
    channel = voice_state.channel

    if voice_client is None:
        await channel.connect()
    elif voice_client.channel.id != channel.id:
        await voice_client.move_to(channel)

    music_queue.append({"type": "yt", "url": url})
    await ctx.send("🎵 已加入播放清單（播放時會抓最新串流）")

    if not is_playing:
        await play_next(ctx)


@bot.command(name="stop")
async def stop_audio(ctx: commands.Context):
    global music_queue, is_playing

    voice_client = ctx.voice_client
    if voice_client is None:
        await ctx.send("我目前不在語音頻道中喔！")
        return

    music_queue.clear()
    is_playing = False
    voice_client.stop()

    await ctx.send("⏹ 已停止播放並清空播放清單！")


@bot.command(name="skip")
async def skip_song(ctx: commands.Context):
    voice_client = ctx.voice_client

    if voice_client is None or not voice_client.is_playing():
        await ctx.send("目前沒有音樂正在播放哦！")
        return

    voice_client.stop()
    await ctx.send("⏭ 已跳到下一首！")


bot.run(TOKEN)