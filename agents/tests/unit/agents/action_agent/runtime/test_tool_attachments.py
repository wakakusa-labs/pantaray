from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from pantaray_agents.agents.action_agent.runtime.handlers.nodes.llm.turn_input import (
    supervisor_prompt_scope_handles,
)
from pantaray_agents.agents.action_agent.runtime.models.conversation import (
    GoalCompletionProposalModel,
    GoalConversationStateModel,
)
from pantaray_agents.agents.action_agent.runtime.tool_attachments import (
    coerce_tool_attachments,
    collect_state_prompt_file_inputs,
)
from pantaray_agents.agents.core.llm_file_inputs import interleave_file_inputs
from pantaray_agents.local_runtime.llm_proxy.content_files import (
    FileInputAccessError,
    WorkspaceFileChangedError,
    prepare_content_file,
)


def test_collects_file_inputs_in_prompt_ref_order() -> None:
    first = {
        "type": "file",
        "source_kind": "workspace_file",
        "ref": "tool_attachment:first",
        "blob_ref": "attachment_blob_first",
        "display_path": "/Users/example/project/a.pdf",
        "workspace_root_path": "/Users/example/project",
        "workspace_relative_path": "a.pdf",
        "mime_type": "application/pdf",
        "byte_size": 10,
        "sha256": "a" * 64,
    }
    second = {
        "type": "file",
        "source_kind": "workspace_file",
        "ref": "tool_attachment:second",
        "blob_ref": "attachment_blob_second",
        "display_path": "/Users/example/project/b.png",
        "workspace_root_path": "/Users/example/project",
        "workspace_relative_path": "b.png",
        "mime_type": "image/png",
        "byte_size": 20,
        "sha256": "b" * 64,
    }
    state = {
        "history_by_scope": {
            "S": [
                {"attachments": [first]},
                {"attachments": [second]},
            ]
        },
    }
    prompt = "later tool_attachment:second then earlier tool_attachment:first"

    file_inputs = collect_state_prompt_file_inputs(
        state=state,
        prompt=prompt,
        scope_handles=("S",),
    )

    assert [item["ref"] for item in file_inputs] == [
        "tool_attachment:second",
        "tool_attachment:first",
    ]
    assert coerce_tool_attachments([first])[0]["source_kind"] == "workspace_file"


def test_collects_only_referenced_attachments_from_requested_scopes() -> None:
    pending_attachment = {
        "type": "file",
        "source_kind": "workspace_file",
        "ref": "tool_attachment:pending",
        "blob_ref": "attachment_blob_pending",
        "display_path": "/Users/example/project/pending.pdf",
        "workspace_root_path": "/Users/example/project",
        "workspace_relative_path": "pending.pdf",
        "mime_type": "application/pdf",
        "byte_size": 30,
        "sha256": "c" * 64,
    }
    unrelated_attachment = {
        **pending_attachment,
        "ref": "tool_attachment:unrelated",
        "blob_ref": "attachment_blob_unrelated",
        "display_path": "/Users/example/project/unrelated.pdf",
        "workspace_relative_path": "unrelated.pdf",
        "sha256": "d" * 64,
    }
    state = {
        "history_by_scope": {
            "G1": [{"attachments": [pending_attachment]}],
            "G2": [{"attachments": [unrelated_attachment]}],
        },
        "context": {
            "last_tool_result_by_scope": {"G1": {"attachments": [unrelated_attachment]}}
        },
    }
    prompt = "Review tool_attachment:pending and tool_attachment:unrelated"

    file_inputs = collect_state_prompt_file_inputs(
        state=state,
        prompt=prompt,
        scope_handles=("S", "G1"),
    )

    assert [item["ref"] for item in file_inputs] == ["tool_attachment:pending"]


def test_supervisor_prompt_scopes_include_all_rendered_goal_conversations() -> None:
    pending = GoalConversationStateModel(
        goal_id="G1",
        next_turn_number=2,
        pending_completion=GoalCompletionProposalModel(
            proposal_id="proposal-1",
            turn_number=1,
            output="Ready for review",
            submitted_at="2026-07-18T00:00:00+00:00",
        ),
    )
    state = {
        "goal_conversations": {
            "G1": pending,
            "G2": GoalConversationStateModel(goal_id="G2"),
        }
    }

    assert supervisor_prompt_scope_handles(state) == (  # type: ignore[arg-type]
        "S",
        "G1",
        "G2",
    )


