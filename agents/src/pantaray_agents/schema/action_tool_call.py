"""採用したActionのツール呼び出しを、実行attemptと独立して識別する。"""

import json
import uuid

from pydantic import BaseModel, ConfigDict, Field


class ActionToolCallOrigin(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    llm_step_id: str = Field(min_length=1)
    call_id: str = Field(min_length=1)

    def shared_turn_step_id(self) -> str:
        """The tool step id of a call that runs at once with its siblings.

        Derived from the call, so startup recovery can find what such a call
        ran (its tool request id names this step) and give back a result that
        landed before its row did.
        """

        identity = json.dumps(
            ["pantaray.action.shared-turn-step.v1", self.llm_step_id, self.call_id],
            separators=(",", ":"),
        )
        return str(uuid.uuid5(uuid.NAMESPACE_URL, identity))
