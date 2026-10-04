from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import TypeAdapter, ValidationError

from pantaray_agents.repositories import action_runtime_resume_contract as resume
from pantaray_agents.repositories.action_support.initial_memory_context_contract import (
    InitialFactsBrief,
    InitialInsightBrief,
    InitialMemoryArtifact,
    InitialMemoryArtifactFile,
    InitialMemoryContext,
    InitialMemorySourceType,
)
from pantaray_agents.schema.repositories.repository import (
    DBRow,
    DBRows,
    RepositoryResult,
)
from pantaray_llm.contracts.conversation import LlmProviderTurn

if TYPE_CHECKING:  # pragma: no cover
    from ..mock_action_agent_repository import MockActionAgentRepository

from ..mock_action_agent_repository_types import (
    validate_mock_action_row,
    validate_mock_action_step_row,
)

_PROVIDER_TURN_ADAPTER: TypeAdapter[LlmProviderTurn] = TypeAdapter(LlmProviderTurn)
APPROVAL_SESSION_ID_KEY, TOOL_REQUEST_ID_KEY = "approval_session_id", "tool_request_id"
_MEMORY_CATEGORIES: tuple[InitialMemorySourceType, ...] = (
    "long_term_insight",
    "facts",
    "agent_experience",
)


def _memory_category(value: object) -> InitialMemorySourceType | None:
    return next((item for item in _MEMORY_CATEGORIES if item == value), None)


def _to_dt(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return datetime.min.replace(tzinfo=UTC)
    return datetime.min.replace(tzinfo=UTC)


def _approval_ref_matches(
    value: object,
    *,
    approval_session_id: str,
    tool_request_id: str,
) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get(APPROVAL_SESSION_ID_KEY) == approval_session_id
        and value.get(TOOL_REQUEST_ID_KEY) == tool_request_id
    )


def _checkpoint_matches_approval_resume(
    checkpoint: object,
    *,
    approval_session_id: str,
    tool_request_id: str,
) -> bool:
    if not isinstance(checkpoint, Mapping):
        return False

    pending_approval = checkpoint.get("pending_approval_request")
    if _approval_ref_matches(
        pending_approval,
        approval_session_id=approval_session_id,
        tool_request_id=tool_request_id,
    ):
        return True

    blockers = checkpoint.get("current_approval_blockers")
    if not isinstance(blockers, list):
        return False
    return any(
        _approval_ref_matches(
            blocker,
            approval_session_id=approval_session_id,
            tool_request_id=tool_request_id,
        )
        for blocker in blockers
    )


def _checkpoint_resume_order_key(
    row: DBRow,
) -> tuple[int, bool, datetime, datetime, str]:
    step_number = row.get("step_number")
    return (
        step_number if isinstance(step_number, int) else 0,
        row.get("completed_at") is not None,
        _to_dt(row.get("completed_at")),
        _to_dt(row.get("created_at")),
        str(row.get("step_id") or ""),
    )


