"""Action Agent ノードで共有するユーティリティ。"""

from __future__ import annotations

import bisect
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Never

from pantaray_agents.agents.action_agent.runtime.log_safety import (
    exception_type_name,
    safe_value_shape,
)
from pantaray_agents.agents.action_agent.runtime.state import (
    ActionAgentState,
    ActionPhase,
    HistoryEntry,
)
from pantaray_agents.agents.action_agent.runtime.state.context import (
    get_context_view as _context_view,
)
from pantaray_agents.agents.action_agent.runtime.steps.counters import (
    increment_local_step_counter,
)
from pantaray_agents.local_runtime.tooling.tool_result_finalization import (
    FinalizedToolOutput,
)
from pantaray_agents.local_runtime.tooling.tool_result_validation import (
    ToolOutputValidationError,
)
from pantaray_agents.schema.agent.action import (
    SHORT_STEP_SUFFIX_PATTERN,
    ShortStepSuffix,
    StepType,
)
from pantaray_agents.schema.agent.action_history import (
    GOAL_SCOPE_RE,
    SUPERVISOR_SCOPE_HANDLE,
    build_short_step_id_regex,
)
from pantaray_agents.schema.agent.base import JSONValue

from ..tool_runtime.shared import (
    FinalizedToolExecutionError,
    ToolValidationError,
)

logger = logging.getLogger(__name__)

# short_step_id の正規表現パターン（SSOT: DBガードと一致）
# 形式: ^{scope}-{local_step_number}-{suffix}$
# - scope: S または G[1-9][0-9]*
# - local_step_number: [1-9][0-9]* (1以上の整数)
# - suffix: USER、THINK、TOOL
SHORT_STEP_ID_RE = build_short_step_id_regex(suffix_pattern=SHORT_STEP_SUFFIX_PATTERN)

_KEY_COMPLETED_AT = "completed_at"
_KEY_CONTEXT = "context"
_KEY_GOAL_ID = "goal_id"
_KEY_HISTORY_BY_SCOPE = "history_by_scope"
_KEY_ID = "id"
_KEY_PHASE = "phase"
_KEY_SHORT_STEP_ID = "short_step_id"
_KEY_STEP_ID = "step_id"
_KEY_STEP_TYPE = "step_type"

type ToolErrorPayload = dict[str, JSONValue]


def _unreachable_step_type(value: Never) -> Never:
    """到達不能な分岐。StepType に新メンバーが追加された場合、型チェッカーがエラーを出す。"""
    raise ValueError(
        f"Invalid step_type for short_step_id: {value!r}. "
        "Expected 'user_request', 'llm_output', or 'tool_execution'."
    )


@dataclass(frozen=True, slots=True)
class ShortStepIdParts:
    """short_step_id の分解結果。"""

    scope_handle: str
    local_step_number: int
    suffix: ShortStepSuffix


@dataclass(frozen=True, slots=True)
class ToolShortStepIdInference:
    """TOOL 用 short_step_id の推定結果。"""

    scope_handle: str
    local_step_number: int
    short_step_id: str


def _build_tool_error_payload(
    exc: Exception,
    *,
    attempt: int,
    max_attempts: int,
    traceback_text: str,
) -> ToolErrorPayload:
    """ツール実行失敗時のエラー情報を構造化する。"""

    cause = exc.cause if isinstance(exc, FinalizedToolExecutionError) else exc

    payload: ToolErrorPayload = {
        "error_type": cause.__class__.__name__,
        "message": str(cause),
        "attempt": attempt,
        "max_attempts": max_attempts,
        "traceback": traceback_text,
    }
    # ToolValidationError など、例外側が追加情報（details）を持つ場合は LLM で自己修復できるように含める。
    details = cause.details if isinstance(cause, ToolValidationError) else None
    if details is not None and isinstance(details, dict):
        payload["details"] = details
    return payload


def finalized_tool_error(exc: BaseException) -> FinalizedToolOutput | None:
    """Return explicitly finalized error data without inspecting arbitrary errors."""

    if isinstance(exc, FinalizedToolExecutionError):
        return exc.finalized_output
    if isinstance(exc, (ToolValidationError, ToolOutputValidationError)):
        return exc.finalized_output
    return None


def error_output(
    finalized: JSONValue | None,
    error_payload: dict[str, JSONValue],
) -> JSONValue:
    return finalized if finalized is not None else {"error": error_payload}


def get_or_increment_local_step_number(
    state: ActionAgentState, scope_handle: str
) -> int:
    """scope_handle ごとの local_step_number をインクリメントして返す。

    内部カウンタを +1 した値を返す（初回呼び出しで 1）。
    返り値はそのままこのステップの local_step_number として使用する。

    Args:
        state: ActionAgentState
        scope_handle: スコープ識別子（`S` または `G{n}`）

    Returns:
        インクリメント後の local_step_number（1 以上）
    """
    return increment_local_step_counter(state, scope_handle=scope_handle)


