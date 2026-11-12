FROM python:3.12-slim

WORKDIR /app

# Install system dependencies if any, clean up to keep container size small
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Expose the default port for the Realtime API
EXPOSE 7860

# Run Uvicorn with optimized low-RAM workers, high keep-alive, and native WebSocket ping/pong
# Note: Do NOT set a low --limit-concurrency (like 200) because every active WebSocket counts towards concurrency!
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860", "--workers", "2", "--timeout-keep-alive", "65", "--ws-ping-interval", "25", "--ws-ping-timeout", "30", "--limit-max-requests", "50000"]
