from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from typing import cast

from ...storage.migrations import MigrationError
from ...storage.users import ensure_user_row
from . import read_access_settings as _read_access_settings
from .common import _configure_connection
from .workspace_settings_models import (
    WorkspaceFolder,
    WorkspaceOrganization,
    WorkspaceProject,
    WorkspaceProjectNameTakenError,
    WorkspaceSettings,
)

READ_ACCESS_SCOPE_FULL_ACCESS = _read_access_settings.READ_ACCESS_SCOPE_FULL_ACCESS
READ_ACCESS_SCOPE_WORKSPACE = _read_access_settings.READ_ACCESS_SCOPE_WORKSPACE
load_read_access_scope = _read_access_settings.load_read_access_scope
load_read_access_scope_in_connection = (
    _read_access_settings.load_read_access_scope_in_connection
)
update_read_access_scope = _read_access_settings.update_read_access_scope

ACTIVE_STATUS = "active"


def create_workspace_organization(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    display_name: str,
    now: str,
) -> WorkspaceOrganization:
    normalized_name = _normalize_display_name(display_name)
    organization_id = str(uuid.uuid4())
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        with connection:
            ensure_user_row(connection, user_id=user_id)
            connection.execute(
                """
                INSERT INTO workspace_organizations(
                    organization_id, user_id, display_name, status, created_at, updated_at
                ) VALUES (?, ?, ?, 'active', ?, ?)
                ON CONFLICT(user_id, display_name) DO UPDATE SET
                    status = 'active',
                    updated_at = excluded.updated_at
                """,
                (organization_id, user_id, normalized_name, now, now),
            )
            row = _fetch_organization_by_name(
                connection=connection,
                user_id=user_id,
                display_name=normalized_name,
            )
    return WorkspaceOrganization(
        organization_id=str(row["organization_id"]),
        display_name=str(row["display_name"]),
    )


def create_workspace_project(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    display_name: str,
    organization_ids: tuple[str, ...],
    now: str,
) -> WorkspaceProject:
    normalized_name = _normalize_display_name(display_name)
    project_id = str(uuid.uuid4())
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        with connection:
            ensure_user_row(connection, user_id=user_id)
            # A taken name is refused rather than answered with that project: a caller that
            # rolls back what it created must never be handed a project it did not create.
            try:
                row = connection.execute(
                    """
                    INSERT INTO workspace_projects(
                        project_id, user_id, display_name, sort_order, status,
                        created_at, updated_at
                    ) VALUES (
                        ?, ?, ?,
                        COALESCE((
                            SELECT MAX(existing.sort_order) + 1
                            FROM workspace_projects AS existing
                            WHERE existing.user_id = ?
                        ), 0),
                        'active', ?, ?
                    )
                    RETURNING display_name, sort_order
                    """,
                    (project_id, user_id, normalized_name, user_id, now, now),
                ).fetchone()
            except sqlite3.IntegrityError as exc:
                raise WorkspaceProjectNameTakenError(
                    f"workspace project name is taken: {normalized_name}"
                ) from exc
            _replace_project_organization_links(
                connection=connection,
                user_id=user_id,
                project_id=project_id,
                organization_ids=organization_ids,
                now=now,
            )
    return WorkspaceProject(
        project_id=project_id,
        display_name=str(row["display_name"]),
        sort_order=int(row["sort_order"]),
        organization_ids=_unique_sorted(organization_ids),
    )


def create_workspace_folder(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    real_path: Path,
    display_name: str,
    organization_ids: tuple[str, ...],
    project_ids: tuple[str, ...],
    now: str,
) -> WorkspaceFolder:
    normalized_name = _normalize_display_name(display_name)
    normalized_project_ids = _unique_sorted(project_ids)
    normalized_organization_ids = _folder_organization_ids(
        organization_ids=organization_ids,
        project_ids=normalized_project_ids,
    )
    canonical_real_path = real_path.resolve(strict=True)
    if not canonical_real_path.is_dir():
        raise MigrationError(f"workspace folder path is not a directory: {real_path}")
    canonical_text = str(canonical_real_path)
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        with connection:
            ensure_user_row(connection, user_id=user_id)
            row = connection.execute(
                """
                SELECT folder_id, created_at
                FROM workspace_folders
                WHERE user_id = ? AND canonical_real_path = ?
                """,
                (user_id, canonical_text),
            ).fetchone()
            if row is None:
                folder_id = str(uuid.uuid4())
                created_at = now
            else:
                folder_id = str(row["folder_id"])
                created_at = str(row["created_at"])
            connection.execute(
                """
                INSERT INTO workspace_folders(
                    folder_id, user_id, display_name, canonical_real_path, real_path,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
                ON CONFLICT(user_id, canonical_real_path) DO UPDATE SET
                    display_name = excluded.display_name,
                    real_path = excluded.real_path,
                    status = 'active',
                    updated_at = excluded.updated_at
                """,
                (
                    folder_id,
                    user_id,
                    normalized_name,
                    canonical_text,
                    str(real_path),
                    created_at,
                    now,
                ),
            )
            _replace_folder_organization_links(
                connection=connection,
                user_id=user_id,
                folder_id=folder_id,
                organization_ids=normalized_organization_ids,
                now=now,
            )
            _replace_folder_project_links(
                connection=connection,
                user_id=user_id,
                folder_id=folder_id,
                project_ids=normalized_project_ids,
                now=now,
            )
    return WorkspaceFolder(
        folder_id=folder_id,
        display_name=normalized_name,
        real_path=str(real_path),
        canonical_real_path=canonical_text,
        organization_ids=normalized_organization_ids,
        project_ids=normalized_project_ids,
    )


