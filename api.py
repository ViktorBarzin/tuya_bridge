# app.py
import os
import time
import threading
import logging
from flask import Flask, request, jsonify, abort
from prometheus_client import generate_latest
import tinytuya

from prometheus_exporter import collect_metrics
from slack import send_message

# --- Logging ---
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tuya-bridge")

# --- Config from env ---
TINYTUYA_REGION = os.getenv("TINYTUYA_REGION", "eu")  # eu, us, cn, in
TINYTUYA_API_KEY = os.getenv("TINYTUYA_API_KEY")
TINYTUYA_API_SECRET = os.getenv("TINYTUYA_API_SECRET")
SERVICE_API_KEY = os.getenv("SERVICE_API_KEY", "change-me")  # used by clients (HA)

# Tuya-cloud-aware health probe knobs. /health returns 503 if the background
# probe has not succeeded within STALE_AFTER seconds; this is what the kubelet
# liveness probe reads to detect a silently-hung bridge (gunicorn workers up
# but the Tuya cloud call path is dead).
HEALTH_PROBE_INTERVAL = int(os.getenv("HEALTH_PROBE_INTERVAL", "60"))
HEALTH_STALE_AFTER = int(os.getenv("HEALTH_STALE_AFTER", "300"))

if not (TINYTUYA_API_KEY and TINYTUYA_API_SECRET):
    log.error("Missing TINYTUYA_API_KEY or TINYTUYA_API_SECRET environment variables.")
    raise SystemExit("Missing Tuya credentials")

# --- Initialize Cloud client (tinytuya handles tokens internally) ---
log.info("Initializing TinyTuya Cloud client for region=%s", TINYTUYA_REGION)
cloud = tinytuya.Cloud(
    apiRegion=TINYTUYA_REGION, apiKey=TINYTUYA_API_KEY, apiSecret=TINYTUYA_API_SECRET
)

# Separate Cloud client for the health probe so its requests.Session doesn't
# race with request handlers that share `cloud`.
_probe_cloud = tinytuya.Cloud(
    apiRegion=TINYTUYA_REGION, apiKey=TINYTUYA_API_KEY, apiSecret=TINYTUYA_API_SECRET
)
_last_tuya_success = 0.0
_last_tuya_error: str | None = None
_probe_lock = threading.Lock()


def _tuya_probe_loop() -> None:
    global _last_tuya_success, _last_tuya_error
    while True:
        try:
            result = _probe_cloud.getdevices()
            if isinstance(result, list):
                with _probe_lock:
                    _last_tuya_success = time.time()
                    _last_tuya_error = None
            else:
                with _probe_lock:
                    _last_tuya_error = f"unexpected response: {result!r}"[:200]
                log.warning("Tuya probe returned non-list: %r", result)
        except Exception as e:
            with _probe_lock:
                _last_tuya_error = f"{type(e).__name__}: {e}"[:200]
            log.warning("Tuya probe failed: %s: %s", type(e).__name__, e)
        time.sleep(HEALTH_PROBE_INTERVAL)


threading.Thread(target=_tuya_probe_loop, daemon=True, name="tuya-probe").start()

# --- Flask app ---
app = Flask(__name__)


def check_auth():
    token = request.headers.get("X-API-KEY", None)
    if token is not None and token == SERVICE_API_KEY:
        return
    token = request.args.get("api-key", None)
    if token is not None and token == SERVICE_API_KEY:
        return
    send_message("Unauthorized access attempt")
    abort(401, "invalid api key")


@app.route("/health", methods=["GET"])
def health():
    with _probe_lock:
        last_success = _last_tuya_success
        last_error = _last_tuya_error
    now = time.time()
    age = now - last_success if last_success > 0 else None
    healthy = age is not None and age <= HEALTH_STALE_AFTER
    body = {
        "ok": healthy,
        "region": TINYTUYA_REGION,
        "last_success_age_seconds": age,
        "stale_after_seconds": HEALTH_STALE_AFTER,
        "last_error": last_error,
    }
    return jsonify(body), (200 if healthy else 503)


@app.route("/devices", methods=["GET"])
def list_devices():
    check_auth()
    try:
        devices = cloud.getdevices()
        return jsonify({"success": True, "devices": devices})
    except Exception as e:
        log.exception("Failed to list devices")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/devices/<device_id>/status", methods=["GET"])
def device_status(device_id):
    check_auth()
    try:
        status = cloud.getstatus(device_id)
        return jsonify({"success": True, "status": status})
    except Exception as e:
        log.exception("Failed to get status for %s", device_id)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/devices/<device_id>/functions", methods=["GET"])
def device_functions(device_id):
    check_auth()
    try:
        funcs = cloud.getfunctions(device_id)
        return jsonify({"success": True, "functions": funcs})
    except Exception as e:
        log.exception("Failed to get functions for %s", device_id)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/devices/<device_id>/commands", methods=["POST"])
def device_commands(device_id):
    log.info(f"Handling POST for {device_id}")
    check_auth()
    body = request.get_json(silent=True) or {}
    commands = body.get("commands")
    if not commands:
        log.info(f"Missing commands list in JSON body")
        send_message(f"Missing commands list in JSON body")
        return (
            jsonify(
                {"success": False, "error": "missing 'commands' list in JSON body"}
            ),
            400,
        )

    # commands is expected to be a list like: [{"code":"switch_1","value":true}, ...]
    payload = {"commands": commands}
    try:
        log.info(f"Handling {commands} for {device_id}")
        res = cloud.sendcommand(device_id, payload)
        log.info(f"Successfully handled {commands} for {device_id}: {res}")
        return jsonify({"success": True, "result": res})
    except Exception as e:
        msg = f"Failed to send command {payload} to {device_id}: {e}"
        send_message(msg)
        log.exception(msg)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/metrics/<device_id>", methods=["GET"])
def metrics(device_id):
    check_auth()
    updated_registry = collect_metrics(cloud, device_id)

    result = generate_latest(updated_registry)
    return result


@app.route("/json/<device_id>", methods=["GET"])
def collect_json(device_id):
    check_auth()
    registry = collect_metrics(cloud, device_id)
    metrics = {}
    for metric in registry.collect():
        for sample in metric.samples:
            metrics[sample.name] = sample.value
    return jsonify(metrics)


# simple root
@app.route("/", methods=["GET"])
def index():
    return jsonify({"service": "tuya-bridge", "version": "1.0"})


if __name__ == "__main__":
    # For local testing only. In k8s we'll use gunicorn in the container.
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
