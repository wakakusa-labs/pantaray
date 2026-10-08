"""One model conversation, run turn by turn until its caller accepts an end.

Every agent that talks to a model through ``LlmActionTurnRequest`` needs this
loop around it. It owns the history, laid out with ``ConversationLayout`` and
only ever appended to, so each request extends the last and a provider turn
goes back on the item it was produced for (``prefix.py``); one send per turn
through ``send_dropping_refused_turns``; repairing an output the model contract
refused (a schema, the commentary limit, a reused call id) by resending with
the reason appended; which calls run now and how (``plan_tool_batch``), every
call not run answered as such; and the last turn, told in words with the tool
list unchanged -- a changed list voids the replayed reasoning.

Storage, prompts, approvals, what reaches the user and the budget stay with
the caller, as does how a run ends: by an ending tool, or by a turn that calls
nothing. Every item the loop appends, apart from ``before_send``'s own, reaches
the caller through exactly one of ``on_turn``, ``on_result`` and ``on_notice``,
in history order, so storing those rebuilds the same history after a restart.
A repair or last-turn notice closes only its own request and is never kept.
"""

from __future__ import annotations

import asyncio
import functools
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from pantaray_agents.conversation.prefix import (
    ConversationLayout,
    fingerprint_request,
    replayable_turn,
)
from pantaray_agents.conversation.provider_turns import (
    ProviderTurnStore,
    read_provider_turn_target,
    send_dropping_refused_turns,
)
from pantaray_agents.conversation.tool_batch import (
    EXCLUSION_NOTICES,
    PROVIDER_DROPPED_NOTICE,
    plan_tool_batch,
)
from pantaray_agents.local_runtime.runtime.connection_store import (
    bind_request_llm_connection,
)
from pantaray_agents.schema.agent.action import ActionProviderTurnRecord
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolDefinition,
    ReactToolRegistry,
    ReactToolResult,
    ToolCallEnvelope,
    ToolConcurrency,
)
from pantaray_llm.contracts.action_turn import LlmActionTurnResponse
from pantaray_llm.contracts.conversation import (
    LlmProviderTurn,
    LlmTurnAssistantItem,
    LlmTurnItem,
    LlmTurnToolResultItem,
    LlmTurnUserItem,
)
from pantaray_llm.contracts.input_block import LlmInputTextBlock
from pantaray_llm.contracts.tool_use import LlmToolCall, LlmToolDefinition
from pantaray_llm.errors import LlmProxyExecutionError
from pantaray_llm.providers.openai_responses.retry_policy import (
    LLM_TOOL_CALL_MAX_CONSECUTIVE_ERRORS,
)

# The Action's bound: five sends of one turn, the first and four repairs.
REPAIR_MAX_ATTEMPTS = LLM_TOOL_CALL_MAX_CONSECUTIVE_ERRORS
NOT_RUN_ERROR_CODE = "TOOL_CALL_NOT_RUN"
# The code the native ReAct loop answers a refused ending with.
ENDING_REJECTED_ERROR_CODE = "COMPLETION_PRECONDITION_FAILED"
_RUN_ENDING = ToolConcurrency("run_ending")


class TurnReply(Protocol):
    """One answered turn: ``ActionTurnReply`` as the mixin returns it."""

    @property
    def response(self) -> LlmActionTurnResponse: ...

    @property
    def provider_turn(self) -> LlmProviderTurn | None: ...


@dataclass(frozen=True, slots=True)
class ConversationEntry:
    """One history item; ``turn_id`` keys an assistant's turn in the store."""

    item: LlmTurnItem
    turn_id: str | None = None


@dataclass(frozen=True, slots=True)
class ConversationRequest:
    """Everything one send carries, for ``send`` to pass on as is.

    ``media_refs`` are the refs of the images the items place, in item order.
    """

    prompt: str
    system_instruction: str
    tools: tuple[LlmToolDefinition, ...]
    max_parallel_tool_calls: int
    conversation: list[LlmTurnItem]
    media_refs: tuple[str, ...]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class RecordedTurn:
    """An answered turn; ``provider_turn`` is to store under its ``turn_id``."""

    entry: ConversationEntry
    reply: TurnReply
    provider_turn: ActionProviderTurnRecord | None


@dataclass(frozen=True, slots=True)
class IdleTurn:
    """A turn that ran no tool, for the caller to end or carry on.

    ``ending_call`` is the ending call the turn made alone, or as the only one
    on its last turn. On a ``final`` turn a ``Continue`` is answered as usual
    and then fails the run.
    """

    response: LlmActionTurnResponse
    ending_call: LlmToolCall | None
    final: bool


