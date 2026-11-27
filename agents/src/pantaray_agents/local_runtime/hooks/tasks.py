"""Tasks an Action schedules: one Markdown file each in ~/.pantaray/tasks.

A file holds one conversation hook (``load_conversation_hook``). Its id is
``task.<file stem>``. Only the files an Action's task tools recorded are read;
any other file in the folder is not a task. A task fires only while its file
still hashes to the content the user said yes to.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pantaray_agents.hooks.definitions import (
    HookDefinition,
    HookDefinitionError,
    RunConversation,
    load_conversation_hook,
)
from pantaray_agents.local_runtime.runtime.action_message_models import (
    action_hook_request_text,
)
from pantaray_agents.schema.agent.action_message import (
    ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS,
)
from pantaray_agents.tools.files.pantaray_tasks import pantaray_tasks_dir

TASK_HOOK_PREFIX: Final = "task."
_STEM: Final = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
# A task's turn belongs to the owner settled on, and waits for a connection
# rather than fail without one; the file names no gates of its own.
TASK_GATES: Final = ("owner_settled", "background_can_send")


@dataclass(frozen=True, slots=True)
class Task:
    hook: HookDefinition
    run: RunConversation
    content_hash: str
    text: str

    @property
    def hook_id(self) -> str:
        return self.hook.id


@dataclass(frozen=True, slots=True)
class InvalidTask:
    """A task file that cannot fire until it is fixed."""

    hook_id: str
    content_hash: str
    text: str
    error: str


TaskFile = Task | InvalidTask


def task_file_path(hook_id: str) -> Path:
    """Where the task ``hook_id`` is written."""

    return pantaray_tasks_dir() / f"{hook_id.removeprefix(TASK_HOOK_PREFIX)}.md"


def read_task_files(hook_ids: Iterable[str]) -> tuple[TaskFile, ...]:
    """The recorded tasks' files as they are now, in id order; a task whose
    file is gone is left out."""

    # Design limit: every call reads each file; fine for tens of small files.
    # Cache by file mtime when a hooks tick exceeds 50 ms.
    return tuple(
        task
        for task in (_read(task_file_path(hook_id)) for hook_id in sorted(hook_ids))
        if task is not None
    )


def _read(path: Path) -> TaskFile | None:
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as error:
        return _invalid(path.stem, b"", f"cannot be read: {error.strerror}")
    return parse_task_file(path.stem, content)


def _invalid(stem: str, content: bytes, error: str) -> InvalidTask:
    return InvalidTask(
        hook_id=f"{TASK_HOOK_PREFIX}{stem}",
        content_hash=hashlib.sha256(content).hexdigest(),
        text=content.decode("utf-8", errors="replace"),
        error=error,
    )


def parse_task_file(stem: str, content: bytes) -> TaskFile:
    """The task a file named ``<stem>.md`` holding ``content`` defines."""

    hook_id = f"{TASK_HOOK_PREFIX}{stem}"
    content_hash = hashlib.sha256(content).hexdigest()
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return _invalid(stem, content, "the file is not UTF-8 text")
    if _STEM.fullmatch(stem) is None:
        return _invalid(
            stem, content, "a task file name uses only a-z, 0-9, - and _, up to 64"
        )
    try:
        hook = load_conversation_hook(
            hook_id,
            text,
            gate_names=frozenset(TASK_GATES),
            # A task fires on a time; no event wakes it.
            event_names=frozenset(),
            requires=TASK_GATES,
        )
    except HookDefinitionError as error:
        return _invalid(stem, content, str(error))
    assert isinstance(hook.run, RunConversation)
    if len(action_hook_request_text(hook.run.prompt)) > (
        ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS
    ):
        return _invalid(stem, content, "the prompt is too long for one message")
    return Task(hook=hook, run=hook.run, content_hash=content_hash, text=text)
