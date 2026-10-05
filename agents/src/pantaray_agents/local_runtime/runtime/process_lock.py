from __future__ import annotations

import fcntl
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from ..storage.migrations import MigrationError

LOCAL_RUNTIME_ALREADY_ACTIVE_ERROR = "LOCAL_RUNTIME_ALREADY_ACTIVE"
RUNTIME_LOCK_FILE_SUFFIX = ".runtime.lock"


@dataclass(frozen=True, slots=True)
class RuntimeProcessLock:
    lock_path: Path
    owner_pid: int
    lock_id: str
    file_descriptor: int


def acquire_runtime_process_lock(*, db_path: Path) -> RuntimeProcessLock:
    """Hold an exclusive flock on the lock file for as long as the fd stays open.

    The kernel drops the lock when its owner exits, so a lock file left by a
    killed process never blocks the next start. The file is never unlinked: a
    new acquirer could otherwise lock an inode that is no longer at the path.
    """
    lock_path = runtime_process_lock_path(db_path=db_path)
    file_descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(file_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(file_descriptor)
        raise MigrationError(
            f"{LOCAL_RUNTIME_ALREADY_ACTIVE_ERROR}: {lock_path} is held by another runtime"
        ) from None
    return RuntimeProcessLock(
        lock_path=lock_path,
        owner_pid=os.getpid(),
        lock_id=str(uuid4()),
        file_descriptor=file_descriptor,
    )


def release_runtime_process_lock(*, runtime_lock: RuntimeProcessLock) -> None:
    if runtime_lock.owner_pid != os.getpid():
        raise MigrationError("runtime process lock owner mismatch")
    os.close(runtime_lock.file_descriptor)


def runtime_process_lock_path(*, db_path: Path) -> Path:
    resolved_db_path = db_path.resolve()
    return (
        resolved_db_path.parent / f"{resolved_db_path.name}{RUNTIME_LOCK_FILE_SUFFIX}"
    )


__all__ = [
    "LOCAL_RUNTIME_ALREADY_ACTIVE_ERROR",
    "RuntimeProcessLock",
    "acquire_runtime_process_lock",
    "release_runtime_process_lock",
    "runtime_process_lock_path",
]