@dataclass(frozen=True, slots=True)
class Finish[T]:
    value: T


@dataclass(frozen=True, slots=True)
class Continue:
    """Carry on, and tell the model why.

    The notice answers the ending call as refused, or follows a turn without
    one as a message, so the next request never ends on the model's own turn.
    """

    notice: str


type SendTurn = Callable[[ConversationRequest], Awaitable[TurnReply]]


@dataclass(frozen=True, slots=True)
class ConversationRun[T]:
    """The head every request is sent behind, the history, bounds and hooks.

    The loop runs ``tools`` and never ``ending_tools``: a call to one alone is
    for ``decide``. ``max_turns`` counts answered turns, not repaired sends;
    ``max_tool_calls`` the calls run. ``before_send`` raises to stop, or returns
    the items that arrived. ``on_result`` stores, redacts or spills a result and
    returns what is sent. A tool raising one of ``interrupts`` pauses the run:
    the turn's later calls are answered as not run, and the call that raised is
    the resumed run's to settle.
    """

    prompt: str
    system_instruction: str
    tools: tuple[ReactToolDefinition, ...]
    ending_tools: tuple[LlmToolDefinition, ...]
    history: Sequence[ConversationEntry]
    provider_turns: ProviderTurnStore
    inference_profile: str
    max_turns: int
    max_tool_calls: int
    max_parallel_tool_calls: int
    send: SendTurn
    before_send: Callable[[], Awaitable[Sequence[LlmTurnItem]]]
    on_turn: Callable[[RecordedTurn], Awaitable[None]]
    on_result: Callable[
        [LlmToolCall, ReactToolResult], Awaitable[LlmTurnToolResultItem]
    ]
    on_notice: Callable[[LlmTurnUserItem], Awaitable[None]]
    decide: Callable[[IdleTurn], Finish[T] | Continue]
    interrupts: tuple[type[BaseException], ...] = ()


class ConversationOutputInvalid(RuntimeError):
    """Every send of one turn came back refused; ``args`` are the reasons."""


class ConversationTurnsExhausted(RuntimeError):
    """The last turn passed without an end the caller accepted."""


async def run_conversation[T](run: ConversationRun[T]) -> T:
    """Run the conversation until ``run.decide`` returns ``Finish``.

    Every exception ``send``, a hook or a tool raises propagates as it was,
    apart from a refused output, which is repaired, and an ``interrupts``
    exception, which first answers the calls left behind it.
    """

    return await _Run(run).run()


