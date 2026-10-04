from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from ..storage.migrations import MigrationError
from .utc_timestamps import now_utc_iso

LOCAL_RUNTIME_ALREADY_ACTIVE_ERROR = "LOCAL_RUNTIME_ALREADY_ACTIVE"
RUNTIME_LOCK_FILE_SUFFIX = ".runtime.lock"


@dataclass(frozen=True, slots=True)
class RuntimeProcessLock:
    lock_path: Path
    owner_pid: int
    lock_id: str
    file_descriptor: int


def acquire_runtime_process_lock(*, db_path: Path) -> RuntimeProcessLock:
    lock_path = runtime_process_lock_path(db_path=db_path)
    owner_pid = os.getpid()
    lock_id = str(uuid4())
    payload = json.dumps(
        {
            "owner_pid": owner_pid,
            "lock_id": lock_id,
            "acquired_at": now_utc_iso(),
        },
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")

    while True:
        try:
            file_descriptor = os.open(
                lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            existing_pid = _read_runtime_lock_owner_pid(lock_path=lock_path)
            if existing_pid is None:
                raise MigrationError(
                    f"{LOCAL_RUNTIME_ALREADY_ACTIVE_ERROR}: runtime lock owner is unreadable"
                ) from None
            if _process_exists(existing_pid):
                raise MigrationError(
                    f"{LOCAL_RUNTIME_ALREADY_ACTIVE_ERROR}: pid={existing_pid}"
                ) from None
            try:
                lock_path.unlink()
            except FileNotFoundError:
                continue
            continue
        try:
            os.write(file_descriptor, payload)
            os.fsync(file_descriptor)
        except Exception:
            os.close(file_descriptor)
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            raise
        return RuntimeProcessLock(
            lock_path=lock_path,
            owner_pid=owner_pid,
            lock_id=lock_id,
            file_descriptor=file_descriptor,
        )


def release_runtime_process_lock(*, runtime_lock: RuntimeProcessLock) -> None:
    if runtime_lock.owner_pid != os.getpid():
        raise MigrationError("runtime process lock owner mismatch")
    try:
        lock_record = _read_runtime_lock_record(lock_path=runtime_lock.lock_path)
        if (
            lock_record is not None
            and lock_record.owner_pid == runtime_lock.owner_pid
            and lock_record.lock_id == runtime_lock.lock_id
        ):
            runtime_lock.lock_path.unlink()
    except FileNotFoundError:
        return
    finally:
        os.close(runtime_lock.file_descriptor)


def runtime_process_lock_path(*, db_path: Path) -> Path:
    resolved_db_path = db_path.resolve()
    return (
        resolved_db_path.parent / f"{resolved_db_path.name}{RUNTIME_LOCK_FILE_SUFFIX}"
    )


@dataclass(frozen=True, slots=True)
class RuntimeLockRecord:
    owner_pid: int
    lock_id: str


def _read_runtime_lock_owner_pid(*, lock_path: Path) -> int | None:
    lock_record = _read_runtime_lock_record(lock_path=lock_path)
    if lock_record is None:
        return None
    return lock_record.owner_pid


def _read_runtime_lock_record(*, lock_path: Path) -> RuntimeLockRecord | None:
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    owner_pid = payload.get("owner_pid")
    lock_id = payload.get("lock_id")
    if not isinstance(owner_pid, int) or owner_pid <= 0:
        return None
    if not isinstance(lock_id, str) or not lock_id:
        return None
    return RuntimeLockRecord(owner_pid=owner_pid, lock_id=lock_id)


def read_runtime_lock_record(*, lock_path: Path) -> RuntimeLockRecord | None:
    return _read_runtime_lock_record(lock_path=lock_path)


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def runtime_process_exists(pid: int) -> bool:
    return _process_exists(pid)


__all__ = [
    "LOCAL_RUNTIME_ALREADY_ACTIVE_ERROR",
    "RuntimeLockRecord",
    "RuntimeProcessLock",
    "acquire_runtime_process_lock",
    "read_runtime_lock_record",
    "release_runtime_process_lock",
    "runtime_process_exists",
    "runtime_process_lock_path",
]
