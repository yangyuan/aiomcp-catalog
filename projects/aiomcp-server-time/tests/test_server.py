import json
import re
from datetime import datetime
from unittest.mock import patch

import pytest

from aiomcp import McpClient, McpServer
from aiomcp_server_time.server import (
    convert_time,
    get_current_time,
    get_local_timezone,
    register_tools,
)


def freeze_time(test_time: str):
    frozen_time = datetime.fromisoformat(test_time)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, timezone=None):
            if timezone is None:
                return frozen_time.replace(tzinfo=None)
            return frozen_time.astimezone(timezone)

    return patch("aiomcp_server_time.server.datetime", FrozenDateTime)


async def create_test_server(local_timezone: str | None = None) -> McpServer:
    server = McpServer("mcp-time")
    await register_tools(server, local_timezone=local_timezone)
    return server


@pytest.mark.parametrize(
    ("test_time", "timezone", "expected"),
    [
        (
            "2024-01-01 12:00:00+00:00",
            "Europe/Warsaw",
            {
                "timezone": "Europe/Warsaw",
                "datetime": "2024-01-01T13:00:00+01:00",
                "day_of_week": "Monday",
                "is_dst": False,
            },
        ),
        (
            "2024-03-31 12:00:00+00:00",
            "America/New_York",
            {
                "timezone": "America/New_York",
                "datetime": "2024-03-31T08:00:00-04:00",
                "day_of_week": "Sunday",
                "is_dst": True,
            },
        ),
    ],
)
def test_get_current_time(test_time, timezone, expected):
    with freeze_time(test_time):
        result = get_current_time(timezone)

    assert result.model_dump() == expected


@pytest.mark.parametrize(
    ("test_time", "source_timezone", "time_str", "target_timezone", "expected"),
    [
        (
            "2024-01-01 00:00:00+00:00",
            "Europe/London",
            "12:00",
            "Europe/Warsaw",
            {
                "source": {
                    "timezone": "Europe/London",
                    "datetime": "2024-01-01T12:00:00+00:00",
                    "day_of_week": "Monday",
                    "is_dst": False,
                },
                "target": {
                    "timezone": "Europe/Warsaw",
                    "datetime": "2024-01-01T13:00:00+01:00",
                    "day_of_week": "Monday",
                    "is_dst": False,
                },
                "time_difference": "+1.0h",
            },
        ),
        (
            "2024-01-01 00:00:00+00:00",
            "Europe/Warsaw",
            "12:00",
            "Asia/Kathmandu",
            {
                "source": {
                    "timezone": "Europe/Warsaw",
                    "datetime": "2024-01-01T12:00:00+01:00",
                    "day_of_week": "Monday",
                    "is_dst": False,
                },
                "target": {
                    "timezone": "Asia/Kathmandu",
                    "datetime": "2024-01-01T16:45:00+05:45",
                    "day_of_week": "Monday",
                    "is_dst": False,
                },
                "time_difference": "+4.75h",
            },
        ),
    ],
)
def test_convert_time(test_time, source_timezone, time_str, target_timezone, expected):
    with freeze_time(test_time):
        result = convert_time(source_timezone, time_str, target_timezone)

    assert result.model_dump() == expected


@pytest.mark.parametrize(
    ("source_timezone", "time_str", "target_timezone", "expected_error"),
    [
        (
            "invalid_timezone",
            "12:00",
            "Europe/London",
            "Invalid timezone: 'No time zone found with key invalid_timezone'",
        ),
        (
            "Europe/Warsaw",
            "25:00",
            "Europe/London",
            "Invalid time format. Expected HH:MM [24-hour format]",
        ),
    ],
)
def test_convert_time_errors(
    source_timezone, time_str, target_timezone, expected_error
):
    with pytest.raises(ValueError, match=re.escape(expected_error)):
        convert_time(source_timezone, time_str, target_timezone)


def test_get_local_timezone_with_override():
    assert str(get_local_timezone("America/New_York")) == "America/New_York"


@patch("aiomcp_server_time.server.get_localzone_name")
def test_get_local_timezone_defaults_to_utc(mock_get_localzone):
    mock_get_localzone.return_value = None

    assert str(get_local_timezone()) == "UTC"


@pytest.mark.asyncio
async def test_aiomcp_server_exposes_reference_tools():
    server = await create_test_server("Europe/Warsaw")
    tools = await server.list_tools()
    tool_names = {tool.name for tool in tools}

    assert tool_names == {"get_current_time", "convert_time"}

    current_time_tool = next(tool for tool in tools if tool.name == "get_current_time")
    assert current_time_tool.description == "Get current time in a specific timezones"
    assert current_time_tool.outputSchema is None
    current_time_input_schema = current_time_tool.inputSchema.model_dump(
        exclude_none=True
    )
    assert current_time_input_schema["properties"]["timezone"]["description"] == (
        "IANA timezone name (e.g., 'America/New_York', 'Europe/London'). "
        "Use 'Europe/Warsaw' as local timezone if no timezone provided by the user."
    )

    convert_tool = next(tool for tool in tools if tool.name == "convert_time")
    assert convert_tool.outputSchema is None
    input_schema = convert_tool.inputSchema.model_dump(exclude_none=True)
    annotations = convert_tool.annotations.model_dump(exclude_none=True)
    assert input_schema["required"] == ["source_timezone", "time", "target_timezone"]
    assert input_schema["properties"]["source_timezone"]["description"] == (
        "Source IANA timezone name (e.g., 'America/New_York', 'Europe/London'). "
        "Use 'Europe/Warsaw' as local timezone if no source timezone provided by the user."
    )
    assert input_schema["properties"]["time"]["description"] == (
        "Time to convert in 24-hour format (HH:MM)"
    )
    assert input_schema["properties"]["target_timezone"]["description"] == (
        "Target IANA timezone name (e.g., 'Asia/Tokyo', 'America/San_Francisco'). "
        "Use 'Europe/Warsaw' as local timezone if no target timezone provided by the user."
    )
    assert annotations == {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }


@pytest.mark.asyncio
async def test_aiomcp_client_can_call_convert_time():
    server = await create_test_server()
    client = McpClient()
    await client.initialize(server)

    try:
        with freeze_time("2024-01-01 00:00:00+00:00"):
            result = await client.invoke(
                "convert_time",
                {
                    "source_timezone": "Europe/Warsaw",
                    "time": "12:00",
                    "target_timezone": "Asia/Kathmandu",
                },
            )
    finally:
        await client.close()

    assert result[0]["type"] == "text"
    payload = json.loads(result[0]["text"])
    assert payload["target"]["datetime"] == "2024-01-01T16:45:00+05:45"
    assert payload["time_difference"] == "+4.75h"
