from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pantaray_agents.agents.memory_file_editor.tools import (
    DELETE_MEMORY_FILE_TOOL_NAME,
    LINK_MEMORY_TOOL_NAME,
    MOVE_MEMORY_FILE_TOOL_NAME,
    UNLINK_MEMORY_TOOL_NAME,
    delete_memory_file_request_schema,
    link_memory_request_schema,
    move_memory_file_request_schema,
    unlink_memory_request_schema,
)
from pantaray_agents.local_runtime.memory_catalog.draft import (
    delete_memory_document,
    link_memory,
    move_memory_document,
    replace_draft_documents,
    seal_link_command,
    unlink_memory,
)
from pantaray_agents.local_runtime.memory_catalog.errors import (
    MemoryContextExpiredError,
    MemoryLinkValidationError,
    MemoryPublicationConflictError,
)
from pantaray_agents.local_runtime.memory_catalog.models import (
    MemoryDocument,
    MemoryDraftCheckpoint,
)
from pantaray_agents.local_runtime.tooling.fs_sandbox import (
    EditablePathPolicy,
    SandboxPathError,
    TextFileError,
)
from pantaray_agents.local_runtime.tooling.memory_retrieval import (
    MemoryContextSession,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolDefinition,
    ReactToolExecutor,
    ReactToolResult,
    tool_error_response,
)

from .logical_draft_io import (
    require_editable_document_path,
    require_new_document_path,
)
from .tool_support import (
    args_object,
    error_code,
    required_string,
    response_schema,
    success,
    tool,
)


class MemoryDraftView(Protocol):
    """The draft attributes that the memory file-editor tools read."""

    @property
    def documents(self) -> tuple[MemoryDocument, ...]: ...

    @property
    def draft_revision(self) -> str: ...


class MemoryDraftSession(Protocol):
    """Compare the captured tree and compute the saved revision under write authority."""

    @property
    def editable_policy(self) -> EditablePathPolicy: ...

    @property
    def draft(self) -> MemoryDraftView: ...

    def replace_document(
        self, *, path: str, content: str, expected_text: str, expected_revision: str
    ) -> str: ...

    def append_document(
        self, *, path: str, content: str, expected_revision: str
    ) -> str: ...

    def definitions(self) -> tuple[ReactToolDefinition, ...]: ...