def list_workspace_settings(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
) -> WorkspaceSettings:
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        return load_workspace_settings_in_connection(
            connection=connection,
            user_id=user_id,
        )


def load_workspace_settings_in_connection(
    *,
    connection: sqlite3.Connection,
    user_id: str,
) -> WorkspaceSettings:
    read_access_scope = load_read_access_scope_in_connection(
        connection=connection,
        user_id=user_id,
    )
    organization_rows = connection.execute(
        """
        SELECT organization_id, display_name
        FROM workspace_organizations
        WHERE user_id = ? AND status = 'active'
        ORDER BY display_name ASC, organization_id ASC
        """,
        (user_id,),
    ).fetchall()
    project_rows = connection.execute(
        """
        SELECT project_id, display_name, sort_order
        FROM workspace_projects
        WHERE user_id = ? AND status = 'active'
        ORDER BY sort_order ASC, display_name ASC, project_id ASC
        """,
        (user_id,),
    ).fetchall()
    folder_rows = connection.execute(
        """
        SELECT folder_id, display_name, real_path, canonical_real_path
        FROM workspace_folders
        WHERE user_id = ? AND status = 'active'
        ORDER BY display_name ASC, folder_id ASC
        """,
        (user_id,),
    ).fetchall()
    project_orgs = _load_links(
        connection=connection,
        table_name="workspace_project_organizations",
        owner_column="project_id",
        target_column="organization_id",
        user_id=user_id,
    )
    folder_orgs = _load_links(
        connection=connection,
        table_name="workspace_folder_organizations",
        owner_column="folder_id",
        target_column="organization_id",
        user_id=user_id,
    )
    folder_projects = _load_links(
        connection=connection,
        table_name="workspace_folder_projects",
        owner_column="folder_id",
        target_column="project_id",
        user_id=user_id,
    )
    return WorkspaceSettings(
        read_access_scope=read_access_scope,
        organizations=tuple(
            WorkspaceOrganization(
                organization_id=str(row["organization_id"]),
                display_name=str(row["display_name"]),
            )
            for row in organization_rows
        ),
        projects=tuple(
            WorkspaceProject(
                project_id=str(row["project_id"]),
                display_name=str(row["display_name"]),
                sort_order=int(row["sort_order"]),
                organization_ids=project_orgs.get(str(row["project_id"]), ()),
            )
            for row in project_rows
        ),
        folders=tuple(
            WorkspaceFolder(
                folder_id=str(row["folder_id"]),
                display_name=str(row["display_name"]),
                real_path=str(row["real_path"]),
                canonical_real_path=str(row["canonical_real_path"]),
                organization_ids=_folder_organization_ids(
                    organization_ids=folder_orgs.get(str(row["folder_id"]), ()),
                    project_ids=folder_projects.get(str(row["folder_id"]), ()),
                ),
                project_ids=folder_projects.get(str(row["folder_id"]), ()),
            )
            for row in folder_rows
        ),
    )


def replace_workspace_project_links(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    project_id: str,
    organization_ids: tuple[str, ...],
    now: str,
) -> WorkspaceProject:
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        with connection:
            row = _fetch_project_by_id(
                connection=connection,
                user_id=user_id,
                project_id=project_id,
            )
            _replace_project_organization_links(
                connection=connection,
                user_id=user_id,
                project_id=project_id,
                organization_ids=organization_ids,
                now=now,
            )
            connection.execute(
                """
                UPDATE workspace_projects
                SET updated_at = ?
                WHERE user_id = ? AND project_id = ?
                """,
                (now, user_id, project_id),
            )
    return WorkspaceProject(
        project_id=project_id,
        display_name=str(row["display_name"]),
        sort_order=int(row["sort_order"]),
        organization_ids=_unique_sorted(organization_ids),
    )


