from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class MemoryUpdateContext(BaseModel):
    """Seed context for one unified Memory run.

    Only derived evidence reaches the prompt. Raw activity capture stays out of
    the run entirely, and Action history stays behind the retrieval tools.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    user_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    short_term_insights: str
    activity_summaries: str
    action_turns: str
    memory_requests: str
    memory_request_ids: tuple[str, ...]
    local_time_note: str = Field(min_length=1)
    memory_file_manifest: str = Field(min_length=1)
    workspace_context_prompt: str
    new_experience_ids: tuple[str, ...]
    draft_revision: str = Field(pattern="^sha256:")


__all__ = ["MemoryUpdateContext"]
