"""Supervisor が 1 ターンで提案したツール呼び出し列の並列実行ポリシー。

このモジュールは純粋な分類・計画ロジックだけを持ち、I/O も state 変更も行わない。
実際のバッチ実行・step 採番は呼び出し側（ACTION ノード）が所有する。

分類基準（承認済み設計 2026-09-07）: `sink` を取らず、`state` を変更せず、
workspace lock を取らないツールだけを並列実行してよい。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from pantaray_agents.agents.action_agent.tools import (
    APPLY_PATCH_TOOL,
    BASH_TOOL,
    CANCEL_SUBAGENT_TOOL_ID,
    CAPTURE_SCREEN_TOOL_ID,
    DRAFT_FINAL_ANSWER_TOOL_ID,
    GET_MEMORY_REFERENCE_TOOL,
    GLOB_TOOL,
    GREP_TOOL,
    HISTORY_FETCH_TOOL,
    LINK_MEMORY_TOOL,
    LIST_TOOL,
    MEMORY_SEARCH_TOOL,
    MEMORY_SQL_TOOL,
    READ_ACTION_PLAN_TOOL_ID,
    READ_TOOL,
    REMEMBER_TOOL_ID,
    RENDER_PDF_PAGE_TOOL_ID,
    RUN_PYTHON_TOOL,
    SEND_MESSAGE_TO_SUBAGENT_TOOL_ID,
    SPAWN_SUBAGENT_TOOL_ID,
    SUBMIT_FINAL_ANSWER_TOOL_ID,
    SUBMIT_SUBAGENT_REPORT_TOOL_ID,
    THINKING_TOOL,
    UNLINK_MEMORY_TOOL,
    WAIT_SUBAGENTS_TOOL_ID,
    WEB_CRAWL_TOOL,
    WEB_EXTRACT_TOOL,
    WEB_SEARCH_TOOL,
    WRITE_ACTION_PLAN_TOOL_ID,
    ZANEI_QUERY_TOOL_ID,
    ZANEI_TIMELINE_TOOL_ID,
)

PARALLEL_SAFE_TOOL_IDS: frozenset[str] = frozenset(
    {
        READ_TOOL.tool_id,
        LIST_TOOL.tool_id,
        GLOB_TOOL.tool_id,
        GREP_TOOL.tool_id,
        WEB_SEARCH_TOOL.tool_id,
        WEB_EXTRACT_TOOL.tool_id,
        WEB_CRAWL_TOOL.tool_id,
        MEMORY_SEARCH_TOOL.tool_id,
        MEMORY_SQL_TOOL.tool_id,
        GET_MEMORY_REFERENCE_TOOL.tool_id,
        HISTORY_FETCH_TOOL.tool_id,
        READ_ACTION_PLAN_TOOL_ID,
    }
)
"""同時実行しても互いの副作用が衝突しない読み取り専用ツール。"""

SOLO_TURN_TOOL_IDS: frozenset[str] = frozenset(
    {
        WAIT_SUBAGENTS_TOOL_ID,
        SUBMIT_FINAL_ANSWER_TOOL_ID,
        SUBMIT_SUBAGENT_REPORT_TOOL_ID,
    }
)
"""そのターンで唯一の呼び出しでなければならないツール。

- ``wait_subagents``: 子プロセスが終端になるまで最大
  ``ACTION_SUBAGENT_WAIT_MAX_SECONDS`` ブロックするため、同一ターンの兄弟呼び出しを
  待たせる。
- ``submit_final_answer``: Action の終端書き込みで status を確定させるため、後続の
  兄弟呼び出しは「確定済み Action への追記」になってしまう。
- ``submit_subagent_report``: 子の終端。後続の兄弟呼び出しは報告に含まれない。
"""

RUN_ENDING_TOOL_IDS: frozenset[str] = frozenset(
    {
        SUBMIT_FINAL_ANSWER_TOOL_ID,
        SUBMIT_SUBAGENT_REPORT_TOOL_ID,
    }
)
"""SOLO_TURN のうち、実行すると run を終えるツール。

