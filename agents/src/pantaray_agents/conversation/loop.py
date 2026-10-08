"""One model conversation, run turn by turn until its caller accepts an end.

The loop every agent on ``LlmActionTurnRequest`` shares. It owns an append-only
history (``prefix.py``), so each request extends the last and a provider turn
goes back where it was produced; one send per turn through
``send_dropping_refused_turns``; repairing refused output; running calls as
``plan_tool_batch`` says; the last turn, told in words with the tools
unchanged; and the context budget, which omits older outputs behind a
boundary over history entries (``window.py``). Storage, prompts, approvals,
the user's view and how a run ends stay with the caller. Every item the loop
appends, but ``before_send``'s, reaches exactly one of ``on_turn``,
``on_result`` and ``on_notice``, in order, so what they stored rebuilds the
history.
"""

from __future__ import annotations

import asyncio
import functools
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from pantaray_agents.conversation.budget import (
    UsageTotals,
    input_bytes,
    turn_usage,
)
from pantaray_agents.conversation.prefix import fingerprint_request
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
from pantaray_agents.conversation.window import (
    ConversationEntry,
    WindowState,
    lay_out,
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
    LlmConversation,
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
class ConversationRequest:
    """One send, for ``send`` to pass on; ``media_refs`` in item order."""

    prompt: str
    system_instruction: str
    tools: tuple[LlmToolDefinition, ...]
    max_parallel_tool_calls: int
    conversation: list[LlmTurnItem]
    media_refs: tuple[str, ...]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class RecordedTurn:
    """An answered turn; ``provider_turn`` is to store under its ``turn_id``.

    ``window`` is where the window stands after it, which a resumed run is
    given back to keep the same boundary and measurements.
    """

    entry: ConversationEntry
    reply: TurnReply
    provider_turn: ActionProviderTurnRecord | None
    window: WindowState


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
    """Carry on: the notice answers the ending call, or follows as a message."""

    notice: str


type SendTurn = Callable[[ConversationRequest], Awaitable[TurnReply]]


@dataclass(frozen=True, slots=True)
class ConversationRun[T]:
    """The head every request is sent behind, the history, bounds and hooks.

    The loop runs ``tools`` and never ``ending_tools``: a call to one alone is
    for ``decide``. ``max_turns`` counts answered turns, not repaired sends;
    ``max_tool_calls`` the calls run. ``before_send`` raises to stop, or returns
    the items that arrived. ``on_result`` stores, redacts or spills a result and
    returns what is sent. Once ``on_turn`` returns, whatever is raised -- a
    stop included -- leaves only after every call of the turn is answered,
    but for one whose own exception it is. The hooks are awaited to their end.

    ``window`` is where the last run left it (``RecordedTurn.window``), and
    ``usage`` reads the run's usage totals, which each send's difference
    calibrates the budget with. A ``ContextCapacityExceeded`` carries the
    boundary the rebuild reached, which stays moved.
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
    window: WindowState
    usage: Callable[[], UsageTotals]
    send: SendTurn
    before_send: Callable[[], Awaitable[Sequence[LlmTurnItem]]]
    on_turn: Callable[[RecordedTurn], Awaitable[None]]
    on_result: Callable[
        [LlmToolCall, ReactToolResult], Awaitable[LlmTurnToolResultItem]
    ]
    on_notice: Callable[[LlmTurnUserItem], Awaitable[None]]
    decide: Callable[[IdleTurn], Finish[T] | Continue]


class ConversationOutputInvalid(RuntimeError):
    """Every send of one turn came back refused; ``args`` are the reasons."""


class ConversationTurnsExhausted(RuntimeError):
    """The last turn passed without an end the caller accepted."""


async def run_conversation[T](run: ConversationRun[T]) -> T:
    """Run the conversation until ``run.decide`` returns ``Finish``.

    Every exception ``send``, a hook or a tool raises propagates as it was,
    apart from a refused output, which is repaired.
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
        self.head_bytes = input_bytes(run.prompt, run.system_instruction) + sum(
            len(tool.model_dump_json().encode("utf-8")) for tool in self.tools
        )
        self.window = run.window
        self.call_ids = {
            call.call_id
            for entry in self.history
            if isinstance(entry.item, LlmTurnAssistantItem)
            for call in entry.item.calls
        }
        self.tool_calls = 0
        self.turn = _Turn(calls=())

    async def run(self) -> T:
        for turn_index in range(self.spec.max_turns):
            last_index = self.spec.max_turns - 1
            final = (
                turn_index == last_index or self.tool_calls >= self.spec.max_tool_calls
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
            self.turn = _Turn(calls=response.calls)
            record = self.store.accept(
                step_id=turn_id, turn=reply.provider_turn, fingerprint=sent.fingerprint
            )
            recorded = RecordedTurn(
                entry=entry, reply=reply, provider_turn=record, window=self.window
            )
            _, stop = await _to_the_end(self.spec.on_turn(recorded))
            # The caller now holds the turn, so whatever is raised from here on,
            # a stop included, leaves only once every call of it is answered.
            try:
                if stop is not None:
                    raise stop
                finished = await self._finish_turn(response, final=final)
            except BaseException as failure:
                await _to_the_end(self._settle(failure))
                raise
            if finished is not None:
                return finished.value
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
            prepared = prepare()
            usage_before = self.spec.usage()
            try:
                with bind_request_llm_connection(connection):
                    reply, sent = await send_dropping_refused_turns(
                        lambda window: self.spec.send(window.request),
                        prepared=prepared,
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
            finally:
                # A refused send was paid for too, and measures the same input.
                self.window = self.window.record(
                    turn_usage(usage_before, self.spec.usage()),
                    rendered_bytes=prepared.rendered_bytes,
                    rebuilt=prepared.rebuilt,
                )
            ids = [call.call_id for call in reply.response.calls]
            taken = [
                id_ for n, id_ in enumerate(ids) if id_ in {*self.call_ids, *ids[:n]}
            ]
            if not taken:
                return reply, sent.request
            # Its result would answer two calls, which refuses every later request.
            reasons.append(f"call_id {taken[0]!r} is already taken; use a new one.")
        raise ConversationOutputInvalid(*reasons)

    def _request(self, notices: list[str], max_parallel: int) -> _Prepared:
        """The request to send, rebuilt behind a later boundary when it must be.

        Laid out for every send from the store as it stands, so a store emptied
        by a refused replay sends the same items without the turns. The tools
        are the same on every request, a rebuilt one included.
        """

        before = self.window
        self.window, window = before.fit(
            self.history,
            lambda omit_before: lay_out(
                self.history,
                omit_before=omit_before,
                fingerprint=self.request_fingerprint,
                turns=self.store.turns,
                notices=notices,
                head_bytes=self.head_bytes,
            ),
        )
        layout = window.layout
        return _Prepared(
            request=ConversationRequest(
                prompt=self.spec.prompt,
                system_instruction=self.spec.system_instruction,
                tools=self.tools,
                max_parallel_tool_calls=max_parallel,
                conversation=layout.items,
                media_refs=tuple(layout.media_refs()),
                fingerprint=layout.fingerprint,
            ),
            rendered_bytes=window.rendered_bytes,
            rebuilt=self.window.omit_before > before.omit_before,
        )

    async def _finish_turn(
        self, response: LlmActionTurnResponse, *, final: bool
    ) -> Finish[T] | None:
        ran = await self._run_calls(response, final=final)
        if response.dropped_call_names:
            # Such a call has no id to answer, so a message names it.
            await self._notice(
                "# System Notice\nNot run this turn: "
                f"{', '.join(response.dropped_call_names)} "
                f"({PROVIDER_DROPPED_NOTICE})."
            )
        if ran:
            return None
        ending = self.turn.ending
        decision = self.spec.decide(
            IdleTurn(response=response, ending_call=ending, final=final)
        )
        if isinstance(decision, Finish):
            return decision
        if ending is not None:
            await self._answer(ending, decision.notice, ENDING_REJECTED_ERROR_CODE)
        else:
            await self._notice(decision.notice)
        if final:
            raise ConversationTurnsExhausted(
                "the last turn ended without an accepted end"
            )
        return None

    async def _run_calls(self, response: LlmActionTurnResponse, *, final: bool) -> bool:
        """Run what may run now, answer the rest; say whether a tool ran."""

        turn = self.turn
        if final:
            ending = [call for call in response.calls if call.name in self.ending_names]
            turn.ending = ending[0] if len(ending) == 1 else None
            last = (
                f"Not run: this call came on the last turn, where {self.what_counts}."
            )
            turn.held = {
                call.call_id: _not_run(EXCLUSION_NOTICES["run_ending_tool"])
                if call.name in self.ending_names
                else last
                for call in response.calls
            }
            await self._settle()
            return False
        plan = plan_tool_batch(
            [_Planned(call) for call in response.calls],
            concurrency=self.concurrency,
            max_parallel=self.spec.max_parallel_tool_calls,
            remaining_tool_steps=self.spec.max_tool_calls - self.tool_calls,
        )
        turn.held = {
            entry.call.call.call_id: _not_run(EXCLUSION_NOTICES[entry.reason])
            for entry in (*plan.deferred, *plan.dropped)
        }
        runnable = [planned.call for planned in plan.calls]
        if len(runnable) == 1 and runnable[0].name in self.ending_names:
            # Calls the provider dropped were siblings the end would lose.
            if not response.dropped_call_names:
                turn.ending = runnable[0]
                return False
            turn.held[runnable.pop().call_id] = _not_run(
                EXCLUSION_NOTICES["run_ending_tool"]
            )
        if plan.mode == "parallel":
            for call in runnable:
                self._start(call)
            await asyncio.wait(turn.tasks.values())
            for call in runnable:
                turn.tasks[call.call_id].result()  # the first failure, in order
        else:
            for call in runnable:
                await self._record(call, await self._start(call))
        await self._settle()
        return bool(runnable)

    def _start(self, call: LlmToolCall) -> asyncio.Future[ReactToolResult]:
        self.tool_calls += 1
        task = asyncio.ensure_future(
            self.registry.execute(_react_call(call), self.tool_calls)
        )
        self.turn.tasks[call.call_id] = task
        return task

    async def _settle(self, failure: BaseException | None = None) -> None:
        """Answer each call not answered yet, once, in the model's order.

        A finished call gets its result, the one whose own exception is
        ``failure`` none (a pause is the resumed run's), any other a not-run
        answer. Without a failure the ending call stays for ``decide``.
        """

        turn = self.turn
        for running in turn.tasks.values():
            running.cancel()
        if turn.tasks:
            await asyncio.wait(turn.tasks.values())
        stopped = _not_run("did not run to the end because the run stopped")
        for call in turn.calls:
            if call.call_id in turn.answered or (
                failure is None and call is turn.ending
            ):
                continue
            task = turn.tasks.get(call.call_id)
            if task is not None and not task.cancelled():
                if task.exception() is None:
                    await self._record(call, task.result())
                    continue
                if task.exception() is failure:
                    continue
            await self._answer(call, turn.held.get(call.call_id, stopped))

    async def _answer(
        self, call: LlmToolCall, message: str, code: str = NOT_RUN_ERROR_CODE
    ) -> None:
        await self._record(
            call,
            ReactToolResult(
                tool_name=call.name,
                status="error",
                output={"status": "error", "error_code": code, "message": message},
                error_message=message,
            ),
        )

    async def _record(self, call: LlmToolCall, result: ReactToolResult) -> None:
        item, stop = await _to_the_end(self.spec.on_result(call, result))
        self.history.append(ConversationEntry(item))
        self.turn.answered.add(call.call_id)
        if stop is not None:
            raise stop

    async def _notice(self, text: str) -> None:
        item = LlmTurnUserItem(
            type="user", content=[LlmInputTextBlock(type="input_text", text=text)]
        )
        _, stop = await _to_the_end(self.spec.on_notice(item))
        self.history.append(ConversationEntry(item))
        if stop is not None:
            raise stop


@dataclass(frozen=True, slots=True)
class _Prepared:
    """One request, with what the budget measured of it."""

    request: ConversationRequest
    rendered_bytes: int
    rebuilt: bool

    @property
    def conversation(self) -> LlmConversation:
        return self.request.conversation


@dataclass(slots=True)
class _Turn:
    """What one recorded turn's calls have been answered with so far."""

    calls: Sequence[LlmToolCall]
    answered: set[str] = field(default_factory=set)
    held: dict[str, str] = field(default_factory=dict)  # not-run answers
    tasks: dict[str, asyncio.Future[ReactToolResult]] = field(default_factory=dict)
    ending: LlmToolCall | None = None


async def _to_the_end[R](work: Awaitable[R]) -> tuple[R, asyncio.CancelledError | None]:
    """Await ``work`` to its end through any stop, and return the stop.

    Its own task behind ``asyncio.shield`` keeps a stop out of a hook half-way
    through storing, which ``Task.uncancel`` clears only after it has landed.
    """

    task = asyncio.ensure_future(work)
    stop: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            stop = stop or exc
    return task.result(), stop


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