class MockActionAgentQueryMixin:
    async def get_runtime_resume_context_for_user_step(
        self: MockActionAgentRepository,
        *,
        user_id: str,
        action_id: str,
        current_user_step_number: int,
    ) -> RepositoryResult[resume.ActionRuntimeResumeContext]:
        action_result = await self.get_action(user_id=user_id, action_id=action_id)
        if not action_result.data:
            return RepositoryResult(error=action_result.error or "Action not found")
        context = resume.select_action_runtime_resume_context(
            self.data.get("action_steps", []),
            expected_user_id=user_id,
            expected_action_id=action_id,
            current_user_step_number=current_user_step_number,
        )
        return RepositoryResult(data=context)

    async def get_runtime_checkpoint_for_approval_resume(
        self: MockActionAgentRepository,
        *,
        user_id: str,
        action_id: str,
        approval_session_id: str,
        tool_request_id: str,
    ) -> RepositoryResult[DBRow]:
        action_result = await self.get_action(user_id=user_id, action_id=action_id)
        if not action_result.data:
            return RepositoryResult(error=action_result.error or "Action not found")

        approval_anchors = [
            step
            for step in self.data.get("action_steps", [])
            if step.get("action_id") == action_id
            and _checkpoint_matches_approval_resume(
                step.get("runtime_state_checkpoint"),
                approval_session_id=approval_session_id,
                tool_request_id=tool_request_id,
            )
        ]
        if not approval_anchors:
            return RepositoryResult(data=None)
        anchor = max(approval_anchors, key=_checkpoint_resume_order_key)
        anchor_step_number = _checkpoint_resume_order_key(anchor)[0]
        checkpoints = [
            step
            for step in self.data.get("action_steps", [])
            if step.get("action_id") == action_id
            and step.get("runtime_state_checkpoint") is not None
            and _checkpoint_resume_order_key(step)[0] >= anchor_step_number
        ]
        selected = max(checkpoints, key=_checkpoint_resume_order_key)
        metadata = {"approval_anchor_step_id": str(anchor.get("step_id") or "")}
        return RepositoryResult(data=selected, metadata=metadata)

    async def get_action(
        self: MockActionAgentRepository,
        *,
        user_id: str,
        action_id: str,
    ) -> RepositoryResult[DBRow]:
        res = await self.get_data("actions", "action_id", action_id)
        row = res.data if isinstance(res.data, dict) else None
        if row is None:
            return res
        if str(row.get("user_id") or "") != str(user_id or ""):
            return RepositoryResult(data=None)
        try:
            return RepositoryResult(data=validate_mock_action_row(row))
        except ValueError as exc:
            return RepositoryResult(error=str(exc))

    async def get_suggestion(
        self: MockActionAgentRepository,
        *,
        user_id: str,
        suggestion_id: str | None,
    ) -> RepositoryResult[DBRow]:
        res = await self.get_data("suggestions", "suggestion_id", suggestion_id)
        row = res.data if isinstance(res.data, dict) else None
        if row is None:
            return res
        if str(row.get("user_id") or "") != str(user_id or ""):
            return RepositoryResult(data=None)
        return res

    async def fetch_activity_description_rows(
        self: MockActionAgentRepository,
        *,
        user_id: str,
        period_from: str,
        period_to: str,
        keywords: list[str] | None,
        limit: int,
    ) -> RepositoryResult[DBRows]:
        if not user_id or not period_from or not period_to:
            return RepositoryResult(data=[])

        safe_limit = min(max(int(limit or 0), 1), 200)
        normalized_keywords = [
            str(keyword).strip().lower()
            for keyword in (keywords or [])
            if str(keyword).strip()
        ]
        rows = [
            row
            for row in (self.data.get("activity_descriptions", []) or [])
            if row.get("user_id") == user_id
            and (row.get("status") in (None, "success"))
        ]

        def _period_end(row: DBRow) -> str:
            return str(
                row.get("period_end")
                or row.get("updated_at")
                or row.get("created_at")
                or ""
            )

        rows.sort(key=_period_end, reverse=True)
        out: DBRows = []
        for row in rows:
            period_end = _period_end(row)
            if period_end < period_from or period_end > period_to:
                continue
            content = str(row.get("description") or row.get("content") or "")
            if normalized_keywords and not any(
                keyword in content.lower() for keyword in normalized_keywords
            ):
                continue
            out.append(
                {
                    "source": "activity_description",
                    "record_id": str(row.get("log_id") or row.get("record_id") or ""),
                    "created_at": row.get("created_at"),
                    "updated_at": row.get("period_end") or row.get("updated_at"),
                    "content": content,
                    "period_start": row.get("period_start"),
                    "period_end": row.get("period_end"),
                }
            )
            if len(out) >= safe_limit:
                break
        return RepositoryResult(data=out)

    async def get_latest_insights(
        self: MockActionAgentRepository,
        user_id: str,
        limit: int = 5,
    ) -> RepositoryResult[DBRows]:
        return await self.get_latest_data("insights", "user_id", user_id, limit)

    async def get_long_term_insight(
        self: MockActionAgentRepository,
        user_id: str,
    ) -> RepositoryResult[DBRow]:
        items = [
            row
            for row in self.data.get("insights", [])
            if row.get("user_id") == user_id and row.get("long_term_insight_data")
        ]
        if not items:
            return RepositoryResult(error="No long term insight")
        items.sort(key=lambda x: x.get("created_at"), reverse=True)
        return RepositoryResult(data=items[0])

    async def get_initial_memory_context(
        self: MockActionAgentRepository,
        user_id: str,
        *,
        action_id: str,
        suggestion_id: str | None,
    ) -> RepositoryResult[InitialMemoryContext]:
        _ = (action_id, suggestion_id)
        insights = [
            row
            for row in self.data.get("insights", [])
            if row.get("user_id") == user_id
        ]
        insight_rows = [row for row in insights if row.get("insight_profile_brief")]
        insight_rows.sort(
            key=lambda row: row.get("updated_at") or row.get("created_at"),
            reverse=True,
        )
        insight_row = insight_rows[0] if insight_rows else None
        fact_rows = [
            row
            for row in self.data.get("facts", [])
            if row.get("user_id") == user_id and row.get("facts_profile_brief")
        ]
        fact_rows.sort(
            key=lambda row: row.get("updated_at") or row.get("created_at"),
            reverse=True,
        )
        fact_row = fact_rows[0] if fact_rows else None
        artifacts: list[InitialMemoryArtifact] = []
        for artifact_row in self.data.get("memory_artifacts", []):
            if artifact_row.get("user_id") != user_id:
                continue
            source_type = _memory_category(artifact_row.get("source_type"))
            if source_type is None:
                continue
            artifact_id = str(artifact_row.get("artifact_id") or "")
            file_rows = [
                row
                for row in self.data.get("memory_artifact_files", [])
                if row.get("artifact_id") == artifact_id
            ]
            if not file_rows:
                raise RuntimeError(
                    f"Memory projection has no files for artifact {artifact_id}."
                )
            root_path = str(artifact_row.get("root_path") or "").strip("/")
            artifacts.append(
                InitialMemoryArtifact(
                    source_type=source_type,
                    source_record_id=str(artifact_row.get("source_record_id") or ""),
                    artifact_id=artifact_id,
                    logical_updated_at=str(
                        artifact_row.get("logical_updated_at") or ""
                    ),
                    files=tuple(
                        InitialMemoryArtifactFile(
                            storage_path="/".join(
                                (root_path, str(row.get("relative_path") or ""))
                            ),
                            sha256=str(row.get("sha256") or ""),
                            byte_size=int(row.get("byte_size") or 0),
                            mime_type=str(row.get("mime_type") or ""),
                        )
                        for row in file_rows
                    ),
                )
            )
        return RepositoryResult(
            data=InitialMemoryContext(
                insight=(
                    InitialInsightBrief(
                        insight_id=str(insight_row.get("insight_id") or ""),
                        insight_profile_brief=str(
                            insight_row.get("insight_profile_brief") or ""
                        ),
                        created_at=str(insight_row.get("created_at") or ""),
                        updated_at=str(insight_row.get("updated_at") or ""),
                    )
                    if insight_row is not None
                    else None
                ),
                facts=(
                    InitialFactsBrief(
                        fact_id=str(fact_row.get("fact_id") or ""),
                        facts_profile_brief=str(
                            fact_row.get("facts_profile_brief") or ""
                        ),
                        created_at=str(fact_row.get("created_at") or ""),
                        updated_at=str(fact_row.get("updated_at") or ""),
                    )
                    if fact_row is not None
                    else None
                ),
                artifacts=tuple(artifacts),
                context_epoch=None,
            )
        )

    async def get_recent_short_term_insights(
        self: MockActionAgentRepository,
        user_id: str,
        *,
        since_iso: str | None = None,
        limit: int = 5,
    ) -> RepositoryResult[DBRows]:
        items = [
            row
            for row in self.data.get("insights", [])
            if row.get("user_id") == user_id and row.get("short_term_insight_data")
        ]
        if since_iso:
            items = [
                row for row in items if str(row.get("created_at", "")) >= since_iso
            ]
        items.sort(key=lambda x: x.get("created_at"), reverse=True)
        return RepositoryResult(data=items[:limit])

    async def get_action_step(
        self: MockActionAgentRepository,
        *,
        user_id: str,
        step_id: str,
    ) -> RepositoryResult[DBRow]:
        for item in self.data.get("action_steps", []):
            if item.get("step_id") != step_id:
                continue
            action_id = str(item.get("action_id") or "")
            if not action_id:
                return RepositoryResult(error="step not found")
            for action_row in self.data.get("actions", []):
                if str(action_row.get("action_id") or "") != action_id:
                    continue
                if str(action_row.get("user_id") or "") != str(user_id or ""):
                    return RepositoryResult(error="step not found")
                try:
                    return RepositoryResult(data=validate_mock_action_step_row(item))
                except (ValidationError, ValueError) as exc:
                    return RepositoryResult(error=str(exc))
            return RepositoryResult(error="step not found")
        return RepositoryResult(error="step not found")

    async def get_action_steps_by_short_step_ids(
        self: MockActionAgentRepository,
        *,
        user_id: str,
        action_id: str,
        short_step_ids: tuple[str, ...],
    ) -> RepositoryResult[DBRows]:
        action_result = await self.get_action(user_id=user_id, action_id=action_id)
        if action_result.error:
            return RepositoryResult(error=action_result.error)
        if not action_result.data:
            return RepositoryResult(error="Action not found")
        latest_by_ref: dict[str, DBRow] = {}
        for ref in short_step_ids:
            matches = [
                row
                for row in self.data.get("action_steps", [])
                if row.get("action_id") == action_id and row.get("short_step_id") == ref
            ]
            if matches:
                latest_by_ref[ref] = max(
                    matches,
                    key=lambda row: (
                        _to_dt(row.get("completed_at")),
                        _to_dt(row.get("created_at")),
                        str(row.get("step_id") or ""),
                    ),
                )
        try:
            return RepositoryResult(
                data=[
                    validate_mock_action_step_row(latest_by_ref[ref])
                    for ref in short_step_ids
                    if ref in latest_by_ref
                ]
            )
        except (ValidationError, ValueError) as exc:
            return RepositoryResult(error=str(exc))

    async def get_action_provider_turns(
        self: MockActionAgentRepository,
        *,
        user_id: str,
        action_id: str,
        identity: str,
    ) -> RepositoryResult[dict[str, LlmProviderTurn]]:
        action_result = await self.get_action(user_id=user_id, action_id=action_id)
        if action_result.error:
            return RepositoryResult(error=action_result.error)
        if not action_result.data:
            return RepositoryResult(error="Action not found")
        return RepositoryResult(
            data={
                str(row["step_id"]): _PROVIDER_TURN_ADAPTER.validate_python(
                    row["provider_turn"]
                )
                for row in self.data.get("action_steps", [])
                if row.get("action_id") == action_id
                and row.get("provider_turn") is not None
                and row.get("provider_turn_identity") == identity
            }
        )

    async def get_actions_by_user(
        self: MockActionAgentRepository,
        user_id: str,
        limit: int = 10,
    ) -> RepositoryResult[DBRows]:
        result = await self.get_latest_data("actions", "user_id", user_id, limit)
        if not result.data:
            return RepositoryResult(
                data=[] if result.error is None else None, error=result.error
            )
        try:
            return RepositoryResult(
                data=[validate_mock_action_row(row) for row in result.data],
                error=result.error,
            )
        except (ValidationError, ValueError) as exc:
            return RepositoryResult(error=str(exc))

    async def get_actions_by_suggestion(
        self: MockActionAgentRepository,
        *,
        user_id: str,
        suggestion_id: str,
    ) -> RepositoryResult[DBRows]:
        if "actions" not in self.data:
            return RepositoryResult(error="actions not found")
        filtered = [
            row
            for row in self.data["actions"]
            if row.get("suggestion_id") == suggestion_id
            and str(row.get("user_id") or "") == str(user_id or "")
        ]
        if not filtered:
            return RepositoryResult(error="No actions found")
        filtered.sort(key=lambda x: x.get("created_at", datetime.min), reverse=True)
        try:
            return RepositoryResult(
                data=[validate_mock_action_row(row) for row in filtered]
            )
        except (ValidationError, ValueError) as exc:
            return RepositoryResult(error=str(exc))

    async def get_latest_structured_fact(
        self: MockActionAgentRepository,
    ) -> RepositoryResult[DBRow]:
        facts = self.data.get("facts", [])
        if not facts:
            return RepositoryResult(error="No structured facts found")
        latest = max(
            facts,
            key=lambda x: x.get("updated_at", x.get("created_at", datetime.min)),
        )
        if not latest:
            return RepositoryResult(error="No structured facts found")
        return RepositoryResult(data=latest)
