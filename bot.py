import discord
from discord.ext import commands
import asyncio
import datetime
import os
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
import yt_dlp
import base64
import tempfile

print("BOOT VERSION: 2026-03-01-sleepcheck-1", flush=True)

# =========================
# 基本設定
# =========================

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

CHANNEL_ID_STR = os.getenv("CHANNEL_ID")
SEND_HOUR = int(os.getenv("SEND_HOUR", "20"))     # 預設 20:00
SEND_MINUTE = int(os.getenv("SEND_MINUTE", "0"))  # 預設 00 分

# ✅ 新增：睡覺提醒頻道
SLEEP_CHANNEL_ID_STR = os.getenv("SLEEP_CHANNEL_ID")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN 環境變數沒有設定！")

if CHANNEL_ID_STR is None:
    raise RuntimeError("CHANNEL_ID 環境變數沒有設定！")
CHANNEL_ID = int(CHANNEL_ID_STR)

if SLEEP_CHANNEL_ID_STR is None:
    raise RuntimeError("SLEEP_CHANNEL_ID 環境變數沒有設定！（睡覺提醒用的新文字頻道 ID）")
SLEEP_CHANNEL_ID = int(SLEEP_CHANNEL_ID_STR)

# 使用 Asia/Taipei 時區
TZ = ZoneInfo("Asia/Taipei")

# 期中考期間
EXAM_START = datetime.date(2026, 4, 20)  # 考試第一天
EXAM_END   = datetime.date(2026, 4, 24)  # 考試最後一天

# Intents（要可讀取訊息內容才能用指令）
intents = discord.Intents.default()
intents.message_content = True
# ✅ 需要抓成員名單來 tag 未回報者（請同時去 Developer Portal 開啟 SERVER MEMBERS INTENT）
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# 音樂狀態
music_queue = []   # 存 {"type": "yt", "url": "..."} 或 {"type": "file", "path": "...", "title": "..."}
is_playing = False

task_started = False  # 用來避免 on_ready 重複啟動 task

# =========================
# Sleep Check 狀態（不落地保存）
# =========================
sleep_today: datetime.date | None = None
sleep_message_id: int | None = None
sleep_responded_users: set[int] = set()


def _sleep_label_time(dt: datetime.datetime) -> str:
    # 顯示用：x月x日的凌晨2:00
    return f"{dt.month}月{dt.day}日的凌晨 2:00"


def _allowed_mentions_all():
    # 允許 @everyone + tag 使用者
    return discord.AllowedMentions(everyone=True, users=True, roles=False)


# =========================
# FFmpeg / yt-dlp 設定
# =========================

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

from typing import Optional

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
        # 先回應避免互動超時（ephemeral）
        await interaction.response.send_message("已記錄 ✅", ephemeral=True)

        # 公開回覆
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

        # 先 defer，避免 interaction failed
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

        # 開 modal，原因必填
        await interaction.response.send_modal(NotSleepModal(self.channel))


# =========================
# Sleep Check 排程：02:00 發 + 02:30 檢查 tag
# =========================

