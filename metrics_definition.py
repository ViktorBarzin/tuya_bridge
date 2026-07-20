from abc import ABC, abstractmethod, abstractproperty
import base64
from dataclasses import dataclass
import logging
import struct
import time
from typing import Any, override
from prometheus_client import (
    CollectorRegistry,
    Gauge,
    generate_latest,
)
from abc import abstractmethod

import tinytuya

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tuya-bridge")

# Rate-limit Tuya error logging. Without this a single expired-subscription event
# flooded logs with a stack trace per request (many-per-second per device).
_ERROR_LOG_THROTTLE_SECONDS = 60
_last_error_log: dict[tuple, float] = {}


def _log_tuya_error(result: Any, is_auth: bool) -> None:
    code = result.get("code") if isinstance(result, dict) else None
    msg = result.get("msg") if isinstance(result, dict) else None
    key = (code, msg)
    now = time.time()
    if now - _last_error_log.get(key, 0.0) < _ERROR_LOG_THROTTLE_SECONDS:
        return
    _last_error_log[key] = now
    log.error("Tuya Cloud error: code=%s msg=%s auth=%s", code, msg, is_auth)


# Tuya Cloud business error codes (and tinytuya wrapper codes, ERR_* = 9xx) that
# mean the CREDENTIAL / SUBSCRIPTION / PERMISSION is the problem — i.e. a human
# must renew the subscription or rotate the API key. Only these drive
# tuya_cloud_up to 0 (which fires the TuyaCloudDown "renew subscription" alert).
# Every other failure — transient server errors like 501 "request fail with
# unknown error", rate limits, timeouts, or ANY code we don't recognise — leaves
# the gauge at 1, so a Tuya-side hiccup is never misreported as an expired key.
_AUTH_ERROR_CODES = frozenset(
    {
        1106,  # permission deny
        28841002,  # no permissions — cloud subscription / trial expired (documented)
        28841101,  # no permissions — API/resource not authorized for this project
        28841105,  # insufficient permissions
        909,  # tinytuya ERR_CLOUDKEY — missing/invalid Tuya key & secret
    }
)

# Unambiguous auth/subscription phrases. A match makes us "certain" even when the
# numeric code is one Tuya added that we haven't enumerated. Kept narrow so a
# transient message (e.g. "request fail with unknown error") never matches.
_AUTH_ERROR_MSG_MARKERS = (
    "no permission",
    "permission deny",
    "not subscribed",
    "subscription",
    "not authorized",
)


def _is_auth_error(result: Any) -> bool:
    """True only when we are CERTAIN a Tuya failure is an auth/permission/
    subscription problem (renew the subscription or rotate the key).

    Returns False for transient server errors (e.g. code 501), rate limits,
    tinytuya connect/timeout/token wrappers, and any unrecognised code — those
    must not masquerade as an expired key. Tuya normally replies HTTP 200 with a
    business ``code`` in the body, but a genuine HTTP 4xx (if ever surfaced) also
    counts as certain.
    """
    if not isinstance(result, dict):
        return False
    for status_key in ("http_status", "status_code", "status"):
        status = result.get(status_key)
        if isinstance(status, int) and 400 <= status < 500:
            return True
    code = result.get("code")
    if isinstance(code, int) and code in _AUTH_ERROR_CODES:
        return True
    msg = result.get("msg")
    if isinstance(msg, str):
        lowered = msg.lower()
        if any(marker in lowered for marker in _AUTH_ERROR_MSG_MARKERS):
            return True
    return False