def test_supervisor_collects_attachment_from_non_pending_worker_conversation() -> None:
    attachment = {
        "type": "file",
        "source_kind": "workspace_file",
        "ref": "tool_attachment:worker-message",
        "blob_ref": "attachment_blob_worker_message",
        "display_path": "/tmp/worker-message.pdf",
        "workspace_root_path": "/tmp",
        "workspace_relative_path": "worker-message.pdf",
        "mime_type": "application/pdf",
        "byte_size": 30,
        "sha256": "e" * 64,
    }
    state = {
        "goal_conversations": {
            "G1": GoalConversationStateModel(goal_id="G1"),
        },
        "history_by_scope": {
            "S": [],
            "G1": [{"attachments": [attachment]}],
        },
    }

    file_inputs = collect_state_prompt_file_inputs(
        state=state,
        prompt="Review tool_attachment:worker-message",
        scope_handles=supervisor_prompt_scope_handles(state),  # type: ignore[arg-type]
    )

    assert [item["ref"] for item in file_inputs] == ["tool_attachment:worker-message"]


def test_interleaves_file_blocks_at_attachment_refs() -> None:
    prompt = "before tool_attachment:first after"
    file_inputs = [
        {
            "ref": "tool_attachment:first",
            "blob_ref": "attachment_blob_first",
            "source_kind": "workspace_file",
            "workspace_root_path": "/tmp",
            "workspace_relative_path": "a.pdf",
            "mime_type": "application/pdf",
            "byte_size": 10,
            "sha256": "a" * 64,
        }
    ]

    contents = interleave_file_inputs(prompt=prompt, file_inputs=file_inputs)

    assert contents[0] == "before tool_attachment:first"
    assert contents[1] == {
        "file_data": {
            "blob_ref": "attachment_blob_first",
            "application_ref": "tool_attachment:first",
            "mime_type": "application/pdf",
            "source_kind": "workspace_file",
            "workspace_root_path": "/tmp",
            "workspace_relative_path": "a.pdf",
            "byte_size": 10,
            "sha256": "a" * 64,
        }
    }
    assert contents[2] == " after"


def test_workspace_file_input_revalidates_original_file_before_llm_upload(
    tmp_path: Path,
) -> None:
    payload = b"%PDF-1.7\nold\n"
    source_path = tmp_path / "source.pdf"
    source_path.write_bytes(payload)
    sha256 = hashlib.sha256(payload).hexdigest()
    attachment = {
        "type": "file",
        "source_kind": "workspace_file",
        "ref": "tool_attachment:workspace",
        "blob_ref": "attachment_blob_workspace",
        "display_path": str(source_path),
        "workspace_root_path": str(tmp_path),
        "workspace_relative_path": "source.pdf",
        "mime_type": "application/pdf",
        "byte_size": len(payload),
        "sha256": sha256,
    }

    file_inputs = collect_state_prompt_file_inputs(
        state={
            "history_by_scope": {"S": [{"attachments": [attachment]}]},
        },
        prompt="Review tool_attachment:workspace",
        scope_handles=("S",),
    )
    contents = interleave_file_inputs(
        prompt="Review tool_attachment:workspace",
        file_inputs=file_inputs,
    )
    file_block = contents[1]
    assert isinstance(file_block, dict)
    prepared = prepare_content_file(
        file_data=file_block["file_data"],
        user_id="user-1",
    )

    assert file_inputs[0]["workspace_root_path"] == str(tmp_path)
    assert file_inputs[0]["workspace_relative_path"] == "source.pdf"
    assert prepared.payload == payload
    assert prepared.sha256 == sha256

    # Same size, other bytes: only the digest tells them apart.
    source_path.write_bytes(b"%PDF-1.7\nnew\n")
    with pytest.raises(WorkspaceFileChangedError) as changed:
        prepare_content_file(
            file_data=file_block["file_data"],
            user_id="user-1",
        )
    assert changed.value.application_ref == "tool_attachment:workspace"
    assert changed.value.path == source_path


