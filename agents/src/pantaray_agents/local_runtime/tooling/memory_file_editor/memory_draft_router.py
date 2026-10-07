"""One editor surface over the unified Memory agent's three memory drafts.

Fact, long-term Insight and Agent Experience are three catalog nodes, so a
unified run holds three independent drafts. The agent must not have to know
that: it sees one tree whose first path segment selects the category, and one
``draft_revision`` that covers all three. The router owns that translation and
keeps every cross-root operation out of reach.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace

from pantaray_agents.agents.memory_file_editor.tools import (
    LINK_MEMORY_TOOL_NAME,
    MOVE_MEMORY_FILE_TOOL_NAME,
    UNLINK_MEMORY_TOOL_NAME,
)
from pantaray_agents.local_runtime.memory_catalog.agent_experience_content import (
    agent_experience_evidence_text,
    experience_id_from_path,
    parse_agent_experience_markdown,
)
from pantaray_agents.local_runtime.memory_catalog.errors import (
    MemoryLinkValidationError,
    MemoryPublicationConflictError,
)
from pantaray_agents.local_runtime.memory_catalog.models import (
    MemoryDocument,
    MemorySource,
)
from pantaray_agents.local_runtime.tooling.fs_sandbox import EditablePathPolicy
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolDefinition,
    ReactToolExecutor,
    ReactToolResult,
    tool_error_response,
)

from .logical_draft_io import draft_document_content, require_editable_document_path
from .memory_domain_tools import MemoryDraftToolSession
from .tool_support import args_object, required_string

FACTS_ROOT = "facts"
INSIGHTS_ROOT = "insights"
AGENT_EXPERIENCE_ROOT = "agent_experience"
_ROUTING_ERROR_CODE = "MEMORY_DRAFT_ROUTE_INVALID"
_STALE_REVISION_ERROR_CODE = "MEMORY_DRAFT_REVISION_STALE"
# Agent Experience evidence refs are canonical host-written lines bound to the
# run's own Action set, so the agent never links or unlinks inside that root.
_REF_TOOL_NAMES = frozenset({LINK_MEMORY_TOOL_NAME, UNLINK_MEMORY_TOOL_NAME})


@dataclass(frozen=True, slots=True)
class RoutedMemoryDraft:
    """The union of every routed draft as one addressable tree."""

    documents: tuple[MemoryDocument, ...]
    draft_revision: str


@dataclass(frozen=True, slots=True)
class MemoryDraftRoute:
    source: MemorySource
    root: str
    session: MemoryDraftToolSession


@dataclass(slots=True)
class MemoryDraftRouter:
    """Delegates one editor surface to the draft that owns each path."""

    routes: tuple[MemoryDraftRoute, ...]

    def __post_init__(self) -> None:
        if not self.routes:
            raise ValueError("every memory category of this run is already published")
        roots = tuple(route.root for route in self.routes)
        if len(set(roots)) != len(roots):
            raise ValueError("memory draft roots must be unique")
        for route in self.routes:
            for pattern in route.session.editable_policy.allowed_globs:
                if not pattern.startswith(f"{route.root}/"):
                    raise ValueError(
                        "routed editable policy must stay under its own root"
                    )

    @property
    def editable_policy(self) -> EditablePathPolicy:
        return EditablePathPolicy(
            tuple(
                pattern
                for route in self.routes
                for pattern in route.session.editable_policy.allowed_globs
            )
        )

    @property
    def draft(self) -> RoutedMemoryDraft:
        documents = tuple(
            document
            for route in self.routes
            for document in route.session.draft.documents
        )
        return RoutedMemoryDraft(
            documents=documents,
            draft_revision=self._combined_revision(),
        )

    def replace_document(
        self, *, path: str, content: str, expected_text: str, expected_revision: str
    ) -> str:
        if self._combined_revision() != expected_revision:
            raise MemoryPublicationConflictError("memory tree changed before editing")
        route = self._require_route(path)
        if route.source == "agent_experience":
            _, before = draft_document_content(
                documents=route.session.draft.documents, path=path
            )
            if agent_experience_evidence_text(before) != agent_experience_evidence_text(
                content
            ):
                raise MemoryLinkValidationError(
                    "Agent Experience evidence is host-owned"
                )
            _require_valid_experience_entry(route, path=path, content=content)
        route.session.replace_document(
            path=path,
            content=content,
            expected_text=expected_text,
            expected_revision=route.session.draft.draft_revision,
        )
        return self._combined_revision()

    def append_document(
        self, *, path: str, content: str, expected_revision: str
    ) -> str:
        if self._combined_revision() != expected_revision:
            raise MemoryPublicationConflictError("memory tree changed before editing")
        route = self._require_route(path)
        if route.source == "agent_experience":
            _require_valid_experience_entry(route, path=path, content=content)
        route.session.append_document(
            path=path,
            content=content,
            expected_revision=route.session.draft.draft_revision,
        )
        return self._combined_revision()

    def definitions(self) -> tuple[ReactToolDefinition, ...]:
        template = self.routes[0].session.definitions()
        return tuple(
            replace(
                definition,
                description=self._routed_description(definition.description),
                execute=self._routed_executor(definition.name),
            )
            for definition in template
        )

    def _routed_description(self, description: str) -> str:
        scope = ", ".join(self.editable_policy.allowed_globs)
        head, _, _ = description.partition("Editable paths:")
        return f"{head.strip()} Editable paths: {scope}."

    def _routed_executor(self, tool_name: str) -> ReactToolExecutor:
        async def execute(call: ReactToolCall, step_number: int) -> ReactToolResult:
            return await self._execute_routed(
                tool_name=tool_name, call=call, step_number=step_number
            )

        return execute

    async def _execute_routed(
        self, *, tool_name: str, call: ReactToolCall, step_number: int
    ) -> ReactToolResult:
        args = args_object(call.tool_args)
        try:
            route = self._route_for_call(tool_name=tool_name, args=args)
        except ValueError as exc:
            return tool_error_response(
                tool_name=tool_name, error_code=_ROUTING_ERROR_CODE, message=str(exc)
            )
        expected = required_string(args, "expected_draft_revision")
        if expected != self._combined_revision():
            return tool_error_response(
                tool_name=tool_name,
                error_code=_STALE_REVISION_ERROR_CODE,
                message="expected_draft_revision does not match the current draft revision",
            )
        definition = _require_definition(route.session.definitions(), tool_name)
        routed_call = ReactToolCall(
            tool_name=call.tool_name,
            tool_args={
                **args,
                "expected_draft_revision": route.session.draft.draft_revision,
            },
            tool_call_envelope=call.tool_call_envelope,
        )
        result = await definition.execute(routed_call, step_number)
        return self._with_combined_revision(result)

    def _with_combined_revision(self, result: ReactToolResult) -> ReactToolResult:
        output = result.output
        if result.status != "success" or not isinstance(output, dict):
            return result
        return replace(
            result,
            output={**output, "draft_revision": self._combined_revision()},
        )

    def _route_for_call(
        self, *, tool_name: str, args: dict[str, JSONValue]
    ) -> MemoryDraftRoute:
        route = self._routed_target(tool_name=tool_name, args=args)
        if tool_name in _REF_TOOL_NAMES and route.root == AGENT_EXPERIENCE_ROOT:
            raise ValueError(
                f"{tool_name} cannot touch {AGENT_EXPERIENCE_ROOT}: "
                "its evidence refs are written by the host"
            )
        if (
            tool_name == MOVE_MEMORY_FILE_TOOL_NAME
            and route.root == AGENT_EXPERIENCE_ROOT
        ):
            # An entry's file name is its Experience ID, so a moved entry can
            # never match its own content.
            raise ValueError(
                f"{tool_name} cannot rename an Agent Experience entry: "
                "its file name is its Experience ID"
            )
        return route

    def _routed_target(
        self, *, tool_name: str, args: dict[str, JSONValue]
    ) -> MemoryDraftRoute:
        if "local_ref_id" in args:
            return self._route_for_local_ref(required_string(args, "local_ref_id"))
        if "destination_path" in args:
            route = self._require_route(required_string(args, "source_path"))
            destination = self._require_route(required_string(args, "destination_path"))
            if destination.root != route.root:
                raise ValueError(
                    f"{tool_name} cannot move a file between memory categories"
                )
            return route
        if "source_path" in args:
            return self._require_route(required_string(args, "source_path"))
        return self._require_route(required_string(args, "path"))

    def _route_for_local_ref(self, local_ref_id: str) -> MemoryDraftRoute:
        matches = tuple(
            route
            for route in self.routes
            if any(
                link.local_ref_id == local_ref_id and link.state != "removed"
                for link in route.session.draft.links
            )
        )
        if len(matches) != 1:
            raise ValueError(
                f"memory reference is absent or ambiguous: {local_ref_id}; "
                "re-read its file and remove the complete tag with apply_patch"
            )
        return matches[0]

    def _require_route(self, path: str) -> MemoryDraftRoute:
        root, _, _ = path.partition("/")
        for route in self.routes:
            if route.root == root:
                return route
        raise ValueError(f"path is outside every memory category: {path}")

    def _combined_revision(self) -> str:
        digest = hashlib.sha256()
        for route in self.routes:
            digest.update(route.root.encode("utf-8"))
            digest.update(b"\0")
            digest.update(route.session.draft.draft_revision.encode("utf-8"))
            digest.update(b"\0")
        return f"sha256:{digest.hexdigest()}"


def _require_valid_experience_entry(
    route: MemoryDraftRoute, *, path: str, content: str
) -> None:
    """Reject a malformed entry before it reaches the draft.

    Publication judges the whole tree at the end of the run, when the agent can
    no longer repair a single entry; a write-time error lets it fix the file.
    """

    canonical = require_editable_document_path(
        path=path, editable_policy=route.session.editable_policy
    )
    parse_agent_experience_markdown(
        content, expected_experience_id=experience_id_from_path(canonical)
    )


def _require_definition(
    definitions: tuple[ReactToolDefinition, ...], tool_name: str
) -> ReactToolDefinition:
    for definition in definitions:
        if definition.name == tool_name:
            return definition
    raise ValueError(f"routed memory draft tool is absent: {tool_name}")


__all__ = [
    "AGENT_EXPERIENCE_ROOT",
    "FACTS_ROOT",
    "INSIGHTS_ROOT",
    "MemoryDraftRoute",
    "MemoryDraftRouter",
    "RoutedMemoryDraft",
]