def get_history_for_scope(
    state: ActionAgentState, scope_handle: str
) -> list[HistoryEntry]:
    """履歴をスコープ単位で取得する（Supervisor/Goal Worker 独立）。

    Args:
        state: ActionAgentState
        scope_handle: スコープ識別子（"S" または "G{n}"）

    Returns:
        指定スコープの履歴リスト（無ければ空配列）。

    Note:
        返り値は state 内部のリストへの **直接参照** である。
        呼び出し側で要素を変更・追加すると state が直接書き換わるため、
        履歴の追加は必ず ``append_history_entry`` を経由すること。
        読み取り専用で利用する場合はコピー不要。
    """
    # 型定義上は dict[str, list[HistoryEntry]] だが、DB 復元や
    # LangGraph の state チャネル経由で型が揺れる可能性があるため、
    # isinstance で防御的に検証する（fail-safe）。
    history_by_scope = state.get(_KEY_HISTORY_BY_SCOPE)
    if isinstance(history_by_scope, dict):
        history = history_by_scope.get(scope_handle)
        if isinstance(history, list):
            return history
    return []


def append_history_entry(
    state: ActionAgentState,
    *,
    scope_handle: str,
    entry: HistoryEntry,
) -> None:
    """指定スコープへ履歴エントリを追加する。

    エントリには ``step_id`` と ``completed_at`` が必須。
    いずれかが空文字または未設定の場合は ValueError を送出する。
    不完全な履歴が state に混入すると、ソートやマージで不整合を招くため
    入口で弾く（fail-closed）。
    """
    step_id = str(entry.get(_KEY_STEP_ID) or "").strip()
    if not step_id:
        raise ValueError(
            f"HistoryEntry must have a non-empty step_id "
            f"(scope_handle={scope_handle!r}, entry keys={list(entry.keys())})"
        )
    completed_at = str(entry.get(_KEY_COMPLETED_AT) or "").strip()
    if not completed_at:
        raise ValueError(
            f"HistoryEntry must have a non-empty completed_at "
            f"(scope_handle={scope_handle!r}, step_id={step_id!r})"
        )
    phase = entry.get(_KEY_PHASE)
    if phase not in {"init", "planning", "executing", "finalizing"}:
        raise ValueError(
            "HistoryEntry must have a lifecycle phase "
            f"(scope_handle={scope_handle!r}, step_id={step_id!r}, phase={phase!r})"
        )
    step_type = entry.get(_KEY_STEP_TYPE)
    if step_type not in {
        StepType.USER_REQUEST,
        StepType.ASSISTANT_MESSAGE,
        StepType.LLM_OUTPUT,
        StepType.TOOL_EXECUTION,
    }:
        raise ValueError(
            "HistoryEntry must have a supported step_type "
            f"(scope_handle={scope_handle!r}, step_id={step_id!r}, "
            f"step_type={step_type!r})"
        )

    history_by_scope = state.get(_KEY_HISTORY_BY_SCOPE)
    if not isinstance(history_by_scope, dict):
        history_by_scope = {}
        state["history_by_scope"] = history_by_scope
    scope_history = history_by_scope.get(scope_handle)
    if not isinstance(scope_history, list):
        scope_history = []
        history_by_scope[scope_handle] = scope_history
    upsert_history_entry(scope_history, entry)


def upsert_history_entry(
    history: list[HistoryEntry],
    entry: HistoryEntry,
) -> None:
    """Store one logical History Ref, replacing an earlier projection if present.

    History stays in ``step_number`` order. Every path but a parallel batch
    writes in that order already; a parallel batch's siblings finish in any
    order, and their rows still land in the order the model declared them.
    """
    short_step_id = entry.get("short_step_id")
    if isinstance(short_step_id, str) and short_step_id:
        history[:] = [
            current
            for current in history
            if current.get("short_step_id") != short_step_id
        ]
    bisect.insort(history, entry, key=lambda current: current["step_number"])


def project_latest_history_entries(
    history: Sequence[HistoryEntry],
) -> list[HistoryEntry]:
    """Return the latest entry for each History Ref in chronological order."""
    latest_reversed: list[HistoryEntry] = []
    seen_refs: set[str] = set()
    for entry in reversed(history):
        short_step_id = entry.get("short_step_id")
        if isinstance(short_step_id, str) and short_step_id:
            if short_step_id in seen_refs:
                continue
            seen_refs.add(short_step_id)
        latest_reversed.append(entry)
    latest_reversed.reverse()
    return latest_reversed


def parse_short_step_id(short_step_id: str) -> ShortStepIdParts:
    """short_step_id をパースして scope, local_step_number, suffix を返す（fail-closed）。

    Args:
        short_step_id: 短縮ID（例: "S-1-USER", "S-2-THINK", "G1-12-TOOL"）

    Returns:
        short_step_id の分解結果

    Raises:
        ValueError: short_step_id が仕様に合わない場合
    """
    match = SHORT_STEP_ID_RE.match(short_step_id)
    if not match:
        raise ValueError(
            f"Invalid short_step_id format: {short_step_id!r}. "
            f"Expected format: 'S-{{n}}-{{{SHORT_STEP_SUFFIX_PATTERN}}}' or "
            f"'G{{n}}-{{m}}-{{{SHORT_STEP_SUFFIX_PATTERN}}}' "
            "(where n, m are positive integers)."
        )
    scope = match.group(1)
    local_step_number = int(match.group(2))
    suffix = ShortStepSuffix(match.group(3))
    return ShortStepIdParts(
        scope_handle=scope,
        local_step_number=local_step_number,
        suffix=suffix,
    )


