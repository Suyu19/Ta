from __future__ import annotations
import discord
from discord.ext import commands, tasks
import asyncio
import datetime
import os
import random
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
from trade_discord_bridge import TradeDiscordBridge

print("BOOT VERSION: 2026-09-02-strategy-v2.2-range-alpha-live1-read-timeout-hotfix1", flush=True)

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
GIVEAWAY_CHANNEL_ID_STR = os.getenv("GIVEAWAY_CHANNEL_ID")
GIVEAWAY_CHANNEL_ID = int(GIVEAWAY_CHANNEL_ID_STR) if GIVEAWAY_CHANNEL_ID_STR else None

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

# 研究所推甄報名資料提交日
GRAD_APP_DATE = datetime.date(2026, 9, 25)

# Intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# =========================
# Strategy v2.2 + Range Alpha / Forward Paper Discord 橋接
# =========================
# TRADE_CHANNEL_ID 可指定獨立交易頻道；若未設定，暫時沿用 CRYPTO_ALERT_CHANNEL_ID。
# 雲端部署時請把 TRADE_DATA_DIR 指向「持久化磁碟」，例如 /data/trading。
trade_bridge = TradeDiscordBridge(
    bot,
    TZ,
    fallback_channel_id=CRYPTO_ALERT_CHANNEL_ID,
)

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
    "BTC": None,  # 每 1000
    "ETH": None,  # 每 100
}

# 紀錄上一次 3% 波動通知的時間與價格
# 規則：一般情況比較「最近 1 小時」；通知後 1 小時內，改成比較「上次通知價格」。
last_percent_alert_state: dict[str, dict[str, datetime.datetime | float | None]] = {
    "BTC": {"time": None, "price": None},
    "ETH": {"time": None, "price": None},
    "BNB": {"time": None, "price": None},
}

# 使用者自訂價格提醒（記憶體保存，重啟後會消失）
custom_price_alerts: dict[str, list[dict]] = {
    "BTC": [],
    "ETH": [],
    "BNB": [],
}

# 上一次看到的價格，用來判斷是否穿越提醒價位
last_seen_prices: dict[str, float | None] = {
    "BTC": None,
    "ETH": None,
    "BNB": None,
}

# =========================
# 記帳系統設定（JSON 保存）
# =========================

ACCOUNTING_FILE = "accounting_data.json"
ACCOUNTING_ACCOUNTS = ("suyu", "gary", "win")

# 帳戶名稱 -> {
#   "balance": float,
#   "records": [
#       {
#           "type": str,
#           "amount": float,
#           "reason": str,
#           "time": str,
#           "balance_after": float,
#           "operator_id": int,
#           "operator_name": str,
#       }
#   ]
# }
accounting_data: dict[str, dict] = {}


def normalize_account_name(name: str) -> str | None:
    key = name.strip().lower()
    if key in ACCOUNTING_ACCOUNTS:
        return key
    return None


def ensure_accounting_accounts():
    for account_name in ACCOUNTING_ACCOUNTS:
        if account_name not in accounting_data or not isinstance(accounting_data[account_name], dict):
            accounting_data[account_name] = {"balance": 0.0, "records": []}

        try:
            accounting_data[account_name]["balance"] = float(accounting_data[account_name].get("balance", 0.0))
        except Exception:
            accounting_data[account_name]["balance"] = 0.0

        if not isinstance(accounting_data[account_name].get("records"), list):
            accounting_data[account_name]["records"] = []


