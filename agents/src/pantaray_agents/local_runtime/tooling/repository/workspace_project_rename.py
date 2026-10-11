from __future__ import annotations

import sqlite3
from pathlib import Path

from ...storage.migrations import MigrationError
from .common import _configure_connection
from .workspace_settings_models import WorkspaceProject, WorkspaceProjectNameTakenError


def rename_workspace_project(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    project_id: str,
    display_name: str,
    now: str,
) -> WorkspaceProject:
    normalized_name = display_name.strip()
    if not normalized_name:
        raise MigrationError("workspace display_name must not be empty")
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        with connection:
            try:
                row = connection.execute(
                    """
                    UPDATE workspace_projects
                    SET display_name = ?, updated_at = ?
                    WHERE user_id = ? AND project_id = ? AND status = 'active'
                    RETURNING sort_order
                    """,
                    (normalized_name, now, user_id, project_id),
                ).fetchone()
            except sqlite3.IntegrityError as exc:
                raise WorkspaceProjectNameTakenError(
                    f"workspace project name is taken: {normalized_name}"
                ) from exc
            if row is None:
                raise MigrationError(f"workspace project not found: {project_id}")
            organization_ids = tuple(
                str(link["organization_id"])
                for link in connection.execute(
                    """
                    SELECT organization_id
                    FROM workspace_project_organizations
                    WHERE user_id = ? AND project_id = ?
                    ORDER BY organization_id ASC
                    """,
                    (user_id, project_id),
                ).fetchall()
            )
    return WorkspaceProject(
        project_id=project_id,
        display_name=normalized_name,
        sort_order=int(row["sort_order"]),
        organization_ids=organization_ids,
    )


__all__ = ["rename_workspace_project"]
