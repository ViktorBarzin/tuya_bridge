# Dockerfile
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

# system deps (if any)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
  && rm -rf /var/lib/apt/lists/*

# copy and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# default port
EXPOSE 8080

# Run with gunicorn. The workload is entirely network I/O against the Tuya
# Cloud, so concurrency comes from threads rather than more workers: a stalled
# call occupies one thread instead of a whole worker, and threads fit the 256Mi
# limit that four more processes would not. --timeout stays a backstop only —
# Tuya calls are bounded by their own connect/read timeouts (see tuya_http.py).
CMD ["gunicorn", "-w", "4", "-k", "gthread", "--threads", "4", "--timeout", "60", "--graceful-timeout", "30", "-b", "0.0.0.0:8080", "api:app"]
