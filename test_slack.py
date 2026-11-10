import os
import sys
import unittest
from unittest import mock

import tinytuya

import tuya_http


class FakeCloud:
    """Stand-in for tinytuya.Cloud so importing api makes no network calls."""

    def __init__(self, *args, **kwargs) -> None:
        self.sendcommand = mock.Mock(return_value={"success": True})

    def getdevices(self) -> list:
        return []


def import_api():
    env = {
        "TINYTUYA_API_KEY": "test-key",
        "TINYTUYA_API_SECRET": "test-secret",
        "SERVICE_API_KEY": "test-service-key",
        "HEALTH_PROBE_INTERVAL": "3600",
    }
    # The real timeout installer wraps tinytuya's module; FakeCloud makes no
    # HTTP calls, so it is skipped here and covered by test_tuya_http.
    with mock.patch.dict(os.environ, env), mock.patch.object(
        tinytuya, "Cloud", FakeCloud
    ), mock.patch.object(tuya_http, "install_request_timeout"):
        sys.modules.pop("api", None)
        import api
    return api


class DeviceCommandSlackTest(unittest.TestCase):
    """Success is logged only; every failure still posts to Slack."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.api = import_api()

    def setUp(self) -> None:
        self.client = self.api.app.test_client()
        self.cloud = FakeCloud()
        patcher_cloud = mock.patch.object(self.api, "cloud", self.cloud)
        patcher_slack = mock.patch.object(self.api, "send_message")
        patcher_cloud.start()
        self.send_message = patcher_slack.start()
        self.addCleanup(patcher_cloud.stop)
        self.addCleanup(patcher_slack.stop)

    def post_commands(self, body: dict, api_key: str = "test-service-key"):
        return self.client.post(
            "/devices/ats-device/commands",
            json=body,
            headers={"X-API-KEY": api_key},
        )

    def test_successful_command_does_not_post_to_slack(self) -> None:
        commands = [{"code": "power_mode", "value": "inverter_power"}]
        with self.assertLogs("tuya-bridge", level="INFO") as logs:
            response = self.post_commands({"commands": commands})

        self.assertEqual(response.status_code, 200)
        self.cloud.sendcommand.assert_called_once_with(
            "ats-device", {"commands": commands}
        )
        self.send_message.assert_not_called()
        self.assertTrue(
            any("Successfully handled" in line for line in logs.output), logs.output
        )

    def test_failed_command_posts_to_slack(self) -> None:
        self.cloud.sendcommand.side_effect = RuntimeError("tuya down")

        response = self.post_commands(
            {"commands": [{"code": "power_mode", "value": "crid_power"}]}
        )

        self.assertEqual(response.status_code, 500)
        self.send_message.assert_called_once()
        self.assertIn("Failed to send command", self.send_message.call_args.args[0])

    def test_missing_commands_posts_to_slack(self) -> None:
        response = self.post_commands({})

        self.assertEqual(response.status_code, 400)
        self.send_message.assert_called_once()

    def test_unauthorized_request_posts_to_slack(self) -> None:
        response = self.post_commands({"commands": []}, api_key="wrong")

        self.assertEqual(response.status_code, 401)
        self.send_message.assert_called_once_with("Unauthorized access attempt")


if __name__ == "__main__":
    unittest.main()