def load_accounting_data():
    global accounting_data
    if not os.path.exists(ACCOUNTING_FILE):
        accounting_data = {}
        ensure_accounting_accounts()
        return
    try:
        with open(ACCOUNTING_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        accounting_data = data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[accounting] 載入記帳資料失敗：{e}", flush=True)
        accounting_data = {}
    ensure_accounting_accounts()


def save_accounting_data():
    ensure_accounting_accounts()
    try:
        with open(ACCOUNTING_FILE, "w", encoding="utf-8") as f:
            json.dump(accounting_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[accounting] 儲存記帳資料失敗：{e}", flush=True)


def get_account(account_name: str):
    normalized = normalize_account_name(account_name)
    if normalized is None:
        raise ValueError("帳戶只支援 suyu、gary 或 win")
    ensure_accounting_accounts()
    return accounting_data[normalized]


def fmt_money(amount: float) -> str:
    if float(amount).is_integer():
        return f"{int(amount):,}"
    return f"{amount:,.2f}"


def add_accounting_record(account_name: str, record_type: str, amount: float, reason: str, operator):
    account_key = normalize_account_name(account_name)
    if account_key is None:
        raise ValueError("帳戶只支援 suyu、gary 或 win")

    account = get_account(account_key)
    if record_type == "income":
        account["balance"] += amount
    elif record_type == "expense":
        account["balance"] -= amount
    else:
        raise ValueError("record_type 必須是 income 或 expense")

    record = {
        "type": record_type,
        "amount": amount,
        "reason": reason[:200],
        "time": datetime.datetime.now(TZ).isoformat(timespec="minutes"),
        "balance_after": account["balance"],
        "operator_id": operator.id,
        "operator_name": str(operator),
    }
    account["records"].append(record)
    account["records"] = account["records"][-200:]
    save_accounting_data()
    return record, account["balance"]


load_accounting_data()

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


def normalize_coin_symbol(symbol: str) -> str | None:
    s = symbol.strip().upper()
    if s in {"BTC", "ETH", "BNB"}:
        return s
    return None


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

    state = last_percent_alert_state[symbol]
    last_time = state["time"]
    last_price = state["price"]

    # 通知後 1 小時內：除非相較「上次通知價格」又漲跌 3% 以上，否則不再通知。
    if isinstance(last_time, datetime.datetime) and isinstance(last_price, (int, float)):
        if (now - last_time).total_seconds() <= 3600:
            change_from_last_alert = pct_change(float(last_price), current_price)
            if abs(change_from_last_alert) >= 3.0:
                direction = "上漲" if change_from_last_alert > 0 else "下跌"
                await channel.send(
                    f"@everyone 🚨 **{symbol} 劇烈波動提醒（連續觸發）**\n"
                    f"目前價格：{fmt_price(symbol, current_price)}\n"
                    f"相較上次通知（{last_time.strftime('%H:%M')}）又{direction}：{abs(change_from_last_alert):.2f}%",
                    allowed_mentions=_allowed_mentions_all(),
                )
                state["time"] = now
                state["price"] = current_price
            return

    # 一般情況：比較約 1 小時前價格，只有 1 小時內漲跌 3% 以上才通知。
    price_1h = None
    target_1h = now - datetime.timedelta(hours=1)
    for ts, price in history:
        if ts <= target_1h:
            price_1h = price
        else:
            break

    if price_1h is None:
        return

    change_1h = pct_change(price_1h, current_price)
    if abs(change_1h) >= 3.0:
        direction = "上漲" if change_1h > 0 else "下跌"
        await channel.send(
            f"@everyone 🚨 **{symbol} 劇烈波動提醒**\n"
            f"目前價格：{fmt_price(symbol, current_price)}\n"
            f"1 小時內{direction}：{abs(change_1h):.2f}%",
            allowed_mentions=_allowed_mentions_all(),
        )
        state["time"] = now
        state["price"] = current_price


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


async def check_custom_price_alerts(channel: discord.TextChannel, symbol: str, current_price: float):
    prev_price = last_seen_prices[symbol]
    alerts = custom_price_alerts[symbol]

    if prev_price is None:
        last_seen_prices[symbol] = current_price
        return

    for alert in alerts:
        if alert["triggered"]:
            continue

        target = alert["price"]

        crossed_up = prev_price < target <= current_price
        crossed_down = prev_price > target >= current_price

        if crossed_up or crossed_down:
            alert["triggered"] = True
            await channel.send(
                f"@everyone 🚨 {symbol} 價格提醒\n"
                f"{symbol} 已觸及你設定的價格：{target:,.2f}\n"
                f"目前價格：{fmt_price(symbol, current_price)}",
                allowed_mentions=_allowed_mentions_all(),
            )

    last_seen_prices[symbol] = current_price


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
        # 使用者要求自動價格波動通知只保留「1 小時 3%」規則，
        # 因此不再啟用 BTC 每 1000 / ETH 每 100 的突破跌破通知。
        # await check_breakout_alerts(channel, symbol, current_price)
        await check_custom_price_alerts(channel, symbol, current_price)


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
                "player_client": ["ios", "default"],
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

    print(f"[yt] start extract: {url}", flush=True)

    def _extract():
        opts = {
            "format": "bestaudio/best",
            "noplaylist": True,
            "quiet": True,
            "nocheckcertificate": True,
            "cachedir": False,
            "force_ipv4": True,
            "socket_timeout": 15,
            "retries": 1,
            "extractor_args": {
                "youtube": {
                    "player_client": ["ios", "default"],
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
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if "entries" in info:
                info = info["entries"][0]
            return {
                "title": info.get("title", "未知音樂"),
                "stream_url": info["url"],
            }

    result = await loop.run_in_executor(None, _extract)
    print(f"[yt] extract done: {result['title']}", flush=True)
    return result


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
        msg = str(e)
        print(f"[yt] extract/play failed: {msg}", flush=True)

        if "Sign in to confirm you’re not a bot" in msg or "Sign in to confirm you're not a bot" in msg:
            await ctx.send("❌ YouTube 目前擋下播放請求，可能是 cookies 過期或雲端 IP 被判定異常。")
        else:
            await ctx.send(f"❌ 取得音訊失敗：\n```\n{msg[:1500]}\n```")

        # 這首失敗時跳下一首，而不是讓整個播放佇列卡住。
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

        if today < GRAD_APP_DATE:
            diff = (GRAD_APP_DATE - today).days
            msg = f"🎓 研究所推甄報名資料提交倒數：還剩 **{diff} 天**！（提交日：9/25）"
        elif today == GRAD_APP_DATE:
            msg = "🎓 今天是研究所推甄報名資料提交日（9/25）！記得確認資料都上傳完成！"
        else:
            days_after = (today - GRAD_APP_DATE).days
            msg = f"🎓 研究所推甄報名資料提交日已過 **{days_after} 天**。"

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
        trade_bridge.start()
        task_started = True


# =========================
# 指令
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


@bot.command(name="grad", aliases=["exam"])
async def grad_countdown(ctx: commands.Context):
    today = datetime.datetime.now(TZ).date()

    if today < GRAD_APP_DATE:
        days = (GRAD_APP_DATE - today).days
        msg = f"🎓 距離研究所推甄報名資料提交日（9/25）還有 **{days} 天**！"
    elif today == GRAD_APP_DATE:
        msg = "🎓 今天是研究所推甄報名資料提交日（9/25）！記得確認資料都上傳完成！"
    else:
        days_after = (today - GRAD_APP_DATE).days
        msg = f"🎓 研究所推甄報名資料提交日已過 **{days_after} 天**。"

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


# =========================
# 自動化抽獎系統
# =========================

# 用來記錄抽獎狀態與名單的變數
is_giveaway_active = False
giveaway_participants = []
giveaway_user_ids = set()


# 1. 開始抽獎指令
@bot.command(name="gstart")
@commands.has_permissions(administrator=True)  # 限制只有管理員能開啟
async def start_giveaway(ctx: commands.Context):
    global is_giveaway_active, giveaway_participants, giveaway_user_ids

    # 啟動抽獎並確保名單是乾淨的
    is_giveaway_active = True
    giveaway_participants.clear()
    giveaway_user_ids.clear()

    await ctx.send(
        "📢 **抽獎已經開始！**\n請在此留言包含「抽」字的內容即可參加，有標示 ⭕ 就算成功囉！\n 歡迎邀請朋友加入本群一起參加抽獎~")


# 2. 監聽留言事件
@bot.listen('on_message')
async def giveaway_listener(message: discord.Message):
    global is_giveaway_active

    # 如果抽獎沒開放，或者是由機器人發出的訊息，直接忽略
    if not is_giveaway_active or message.author.bot:
        return

    # 確認是否在指定的抽獎頻道 (如果有設定的話)
    if GIVEAWAY_CHANNEL_ID and message.channel.id != GIVEAWAY_CHANNEL_ID:
        return

    # 檢查留言是否包含「抽」
    if "抽" in message.content:
        user_id = message.author.id

        # 防呆機制：如果同一個人重複留言，就不會重複加入名單
        if user_id in giveaway_user_ids:
            return

        # 加入抽獎池
        giveaway_user_ids.add(user_id)
        giveaway_participants.append(message.author)

        # 加上 ⭕ 反應，並短暫回覆序號
        try:
            await message.add_reaction("⭕")

            # 傳送短暫的提示訊息告知他是第幾位，3秒後自動刪除，保持版面乾淨
            reply_msg = await message.reply(f"✅ 登記成功！你是第 **{len(giveaway_participants)}** 位參加者。")
            await asyncio.sleep(10)
            await reply_msg.delete()
        except Exception as e:
            print(f"抽獎反應發生錯誤：{e}", flush=True)


# 3. 抽出得獎者並結束抽獎指令
@bot.command(name="roll")
@commands.has_permissions(administrator=True)  # 限制只有管理員能開獎
async def draw_winner(ctx: commands.Context):
    global is_giveaway_active, giveaway_participants, giveaway_user_ids

    if not is_giveaway_active:
        await ctx.send("⚠️ 目前沒有正在進行的抽獎喔！請先使用 `!gstart` 開始抽獎。")
        return

    if not giveaway_participants:
        await ctx.send("❌ 目前沒有任何人參加抽獎喔！")
        # 如果沒人參加但你想關閉抽獎，也可以把下面這行加上去
        # is_giveaway_active = False
        return

    # 隨機抽取一位
    winner = random.choice(giveaway_participants)
    total_participants = len(giveaway_participants)

    await ctx.send(
        f"🎉 **開獎囉！**\n"
        f"恭喜 {winner.mention} 中獎了！ （本次共有 {total_participants} 人參加）\n "

    )

    # 結束抽獎並清空名單
    is_giveaway_active = False
    giveaway_participants.clear()
    giveaway_user_ids.clear()
    await ctx.send("🛑 本次抽獎已結束，名單已自動歸零。")


@bot.command(name="setalert")
async def set_alert(ctx: commands.Context, coin: str, price: float):
    symbol = normalize_coin_symbol(coin)
    if symbol is None:
        await ctx.send("❌ 只支援 BTC / ETH / BNB\n用法：`!setalert btc 70000`")
        return

    if price <= 0:
        await ctx.send("❌ 價格必須大於 0")
        return

    alerts = custom_price_alerts[symbol]

    for alert in alerts:
        if abs(alert["price"] - price) < 1e-9 and not alert["triggered"]:
            await ctx.send(f"⚠️ {symbol} {price:,.2f} 的提醒已經存在了。")
            return

    alerts.append({
        "price": float(price),
        "triggered": False,
        "created_by": ctx.author.id,
    })

    alerts.sort(key=lambda x: x["price"])

    await ctx.send(f"✅ 已設定 {symbol} 價格提醒：{price:,.2f}")


@bot.command(name="alerts")
async def list_alerts(ctx: commands.Context):
    lines = ["📌 目前已設定的價格提醒："]
    has_any = False

    for symbol in ["BTC", "ETH", "BNB"]:
        alerts = custom_price_alerts[symbol]
        active_alerts = [a for a in alerts if not a["triggered"]]

        if active_alerts:
            has_any = True
            lines.append(f"\n{symbol}：")
            for idx, alert in enumerate(active_alerts, start=1):
                lines.append(f"  {idx}. {alert['price']:,.2f}")

    if not has_any:
        await ctx.send("目前沒有任何未觸發的價格提醒。")
        return

    await ctx.send("\n".join(lines))


@bot.command(name="delalert")
async def delete_alert(ctx: commands.Context, coin: str, price: float):
    symbol = normalize_coin_symbol(coin)
    if symbol is None:
        await ctx.send("❌ 只支援 BTC / ETH / BNB\n用法：`!delalert btc 70000`")
        return

    alerts = custom_price_alerts[symbol]

    for i, alert in enumerate(alerts):
        if abs(alert["price"] - price) < 1e-9 and not alert["triggered"]:
            alerts.pop(i)
            await ctx.send(f"🗑️ 已刪除 {symbol} 價格提醒：{price:,.2f}")
            return

    await ctx.send(f"❌ 找不到 {symbol} {price:,.2f} 的未觸發提醒。")


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


# =========================
# 記帳指令
# =========================

ACCOUNT_USAGE_TEXT = "帳戶只支援 `suyu` / `gary` / `win`。用法：`!income suyu 1000 打工薪水`"


def format_account_record_line(record: dict) -> str:
    record_type = record.get("type")
    if record_type == "income":
        type_text = "收入"
        sign = "+"
    elif record_type == "expense":
        type_text = "支出"
        sign = "-"
    else:
        type_text = "設定餘額"
        sign = ""

    operator_name = record.get("operator_name", "未知操作者")
    return (
        f"{record.get('time', '')}｜{type_text} {sign}{fmt_money(float(record.get('amount', 0)))}｜"
        f"{record.get('reason', '未填寫')}｜餘額 {fmt_money(float(record.get('balance_after', 0)))}｜操作：{operator_name}"
    )


@bot.command(name="income")
async def add_income(ctx: commands.Context, account_name: str, amount: float, *, reason: str = ""):
    account_key = normalize_account_name(account_name)
    if account_key is None:
        await ctx.send(f"❌ {ACCOUNT_USAGE_TEXT}")
        return

    reason = reason.strip() or "未填寫"
    if amount <= 0:
        await ctx.send("❌ 收入金額必須大於 0。用法：`!income suyu 1000 打工薪水`")
        return

    record, balance = add_accounting_record(account_key, "income", amount, reason, ctx.author)
    await ctx.send(
        f"✅ 已記錄 **{account_key}** 收入：+{fmt_money(amount)}\n"
        f"事由：{record['reason']}\n"
        f"{account_key} 目前餘額：{fmt_money(balance)}"
    )


@bot.command(name="expense")
async def add_expense(ctx: commands.Context, account_name: str, amount: float, *, reason: str = ""):
    account_key = normalize_account_name(account_name)
    if account_key is None:
        await ctx.send(f"❌ {ACCOUNT_USAGE_TEXT}")
        return

    reason = reason.strip() or "未填寫"
    if amount <= 0:
        await ctx.send("❌ 支出金額必須大於 0。用法：`!expense gary 120 午餐`")
        return

    record, balance = add_accounting_record(account_key, "expense", amount, reason, ctx.author)
    await ctx.send(
        f"✅ 已記錄 **{account_key}** 支出：-{fmt_money(amount)}\n"
        f"事由：{record['reason']}\n"
        f"{account_key} 目前餘額：{fmt_money(balance)}"
    )


@bot.command(name="setbalance")
async def set_balance(ctx: commands.Context, account_name: str, amount: float):
    account_key = normalize_account_name(account_name)
    if account_key is None:
        await ctx.send("❌ 帳戶只支援 `suyu` / `gary` / `win`。用法：`!setbalance suyu 5000`")
        return

    account = get_account(account_key)
    account["balance"] = float(amount)
    account["records"].append({
        "type": "setbalance",
        "amount": float(amount),
        "reason": "手動設定餘額",
        "time": datetime.datetime.now(TZ).isoformat(timespec="minutes"),
        "balance_after": float(amount),
        "operator_id": ctx.author.id,
        "operator_name": str(ctx.author),
    })
    account["records"] = account["records"][-200:]
    save_accounting_data()
    await ctx.send(f"✅ 已手動設定 **{account_key}** 餘額為：{fmt_money(amount)}")


@bot.command(name="balance")
async def show_balance(ctx: commands.Context, account_name: str = "all"):
    if account_name.lower() in {"all", "全部"}:
        lines = ["💰 目前餘額："]
        for account_key in ACCOUNTING_ACCOUNTS:
            account = get_account(account_key)
            lines.append(f"{account_key}：{fmt_money(float(account['balance']))}")
        await ctx.send("\n".join(lines))
        return

    account_key = normalize_account_name(account_name)
    if account_key is None:
        await ctx.send("❌ 帳戶只支援 `suyu` / `gary` / `win`。用法：`!balance suyu`、`!balance win` 或 `!balance all`")
        return

    account = get_account(account_key)
    await ctx.send(f"💰 **{account_key}** 目前餘額：{fmt_money(float(account['balance']))}")


@bot.command(name="records")
async def show_records(ctx: commands.Context, account_name: str = "all", count: int = 5):
    count = max(1, min(count, 10))

    if account_name.lower() in {"all", "全部"}:
        lines = [f"📒 最近記帳紀錄（每人最多 {count} 筆）："]
        for account_key in ACCOUNTING_ACCOUNTS:
            account = get_account(account_key)
            records = account["records"][-count:]
            lines.append(f"\n【{account_key}】")
            if not records:
                lines.append("目前沒有記帳紀錄。")
                continue
            for record in reversed(records):
                lines.append(format_account_record_line(record))
        await ctx.send("\n".join(lines))
        return

    account_key = normalize_account_name(account_name)
    if account_key is None:
        await ctx.send("❌ 帳戶只支援 `suyu` / `gary` / `win`。用法：`!records suyu 5`、`!records win 5` 或 `!records all 5`")
        return

    account = get_account(account_key)
    records = account["records"][-count:]

    if not records:
        await ctx.send(f"**{account_key}** 目前沒有記帳紀錄。")
        return

    lines = [f"📒 **{account_key}** 最近 {len(records)} 筆記帳紀錄："]
    for record in reversed(records):
        lines.append(format_account_record_line(record))

    await ctx.send("\n".join(lines))



# =========================
# Forward Paper 交易控制指令
# =========================

def _is_admin(ctx: commands.Context) -> bool:
    return bool(
        ctx.guild
        and isinstance(ctx.author, discord.Member)
        and ctx.author.guild_permissions.administrator
    )


@bot.command(name="trade")
async def trade_control(ctx: commands.Context, action: str = "status"):
    """
    !trade status
    !trade stop
    !trade start
    !trade test
    """
    action = action.strip().lower()

    if action in {"status", "狀態"}:
        await ctx.send(await trade_bridge.status_text())
        return

    if action in {"stop", "pause", "停止", "暫停"}:
        if not _is_admin(ctx):
            await ctx.send("❌ 只有管理員可以暫停交易系統新增風險。")
            return

        await ctx.send(await trade_bridge.pause_new_risk())
        return

    if action in {"start", "resume", "開始", "恢復"}:
        if not _is_admin(ctx):
            await ctx.send("❌ 只有管理員可以恢復交易系統新增風險。")
            return

        await ctx.send(await trade_bridge.resume_new_risk())
        return

    if action in {"test", "測試"}:
        if not _is_admin(ctx):
            await ctx.send("❌ 只有管理員可以執行交易通報測試。")
            return

        await ctx.send(await trade_bridge.send_test_notification())
        return

    await ctx.send(
        "用法：`!trade status`、`!trade stop`、`!trade start`、`!trade test`"
    )


@bot.command(name="start")
async def start_command(ctx: commands.Context, mode: str = ""):
    """
    保留 !start trade 作為 !stop trade 的對稱指令。
    """
    if mode.strip().lower() != "trade":
        await ctx.send("用法：`!start trade`")
        return

    if not _is_admin(ctx):
        await ctx.send("❌ 只有管理員可以恢復交易系統新增風險。")
        return

    await ctx.send(await trade_bridge.resume_new_risk())


@bot.command(name="help")
async def custom_help(ctx: commands.Context):
    msg = (
        "!後：\n"
        "  help  顯示所有可用功能指令\n"
        "  grad  顯示研究所推甄報名資料提交倒數（exam 也可用）\n"
        "  price  顯示 BTC / ETH / BNB 目前價格\n"
        "  dailytest  測試每日幣圈摘要（管理員）\n"
        "  setalert <幣種> <價格>  設定價格提醒\n"
        "  alerts  查看目前未觸發的價格提醒\n"
        "  delalert <幣種> <價格>  刪除價格提醒\n\n"
        "【交易系統（TRADE_MODE=paper / live）】\n"
        "  trade status  查看目前交易策略 / 真實帳戶狀態\n"
        "  trade stop  暫停 OPEN / ADD（現有倉仍持續執行風險管理；管理員）\n"
        "  trade start  恢復 OPEN / ADD（管理員；LIVE 時會恢復真實新增風險）\n"
        "  trade test  測試交易頻道通報（管理員）\n"
        "  stop trade / start trade  可作為上述 stop/start 的快捷指令\n"
        "  每天 20:00 自動發送交易摘要\n  ⚠️ TRADE_MODE=live 時 OPEN / ADD / TP / EXIT / Hedge 皆為真實 Binance Futures 訂單\n\n"
        "  income <suyu/gary/win> <金額> <事由>  新增收入，例如：!income win 1000 打工薪水\n"
        "  expense <suyu/gary/win> <金額> <事由>  新增支出，例如：!expense win 120 午餐\n"
        "  setbalance <suyu/gary/win> <金額>  手動設定餘額，例如：!setbalance win 5000\n"
        "  balance [suyu/gary/win/all]  查看餘額，例如：!balance all\n"
        "  records [suyu/gary/win/all] [數量]  查看最近記帳紀錄，最多 10 筆，例如：!records win 5\n\n"
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
        "【自動提醒】\n\n"
        "  BTC / ETH / BNB：1 小時內漲跌超過 3% 會 @everyone 提醒\n"
        "  通知後 1 小時內，只有相較上次通知價格又漲跌 3% 才會再次提醒\n"
        "  自訂價格提醒觸發時會 @everyone\n"
        "  每天 19:00 自動發送每日幣圈摘要與 2 則重點新聞\n\n"

        "🎁 抽獎系統\n "
        " !gstart 開啟抽獎並清空舊名單\n"
        " !roll  從留言「抽」的人中隨機抽出一名幸運兒並結束抽獎\n"
        " !gclear  - 手動清空目前的抽獎名單"

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
            await ctx.voice_client.move_to(channel)
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
async def stop_audio(ctx: commands.Context, mode: str = ""):
    global music_queue, is_playing

    # !stop trade：暫停「新增風險」，但不凍結既有持倉的 TP / Stop / Funding。
    if mode.strip().lower() == "trade":
        if not _is_admin(ctx):
            await ctx.send("❌ 只有管理員可以暫停交易系統新增風險。")
            return

        await ctx.send(await trade_bridge.pause_new_risk())
        return

    # 原本的 !stop：停止音樂。
    if mode.strip():
        await ctx.send("音樂停止請用 `!stop`；交易暫停請用 `!stop trade`。")
        return

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