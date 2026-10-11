"""Withdrawing a USER message no run has adopted yet."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.runtime.action_message_withdrawal import (
    ActionMessageAdoptedError,
    withdraw_action_message,
)
from pantaray_agents.local_runtime.runtime.action_messages import (
    ExistingActionTarget,
    MessageIdentityConflictError,
    MessageWithdrawnError,
    NewActionTarget,
    StartedActionMessageResult,
    SubmitActionMessageCommand,
    submit_action_message,
)
from pantaray_agents.local_runtime.runtime.identity import (
    register_logged_out_owner,
    reset_logged_out_owner,
)
from pantaray_agents.local_runtime.storage.migrations import load_default_migrations
from pantaray_agents.local_runtime.tooling.action_session_temp_paths import (
    resolve_action_storage_paths,
)
from pantaray_agents.schema.agent.action_message import (
    ActionUserMessageInput,
    FileAttachmentInput,
)
from pantaray_agents.schema.agent.image import ImageInput

from .action_seed import ensure_user_row
from .migrated_db import prepare_test_database

USER = "user-1"
OTHER_USER = "user-2"
PDF_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"
IMAGE = f"{USER}/2026-10-11/3f861ab4-7b3c-5d90-b520-3424da5bca75.png"
PAYLOAD = b"%PDF-1.7 staged"


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path, busy_timeout_ms=1_000, migrations=load_default_migrations()
    )
    with closing(sqlite3.connect(db_path)) as connection, connection:
        for user_id in (USER, OTHER_USER):
            ensure_user_row(
                connection, user_id=user_id, timestamp="2026-10-11T00:00:00Z"
            )
    monkeypatch.setenv("LOCAL_DB_PATH", str(db_path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")
    monkeypatch.setenv("LOCAL_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    register_logged_out_owner(USER)
    yield db_path
    reset_logged_out_owner()


def _start(
    user_id: str = USER, message_id: str = "first"
) -> StartedActionMessageResult:
    result = submit_action_message(
        SubmitActionMessageCommand(
            user_id=user_id,
            target=NewActionTarget(),
            message=ActionUserMessageInput(
                message_id=f"{user_id}-{message_id}", content="Start"
            ),
        )
    )
    assert isinstance(result, StartedActionMessageResult)
    return result


def _command(
    started: StartedActionMessageResult,
    message_id: str,
    *,
    images: tuple[str, ...] = (),
    files: tuple[FileAttachmentInput, ...] = (),
    user_id: str = USER,
) -> SubmitActionMessageCommand:
    """A message sent while the first run is busy: it stays unadopted."""

    return SubmitActionMessageCommand(
        user_id=user_id,
        target=ExistingActionTarget(
            action_id=started.action_id, expected_process_id=started.process_id
        ),
        message=ActionUserMessageInput(
            message_id=message_id,
            content="And this",
            images=tuple(ImageInput(kind="image", storage_path=p) for p in images),
            files=files,
        ),
    )


def _follow_up(started: StartedActionMessageResult, message_id: str) -> str:
    return submit_action_message(_command(started, message_id)).user_step_id


def _count(db_path: Path, table: str) -> int:
    with closing(sqlite3.connect(db_path)) as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _step_ids(db_path: Path) -> set[str]:
    with closing(sqlite3.connect(db_path)) as connection:
        return {
            row[0]
            for row in connection.execute("SELECT step_id FROM agent_action_steps")
        }


def test_withdrawing_removes_only_the_unadopted_step_and_its_attachments(
    runtime: Path, tmp_path: Path
) -> None:
    started = _start()
    staged = tmp_path / f"artifacts/generated/attachments/{USER}/{PDF_ID}.pdf"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(PAYLOAD)
    image = tmp_path / "artifacts/generated/images" / IMAGE
    image.parent.mkdir(parents=True)
    image.write_bytes(b"png")
    file = FileAttachmentInput(
        attachment_id=PDF_ID, name="a.pdf", byte_size=len(PAYLOAD)
    )
    withdrawn = submit_action_message(
        _command(started, "message-2", images=(IMAGE,), files=(file,))
    ).user_step_id
    kept = _follow_up(started, "message-3")
    attachment = (
        resolve_action_storage_paths(
            db_path=runtime, user_id=USER, action_id=started.action_id
        ).workspace
        / f"attachments/{PDF_ID}"
    )
    assert attachment.is_dir()
    before = _step_ids(runtime)

    withdraw_action_message(
        user_id=USER, action_id=started.action_id, step_id=withdrawn
    )

    assert _step_ids(runtime) == before - {withdrawn}
    assert kept in _step_ids(runtime)
    assert not attachment.exists()
    # The image store also holds what users attach elsewhere; nothing goes from it.
    assert image.exists()
    # Already gone: nothing left to withdraw, and nothing else changes.
    withdraw_action_message(
        user_id=USER, action_id=started.action_id, step_id=withdrawn
    )
    assert _step_ids(runtime) == before - {withdrawn}


def test_a_retry_of_a_withdrawn_send_never_registers_it_again(runtime: Path) -> None:
    started = _start()
    command = _command(started, "message-2")
    withdrawn = submit_action_message(command).user_step_id
    withdraw_action_message(
        user_id=USER, action_id=started.action_id, step_id=withdrawn
    )
    before = [_count(runtime, table) for table in ("processes", "jobs")]

    # The same request again, as a retry after an unknown outcome sends it.
    with pytest.raises(MessageWithdrawnError):
        submit_action_message(command)
    # The id reused for another Action is not that message.
    elsewhere = _start(message_id="another")
    with pytest.raises(MessageIdentityConflictError):
        submit_action_message(
            command.model_copy(
                update={
                    "target": ExistingActionTarget(
                        action_id=elsewhere.action_id,
                        expected_process_id=elsewhere.process_id,
                    )
                }
            )
        )

    assert withdrawn not in _step_ids(runtime)
    # Only the second Action's own run: none for the retry.
    after = [_count(runtime, table) for table in ("processes", "jobs")]
    assert after == [count + 1 for count in before]


def test_an_adopted_message_is_refused_and_kept(runtime: Path) -> None:
    started = _start()
    pending = _follow_up(started, "message-2")
    # The run adopted it before the withdrawal reached the database.
    with closing(sqlite3.connect(runtime)) as connection, connection:
        connection.execute(
            """UPDATE agent_action_steps SET step_number = 2, local_step_number = 2,
               short_step_id = 'S-2-USER', adopted_process_id = expected_process_id
               WHERE step_id = ?""",
            (pending,),
        )
    before = _step_ids(runtime)

    for step_id in (started.user_step_id, pending):
        with pytest.raises(ActionMessageAdoptedError):
            withdraw_action_message(
                user_id=USER, action_id=started.action_id, step_id=step_id
            )

    assert _step_ids(runtime) == before


def test_another_action_or_user_is_never_touched(runtime: Path) -> None:
    started = _start()
    pending = _follow_up(started, "message-2")
    register_logged_out_owner(OTHER_USER)
    others = _start(OTHER_USER)
    others_pending = submit_action_message(
        _command(others, f"{OTHER_USER}-message-2", user_id=OTHER_USER)
    ).user_step_id
    register_logged_out_owner(USER)
    before = _step_ids(runtime)

    # The step under an Action it does not belong to, and another user's step.
    other_action = _start(message_id="another").action_id
    withdraw_action_message(user_id=USER, action_id=other_action, step_id=pending)
    withdraw_action_message(
        user_id=USER, action_id=others.action_id, step_id=others_pending
    )

    assert before <= _step_ids(runtime)
