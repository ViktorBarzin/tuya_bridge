import unittest

from notifications import command_processed_message


class CommandProcessedMessageTest(unittest.TestCase):
    def test_describes_device_and_command_once(self) -> None:
        message = command_processed_message(
            "ats-device", [{"code": "power_mode", "value": "inverter_power"}]
        )

        self.assertEqual(
            message,
            "Processed command for ats-device: "
            "[{'code': 'power_mode', 'value': 'inverter_power'}]",
        )


if __name__ == "__main__":
    unittest.main()
