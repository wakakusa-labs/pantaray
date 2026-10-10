from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pantaray_agents.auth_http import get_current_user_id_from_token
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.local_runtime.tooling.repository import (
    WorkspaceProject,
    WorkspaceSettings,
)
from pantaray_agents.local_runtime.tooling.repository.workspace_settings_models import (
    WorkspaceProjectNameTakenError,
)
from pantaray_agents.routers import workspace_settings as router_module


def _client(*, resolved_user_id: str = "user-1") -> TestClient:
    app = FastAPI()
    app.include_router(router_module.router)
    app.dependency_overrides[get_current_user_id_from_token] = lambda: resolved_user_id
    return TestClient(app)


def test_workspace_settings_response_includes_project_sort_order(monkeypatch) -> None:
    monkeypatch.setattr(
        router_module,
        "read_local_runtime_db_config",
        lambda: (Path("runtime.db"), 1_000),
    )
    monkeypatch.setattr(
        router_module,
        "list_workspace_settings",
        lambda **_kwargs: WorkspaceSettings(
            read_access_scope="workspace",
            organizations=(),
            projects=(
                WorkspaceProject(
                    project_id="project-1",
                    display_name="Project",
                    sort_order=3,
                    organization_ids=(),
                ),
            ),
            folders=(),
        ),
    )

    with _client() as client:
        response = client.get("/v1/agents/users/user-1/workspace-settings")

    assert response.status_code == 200
    assert response.json()["projects"][0]["sort_order"] == 3


@pytest.mark.parametrize(
    ("method", "path", "repository_name", "base_body"),
    (
        (
            "post",
            "/v1/agents/users/user-1/workspace-settings/projects",
            "create_workspace_project",
            {"display_name": "Project"},
        ),
        (
            "put",
            "/v1/agents/users/user-1/workspace-settings/projects/project-1/links",
            "replace_workspace_project_links",
            {},
        ),
    ),
)
def test_workspace_project_routes_allow_at_most_one_organization(
    monkeypatch,
    method: str,
    path: str,
    repository_name: str,
    base_body: dict[str, object],
) -> None:
    captured: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        router_module,
        "read_local_runtime_db_config",
        lambda: (Path("runtime.db"), 1_000),
    )

    def _save_project(**kwargs):
        organization_ids = kwargs["organization_ids"]
        captured.append(organization_ids)
        return WorkspaceProject(
            project_id="project-1",
            display_name="Project",
            sort_order=0,
            organization_ids=organization_ids,
        )

    monkeypatch.setattr(router_module, repository_name, _save_project)

    with _client() as client:
        rejected = client.request(
            method,
            path,
            json={**base_body, "organization_ids": ["org-1", "org-2"]},
        )
        accepted = client.request(
            method,
            path,
            json={**base_body, "organization_ids": ["org-1"]},
        )

    assert rejected.status_code == 422
    assert accepted.status_code == 200
    assert accepted.json()["organization_ids"] == ["org-1"]
    assert captured == [("org-1",)]


def test_workspace_project_order_route_forwards_complete_order(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        router_module,
        "read_local_runtime_db_config",
        lambda: (Path("runtime.db"), 1_000),
    )

    def _replace(**kwargs):
        captured.update(kwargs)
        return kwargs["project_ids"]

    monkeypatch.setattr(router_module, "replace_workspace_project_order", _replace)

    with _client() as client:
        response = client.put(
            "/v1/agents/users/user-1/workspace-settings/projects/order",
            json={"project_ids": ["project-b", "project-a"]},
        )

    assert response.status_code == 200
    assert response.json() == {"project_ids": ["project-b", "project-a"]}
    assert captured["user_id"] == "user-1"
    assert captured["project_ids"] == ("project-b", "project-a")


def test_workspace_project_order_route_rejects_duplicate_ids() -> None:
    with _client() as client:
        response = client.put(
            "/v1/agents/users/user-1/workspace-settings/projects/order",
            json={"project_ids": ["project-a", "project-a"]},
        )

    assert response.status_code == 422


def test_workspace_project_order_route_returns_generic_set_mismatch(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        router_module,
        "read_local_runtime_db_config",
        lambda: (Path("runtime.db"), 1_000),
    )
    monkeypatch.setattr(
        router_module,
        "replace_workspace_project_order",
        lambda **_kwargs: (_ for _ in ()).throw(MigrationError("set mismatch")),
    )

    with _client() as client:
        response = client.put(
            "/v1/agents/users/user-1/workspace-settings/projects/order",
            json={"project_ids": ["unknown-project"]},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "Failed to update workspace project order: set mismatch"
    )


def test_workspace_project_order_route_rejects_user_mismatch() -> None:
    with _client(resolved_user_id="user-2") as client:
        response = client.put(
            "/v1/agents/users/user-1/workspace-settings/projects/order",
            json={"project_ids": []},
        )

    assert response.status_code == 403


def test_workspace_project_create_answers_a_taken_name_with_conflict(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        router_module,
        "read_local_runtime_db_config",
        lambda: (Path("runtime.db"), 1_000),
    )

    def _taken(**_kwargs):
        raise WorkspaceProjectNameTakenError("workspace project name is taken: Core")

    monkeypatch.setattr(router_module, "create_workspace_project", _taken)

    with _client() as client:
        response = client.post(
            "/v1/agents/users/user-1/workspace-settings/projects",
            json={"display_name": "Core"},
        )

    assert response.status_code == 409


@pytest.mark.parametrize(
    ("method", "path", "body"),
    (
        (
            "post",
            "/v1/agents/users/user-1/workspace-settings/folders",
            {"display_name": "repo", "real_path": "/tmp/repo", "project_ids": []},
        ),
        (
            "put",
            "/v1/agents/users/user-1/workspace-settings/folders/folder-1/links",
            {"project_ids": []},
        ),
    ),
)
def test_workspace_folder_routes_refuse_a_folder_without_a_project(
    monkeypatch, method: str, path: str, body: dict[str, object]
) -> None:
    monkeypatch.setattr(
        router_module,
        "read_local_runtime_db_config",
        lambda: (Path("runtime.db"), 1_000),
    )

    with _client() as client:
        response = client.request(method, path, json=body)

    assert response.status_code == 422