@dataclass
class MetricsDefinition(ABC):
    registry: CollectorRegistry
    cloud: tinytuya.Cloud
    device_id: str

    @property
    @abstractmethod
    def metrics_schema(self) -> dict[str, Gauge]: ...

    @abstractmethod
    def collect(self) -> CollectorRegistry: ...

    def _extract_datapoints(self, result: Any) -> list | None:
        """Validate a Tuya Cloud response and emit the tuya_cloud_up gauge.

        Returns the device datapoint list on success, or None if the call did
        not yield data. Callers should return self.registry unchanged when this
        returns None so the /metrics endpoint stays 200 OK.

        tuya_cloud_up drops to 0 ONLY when we are certain the failure is an
        auth/permission/subscription problem (see ``_is_auth_error``). A
        transient Tuya server error (e.g. code 501) leaves the gauge at 1 so it
        is never mistaken for an expired key — a sustained transient still shows
        up downstream as a *MetricsMissing alert.
        """
        up = Gauge(
            "tuya_cloud_up",
            "1 unless Tuya Cloud rejected the call for an auth/permission/"
            "subscription reason; transient server errors (e.g. 501) keep it 1",
            registry=self.registry,
        )
        data = result.get("result") if isinstance(result, dict) else None
        if not isinstance(data, list):
            is_auth = _is_auth_error(result)
            up.set(0 if is_auth else 1)
            _log_tuya_error(result, is_auth)
            return None
        up.set(1)
        return data


class AutomaticTransferSwitch(MetricsDefinition):
    @property
    @override
    def metrics_schema(self) -> dict[str, Gauge]:
        metrics = {
            "fault": Gauge("fault", "Device fault code", registry=self.registry),
            "power_fault": Gauge(
                "power_fault", "Power fault flag", registry=self.registry
            ),
            "load_power": Gauge(
                "load_power_watts", "Load power (W)", registry=self.registry
            ),
            "load_current": Gauge(
                "load_current_amps", "Load current (A)", registry=self.registry
            ),
            "overpower_value": Gauge(
                "overpower_value", "Overpower threshold", registry=self.registry
            ),
            "lowpower_switch": Gauge(
                "lowpower_switch", "Low power threshold", registry=self.registry
            ),
            "lowpower_reset": Gauge(
                "lowpower_reset", "Low power reset threshold", registry=self.registry
            ),
            "totalele_add": Gauge(
                "totalele_add",
                "Total accumulated energy (Wh) from inverter",
                registry=self.registry,
            ),
            "dwele_add": Gauge(
                "dwele_add",
                "Total accumulated energy (Wh) from grid",
                registry=self.registry,
            ),
            "voltage_l1": Gauge(
                "voltage_l1_volts", "L1 voltage (V)", registry=self.registry
            ),
            "voltage_l2": Gauge(
                "voltage_l2_volts", "L2 voltage (V)", registry=self.registry
            ),
            "voltage_batt": Gauge(
                "voltage_battery_volts", "Battery voltage (V)", registry=self.registry
            ),
            "power_mode": Gauge(
                "power_mode", "Power source (0=grid,1=inverter)", registry=self.registry
            ),
        }
        return metrics

    @override
    def collect(self) -> CollectorRegistry:
        result = self.cloud.getstatus(self.device_id)
        data = self._extract_datapoints(result)
        if data is None:
            return self.registry
        metrics = self.metrics_schema

        # convert list of dicts into {code: value}
        datapoints = {item["code"]: item["value"] for item in data if "code" in item}

        for code, val in datapoints.items():
            if code == "voltage_display":
                v1, v2, batt = self.parse_voltage_string(val)
                if v1 is not None:
                    metrics["voltage_l1"].set(v1)
                if v2 is not None:
                    metrics["voltage_l2"].set(v2)
                if batt is not None:
                    metrics["voltage_batt"].set(batt)

            elif code == "power_mode":
                metrics["power_mode"].set(self.map_power_mode(val))

            elif code in metrics:
                try:
                    metrics[code].set(float(val))
                except (TypeError, ValueError):
                    pass
        return self.registry

    def parse_voltage_string(self, vstr):
        """Parse concatenated voltage string like '0238024024.9'"""
        try:
            # crude example: split into 3 parts of 4, 4, and rest
            # example value: 0238023924.8
            l1 = int(vstr[0:4])
            l2 = int(vstr[4:8])
            batt = float(vstr[8:])
            return l1, l2, batt
        except Exception:
            return None, None, None

    def map_power_mode(self, mode_str):
        """
        Map power mode strings to numeric values for Prometheus.
        - 'crid_power' or 'grid_power' => 0
        - 'invert_power' or 'inverter_power' => 1
        """
        if not isinstance(mode_str, str):
            return 0
        mode_str = mode_str.lower()
        if "invert" in mode_str:
            return 1
        return 0


