import base64
import json
from pathlib import Path

import pytest

from aiomcp import McpClient, McpServer
from aiomcp_server_filesystem.server import SERVER_NAME, register_tools


async def create_test_server(root: Path) -> McpServer:
    server = McpServer(SERVER_NAME)
    await register_tools(server, [str(root)])
    return server


@pytest.mark.asyncio
async def test_aiomcp_server_exposes_reference_filesystem_tools(tmp_path):
    server = await create_test_server(tmp_path)
    tools = {tool.name: tool for tool in await server.list_tools()}

    assert set(tools) == {
        "read_file",
        "read_text_file",
        "read_media_file",
        "read_multiple_files",
        "write_file",
        "edit_file",
        "create_directory",
        "list_directory",
        "list_directory_with_sizes",
        "directory_tree",
        "move_file",
        "search_files",
        "get_file_info",
        "list_allowed_directories",
    }
    assert tools["read_file"].description == (
        "Read the complete contents of a file as text. DEPRECATED: Use read_text_file instead."
    )
    assert tools["read_text_file"].description.startswith(
        "Read the complete contents of a file from the file system as text."
    )
    assert tools["read_text_file"].outputSchema is None
    read_schema = tools["read_text_file"].inputSchema.model_dump(exclude_none=True)
    assert read_schema["properties"]["tail"]["description"] == (
        "If provided, returns only the last N lines of the file"
    )
    assert read_schema["properties"]["head"]["description"] == (
        "If provided, returns only the first N lines of the file"
    )
    assert tools["write_file"].annotations.model_dump(exclude_none=True) == {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
    }
    assert tools["edit_file"].annotations.model_dump(exclude_none=True) == {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
    }


@pytest.mark.asyncio
async def test_aiomcp_client_can_use_filesystem_tools(tmp_path):
    (tmp_path / "notes.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    (tmp_path / "image.png").write_bytes(b"png bytes")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "match.txt").write_text("needle", encoding="utf-8")

    server = await create_test_server(tmp_path)
    client = McpClient()
    await client.initialize(server)

    try:
        read_result = await client.invoke(
            "read_text_file",
            {"path": "notes.txt", "head": 2},
        )
        media_result = await client.invoke("read_media_file", {"path": "image.png"})
        list_result = await client.invoke("list_directory", {"path": "."})
        search_result = await client.invoke(
            "search_files",
            {"path": ".", "pattern": "nested/*.txt"},
        )
        tree_result = await client.invoke("directory_tree", {"path": "."})
        edit_result = await client.invoke(
            "edit_file",
            {
                "path": "notes.txt",
                "edits": [{"oldText": "beta", "newText": "delta"}],
                "dryRun": True,
            },
        )
        write_result = await client.invoke(
            "write_file",
            {"path": "created.txt", "content": "created"},
        )
    finally:
        await client.close()

    assert read_result == [{"type": "text", "text": "alpha\nbeta"}]
    assert media_result[0] == {
        "type": "image",
        "data": base64.b64encode(b"png bytes").decode("ascii"),
        "mimeType": "image/png",
    }
    assert "[FILE] notes.txt" in list_result[0]["text"]
    assert str(tmp_path / "nested" / "match.txt") in search_result[0]["text"]
    tree = json.loads(tree_result[0]["text"])
    assert {entry["name"] for entry in tree} >= {"notes.txt", "nested"}
    assert "-beta" in edit_result[0]["text"]
    assert write_result == [
        {"type": "text", "text": "Successfully wrote to created.txt"}
    ]
    assert (tmp_path / "created.txt").read_text(encoding="utf-8") == "created"


@pytest.mark.asyncio
async def test_path_validation_rejects_paths_outside_allowed_directory(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    server = await create_test_server(tmp_path)
    client = McpClient()
    await client.initialize(server)

    try:
        result = await client.invoke_result(
            "read_text_file", {"path": str(outside)}, timeout=1
        )
    finally:
        await client.close()
        outside.unlink(missing_ok=True)

    assert result.isError is True
    assert (
        "Access denied - path outside allowed directories" in result.content[0]["text"]
    )
