from __future__ import annotations

from fnmatch import fnmatch
from pathlib import PurePosixPath

from pantaray_agents.local_runtime.memory_catalog.draft import (
    validate_memory_documents,
)
from pantaray_agents.local_runtime.memory_catalog.models import MemoryDocument
from pantaray_agents.local_runtime.tooling.fs_sandbox import (
    EditablePathPolicy,
    SandboxPathError,
    SandboxPathErrorCode,
    SandboxShellError,
    TextFileError,
    TextFileErrorCode,
)
from pantaray_agents.local_runtime.tooling.fs_sandbox.shell_validation import (
    TEST_FILE_PREDICATES,
    TEST_STRING_COMPARATORS,
    ParsedSandboxShellCommand,
    parse_sandbox_shell_commands,
)
from pantaray_agents.tools.contract import BrokerPolicyError
from pantaray_agents.tools.files.text_lines import read_text_value_lines

from .bounded_workspace_io import (
    MAX_READ_LINE_LIMIT,
    SEARCH_MAX_FILE_BYTES,
    SEARCH_MAX_FILES,
    SEARCH_MAX_LINE_CHARS,
    SEARCH_MAX_TOTAL_BYTES,
    SearchMatch,
    SearchResult,
    SearchTruncationReason,
    TextPage,
)

_MAX_SHELL_OUTPUT_CHARS = 20_000
_SHELL_ROOT = "/memory_draft"
_ALLOWED_LS_OPTIONS = frozenset(("-1", "-a", "-al", "-la", "-l", "-R", "--"))


def validate_memory_document_path(path: str) -> str:
    """Return a canonical relative POSIX document path or reject it."""
    try:
        validate_memory_documents((MemoryDocument(path, ""),))
    except ValueError as exc:
        raise SandboxPathError(
            SandboxPathErrorCode.PATH_DENIED,
            "path must be a canonical relative POSIX path",
        ) from exc
    return path


def require_editable_document_path(
    *, path: str, editable_policy: EditablePathPolicy
) -> str:
    canonical = validate_memory_document_path(path)
    if not editable_policy.allows(canonical):
        raise SandboxPathError(
            SandboxPathErrorCode.PATH_DENIED,
            f"path is outside editable policy: {canonical}",
        )
    return canonical


def require_new_document_path(
    *, documents: tuple[MemoryDocument, ...], path: str
) -> None:
    if any(document.source_path == path for document in documents):
        raise TextFileError(
            TextFileErrorCode.TARGET_EXISTS,
            f"path already exists; use apply_patch to edit existing files: {path}",
        )
    if any(document.source_path.startswith(f"{path}/") for document in documents):
        raise SandboxPathError(
            SandboxPathErrorCode.TARGET_NOT_FILE,
            f"path is not a file: {path}",
        )
    parts = PurePosixPath(path).parts
    parents = {"/".join(parts[:index]) for index in range(1, len(parts))}
    if any(document.source_path in parents for document in documents):
        raise SandboxPathError(
            SandboxPathErrorCode.PATH_DENIED,
            f"parent path is a memory file: {path}",
        )


def read_draft_text_page(
    *,
    documents: tuple[MemoryDocument, ...],
    path: str,
    offset: int,
    column: int,
    limit: int,
) -> TextPage:
    if offset < 1:
        raise ValueError("offset must be >= 1")
    if column < 1:
        raise ValueError("column must be >= 1")
    if not 1 <= limit <= MAX_READ_LINE_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_READ_LINE_LIMIT}")
    canonical = validate_memory_document_path(path)
    document = _document_by_path(documents, canonical)
    try:
        page = read_text_value_lines(
            text=document.content,
            offset=offset,
            column=column,
            limit=limit,
        )
    except BrokerPolicyError as exc:
        raise TextFileError(exc.code, str(exc)) from exc
    return TextPage(
        relative_path=canonical,
        text=page.content,
        offset=offset,
        column=column,
        end_line=page.end_line,
        end_column=page.end_column,
        total_lines=page.total_lines,
        next_offset=page.next_offset,
        next_column=page.next_column,
        truncated=page.truncated,
        truncation_reason=page.truncation_reason,
        retry_hint=page.retry_hint,
    )


