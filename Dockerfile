# 使用官方 Python 映像
FROM python:3.11-slim

# 安裝 ffmpeg（重點）
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# 設定工作目錄
WORKDIR /app

# 先複製 requirements 並安裝依賴
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再複製其他程式碼
COPY . .

# 啟動指令
CMD ["python", "bot.py"]
