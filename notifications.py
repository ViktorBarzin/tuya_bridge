from typing import Any


def command_processed_message(device_id: str, commands: list[dict[str, Any]]) -> str:
    return f"Processed command for {device_id}: {commands}"
