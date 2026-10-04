"""Final ノードの実装。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pantaray_agents.agents.action_agent.runtime.state import ActionAgentState
from pantaray_agents.agents.action_agent.runtime.state.updates import (
    append_state_error,
    set_status_with_updated_at,
)
from pantaray_agents.agents.action_agent.support.severity import (
    FATAL_SEVERITIES,
)
from pantaray_agents.agents.action_agent.support.status import (
    ACTION_TERMINAL_STATUSES,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso

if TYPE_CHECKING:  # pragma: no cover
    from pantaray_agents.agents.action_agent import ActionAgent
    from pantaray_agents.agents.action_agent.runtime.graph import ActionGraphRuntime


def _has_fatal_state_error(state: ActionAgentState) -> bool:
    """state.errors に致命エラーが含まれるか判定する。"""
    raw_errors = state.get("errors")
    if not isinstance(raw_errors, list):
        return False

    for error in raw_errors:
        if not isinstance(error, dict):
            continue
        severity = str(error.get("severity", "")).strip().lower()
        if severity in FATAL_SEVERITIES:
            return True
    return False


async def finalize_step(
    agent: ActionAgent,
    state: ActionAgentState,
    runtime: ActionGraphRuntime,
) -> ActionAgentState:
    """Done ノードの処理。"""

    state["phase"] = "finalizing"

    # Cancel API の結果が DB に反映されていれば、成果物の送信や最終永続化を抑止する。
    # THINK/ACTION でのチェック後〜finalize の間に入ったキャンセルを拾うための再確認。
    canceled = await runtime.services.cancellation.check_cancellation(state)
    if canceled:
        # canceled 状態では最終出力をクライアントへ返さない（漏洩防止）。
        state["final_output"] = ""
        return state

    final_output = state.get("final_output") or ""
    status = state.get("status", "success")
    if status not in ACTION_TERMINAL_STATUSES:
        # 安全弁: fatal error が state に残っている場合、final_output があっても success に昇格させない。
        if _has_fatal_state_error(state):
            status = "error"
        else:
            status = "success" if final_output else "processing"

    # final_output もなく、status がまだ processing のまま finalize まで来た場合は、
    # 予期しない終了として error に落としつつ AgentError を記録する。
    if not final_output and status == "processing":
        # superseded（権威なし）状態では、この実行は最終出力を確定できないことがある。
        # それを「予期しない終了」として error に落とすと誤検知になるため、ここでは error 化しない。
        if state.get("run_authority") != "superseded" and not state.get("skip_persist"):
            error = runtime.services.response.build_agent_error(
                error_type="internal_error",
                error_code="ACTION_FINALIZE_UNEXPECTED_PROCESSING",
                error_message="Action finalized without final_output or terminal status.",
                error_details={
                    "reason": "finalize_step reached with status=processing and no final_output",
                    "phase": state.get("phase"),
                },
            )
            append_state_error(state, error=error)
            status = "error"

    set_status_with_updated_at(state, status=status, updated_at=now_utc_iso())
    return state


__all__ = ["finalize_step"]
