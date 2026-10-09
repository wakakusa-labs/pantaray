from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pantaray_agents.tools.contract import BrokerPolicyError
from pantaray_agents.tools.files.manifest_paths import (
    WORKSPACE_ROOT_AUTHORITY_INVALID,
    resolve_local_path,
)

from ..storage.transactions import SQLiteTransactionOwnershipError
from .action_subagent_resource_identity import (
    ActionSubagentResourceIdentityError,
    CanonicalResourceIdentity,
    ExternalResourceIdentity,
    WorkspaceResourceIdentity,
    normalize_workspace_resource_key,
    resource_identities_overlap,
)
from .command_write_grant_store import (
    load_other_actor_command_write_roots,
    resource_overlaps_command_write_roots,
)
from .workspace_manifest_roots import (
    ManifestRoot,
    load_ready_manifest_root_in_connection,
    load_ready_manifest_roots_in_connection,
)


class ActionSubagentResourceClaimConflictError(RuntimeError):
    """A requested resource overlaps an active sibling claim."""


@dataclass(frozen=True, slots=True)
class WorkspacePathResourceClaim:
    claim_id: str
    manifest_id: str
    raw_path: str
    current_cwd: Path


@dataclass(frozen=True, slots=True)
class ExternalResourceClaim:
    claim_id: str
    root_identity: str
    normalized_key: str


type ActionSubagentResourceClaim = WorkspacePathResourceClaim | ExternalResourceClaim


@dataclass(frozen=True, slots=True)
class _NormalizedClaim:
    claim_id: str
    resource_kind: Literal["workspace_path", "external_resource"]
    root_identity: str
    normalized_key: str


def acquire_action_subagent_resource_claims_in_connection(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    action_id: str,
    parent_process_id: str,
    child_process_id: str,
    acquired_at: str,
    claims: tuple[ActionSubagentResourceClaim, ...],
) -> None:
    """Acquire non-overlapping claims inside the caller's write transaction."""

    if not connection.in_transaction:
        raise SQLiteTransactionOwnershipError(
            "Action subagent resource claims require a caller-owned transaction"
        )

    active_claims = _load_active_claims(
        connection, user_id, action_id, parent_process_id
    )
    other_command_roots = load_other_actor_command_write_roots(
        connection,
        user_id=user_id,
        action_id=action_id,
        actor_process_id=child_process_id,
    )
    roots_by_manifest: dict[str, tuple[ManifestRoot, ...]] = {}
    for claim in claims:
        normalized = _normalize_claim(
            connection, user_id, action_id, claim, roots_by_manifest
        )
        if any(_claims_overlap(normalized, active) for active in active_claims):
            raise ActionSubagentResourceClaimConflictError(
                "Action subagent resource claim overlaps an active claim"
            )
        if resource_overlaps_command_write_roots(
            _normalized_claim_identity(normalized), other_command_roots
        ):
            raise ActionSubagentResourceClaimConflictError(
                "Action subagent resource claim overlaps an unreclaimed command write grant"
            )
        connection.execute(
            """
            INSERT INTO action_subagent_resource_claims(
                claim_id, user_id, action_id, parent_process_id,
                child_process_id, resource_kind, root_identity,
                normalized_key, acquired_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized.claim_id,
                user_id,
                action_id,
                parent_process_id,
                child_process_id,
                normalized.resource_kind,
                normalized.root_identity,
                normalized.normalized_key,
                acquired_at,
            ),
        )
        active_claims.append(normalized)


def _load_active_claims(
    connection: sqlite3.Connection,
    user_id: str,
    action_id: str,
    parent_process_id: str,
) -> list[_NormalizedClaim]:
    rows = connection.execute(
        """
        SELECT claim_id, resource_kind, root_identity, normalized_key
        FROM action_subagent_resource_claims
        WHERE user_id = ? AND action_id = ? AND parent_process_id = ?
          AND released_at IS NULL
        """,
        (user_id, action_id, parent_process_id),
    ).fetchall()
    return [
        _NormalizedClaim(
            claim_id=str(row["claim_id"]),
            resource_kind=row["resource_kind"],
            root_identity=str(row["root_identity"]),
            normalized_key=str(row["normalized_key"]),
        )
        for row in rows
    ]


def _normalize_claim(
    connection: sqlite3.Connection,
    user_id: str,
    action_id: str,
    claim: ActionSubagentResourceClaim,
    roots_by_manifest: dict[str, tuple[ManifestRoot, ...]],
) -> _NormalizedClaim:
    if isinstance(claim, ExternalResourceClaim):
        if not claim.root_identity.strip() or not claim.normalized_key.strip():
            raise ValueError("external resource identity must not be blank")
        return _NormalizedClaim(
            claim_id=claim.claim_id,
            resource_kind="external_resource",
            root_identity=claim.root_identity,
            normalized_key=claim.normalized_key,
        )

    roots = roots_by_manifest.get(claim.manifest_id)
    if roots is None:
        scratch_root = load_ready_manifest_root_in_connection(
            connection=connection,
            user_id=user_id,
            action_id=action_id,
            manifest_id=claim.manifest_id,
            root_id=f"root:{action_id}:scratch",
        )
        if scratch_root is None:
            raise BrokerPolicyError("ready Action workspace manifest is not available")
        roots = load_ready_manifest_roots_in_connection(
            connection=connection,
            user_id=user_id,
            manifest_id=claim.manifest_id,
        )
        roots_by_manifest[claim.manifest_id] = roots
    try:
        resolved = resolve_local_path(
            roots=roots,
            raw_path=claim.raw_path,
            cwd_path=claim.current_cwd,
            capability="apply_patch",
            must_exist=False,
        )
    except BrokerPolicyError as exc:
        if exc.code == WORKSPACE_ROOT_AUTHORITY_INVALID:
            raise
        raise ActionSubagentResourceIdentityError(str(exc)) from exc
    return _NormalizedClaim(
        claim_id=claim.claim_id,
        resource_kind="workspace_path",
        root_identity=claim.manifest_id,
        normalized_key=normalize_workspace_resource_key(resolved.path),
    )


def _claims_overlap(left: _NormalizedClaim, right: _NormalizedClaim) -> bool:
    return resource_identities_overlap(
        _normalized_claim_identity(left),
        _normalized_claim_identity(right),
    )


def _normalized_claim_identity(
    claim: _NormalizedClaim,
) -> CanonicalResourceIdentity:
    if claim.resource_kind == "workspace_path":
        return WorkspaceResourceIdentity(
            manifest_id=claim.root_identity,
            normalized_key=claim.normalized_key,
        )
    return ExternalResourceIdentity(
        root_identity=claim.root_identity,
        normalized_key=claim.normalized_key,
    )