@dataclass(slots=True)
class MemoryDraftToolSession:
    editable_policy: EditablePathPolicy
    draft: MemoryDraftCheckpoint
    memory_context: MemoryContextSession

    def __post_init__(self) -> None:
        if self.draft.user_id != self.memory_context.user_id:
            raise ValueError("memory draft and context belong to different users")

    def definitions(self) -> tuple[ReactToolDefinition, ...]:
        return memory_file_domain_definitions(
            editable_policy=self.editable_policy,
            link=self.link,
            unlink=self.unlink,
            move_file=self.move_file,
            delete_file=self.delete_file,
        )

    def replace_document(
        self, *, path: str, content: str, expected_text: str, expected_revision: str
    ) -> str:
        if self.draft.draft_revision != expected_revision:
            raise MemoryPublicationConflictError("memory tree changed before editing")
        canonical = require_editable_document_path(
            path=path,
            editable_policy=self.editable_policy,
        )
        current = next(
            (item for item in self.draft.documents if item.source_path == canonical),
            None,
        )
        if current is None:
            raise MemoryLinkValidationError("edited file is absent from memory draft")
        if current.content != expected_text:
            raise MemoryPublicationConflictError(
                "memory file changed before apply_patch"
            )
        documents = tuple(
            MemoryDocument(item.source_path, content)
            if item.source_path == canonical
            else item
            for item in self.draft.documents
        )
        self.draft = replace_draft_documents(
            draft=self.draft,
            documents=documents,
        )
        return self.draft.draft_revision

    def append_document(
        self, *, path: str, content: str, expected_revision: str
    ) -> str:
        if self.draft.draft_revision != expected_revision:
            raise MemoryPublicationConflictError("memory tree changed before editing")
        canonical = require_editable_document_path(
            path=path,
            editable_policy=self.editable_policy,
        )
        require_new_document_path(documents=self.draft.documents, path=canonical)
        self.draft = replace_draft_documents(
            draft=self.draft,
            documents=(*self.draft.documents, MemoryDocument(canonical, content)),
        )
        return self.draft.draft_revision

    async def link(self, call: ReactToolCall, step_number: int) -> ReactToolResult:
        args = args_object(call.tool_args)
        tool_invocation_id = self._invocation_id(step_number)
        replay = next(
            (
                command
                for command in self.draft.applied_commands
                if command.tool_invocation_id == tool_invocation_id
            ),
            None,
        )
        if replay is not None:
            if replay.local_ref_id is None:
                return tool_error_response(
                    tool_name=call.tool_name,
                    error_code="MEMORY_LINK_INVALID",
                    message="tool invocation was already used by another memory operation",
                )
            return success(
                call.tool_name,
                {
                    "status": "success",
                    "local_ref_id": replay.local_ref_id,
                    "draft_revision": self.draft.draft_revision,
                },
            )
        try:
            source_path = require_editable_document_path(
                path=required_string(args, "source_path"),
                editable_policy=self.editable_policy,
            )
            command = seal_link_command(
                epoch=self.memory_context.require_epoch(),
                draft=self.draft,
                tool_invocation_id=tool_invocation_id,
                target_handle=required_string(args, "target_handle"),
                source_path=source_path,
                exact_text=required_string(args, "exact_text"),
                occurrence=_required_int(args, "occurrence"),
                note=required_string(args, "note"),
                expected_draft_revision=required_string(
                    args, "expected_draft_revision"
                ),
            )
            updated = link_memory(draft=self.draft, command=command)
            self.draft = updated
        except (
            MemoryContextExpiredError,
            MemoryLinkValidationError,
            MemoryPublicationConflictError,
            SandboxPathError,
        ) as exc:
            return tool_error_response(
                tool_name=call.tool_name,
                error_code="MEMORY_LINK_INVALID",
                message=str(exc),
            )
        return success(
            call.tool_name,
            {
                "status": "success",
                "local_ref_id": command.local_ref_id,
                "draft_revision": updated.draft_revision,
            },
        )

    async def unlink(self, call: ReactToolCall, step_number: int) -> ReactToolResult:
        args = args_object(call.tool_args)
        local_ref_id = required_string(args, "local_ref_id")
        try:
            link = next(
                (
                    item
                    for item in self.draft.links
                    if item.local_ref_id == local_ref_id and item.state != "removed"
                ),
                None,
            )
            if link is not None:
                require_editable_document_path(
                    path=link.source_path,
                    editable_policy=self.editable_policy,
                )
            updated = unlink_memory(
                draft=self.draft,
                tool_invocation_id=self._invocation_id(step_number),
                local_ref_id=local_ref_id,
                expected_draft_revision=required_string(
                    args, "expected_draft_revision"
                ),
            )
            self.draft = updated
        except (
            MemoryLinkValidationError,
            MemoryPublicationConflictError,
            SandboxPathError,
        ) as exc:
            return tool_error_response(
                tool_name=call.tool_name,
                error_code="MEMORY_LINK_INVALID",
                message=str(exc),
            )
        return success(
            call.tool_name,
            {
                "status": "success",
                "local_ref_id": local_ref_id,
                "draft_revision": updated.draft_revision,
            },
        )

    async def move_file(self, call: ReactToolCall, step_number: int) -> ReactToolResult:
        args = args_object(call.tool_args)
        try:
            tool_invocation_id = self._invocation_id(step_number)
            source_path = require_editable_document_path(
                path=required_string(args, "source_path"),
                editable_policy=self.editable_policy,
            )
            destination_path = require_editable_document_path(
                path=required_string(args, "destination_path"),
                editable_policy=self.editable_policy,
            )
            if not any(
                command.tool_invocation_id == tool_invocation_id
                for command in self.draft.applied_commands
            ):
                require_new_document_path(
                    documents=tuple(
                        document
                        for document in self.draft.documents
                        if document.source_path != source_path
                    ),
                    path=destination_path,
                )
            updated = move_memory_document(
                draft=self.draft,
                tool_invocation_id=tool_invocation_id,
                source_path=source_path,
                destination_path=destination_path,
                expected_draft_revision=required_string(
                    args, "expected_draft_revision"
                ),
            )
            self.draft = updated
        except (
            SandboxPathError,
            TextFileError,
            MemoryLinkValidationError,
            MemoryPublicationConflictError,
        ) as exc:
            return tool_error_response(
                tool_name=call.tool_name, error_code=error_code(exc), message=str(exc)
            )
        return success(
            call.tool_name,
            {
                "status": "success",
                "path": destination_path,
                "draft_revision": updated.draft_revision,
            },
        )

    async def delete_file(
        self, call: ReactToolCall, step_number: int
    ) -> ReactToolResult:
        args = args_object(call.tool_args)
        try:
            source_path = require_editable_document_path(
                path=required_string(args, "path"),
                editable_policy=self.editable_policy,
            )
            updated = delete_memory_document(
                draft=self.draft,
                tool_invocation_id=self._invocation_id(step_number),
                source_path=source_path,
                expected_draft_revision=required_string(
                    args, "expected_draft_revision"
                ),
            )
            self.draft = updated
        except (
            SandboxPathError,
            MemoryLinkValidationError,
            MemoryPublicationConflictError,
        ) as exc:
            return tool_error_response(
                tool_name=call.tool_name, error_code=error_code(exc), message=str(exc)
            )
        return success(
            call.tool_name,
            {
                "status": "success",
                "path": source_path,
                "draft_revision": updated.draft_revision,
            },
        )

    def _invocation_id(self, step_number: int) -> str:
        return f"{self.memory_context.run_id}:{step_number}"


