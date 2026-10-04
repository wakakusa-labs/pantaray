from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..storage.migrations.connection import configure_connection
from .action_subagent_resource_identity import (
    ActionSubagentResourceIdentityError,
    CanonicalResourceIdentity,
    ExternalResourceIdentity,
    WorkspaceResourceIdentity,
    resource_identities_overlap,
    resource_is_within,
    workspace_intersection,
)
from .command_write_grant_store import (
    insert_command_write_grants,
    load_other_actor_command_write_roots,
    resource_overlaps_command_write_roots,
)
from .models import CommandToolInvocationStartInput, ToolInvocationStartInput


class ActionSubagentResourceActorError(RuntimeError):
    """A process is not an active parent or child in the requested Action."""


class ActionSubagentResourceWriteDeniedError(RuntimeError):
    """A workspace or external write is outside the actor's authority."""


@dataclass(frozen=True, slots=True)
class AuthorizedClaimActor:
    process_id: str
    role: Literal["parent", "child"]
    parent_process_id: str


@dataclass(frozen=True, slots=True)
class _ActiveClaim:
    child_process_id: str
    resource: CanonicalResourceIdentity


@dataclass(frozen=True, slots=True)
class _AuthoritySnapshot:
    actor: AuthorizedClaimActor
    claims: tuple[_ActiveClaim, ...]
    other_command_write_roots: tuple[Path, ...]


def authorize_action_subagent_resource_writes(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    action_id: str,
    actor_process_id: str,
    resources: tuple[CanonicalResourceIdentity, ...],
) -> AuthorizedClaimActor:
    """Authorize exact writes against one active-claim snapshot."""

    if not resources:
        raise ActionSubagentResourceIdentityError(
            "resource write authority requires at least one resource"
        )
    snapshot = _load_authority_snapshot(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=user_id,
        action_id=action_id,
        actor_process_id=actor_process_id,
    )
    _authorize_resources(snapshot, resources)
    return snapshot.actor


def record_command_write_authority_in_connection(
    connection: sqlite3.Connection, invocation: ToolInvocationStartInput
) -> None:
    if not isinstance(invocation, CommandToolInvocationStartInput):
        return
    snapshot = _load_authority_snapshot_in_connection(
        connection,
        user_id=invocation.user_id,
        action_id=invocation.action_id,
        actor_process_id=invocation.actor_process_id,
    )
    resources = tuple(
        WorkspaceResourceIdentity.from_resolved_path(
            manifest_id=invocation.manifest_id, resolved_path=root
        )
        for root in invocation.write_roots
    )
    _authorize_resources(snapshot, resources)
    insert_command_write_grants(connection, invocation)


def _authorize_resources(
    snapshot: _AuthoritySnapshot, resources: tuple[CanonicalResourceIdentity, ...]
) -> None:
    if snapshot.actor.role == "parent":
        denied = any(
            resource_identities_overlap(resource, claim.resource)
            for resource in resources
            for claim in snapshot.claims
        )
    else:
        own_claims = tuple(
            claim.resource
            for claim in snapshot.claims
            if claim.child_process_id == snapshot.actor.process_id
        )
        denied = any(
            not any(resource_is_within(resource, claim) for claim in own_claims)
            for resource in resources
        )
    if denied or any(
        resource_overlaps_command_write_roots(
            resource, snapshot.other_command_write_roots
        )
        for resource in resources
    ):
        raise ActionSubagentResourceWriteDeniedError(
            "Action subagent resource write is outside active claim authority"
        )


