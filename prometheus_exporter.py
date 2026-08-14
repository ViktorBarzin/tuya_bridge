from abc import abstractmethod
import threading
import time
import cachetools
from typing import Any, override
from tenacity import retry, stop_after_attempt, stop_after_delay, wait_exponential
import tinytuya
from prometheus_client import (
    CollectorRegistry,
    generate_latest,
    Gauge,
)

from metrics_definition import (
    AutomaticTransferSwitch,
    Fuse,
    MetricsDefinition,
    Thermostat,
)

cache = cachetools.TTLCache(maxsize=100, ttl=30)

device_id_to_metrics: dict[str, type[MetricsDefinition]] = {
    "bfe98afa941d5a1e2def8s": AutomaticTransferSwitch,
    "bf1a684e80ae942e4dji6b": Fuse,  # main
    "bf62301ef04e38d881ugcu": Fuse,  # garage
    "bf7efce9519bd508df431s": Thermostat,  # hol
    "bf70e80159641f61a5lzho": Thermostat,  # master bedroom
    "bf9597a0064f0349d4b09x": Thermostat,  # office
    "bfe64da91577117e0annt5": Thermostat,  # kid's room
}


# The cache is shared by the gunicorn worker's request threads, so its lookups
# need a lock; cachetools still calls collect_metrics outside it, meaning a slow
# Tuya call never blocks the other threads.
#
# stop_after_delay caps the whole retry sequence. Retries exist for transient
# Tuya errors, which fail fast and so still get all three attempts; a call that
# instead burns its connect/read timeout gives up early rather than stacking
# three of them, which no caller is still waiting for by then anyway.
@cachetools.cached(cache, lock=threading.Lock())
@retry(
    stop=stop_after_attempt(3) | stop_after_delay(15),
    wait=wait_exponential(multiplier=1, min=2, max=10),
)
def collect_metrics(cloud: tinytuya.Cloud, device_id: str) -> CollectorRegistry:
    registry = CollectorRegistry()

    collector = device_id_to_metrics[device_id](
        registry=registry, cloud=cloud, device_id=device_id
    )
    return collector.collect()