def _required_int(args: dict[str, JSONValue], key: str) -> int:
    value = args.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    return value


def _draft_response_schema(identifier: str) -> dict[str, JSONValue]:
    return dict(
        response_schema(
            required=("status", identifier, "draft_revision"),
            properties={
                identifier: {"type": "string"},
                "draft_revision": {"type": "string", "pattern": "^sha256:"},
            },
        )
    )


def memory_file_domain_definitions(
    *,
    editable_policy: EditablePathPolicy,
    link: ReactToolExecutor,
    unlink: ReactToolExecutor,
    move_file: ReactToolExecutor,
    delete_file: ReactToolExecutor,
) -> tuple[ReactToolDefinition, ...]:
    editable_scope = ", ".join(editable_policy.allowed_globs)
    return (
        tool(
            name=LINK_MEMORY_TOOL_NAME,
            description=(
                "Attach a note-bearing semantic ref to an exact memory block. "
                "The target_handle must be visible in this request. "
                f"Editable paths: {editable_scope}."
            ),
            request_schema=link_memory_request_schema(),
            response_schema=_draft_response_schema("local_ref_id"),
            execute=link,
        ),
        tool(
            name=UNLINK_MEMORY_TOOL_NAME,
            description=(
                "Remove one complete semantic ref from the memory file. "
                f"Editable paths: {editable_scope}."
            ),
            request_schema=unlink_memory_request_schema(),
            response_schema=_draft_response_schema("local_ref_id"),
            execute=unlink,
        ),
        tool(
            name=MOVE_MEMORY_FILE_TOOL_NAME,
            description=(
                "Move one memory file and its ref locators atomically. "
                f"Editable paths: {editable_scope}."
            ),
            request_schema=move_memory_file_request_schema(),
            response_schema=_draft_response_schema("path"),
            execute=move_file,
        ),
        tool(
            name=DELETE_MEMORY_FILE_TOOL_NAME,
            description=(
                "Delete one memory file and mark every ref in it as removed. "
                f"Editable paths: {editable_scope}."
            ),
            request_schema=delete_memory_file_request_schema(),
            response_schema=_draft_response_schema("path"),
            execute=delete_file,
        ),
    )
