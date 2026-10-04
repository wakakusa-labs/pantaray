import json

import pytest

from pantaray_agents.local_runtime.action_conversation.tool_subject import (
    OUTPUT_PREVIEW_MAX_CHARACTERS,
    SUBJECT_MAX_CHARACTERS,
    project_tool_output_preview,
    project_tool_subject,
)
from pantaray_agents.schema.agent.base import JSONValue


def _args(tool_id: str, **args: JSONValue) -> str:
    return json.dumps({"tool_id": tool_id, "args": args})


@pytest.mark.parametrize(
    ("tool_id", "tool_args", "expected"),
    [
        pytest.param("read", _args("read", path="src/app.py", limit=40), "src/app.py"),
        pytest.param("list", _args("list", path="src/", max_depth=2), "src/"),
        pytest.param(
            "glob", _args("glob", base_path=".", pattern="**/*.py"), "**/*.py"
        ),
        pytest.param(
            "grep",
            _args("grep", base_path="src", pattern="retrieval"),
            "retrieval (src)",
            id="grep-names-where-it-looked",
        ),
        pytest.param("grep", _args("grep", pattern="retrieval"), "retrieval"),
        pytest.param(
            "bash",
            _args("bash", command="PYTHONPATH=src python - <<'PY'\nprint(1)\nPY"),
            "PYTHONPATH=src python - <<'PY'…",
            id="bash-keeps-the-first-command-line",
        ),
        pytest.param(
            "run_python",
            _args("run_python", code="import os\nos.getcwd()"),
            "import os…",
        ),
        pytest.param(
            "apply_patch",
            _args(
                "apply_patch",
                changes=[
                    {"op": "update", "path": "a.py", "edits": []},
                    {"op": "delete", "path": "b.py"},
                ],
            ),
            "a.py, b.py",
            id="apply-patch-names-every-touched-file",
        ),
        pytest.param(
            "capture_screen",
            _args("capture_screen", app_name="Google Chrome"),
            "Google Chrome",
        ),
        pytest.param(
            "web_search",
            _args(
                "web_search", query="Japanese information retrieval", topic="general"
            ),
            "Japanese information retrieval",
        ),
        pytest.param(
            "web_extract",
            _args("web_extract", urls=["https://a.example", "https://b.example"]),
            "https://a.example, https://b.example",
        ),
        pytest.param(
            "web_crawl",
            _args("web_crawl", url="https://a.example"),
            "https://a.example",
        ),
        pytest.param("memory_search", _args("memory_search", query="議事録"), "議事録"),
        pytest.param(
            "memory_sql",
            _args("memory_sql", sql="SELECT id\nFROM nodes"),
            "SELECT id…",
        ),
        pytest.param(
            "get_memory_reference",
            _args("get_memory_reference", local_ref_id="ref-1"),
            "ref-1",
            id="memory-reference-falls-back-to-the-local-ref",
        ),
        pytest.param(
            "history_fetch", _args("history_fetch", refs=["h1", "h2"]), "h1, h2"
        ),
        pytest.param(
            "zanei_query", _args("zanei_query", event_id="e-1", field="url"), "e-1"
        ),
        pytest.param(
            "spawn_subagent",
            _args(
                "spawn_subagent", model="fast", task="Summarize the paper\nin Japanese"
            ),
            "Summarize the paper…",
        ),
        pytest.param(
            "send_message_to_subagent",
            _args(
                "send_message_to_subagent", child_process_id="p-1", content="stop now"
            ),
            "stop now",
        ),
        pytest.param(
            "wait_subagents",
            _args("wait_subagents", child_process_ids=["p-1", "p-2"]),
            "p-1, p-2",
        ),
        pytest.param(
            "cancel_subagent", _args("cancel_subagent", child_process_id="p-1"), "p-1"
        ),
    ],
)
def test_projects_the_one_salient_argument(
    tool_id: str, tool_args: str, expected: str
) -> None:
    assert project_tool_subject(tool_id, tool_args) == expected


