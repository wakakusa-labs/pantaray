"""Give the next run the messages a stopped run never took.

A message sent while a run is busy waits for that run's next THINK. When the run
ends first, a Stop (or a run that ends without a checkpoint) records the message
as not executed instead of starting another run. The next run the user starts on
the Action takes those messages first, in the order they were sent, the way a
finished run hands its waiting messages to its successor.

The run's root is its earliest message, as with that hand-off: the job continues
from it, and runs are ordered by it (``MIN(accepted_sequence)`` of the USER steps
a run adopted). So only a message sent after the latest run's root can join; an
older one would sort the new run before that latest run. Such a message exists
only when a run left it behind and the next run could not take it. Nor can a run
that starts fresh, on an Action with no checkpoint yet, take any: they join a run
through a checkpoint, and that run has none. They stay not executed, and the user
can withdraw them.
"""

from __future__ import annotations

import json
import sqlite3

from pantaray_agents.agents.action_agent.runtime.checkpoint import (
    RUNTIME_STATE_CHECKPOINT_VERSION,
    build_runtime_state_checkpoint,
    restore_runtime_state_checkpoint,
    validate_runtime_checkpoint_payload,
)
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.user_request import (
    project_persisted_user_request_step,
)
from pantaray_agents.agents.action_agent.runtime.state import ActionAgentState
from pantaray_agents.agents.action_agent.runtime.user_image_attachments import (
    build_user_image_attachments,
)
from pantaray_agents.application.action.new_user_turn import (
    reset_state_for_new_user_turn,
)
from pantaray_agents.application.action.resume_service import (
    project_intervening_user_steps,
)
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.schema.agent.action_message import ActionUserMessageInput
from pantaray_agents.tasks.action_user_message import render_action_user_request_text

from .action_checkpoint_retention import prune_action_checkpoints_in_connection
from .action_message_models import _StepPosition
from .action_resumability import (
    action_has_restorable_checkpoint,
    load_unanswered_user_steps_in_connection,
)
from .action_user_adoption import (
    PendingActionUserStep,
    adopt_pending_action_user_steps_in_connection,
    load_not_executed_action_user_steps_in_connection,
)


def next_run_takes_messages_after_in_connection(
    *, connection: sqlite3.Connection, user_id: str, action_id: str
) -> int | None:
    """The ``accepted_sequence`` after which the next run takes not-executed
    messages (the latest run's root), or ``None`` when it takes none.

    The conversation labels a waiting message by this same answer.
    """

    if not action_has_restorable_checkpoint(
        connection=connection, user_id=user_id, action_id=action_id
    ):
        return None
    (latest_root,) = connection.execute(
        """SELECT COALESCE(MAX(root), 0) FROM (
            SELECT MIN(accepted_sequence) AS root FROM agent_action_steps
            WHERE user_id = ? AND action_id = ? AND step_type = 'user_request'
              AND status = 'success' AND adopted_process_id IS NOT NULL
            GROUP BY adopted_process_id)""",
        (user_id, action_id),
    ).fetchone()
    return int(latest_root)