def draft_document_content(
    *, documents: tuple[MemoryDocument, ...], path: str
) -> tuple[str, str]:
    canonical = validate_memory_document_path(path)
    return canonical, _document_by_path(documents, canonical).content


def search_draft_documents(
    *, documents: tuple[MemoryDocument, ...], query: str, limit: int
) -> SearchResult:
    if not query.strip():
        raise ValueError("query must not be blank")
    if limit < 1:
        raise ValueError("limit must be >= 1")
    matches: list[SearchMatch] = []
    bytes_seen = 0
    reason: SearchTruncationReason | None = None
    skipped_files = 0
    for files_seen, document in enumerate(
        sorted(documents, key=lambda item: item.source_path), start=1
    ):
        if files_seen > SEARCH_MAX_FILES:
            reason = SearchTruncationReason.FILE_LIMIT
            break
        size = len(document.content.encode("utf-8"))
        if query in document.source_path:
            matches.append(SearchMatch(document.source_path, 0, ""))
        if len(matches) >= limit:
            reason = SearchTruncationReason.MATCH_LIMIT
            break
        if size > SEARCH_MAX_FILE_BYTES:
            skipped_files += 1
            continue
        if bytes_seen + size > SEARCH_MAX_TOTAL_BYTES:
            reason = SearchTruncationReason.BYTE_LIMIT
            break
        bytes_seen += size
        for line_number, line in enumerate(document.content.splitlines(), start=1):
            if query not in line:
                continue
            visible = line
            if len(visible) > SEARCH_MAX_LINE_CHARS:
                visible = f"{visible[:SEARCH_MAX_LINE_CHARS]}...[line truncated]"
            matches.append(SearchMatch(document.source_path, line_number, visible))
            if len(matches) >= limit:
                reason = SearchTruncationReason.MATCH_LIMIT
                break
        if reason is not None:
            break
    return SearchResult(tuple(matches), reason, skipped_files)


def run_draft_shell_command(
    *, documents: tuple[MemoryDocument, ...], cmd: str
) -> tuple[int, str, str]:
    normalized_cmd = cmd.strip()
    commands = parse_sandbox_shell_commands(normalized_cmd)
    for command in commands:
        _require_read_only(command)
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    exit_code = 0
    for command in commands:
        if command.separator == "and" and exit_code != 0:
            continue
        exit_code, stdout, stderr = _execute_command(
            documents=documents,
            command=command,
        )
        stdout_parts.append(stdout)
        stderr_parts.append(stderr)
    return (
        exit_code,
        _truncate_shell_output("".join(stdout_parts)),
        _truncate_shell_output("".join(stderr_parts)),
    )


def _document_by_path(
    documents: tuple[MemoryDocument, ...], path: str
) -> MemoryDocument:
    document = next((item for item in documents if item.source_path == path), None)
    if document is None:
        raise SandboxPathError(
            SandboxPathErrorCode.PATH_NOT_FOUND,
            f"path does not exist: {path}",
        )
    return document


def _require_read_only(command: ParsedSandboxShellCommand) -> None:
    if command.argv[0] not in {"find", "ls", "pwd", "test"}:
        raise SandboxShellError(
            "COMMAND_DENIED", "memory editor shell permits only find, ls, pwd, and test"
        )


def _execute_command(
    *, documents: tuple[MemoryDocument, ...], command: ParsedSandboxShellCommand
) -> tuple[int, str, str]:
    name = command.argv[0]
    if name == "find":
        return _execute_find(documents=documents, argv=command.argv)
    if name == "ls":
        return _execute_ls(documents=documents, argv=command.argv)
    if name == "pwd":
        if len(command.argv) != 1:
            raise SandboxShellError("INVALID_COMMAND", "pwd does not accept arguments")
        return 0, f"{_SHELL_ROOT}\n", ""
    return _execute_test(documents=documents, argv=command.argv)