async def run_sleep_check_now(channel: discord.TextChannel):
    """立刻執行一次 02:30 檢查：@everyone + tag 未回報者"""
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
    """
    每天 02:00 發睡覺提醒（含按鈕）
    每天 02:30 檢查未回報者並 tag + @everyone
    不保存資料：只用記憶體 set 記今天按過的人
    """
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

        # 今天 02:00
        send_dt = now.replace(hour=2, minute=0, second=0, microsecond=0)
        # 今天 02:30
        check_dt = now.replace(hour=2, minute=30, second=0, microsecond=0)

        # 如果現在已經過了 02:30，代表今天的流程已過，排到明天
        if now >= check_dt:
            send_dt = send_dt + datetime.timedelta(days=1)
            check_dt = check_dt + datetime.timedelta(days=1)
        # 若過了 02:00 但還沒到 02:30：今天不再發提醒（避免重啟後補發），只跑 02:30 檢查
        elif now >= send_dt:
            # 不改 send_dt（保持今天），但我們會判斷是否已經發過
            pass

        # ---------- 02:00 發提醒 ----------
        # sleep_today 用來避免重複發（例如 bot 重啟 / on_ready 多次）
        # 規則：只有當 now < 02:00 時才會等待到 02:00；如果 now 在 02:00~02:30 之間，會嘗試「若今天未發過」才補發。
        if sleep_today != send_dt.date():
            # 等到 send_dt
            wait_send = (send_dt - datetime.datetime.now(TZ)).total_seconds()
            if wait_send > 0:
                await asyncio.sleep(wait_send)

            # 發提醒前，再更新一次 now
            now2 = datetime.datetime.now(TZ)
            today = now2.date()

            # 重置今日狀態
            sleep_today = today
            sleep_message_id = None
            sleep_responded_users = set()

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

        # ---------- 等到 02:30 檢查 ----------
        wait_check = (check_dt - datetime.datetime.now(TZ)).total_seconds()
        if wait_check > 0:
            await asyncio.sleep(wait_check)

        # 檢查當下仍是同一天的流程（避免跨天 race）
        now3 = datetime.datetime.now(TZ)
        if sleep_today != now3.date():
            # 代表今天沒有正常發出/被重置，直接進下一輪
            continue

        # 抓成員名單，找出未回報者
        guild = channel.guild

        # 取得 guild 成員（members intent 開啟會更完整）
        members: list[discord.Member] = []
        try:
            # 如果快取有，就用快取；不夠完整也沒關係（你不存資料的前提下，寧可少 tag）
            members = [m for m in guild.members]
            if len(members) == 0:
                # 嘗試用 fetch_members 補
                async for m in guild.fetch_members(limit=None):
                    members.append(m)
        except Exception as e:
            print(f"[sleep] 取得成員名單失敗：{e}", flush=True)

        # 過濾：不 tag bot / system
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
            # 先 @everyone（你指定要全體）
            await channel.send(
                "@everyone ⏰ 02:30 了！還沒回報的人請趕快按上方按鈕回報～",
                allowed_mentions=_allowed_mentions_all(),
            )

            # 再分批 tag 未回報者（避免 2000 字爆掉）
            chunk = []
            current_len = 0
            for m in targets:
                mention = m.mention
                # +1 是空格
                add_len = len(mention) + 1
                if current_len + add_len > 1800:  # 留一點安全空間
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

        # 進入下一輪（明天）
        # sleep_today 會在下一輪 02:00 重置，不用特別清


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


@bot.event
async def on_ready():
    global task_started
    print(f"Bot 已登入：{bot.user}", flush=True)
    if not task_started:
        asyncio.create_task(countdown_task())
        asyncio.create_task(sleep_check_task())
        task_started = True


# =========================
# 指令：exam / help / sleeptest / sleepcheck
# =========================

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


@bot.command(name="help")
async def custom_help(ctx: commands.Context):
    msg = (
        "!後：\n"
        "  help  顯示所有可用功能指令\n"
        "  join   加入語音頻道陪你\n"
        "  bye   離開語音頻道\n\n"
        "  clear （數字） 清除當前頻道最近 X 則訊息\n\n"
        "  play  播放這則訊息附帶的 mp3 檔\n"
        "  yt      後接網址播放音樂\n"
        "  skip  跳到清單下一首\n"
        "  stop  停止所有音樂播放\n\n"
        "  sleeptest   立刻發出睡覺回報按鈕（測試）\n"
        "  sleepcheck  立刻做一次未回報檢查（測試）"
    )
    await ctx.send(msg)


@bot.command(name="sleeptest")
@commands.has_permissions(administrator=True)
async def sleep_test(ctx: commands.Context):
    """立刻在睡覺頻道發出提醒（含按鈕），並重置今日回報狀態"""
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
    """立刻做一次 02:30 檢查（@everyone + tag 未回報者）"""
    channel = bot.get_channel(SLEEP_CHANNEL_ID)
    if channel is None:
        channel = await bot.fetch_channel(SLEEP_CHANNEL_ID)

    if not isinstance(channel, discord.TextChannel):
        await ctx.send("❌ SLEEP_CHANNEL_ID 不是文字頻道，請檢查設定。")
        return

    await run_sleep_check_now(channel)
    await ctx.send("✅ 已執行一次測試檢查（請看睡覺頻道）。")

async def custom_help(ctx: commands.Context):
    msg = (
        "!後：\n"
        "  help  顯示所有可用功能指令\n"
        "  join   加入語音頻道陪你\n"
        "  bye   離開語音頻道\n\n"
        "  clear （數字） 清除當前頻道最近 X 則訊息\n\n"
        "  play  播放這則訊息附帶的 mp3 檔\n"
        "  yt      後接網址播放音樂\n"
        "  skip  跳到清單下一首\n"
        "  stop  停止所有音樂播放"
    )
    await ctx.send(msg)


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