class WaitingMessages:
    """The not-executed messages one new run takes before the message starting it.

    One submit transaction drives it: ``load`` before the job is enqueued (the job
    continues from the first of them), ``adopt`` before the new message takes its
    position, and ``checkpoint_with`` once that message is written.
    """

    def __init__(
        self,
        *,
        connection: sqlite3.Connection,
        user_id: str,
        action_id: str,
        suggestion_id: str | None,
        steps: tuple[PendingActionUserStep, ...],
    ) -> None:
        self._connection = connection
        self._user_id = user_id
        self._action_id = action_id
        self._suggestion_id = suggestion_id
        self.steps = steps
        self._state: ActionAgentState | None = None
        self._process_id: str | None = None
        self._adopted_at = ""

    @classmethod
    def load(
        cls,
        *,
        connection: sqlite3.Connection,
        user_id: str,
        action_id: str,
        suggestion_id: str | None,
    ) -> WaitingMessages | None:
        """The messages the next run takes, or ``None`` when there are none."""

        after = next_run_takes_messages_after_in_connection(
            connection=connection, user_id=user_id, action_id=action_id
        )
        if after is None:
            return None
        steps = load_not_executed_action_user_steps_in_connection(
            connection=connection,
            user_id=user_id,
            action_id=action_id,
            after_accepted_sequence=after,
        )
        if not steps:
            return None
        return cls(
            connection=connection,
            user_id=user_id,
            action_id=action_id,
            suggestion_id=suggestion_id,
            steps=steps,
        )

    def adopt(self, *, process_id: str, adopted_at: str) -> None:
        """Adopt them into the run ``process_id`` opens, on the latest checkpoint
        and the messages stopped runs took after it without answering."""

        row = self._connection.execute(
            """SELECT step_number, runtime_state_checkpoint,
                      runtime_state_checkpoint_version
            FROM agent_action_steps
            WHERE user_id = ? AND action_id = ? AND runtime_state_checkpoint IS NOT NULL
            ORDER BY step_number DESC, completed_at IS NULL ASC, completed_at DESC,
                     created_at DESC, step_id DESC
            LIMIT 1""",
            (self._user_id, self._action_id),
        ).fetchone()
        checkpoint_step_number, checkpoint_json, version = row
        if version != RUNTIME_STATE_CHECKPOINT_VERSION:
            raise MigrationError("Action checkpoint cannot take the waiting messages")
        checkpoint = json.loads(checkpoint_json)
        validate_runtime_checkpoint_payload(checkpoint)
        state = project_intervening_user_steps(
            reset_state_for_new_user_turn(
                restore_runtime_state_checkpoint(
                    checkpoint,
                    expected_action_id=self._action_id,
                    expected_suggestion_id=self._suggestion_id,
                    expected_user_id=self._user_id,
                ),
                started_at=adopted_at,
            ),
            load_unanswered_user_steps_in_connection(
                self._connection,
                user_id=self._user_id,
                action_id=self._action_id,
                after_step_number=checkpoint_step_number,
            ),
            user_id=self._user_id,
        )
        self._state = adopt_pending_action_user_steps_in_connection(
            connection=self._connection,
            user_id=self._user_id,
            action_id=self._action_id,
            adopted_process_id=process_id,
            state=state,
            adopted_at=adopted_at,
            pending_steps=self.steps,
        )
        self._process_id = process_id
        self._adopted_at = adopted_at

    def checkpoint_with(
        self,
        *,
        step_id: str,
        position: _StepPosition | None,
        message: ActionUserMessageInput,
    ) -> None:
        """Checkpoint the run's state with the new message after them.

        The job continues from the first of them, so the run finds every message
        of its turn in this checkpoint, the new one (sent at ``adopted_at``) too.
        """

        if self._state is None or self._process_id is None or position is None:
            raise MigrationError("waiting messages must be adopted first")
        state = project_persisted_user_request_step(
            self._state,
            step_id=step_id,
            step_number=position.step_number,
            local_step_number=position.local_step_number,
            short_step_id=position.short_step_id,
            request_text=render_action_user_request_text(message),
            occurred_at=self._adopted_at,
            history_phase=self._state["phase"],
            attachments=build_user_image_attachments(
                user_id=self._user_id, images=message.images
            ),
        )
        cursor = self._connection.execute(
            """UPDATE agent_action_steps
            SET runtime_state_checkpoint = ?, runtime_state_checkpoint_version = ?
            WHERE step_id = ? AND adopted_process_id = ?""",
            (
                json.dumps(
                    build_runtime_state_checkpoint(state),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                RUNTIME_STATE_CHECKPOINT_VERSION,
                step_id,
                self._process_id,
            ),
        )
        if cursor.rowcount != 1:
            raise MigrationError("new USER step was not adopted by the new run")
        prune_action_checkpoints_in_connection(
            self._connection, user_id=self._user_id, action_id=self._action_id
        )


__all__ = ["WaitingMessages", "next_run_takes_messages_after_in_connection"]
