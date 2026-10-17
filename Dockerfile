FROM python:3.12-slim

WORKDIR /app

# Install system dependencies if any, clean up to keep container size small
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Expose the default port for the Realtime API
EXPOSE 7860

# Run Uvicorn listening on port 7860 with keepalive and protocol-level WebSocket ping/timeouts
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860", "--ws-ping-interval", "20", "--ws-ping-timeout", "20", "--timeout-keep-alive", "5"]
