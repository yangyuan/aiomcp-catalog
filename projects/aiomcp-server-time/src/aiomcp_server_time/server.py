from datetime import datetime, timedelta
from typing import Annotated, Any

from aiomcp import McpServer
from aiomcp.transports.stdio import McpStdioServerTransport
from pydantic import BaseModel, Field
from tzlocal import get_localzone_name
from zoneinfo import ZoneInfo


class TimeResult(BaseModel):
    timezone: str
    datetime: str
    day_of_week: str
    is_dst: bool


class TimeConversionResult(BaseModel):
    source: TimeResult
    target: TimeResult
    time_difference: str


TIME_TOOL_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}


SERVER_NAME = "mcp-time"


def get_timezone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except Exception as exc:
        raise ValueError(f"Invalid timezone: {exc}") from exc


def get_local_timezone(local_timezone_override: str | None = None) -> ZoneInfo:
    if local_timezone_override:
        return get_timezone(local_timezone_override)

    local_timezone_name = get_localzone_name()
    if local_timezone_name is not None:
        return get_timezone(local_timezone_name)
    return ZoneInfo("UTC")


def get_current_time(
    timezone: Annotated[
        str,
        Field(
            description="IANA timezone name (e.g., 'America/New_York', 'Europe/London'). Use '{CURRENT_TIMEZONE}' as local timezone if no timezone provided by the user."
        ),
    ],
) -> Any:
    resolved_timezone = get_timezone(timezone)
    current_time = datetime.now(resolved_timezone)

    return TimeResult(
        timezone=timezone,
        datetime=current_time.isoformat(timespec="seconds"),
        day_of_week=current_time.strftime("%A"),
        is_dst=bool(current_time.dst()),
    )


def convert_time(
    source_timezone: Annotated[
        str,
        Field(
            description="Source IANA timezone name (e.g., 'America/New_York', 'Europe/London'). Use '{CURRENT_TIMEZONE}' as local timezone if no source timezone provided by the user."
        ),
    ],
    time: Annotated[
        str,
        Field(description="Time to convert in 24-hour format (HH:MM)"),
    ],
    target_timezone: Annotated[
        str,
        Field(
            description="Target IANA timezone name (e.g., 'Asia/Tokyo', 'America/San_Francisco'). Use '{CURRENT_TIMEZONE}' as local timezone if no target timezone provided by the user."
        ),
    ],
) -> Any:
    resolved_source_timezone = get_timezone(source_timezone)
    resolved_target_timezone = get_timezone(target_timezone)

    try:
        parsed_time = datetime.strptime(time, "%H:%M").time()
    except ValueError as exc:
        raise ValueError(
            "Invalid time format. Expected HH:MM [24-hour format]"
        ) from exc

    now = datetime.now(resolved_source_timezone)
    source_time = datetime(
        now.year,
        now.month,
        now.day,
        parsed_time.hour,
        parsed_time.minute,
        tzinfo=resolved_source_timezone,
    )
    target_time = source_time.astimezone(resolved_target_timezone)

    source_offset = source_time.utcoffset() or timedelta()
    target_offset = target_time.utcoffset() or timedelta()
    hours_difference = (target_offset - source_offset).total_seconds() / 3600
    if hours_difference.is_integer():
        time_difference = f"{hours_difference:+.1f}h"
    else:
        time_difference = f"{hours_difference:+.2f}".rstrip("0").rstrip(".") + "h"

    return TimeConversionResult(
        source=TimeResult(
            timezone=source_timezone,
            datetime=source_time.isoformat(timespec="seconds"),
            day_of_week=source_time.strftime("%A"),
            is_dst=bool(source_time.dst()),
        ),
        target=TimeResult(
            timezone=target_timezone,
            datetime=target_time.isoformat(timespec="seconds"),
            day_of_week=target_time.strftime("%A"),
            is_dst=bool(target_time.dst()),
        ),
        time_difference=time_difference,
    )


async def register_tools(server: McpServer, local_timezone: str | None = None) -> None:
    resolved_local_timezone = str(get_local_timezone(local_timezone))
    format_map = {"CURRENT_TIMEZONE": resolved_local_timezone}

    await server.register_tool(
        func=get_current_time,
        description="Get current time in a specific timezones",
        annotations=TIME_TOOL_ANNOTATIONS,
        format_map=format_map,
    )
    await server.register_tool(
        func=convert_time,
        description="Convert time between timezones",
        annotations=TIME_TOOL_ANNOTATIONS,
        format_map=format_map,
    )


async def host_stdio(local_timezone: str | None = None) -> None:
    server = McpServer(SERVER_NAME)
    await register_tools(server, local_timezone=local_timezone)

    transport = McpStdioServerTransport()
    await server.host(transport)


async def host_http(url: str, local_timezone: str | None = None) -> None:
    server = McpServer(SERVER_NAME)
    await register_tools(server, local_timezone=local_timezone)

    await server.host(url)
