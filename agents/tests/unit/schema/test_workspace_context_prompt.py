from __future__ import annotations

from pathlib import Path

from tests.unit.local_runtime.test_workspace_settings_repository import (
    TIMESTAMP,
    _bootstrap_db,
)

from pantaray_agents.local_runtime.tooling.repository.workspace_context import (
    load_workspace_context_catalog,
)
from pantaray_agents.local_runtime.tooling.repository.workspace_settings import (
    create_workspace_folder,
    create_workspace_organization,
    create_workspace_project,
)
from pantaray_agents.schema.workspace_context import (
    WorkspaceContextCatalog,
    WorkspaceContextFolder,
    WorkspaceContextOrganization,
    WorkspaceContextProject,
    render_workspace_context_prompt,
)


def test_render_workspace_context_prompt_omits_empty_catalog() -> None:
    catalog = WorkspaceContextCatalog(organizations=(), projects=(), folders=())

    assert render_workspace_context_prompt(catalog) == (
        "Read/search access: workspace roots only\n"
        "Edit/command access: workspace roots only"
    )


def test_render_workspace_context_prompt_is_optional_and_uses_local_paths() -> None:
    catalog = WorkspaceContextCatalog(
        organizations=(WorkspaceContextOrganization(display_name="Wakakusa"),),
        projects=(
            WorkspaceContextProject(
                display_name="Pantaray",
                organization_names=("Wakakusa",),
            ),
        ),
        folders=(
            WorkspaceContextFolder(
                display_name="Product Repo",
                path="/Users/example/product-repo",
                organization_names=("Wakakusa",),
                project_names=("Pantaray",),
            ),
        ),
    )

    rendered = render_workspace_context_prompt(catalog)

    assert "Workspace Context" not in rendered
    assert "Use this information only as optional context" not in rendered
    assert "Do not assume this list is complete" not in rendered
    assert "Read/search access: workspace roots only" in rendered
    assert "Edit/command access: workspace roots only" in rendered
    assert "- Wakakusa" in rendered
    assert "- Pantaray (organizations: Wakakusa)" in rendered
    assert (
        "- Product Repo: /Users/example/product-repo "
        "(projects: Pantaray; organizations: Wakakusa)"
    ) in rendered


def test_load_workspace_context_catalog_maps_ids_to_display_names(
    tmp_path: Path,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    organization = create_workspace_organization(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        display_name="Wakakusa",
        now=TIMESTAMP,
    )
    project = create_workspace_project(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        display_name="Pantaray",
        organization_ids=(organization.organization_id,),
        now=TIMESTAMP,
    )
    folder = create_workspace_folder(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        real_path=repo_path,
        display_name="Product Repo",
        organization_ids=(),
        project_ids=(project.project_id,),
        now=TIMESTAMP,
    )

    catalog = load_workspace_context_catalog(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
    )

    assert catalog.organizations[0].display_name == "Wakakusa"
    assert catalog.projects[0].organization_names == ("Wakakusa",)
    assert catalog.folders[0].project_names == ("Pantaray",)
    assert catalog.folders[0].organization_names == ("Wakakusa",)
    assert catalog.folders[0].path == folder.real_path
    assert catalog.read_access_scope == "full_access"


def test_unassigned_folder_keeps_direct_organization_context(tmp_path: Path) -> None:
    db_path = _bootstrap_db(tmp_path)
    repo_path = tmp_path / "unassigned-repo"
    repo_path.mkdir()
    organization = create_workspace_organization(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        display_name="Direct Organization",
        now=TIMESTAMP,
    )
    create_workspace_folder(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        real_path=repo_path,
        display_name="Unassigned Repo",
        organization_ids=(organization.organization_id,),
        project_ids=(),
        now=TIMESTAMP,
    )

    catalog = load_workspace_context_catalog(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
    )

    assert catalog.folders[0].project_names == ()
    assert catalog.folders[0].organization_names == ("Direct Organization",)
