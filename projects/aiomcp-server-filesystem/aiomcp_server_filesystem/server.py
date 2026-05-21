import base64
import difflib
import fnmatch
import json
import os
import shutil
import stat
from datetime import datetime
from pathlib import Path
from secrets import token_hex
from typing import Annotated, Any, Literal, Sequence

from aiomcp import McpServer
from aiomcp.transports.stdio import McpStdioServerTransport
from pydantic import BaseModel, Field

SERVER_NAME = "aiomcp-filesystem-server"

READ_ONLY_ANNOTATIONS = {"readOnlyHint": True}
CREATE_DIRECTORY_ANNOTATIONS = {
    "readOnlyHint": False,
    "idempotentHint": True,
    "destructiveHint": False,
}
WRITE_FILE_ANNOTATIONS = {
    "readOnlyHint": False,
    "idempotentHint": True,
    "destructiveHint": True,
}
MUTATING_FILE_ANNOTATIONS = {
    "readOnlyHint": False,
    "idempotentHint": False,
    "destructiveHint": True,
}


class EditOperation(BaseModel):
    oldText: Annotated[
        str,
        Field(description="Text to search for - must match exactly"),
    ]
    newText: Annotated[str, Field(description="Text to replace with")]


TailLineCount = Annotated[
    float | None,
    Field(description="If provided, returns only the last N lines of the file"),
]
HeadLineCount = Annotated[
    float | None,
    Field(description="If provided, returns only the first N lines of the file"),
]
MultipleFilePaths = Annotated[
    list[str],
    Field(
        description="Array of file paths to read. Each path must be a string pointing to a valid file within allowed directories.",
        json_schema_extra={"minItems": 1},
    ),
]
DryRun = Annotated[
    bool,
    Field(False, description="Preview changes using git-style diff format"),
]
SizeSort = Annotated[
    Literal["name", "size"],
    Field("name", description="Sort entries by name or size"),
]
ExcludePatterns = Annotated[list[str] | None, Field(default=[])]


