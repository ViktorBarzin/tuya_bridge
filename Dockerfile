# Dockerfile
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

# Every dependency ships a manylinux wheel, so no compiler is needed; the old
# build-essential layer only added linux-libc-dev and its CVEs to the image.
# apt-get upgrade pulls trixie-security fixes the base image has not picked up
# yet, and pip goes once the dependencies are in: nothing calls it at runtime,
# and its vendored urllib3, msgpack and setuptools carry High CVEs.
COPY requirements.txt .
RUN apt-get update && apt-get upgrade -y \
  && rm -rf /var/lib/apt/lists/* \
  && pip install --no-cache-dir -r requirements.txt \
  && pip uninstall -y pip

COPY . .

# default port
EXPOSE 8080

# Run with gunicorn. The workload is entirely network I/O against the Tuya
# Cloud, so concurrency comes from threads rather than more workers: a stalled
# call occupies one thread instead of a whole worker, and threads fit the 256Mi
# limit that four more processes would not. --timeout stays a backstop only —
# Tuya calls are bounded by their own connect/read timeouts (see tuya_http.py).
CMD ["gunicorn", "-w", "4", "-k", "gthread", "--threads", "4", "--timeout", "60", "--graceful-timeout", "30", "-b", "0.0.0.0:8080", "api:app"]
