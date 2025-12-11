import discord
from discord.ext import commands
import asyncio
import datetime
import os
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
import yt_dlp

async def play_next(ctx):
    global is_playing

    if len(music_queue) == 0:
        is_playing = False
        return

    is_playing = True
    next_song = music_queue.pop(0)  # 取下一首
    source = next_song["source"]
    title = next_song["title"]

    voice_client = ctx.voice_client

    def after_playing(error):
        if error:
            print(f"播放發生錯誤：{error}")
        asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)

    voice_client.play(source, after=after_playing)
    await ctx.send(f"▶ 正在播放：**{title}**")

YDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

# 讀取 .env（本機用；Railway 會用環境變數）
load_dotenv()
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
EXAM_START = datetime.date(2026, 1, 5)
EXAM_END = datetime.date(2026, 1, 10)

# Intents（要可讀取訊息內容才能用指令）
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

music_queue = []   # 儲存 { 'source': audio_source, 'title': 標題 } 的列表
is_playing = False

task_started = False


async def countdown_task():
    await bot.wait_until_ready()
    channel = bot.get_channel(CHANNEL_ID)

    if channel is None:
        print("找不到頻道，請確認 CHANNEL_ID 是否正確！")
        return

    print("倒數排程啟動…（時區：Asia/Taipei）")

    while not bot.is_closed():
        now = datetime.datetime.now(TZ)
        today_send = now.replace(
            hour=SEND_HOUR, minute=SEND_MINUTE, second=0, microsecond=0
        )

        # 決定下一次發訊息時間（今天或明天）
        if now >= today_send:
            next_send = today_send + datetime.timedelta(days=1)
        else:
            next_send = today_send

        wait_seconds = (next_send - now).total_seconds()
        print(f"下一次發訊息時間（Asia/Taipei）：{next_send}（等待 {wait_seconds:.0f} 秒）")
        await asyncio.sleep(wait_seconds)

        # 重新取台北時間避免跨日問題
        now = datetime.datetime.now(TZ)
        today = now.date()
        diff = (EXAM_END - today).days

        # 訊息邏輯
        if today == EXAM_START:
            msg = "(1/05) 今天是期末考第一天！Fight！！"
        elif EXAM_START < today < EXAM_END:
            msg = f"({today.month}/{today.day}) 期末考進行中！加油！！"
        elif today == EXAM_END:
            msg = "(1/10) 今天是期末考的最後一天！（2026-01-10）加油！"
        elif today > EXAM_END:
            msg = f"📘 期末考已經結束 {abs(diff)} 天，辛苦了～"
        else:
            msg = f"📘 期末考倒數：還剩 **{diff} 天**！（結束日：{EXAM_END}）"

        await channel.send(msg)


@bot.event
async def on_ready():
    global task_started
    print(f"Bot 已登入：{bot.user}")
    if not task_started:
        asyncio.create_task(countdown_task())
        task_started = True


# =========================
#  !join：讓 Bot 進語音
# =========================
@bot.command(name="join")
async def join_voice(ctx: commands.Context):
    """使用者所在的語音頻道，讓 Bot 自動加入"""
    voice_state = ctx.author.voice

    if voice_state is None or voice_state.channel is None:
        await ctx.send("要先進入一個語音頻道，我才能跟上去唷！")
        return

    channel = voice_state.channel

    # 如果已經在某個語音頻道
    if ctx.voice_client is not None:
        if ctx.voice_client.channel.id == channel.id:
            await ctx.send("我已經在這個語音頻道裡啦！")
            return
        # 移動到新的語音頻道
        await ctx.voice_client.move_to(channel)
        await ctx.send(f"跟隨你到：{channel.name} 頻道囉~")
    else:
        # 尚未連接任何語音頻道 → 加入
        await channel.connect()
        await ctx.send(f"我已經加入：{channel.name} 頻道陪你囉~")


# =========================
#  !bye：離開語音
# =========================
@bot.command(name="bye")
async def leave_voice(ctx: commands.Context):
    voice_client = ctx.voice_client

    if voice_client is None:
        await ctx.send("我現在沒有在任何語音頻道裡唷！")
        return

    await voice_client.disconnect()
    await ctx.send("下次歡迎再來找我唷~")


# =========================
#  !clear：清除訊息
# =========================
@bot.command(name="clear")
@commands.has_permissions(manage_messages=True)
async def clear_messages(ctx: commands.Context, amount: int):
    """
    清除當前頻道最近 amount 則訊息（包含這次指令）
    用法：!clear (數字)
    """
    if amount <= 0:
        await ctx.send("請輸入大於 0 的數量喔！")
        return

    # +1 是把這次 !clear 指令本身也一起刪掉
    deleted = await ctx.channel.purge(limit=amount + 1)
    count = len(deleted) - 1  # 扣掉指令那一則
    msg = await ctx.send(f"🧹 已清除 {count} 則訊息")
    await asyncio.sleep(3)
    await msg.delete()