def replace_workspace_folder_links(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    folder_id: str,
    organization_ids: tuple[str, ...],
    project_ids: tuple[str, ...],
    now: str,
) -> WorkspaceFolder:
    normalized_project_ids = _unique_sorted(project_ids)
    normalized_organization_ids = _folder_organization_ids(
        organization_ids=organization_ids,
        project_ids=normalized_project_ids,
    )
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        with connection:
            row = _fetch_folder_by_id(
                connection=connection,
                user_id=user_id,
                folder_id=folder_id,
            )
            _replace_folder_organization_links(
                connection=connection,
                user_id=user_id,
                folder_id=folder_id,
                organization_ids=normalized_organization_ids,
                now=now,
            )
            _replace_folder_project_links(
                connection=connection,
                user_id=user_id,
                folder_id=folder_id,
                project_ids=normalized_project_ids,
                now=now,
            )
            connection.execute(
                """
                UPDATE workspace_folders
                SET updated_at = ?
                WHERE user_id = ? AND folder_id = ?
                """,
                (now, user_id, folder_id),
            )
    return WorkspaceFolder(
        folder_id=folder_id,
        display_name=str(row["display_name"]),
        real_path=str(row["real_path"]),
        canonical_real_path=str(row["canonical_real_path"]),
        organization_ids=normalized_organization_ids,
        project_ids=normalized_project_ids,
    )


def _normalize_display_name(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise MigrationError("workspace display_name must not be empty")
    return normalized


def _unique_sorted(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(set(values)))


def _folder_organization_ids(
    *,
    organization_ids: tuple[str, ...],
    project_ids: tuple[str, ...],
) -> tuple[str, ...]:
    if project_ids:
        return ()
    return _unique_sorted(organization_ids)


def _fetch_organization_by_name(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    display_name: str,
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT organization_id, display_name
        FROM workspace_organizations
        WHERE user_id = ? AND display_name = ?
        """,
        (user_id, display_name),
    ).fetchone()
    if row is None:
        raise MigrationError("workspace organization upsert did not return a row")
    return cast(sqlite3.Row, row)


def _fetch_project_by_id(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    project_id: str,
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT project_id, display_name, sort_order
        FROM workspace_projects
        WHERE user_id = ? AND project_id = ? AND status = 'active'
        """,
        (user_id, project_id),
    ).fetchone()
    if row is None:
        raise MigrationError(f"workspace project not found: {project_id}")
    return cast(sqlite3.Row, row)


def _fetch_folder_by_id(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    folder_id: str,
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT folder_id, display_name, real_path, canonical_real_path
        FROM workspace_folders
        WHERE user_id = ? AND folder_id = ? AND status = 'active'
        """,
        (user_id, folder_id),
    ).fetchone()
    if row is None:
        raise MigrationError(f"workspace folder not found: {folder_id}")
    return cast(sqlite3.Row, row)


def _replace_project_organization_links(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    project_id: str,
    organization_ids: tuple[str, ...],
    now: str,
) -> None:
    connection.execute(
        "DELETE FROM workspace_project_organizations WHERE user_id = ? AND project_id = ?",
        (user_id, project_id),
    )
    connection.executemany(
        """
        INSERT INTO workspace_project_organizations(
            user_id, project_id, organization_id, created_at
        ) VALUES (?, ?, ?, ?)
        """,
        (
            (user_id, project_id, organization_id, now)
            for organization_id in _unique_sorted(organization_ids)
        ),
    )


def _replace_folder_organization_links(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    folder_id: str,
    organization_ids: tuple[str, ...],
    now: str,
) -> None:
    connection.execute(
        "DELETE FROM workspace_folder_organizations WHERE user_id = ? AND folder_id = ?",
        (user_id, folder_id),
    )
    connection.executemany(
        """
        INSERT INTO workspace_folder_organizations(
            user_id, folder_id, organization_id, created_at
        ) VALUES (?, ?, ?, ?)
        """,
        (
            (user_id, folder_id, organization_id, now)
            for organization_id in _unique_sorted(organization_ids)
        ),
    )


def _replace_folder_project_links(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    folder_id: str,
    project_ids: tuple[str, ...],
    now: str,
) -> None:
    connection.execute(
        "DELETE FROM workspace_folder_projects WHERE user_id = ? AND folder_id = ?",
        (user_id, folder_id),
    )
    connection.executemany(
        """
        INSERT INTO workspace_folder_projects(
            user_id, folder_id, project_id, created_at
        ) VALUES (?, ?, ?, ?)
        """,
        (
            (user_id, folder_id, project_id, now)
            for project_id in _unique_sorted(project_ids)
        ),
    )


def _load_links(
    *,
    connection: sqlite3.Connection,
    table_name: str,
    owner_column: str,
    target_column: str,
    user_id: str,
) -> dict[str, tuple[str, ...]]:
    rows = connection.execute(
        f"""
        SELECT {owner_column} AS owner_id, {target_column} AS target_id
        FROM {table_name}
        WHERE user_id = ?
        ORDER BY {target_column} ASC
        """,
        (user_id,),
    ).fetchall()
    links: dict[str, list[str]] = {}
    for row in rows:
        links.setdefault(str(row["owner_id"]), []).append(str(row["target_id"]))
    return {owner_id: tuple(target_ids) for owner_id, target_ids in links.items()}