def resolve_action_subagent_command_write_roots(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    action_id: str,
    actor_process_id: str,
    candidate_roots: tuple[WorkspaceResourceIdentity, ...],
) -> tuple[WorkspaceResourceIdentity, ...]:
    """Return the workspace roots writable by one parent or child command."""

    snapshot = _load_authority_snapshot(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=user_id,
        action_id=action_id,
        actor_process_id=actor_process_id,
    )
    if snapshot.actor.role == "parent":
        return _dedupe_workspace_identities(
            tuple(
                candidate
                for candidate in candidate_roots
                if not any(
                    resource_identities_overlap(candidate, claim.resource)
                    for claim in snapshot.claims
                )
                and not resource_overlaps_command_write_roots(
                    candidate, snapshot.other_command_write_roots
                )
            )
        )

    own_workspace_claims = tuple(
        claim.resource
        for claim in snapshot.claims
        if claim.child_process_id == snapshot.actor.process_id
        and isinstance(claim.resource, WorkspaceResourceIdentity)
    )
    intersections: list[WorkspaceResourceIdentity] = []
    for candidate in candidate_roots:
        for claim in own_workspace_claims:
            intersection = workspace_intersection(candidate, claim)
            if intersection is not None and not resource_overlaps_command_write_roots(
                intersection, snapshot.other_command_write_roots
            ):
                intersections.append(intersection)
    return _dedupe_workspace_identities(tuple(intersections))


def _load_authority_snapshot(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    action_id: str,
    actor_process_id: str,
) -> _AuthoritySnapshot:
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        return _load_authority_snapshot_in_connection(
            connection,
            user_id=user_id,
            action_id=action_id,
            actor_process_id=actor_process_id,
        )


def _load_authority_snapshot_in_connection(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    action_id: str,
    actor_process_id: str,
) -> _AuthoritySnapshot:
    actor = _load_authorized_actor(
        connection,
        user_id=user_id,
        action_id=action_id,
        actor_process_id=actor_process_id,
    )
    rows = connection.execute(
        """
        SELECT child_process_id, resource_kind, root_identity, normalized_key
        FROM action_subagent_resource_claims
        WHERE user_id = ? AND action_id = ? AND parent_process_id = ?
          AND released_at IS NULL
        """,
        (user_id, action_id, actor.parent_process_id),
    ).fetchall()
    claims = tuple(_active_claim_from_row(row) for row in rows)
    return _AuthoritySnapshot(
        actor=actor,
        claims=claims,
        other_command_write_roots=load_other_actor_command_write_roots(
            connection,
            user_id=user_id,
            action_id=action_id,
            actor_process_id=actor_process_id,
        ),
    )


def _load_authorized_actor(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    action_id: str,
    actor_process_id: str,
) -> AuthorizedClaimActor:
    row = connection.execute(
        """
        SELECT actor.process_id, actor.kind, actor.parent_process_id
        FROM processes AS actor
        JOIN agent_actions AS action
          ON action.user_id = actor.user_id AND action.action_id = actor.action_id
        LEFT JOIN processes AS parent
          ON parent.process_id = actor.parent_process_id
         AND parent.user_id = actor.user_id AND parent.action_id = actor.action_id
        WHERE actor.process_id = ? AND actor.user_id = ? AND actor.action_id = ?
          AND actor.status = 'running' AND action.status = 'processing'
          AND (
            (actor.kind = 'action' AND actor.parent_process_id IS NULL)
            OR
            (actor.kind = 'action_subagent' AND parent.kind = 'action'
              AND parent.status = 'running')
          )
        """,
        (actor_process_id, user_id, action_id),
    ).fetchone()
    if row is None:
        raise ActionSubagentResourceActorError(
            "resource actor is not active in the requested Action"
        )
    role: Literal["parent", "child"] = "parent" if row["kind"] == "action" else "child"
    stored_parent = row["parent_process_id"]
    return AuthorizedClaimActor(
        process_id=str(row["process_id"]),
        role=role,
        parent_process_id=(
            str(row["process_id"]) if role == "parent" else str(stored_parent)
        ),
    )


def _active_claim_from_row(row: sqlite3.Row) -> _ActiveClaim:
    if row["resource_kind"] == "workspace_path":
        resource: CanonicalResourceIdentity = WorkspaceResourceIdentity(
            manifest_id=str(row["root_identity"]),
            normalized_key=str(row["normalized_key"]),
        )
    else:
        resource = ExternalResourceIdentity(
            root_identity=str(row["root_identity"]),
            normalized_key=str(row["normalized_key"]),
        )
    return _ActiveClaim(
        child_process_id=str(row["child_process_id"]), resource=resource
    )


def _dedupe_workspace_identities(
    resources: tuple[WorkspaceResourceIdentity, ...],
) -> tuple[WorkspaceResourceIdentity, ...]:
    return tuple(dict.fromkeys(resources))
