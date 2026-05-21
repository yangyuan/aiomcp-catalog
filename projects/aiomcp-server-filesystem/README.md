# aiomcp-server-filesystem

Filesystem MCP server powered by `aiomcp`.

## Usage

```bash
aiomcp-server-filesystem /path/to/allowed-directory [/path/to/another-directory]
```

The server intentionally uses command-line allowed directories for access control. Dynamic MCP Roots updates from the reference server are not implemented here.

## Tools

- `read_file`
- `read_text_file`
- `read_media_file`
- `read_multiple_files`
- `write_file`
- `edit_file`
- `create_directory`
- `list_directory`
- `list_directory_with_sizes`
- `directory_tree`
- `move_file`
- `search_files`
- `get_file_info`
- `list_allowed_directories`