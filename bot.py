import discord
import asyncio
import datetime
import os
from dotenv import load_dotenv

# 讀取 .env 設定
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
SEND_HOUR = int(os.getenv("SEND_HOUR", "19"))
SEND_MINUTE = int(os.getenv("SEND_MINUTE", "30"))

# 期末考期間
EXAM_START = datetime.date(2026, 1, 5)
EXAM_END = datetime.date(2026, 1, 10)

intents = discord.Intents.default()
client = discord.Client(intents=intents)

# 避免 on_ready 觸發多次時重複啟動任務
task_started = False


async def countdown_task():
    await client.wait_until_ready()
    channel = client.get_channel(CHANNEL_ID)

    if channel is None:
        print("找不到頻道，請確認 CHANNEL_ID 是否正確！")
        return

    print("倒數排程啟動…")

    while not client.is_closed():
        now = datetime.datetime.now()
        today_send = now.replace(
            hour=SEND_HOUR, minute=SEND_MINUTE, second=0, microsecond=0
        )

        # 決定下一次發訊息時間（今天或明天）
        if now >= today_send:
            next_send = today_send + datetime.timedelta(days=1)
        else:
            next_send = today_send

        wait_seconds = (next_send - now).total_seconds()
        print(f"下一次發訊息時間：{next_send}（等待 {wait_seconds:.0f} 秒）")
        await asyncio.sleep(wait_seconds)

        # 計算今天日期 & 距離考試結束日天數
        today = datetime.date.today()
        diff = (EXAM_END - today).days

        # --- 訊息邏輯 ---

        if today == EXAM_START:
            # 1/05
            msg = "(1/05) 今天是期末考第一天！Fight！！"

        elif EXAM_START < today < EXAM_END:
            # 1/06 ~ 1/09
            msg = f"({today.month}/{today.day}) 期末考進行中！加油！！"

        elif today == EXAM_END:
            # 1/10
            msg = "(1/10) 今天是期末考的最後一天！（2026-01-10）加油！"

        elif today > EXAM_END:
            # 1/10 之後
            msg = f"📘 期末考已經結束 {abs(diff)} 天，辛苦了～"

        else:
            # 考試開始之前：對 1/10 做倒數
            msg = f"📘 期末考倒數：還剩 **{diff} 天**！"

        await channel.send(msg)


@client.event
async def on_ready():
    global task_started
    print(f"Bot 已登入：{client.user}")
    if not task_started:
        asyncio.create_task(countdown_task())
        task_started = True


client.run(TOKEN)
