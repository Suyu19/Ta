
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

print("BOOT VERSION: 2026-02-21-ytcookies-debug-1", flush=True)

# =========================
# 基本設定
# =========================

# 讀取 .env（本機用；Railway 會用環境變數）
load_dotenv()

print("YT_COOKIES_B64 exists:", os.getenv("YT_COOKIES_B64") is not None, flush=True)
if os.getenv("YT_COOKIES_B64"):
    print("YT_COOKIES_B64 length:", len(os.getenv("YT_COOKIES_B64")), flush=True)

print("YT_COOKIES_B64 exists:", os.getenv("YT_COOKIES_B64") is not None)
if os.getenv("YT_COOKIES_B64"):
    print("YT_COOKIES_B64 length:", len(os.getenv("YT_COOKIES_B64")))
TOKEN = os.getenv("DISCORD_TOKEN")

CHANNEL_ID_STR = os.getenv("CHANNEL_ID")
SEND_HOUR = int(os.getenv("SEND_HOUR", "20"))     # 預設 20:00
SEND_MINUTE = int(os.getenv("SEND_MINUTE", "0"))  # 預設 00 分

if CHANNEL_ID_STR is None:
    raise RuntimeError("CHANNEL_ID 環境變數沒有設定！")
CHANNEL_ID = int(CHANNEL_ID_STR)

# 使用 Asia/Taipei 時區
TZ = ZoneInfo("Asia/Taipei")

# 期末考期間
EXAM_START = datetime.date(2026, 1, 5)  # 考試第一天
EXAM_END   = datetime.date(2026, 1, 9)  # 考試最後一天

# Intents（要可讀取訊息內容才能用指令）
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# 音樂狀態
music_queue = []   # 存 {"type": "yt", "url": "..."} 或 {"type": "file", "path": "...", "title": "..."}
is_playing = False
task_started = False


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
        print("[yt] YT_COOKIES_B64 not set")
        return None

    path = os.path.join(tempfile.gettempdir(), "yt_cookies.txt")
    try:
        # 每次啟動都覆蓋寫入，避免舊檔壞掉或寫到一半
        with open(path, "wb") as f:
            f.write(base64.b64decode(b64))
        print(f"[yt] cookies written: {path} ({os.path.getsize(path)} bytes)")
        return path
    except Exception as e:
        print(f"[yt] cookies decode/write failed: {e}")
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

    # ✅ cookies：沒有就很容易被擋
    if cookies_path:
        opts["cookiefile"] = cookies_path
    else:
        # 這行讓你在 Railway logs 一眼看懂：cookies 根本沒吃到
        print("[yt] WARNING: cookiefile not available -> likely to get 'not a bot' error")

    # ✅ JS runtime：你 log 說找不到，所以我們會靠 Dockerfile 裝 node
    # yt-dlp 通常會自動偵測 node/deno；不用強塞參數也行（先裝起來最重要）

    return opts

    if cookies_path:
        opts["cookiefile"] = cookies_path

    return opts


async def get_stream_info(url: str):
    """
    播放前才去抓最新的 stream_url，避免排隊時 URL 過期。
    """
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

    # 如果突然不在語音了
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
        # 失敗就繼續下一首，避免卡住
        asyncio.create_task(play_next(ctx))
        return

    def after_playing(error):
        if error:
            print(f"播放發生錯誤：{error}")

        # 如果是檔案播放，播完刪掉暫存
        if item["type"] == "file":
            try:
                p = item["path"]
                if os.path.exists(p):
                    os.remove(p)
            except Exception as ex:
                print(f"刪除暫存檔失敗：{ex}")

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
        print("找不到頻道，請確認 CHANNEL_ID 是否正確！")
        return

    print("倒數排程啟動…（時區：Asia/Taipei）")

    while not bot.is_closed():
        now = datetime.datetime.now(TZ)
        today_send = now.replace(hour=SEND_HOUR, minute=SEND_MINUTE, second=0, microsecond=0)

        if now >= today_send:
            next_send = today_send + datetime.timedelta(days=1)
        else:
            next_send = today_send

        wait_seconds = (next_send - now).total_seconds()
        print(f"下一次發訊息時間（Asia/Taipei）：{next_send}（等待 {wait_seconds:.0f} 秒）")
        await asyncio.sleep(wait_seconds)

        now = datetime.datetime.now(TZ)
        today = now.date()

        if today == EXAM_START:
            msg = "(1/05) 今天是期末考第一天！Fight！！💪📚"
        elif EXAM_START < today < EXAM_END:
            msg = f"({today.month}/{today.day}) 期末考進行中！加油！！🔥"
        elif today == EXAM_END:
            msg = "(1/09) 今天是期末考最後一天！撐住！！🎯"
        elif today > EXAM_END:
            days_after = (today - EXAM_END).days
            msg = f"📘 期末考已經結束 {days_after} 天，辛苦了～🎉"
        else:
            diff = (EXAM_START - today).days
            msg = f"📘 期末考倒數：還剩 **{diff} 天**！（考試第一天：1/05）"

        await channel.send(msg)


@bot.event
async def on_ready():
    global task_started
    print(f"Bot 已登入：{bot.user}")
    if not task_started:
        asyncio.create_task(countdown_task())
        task_started = True


# =========================
# 指令：exam / help
# =========================

@bot.command(name="exam")
async def exam_countdown(ctx: commands.Context):
    today = datetime.date.today()

    if today < EXAM_START:
        days = (EXAM_START - today).days
        msg = f"📘 距離期末考第一天（1/05）還有 **{days} 天**！"
    elif today == EXAM_START:
        msg = "📘 今天是期末考第一天（1/05）！Fight！！🔥"
    elif EXAM_START < today < EXAM_END:
        day_no = (today - EXAM_START).days + 1
        left = (EXAM_END - today).days
        msg = f"📘 期末考進行中（第 **{day_no} 天**）！\n⏳ 距離最後一天（1/09）還有 **{left} 天**"
    elif today == EXAM_END:
        msg = "📘 今天是期末考最後一天（1/09） 解脫了！"
    else:
        days_after = (today - EXAM_END).days
        msg = f"🎉 期末考已結束 **{days_after} 天**，辛苦了～"

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
        print(f"clear 指令錯誤：{error}")


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

    # queue 存檔案
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