class Fuse(MetricsDefinition):
    """
    RC-RCBO device

         Example format:

         {
        "result": [
            {
                "code": "switch_1",
                "value": true
            },
            {
                "code": "countdown_1",
                "value": 0
            },
            {
                "code": "fault",
                "value": 0
            },
            {
                "code": "relay_status",
                "value": "2"
            },
            {
                "code": "child_lock",
                "value": false
            },
            {
                "code": "Voltage",
                "value": "CUsAAAAA"
            },
            {
                "code": "Current",
                "value": "AAcTAAAAAAAA"
            },
            {
                "code": "ActivePower",
                "value": "AA40AA40AAAAAAAA"
            },
            {
                "code": "LeakageCurrent",
                "value": 9
            },
            {
                "code": "Temperature",
                "value": 36
            },
            {
                "code": "RemainingEnergy",
                "value": 0
            },
            {
                "code": "CostParameters",
                "value": "CRQA"
            },
            {
                "code": "LeakageParameters",
                "value": "AQAAASwBAQA="
            },
            {
                "code": "VoltageThreshold",
                "value": "CcQBAQfQAQE="
            },
            {
                "code": "CurrentThreshold",
                "value": "ALuAAQE="
            },
            {
                "code": "TemperatureThreshold",
                "value": "MgEB"
            },
            {
                "code": "KWH",
                "value": 351644
            },
            {
                "code": "NumberAndType",
                "value": "280100000002        "
            },
            {
                "code": "locking",
                "value": false
            },
            {
                "code": "RKWH",
                "value": 0
            },
            {
                "code": "VRecording",
                "value": "CUYAAAAA"
            },
            {
                "code": "IRecording",
                "value": "AArqAAAAAAAA"
            }
        ],
        "success": true,
        "t": 1761770761925,
        "tid": "47de5af7b50811f0a25e9ed280a40f39"
    }

    """

    @override
    def collect(self) -> CollectorRegistry:
        result = self.cloud.getstatus(self.device_id)
        data = self._extract_datapoints(result)
        if data is None:
            return self.registry
        metrics = self.metrics_schema

        # convert list of dicts into {code: value}
        datapoints = {item["code"]: item["value"] for item in data if "code" in item}

        for code, value in datapoints.items():
            # Special cases
            if code.lower().startswith("voltagethreshold"):
                low_threshold, high_threshold = self.decode_voltage_threshold(value)
                metrics["LowVoltageThreshold"].set(low_threshold)
                metrics["HighVoltageThreshold"].set(high_threshold)

            if metrics.get(code) is None:
                log.debug(f"{code=} not used in our definition")
                continue
            decoded = self.decode_metric(code, value)
            # metrics[code].set(self.decode_metric(code, value))
            metrics[code].set(decoded)

        return self.registry

    @property
    @override
    def metrics_schema(self) -> dict[str, Gauge]:
        return {
            "switch_1": Gauge(
                "switch_1", "Switch status (0=false, 1=true)", registry=self.registry
            ),
            "countdown_1": Gauge("countdown_1", "Countdown", registry=self.registry),
            "fault": Gauge("fault", "Fault", registry=self.registry),
            "relay_status": Gauge(
                "relay_status", "Relay status", registry=self.registry
            ),
            "child_lock": Gauge(
                "child_lock", "Child lock (0=off, 1=on)", registry=self.registry
            ),
            "Voltage": Gauge("voltage", "Voltage", registry=self.registry),
            "Current": Gauge("current", "Current", registry=self.registry),
            "ActivePower": Gauge(
                "active_power",
                "Active power",
                registry=self.registry,
            ),
            "LeakageCurrent": Gauge(
                "leakage_current",
                "Leakage current",
                registry=self.registry,
            ),
            "Temperature": Gauge("temperature", "Temperature", registry=self.registry),
            "RemainingEnergy": Gauge(
                "remaining_energy", "Remaining energy", registry=self.registry
            ),
            "LowVoltageThreshold": Gauge(
                "low_voltage_threshold", "Low voltage threshold", registry=self.registry
            ),
            "HighVoltageThreshold": Gauge(
                "high_voltage_threshold",
                "High voltage threshold",
                registry=self.registry,
            ),
            "CurrentThreshold": Gauge(
                "current_threshold", "Current Threshold", registry=self.registry
            ),
            "TemperatureThreshold": Gauge(
                "temperature_threshold", "Temperature threshold", registry=self.registry
            ),
            "KWH": Gauge("kwh", "kwh", registry=self.registry),
            "RKWH": Gauge("rkwh", "rkwh", registry=self.registry),
        }

    def decode_metric(self, name: str, val: str | int | bool):
        if isinstance(val, int):
            return val
        if isinstance(val, bool):
            return int(bool)
        if isinstance(val, str):
            try:
                return float(val)
            except Exception:
                ...  # continue trying

        if name.lower().startswith("temperaturethreshold"):
            raw = base64.b64decode(val)
            value = int(raw[0])
            return value
        try:
            raw = base64.b64decode(val)
            # Most Tuya encodings use 4 bytes for a little-endian integer
            num = struct.unpack("<I", raw[:4])[0]

            if name.lower().startswith("voltage"):
                return int.from_bytes(raw[0:2], "big")
            elif name.lower().startswith("current"):
                return int.from_bytes(raw[0:3], "big") / 1000
            elif name.lower().startswith("activepower"):
                return int.from_bytes(raw[0:3], "big") / 10000  # kWh
            else:
                return float(num)
        except Exception as e:
            print(f"{e} for {name=}:{val=}")
            return -1

    def decode_voltage_threshold(self, val: str) -> tuple[float, float]:
        raw = base64.b64decode(val)
        high_threshold = int.from_bytes(raw[0:2], "big") / 10
        low_threshold = int.from_bytes(raw[4:6], "big") / 10

        return low_threshold, high_threshold


