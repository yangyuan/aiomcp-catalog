def main() -> None:
    import argparse
    import asyncio

    from aiomcp_server_filesystem.server import host_http, host_stdio

    parser = argparse.ArgumentParser(description="Filesystem MCP server.")
    parser.add_argument(
        "allowed_directories",
        nargs="+",
        metavar="allowed-directory",
        help="Directory this server is allowed to access.",
    )
    parser.add_argument(
        "--http",
        metavar="URL",
        help="Host an HTTP MCP endpoint at URL instead of using stdio.",
    )

    args = parser.parse_args()

    try:
        if args.http:
            asyncio.run(host_http(args.http, args.allowed_directories))
        else:
            asyncio.run(host_stdio(args.allowed_directories))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
