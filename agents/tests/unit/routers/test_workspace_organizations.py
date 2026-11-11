from __future__ import annotations

import base64
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pantaray_agents.auth_http import get_current_user_id_from_token
from pantaray_agents.local_runtime.storage.migrations import (
    apply_migrations,
    load_default_migrations,
)
from pantaray_agents.routers import workspace_organizations, workspace_settings

SETTINGS_URL = "/v1/agents/users/user-1/workspace-settings"
ORGANIZATIONS_URL = f"{SETTINGS_URL}/organizations"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 32
WEBP_BYTES = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 32


def _data_url(media_type: str, data: bytes) -> str:
    return f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    db_path = tmp_path / "runtime.db"
    apply_migrations(
        db_path=db_path, busy_timeout_ms=1_000, migrations=load_default_migrations()
    )
    for module in (workspace_settings, workspace_organizations):
        monkeypatch.setattr(
            module, "read_local_runtime_db_config", lambda: (db_path, 1_000)
        )
    app = FastAPI()
    app.include_router(workspace_settings.router)
    app.include_router(workspace_organizations.router)
    app.dependency_overrides[get_current_user_id_from_token] = lambda: "user-1"
    return TestClient(app)


def _listed_organizations(client: TestClient) -> list[dict[str, object]]:
    response = client.get(SETTINGS_URL)
    assert response.status_code == 200
    organizations: list[dict[str, object]] = response.json()["organizations"]
    return organizations


@pytest.mark.parametrize(
    ("media_type", "data"),
    (("image/png", PNG_BYTES), ("image/jpeg", JPEG_BYTES), ("image/webp", WEBP_BYTES)),
)
def test_organization_created_with_an_icon_lists_it(
    client: TestClient, media_type: str, data: bytes
) -> None:
    icon = _data_url(media_type, data)

    created = client.post(
        ORGANIZATIONS_URL, json={"display_name": " Acme ", "icon_data_url": icon}
    )

    assert created.status_code == 200
    assert created.json()["display_name"] == "Acme"
    assert created.json()["icon_data_url"] == icon
    assert _listed_organizations(client) == [created.json()]


@pytest.mark.parametrize(
    "icon",
    (
        _data_url("image/png", b"\x89PNG\r\n\x1a\n" + b"\x00" * (64 * 1024)),
        "x" * (2 * 64 * 1024 + 1),
        _data_url("image/gif", b"GIF89a" + b"\x00" * 32),
        _data_url("image/png", JPEG_BYTES),
        _data_url("image/png", b"not an image"),
        "data:image/png;base64,@@@@",
        f"data:image/png,{base64.b64encode(PNG_BYTES).decode('ascii')}",
        base64.b64encode(PNG_BYTES).decode("ascii"),
    ),
    ids=(
        "too-large",
        "too-long",
        "unsupported-type",
        "type-mismatch",
        "not-an-image",
        "invalid-base64",
        "not-base64-url",
        "not-a-data-url",
    ),
)
def test_organization_icon_outside_the_contract_is_refused(
    client: TestClient, icon: str
) -> None:
    created = client.post(
        ORGANIZATIONS_URL, json={"display_name": "Acme", "icon_data_url": icon}
    )

    assert created.status_code == 422
    assert _listed_organizations(client) == []


def test_organization_update_renames_sets_and_clears_the_icon(
    client: TestClient,
) -> None:
    organization_id = client.post(
        ORGANIZATIONS_URL, json={"display_name": "Acme"}
    ).json()["organization_id"]
    icon = _data_url("image/png", PNG_BYTES)
    url = f"{ORGANIZATIONS_URL}/{organization_id}"

    renamed = client.put(url, json={"display_name": "Acme Inc", "icon_data_url": icon})
    assert renamed.status_code == 200
    assert _listed_organizations(client) == [
        {
            "organization_id": organization_id,
            "display_name": "Acme Inc",
            "icon_data_url": icon,
        }
    ]

    cleared = client.put(url, json={"display_name": "Acme Inc", "icon_data_url": None})
    assert cleared.status_code == 200
    assert _listed_organizations(client)[0]["icon_data_url"] is None

    refused = client.put(
        url,
        json={
            "display_name": "Acme Inc",
            "icon_data_url": _data_url("image/png", b"x"),
        },
    )
    assert refused.status_code == 422
    # The icon is the organization's whole state, so leaving it out is not "keep it".
    assert client.put(url, json={"display_name": "Acme Inc"}).status_code == 422


def test_organization_update_refuses_a_taken_name_and_an_unknown_organization(
    client: TestClient,
) -> None:
    client.post(ORGANIZATIONS_URL, json={"display_name": "Acme"})
    other_id = client.post(ORGANIZATIONS_URL, json={"display_name": "Beta"}).json()[
        "organization_id"
    ]

    taken = client.put(
        f"{ORGANIZATIONS_URL}/{other_id}",
        json={"display_name": "Acme", "icon_data_url": None},
    )
    unknown = client.put(
        f"{ORGANIZATIONS_URL}/missing",
        json={"display_name": "Gamma", "icon_data_url": None},
    )

    assert taken.status_code == 409
    assert unknown.status_code == 400
    assert sorted(item["display_name"] for item in _listed_organizations(client)) == [
        "Acme",
        "Beta",
    ]


def test_project_is_created_inside_an_organization_or_without_one(
    client: TestClient,
) -> None:
    organization_id = client.post(
        ORGANIZATIONS_URL, json={"display_name": "Acme"}
    ).json()["organization_id"]

    inside = client.post(
        f"{SETTINGS_URL}/projects",
        json={"display_name": "Core", "organization_ids": [organization_id]},
    )
    outside = client.post(f"{SETTINGS_URL}/projects", json={"display_name": "Notes"})

    assert inside.status_code == 200
    assert outside.status_code == 200
    projects = {
        project["display_name"]: project["organization_ids"]
        for project in client.get(SETTINGS_URL).json()["projects"]
    }
    assert projects == {"Core": [organization_id], "Notes": []}
