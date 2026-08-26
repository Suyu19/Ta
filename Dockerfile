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

# Show exactly what Railway is installing.
RUN echo "===== requirements.txt =====" && \
    cat /app/requirements.txt && \
    echo "============================"

RUN python -m pip install --upgrade pip setuptools wheel && \
    python -m pip install --no-cache-dir -r /app/requirements.txt

# Hard build-time verification.
# If pandas/numpy/requests are missing, the IMAGE MUST NOT BUILD.
RUN python -c "import pandas, numpy, requests; print('DEPENDENCY CHECK PASS'); print('pandas=', pandas.__version__); print('numpy=', numpy.__version__); print('requests=', requests.__version__)"

COPY . /app

CMD ["python", "bot.py"]
