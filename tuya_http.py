"""Bound the HTTP calls tinytuya makes to the Tuya Cloud API.

tinytuya's Cloud client issues `requests.get` / `requests.post` without a
`timeout` (still the case in the newest release, 1.20.0), so a Tuya endpoint
that accepts no SYN parks the calling thread in `sock.connect()` for as long as
the kernel keeps retrying. Under gunicorn that means a worker sits on a single
request until the master kills it — the WORKER TIMEOUT reported in infra#23.

tinytuya exposes no knob for this: `Cloud.__init__` takes `**extrakw` and drops
it. So we wrap the `requests` reference inside tinytuya's Cloud module with a
proxy that supplies a default timeout.

A connect timeout does more than fail faster here: `urllib3.util.connection`
walks *every* address `getaddrinfo` returned, so a bounded connect on a
blackholed Tuya IP moves on to the next one and the call still succeeds.
"""

import logging
import sys
from typing import Any

import tinytuya

log = logging.getLogger("tuya-bridge")


class TimeoutRequests:
    """Proxy for the `requests` module that defaults `timeout` on get/post."""

    def __init__(self, wrapped: Any, timeout: tuple[int, int]) -> None:
        self.wrapped = wrapped
        self.timeout = timeout

    def get(self, *args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", self.timeout)
        return self.wrapped.get(*args, **kwargs)

    def post(self, *args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", self.timeout)
        return self.wrapped.post(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        # Everything tinytuya reaches for besides get/post — requests.Request
        # while it builds the signature, exception classes — passes straight
        # through to the real module.
        return getattr(self.wrapped, name)


def install_request_timeout(connect: int, read: int) -> None:
    """Give every Tuya Cloud HTTP call a connect/read timeout.

    Must run before the first `tinytuya.Cloud(...)`, which fetches a token in
    its constructor. Idempotent: re-installing retunes the existing proxy
    instead of wrapping it again, so the timeout that reaches `requests` is
    always the most recent one.
    """
    module = sys.modules[tinytuya.Cloud.__module__]
    timeout = (connect, read)

    current = getattr(module, "requests", None)
    if current is None:
        raise RuntimeError(
            f"{module.__name__} has no `requests` attribute to wrap — tinytuya "
            "has changed its HTTP layer, and Tuya calls would run untimed again"
        )

    if isinstance(current, TimeoutRequests):
        current.timeout = timeout
    else:
        module.requests = TimeoutRequests(current, timeout)

    log.info("Tuya HTTP timeout: connect=%ss read=%ss", connect, read)
