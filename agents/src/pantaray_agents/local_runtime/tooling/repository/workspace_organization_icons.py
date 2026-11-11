"""Organization icons: shown only by the app, so kept apart from the agent-facing settings."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ...storage.migrations import MigrationError
from .common import _configure_connection
from .workspace_settings import _normalize_display_name
from .workspace_settings_models import (
    WorkspaceOrganization,
    WorkspaceOrganizationNameTakenError,
)


def load_workspace_organization_icons(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
) -> dict[str, str]:
    """The icon data URL of each active organization that has one, by organization id."""
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        rows = connection.execute(
            """
            SELECT organization_id, icon_data_url
            FROM workspace_organizations
            WHERE user_id = ? AND status = 'active' AND icon_data_url IS NOT NULL
            """,
            (user_id,),
        ).fetchall()
    return {str(row["organization_id"]): str(row["icon_data_url"]) for row in rows}


def update_workspace_organization(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    organization_id: str,
    display_name: str,
    icon_data_url: str | None,
    now: str,
) -> WorkspaceOrganization:
    """Replaces the organization's name and icon; a None icon clears it."""
    normalized_name = _normalize_display_name(display_name)
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        with connection:
            try:
                row = connection.execute(
                    """
                    UPDATE workspace_organizations
                    SET display_name = ?, icon_data_url = ?, updated_at = ?
                    WHERE user_id = ? AND organization_id = ? AND status = 'active'
                    RETURNING organization_id
                    """,
                    (normalized_name, icon_data_url, now, user_id, organization_id),
                ).fetchone()
            except sqlite3.IntegrityError as exc:
                raise WorkspaceOrganizationNameTakenError(
                    f"workspace organization name is taken: {normalized_name}"
                ) from exc
    if row is None:
        raise MigrationError(f"workspace organization not found: {organization_id}")
    return WorkspaceOrganization(
        organization_id=organization_id, display_name=normalized_name
    )


__all__ = ["load_workspace_organization_icons", "update_workspace_organization"]