先頭で単独実行すると、後回しにした兄弟呼び出しはモデルが出し直す前に run が
終わって二度と実行されない。同じツールを 2 件出したターンで 1 件目だけを通すと、
2 件目の訂正が失われる。そこでターンの唯一の呼び出しでなければ、位置や重複に
かかわらず実行せず（``run_ending_tool``）、残りを実行して、1 件だけを単独の
ターンで出し直させる。``wait_subagents`` は run を終えないので、後回しにした兄弟は
次ターンで出し直せる。
"""

SERIAL_ONLY_TOOL_IDS: frozenset[str] = frozenset(
    {
        THINKING_TOOL.tool_id,
        LINK_MEMORY_TOOL.tool_id,
        UNLINK_MEMORY_TOOL.tool_id,
        REMEMBER_TOOL_ID,
        APPLY_PATCH_TOOL.tool_id,
        BASH_TOOL.tool_id,
        RUN_PYTHON_TOOL.tool_id,
        CAPTURE_SCREEN_TOOL_ID,
        RENDER_PDF_PAGE_TOOL_ID,
        WRITE_ACTION_PLAN_TOOL_ID,
        SPAWN_SUBAGENT_TOOL_ID,
        SEND_MESSAGE_TO_SUBAGENT_TOOL_ID,
        CANCEL_SUBAGENT_TOOL_ID,
        DRAFT_FINAL_ANSWER_TOOL_ID,
        ZANEI_QUERY_TOOL_ID,
        ZANEI_TIMELINE_TOOL_ID,
    }
)
"""バッチに含めてよいが、宣言順の逐次実行しか許さないツール。

``capture_screen`` は承認待ちで Action を一時停止し、その後 desktop client の応答を
最大 15 秒待つ。同一ターンの兄弟呼び出しと並べると、承認前に他のツールが走ったのか
どうかが履歴から読み取れなくなる。

``render_pdf_page`` は読み取り専用だが、1 回で最大 8 枚の画像を返す。同じターンに
何本も並べると、そのターンだけで数十枚がモデルへ送られ、直後のターンで窓から
落ちる。1 本ずつ走らせて、必要なページだけを見せる。

``zanei_timeline`` / ``zanei_query`` は読み取り専用だが、run ごとに 1 つだけ持つ
``runtime.zanei_session``（カーソル・observation 登録簿・ページ予算）を共有して
書き換える。並列化するとカーソルの進み方とページ境界が非決定になり、未オープン時は
permit と reader を二重に取得してしまう。
"""

MEMORY_EPOCH_WRITER_TOOL_IDS: frozenset[str] = frozenset(
    {
        MEMORY_SEARCH_TOOL.tool_id,
        GET_MEMORY_REFERENCE_TOOL.tool_id,
    }
)
"""``state["memory_context_epoch"]`` を read-modify-write する allowlist ツール。