class Thermostat(MetricsDefinition):
    """
    https://pb.viktorbarzin.me/?beeb58e7ee5218b6#3gcvhrAu3d5vP1dPQcoTJ44hEURsBdT7xP4VSYKNPMSi
      {
    "result": [
        {
            "code": "switch",
            "value": true
        },
        {
            "code": "mode",
            "value": "heat"
        },
        {
            "code": "work_state",
            "value": "heating_off"
        },
        {
            "code": "temp_set",
            "value": 210
        },
        {
            "code": "temp_set_f",
            "value": 50
        },
        {
            "code": "upper_temp_f",
            "value": 50
        },
        {
            "code": "upper_temp",
            "value": 350
        },
        {
            "code": "lower_temp_f",
            "value": 50
        },
        {
            "code": "temp_current",
            "value": 240
        },
        {
            "code": "lower_temp",
            "value": 100
        },
        {
            "code": "temp_correction",
            "value": -10
        },
        {
            "code": "holiday_temp_set",
            "value": 200
        },
        {
            "code": "holiday_days_set",
            "value": 1
        },
        {
            "code": "humidity",
            "value": 35
        },
        {
            "code": "child_lock",
            "value": false
        },
        {
            "code": "sensor_choose",
            "value": "internal"
        },
        {
            "code": "backlight",
            "value": 50
        },
        {
            "code": "run_mode",
            "value": "program"
        },
        {
            "code": "control_algorithm",
            "value": "TPI_UFH"
        },
        {
            "code": "max_heat_temp_set_f",
            "value": 350
        },
        {
            "code": "min_heat_temp_set_f",
            "value": 100
        },
        {
            "code": "max_cool_temp_set_f",
            "value": 150
        },
        {
            "code": "min_cool_temp_set_f",
            "value": 70
        },
        {
            "code": "frost_set",
            "value": 50
        },
        {
            "code": "valve_protection",
            "value": true
        },
        {
            "code": "relay_type",
            "value": "NO_COM"
        },
        {
            "code": "week_program_13_1",
            "value": "AQUAAOYIAADSDgAA0hAAAOYRAADmFgAA0g=="
        },
        {
            "code": "week_program_13_2",
            "value": "AgUAAOYIAADSDgAA0hAAAOYRAADmFgAA0g=="
        },
        {
            "code": "week_program_13_3",
            "value": "AwUAAOYIAADSDgAA0hAAAOYRAADmFgAA0g=="
        },
        {
            "code": "week_program_13_4",
            "value": "BAUAAOYIAADSDgAA0hAAAOYRAADmFgAA0g=="
        },
        {
            "code": "week_program_13_5",
            "value": "BQUAAOYIAADSDgAA0hAAAOYRAADmFgAA0g=="
        },
        {
            "code": "week_program_13_6",
            "value": "BgYeAOYIAADmDgAA5hAAAOYSAADmFgAA0g=="
        },
        {
            "code": "week_program_13_7",
            "value": "BwYeAOYIAADmDgAA5hAAAOYSAADmFgAA0g=="
        },
        {
            "code": "current_temp_floor",
            "value": 0
        },
        {
            "code": "temp_resolution",
            "value": "0_1"
        },
        {
            "code": "warm_floor",
            "value": "OFF"
        },
        {
            "code": "pin_to_unlock",
            "value": false
        },
        {
            "code": "sensor_error",
            "value": "E2"
        },
        {
            "code": "is_password_set",
            "value": false
        }
    ],
    "success": true,
    "t": 1767355102647,
    "tid": "5657803fe7d211f0a6d2e2403ac67220"
    }
    """

    @override
    def collect(self) -> CollectorRegistry:
        result = self.cloud.getstatus(self.device_id)
        data = self._extract_datapoints(result)
        if data is None:
            return self.registry
        metrics = self.metrics_schema

        # convert list of dicts into {code: value}
        datapoints = {item["code"]: item["value"] for item in data if "code" in item}

        for code, value in datapoints.items():
            if metrics.get(code) is None:
                log.debug(f"{code=} not used in our definition")
                continue
            if code == "work_state":
                if value == "heating":
                    decoded = 0
                elif value == "heating_off":
                    decoded = 1
                elif value == 'cooling':
                    decoded = 2
                elif value == 'cooling_off':
                    decoded = 3
                else:
                    decoded = -1
            elif code == 'run_mode':
                # Manual, Program, Holiday, Frost, Temporary
                value = value.lower()
                if value == 'manual':
                    decoded = 0
                elif value == 'program':
                    decoded = 1
                elif value == 'holiday':
                    decoded = 2
                elif value == 'frost':
                    decoded = 3
                elif value == 'temporary':
                    decoded = 4
            else:
                decoded = value
            # metrics[code].set(self.decode_metric(code, value))
            metrics[code].set(decoded)

        return self.registry

    @property
    @override
    def metrics_schema(self) -> dict[str, Gauge]:
        return {
            "RKWH": Gauge("rkwh", "rkwh", registry=self.registry),
            "switch": Gauge(
                "switch",
                "Device switch state (1=true, 0=false)",
                registry=self.registry,
            ),
            "temp_set": Gauge(
                "temp_set", "Configured temperature", registry=self.registry
            ),
            "temp_set_f": Gauge(
                "temp_set_f", "Configured temperature (F)", registry=self.registry
            ),
            "upper_temp_f": Gauge(
                "upper_temp_f", "Upper temperature limit (F)", registry=self.registry
            ),
            "upper_temp": Gauge(
                "upper_temp", "Upper temperature limit", registry=self.registry
            ),
            "lower_temp_f": Gauge(
                "lower_temp_f", "Lower temperature limit (F)", registry=self.registry
            ),
            "temp_current": Gauge(
                "temp_current", "Current temperature", registry=self.registry
            ),
            "lower_temp": Gauge(
                "lower_temp", "Lower temperature limit", registry=self.registry
            ),
            "temp_correction": Gauge(
                "temp_correction", "Temperature correction", registry=self.registry
            ),
            "holiday_temp_set": Gauge(
                "holiday_temp_set",
                "Holiday temperature setpoint",
                registry=self.registry,
            ),
            "holiday_days_set": Gauge(
                "holiday_days_set", "Holiday days configured", registry=self.registry
            ),
            "humidity": Gauge(
                "humidity", "Current humidity percentage", registry=self.registry
            ),
            "child_lock": Gauge(
                "child_lock",
                "Child lock enabled (1=true, 0=false)",
                registry=self.registry,
            ),
            "backlight": Gauge("backlight", "Backlight level", registry=self.registry),
            "work_state": Gauge("work_state", "Work state (heating_off=0, heating_on=1)", registry=self.registry),
            "run_mode": Gauge("run_mode", "Work mode(Manual=0, Program=1, Holiday=2, Frost=3, Temporary=4)", registry=self.registry),
        }
