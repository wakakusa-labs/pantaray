"""AGENTS.md instructions for the Action.

Pantaray's default instructions ship with the package and ride in the stable
prompt head ahead of every file the user wrote. The Pantaray-wide file
(``~/.pantaray/AGENTS.md``) is read once when the Action starts and rides in the
stable prompt head. Repository files are attached to the
result of the first tool call that works in their directory: every AGENTS.md from
the project root down to that directory, each at most once per Action. The
attached set lives in the checkpointed context, so a resumed Action does not send
a file twice. Both follow Codex (``codex-rs/core/src/agents_md.rs``): the project
root is the nearest ancestor holding ``.git``, the text is read as lossy UTF-8,
and blank files are skipped.
"""

from __future__ import annotations

import logging
import os
import stat
from collections.abc import Iterator, Mapping
from pathlib import Path

from pantaray_agents.agents.action_agent.runtime.state import ActionAgentState
from pantaray_agents.agents.action_agent.runtime.state.context import ensure_context
from pantaray_agents.local_runtime.descriptor_access import (
    DescriptorPathError,
    DescriptorPathMissingError,
    open_regular_file_descriptor,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_common import (
    APPLY_PATCH_TOOL_ID,
    BASH_TOOL_ID,
    GLOB_TOOL_ID,
    GREP_TOOL_ID,
    LIST_TOOL_ID,
    READ_TOOL_ID,
    RUN_PYTHON_TOOL_ID,
    BrokerContext,
    BrokerPolicyError,
)
from pantaray_agents.local_runtime.tooling.brokering.manifest_paths import (
    ResolvedManifestPath,
)
from pantaray_agents.local_runtime.tooling.brokering.tool_path_policy import (
    resolve_read_tool_path,
)
from pantaray_agents.schema.agent.base import JSONValue

logger = logging.getLogger(__name__)

AGENTS_MD_FILENAME = "AGENTS.md"
PANTARAY_AGENTS_MD_DISPLAY_DIR = "~/.pantaray"
# Codex's default project_doc_max_bytes (codex-rs/core/src/config/mod.rs,
# AGENTS_MD_MAX_BYTES): room for any real instruction file while one runaway
# file cannot crowd out the context. It bounds the Pantaray-wide file and, as a
# shared budget, each batch attached to one tool result.
AGENTS_MD_MAX_BYTES = 32 * 1024
_PROJECT_ROOT_MARKER = ".git"


def _render_block(heading: str, data: bytes) -> str:
    # Codex's wrapper (codex-rs/core/src/context/user_instructions.rs).
    text = data.decode("utf-8", errors="replace")
    return (
        f"# AGENTS.md instructions {heading}\n\n<INSTRUCTIONS>\n{text}\n</INSTRUCTIONS>"
    )


# Read at import so a package built without the file fails when the helper
# starts, not when the first Action does.
PANTARAY_DEFAULT_AGENTS_MD = _render_block(
    "(Pantaray default)",
    (
        Path(__file__).parents[3] / "prompts" / "action" / "default_agents.md"
    ).read_bytes(),
)


def load_pantaray_agents_md() -> str:
    """The Pantaray-wide instructions block, or ``""`` when there is none."""

    path = Path.home() / ".pantaray" / AGENTS_MD_FILENAME
    try:
        # O_NONBLOCK so a FIFO planted under the name cannot block the open.
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return ""
    except OSError:
        logger.warning("Skipping unreadable AGENTS.md: %s", path)
        return ""
    data = _read_regular_file(descriptor, path, max_bytes=AGENTS_MD_MAX_BYTES)
    if data is None:
        return ""
    return _render_block(f"for {PANTARAY_AGENTS_MD_DISPLAY_DIR}", data)


def attach_repository_agents_md(
    state: ActionAgentState,
    *,
    tool_id: str,
    args: Mapping[str, JSONValue],
    read_context: BrokerContext,
) -> str | None:
    """Claim and render the AGENTS.md files this call is the first to reach.

    ``read_context`` is the Action's read-tool context, so a file is attached only
    when the ``read`` tool could open it: inside the read scope, outside private
    app storage, and not through a symlink that leaves its root. Runs without an
    ``await`` so parallel sibling calls sharing one state cannot claim twice.
    """

    context = ensure_context(state)
    attached = list(context.get("agents_md_attached_paths", []))
    blocks: list[str] = []
    remaining = AGENTS_MD_MAX_BYTES
    for raw_path in _touched_paths(tool_id, args):
        try:
            touched = resolve_read_tool_path(
                context=read_context, raw_path=raw_path, must_exist=True
            )
        except (BrokerPolicyError, OSError):
            continue
        for scope, root, relative in _instruction_files(read_context, touched):
            key = str(root / relative)
            if key in attached or remaining == 0:
                continue
            data = _read_repository_file(root, relative, max_bytes=remaining)
            if data is None:
                continue
            attached.append(key)
            remaining -= len(data)
            blocks.append(_render_block(f"for {scope}", data))
    context["agents_md_attached_paths"] = attached
    return "\n\n".join(blocks) if blocks else None


def _touched_paths(tool_id: str, args: Mapping[str, JSONValue]) -> Iterator[str]:
    """Raw paths of the files or directories the call works in."""

    if tool_id in {READ_TOOL_ID, LIST_TOOL_ID}:
        yield str(args["path"])
    elif tool_id in {GLOB_TOOL_ID, GREP_TOOL_ID}:
        yield str(args["base_path"])
    elif tool_id == APPLY_PATCH_TOOL_ID:
        changes = args["changes"]
        assert isinstance(changes, list)
        for change in changes:
            assert isinstance(change, dict)
            # The parent, because a deleted file no longer resolves.
            yield str(Path(str(change["path"])).parent)
    elif tool_id in {BASH_TOOL_ID, RUN_PYTHON_TOOL_ID}:
        # Without a cwd the command runs in the Action's scratch directory.
        cwd = args.get("cwd")
        if isinstance(cwd, str):
            yield cwd


def _instruction_files(
    context: BrokerContext, touched: ResolvedManifestPath
) -> Iterator[tuple[Path, Path, Path]]:
    """Readable AGENTS.md files from the project root down to the touched dir.

    Each is ``(directory it governs, validated root, real path below it)``. The
    root is the one ``read`` validated against (the registered folder), not the
    project root, so no directory between them is trusted when opening. The real
    path, not the link, is opened, so an in-repository ``AGENTS.md -> CLAUDE.md``
    still works.
    """

    directory = touched.path if touched.path.is_dir() else touched.path.parent
    in_workspace = touched.root in context.manifest_roots
    # Never above the registered folder. A full-access path outside every folder
    # is bounded only by the filesystem, and outside a repository it contributes
    # just its own directory, as in Codex.
    lineage = _lineage(
        directory, touched.root.canonical_real_path if in_workspace else None
    )
    project_root = next(
        (
            ancestor
            for ancestor in reversed(lineage)
            if (ancestor / _PROJECT_ROOT_MARKER).exists()
        ),
        lineage[0] if in_workspace else directory,
    )
    for ancestor in lineage[lineage.index(project_root) :]:
        candidate = ancestor / AGENTS_MD_FILENAME
        if not candidate.is_file():
            continue
        try:
            readable = resolve_read_tool_path(
                context=context,
                raw_path=str(candidate),
                must_exist=True,
                must_be_file=True,
            )
        except (BrokerPolicyError, OSError):
            continue
        if readable.path.is_relative_to(project_root):
            yield (
                ancestor,
                readable.root.canonical_real_path,
                Path(readable.root_relative_path),
            )


def _lineage(directory: Path, boundary: Path | None) -> list[Path]:
    """Directories from the boundary (or filesystem root) down to ``directory``."""

    lineage = [directory]
    while lineage[-1] != boundary and lineage[-1].parent != lineage[-1]:
        lineage.append(lineage[-1].parent)
    return lineage[::-1]


def _read_repository_file(
    root: Path, relative: Path, *, max_bytes: int
) -> bytes | None:
    """Open the validated real path without following any symlink below the root.

    A file or directory swapped for a symlink after validation fails to open and
    is skipped, so the swap cannot redirect the read outside what was checked.
    """

    path = root / relative
    try:
        descriptor = open_regular_file_descriptor(
            root_path=root, relative_path=str(relative)
        )
    except DescriptorPathMissingError:
        return None
    except (DescriptorPathError, OSError):
        logger.warning("Skipping unreadable AGENTS.md: %s", path)
        return None
    return _read_regular_file(descriptor, path, max_bytes=max_bytes)


def _read_regular_file(descriptor: int, path: Path, *, max_bytes: int) -> bytes | None:
    """At most ``max_bytes`` of a regular, non-blank file; ``None`` otherwise.

    Takes ownership of ``descriptor``. Never raises: an instruction file that
    cannot be read is skipped, not a failure of the call or the Action.
    """

    with os.fdopen(descriptor, "rb") as file:
        try:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                return None
            data = file.read(max_bytes + 1)
        except OSError:
            logger.warning("Skipping unreadable AGENTS.md: %s", path)
            return None
    if len(data) > max_bytes:
        logger.warning(
            "AGENTS.md exceeds the remaining %d-byte budget; truncating: %s",
            max_bytes,
            path,
        )
        data = data[:max_bytes]
    if not data.decode("utf-8", errors="replace").strip():
        return None
    return data


__all__ = [
    "AGENTS_MD_MAX_BYTES",
    "PANTARAY_DEFAULT_AGENTS_MD",
    "attach_repository_agents_md",
    "load_pantaray_agents_md",
]
