"""The single durable write of one action_step."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol


class ActionStepPersistenceError(RuntimeError):
    """action_step 永続化に失敗したことを示す例外。"""


class SupportsRepositoryError(Protocol):
    """`error` 属性を持つ RepositoryResult 互換型。"""

    @property
    def error(self) -> str | Exception | None: ...


def _repository_error_message(result: SupportsRepositoryError) -> str | None:
    raw_error = result.error
    if isinstance(raw_error, Exception):
        text = str(raw_error).strip()
        return text if text else raw_error.__class__.__name__
    if isinstance(raw_error, str):
        text = raw_error.strip()
        return text if text else None
    return None


async def persist_action_step[TRepositoryResult: SupportsRepositoryError](
    *,
    save: Callable[[], Awaitable[TRepositoryResult]],
    step_name: str,
    step_id: str,
) -> TRepositoryResult:
    """Write the step once; any failure is fatal to the run.

    The repository waits out a concurrent writer through SQLite's busy_timeout,
    so a write that still fails is not a transient condition to retry here.
    """
    try:
        result = await save()
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        raise ActionStepPersistenceError(
            _failure_message(
                step_name=step_name,
                step_id=step_id,
                error=str(exc).strip() or type(exc).__name__,
            )
        ) from exc
    error_message = _repository_error_message(result)
    if error_message is not None:
        raise ActionStepPersistenceError(
            _failure_message(step_name=step_name, step_id=step_id, error=error_message)
        )
    return result


def _failure_message(*, step_name: str, step_id: str, error: str) -> str:
    return (
        "Failed to persist action step: "
        f"step_name={step_name} step_id={step_id} error={error}"
    )


__all__ = [
    "ActionStepPersistenceError",
    "persist_action_step",
]