def _execute_find(
    *, documents: tuple[MemoryDocument, ...], argv: tuple[str, ...]
) -> tuple[int, str, str]:
    args = argv[1:]
    expression_start = next(
        (index for index, token in enumerate(args) if token.startswith("-")),
        len(args),
    )
    raw_paths = args[:expression_start] or (".",)
    max_depth, min_depth, kind, name_pattern, path_pattern = _find_filters(
        args[expression_start:]
    )
    files, directories = _tree_paths(documents)
    output: set[str] = set()
    for raw_path in raw_paths:
        base = _validate_shell_path(raw_path, allow_root=True)
        if base not in files and base not in directories:
            raise SandboxShellError(
                "PATH_NOT_FOUND", f"path does not exist: {raw_path}"
            )
        for path in files | directories:
            if not _is_at_or_below(path=path, base=base):
                continue
            depth = _relative_depth(path=path, base=base)
            if depth < min_depth or (max_depth is not None and depth > max_depth):
                continue
            if kind == "f" and path not in files:
                continue
            if kind == "d" and path not in directories:
                continue
            output_path = "." if path == "." else path
            if name_pattern is not None and not fnmatch(
                PurePosixPath(output_path).name, name_pattern
            ):
                continue
            if path_pattern is not None and not fnmatch(output_path, path_pattern):
                continue
            output.add(output_path)
    text = "\n".join(sorted(output))
    return 0, f"{text}\n" if text else "", ""


def _execute_ls(
    *, documents: tuple[MemoryDocument, ...], argv: tuple[str, ...]
) -> tuple[int, str, str]:
    raw_paths = _command_paths(argv, allowed_options=_ALLOWED_LS_OPTIONS) or (".",)
    recursive = "-R" in argv
    include_hidden = any(option in argv for option in ("-a", "-al", "-la"))
    files, directories = _tree_paths(documents)
    output: list[str] = []
    for raw_path in raw_paths:
        path = _validate_shell_path(raw_path, allow_root=True)
        candidates: tuple[str, ...]
        if path in files:
            candidates = (path,)
        elif path in directories:
            candidates = tuple(
                candidate
                for candidate in files | directories
                if candidate != "."
                and _is_at_or_below(path=candidate, base=path)
                and (recursive or _relative_depth(path=candidate, base=path) == 1)
            )
        else:
            raise SandboxShellError(
                "PATH_NOT_FOUND", f"path does not exist: {raw_path}"
            )
        output.extend(
            candidate
            for candidate in sorted(candidates)
            if include_hidden or not PurePosixPath(candidate).name.startswith(".")
        )
    text = "\n".join(output)
    return 0, f"{text}\n" if text else "", ""


def _execute_test(
    *, documents: tuple[MemoryDocument, ...], argv: tuple[str, ...]
) -> tuple[int, str, str]:
    args = argv[1:]
    files, directories = _tree_paths(documents)
    if len(args) == 1:
        _validate_shell_path(args[0], allow_root=True)
        result = bool(args[0])
    elif len(args) == 2 and args[0] in TEST_FILE_PREDICATES:
        path = _validate_shell_path(args[1], allow_root=True)
        exists = path in files or path in directories
        result = {
            "-d": path in directories,
            "-e": exists,
            "-f": path in files,
            "-r": exists,
            "-s": path in files and bool(_document_by_path(documents, path).content),
            "-w": exists,
        }[args[0]]
    elif len(args) == 3 and args[1] in TEST_STRING_COMPARATORS:
        _validate_shell_path(args[0], allow_root=True)
        _validate_shell_path(args[2], allow_root=True)
        equal = args[0] == args[2]
        result = equal == (args[1] == "=")
    else:
        raise SandboxShellError("INVALID_COMMAND", "test: unsupported expression")
    return (0 if result else 1), "", ""


