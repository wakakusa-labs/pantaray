from __future__ import annotations

from pantaray_agents.agents.action_agent.tools import (
    BASH_TOOL,
    GLOB_TOOL,
    GREP_TOOL,
    LIST_TOOL,
    SUPERVISOR_SINGLE_REACT_TOOL_IDS,
    TOOL_REGISTRY,
)


def _tool_field_description(tool_id: str, field_name: str) -> str:
    properties = TOOL_REGISTRY[tool_id].build_validation_input_schema()["properties"]
    assert isinstance(properties, dict)
    field = properties[field_name]
    assert isinstance(field, dict)
    return str(field["description"])


def _field_description(field_name: str) -> str:
    return _tool_field_description(BASH_TOOL.tool_id, field_name)


def _field_names(tool_id: str) -> list[str]:
    properties = TOOL_REGISTRY[tool_id].build_validation_input_schema()["properties"]
    assert isinstance(properties, dict)
    return sorted(properties)


def test_bash_tool_contract_explains_shell_scope_and_lifetime() -> None:
    command_description = _field_description("command")
    cwd_description = _field_description("cwd")
    combined = "\n".join(
        [
            BASH_TOOL.description,
            BASH_TOOL.guide.what,
            BASH_TOOL.guide.when,
            BASH_TOOL.guide.pitfalls,
            command_description,
            cwd_description,
        ]
    )

    assert "non-interactive shell command or script" in combined
    assert "Pipes, redirections, heredocs" in command_description
    assert "environment assignments" in command_description
    assert "Set cwd to a workspace path" in cwd_description
    assert "path arguments are relative to cwd" in combined
    assert (
        "Direct file content edits should normally go through apply_patch" in combined
    )
    assert "HOME unless use_login_environment is set, are temporary" in combined
    assert "user's command network setting" in combined
    assert "finite execution timeout" in combined
    assert "Shell startup files are not loaded" in combined


def test_discovery_tool_contracts_explain_read_scope_path_semantics() -> None:
    assert "local" in LIST_TOOL.prompt_contract.description
    assert "local" in GLOB_TOOL.prompt_contract.description
    assert "local" in GREP_TOOL.prompt_contract.description
    assert _field_names("list") == ["limit", "max_depth", "path"]
    assert _field_names("glob") == ["base_path", "limit", "pattern"]
    assert _field_names("grep") == [
        "base_path",
        "include_glob",
        "max_matches",
        "pattern",
    ]
    discovery_guides = "\n".join(
        [
            LIST_TOOL.guide.pitfalls,
            GLOB_TOOL.guide.pitfalls,
            GREP_TOOL.guide.pitfalls,
        ]
    )
    assert "local path" in discovery_guides
    assert "Workspace Path Rules" in discovery_guides
    assert "Read/search access" in discovery_guides
    assert "Relative paths" in discovery_guides
    assert "retry_hint" in discovery_guides
    assert "warning" in discovery_guides
    assert "next_action_hint" not in discovery_guides
    list_contract = "\n".join(
        [
            LIST_TOOL.guide.pitfalls,
            _tool_field_description("list", "max_depth"),
            _tool_field_description("list", "limit"),
        ]
    )
    assert "List is not paginated" in list_contract
    assert "do not pass offset" in list_contract
    assert "do not expect next_offset" in list_contract
    assert "1-500" in list_contract
    assert "not a page size" in list_contract
    assert "default 2" in list_contract
    assert "literal text" in GREP_TOOL.guide.pitfalls
    assert "include_glob" in GREP_TOOL.guide.pitfalls


def test_discovery_tools_are_local_parent_tools() -> None:
    for tool in (LIST_TOOL, GLOB_TOOL, GREP_TOOL):
        assert TOOL_REGISTRY[tool.tool_id] is tool
        assert tool.tool_id in SUPERVISOR_SINGLE_REACT_TOOL_IDS


def test_command_tools_ask_for_outside_write_folders_with_a_user_facing_reason() -> (
    None
):
    description = BASH_TOOL.prompt_contract.description
    justification = _field_description("justification")
    assert "rerun the same call with additional_write_folders" in description
    assert "Do not ask the user in chat first" in description
    assert (
        "Give justification whenever you set use_login_environment or "
        "additional_write_folders" in justification
    )
    assert "language of the user's request" in justification
    assert "naming only the service the command actually uses" in justification
    assert "Do not include command names, paths, or file names" in justification
    assert _tool_field_description("run_python", "justification") == justification
    assert (
        "additional_write_folders and justification exactly as the bash tool"
        in TOOL_REGISTRY["run_python"].prompt_contract.description
    )
    assert "approved cwd" not in _field_description("use_login_environment")