class FilesystemServer:
    @staticmethod
    def _normalize_line_endings(text: str) -> str:
        return text.replace("\r\n", "\n")

    @staticmethod
    def _format_size(byte_count: int) -> str:
        units = ["B", "KB", "MB", "GB", "TB"]
        if byte_count == 0:
            return "0 B"

        unit_index = 0
        size = float(byte_count)
        while size >= 1024 and unit_index < len(units) - 1:
            size /= 1024
            unit_index += 1

        if unit_index == 0:
            return f"{byte_count} B"
        return f"{size:.2f} {units[unit_index]}"

    @staticmethod
    def _as_json_time(timestamp: float) -> str:
        return datetime.fromtimestamp(timestamp).isoformat()

    @staticmethod
    def _line_count(value: float | None, name: str) -> int | None:
        if value is None:
            return None
        if value < 0:
            raise ValueError(f"{name} must be greater than or equal to 0")
        if not float(value).is_integer():
            raise ValueError(f"{name} must be an integer")
        return int(value)

    def __init__(self, allowed_directories: Sequence[str]) -> None:
        self._allowed_paths = self._prepare_allowed_directories(allowed_directories)

    @property
    def allowed_directories(self) -> list[str]:
        return [str(path) for path in self._allowed_paths]

    def _prepare_allowed_directories(self, directories: Sequence[str]) -> list[Path]:
        allowed_paths: list[Path] = []
        for directory in directories:
            expanded = Path(os.path.expanduser(directory.strip().strip("'\"")))
            path = expanded if expanded.is_absolute() else Path.cwd() / expanded
            if not path.exists():
                raise ValueError(f"Allowed directory does not exist: {directory}")
            if not path.is_dir():
                raise ValueError(f"Allowed path is not a directory: {directory}")
            resolved = path.resolve(strict=True)
            if resolved not in allowed_paths:
                allowed_paths.append(resolved)

        if not allowed_paths:
            raise ValueError("At least one allowed directory must be provided")
        return allowed_paths

    def _path_key(self, path: Path) -> str:
        return os.path.normcase(os.path.abspath(os.fspath(path)))

    def _is_within_allowed(self, path: Path) -> bool:
        requested = self._path_key(path)
        for allowed_path in self._allowed_paths:
            allowed = self._path_key(allowed_path)
            try:
                if os.path.commonpath([requested, allowed]) == allowed:
                    return True
            except ValueError:
                continue
        return False

    def _candidate_path(self, requested_path: str) -> Path:
        cleaned = requested_path.strip().strip("'\"")
        expanded = os.path.expanduser(cleaned)
        candidate = Path(expanded)
        if candidate.is_absolute():
            return candidate

        for allowed_path in self._allowed_paths:
            joined = (allowed_path / candidate).resolve(strict=False)
            if self._is_within_allowed(joined):
                return joined
        return (self._allowed_paths[0] / candidate).resolve(strict=False)

    def _validate_path(
        self,
        requested_path: str,
        *,
        must_exist: bool = False,
        allow_missing_ancestors: bool = False,
    ) -> Path:
        candidate = self._candidate_path(requested_path).resolve(strict=False)
        if not self._is_within_allowed(candidate):
            raise ValueError(
                "Access denied - path outside allowed directories: "
                f"{candidate} not in {', '.join(self.allowed_directories)}"
            )

        if candidate.exists():
            real_path = candidate.resolve(strict=True)
            if not self._is_within_allowed(real_path):
                raise ValueError(
                    "Access denied - symlink target outside allowed directories: "
                    f"{real_path} not in {', '.join(self.allowed_directories)}"
                )
            return real_path

        if must_exist:
            raise FileNotFoundError(f"Path does not exist: {candidate}")

        parent = candidate.parent
        if not allow_missing_ancestors and not parent.exists():
            raise FileNotFoundError(f"Parent directory does not exist: {parent}")

        ancestor = parent
        while not ancestor.exists() and ancestor != ancestor.parent:
            ancestor = ancestor.parent
        real_ancestor = ancestor.resolve(strict=True)
        if not self._is_within_allowed(real_ancestor):
            raise ValueError(
                "Access denied - parent directory outside allowed directories: "
                f"{real_ancestor} not in {', '.join(self.allowed_directories)}"
            )
        return candidate

    def _read_text(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")

    def _write_text_atomic(self, path: Path, content: str) -> None:
        temp_path = path.with_name(f"{path.name}.{token_hex(16)}.tmp")
        try:
            temp_path.write_text(content, encoding="utf-8")
            os.replace(temp_path, path)
        finally:
            if temp_path.exists():
                temp_path.unlink()

    def _matches_pattern(self, relative_path: str, pattern: str) -> bool:
        normalized_path = relative_path.replace(os.sep, "/")
        normalized_pattern = pattern.replace(os.sep, "/")
        return fnmatch.fnmatchcase(normalized_path, normalized_pattern)

    def _is_excluded(self, relative_path: str, exclude_patterns: Sequence[str]) -> bool:
        normalized_path = relative_path.replace(os.sep, "/")
        name = normalized_path.rsplit("/", 1)[-1]
        for pattern in exclude_patterns:
            normalized_pattern = pattern.replace(os.sep, "/")
            if fnmatch.fnmatchcase(normalized_path, normalized_pattern):
                return True
            if fnmatch.fnmatchcase(name, normalized_pattern):
                return True
            if fnmatch.fnmatchcase(normalized_path, f"**/{normalized_pattern}"):
                return True
            if fnmatch.fnmatchcase(normalized_path, f"**/{normalized_pattern}/**"):
                return True
        return False

    def read_text_file(
        self,
        path: str,
        tail: TailLineCount = None,
        head: HeadLineCount = None,
    ) -> Any:
        valid_path = self._validate_path(path, must_exist=True)
        if head is not None and tail is not None:
            raise ValueError(
                "Cannot specify both head and tail parameters simultaneously"
            )

        head_count = self._line_count(head, "head")
        tail_count = self._line_count(tail, "tail")

        content = self._read_text(valid_path)
        if head_count is not None:
            content = "\n".join(content.splitlines()[:head_count])
        elif tail_count is not None:
            content = "\n".join(
                content.splitlines()[-tail_count:] if tail_count else []
            )
        return [{"type": "text", "text": content}]

    def read_media_file(self, path: str) -> Any:
        valid_path = self._validate_path(path, must_exist=True)
        mime_types = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".bmp": "image/bmp",
            ".svg": "image/svg+xml",
            ".mp3": "audio/mpeg",
            ".wav": "audio/wav",
            ".ogg": "audio/ogg",
            ".flac": "audio/flac",
        }
        mime_type = mime_types.get(
            valid_path.suffix.lower(), "application/octet-stream"
        )
        if mime_type.startswith("image/"):
            content_type = "image"
        elif mime_type.startswith("audio/"):
            content_type = "audio"
        else:
            raise ValueError(f"Unsupported media file type: {valid_path.suffix}")
        return [
            {
                "type": content_type,
                "data": base64.b64encode(valid_path.read_bytes()).decode("ascii"),
                "mimeType": mime_type,
            }
        ]

    def read_multiple_files(self, paths: MultipleFilePaths) -> Any:
        if not paths:
            raise ValueError("At least one file path must be provided")

        results: list[str] = []
        for file_path in paths:
            try:
                valid_path = self._validate_path(file_path, must_exist=True)
                results.append(f"{file_path}:\n{self._read_text(valid_path)}\n")
            except Exception as exc:
                results.append(f"{file_path}: Error - {exc}")
        return [{"type": "text", "text": "\n---\n".join(results)}]

    def write_file(self, path: str, content: str) -> Any:
        valid_path = self._validate_path(path)
        self._write_text_atomic(valid_path, content)
        return [{"type": "text", "text": f"Successfully wrote to {path}"}]

    def edit_file(
        self,
        path: str,
        edits: list[EditOperation],
        dryRun: DryRun = False,
    ) -> Any:
        valid_path = self._validate_path(path, must_exist=True)
        original_content = self._normalize_line_endings(self._read_text(valid_path))
        modified_content = original_content

        for edit in edits:
            if isinstance(edit, dict):
                old_text_raw = edit["oldText"]
                new_text_raw = edit["newText"]
            else:
                old_text_raw = edit.oldText
                new_text_raw = edit.newText

            old_text = self._normalize_line_endings(old_text_raw)
            new_text = self._normalize_line_endings(new_text_raw)

            if old_text in modified_content:
                modified_content = modified_content.replace(old_text, new_text, 1)
                continue

            old_lines = old_text.split("\n")
            content_lines = modified_content.split("\n")
            match_found = False

            for index in range(0, len(content_lines) - len(old_lines) + 1):
                potential_match = content_lines[index : index + len(old_lines)]
                if all(
                    old_line.strip() == content_line.strip()
                    for old_line, content_line in zip(old_lines, potential_match)
                ):
                    original_indent = content_lines[index][
                        : len(content_lines[index]) - len(content_lines[index].lstrip())
                    ]
                    new_lines: list[str] = []
                    for line_index, line in enumerate(new_text.split("\n")):
                        if line_index == 0:
                            new_lines.append(original_indent + line.lstrip())
                            continue
                        old_indent = (
                            old_lines[line_index][
                                : len(old_lines[line_index])
                                - len(old_lines[line_index].lstrip())
                            ]
                            if line_index < len(old_lines)
                            else ""
                        )
                        new_indent = line[: len(line) - len(line.lstrip())]
                        if old_indent and new_indent:
                            relative_indent = max(0, len(new_indent) - len(old_indent))
                            new_lines.append(
                                original_indent + " " * relative_indent + line.lstrip()
                            )
                        else:
                            new_lines.append(line)

                    content_lines[index : index + len(old_lines)] = new_lines
                    modified_content = "\n".join(content_lines)
                    match_found = True
                    break

            if not match_found:
                raise ValueError(
                    f"Could not find exact match for edit:\n{old_text_raw}"
                )

        diff = "\n".join(
            difflib.unified_diff(
                original_content.splitlines(),
                modified_content.splitlines(),
                fromfile=str(valid_path),
                tofile=str(valid_path),
                fromfiledate="original",
                tofiledate="modified",
                lineterm="",
            )
        )
        formatted_diff = f"```diff\n{diff}\n```\n\n"

        if not dryRun:
            self._write_text_atomic(valid_path, modified_content)
        return [{"type": "text", "text": formatted_diff}]

    def create_directory(self, path: str) -> Any:
        valid_path = self._validate_path(path, allow_missing_ancestors=True)
        valid_path.mkdir(parents=True, exist_ok=True)
        return [{"type": "text", "text": f"Successfully created directory {path}"}]

    def list_directory(self, path: str) -> Any:
        valid_path = self._validate_path(path, must_exist=True)
        entries = sorted(valid_path.iterdir(), key=lambda entry: entry.name.lower())
        text = "\n".join(
            f"{'[DIR]' if entry.is_dir() else '[FILE]'} {entry.name}"
            for entry in entries
        )
        return [{"type": "text", "text": text}]

    def list_directory_with_sizes(self, path: str, sortBy: SizeSort = "name") -> Any:
        valid_path = self._validate_path(path, must_exist=True)
        detailed_entries = []
        for entry in valid_path.iterdir():
            try:
                entry_stats = entry.stat()
                size = entry_stats.st_size
            except OSError:
                size = 0
            detailed_entries.append(
                {"name": entry.name, "isDirectory": entry.is_dir(), "size": size}
            )

        if sortBy == "size":
            sorted_entries = sorted(
                detailed_entries, key=lambda item: item["size"], reverse=True
            )
        else:
            sorted_entries = sorted(
                detailed_entries, key=lambda item: str(item["name"]).lower()
            )

        formatted_entries = []
        for entry in sorted_entries:
            prefix = "[DIR]" if entry["isDirectory"] else "[FILE]"
            size_text = (
                "" if entry["isDirectory"] else self._format_size(int(entry["size"]))
            )
            formatted_entries.append(
                f"{prefix} {str(entry['name']):<30} {size_text:>10}"
            )

        total_files = sum(1 for entry in detailed_entries if not entry["isDirectory"])
        total_dirs = sum(1 for entry in detailed_entries if entry["isDirectory"])
        total_size = sum(
            int(entry["size"]) for entry in detailed_entries if not entry["isDirectory"]
        )
        summary = [
            "",
            f"Total: {total_files} files, {total_dirs} directories",
            f"Combined size: {self._format_size(total_size)}",
        ]
        return [{"type": "text", "text": "\n".join([*formatted_entries, *summary])}]

    def directory_tree(self, path: str, excludePatterns: ExcludePatterns = None) -> Any:
        root_path = self._validate_path(path, must_exist=True)
        exclude_patterns = excludePatterns or []

        def build_tree(current_path: Path) -> list[dict[str, object]]:
            valid_path = self._validate_path(str(current_path), must_exist=True)
            entries: list[dict[str, object]] = []
            for entry in sorted(
                valid_path.iterdir(), key=lambda item: item.name.lower()
            ):
                relative_path = os.path.relpath(entry, root_path)
                if self._is_excluded(relative_path, exclude_patterns):
                    continue
                entry_data: dict[str, object] = {
                    "name": entry.name,
                    "type": "directory" if entry.is_dir() else "file",
                }
                if entry.is_dir():
                    entry_data["children"] = build_tree(entry)
                entries.append(entry_data)
            return entries

        return [{"type": "text", "text": json.dumps(build_tree(root_path), indent=2)}]

    def move_file(self, source: str, destination: str) -> Any:
        valid_source = self._validate_path(source, must_exist=True)
        valid_destination = self._validate_path(destination)
        if valid_destination.exists():
            raise FileExistsError(f"Destination already exists: {destination}")
        shutil.move(str(valid_source), str(valid_destination))
        return [
            {"type": "text", "text": f"Successfully moved {source} to {destination}"}
        ]

    def search_files(
        self, path: str, pattern: str, excludePatterns: ExcludePatterns = None
    ) -> Any:
        root_path = self._validate_path(path, must_exist=True)
        exclude_patterns = excludePatterns or []
        results: list[str] = []

        for current_root, dirnames, filenames in os.walk(root_path):
            current_path = Path(current_root)
            dirnames[:] = [
                dirname
                for dirname in dirnames
                if not self._is_excluded(
                    os.path.relpath(current_path / dirname, root_path), exclude_patterns
                )
            ]
            for name in [*dirnames, *filenames]:
                full_path = current_path / name
                try:
                    self._validate_path(str(full_path), must_exist=True)
                except Exception:
                    continue
                relative_path = os.path.relpath(full_path, root_path)
                if self._is_excluded(relative_path, exclude_patterns):
                    continue
                if self._matches_pattern(relative_path, pattern):
                    results.append(str(full_path))

        return [
            {
                "type": "text",
                "text": "\n".join(results) if results else "No matches found",
            }
        ]

    def get_file_info(self, path: str) -> Any:
        valid_path = self._validate_path(path, must_exist=True)
        stats = valid_path.stat()
        info = {
            "size": stats.st_size,
            "created": self._as_json_time(stats.st_ctime),
            "modified": self._as_json_time(stats.st_mtime),
            "accessed": self._as_json_time(stats.st_atime),
            "isDirectory": valid_path.is_dir(),
            "isFile": valid_path.is_file(),
            "permissions": oct(stat.S_IMODE(stats.st_mode))[-3:],
        }
        return [
            {
                "type": "text",
                "text": "\n".join(f"{key}: {value}" for key, value in info.items()),
            }
        ]

    def list_allowed_directories(self) -> Any:
        return [
            {
                "type": "text",
                "text": "Allowed directories:\n" + "\n".join(self.allowed_directories),
            }
        ]


