"""What every web tools route raises and returns, whichever one served the call."""

from __future__ import annotations

from typing import NotRequired, Required, TypedDict

from pantaray_agents.schema.agent.base import JSONValue
from pantaray_llm.errors import ProxyErrorCode, ProxyProvider, ProxySuggestedAction


class WebContentExecutionError(RuntimeError):
    """外部本文取得の実行失敗。"""

    def __init__(
        self,
        *,
        error_code: ProxyErrorCode,
        error_message: str,
        retryable: bool,
        suggested_action: ProxySuggestedAction | None = None,
        request_id: str | None = None,
        upstream_provider: ProxyProvider | None = None,
        upstream_request_id: str | None = None,
        profile_id: str | None = None,
        upstream_status_code: int | None = None,
        upstream_code: str | None = None,
    ) -> None:
        super().__init__(error_message)
        self.error_code = error_code
        self.error_message = error_message
        self.retryable = retryable
        # One error code has different fixes per connection kind: a lapsed
        # Pantaray session needs a new sign-in, a rejected Tavily key needs a
        # new key. Only the side that raised the failure knows which, so it
        # states the guidance here and the Action boundary forwards it. Left
        # unset, the shared contract derives guidance from the code alone.
        self.suggested_action = suggested_action
        self.request_id = request_id
        self.upstream_provider = upstream_provider
        self.upstream_request_id = upstream_request_id
        self.profile_id = profile_id
        self.upstream_status_code = upstream_status_code
        self.upstream_code = upstream_code


class WebContentInvalidResponseError(ValueError):
    """外部レスポンス shape 不正。"""


class WebToolsWrapperContext(TypedDict, total=False):
    user_id: Required[str]
    action_id: NotRequired[str]
    request_id: Required[str]


class WebToolsWrapperResponse(TypedDict, total=False):
    tool_id: Required[str]
    status: Required[str]
    request_id: Required[str]
    response_time_ms: NotRequired[int]
    result: NotRequired[dict[str, JSONValue]]
    error: NotRequired[dict[str, JSONValue]]


__all__ = [
    "WebContentExecutionError",
    "WebContentInvalidResponseError",
    "WebToolsWrapperContext",
    "WebToolsWrapperResponse",
]
