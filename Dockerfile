FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        nodejs \
        npm \
        ca-certificates && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt

RUN echo "===== requirements.txt =====" && \
    cat /app/requirements.txt && \
    echo "============================"

RUN python -m pip install --upgrade pip setuptools wheel

# Install all original bot dependencies.
RUN python -m pip install --no-cache-dir -r /app/requirements.txt

# Explicitly guarantee the trading-engine dependencies exist,
# even if requirements.txt is stale/malformed/cached unexpectedly.
RUN python -m pip install --no-cache-dir \
    "pandas>=2.0" \
    "numpy>=1.24" \
    "requests>=2.31" \
    "tzdata>=2024.1"

# Build must fail here if the trading runtime is incomplete.
RUN python -c "import pandas, numpy, requests, zoneinfo; print('TRADING DEPENDENCY CHECK PASS'); print('pandas=', pandas.__version__); print('numpy=', numpy.__version__); print('requests=', requests.__version__)"

COPY . /app

CMD ["python", "bot.py"]