def test_workspace_file_input_rejects_symlink_replacement_without_reading_target(
    tmp_path: Path,
) -> None:
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    source_path = workspace_path / "source.pdf"
    original_payload = b"%PDF-1.7\ntrusted\n"
    source_path.write_bytes(original_payload)
    outside_path = tmp_path / "outside.pdf"
    outside_payload = b"%PDF-1.7\noutside-secret\n"
    outside_path.write_bytes(outside_payload)
    file_data = {
        "source_kind": "workspace_file",
        "blob_ref": "attachment_blob_workspace",
        "application_ref": "tool_attachment:workspace",
        "mime_type": "application/pdf",
        "workspace_root_path": str(workspace_path),
        "workspace_relative_path": "source.pdf",
        "byte_size": len(outside_payload),
        "sha256": hashlib.sha256(outside_payload).hexdigest(),
    }
    source_path.unlink()
    source_path.symlink_to(outside_path)

    with pytest.raises(WorkspaceFileChangedError):
        prepare_content_file(
            file_data=file_data,
            user_id="user-1",
        )


def test_workspace_file_input_rejects_intermediate_directory_symlink(
    tmp_path: Path,
) -> None:
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    outside_path = tmp_path / "outside"
    outside_path.mkdir()
    payload = b"%PDF-1.7\noutside-secret\n"
    (outside_path / "source.pdf").write_bytes(payload)
    (workspace_path / "linked").symlink_to(outside_path, target_is_directory=True)

    with pytest.raises(WorkspaceFileChangedError):
        prepare_content_file(
            file_data={
                "source_kind": "workspace_file",
                "blob_ref": "attachment_blob_workspace",
                "application_ref": "tool_attachment:workspace",
                "mime_type": "application/pdf",
                "workspace_root_path": str(workspace_path),
                "workspace_relative_path": "linked/source.pdf",
                "byte_size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            },
            user_id="user-1",
        )


def test_workspace_file_input_rejects_root_directory_symlink_replacement(
    tmp_path: Path,
) -> None:
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    (workspace_path / "source.pdf").write_bytes(b"%PDF-1.7\ntrusted\n")
    moved_workspace_path = tmp_path / "moved-workspace"
    workspace_path.rename(moved_workspace_path)
    outside_path = tmp_path / "outside"
    outside_path.mkdir()
    payload = b"%PDF-1.7\noutside-secret\n"
    (outside_path / "source.pdf").write_bytes(payload)
    workspace_path.symlink_to(outside_path, target_is_directory=True)

    with pytest.raises(WorkspaceFileChangedError):
        prepare_content_file(
            file_data={
                "source_kind": "workspace_file",
                "blob_ref": "attachment_blob_workspace",
                "application_ref": "tool_attachment:workspace",
                "mime_type": "application/pdf",
                "workspace_root_path": str(workspace_path),
                "workspace_relative_path": "source.pdf",
                "byte_size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            },
            user_id="user-1",
        )


def test_workspace_file_input_rejects_parent_traversal() -> None:
    with pytest.raises(FileInputAccessError, match="unsafe path component"):
        prepare_content_file(
            file_data={
                "source_kind": "workspace_file",
                "blob_ref": "attachment_blob_workspace",
                "application_ref": "tool_attachment:workspace",
                "mime_type": "application/pdf",
                "workspace_root_path": "/workspace",
                "workspace_relative_path": "../outside.pdf",
                "byte_size": 1,
                "sha256": "a" * 64,
            },
            user_id="user-1",
        )


def test_workspace_file_input_rejects_special_file_before_read(
    tmp_path: Path,
) -> None:
    fifo_path = tmp_path / "attachment.pdf"
    os.mkfifo(fifo_path)

    with pytest.raises(WorkspaceFileChangedError):
        prepare_content_file(
            file_data={
                "source_kind": "workspace_file",
                "blob_ref": "attachment_blob_workspace",
                "application_ref": "tool_attachment:workspace",
                "mime_type": "application/pdf",
                "workspace_root_path": str(tmp_path),
                "workspace_relative_path": "attachment.pdf",
                "byte_size": 1,
                "sha256": "a" * 64,
            },
            user_id="user-1",
        )


_USER_IMAGE_PAYLOAD = b"\x89PNG\r\n\x1a\nuser-image"
_USER_IMAGE_SHA256 = hashlib.sha256(_USER_IMAGE_PAYLOAD).hexdigest()
_USER_IMAGE_STORAGE_PATH = "user-1/2026-09-08/1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed.png"


def _user_image_attachment() -> dict[str, object]:
    return {
        "type": "file",
        "source_kind": "local_image_blob",
        "ref": f"user_attachment:{_USER_IMAGE_SHA256[:24]}",
        "blob_ref": f"attachment_blob_{_USER_IMAGE_SHA256[:24]}",
        "display_path": "image-1.png",
        "storage_path": _USER_IMAGE_STORAGE_PATH,
        "mime_type": "image/png",
        "byte_size": len(_USER_IMAGE_PAYLOAD),
        "sha256": _USER_IMAGE_SHA256,
    }


def _write_user_image(artifact_root: Path, payload: bytes) -> None:
    image_path = artifact_root / "generated" / "images" / _USER_IMAGE_STORAGE_PATH
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(payload)


def _user_image_file_block(prompt: str) -> dict[str, object]:
    file_inputs = collect_state_prompt_file_inputs(
        state={
            "history_by_scope": {"S": [{"attachments": [_user_image_attachment()]}]}
        },
        prompt=prompt,
        scope_handles=("S",),
    )
    contents = interleave_file_inputs(prompt=prompt, file_inputs=file_inputs)
    file_block = contents[1]
    assert isinstance(file_block, dict)
    return file_block["file_data"]


def test_user_image_attachment_is_read_by_storage_path_as_an_image_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = tmp_path / "artifacts"
    _write_user_image(artifact_root, _USER_IMAGE_PAYLOAD)
    monkeypatch.setenv("LOCAL_ARTIFACT_ROOT", str(artifact_root))
    prompt = f"Attached images: user_attachment:{_USER_IMAGE_SHA256[:24]}"

    file_data = _user_image_file_block(prompt)
    assert "blob_path" not in file_data
    assert file_data["storage_path"] == _USER_IMAGE_STORAGE_PATH

    prepared = prepare_content_file(
        file_data=file_data,
        user_id="user-1",
    )

    assert prepared.payload == _USER_IMAGE_PAYLOAD
    assert prepared.sha256 == _USER_IMAGE_SHA256
    assert prepared.application_ref == f"user_attachment:{_USER_IMAGE_SHA256[:24]}"


def test_user_image_attachment_is_scoped_to_the_requesting_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = tmp_path / "artifacts"
    _write_user_image(artifact_root, _USER_IMAGE_PAYLOAD)
    monkeypatch.setenv("LOCAL_ARTIFACT_ROOT", str(artifact_root))
    prompt = f"Attached images: user_attachment:{_USER_IMAGE_SHA256[:24]}"

    with pytest.raises(FileInputAccessError, match="invalid storage_path"):
        prepare_content_file(
            file_data=_user_image_file_block(prompt),
            user_id="user-2",
        )


def test_user_image_attachment_rejects_replaced_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = tmp_path / "artifacts"
    _write_user_image(artifact_root, _USER_IMAGE_PAYLOAD)
    monkeypatch.setenv("LOCAL_ARTIFACT_ROOT", str(artifact_root))
    prompt = f"Attached images: user_attachment:{_USER_IMAGE_SHA256[:24]}"
    file_data = _user_image_file_block(prompt)
    _write_user_image(artifact_root, _USER_IMAGE_PAYLOAD + b"tampered")

    with pytest.raises(RuntimeError, match="byte_size mismatch"):
        prepare_content_file(
            file_data=file_data,
            user_id="user-1",
        )


def test_user_image_attachment_rejects_deleted_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = tmp_path / "artifacts"
    _write_user_image(artifact_root, _USER_IMAGE_PAYLOAD)
    monkeypatch.setenv("LOCAL_ARTIFACT_ROOT", str(artifact_root))
    prompt = f"Attached images: user_attachment:{_USER_IMAGE_SHA256[:24]}"
    file_data = _user_image_file_block(prompt)
    (artifact_root / "generated" / "images" / _USER_IMAGE_STORAGE_PATH).unlink()

    with pytest.raises(FileInputAccessError, match="was not found"):
        prepare_content_file(
            file_data=file_data,
            user_id="user-1",
        )
