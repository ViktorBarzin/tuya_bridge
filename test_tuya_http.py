import sys
import unittest

import tinytuya

from tuya_http import TimeoutRequests, install_request_timeout


class FakeRequests:
    """Stand-in for the `requests` module as tinytuya.Cloud uses it."""

    Request = object()

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def get(self, *args, **kwargs):
        self.calls.append(("get", args, kwargs))
        return "get-response"

    def post(self, *args, **kwargs):
        self.calls.append(("post", args, kwargs))
        return "post-response"


class TimeoutRequestsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.wrapped = FakeRequests()
        self.proxy = TimeoutRequests(self.wrapped, (3, 10))

    def test_injects_timeout_into_get(self) -> None:
        self.assertEqual(self.proxy.get("https://tuya", headers={}), "get-response")

        _, _, kwargs = self.wrapped.calls[0]
        self.assertEqual(kwargs["timeout"], (3, 10))

    def test_injects_timeout_into_post(self) -> None:
        self.assertEqual(self.proxy.post("https://tuya", data=b""), "post-response")

        _, _, kwargs = self.wrapped.calls[0]
        self.assertEqual(kwargs["timeout"], (3, 10))

    def test_does_not_override_an_explicit_timeout(self) -> None:
        self.proxy.get("https://tuya", timeout=99)

        _, _, kwargs = self.wrapped.calls[0]
        self.assertEqual(kwargs["timeout"], 99)

    def test_forwards_positional_arguments_unchanged(self) -> None:
        self.proxy.get("https://tuya", headers={"a": "b"})

        _, args, kwargs = self.wrapped.calls[0]
        self.assertEqual(args, ("https://tuya",))
        self.assertEqual(kwargs["headers"], {"a": "b"})

    def test_passes_through_other_module_attributes(self) -> None:
        # tinytuya.Cloud also reaches for requests.Request when signing.
        self.assertIs(self.proxy.Request, FakeRequests.Request)


class InstallRequestTimeoutTest(unittest.TestCase):
    """The patch targets tinytuya's real module, so save/restore around it."""

    def setUp(self) -> None:
        self.module = sys.modules[tinytuya.Cloud.__module__]
        self.original = self.module.requests
        self.addCleanup(setattr, self.module, "requests", self.original)

    def test_wraps_the_requests_module_tinytuya_calls(self) -> None:
        install_request_timeout(connect=3, read=10)

        self.assertIsInstance(self.module.requests, TimeoutRequests)
        self.assertEqual(self.module.requests.timeout, (3, 10))

    def test_patched_module_still_exposes_the_real_requests_api(self) -> None:
        install_request_timeout(connect=3, read=10)

        self.assertIs(self.module.requests.Request, self.original.Request)

    def test_is_idempotent(self) -> None:
        install_request_timeout(connect=3, read=10)
        install_request_timeout(connect=5, read=20)

        # Re-installing must retune in place, never wrap a wrapper — otherwise
        # each call adds a layer and the innermost (stale) timeout is the one
        # that reaches requests.
        self.assertIs(self.module.requests.wrapped, self.original)
        self.assertEqual(self.module.requests.timeout, (5, 20))

    def test_raises_when_the_target_module_has_no_requests_attribute(self) -> None:
        # Guards against a future tinytuya refactor silently dropping the patch
        # and taking the timeouts with it.
        del self.module.requests

        with self.assertRaises(RuntimeError):
            install_request_timeout(connect=3, read=10)


if __name__ == "__main__":
    unittest.main()
