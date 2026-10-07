"""Workspace root catalog rendering for Action Agent prompts."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from pantaray_agents.local_runtime.runtime.bootstrap import read_local_runtime_db_config
from pantaray_agents.local_runtime.tooling.repository.common import (
    _configure_connection,
    _deserialize_json_string_map,
)
from pantaray_agents.schema.read_access import ReadAccessScope
from pantaray_agents.schema.workspace_context import (
    WorkspaceContextCatalog,
    workspace_context_catalog_from_snapshot,
)

AGENT_EXPERIENCE_SOURCE_TYPE = "agent_experience"
AGENT_EXPERIENCE_PROMPT_HINT = (
    "useful past procedures may exist here; explore only when needed"
)


@dataclass(frozen=True, slots=True)
class LocalWorkspaceManifestCatalog:
    root_catalog: str
    workspace_context: WorkspaceContextCatalog


@dataclass(frozen=True, slots=True)
class ManifestRootCatalogRow:
    source_type: str
    display_name: str
    real_path: str
    can_read: bool
    can_apply_patch: bool
    can_process_read: bool
    can_process_write: bool


@dataclass(frozen=True, slots=True)
class ManifestCatalogRows:
    roots: tuple[ManifestRootCatalogRow, ...]
    workspace_context_snapshot: object
    cwd_path: str


def load_required_local_workspace_manifest_catalog(
    *,
    user_id: str,
    manifest_id: object,
    execution_session_id: str,
    read_access_scope: ReadAccessScope,
) -> LocalWorkspaceManifestCatalog:
    if not isinstance(manifest_id, str) or not manifest_id.strip():
        raise RuntimeError("Action workspace manifest id is required")
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    catalog_rows = _load_manifest_catalog_rows(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=user_id,
        manifest_id=manifest_id.strip(),
        execution_session_id=execution_session_id,
    )
    if not catalog_rows.roots:
        raise RuntimeError("Action workspace manifest has no roots")
    try:
        workspace_context = workspace_context_catalog_from_snapshot(
            catalog_rows.workspace_context_snapshot,
            read_access_scope=read_access_scope,
        )
    except ValueError as exc:
        raise RuntimeError("Action workspace context snapshot is invalid") from exc
    return LocalWorkspaceManifestCatalog(
        root_catalog=_format_root_catalog(
            catalog_rows.roots, cwd_path=catalog_rows.cwd_path
        ),
        workspace_context=workspace_context,
    )


def _load_manifest_catalog_rows(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    manifest_id: str,
    execution_session_id: str,
) -> ManifestCatalogRows:
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        manifest_row = connection.execute(
            """
            SELECT manifest.workspace_context_snapshot_json, session.cwd_path
            FROM workspace_manifests AS manifest
            JOIN execution_sessions AS session
              ON session.user_id = manifest.user_id
             AND session.action_id = manifest.action_id
            WHERE manifest.user_id = ?
              AND manifest.manifest_id = ?
              AND manifest.status = 'ready'
              AND session.execution_session_id = ?
            """,
            (user_id, manifest_id, execution_session_id),
        ).fetchone()
        if manifest_row is None:
            raise RuntimeError(
                "Action workspace manifest and execution session do not match"
            )
        rows = connection.execute(
            """
            SELECT
                wmr.source_type,
                wmr.display_name,
                wmr.real_path,
                wmr.can_read,
                wmr.can_apply_patch,
                wmr.can_process_read,
                wmr.can_process_write
            FROM workspace_manifest_roots AS wmr
            WHERE wmr.manifest_id = ?
            ORDER BY wmr.source_type DESC, wmr.real_path ASC
            """,
            (manifest_id,),
        ).fetchall()
    return ManifestCatalogRows(
        roots=tuple(_catalog_row(row) for row in rows),
        cwd_path=str(manifest_row["cwd_path"]),
        workspace_context_snapshot=_deserialize_json_string_map(
            manifest_row["workspace_context_snapshot_json"],
            field_name="workspace_manifests.workspace_context_snapshot_json",
        ),
    )


def _catalog_row(row: sqlite3.Row) -> ManifestRootCatalogRow:
    return ManifestRootCatalogRow(
        source_type=str(row["source_type"]),
        display_name=str(row["display_name"]),
        real_path=str(row["real_path"]),
        can_read=bool(row["can_read"]),
        can_apply_patch=bool(row["can_apply_patch"]),
        can_process_read=bool(row["can_process_read"]),
        can_process_write=bool(row["can_process_write"]),
    )


def _format_root_catalog(
    rows: tuple[ManifestRootCatalogRow, ...], *, cwd_path: str
) -> str:
    lines = [
        f"- `.` = `{cwd_path}`: current execution cwd for all relative paths.",
    ]
    lines.extend(_format_mount_catalog_row(row) for row in rows)
    return "\n".join(lines)


def _format_mount_catalog_row(row: ManifestRootCatalogRow) -> str:
    if row.source_type == AGENT_EXPERIENCE_SOURCE_TYPE:
        return f"- `{row.real_path}`: {AGENT_EXPERIENCE_PROMPT_HINT}"
    return (
        f"- `{row.real_path}`: {row.display_name} "
        f"({row.source_type}; capabilities={', '.join(_capabilities(row))})"
    )


def _capabilities(row: ManifestRootCatalogRow) -> tuple[str, ...]:
    return tuple(
        label
        for label, enabled in (
            ("read", row.can_read),
            ("apply_patch", row.can_apply_patch),
            ("process_read", row.can_process_read),
            ("process_write", row.can_process_write),
        )
        if enabled
    )


__all__ = [
    "LocalWorkspaceManifestCatalog",
    "load_required_local_workspace_manifest_catalog",
]
