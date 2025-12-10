import discord
from discord.ext import commands
import asyncio
import datetime
import os
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
import yt_dlp
from discord import FFmpegPCMAudio

# 讀取 .env（本機用；Railway 會用環境變數）
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

CHANNEL_ID_STR = os.getenv("CHANNEL_ID")
SEND_HOUR = int(os.getenv("SEND_HOUR", "20"))
SEND_MINUTE = int(os.getenv("SEND_MINUTE", "0"))

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

task_started = False

# yt-dlp / ffmpeg 設定（目前沒用到，但保留也沒關係）
YDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
}
FFMPEG_OPTS = {
    "before_options": "-nostdin",
    "options": "-vn",
}


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

        if now >= today_send:
            next_send = today_send + datetime.timedelta(days=1)
        else:
            next_send = today_send
        next_send = today_send

        wait_seconds = (next_send - now).total_seconds()
        print(f"下一次發訊息時間（Asia/Taipei）：{next_send}（等待 {wait_seconds:.0f} 秒）")
        await asyncio.sleep(wait_seconds)

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
#  指令：!join 讓 Bot 進語音
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


# ==========================================
#  !bye 指令：離開語音頻道
# ==========================================
@bot.command(name="bye")
async def leave_voice(ctx: commands.Context):
    voice_client = ctx.voice_client

    if voice_client is None:
        await ctx.send("我現在沒有在任何語音頻道裡唷！")
        return

    await voice_client.disconnect()
    await ctx.send("下次歡迎再來找我唷~")


# ==========================================
#  !clear 指令：清除訊息
# ==========================================
@bot.command(name="clear")
@commands.has_permissions(manage_messages=True)
async def clear_messages(ctx: commands.Context, amount: int):
    """
    清除當前頻道最近 amount 則訊息（包含這次指令）
    用法：!clear 10
    """
    if amount <= 0:
        await ctx.send("請輸入大於 0 的數量喔！")
        return

    # 多 +1 是把這次 !clear 指令本身也一起刪掉
    deleted = await ctx.channel.purge(limit=amount + 1)
    count = len(deleted) - 1  # 扣掉指令那一則
    msg = await ctx.send(f"🧹 已清除 {count} 則訊息")
    # 幾秒後自動把這則提示刪掉，避免又堆訊息
    await asyncio.sleep(3)
    await msg.delete()


@clear_messages.error
async def clear_messages_error(ctx: commands.Context, error):
    # 沒權限時的提示
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("你沒有管理訊息的權限，不能使用這個指令喔！")
    else:
        # 其他錯誤就印在 console，方便 debug
        print(f"clear 指令錯誤：{error}")


bot.run(TOKEN)
