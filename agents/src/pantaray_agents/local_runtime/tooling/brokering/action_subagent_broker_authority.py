from __future__ import annotations

from pathlib import Path
from typing import cast

from ..action_subagent_resource_authority import (
    ActionSubagentResourceActorError,
    ActionSubagentResourceWriteDeniedError,
    authorize_action_subagent_resource_writes,
    resolve_action_subagent_command_write_roots,
)
from ..action_subagent_resource_identity import (
    ActionSubagentResourceIdentityError,
    WorkspaceResourceIdentity,
)
from .broker_common import BrokerContext, BrokerPolicyError

ACTION_SUBAGENT_WRITE_DENIED = "ACTION_SUBAGENT_WRITE_DENIED"


def authorize_direct_workspace_writes(
    *, context: BrokerContext, resolved_paths: tuple[Path, ...]
) -> None:
    resources = tuple(
        WorkspaceResourceIdentity.from_resolved_path(
            manifest_id=context.manifest_id,
            resolved_path=resolved_path,
        )
        for resolved_path in resolved_paths
    )
    try:
        authorize_action_subagent_resource_writes(
            db_path=context.db_path,
            busy_timeout_ms=context.busy_timeout_ms,
            user_id=context.execution_session.user_id,
            action_id=cast(str, context.execution_session.action_id),
            actor_process_id=context.actor_process_id,
            resources=resources,
        )
    except (
        ActionSubagentResourceActorError,
        ActionSubagentResourceIdentityError,
        ActionSubagentResourceWriteDeniedError,
    ) as exc:
        raise BrokerPolicyError(
            "workspace write is outside the broker actor's active claim authority",
            code=ACTION_SUBAGENT_WRITE_DENIED,
        ) from exc


def resolve_command_workspace_write_roots(
    *, context: BrokerContext, candidate_roots: tuple[Path, ...]
) -> tuple[Path, ...]:
    candidates = tuple(
        WorkspaceResourceIdentity.from_resolved_path(
            manifest_id=context.manifest_id,
            resolved_path=root,
        )
        for root in candidate_roots
    )
    try:
        authorized = resolve_action_subagent_command_write_roots(
            db_path=context.db_path,
            busy_timeout_ms=context.busy_timeout_ms,
            user_id=context.execution_session.user_id,
            action_id=cast(str, context.execution_session.action_id),
            actor_process_id=context.actor_process_id,
            candidate_roots=candidates,
        )
    except (
        ActionSubagentResourceActorError,
        ActionSubagentResourceIdentityError,
        ActionSubagentResourceWriteDeniedError,
    ) as exc:
        raise BrokerPolicyError(
            "command write roots are outside the broker actor's active claim authority",
            code=ACTION_SUBAGENT_WRITE_DENIED,
        ) from exc
    return tuple(Path(root.normalized_key) for root in authorized)


__all__ = [
    "ACTION_SUBAGENT_WRITE_DENIED",
    "authorize_direct_workspace_writes",
    "resolve_command_workspace_write_roots",
]