def _find_filters(
    expression: tuple[str, ...],
) -> tuple[int | None, int, str | None, str | None, str | None]:
    max_depth: int | None = None
    min_depth = 0
    kind: str | None = None
    name_pattern: str | None = None
    path_pattern: str | None = None
    index = 0
    while index < len(expression):
        token = expression[index]
        if token == "-print":
            index += 1
            continue
        if token in {"-maxdepth", "-mindepth"}:
            value = _option_value(expression, index, token)
            if not value.isdigit():
                raise SandboxShellError(
                    "INVALID_COMMAND", f"{token} requires a non-negative integer"
                )
            if token == "-maxdepth":
                max_depth = int(value)
            else:
                min_depth = int(value)
            index += 2
            continue
        if token == "-type":
            value = _option_value(expression, index, token)
            if value not in {"f", "d"}:
                raise SandboxShellError(
                    "INVALID_COMMAND", "-type requires one of: d, f"
                )
            kind = value
            index += 2
            continue
        if token in {"-name", "-path"}:
            value = _option_value(expression, index, token)
            if value.startswith("/") or ".." in PurePosixPath(value).parts:
                raise SandboxShellError(
                    "PATH_DENIED", f"pattern escapes the memory draft: {value}"
                )
            if token == "-name":
                name_pattern = value
            else:
                path_pattern = value
            index += 2
            continue
        raise SandboxShellError(
            "COMMAND_DENIED", f"unsupported find expression: {token}"
        )
    return max_depth, min_depth, kind, name_pattern, path_pattern


def _option_value(tokens: tuple[str, ...], index: int, option: str) -> str:
    if index + 1 >= len(tokens):
        raise SandboxShellError("INVALID_COMMAND", f"{option} requires a value")
    return tokens[index + 1]


def _command_paths(
    argv: tuple[str, ...], *, allowed_options: frozenset[str]
) -> tuple[str, ...]:
    paths: list[str] = []
    parsing_options = True
    for token in argv[1:]:
        if parsing_options and token == "--":
            parsing_options = False
            continue
        if parsing_options and token.startswith("-"):
            if token not in allowed_options:
                raise SandboxShellError(
                    "COMMAND_DENIED", f"unsupported option for {argv[0]}: {token}"
                )
            continue
        paths.append(token)
    return tuple(paths)


def _validate_shell_path(path: str, *, allow_root: bool) -> str:
    if allow_root and path in {".", "./"}:
        return "."
    return validate_memory_document_path(path)


def _tree_paths(
    documents: tuple[MemoryDocument, ...],
) -> tuple[set[str], set[str]]:
    files = {document.source_path for document in documents}
    directories = {"."}
    for source_path in files:
        parent = PurePosixPath(source_path).parent
        while str(parent) != ".":
            directories.add(str(parent))
            parent = parent.parent
    return files, directories


def _is_at_or_below(*, path: str, base: str) -> bool:
    return base == "." or path == base or path.startswith(f"{base}/")


def _relative_depth(*, path: str, base: str) -> int:
    if path == base:
        return 0
    relative = path if base == "." else path.removeprefix(f"{base}/")
    return len(PurePosixPath(relative).parts)


def _truncate_shell_output(text: str) -> str:
    if len(text) <= _MAX_SHELL_OUTPUT_CHARS:
        return text
    suffix = "\n...[truncated]"
    return f"{text[: _MAX_SHELL_OUTPUT_CHARS - len(suffix)]}{suffix}"


__all__ = [
    "draft_document_content",
    "read_draft_text_page",
    "require_editable_document_path",
    "require_new_document_path",
    "run_draft_shell_command",
    "search_draft_documents",
    "validate_memory_document_path",
]
