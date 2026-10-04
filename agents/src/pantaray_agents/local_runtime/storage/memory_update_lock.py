from __future__ import annotations

import fcntl
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pantaray_agents.utils.timestamps import format_iso8601_utc_z_milliseconds

LOCKS_DIRNAME = "locks"


class MemoryUpdateLockConflictError(RuntimeError):
    """Raised when a user's memory tree is already locked."""


@dataclass(slots=True)
class MemoryUpdateLockLease:
    root_path: Path
    user_id: str
    owner_id: str
    _descriptor: int | None = None

    def __enter__(self) -> MemoryUpdateLockLease:
        lock_path = memory_update_lock_path(
            root_path=self.root_path,
            user_id=self.user_id,
        )
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.ftruncate(descriptor, 0)
            payload = json.dumps(
                {
                    "user_id": self.user_id,
                    "owner_id": self.owner_id,
                    "pid": os.getpid(),
                    "acquired_at": _utc_now(),
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
            os.write(descriptor, payload)
            os.fsync(descriptor)
            self._descriptor = descriptor
            return self
        except BlockingIOError as exc:
            os.close(descriptor)
            raise MemoryUpdateLockConflictError(
                "user memory update is already locked"
            ) from exc
        except Exception:
            os.close(descriptor)
            raise

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        descriptor = self._descriptor
        self._descriptor = None
        if descriptor is None:
            return
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def memory_update_lock_path(*, root_path: Path, user_id: str) -> Path:
    _require_user_id(user_id)
    return root_path / "memory" / LOCKS_DIRNAME / f"{user_id}.lock"


def _require_user_id(user_id: str) -> None:
    if not user_id.strip():
        raise ValueError("user_id must not be empty")
    if "/" in user_id or "\\" in user_id or ".." in user_id:
        raise ValueError("user_id contains forbidden path characters")


# Storage sits below local_runtime.runtime, so it formats with the primitive.
def _utc_now() -> str:
    return format_iso8601_utc_z_milliseconds(datetime.now(UTC))


__all__ = [
    "MemoryUpdateLockConflictError",
    "MemoryUpdateLockLease",
    "memory_update_lock_path",
]