async def register_tools(
    server: McpServer, allowed_directories: Sequence[str]
) -> FilesystemServer:
    state = FilesystemServer(allowed_directories)

    await server.register_tool(
        func=state.read_text_file,
        alias="read_file",
        title="Read File (Deprecated)",
        description="Read the complete contents of a file as text. DEPRECATED: Use read_text_file instead.",
        annotations=READ_ONLY_ANNOTATIONS,
    )
    await server.register_tool(
        func=state.read_text_file,
        title="Read Text File",
        description="Read the complete contents of a file from the file system as text. Handles various text encodings and provides detailed error messages if the file cannot be read. Use this tool when you need to examine the contents of a single file. Use the 'head' parameter to read only the first N lines of a file, or the 'tail' parameter to read only the last N lines of a file. Operates on the file as text regardless of extension. Only works within allowed directories.",
        annotations=READ_ONLY_ANNOTATIONS,
    )
    await server.register_tool(
        func=state.read_media_file,
        title="Read Media File",
        description="Read an image or audio file. Returns the base64 encoded data and MIME type. Only works within allowed directories.",
        annotations=READ_ONLY_ANNOTATIONS,
    )
    await server.register_tool(
        func=state.read_multiple_files,
        title="Read Multiple Files",
        description="Read the contents of multiple files simultaneously. This is more efficient than reading files one by one when you need to analyze or compare multiple files. Each file's content is returned with its path as a reference. Failed reads for individual files won't stop the entire operation. Only works within allowed directories.",
        annotations=READ_ONLY_ANNOTATIONS,
    )
    await server.register_tool(
        func=state.write_file,
        title="Write File",
        description="Create a new file or completely overwrite an existing file with new content. Use with caution as it will overwrite existing files without warning. Handles text content with proper encoding. Only works within allowed directories.",
        annotations=WRITE_FILE_ANNOTATIONS,
    )
    await server.register_tool(
        func=state.edit_file,
        title="Edit File",
        description="Make line-based edits to a text file. Each edit replaces exact line sequences with new content. Returns a git-style diff showing the changes made. Only works within allowed directories.",
        annotations=MUTATING_FILE_ANNOTATIONS,
    )
    await server.register_tool(
        func=state.create_directory,
        title="Create Directory",
        description="Create a new directory or ensure a directory exists. Can create multiple nested directories in one operation. If the directory already exists, this operation will succeed silently. Perfect for setting up directory structures for projects or ensuring required paths exist. Only works within allowed directories.",
        annotations=CREATE_DIRECTORY_ANNOTATIONS,
    )
    await server.register_tool(
        func=state.list_directory,
        title="List Directory",
        description="Get a detailed listing of all files and directories in a specified path. Results clearly distinguish between files and directories with [FILE] and [DIR] prefixes. This tool is essential for understanding directory structure and finding specific files within a directory. Only works within allowed directories.",
        annotations=READ_ONLY_ANNOTATIONS,
    )
    await server.register_tool(
        func=state.list_directory_with_sizes,
        title="List Directory with Sizes",
        description="Get a detailed listing of all files and directories in a specified path, including sizes. Results clearly distinguish between files and directories with [FILE] and [DIR] prefixes. This tool is useful for understanding directory structure and finding specific files within a directory. Only works within allowed directories.",
        annotations=READ_ONLY_ANNOTATIONS,
    )
    await server.register_tool(
        func=state.directory_tree,
        title="Directory Tree",
        description="Get a recursive tree view of files and directories as a JSON structure. Each entry includes 'name', 'type' (file/directory), and 'children' for directories. Files have no children array, while directories always have a children array (which may be empty). The output is formatted with 2-space indentation for readability. Only works within allowed directories.",
        annotations=READ_ONLY_ANNOTATIONS,
    )
    await server.register_tool(
        func=state.move_file,
        title="Move File",
        description="Move or rename files and directories. Can move files between directories and rename them in a single operation. If the destination exists, the operation will fail. Works across different directories and can be used for simple renaming within the same directory. Both source and destination must be within allowed directories.",
        annotations=MUTATING_FILE_ANNOTATIONS,
    )
    await server.register_tool(
        func=state.search_files,
        title="Search Files",
        description="Recursively search for files and directories matching a pattern. The patterns should be glob-style patterns that match paths relative to the working directory. Use pattern like '*.ext' to match files in current directory, and '**/*.ext' to match files in all subdirectories. Returns full paths to all matching items. Great for finding files when you don't know their exact location. Only searches within allowed directories.",
        annotations=READ_ONLY_ANNOTATIONS,
    )
    await server.register_tool(
        func=state.get_file_info,
        title="Get File Info",
        description="Retrieve detailed metadata about a file or directory. Returns comprehensive information including size, creation time, last modified time, permissions, and type. This tool is perfect for understanding file characteristics without reading the actual content. Only works within allowed directories.",
        annotations=READ_ONLY_ANNOTATIONS,
    )
    await server.register_tool(
        func=state.list_allowed_directories,
        title="List Allowed Directories",
        description="Returns the list of directories that this server is allowed to access. Subdirectories within these allowed directories are also accessible. Use this to understand which directories and their nested paths are available before trying to access files.",
        annotations=READ_ONLY_ANNOTATIONS,
    )
    return state


async def host_stdio(allowed_directories: Sequence[str]) -> None:
    server = McpServer(SERVER_NAME)
    await register_tools(server, allowed_directories)

    transport = McpStdioServerTransport()
    await server.host(transport)


async def host_http(url: str, allowed_directories: Sequence[str]) -> None:
    server = McpServer(SERVER_NAME)
    await register_tools(server, allowed_directories)

    await server.host(url)
