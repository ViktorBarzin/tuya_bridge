import unittest

from prometheus_client import CollectorRegistry

from metrics_definition import MetricsDefinition, _is_auth_error


class _StubMetrics(MetricsDefinition):
    """Minimal concrete subclass to exercise the shared _extract_datapoints()."""

    @property
    def metrics_schema(self):
        return {}

    def collect(self):  # pragma: no cover - not exercised by these tests
        return self.registry


def _extract(result):
    """Run _extract_datapoints against a fresh registry; return (gauge, data)."""
    registry = CollectorRegistry()
    stub = _StubMetrics(registry=registry, cloud=None, device_id="dev")
    data = stub._extract_datapoints(result)
    return registry.get_sample_value("tuya_cloud_up"), data


class IsAuthErrorTest(unittest.TestCase):
    def test_permission_and_subscription_codes_are_auth(self):
        # Renewing the subscription / rotating the key is the real fix for these.
        for code in (1106, 28841002, 909):
            with self.subTest(code=code):
                self.assertTrue(_is_auth_error({"success": False, "code": code}))

    def test_subscription_message_is_auth_even_with_unknown_code(self):
        # Robust to Tuya code drift: an unmistakable auth/subscription message
        # counts as "certain" even if the numeric code is not in the allowlist.
        self.assertTrue(
            _is_auth_error(
                {
                    "success": False,
                    "code": 999999,
                    "msg": "No permissions. Your subscription has expired, please renew.",
                }
            )
        )

    def test_http_4xx_status_is_auth(self):
        self.assertTrue(_is_auth_error({"success": False, "status_code": 401}))
        self.assertTrue(_is_auth_error({"success": False, "http_status": 403}))

    def test_transient_501_is_not_auth(self):
        # THE case that misfired: a Tuya server-side 501 must never look like auth.
        self.assertFalse(
            _is_auth_error(
                {"success": False, "code": 501, "msg": "request fail with unkown error"}
            )
        )

    def test_other_transient_and_ratelimit_and_wrapper_codes_are_not_auth(self):
        for code in (500, 502, 503, 504, 2001, 2008, 902, 910, 911):
            with self.subTest(code=code):
                self.assertFalse(_is_auth_error({"success": False, "code": code}))

    def test_http_5xx_status_is_not_auth(self):
        self.assertFalse(_is_auth_error({"success": False, "status_code": 502}))

    def test_unknown_code_is_not_auth(self):
        # "only when certain": an unrecognised code must not raise the alert.
        self.assertFalse(_is_auth_error({"success": False, "code": 123456}))

    def test_non_dict_is_not_auth(self):
        for result in (None, "boom", [], 501):
            with self.subTest(result=result):
                self.assertFalse(_is_auth_error(result))


class ExtractDatapointsGaugeTest(unittest.TestCase):
    def test_success_sets_up_1_and_returns_data(self):
        up, data = _extract({"success": True, "result": [{"code": "x", "value": 1}]})
        self.assertEqual(up, 1.0)
        self.assertEqual(data, [{"code": "x", "value": 1}])

    def test_transient_501_keeps_up_1_and_returns_none(self):
        up, data = _extract(
            {"success": False, "code": 501, "msg": "request fail with unkown error"}
        )
        self.assertEqual(up, 1.0)
        self.assertIsNone(data)

    def test_expired_subscription_sets_up_0(self):
        up, data = _extract({"success": False, "code": 28841002, "msg": "No permissions"})
        self.assertEqual(up, 0.0)
        self.assertIsNone(data)

    def test_permission_deny_sets_up_0(self):
        up, _ = _extract({"success": False, "code": 1106, "msg": "permission deny"})
        self.assertEqual(up, 0.0)

    def test_malformed_or_empty_response_keeps_up_1(self):
        # No result list and nothing that proves an auth problem -> do not alarm.
        for result in ({"success": False}, {}, None):
            with self.subTest(result=result):
                up, data = _extract(result)
                self.assertEqual(up, 1.0)
                self.assertIsNone(data)


if __name__ == "__main__":
    unittest.main()
