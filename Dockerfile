FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Discord voice / yt-dlp runtime dependencies
# Node.js is kept because yt-dlp may require a JS runtime.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        nodejs \
        npm \
        ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Copy dependency file first so Docker can cache this layer safely.
COPY requirements.txt /app/requirements.txt

RUN python -m pip install --upgrade pip setuptools wheel && \
    python -m pip install --no-cache-dir -r /app/requirements.txt

# Copy application only after dependencies are installed.
COPY . /app

CMD ["python", "bot.py"]