class _Run[T]:
    def __init__(self, run: ConversationRun[T]) -> None:
        self.spec = run
        self.history = list(run.history)
        self.store = run.provider_turns
        self.tools = (
            *run.ending_tools,
            *(
                LlmToolDefinition(
                    name=tool.name,
                    description=tool.description,
                    parameters=dict(tool.request_schema),
                )
                for tool in run.tools
            ),
        )
        self.registry = ReactToolRegistry(run.tools)
        self.ending_names = frozenset(tool.name for tool in run.ending_tools)
        self.what_counts = (
            f"only a single call to {' or '.join(sorted(self.ending_names))} counts"
            if self.ending_names
            else "no tool call runs"
        )
        self.concurrency = {
            tool.name: tool.concurrency for tool in run.tools
        } | dict.fromkeys(self.ending_names, _RUN_ENDING)
        self.request_fingerprint = fingerprint_request(
            head=run.prompt, system_instruction=run.system_instruction, tools=self.tools
        )
        self.call_ids = {
            call.call_id
            for entry in self.history
            if isinstance(entry.item, LlmTurnAssistantItem)
            for call in entry.item.calls
        }
        self.tool_calls = 0

    async def run(self) -> T:
        for turn_index in range(self.spec.max_turns):
            final = (
                turn_index == self.spec.max_turns - 1
                or self.tool_calls >= self.spec.max_tool_calls
            )
            self.history.extend(
                ConversationEntry(item) for item in await self.spec.before_send()
            )
            reply, sent = await self._send(final=final)
            response = reply.response
            turn_id = uuid.uuid4().hex
            entry = ConversationEntry(
                LlmTurnAssistantItem(
                    type="assistant",
                    text=[message.text for message in response.messages],
                    calls=list(response.calls),
                ),
                turn_id=turn_id,
            )
            self.history.append(entry)
            self.call_ids.update(call.call_id for call in response.calls)
            await self.spec.on_turn(
                RecordedTurn(
                    entry=entry,
                    reply=reply,
                    provider_turn=self.store.accept(
                        step_id=turn_id,
                        turn=reply.provider_turn,
                        fingerprint=sent.fingerprint,
                    ),
                )
            )
            if final:
                ran, ending_call = False, await self._close_last_turn(response.calls)
            else:
                ran, ending_call = await self._run_calls(response)
            if response.dropped_call_names:
                # Such a call has no id to answer, so a message names it.
                await self._notice(
                    "# System Notice\nNot run this turn: "
                    f"{', '.join(response.dropped_call_names)} "
                    f"({PROVIDER_DROPPED_NOTICE})."
                )
            if ran:
                continue
            decision = self.spec.decide(
                IdleTurn(response=response, ending_call=ending_call, final=final)
            )
            if isinstance(decision, Finish):
                return decision.value
            if ending_call is not None:
                await self._answer(
                    ending_call, decision.notice, ENDING_REJECTED_ERROR_CODE
                )
            else:
                await self._notice(decision.notice)
            if final:
                raise ConversationTurnsExhausted(
                    "the last turn ended without an accepted end"
                )
        raise ConversationTurnsExhausted("the run has no turn to send")

    async def _send(self, *, final: bool) -> tuple[TurnReply, ConversationRequest]:
        left = self.spec.max_tool_calls - self.tool_calls
        max_parallel = 1 if final else min(self.spec.max_parallel_tool_calls, left)
        last = f"This is the last turn, where {self.what_counts}."
        reasons: list[str] = []
        for _ in range(REPAIR_MAX_ATTEMPTS):
            # A repair notice goes last, so a repaired send is an append to the
            # one it repairs. It is never kept, and the turn produced behind it
            # never goes back, its prefix holding the notice.
            notices = ([last] if final else []) + [
                f"The previous output could not be processed because: {reason}\n"
                "Return commentary and/or tool calls with valid arguments."
                for reason in reasons[-1:]
            ]
            identity, connection = read_provider_turn_target(
                inference_profile=self.spec.inference_profile
            )
            self.store.use_identity(identity)
            prepare = functools.partial(self._request, notices, max_parallel)
            try:
                with bind_request_llm_connection(connection):
                    reply, sent = await send_dropping_refused_turns(
                        self.spec.send,
                        prepared=prepare(),
                        prepare=prepare,
                        store=self.store,
                    )
            except LlmProxyExecutionError as exc:
                if exc.recovery != "repair_next_turn":
                    raise
                detail = exc.error_message.strip() or "the output was invalid"
                violation = exc.tool_call_violation_reason or "invalid_response"
                reasons.append(f"{violation}: {detail}")
                continue
            ids = [call.call_id for call in reply.response.calls]
            taken = [
                id_ for n, id_ in enumerate(ids) if id_ in {*self.call_ids, *ids[:n]}
            ]
            if not taken:
                return reply, sent
            # Its result would answer two calls, which refuses every later request.
            reasons.append(f"call_id {taken[0]!r} is already taken; use a new one.")
        raise ConversationOutputInvalid(*reasons)

    def _request(self, notices: list[str], max_parallel: int) -> ConversationRequest:
        """Lay the history out behind the head, each turn back where it still fits.

        Rebuilt for every send from the store as it stands, so a store emptied
        by a refused replay sends the same items without the turns.
        """

        layout = ConversationLayout(fingerprint=self.request_fingerprint)
        for entry in self.history:
            item = entry.item
            if isinstance(item, LlmTurnAssistantItem) and entry.turn_id is not None:
                turn = replayable_turn(
                    self.store.turns.get(entry.turn_id),
                    item.calls,
                    prefix=layout.fingerprint,
                )
                if turn is not None:
                    layout.add(
                        item.model_copy(update={"provider_turn": turn}),
                        turn_of=entry.turn_id,
                    )
                    continue
            layout.add(item)
        for notice in notices:
            layout.add_text(f"# System Notice\n{notice}")
        return ConversationRequest(
            prompt=self.spec.prompt,
            system_instruction=self.spec.system_instruction,
            tools=self.tools,
            max_parallel_tool_calls=max_parallel,
            conversation=layout.items,
            media_refs=tuple(layout.media_refs()),
            fingerprint=layout.fingerprint,
        )

    async def _run_calls(
        self, response: LlmActionTurnResponse
    ) -> tuple[bool, LlmToolCall | None]:
        """Run what the plan lets run now; say whether a tool ran, or the end."""

        plan = plan_tool_batch(
            [_Planned(call) for call in response.calls],
            concurrency=self.concurrency,
            max_parallel=self.spec.max_parallel_tool_calls,
            remaining_tool_steps=self.spec.max_tool_calls - self.tool_calls,
        )
        excluded = [
            (entry.call.call, _not_run(EXCLUSION_NOTICES[entry.reason]))
            for entry in (*plan.deferred, *plan.dropped)
        ]
        runnable = [planned.call for planned in plan.calls]
        if len(runnable) == 1 and runnable[0].name in self.ending_names:
            # Calls the provider dropped were siblings the end would lose.
            if not response.dropped_call_names:
                return False, runnable[0]
            excluded = [
                (runnable.pop(), _not_run(EXCLUSION_NOTICES["run_ending_tool"]))
            ]
        if plan.mode == "parallel":
            await self._run_at_once(runnable, excluded)
        else:
            await self._run_in_order(runnable, excluded)
        return bool(runnable), None

    async def _run_in_order(
        self,
        calls: Sequence[LlmToolCall],
        excluded: Sequence[tuple[LlmToolCall, str]],
    ) -> None:
        for index, call in enumerate(calls):
            self.tool_calls += 1
            try:
                result = await self.registry.execute(_react_call(call), self.tool_calls)
            except self.spec.interrupts:
                notice = _not_run("came after a call that paused the run")
                left = [(later, notice) for later in calls[index + 1 :]]
                await self._answer_all([*left, *excluded])
                raise
            await self._append_result(call, result)
        await self._answer_all(excluded)

    async def _run_at_once(
        self,
        calls: Sequence[LlmToolCall],
        excluded: Sequence[tuple[LlmToolCall, str]],
    ) -> None:
        first = self.tool_calls + 1
        self.tool_calls += len(calls)
        # Waits for every call, so none is left running behind a failure; what
        # came back and what was held back are answered before the first
        # failure, in the model's order, is raised.
        outcomes = await asyncio.gather(
            *(
                self.registry.execute(_react_call(call), first + index)
                for index, call in enumerate(calls)
            ),
            return_exceptions=True,
        )
        failure: BaseException | None = None
        for call, outcome in zip(calls, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                failure = failure or outcome
            else:
                await self._append_result(call, outcome)
        await self._answer_all(excluded)
        if failure is not None:
            raise failure

    async def _close_last_turn(
        self, calls: Sequence[LlmToolCall]
    ) -> LlmToolCall | None:
        """Answer every call of the last turn but a single ending one as not run."""

        ending = [call for call in calls if call.name in self.ending_names]
        ending_call = ending[0] if len(ending) == 1 else None
        last = f"Not run: this call came on the last turn, where {self.what_counts}."
        await self._answer_all(
            [
                (call, _not_run(EXCLUSION_NOTICES["run_ending_tool"]))
                if call.name in self.ending_names
                else (call, last)
                for call in calls
                if call is not ending_call
            ]
        )
        return ending_call

    async def _answer_all(self, calls: Sequence[tuple[LlmToolCall, str]]) -> None:
        for call, message in calls:
            await self._answer(call, message)

    async def _answer(
        self, call: LlmToolCall, message: str, code: str = NOT_RUN_ERROR_CODE
    ) -> None:
        await self._append_result(
            call,
            ReactToolResult(
                tool_name=call.name,
                status="error",
                output={"status": "error", "error_code": code, "message": message},
                error_message=message,
            ),
        )

    async def _append_result(self, call: LlmToolCall, result: ReactToolResult) -> None:
        item = await self.spec.on_result(call, result)
        self.history.append(ConversationEntry(item))

    async def _notice(self, text: str) -> None:
        item = LlmTurnUserItem(
            type="user", content=[LlmInputTextBlock(type="input_text", text=text)]
        )
        self.history.append(ConversationEntry(item))
        await self.spec.on_notice(item)


@dataclass(frozen=True, slots=True)
class _Planned:
    call: LlmToolCall

    @property
    def tool_id(self) -> str:
        return self.call.name


def _react_call(call: LlmToolCall) -> ReactToolCall:
    return ReactToolCall(
        tool_name=call.name,
        tool_args=call.arguments,
        tool_call_envelope=ToolCallEnvelope(
            tool_id=call.name, reason=None, args=call.arguments
        ),
    )


def _not_run(notice: str) -> str:
    return (
        f"Not run: this call {notice}. "
        "Request it again in a later turn if it is still needed."
    )