``run_memory_search_tool`` と ``run_get_memory_reference_tool`` は現在の epoch を読み、
拡張した epoch を同じキーへ書き戻す。同時に走らせると片方の拡張が失われ、以降の
ターンで context handle が解決できなくなる。よって 1 バッチにつき 1 件だけ許す。
"""

type BatchMode = Literal["parallel", "sequential"]

type ExclusionReason = Literal[
    "run_ending_tool",
    "solo_turn_tool",
    "after_solo_turn_tool",
    "max_parallel_exceeded",
    "tool_step_budget_exhausted",
]


EXCLUSION_NOTICES: dict[ExclusionReason, str] = {
    "run_ending_tool": "must be the only call of its turn; send exactly one, alone",
    "solo_turn_tool": "must be the only call of its turn",
    "after_solo_turn_tool": "was queued behind a call that must run alone",
    "max_parallel_exceeded": "exceeded the parallel tool call limit of this turn",
    "tool_step_budget_exhausted": "exceeded the remaining tool step budget",
}
"""モデルへ伝える、その呼び出しをこのターンで実行しなかった理由。"""

PROVIDER_DROPPED_NOTICE = "was dropped by the model provider above the requested limit"


class ToolCallLike(Protocol):
    """計画に必要な唯一の属性（``ToolCallModel`` が満たす）。"""

    @property
    def tool_id(self) -> str: ...


@dataclass(frozen=True)
class ExcludedToolCall[CallT: ToolCallLike]:
    """このターンでは実行しない呼び出しと、その理由。"""

    call: CallT
    reason: ExclusionReason


@dataclass(frozen=True)
class ToolBatchPlan[CallT: ToolCallLike]:
    """1 ターン分のツールバッチ実行計画。"""

    calls: tuple[CallT, ...]
    """今すぐ実行する呼び出し（宣言順）。"""

    mode: BatchMode
    """``calls`` の実行方法。"""

    deferred: tuple[ExcludedToolCall[CallT], ...]
    """次ターン以降にモデルが再提案すべき呼び出し。"""

    dropped: tuple[ExcludedToolCall[CallT], ...]
    """予算超過で切り捨てた呼び出し。"""


def plan_tool_batch[CallT: ToolCallLike](
    calls: Sequence[CallT],
    *,
    max_parallel: int,
    remaining_tool_steps: int,
) -> ToolBatchPlan[CallT]:
    """提案されたツール呼び出し列を、このターンで実行する形へ決定的に切り分ける。

    順序は常に宣言順を保ち、並べ替えは行わない。

    0. RUN_ENDING ツールがターンの唯一の呼び出しでなければ、位置や重複にかかわらず
       すべて ``run_ending_tool`` として外し、残りの列で以下を行う。実行分が空の
       計画もありうる。
    1. SOLO_TURN ツールで列を分割する。先頭にあればそれ 1 件だけを実行し、残りは
       ``after_solo_turn_tool`` として次ターンへ回す。途中にあれば、その手前までを
       実行し、SOLO_TURN ツール自身（``solo_turn_tool``）と後続を次ターンへ回す。
    2. 残った先頭側を ``min(max_parallel, remaining_tool_steps)`` 件に切り詰め、
       溢れた分を ``dropped`` にする。
    3. 実行分が 2 件以上で全件 allowlist、かつ epoch を書くツールが 1 件以下のときだけ
       ``parallel``。それ以外は ``sequential``。
    """

    ending = [call for call in calls if call.tool_id in RUN_ENDING_TOOL_IDS]
    held_back: tuple[ExcludedToolCall[CallT], ...] = ()
    if ending and len(calls) > 1:
        held_back = _defer_all(ending, "run_ending_tool")
        calls = [call for call in calls if call.tool_id not in RUN_ENDING_TOOL_IDS]
    runnable, deferred = _split_at_solo_turn_tool(calls)
    limit = max(min(max_parallel, remaining_tool_steps), 0)
    dropped = tuple(
        ExcludedToolCall(
            call=call,
            reason=(
                "tool_step_budget_exhausted"
                if index >= remaining_tool_steps
                else "max_parallel_exceeded"
            ),
        )
        for index, call in enumerate(runnable[limit:], start=limit)
    )
    accepted = runnable[:limit]
    return ToolBatchPlan(
        calls=accepted,
        mode=_batch_mode(accepted),
        deferred=(*deferred, *held_back),
        dropped=dropped,
    )


def _split_at_solo_turn_tool[CallT: ToolCallLike](
    calls: Sequence[CallT],
) -> tuple[tuple[CallT, ...], tuple[ExcludedToolCall[CallT], ...]]:
    for index, call in enumerate(calls):
        if call.tool_id in SOLO_TURN_TOOL_IDS:
            if index == 0:
                return (calls[0],), _defer_all(calls[1:], "after_solo_turn_tool")
            return tuple(calls[:index]), (
                ExcludedToolCall(call=call, reason="solo_turn_tool"),
                *_defer_all(calls[index + 1 :], "after_solo_turn_tool"),
            )
    return tuple(calls), ()


def _defer_all[CallT: ToolCallLike](
    calls: Sequence[CallT], reason: ExclusionReason
) -> tuple[ExcludedToolCall[CallT], ...]:
    return tuple(ExcludedToolCall(call=call, reason=reason) for call in calls)


def _batch_mode[CallT: ToolCallLike](calls: tuple[CallT, ...]) -> BatchMode:
    # 1 件のバッチは既存の単発実行パスと同じ意味なので sequential に倒す。
    if len(calls) < 2:
        return "sequential"
    if any(call.tool_id not in PARALLEL_SAFE_TOOL_IDS for call in calls):
        return "sequential"
    epoch_writers = sum(
        1 for call in calls if call.tool_id in MEMORY_EPOCH_WRITER_TOOL_IDS
    )
    return "sequential" if epoch_writers > 1 else "parallel"


__all__ = [
    "EXCLUSION_NOTICES",
    "MEMORY_EPOCH_WRITER_TOOL_IDS",
    "PROVIDER_DROPPED_NOTICE",
    "RUN_ENDING_TOOL_IDS",
    "PARALLEL_SAFE_TOOL_IDS",
    "SERIAL_ONLY_TOOL_IDS",
    "SOLO_TURN_TOOL_IDS",
    "BatchMode",
    "ExcludedToolCall",
    "ExclusionReason",
    "ToolBatchPlan",
    "ToolCallLike",
    "plan_tool_batch",
]