def scope_for_goal_id(goal_id: str) -> str:
    """goal_id（例: "G1"）から scope 識別子（例: "G1"）を抽出する。

    Args:
        goal_id: Goal の内部 ID（例: "G1", "G2"）

    Returns:
        scope 識別子（例: "G1", "G2"）

    Raises:
        ValueError: goal_id が仕様に合わない場合
    """
    if GOAL_SCOPE_RE.fullmatch(goal_id):
        return goal_id
    raise ValueError(
        f"Invalid goal_id format: {goal_id!r}. Expected format: 'G{{n}}' (where n is a positive integer)."
    )


def build_short_step_id(
    scope_handle: str,
    local_step_number: int,
    step_type: StepType,
) -> str:
    """短縮IDを生成する。

    Args:
        scope_handle: スコープ識別子（`S` または `G{n}`）
        local_step_number: スコープ内での論理ステップ番号（1以上）
        step_type: "user_request"、"llm_output"、"tool_execution" のいずれか

    Returns:
        短縮ID（例: "S-1-USER", "S-2-THINK", "G1-2-TOOL"）

    Raises:
        ValueError: 引数が仕様に合わない場合
    """
    if local_step_number < 1:
        raise ValueError(f"local_step_number must be >= 1, got {local_step_number}")
    match step_type:
        case StepType.ASSISTANT_MESSAGE:
            type_suffix = ShortStepSuffix.ASSISTANT
        case StepType.USER_REQUEST:
            type_suffix = ShortStepSuffix.USER
        case StepType.LLM_OUTPUT:
            type_suffix = ShortStepSuffix.THINK
        case StepType.TOOL_EXECUTION:
            type_suffix = ShortStepSuffix.TOOL
        case _ as unreachable:
            _unreachable_step_type(unreachable)
    short_step_id = f"{scope_handle}-{local_step_number}-{type_suffix.value}"
    # fail-closed: 生成物が仕様外なら即座に止める（DBガードと整合）
    parse_short_step_id(short_step_id)
    return short_step_id


def require_active_action_phase(state: ActionAgentState) -> ActionPhase:
    """Return the lifecycle phase allowed to run Supervisor THINK/ACTION."""

    phase = state.get("phase")
    if phase in {"planning", "executing"}:
        return phase
    raise ValueError(
        "Supervisor execution is available only while planning or executing; "
        f"got phase={phase!r}."
    )


def infer_tool_short_step_id_from_previous_supervisor_think(
    state: ActionAgentState,
    *,
    default_scope_handle: str,
) -> ToolShortStepIdInference:
    """Infer a TOOL short ID from the preceding Supervisor THINK step.

    The pair is identified by lifecycle phase and StepType, independent of graph
    node names or persistence labels.

    Returns:
        TOOL 用の short_step_id 推定結果
    """

    scope_handle = default_scope_handle
    local_step_number: int | None = None
    phase = require_active_action_phase(state)
    history = get_history_for_scope(state, default_scope_handle)
    for entry in reversed(history):
        if entry.get(_KEY_PHASE) != phase:
            continue
        if entry.get(_KEY_STEP_TYPE) != StepType.LLM_OUTPUT:
            continue
        prev_short_step_id = entry.get(_KEY_SHORT_STEP_ID)
        if isinstance(prev_short_step_id, str) and prev_short_step_id:
            try:
                parsed = parse_short_step_id(prev_short_step_id)
                scope_handle = parsed.scope_handle
                local_step_number = parsed.local_step_number
            except ValueError as exc:
                logger.debug(
                    "invalid short_step_id in supervisor thinking history "
                    "(short_step_id_shape=%s exception_type=%s)",
                    safe_value_shape(prev_short_step_id),
                    exception_type_name(exc),
                )
                local_step_number = None
        break

    if local_step_number is None:
        local_step_number = get_or_increment_local_step_number(state, scope_handle)
    short_step_id = build_short_step_id(
        scope_handle, local_step_number, StepType.TOOL_EXECUTION
    )
    return ToolShortStepIdInference(
        scope_handle=scope_handle,
        local_step_number=local_step_number,
        short_step_id=short_step_id,
    )


__all__ = [
    "_build_tool_error_payload",
    "_context_view",
    "append_history_entry",
    "get_history_for_scope",
    "get_or_increment_local_step_number",
    "build_short_step_id",
    "parse_short_step_id",
    "project_latest_history_entries",
    "SUPERVISOR_SCOPE_HANDLE",
    "scope_for_goal_id",
    "SHORT_STEP_ID_RE",
    "ShortStepIdParts",
    "ToolShortStepIdInference",
    "upsert_history_entry",
]
