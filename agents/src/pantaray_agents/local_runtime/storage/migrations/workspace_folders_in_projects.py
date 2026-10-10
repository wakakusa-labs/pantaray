from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime

from pantaray_agents.local_runtime.activity_summary_schedule import iso_z


def apply_workspace_folders_in_projects_migration(
    connection: sqlite3.Connection,
) -> None:
    """Puts each registered folder no project holds into a new project of its own.

    The app lists folders only under their projects, and a registered folder stays inside
    the read, edit and command boundary, so one that no project holds would grant access
    while nothing shows it. Each such folder gets a new project named after it, numbered
    as the app numbers a taken name, and takes its organizations along: a project may hold
    several, and the workspace context reads them all.
    """
    now = iso_z(datetime.now(UTC))
    folders = connection.execute(
        """
        SELECT folder.user_id, folder.folder_id, folder.display_name
        FROM workspace_folders AS folder
        WHERE folder.status = 'active'
          AND NOT EXISTS (
              SELECT 1
              FROM workspace_folder_projects AS link
              JOIN workspace_projects AS project
                  ON project.user_id = link.user_id
                 AND project.project_id = link.project_id
                 AND project.status = 'active'
              WHERE link.user_id = folder.user_id AND link.folder_id = folder.folder_id
          )
        ORDER BY folder.user_id, folder.display_name, folder.folder_id
        """
    ).fetchall()
    taken_names: dict[str, set[str]] = {}
    next_sort_order: dict[str, int] = {}
    for user_id, folder_id, folder_name in folders:
        if user_id not in taken_names:
            taken_names[user_id] = {
                str(row[0])
                for row in connection.execute(
                    "SELECT display_name FROM workspace_projects WHERE user_id = ?",
                    (user_id,),
                )
            }
            next_sort_order[user_id] = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sort_order) + 1, 0) FROM workspace_projects"
                    " WHERE user_id = ?",
                    (user_id,),
                ).fetchone()[0]
            )
        project_name = folder_name
        copy = 2
        while project_name in taken_names[user_id]:
            project_name = f"{folder_name} {copy}"
            copy += 1
        taken_names[user_id].add(project_name)
        project_id = str(uuid.uuid4())
        connection.execute(
            """
            INSERT INTO workspace_projects(
                project_id, user_id, display_name, sort_order, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'active', ?, ?)
            """,
            (project_id, user_id, project_name, next_sort_order[user_id], now, now),
        )
        next_sort_order[user_id] += 1
        connection.execute(
            "INSERT INTO workspace_folder_projects(user_id, folder_id, project_id, created_at)"
            " VALUES (?, ?, ?, ?)",
            (user_id, folder_id, project_id, now),
        )
        # A folder's own organizations count only while no project holds it.
        connection.execute(
            """
            INSERT INTO workspace_project_organizations(
                user_id, project_id, organization_id, created_at
            )
            SELECT user_id, ?, organization_id, ?
            FROM workspace_folder_organizations
            WHERE user_id = ? AND folder_id = ?
            """,
            (project_id, now, user_id, folder_id),
        )
        connection.execute(
            "DELETE FROM workspace_folder_organizations WHERE user_id = ? AND folder_id = ?",
            (user_id, folder_id),
        )