@clear_messages.error
async def clear_messages_error(ctx: commands.Context, error):
    # 沒權限
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("你沒有**管理訊息**的權限，不能使用這個指令！")
    else:
        print(f"clear 指令錯誤：{error}")



# =========================
#  !play：播放上傳的 mp3 檔（加強版，會顯示錯誤）
# =========================
@bot.command(name="play")
async def play_audio(ctx: commands.Context):
    """
    播放使用者這則訊息附帶的 mp3 檔
    用法：在文字頻道傳送訊息時附上 mp3 檔，並輸入：!play
    """

    # 1. 確認使用者有在語音頻道
    voice_state = ctx.author.voice
    if voice_state is None or voice_state.channel is None:
        await ctx.send("你要先進入一個語音頻道，我才能幫你播音樂唷！")
        return

    # 2. 讓 Bot 加入或移動到使用者的語音頻道
    voice_client = ctx.voice_client
    channel = voice_state.channel

    if voice_client is None:
        voice_client = await channel.connect()
        await ctx.send(f"我已經加入：{channel.name} 頻道囉，準備幫你播音樂～")
    else:
        if voice_client.channel.id != channel.id:
            await voice_client.move_to(channel)
            await ctx.send(f"我換到：{channel.name} 頻道囉～")

    # 3. 檢查這則訊息有沒有附檔
    if not ctx.message.attachments:
        await ctx.send("請把 mp3 檔案當作**附件**一起傳給我，再使用 `!play` 喔～")
        return

    attachment = ctx.message.attachments[0]

    # 只接受 mp3
    if not attachment.filename.lower().endswith(".mp3"):
        await ctx.send("目前我只支援 `.mp3` 檔案喔 QQ")
        return

    # 4. 把 mp3 存成暫存檔
    temp_filename = f"temp_{attachment.id}.mp3"
    await attachment.save(temp_filename)
    await ctx.send(f"收到檔案 `{attachment.filename}`，準備播放～")

    # 5. 如果正在播東西，先停掉
    if voice_client.is_playing():
        voice_client.stop()

    # 6. 使用 FFmpeg 播放，並加上錯誤處理
    def after_playing(error):
        # 播放結束後刪掉暫存檔
        try:
            if os.path.exists(temp_filename):
                os.remove(temp_filename)
        except Exception as e:
            print(f"刪除暫存檔失敗：{e}")

        if error:
            print(f"播放時發生錯誤：{error}")

    try:
        # 這裡如果 ffmpeg 沒裝好 / lib 有問題，會直接丟例外
        audio_source = discord.FFmpegPCMAudio(temp_filename)
        # 可選：如果覺得音量太小，可以包一層音量控制
        # from discord import PCMVolumeTransformer
        # audio_source = PCMVolumeTransformer(audio_source, volume=1.0)

        voice_client.play(audio_source, after=after_playing)
        await ctx.send("我開始演奏囉！")
    except Exception as e:
        # 關鍵：把錯誤丟回 DC，方便你看到
        await ctx.send(f"播放時發生錯誤：`{e}`\n（也可以去 Railway Logs 看更詳細的訊息）")
        # 同時在主機 log 印出詳細內容
        import traceback
        traceback.print_exc()

# =========================
#  !yt：播放 YouTube 連結的音樂
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
        voice_client = await channel.connect()

    elif voice_client.channel.id != channel.id:
        await voice_client.move_to(channel)

    await ctx.send("🔎 正在從 YouTube 取得音訊串流…")

    loop = asyncio.get_running_loop()

    def ytdlp_extract():
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        info = await loop.run_in_executor(None, ytdlp_extract)
    except Exception as e:
        await ctx.send(f"❌ 發生錯誤：`{e}`")
        return

    if "entries" in info:
        info = info["entries"][0]

    stream_url = info["url"]
    title = info.get("title", "未知音樂")

    audio_source = discord.FFmpegPCMAudio(stream_url, **FFMPEG_OPTIONS)

    # 加入 queue
    music_queue.append({"source": audio_source, "title": title})
    await ctx.send(f"🎵 已加入播放清單：**{title}**")

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

    voice_client.stop()  # after_playing() 會自動播放下一首
    await ctx.send("⏭ 已跳到下一首！")




bot.run(TOKEN)