@pytest.mark.parametrize(
    ("tool_id", "tool_args"),
    [
        pytest.param("zanei_timeline", _args("zanei_timeline"), id="no-arguments"),
        pytest.param(
            "draft_final_answer",
            _args("draft_final_answer", answer="長い回答"),
            id="the-answer-is-not-a-subject",
        ),
        pytest.param("read", None, id="row-without-arguments"),
        pytest.param("read", _args("read"), id="missing-named-argument"),
        pytest.param("read", _args("read", path="   "), id="blank-argument"),
        pytest.param("read", '{"tool_id":"read"}', id="envelope-without-args"),
        pytest.param("read", '"read"', id="envelope-that-is-not-an-object"),
        pytest.param(
            "read", _args("read", path=["src/app.py"]), id="wrongly-typed-argument"
        ),
        pytest.param(
            "apply_patch",
            _args("apply_patch", changes=[{"op": "add"}]),
            id="pathless-change",
        ),
        pytest.param(
            "brand_new_tool", _args("brand_new_tool", path="x"), id="unknown-tool"
        ),
    ],
)
def test_has_no_subject(tool_id: str, tool_args: str | None) -> None:
    assert project_tool_subject(tool_id, tool_args) is None


def test_bounds_a_long_subject_and_says_it_was_cut() -> None:
    subject = project_tool_subject("bash", _args("bash", command="x" * 400))

    assert subject is not None
    assert len(subject) == SUBJECT_MAX_CHARACTERS + 1
    assert subject.endswith("…")


def test_flattens_a_wrapped_subject_onto_one_line() -> None:
    assert (
        project_tool_subject("web_search", _args("web_search", query="  a\t\t b  "))
        == "a b"
    )


def test_previews_a_textual_result_without_its_json_quoting() -> None:
    assert (
        project_tool_output_preview("read", "done\nwith it", "inline_json")
        == "done with it"
    )


def test_bounds_a_long_preview() -> None:
    preview = project_tool_output_preview("read", "word " * 200, "inline_json")

    assert preview is not None
    assert len(preview) == OUTPUT_PREVIEW_MAX_CHARACTERS + 1
    assert preview.endswith("…")


@pytest.mark.parametrize(
    ("tool_id", "output", "storage_kind"),
    [
        pytest.param("read", None, "inline_json", id="no-output"),
        pytest.param("read", "", "inline_json", id="empty-output"),
        pytest.param(
            "read",
            {"storage": "action_file", "path": "output-0.json"},
            "action_file",
            id="spilled-result-metadata",
        ),
        pytest.param(
            "read", "data:image/png;base64,iVBORw0KGgo=", "inline_json", id="data-url"
        ),
        pytest.param(
            "read",
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk",
            "inline_json",
            id="encoded-bytes",
        ),
        pytest.param(
            # The encoded run starts late enough that truncation would leave fewer
            # characters than the pattern needs, so the cut must not come first.
            "read",
            "captured screen shot of the frontmost window, encoded as "
            + "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk",
            "inline_json",
            id="encoded-bytes-past-the-cut",
        ),
        pytest.param(
            "read",
            {"status": "captured", "app_name": "Finder"},
            "inline_json",
            id="object-result",
        ),
        pytest.param("read", [{"path": "src/app.py"}], "inline_json", id="list-result"),
        pytest.param(
            "read", '{"status": "draft_updated"}', "inline_json", id="json-text"
        ),
        pytest.param(
            # Hidden reasoning stays hidden even when it arrives as plain prose.
            "thinking",
            "The user is asking about retrieval, so the answer should weigh…",
            "inline_json",
            id="hidden-reasoning",
        ),
    ],
)
def test_has_no_preview(tool_id: str, output: JSONValue, storage_kind: str) -> None:
    assert (
        project_tool_output_preview(tool_id, output, storage_kind) is None  # type: ignore[arg-type]
    )
