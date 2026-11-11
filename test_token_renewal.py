"""A token renewal that raises must not leave a client stuck (infra#126).

tinytuya's `_gettoken()` clears `token` before it asks for a new one. When that
request raises (a ReadTimeout from tuya_http's timeout), `token` stays None and
every later call returns `self.error` (None) without trying again, so
`getdevices()` raises TypeError until the worker restarts.
"""

import unittest
from unittest import mock

import requests
import tinytuya

import prometheus_exporter
from test_slack import import_api
from tuya_http import ensure_token

FRESH_TOKEN = {"success": True, "t": 0, "result": {"access_token": "fresh"}}
DEVICES = [{"id": "dev"}]


class StuckCloud:
    """tinytuya.Cloud as it is left after a renewal raised: no token, every
    call fails the way getdevices() does in production, until _gettoken()."""

    def __init__(self) -> None:
        self.token: str | None = None
        self.renewals = 0
        self.sendcommand = mock.Mock(side_effect=self._call)

    def _gettoken(self) -> str:
        self.renewals += 1
        self.token = "fresh"
        return self.token

    def _call(self, *args: object) -> object:
        if not self.token:
            raise TypeError("'NoneType' object is not iterable")
        return {"success": True, "result": True}

    def getdevices(self) -> object:
        self._call()
        return DEVICES

    def getstatus(self, device_id: str) -> object:
        return self._call(device_id)

    def getfunctions(self, device_id: str) -> object:
        return self._call(device_id)


class EnsureTokenTest(unittest.TestCase):
    """Runs tinytuya's own token code; only the HTTP layer is faked."""

    def make_stuck_cloud(self) -> tinytuya.Cloud:
        # initial_token skips the constructor's network call.
        cloud = tinytuya.Cloud(
            apiRegion="eu", apiKey="k", apiSecret="s", initial_token="old"
        )
        with mock.patch.object(
            cloud, "_tuyaplatform", side_effect=requests.exceptions.ReadTimeout()
        ):
            with self.assertRaises(requests.exceptions.ReadTimeout):
                cloud._gettoken()
        return cloud

    def test_failed_renewal_leaves_tinytuya_without_a_token(self) -> None:
        # The bug itself: tinytuya does not retry, it returns its (None) error.
        cloud = self.make_stuck_cloud()

        self.assertIsNone(cloud.token)
        self.assertIsNone(cloud.cloudrequest("/v1.0/devices"))

    def test_reacquires_the_token_so_the_next_call_succeeds(self) -> None:
        cloud = self.make_stuck_cloud()
        platform = mock.Mock(side_effect=[FRESH_TOKEN, {"success": True}])

        with mock.patch.object(cloud, "_tuyaplatform", platform):
            ensure_token(cloud)
            result = cloud.cloudrequest("/v1.0/devices")

        self.assertEqual(cloud.token, "fresh")
        self.assertEqual(result, {"success": True})

    def test_leaves_a_valid_token_alone(self) -> None:
        cloud = tinytuya.Cloud(
            apiRegion="eu", apiKey="k", apiSecret="s", initial_token="old"
        )

        with mock.patch.object(cloud, "_tuyaplatform") as platform:
            ensure_token(cloud)

        platform.assert_not_called()
        self.assertEqual(cloud.token, "old")


class StopProbe(Exception):
    pass


class ApiRecoversTest(unittest.TestCase):
    """Every path that calls Tuya re-acquires a missing token first."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.api = import_api()

    def setUp(self) -> None:
        self.cloud = StuckCloud()
        self.client = self.api.app.test_client()
        for patcher in (
            mock.patch.object(self.api, "cloud", self.cloud),
            mock.patch.object(self.api, "send_message"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def get(self, path: str):
        return self.client.get(path, headers={"X-API-KEY": "test-service-key"})

    def test_probe_loop_recovers(self) -> None:
        with mock.patch.object(self.api, "_probe_cloud", self.cloud), mock.patch.object(
            self.api, "_last_tuya_success", 0.0
        ), mock.patch.object(self.api, "_last_tuya_error", None), mock.patch.object(
            self.api.time, "sleep", side_effect=StopProbe
        ):
            with self.assertRaises(StopProbe):
                self.api._tuya_probe_loop()

            self.assertIsNone(self.api._last_tuya_error)
            self.assertGreater(self.api._last_tuya_success, 0.0)

    def test_list_devices_recovers(self) -> None:
        response = self.get("/devices")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["devices"], DEVICES)

    def test_device_status_recovers(self) -> None:
        self.assertEqual(self.get("/devices/dev/status").status_code, 200)

    def test_device_functions_recovers(self) -> None:
        self.assertEqual(self.get("/devices/dev/functions").status_code, 200)

    def test_device_commands_recovers(self) -> None:
        response = self.client.post(
            "/devices/dev/commands",
            json={"commands": [{"code": "switch_1", "value": True}]},
            headers={"X-API-KEY": "test-service-key"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.cloud.renewals, 1)


class StatusMetrics:
    def __init__(self, registry: object, cloud: StuckCloud, device_id: str) -> None:
        self.cloud = cloud
        self.device_id = device_id

    def collect(self) -> object:
        return self.cloud.getstatus(self.device_id)


class CollectMetricsRecoversTest(unittest.TestCase):
    def setUp(self) -> None:
        prometheus_exporter.cache.clear()
        self.addCleanup(prometheus_exporter.cache.clear)

    def test_collect_metrics_recovers(self) -> None:
        cloud = StuckCloud()

        with mock.patch.dict(
            prometheus_exporter.device_id_to_metrics, {"dev": StatusMetrics}
        ):
            result = prometheus_exporter.collect_metrics(cloud, "dev")

        self.assertEqual(result, {"success": True, "result": True})
        self.assertEqual(cloud.renewals, 1)


if __name__ == "__main__":
    unittest.main()
