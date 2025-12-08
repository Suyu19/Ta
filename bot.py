import discord
import asyncio
import datetime
import os
from zoneinfo import ZoneInfo  # Python 3.9+ 內建
from dotenv import load_dotenv

# 讀取 .env 設定（本機用；Railway 上會用環境變數）
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# 這裡只讀字串，稍後再轉 int
CHANNEL_ID_STR = os.getenv("CHANNEL_ID")
SEND_HOUR = int(os.getenv("SEND_HOUR", "20"))     # 預設 20:00
SEND_MINUTE = int(os.getenv("SEND_MINUTE", "0"))  # 預設 00 分

# 確認 CHANNEL_ID 有設到
if CHANNEL_ID_STR is None:
    raise RuntimeError("CHANNEL_ID 環境變數沒有設定！")

CHANNEL_ID = int(CHANNEL_ID_STR)

# 使用 Asia/Taipei 時區
TZ = ZoneInfo("Asia/Taipei")

# 期末考期間
EXAM_START = datetime.date(2026, 1, 5)
EXAM_END = datetime.date(2026, 1, 10)

intents = discord.Intents.default()
client = discord.Client(intents=intents)

task_started = False


async def countdown_task():
    await client.wait_until_ready()
    channel = client.get_channel(CHANNEL_ID)

    if channel is None:
        print("找不到頻道，請確認 CHANNEL_ID 是否正確！")
        return

    print("倒數排程啟動…（時區：Asia/Taipei）")

    while not client.is_closed():
        # 取得「台北時間」現在時刻
        now = datetime.datetime.now(TZ)
        today_send = now.replace(
            hour=SEND_HOUR, minute=SEND_MINUTE, second=0, microsecond=0
        )

        # 決定下一次發訊息時間（今天或明天的 20:00）
        if now >= today_send:
            next_send = today_send + datetime.timedelta(days=1)
        else:
            next_send = today_send

        wait_seconds = (next_send - now).total_seconds()
        print(f"下一次發訊息時間（Asia/Taipei）：{next_send}（等待 {wait_seconds:.0f} 秒）")
        await asyncio.sleep(wait_seconds)

        # 用台北時間決定今天日期
        now = datetime.datetime.now(TZ)
        today = now.date()
        diff = (EXAM_END - today).days

        # --- 訊息邏輯 ---
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


@client.event
async def on_ready():
    global task_started
    print(f"Bot 已登入：{client.user}")
    if not task_started:
        asyncio.create_task(countdown_task())
        task_started = True


client.run(TOKEN)
