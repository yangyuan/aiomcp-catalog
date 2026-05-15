def main() -> None:
    import argparse
    import asyncio

    from aiomcp_server_time.server import host_http, host_stdio

    parser = argparse.ArgumentParser(
        description="Time and timezone conversion MCP server."
    )
    parser.add_argument(
        "--http",
        metavar="URL",
        help="Host an HTTP MCP endpoint at URL instead of using stdio.",
    )
    parser.add_argument("--local-timezone", type=str, help="Override local timezone")

    args = parser.parse_args()

    try:
        if args.http:
            asyncio.run(host_http(args.http, local_timezone=args.local_timezone))
        else:
            asyncio.run(host_stdio(local_timezone=args.local_timezone))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
