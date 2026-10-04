"""Live end-to-end check of the direct route for a logged-out owner.

Starts a real local backend helper in a throwaway runtime root the way Electron
main does, configures it over the control socket with no cloud session and a
stored OpenAI key plus a Tavily key, then drives an Action through the same entry
points the renderer uses -- the local HTTP API and the orchestration WebSocket,
both authenticated with the helper's local API token -- so the LLM call reaches
the real OpenAI API and the web tool reaches the real Tavily API.

`--barrier` runs model-switch and stop-barrier scenarios. The control socket
serves its operations on its own thread and loop, the Action runs on a worker
thread, and the WebSocket is served by uvicorn's loop. A change across these
boundaries cannot be shown by driving `dispatch_control_request` on one loop.

Both keys are read from the environment and never printed; the helper is spawned
without them, because `runtime/bootstrap.py` refuses to start when a provider
secret is in its environment.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import websockets
from local_helper import HelperHandle, control_request, start_helper, terminate_helper

AGENTS_ROOT = Path(__file__).resolve().parents[2]
MODEL = os.environ.get("SMOKE_MODEL", "gpt-6-luna")
# A second real model for checking that a running Action survives the switch.
ALTERNATE_MODEL = os.environ.get("SMOKE_ALTERNATE_MODEL", "gpt-5.6-terra")
# macOS caps a Unix socket path at ~104 bytes and the control socket lives under
# the runtime root, so the root goes straight under /tmp rather than $TMPDIR.
RUNTIME_ROOT_PARENT = "/tmp"
# Long enough to show what one turn's shape does to the next: a prompt cache
# reads nothing on the first turn, and a run of one search proves nothing about
# the turns after it. This asks for a parallel turn, sequential turns, a write
# and a read back, so the run crosses every shape the projection has to send.
ACTION_PROMPT = (
    "次の手順どおりに進めてください。"
    "(1) 1 回のターンで Python、Node.js、Go の最新の安定版のバージョンを "
    "web_search で 3 件まとめて調べる。"
    "(2) 次のターンで Rust の最新の安定版のバージョンを web_search で調べる。"
    "(3) 次のターンで Ruby の最新の安定版のバージョンを web_search で調べる。"
    "(4) 調べた 5 つを 1 行ずつ書いた versions.md を、"
    "スクラッチ作業領域に apply_patch で作る。"
    "(5) 作った versions.md を read で読み直して内容を確かめる。"
    "(6) 最後に 5 行でまとめて答える。"
)
FAILURE_PROMPT = "今日の天気を一言で教えてください。"
WEB_SEARCH_TOOL_ID = "web_search"
GENERIC_FAILURE_MESSAGE = "Action execution failed."
ACTION_SUCCESS_TIMEOUT_SECONDS = 900.0
ACTION_FAILURE_TIMEOUT_SECONDS = 240.0
BACKGROUND_JOB_TIMEOUT_SECONDS = 300.0
TERMINAL_ACTION_STATUSES = frozenset({"success", "error", "canceled"})
POLL_INTERVAL_SECONDS = 1.0
ACTIVITY_SUMMARY_JOB_TYPE = "summarize_activity"

# --- stop barrier scenario constants ---
BARRIER_ACTION_PROMPT = (
    "Python、Node.js、Go の最新の安定版のバージョンを、それぞれ web_search で "
    "1 回ずつ調べて、最後に 3 行でまとめてください。"
)
# Design 6.2: a control-socket operation answers Electron main within 5 seconds,
# barrier included.
CONTROL_RESPONSE_BUDGET_SECONDS = 5.0
# `owner_bound_sockets.OWNER_CHANGED_CLOSE_CODE`: Going Away, which Electron
# main reconnects after.
OWNER_CHANGED_CLOSE_CODE = 1001
CANCELED_TERMINAL_TIMEOUT_SECONDS = 240.0
TOOL_STEP_WAIT_SECONDS = 180.0
SOCKET_CLOSE_WAIT_SECONDS = 15.0
RUNNING_BACKGROUND_JOB_WAIT_SECONDS = 90.0
# `job_control.LOCAL_JOB_OPERATIONAL_RETRY_DELAY_SECONDS` is 5s, plus the run.
REQUEUED_JOB_WAIT_SECONDS = 120.0
RESTART_TERMINAL_TIMEOUT_SECONDS = 420.0
BARRIER_ACCOUNT_USER_ID = "live-barrier-account"
BARRIER_ACCOUNT_SESSION_VERSION = "1"


@dataclass
class ActionOutcome:
    action_id: str
    status: str
    final_output: str | None
    error: dict[str, Any] | None
    tool_labels: tuple[str, ...]


@dataclass
class LiveSession:
    """The renderer's live WebSocket session: it reads events and attaches runs."""

    socket: Any
    session_id: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    approved: set[str] = field(default_factory=set)
    # Set when the server closes the socket; the stop barrier does that on an
    # owner change and nothing else should.
    close_code: int | None = None
    close_reason: str | None = None

    async def collect(self) -> None:
        try:
            async for raw in self.socket:
                event = json.loads(raw)
                if event.get("event") == "session_started":
                    self.session_id = str(
                        (event.get("data") or {}).get("session_id") or ""
                    )
                self.events.append(event)
        except websockets.ConnectionClosed:
            pass
        finally:
            # A clean server close ends the iteration without raising, so the
            # code is read off the connection rather than off an exception.
            self.close_code = self.socket.close_code
            self.close_reason = self.socket.close_reason

    async def await_close(self, *, timeout_seconds: float) -> int | None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.close_code is not None:
                return self.close_code
            await asyncio.sleep(0.05)
        return None

    async def send(self, event: str, data: dict[str, Any]) -> None:
        await self.socket.send(json.dumps({"event": event, "data": data}))

    async def await_session_id(self, *, timeout_seconds: float = 10.0) -> str:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.session_id:
                return self.session_id
            await asyncio.sleep(0.05)
        raise TimeoutError("the WebSocket never sent session_started")

    def names(self) -> list[str]:
        return [str(event.get("event")) for event in self.events]

    def of(self, name: str) -> list[dict[str, Any]]:
        return [event for event in self.events if event.get("event") == name]


def _require_key(name: str) -> str:
    value = os.environ.get(name, "").strip().strip("'\"")
    if not value:
        raise SystemExit(f"{name} is not set")
    return value


def _redact(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    return text


def llm_connection_payload(api_key: str, *, model: str = MODEL) -> dict[str, Any]:
    return {"kind": "api_key", "provider": "openai", "model": model, "api_key": api_key}


def web_search_payload(api_key: str) -> dict[str, Any]:
    return {"provider": "tavily", "api_key": api_key}


def configure(
    helper: HelperHandle,
    *,
    llm_connection: dict[str, Any] | None,
    web_search_credential: dict[str, Any] | None,
) -> dict[str, Any]:
    """The payload Electron main sends after every helper start (design 6.2)."""
    return control_request(
        helper.socket_path,
        "configure",
        {
            "cloud_session": {"state": "absent"},
            "llm_connection": llm_connection,
            "web_search_credential": web_search_credential,
        },
    )


async def approve_pending_tools(
    client: httpx.AsyncClient, session: LiveSession, *, owner_id: str, action_id: str
) -> None:
    """Approve exactly what the renderer's approval panel would approve."""
    for event in session.of("process_paused"):
        data = event.get("data") or {}
        # ProcessPausedMessage carries every pending approval in approval_blockers.
        for blocker in data.get("approval_blockers") or []:
            approval_session_id = str(blocker["approval_session_id"])
            if approval_session_id in session.approved:
                continue
            session.approved.add(approval_session_id)
            response = await client.post(
                f"/v1/agents/users/{owner_id}/actions/{action_id}/approvals",
                json={
                    "decision": "approved_once",
                    "process_id": str(blocker["process_id"]),
                    "tool_request_id": str(blocker["tool_request_id"]),
                    "approval_session_id": approval_session_id,
                },
            )
            response.raise_for_status()


async def submit_action(
    client: httpx.AsyncClient, *, owner_id: str, prompt: str
) -> dict[str, Any]:
    response = await client.post(
        f"/v1/agents/users/{owner_id}/actions/messages",
        json={
            "target": {"kind": "new"},
            "message": {
                "version": 1,
                "message_id": str(uuid.uuid4()),
                "content": prompt,
                "images": [],
                "language": "ja",
            },
        },
    )
    response.raise_for_status()
    body: dict[str, Any] = response.json()
    return body


async def attach_action(session: LiveSession, submitted: dict[str, Any]) -> None:
    """Bind the submitted run to the live session.

    The HTTP submit only enqueues the run; the socket starts forwarding its
    events once the client asks for them, which is what the renderer's
    `resume_session` does after the POST returns a process id.
    """
    if submitted.get("disposition") != "started":
        return
    await session.send(
        "resume_session",
        {
            "session_id": await session.await_session_id(),
            "process_id": submitted["process_id"],
            "last_cursor": None,
            "last_chunk_index": -1,
            "kind": "action",
            "action_id": submitted["action_id"],
        },
    )


async def await_action_terminal(
    client: httpx.AsyncClient,
    session: LiveSession,
    *,
    owner_id: str,
    action_id: str,
    timeout_seconds: float,
) -> ActionOutcome:
    deadline = time.monotonic() + timeout_seconds
    page: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = await client.get(
            f"/v1/agents/users/{owner_id}/actions/{action_id}/state"
        )
        response.raise_for_status()
        page = response.json()
        if page["action"]["status"] in TERMINAL_ACTION_STATUSES:
            # The terminal event can trail the durable state by a relay tick.
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            return _read_outcome(page)
        await approve_pending_tools(
            client, session, owner_id=owner_id, action_id=action_id
        )
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
    raise TimeoutError(
        f"action {action_id} stayed {page.get('action', {}).get('status')!r} "
        f"for {timeout_seconds:.0f}s"
    )


def _read_outcome(page: dict[str, Any]) -> ActionOutcome:
    runs = page["runs"]
    latest = next(
        (run for run in runs if run["run_id"] == page["action"]["latest_run_id"]),
        runs[-1] if runs else {},
    )
    return ActionOutcome(
        action_id=page["action"]["action_id"],
        status=page["action"]["status"],
        final_output=latest.get("final_output"),
        error=latest.get("error"),
        tool_labels=tuple(
            str(entry.get("label"))
            for entry in latest.get("entries", ())
            if entry.get("step_kind") == "tool"
        ),
    )


async def run_action(
    client: httpx.AsyncClient,
    session: LiveSession,
    *,
    owner_id: str,
    prompt: str,
    timeout_seconds: float,
) -> ActionOutcome:
    submitted = await submit_action(client, owner_id=owner_id, prompt=prompt)
    # Nobody is at the keyboard to answer a consent prompt, so the run grants it
    # up front the way the Agent Overlay's own control does; the broker reads the
    # mode per tool request, before the first write preflight.
    approval = await client.put(
        f"/v1/agents/users/{owner_id}/actions/{submitted['action_id']}/approval-mode",
        json={"approval_mode": "always_allow"},
    )
    approval.raise_for_status()
    await attach_action(session, submitted)
    return await await_action_terminal(
        client,
        session,
        owner_id=owner_id,
        action_id=submitted["action_id"],
        timeout_seconds=timeout_seconds,
    )


@dataclass
class FailureObservation:
    """One failed Action seen from both sides of the public boundary."""

    outcome: ActionOutcome
    route: str
    # What a GUI can read: the conversation page's run error and the events the
    # live socket delivered for this Action.
    visible: str
    # What the runtime persisted for the same terminal.
    durable: str


def _visible_failure_surface(outcome: ActionOutcome, session: LiveSession) -> str:
    return json.dumps(
        {
            "run_error": outcome.error,
            "events": [
                event
                for event in session.events
                if (event.get("data") or {}).get("action_id") == outcome.action_id
            ],
        },
        ensure_ascii=False,
    )


def _durable_terminal_payload(helper: HelperHandle, action_id: str) -> str:
    rows = _query(
        helper,
        "SELECT payload FROM agent_process_events "
        "WHERE action_id = ? AND event_name = 'process_completed' ORDER BY sequence",
        (action_id,),
    )
    return json.dumps([row[0] for row in rows], ensure_ascii=False)


# --- checks -----------------------------------------------------------------


def check_helper_started(helper: HelperHandle) -> str:
    status = control_request(helper.socket_path, "status", {})
    assert status["configured"] is False, status
    assert status["cloud_session_state"] == "absent", status
    assert status["llm_route"] == "unconfigured", status
    assert status["web_search_route"] == "unconfigured", status
    assert status["active_owner_id"] == helper.owner_id, status
    return (
        f"owner={helper.owner_id} backend={helper.base_url} "
        f"cloud_session={status['cloud_session_state']}"
    )


def check_configure_direct(helper: HelperHandle, *, openai: str, tavily: str) -> str:
    response = configure(
        helper,
        llm_connection=llm_connection_payload(openai),
        web_search_credential=web_search_payload(tavily),
    )
    assert response["configured"] is True, response
    assert response["cloud_session_state"] == "absent", response
    assert response["llm_route"] == "direct", response
    assert response["web_search_route"] == "direct", response
    status = control_request(helper.socket_path, "status", {})
    assert status["active_owner_id"] == helper.owner_id, status
    assert status["credential_generation"] == 0, status
    return (
        f"llm_route={response['llm_route']} "
        f"web_search_route={response['web_search_route']} "
        f"owner={status['active_owner_id']} model={MODEL}"
    )


async def check_action_with_web_search(
    client: httpx.AsyncClient, session: LiveSession, *, owner_id: str
) -> tuple[str, ActionOutcome]:
    outcome = await run_action(
        client,
        session,
        owner_id=owner_id,
        prompt=ACTION_PROMPT,
        timeout_seconds=ACTION_SUCCESS_TIMEOUT_SECONDS,
    )
    assert outcome.status == "success", (outcome.status, outcome.error)
    assert outcome.final_output, outcome
    assert WEB_SEARCH_TOOL_ID in outcome.tool_labels, outcome.tool_labels
    names = session.names()
    for expected in ("process_started", "action_step", "process_completed"):
        assert expected in names, names
    detail = (
        f"action={outcome.action_id} tools={outcome.tool_labels} "
        f"final={outcome.final_output[:160]!r} ws_events={sorted(set(names))}"
    )
    return detail, outcome


async def check_history_page(
    client: httpx.AsyncClient, *, owner_id: str, outcome: ActionOutcome
) -> str:
    """The history page lists the Action, and reopening it replays the answer."""
    response = await client.get("/api/agent/history", params={"limit": 25})
    response.raise_for_status()
    page = response.json()
    item = next(
        (
            entry
            for entry in page["items"]
            if entry.get("kind") == "conversation"
            and entry.get("action_id") == outcome.action_id
        ),
        None,
    )
    assert item is not None, page
    # A finished Action is `idle`: nothing about it is waiting on the user.
    assert item["status"] == "idle", item
    state = await client.get(
        f"/v1/agents/users/{owner_id}/actions/{outcome.action_id}/state"
    )
    state.raise_for_status()
    replayed = _read_outcome(state.json())
    assert replayed.final_output == outcome.final_output, replayed.final_output
    return f"title={item['title'][:60]!r} status={item['status']} final_output_matches"


async def check_background_activity_summary(helper: HelperHandle) -> str:
    """A background job runs only because the stored connection can send.

    Nothing seeds this: the worker's periodic schedule enqueues the activity
    summaries at startup, and `claimable_specs` releases them only once
    `configure` has landed a connection the runtime could really send on.
    """
    deadline = time.monotonic() + BACKGROUND_JOB_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        jobs = _query(
            helper,
            "SELECT status, COUNT(*) FROM jobs WHERE job_type = ? GROUP BY status",
            (ACTIVITY_SUMMARY_JOB_TYPE,),
        )
        summaries = _query(
            helper,
            "SELECT summary_type, status, substr(summary, 1, 80) FROM "
            "activity_summaries WHERE user_id = ? AND status = 'success'",
            (helper.owner_id,),
        )
        if summaries and not any(status == "queued" for status, _ in jobs):
            statuses = dict(jobs)
            assert statuses.get("completed"), statuses
            assert "failed" not in statuses, statuses
            summary_type, _, head = summaries[0]
            return (
                f"jobs={statuses} summaries={len(summaries)} "
                f"first={summary_type} head={head!r}"
            )
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
    raise TimeoutError(
        f"no activity summary completed: jobs="
        f"{_query(helper, 'SELECT status, COUNT(*) FROM jobs WHERE job_type = ? GROUP BY status', (ACTIVITY_SUMMARY_JOB_TYPE,))}"
    )


def _query(helper: HelperHandle, sql: str, parameters: tuple[Any, ...]) -> list[Any]:
    with sqlite3.connect(helper.root / "runtime.sqlite3") as connection:
        return connection.execute(sql, parameters).fetchall()


async def observe_missing_connection(
    helper: HelperHandle,
    client: httpx.AsyncClient,
    session: LiveSession,
    *,
    owner_id: str,
) -> FailureObservation:
    response = control_request(helper.socket_path, "clear_llm_connection", {})
    assert response["llm_route"] == "unconfigured", response
    return await _observe_failed_action(
        helper, client, session, owner_id=owner_id, route=response["llm_route"]
    )


async def observe_invalid_key(
    helper: HelperHandle,
    client: httpx.AsyncClient,
    session: LiveSession,
    *,
    owner_id: str,
) -> FailureObservation:
    response = control_request(
        helper.socket_path,
        "set_llm_connection",
        llm_connection_payload("sk-invalid-live-check"),
    )
    assert response["llm_route"] == "direct", response
    return await _observe_failed_action(
        helper, client, session, owner_id=owner_id, route=response["llm_route"]
    )


async def _observe_failed_action(
    helper: HelperHandle,
    client: httpx.AsyncClient,
    session: LiveSession,
    *,
    owner_id: str,
    route: str,
) -> FailureObservation:
    outcome = await run_action(
        client,
        session,
        owner_id=owner_id,
        prompt=FAILURE_PROMPT,
        timeout_seconds=ACTION_FAILURE_TIMEOUT_SECONDS,
    )
    return FailureObservation(
        outcome=outcome,
        route=route,
        visible=_visible_failure_surface(outcome, session),
        durable=_durable_terminal_payload(helper, outcome.action_id),
    )


# --- stop barrier scenarios -------------------------------------------------


@asynccontextmanager
async def open_live_session(
    helper: HelperHandle, *, owner_id: str | None = None
) -> AsyncIterator[LiveSession]:
    """Connect a renderer-shaped WebSocket and read its events in the background."""
    async with websockets.connect(
        f"{helper.base_url.replace('http://', 'ws://')}"
        f"/v1/agents/users/{owner_id or helper.owner_id}/orchestrations",
        additional_headers={"Authorization": f"Bearer {helper.local_api_token}"},
    ) as socket:
        session = LiveSession(socket=socket)
        collector = asyncio.create_task(session.collect())
        try:
            yield session
        finally:
            collector.cancel()


def open_api_client(helper: HelperHandle) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=helper.base_url,
        headers={"Authorization": f"Bearer {helper.local_api_token}"},
        timeout=30.0,
    )


async def timed_control(
    helper: HelperHandle, operation: str, payload: dict[str, Any]
) -> tuple[dict[str, Any], float]:
    """Send one control request off the event loop, and time the whole answer."""
    started = time.monotonic()
    response = await asyncio.to_thread(
        control_request, helper.socket_path, operation, payload
    )
    return response, time.monotonic() - started


async def start_long_action(
    client: httpx.AsyncClient, session: LiveSession, *, owner_id: str
) -> str:
    """Start an Action long enough to still be running when the barrier lands."""
    submitted = await submit_action(
        client, owner_id=owner_id, prompt=BARRIER_ACTION_PROMPT
    )
    await attach_action(session, submitted)
    action_id = str(submitted["action_id"])
    await await_completed_tool_step(session, action_id=action_id)
    return action_id


async def await_completed_tool_step(
    session: LiveSession,
    *,
    action_id: str,
    timeout_seconds: float = TOOL_STEP_WAIT_SECONDS,
) -> dict[str, Any]:
    """Wait until the run has actually done something the barrier must stop."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        for event in session.of("action_step"):
            data = event.get("data") or {}
            if (
                data.get("action_id") == action_id
                and data.get("step_kind") == "tool"
                and data.get("status") not in (None, "processing")
            ):
                return data
        await asyncio.sleep(0.2)
    raise TimeoutError(f"action {action_id} ran no tool step within {timeout_seconds}s")


def read_action_status(helper: HelperHandle, *, action_id: str) -> str | None:
    """Read the durable status directly, for when the owner is no longer ours."""
    rows = _query(
        helper, "SELECT status FROM agent_actions WHERE action_id = ?", (action_id,)
    )
    return None if not rows else str(rows[0][0])


async def await_durable_action_terminal(
    helper: HelperHandle, *, action_id: str, timeout_seconds: float
) -> str:
    deadline = time.monotonic() + timeout_seconds
    status: str | None = None
    while time.monotonic() < deadline:
        status = read_action_status(helper, action_id=action_id)
        if status in TERMINAL_ACTION_STATUSES:
            return status
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
    raise TimeoutError(
        f"action {action_id} stayed {status!r} for {timeout_seconds:.0f}s"
    )


async def check_credential_clear_cancels_running_action(
    helper: HelperHandle,
    client: httpx.AsyncClient,
    session: LiveSession,
    *,
    owner_id: str,
    api_key: str,
    model: str,
) -> str:
    """Removing the credential stops the run before another provider send."""
    action_id = await start_long_action(client, session, owner_id=owner_id)
    response, elapsed = await timed_control(helper, "clear_llm_connection", {})
    try:
        assert elapsed <= CONTROL_RESPONSE_BUDGET_SECONDS, elapsed
        assert response["llm_route"] == "unconfigured", response
        outcome = await await_action_terminal(
            client,
            session,
            owner_id=owner_id,
            action_id=action_id,
            timeout_seconds=CANCELED_TERMINAL_TIMEOUT_SECONDS,
        )
    finally:
        await timed_control(
            helper, "set_llm_connection", llm_connection_payload(api_key, model=model)
        )
    assert outcome.status == "canceled", (outcome.status, outcome.error)
    # No owner change, so the socket this session holds must survive it.
    assert session.close_code is None, (session.close_code, session.close_reason)
    return (
        f"action={action_id} control={elapsed * 1000:.0f}ms "
        f"status={outcome.status} error={outcome.error} "
        f"ws_close={session.close_code} credential=cleared_then_restored"
    )


async def check_model_switch_keeps_running_action(
    helper: HelperHandle,
    client: httpx.AsyncClient,
    session: LiveSession,
    *,
    owner_id: str,
    api_key: str,
    to_model: str,
) -> str:
    """One Action continues when the next inference selects a new model."""
    action_id = await start_long_action(client, session, owner_id=owner_id)
    assert read_action_status(helper, action_id=action_id) == "processing"
    response, elapsed = await timed_control(
        helper, "set_llm_connection", llm_connection_payload(api_key, model=to_model)
    )
    assert elapsed <= CONTROL_RESPONSE_BUDGET_SECONDS, elapsed
    assert response["llm_route"] == "direct", response
    outcome = await await_action_terminal(
        client,
        session,
        owner_id=owner_id,
        action_id=action_id,
        timeout_seconds=ACTION_SUCCESS_TIMEOUT_SECONDS,
    )
    assert outcome.status == "success", (outcome.status, outcome.error)
    assert session.close_code is None, session.close_code
    return (
        f"action={action_id} control={elapsed * 1000:.0f}ms "
        f"status={outcome.status} model={to_model}"
    )


async def check_next_action_uses_the_new_connection(
    client: httpx.AsyncClient, session: LiveSession, *, owner_id: str
) -> str:
    """The connection that replaced the old one carries a whole Action."""
    outcome = await run_action(
        client,
        session,
        owner_id=owner_id,
        prompt=ACTION_PROMPT,
        timeout_seconds=ACTION_SUCCESS_TIMEOUT_SECONDS,
    )
    assert outcome.status == "success", (outcome.status, outcome.error)
    assert outcome.final_output, outcome
    return f"action={outcome.action_id} final={outcome.final_output[:100]!r}"


async def check_same_identity_stops_nothing(
    helper: HelperHandle,
    client: httpx.AsyncClient,
    session: LiveSession,
    *,
    owner_id: str,
    api_key: str,
    model: str,
) -> str:
    """Re-sending the connection it already has changes no identity, so no barrier."""
    action_id = await start_long_action(client, session, owner_id=owner_id)
    response, elapsed = await timed_control(
        helper, "set_llm_connection", llm_connection_payload(api_key, model=model)
    )
    assert elapsed <= CONTROL_RESPONSE_BUDGET_SECONDS, elapsed
    assert response["llm_route"] == "direct", response
    outcome = await await_action_terminal(
        client,
        session,
        owner_id=owner_id,
        action_id=action_id,
        timeout_seconds=ACTION_SUCCESS_TIMEOUT_SECONDS,
    )
    assert outcome.status == "success", (outcome.status, outcome.error)
    assert session.close_code is None, session.close_code
    return (
        f"action={action_id} control={elapsed * 1000:.0f}ms "
        f"status={outcome.status} final={(outcome.final_output or '')[:80]!r}"
    )


async def check_owner_switch_closes_sockets(
    helper: HelperHandle,
    client: httpx.AsyncClient,
    session: LiveSession,
    *,
    owner_id: str,
    report: Report,
) -> str:
    """A new owner closes the old owner's sockets and stops the old owner's run."""
    action_id = await start_long_action(client, session, owner_id=owner_id)
    expires_at = (
        (datetime.now(UTC) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    )
    response, elapsed = await timed_control(
        helper,
        "set_cloud_session",
        {
            "account_user_id": BARRIER_ACCOUNT_USER_ID,
            "access_token": "live-barrier-placeholder-token",
            "expires_at": expires_at,
            "session_version": BARRIER_ACCOUNT_SESSION_VERSION,
        },
    )
    try:
        assert elapsed <= CONTROL_RESPONSE_BUDGET_SECONDS, elapsed
        assert response["cloud_session_state"] == "present", response
        close_code = await session.await_close(
            timeout_seconds=SOCKET_CLOSE_WAIT_SECONDS
        )
        assert close_code == OWNER_CHANGED_CLOSE_CODE, (
            close_code,
            session.close_reason,
        )
        # The account owns the runtime now, so the old owner's Action is no
        # longer readable through the local API; its durable terminal still is.
        terminal = await await_durable_action_terminal(
            helper,
            action_id=action_id,
            timeout_seconds=CANCELED_TERMINAL_TIMEOUT_SECONDS,
        )
        assert terminal == "canceled", terminal
        status = await asyncio.to_thread(
            control_request, helper.socket_path, "status", {}
        )
        assert status["active_owner_id"] == BARRIER_ACCOUNT_USER_ID, status
        async with open_live_session(
            helper, owner_id=BARRIER_ACCOUNT_USER_ID
        ) as rebound:
            account_session_id = await rebound.await_session_id()
    finally:
        # Whatever happened above, the account must not keep the runtime: every
        # later scenario reads and writes as the logged-out owner.
        await _report_restored_logged_out_owner(
            helper,
            client,
            report,
            owner_id=owner_id,
            action_id=action_id,
            switched=response,
        )
    return (
        f"action={action_id} control={elapsed * 1000:.0f}ms "
        f"ws_close={close_code}/{session.close_reason!r} terminal={terminal} "
        f"account_owner={status['active_owner_id']} "
        f"rebound_session={bool(account_session_id)}"
    )


async def _report_restored_logged_out_owner(
    helper: HelperHandle,
    client: httpx.AsyncClient,
    report: Report,
    *,
    owner_id: str,
    action_id: str,
    switched: dict[str, Any],
) -> None:
    """Sign the account out again, and read the canceled Action back as its owner."""
    try:
        cleared, elapsed = await timed_control(
            helper,
            "clear_cloud_session",
            {
                "reason": "signed_out",
                "account_user_id": BARRIER_ACCOUNT_USER_ID,
                "session_version": BARRIER_ACCOUNT_SESSION_VERSION,
                "credential_generation": switched["credential_generation"],
                "helper_instance_id": switched["helper_instance_id"],
            },
        )
        assert cleared["stale"] is False, cleared
        assert cleared["cloud_session_state"] == "absent", cleared
        restored = await asyncio.to_thread(
            control_request, helper.socket_path, "status", {}
        )
        assert restored["active_owner_id"] == owner_id, restored
        page = await client.get(
            f"/v1/agents/users/{owner_id}/actions/{action_id}/state"
        )
        page.raise_for_status()
        replayed = _read_outcome(page.json())
        report.passed(
            "owner_switch_restores_logged_out_owner",
            f"clear={elapsed * 1000:.0f}ms owner={restored['active_owner_id']} "
            f"canceled_action={replayed.status} error={replayed.error}",
        )
    except Exception as error:
        report.failed("owner_switch_restores_logged_out_owner", error)


async def check_background_job_requeues_on_credential_clear(
    helper: HelperHandle, *, api_key: str, to_model: str
) -> str:
    """A background job mid-call goes back to `queued` instead of failing.

    The worker's periodic schedule enqueues the activity summaries at startup,
    so one of them is claimed as soon as `configure` opens admission; this waits
    for that claim and clears its LLM connection underneath it.
    """
    job_id = await _await_running_background_job(helper)
    response, elapsed = await timed_control(helper, "clear_llm_connection", {})
    try:
        assert elapsed <= CONTROL_RESPONSE_BUDGET_SECONDS, elapsed
        assert response["llm_route"] == "unconfigured", response
        requeued = await _await_job_status(
            helper, job_id=job_id, statuses={"queued"}, timeout_seconds=30.0
        )
    finally:
        await timed_control(
            helper,
            "set_llm_connection",
            llm_connection_payload(api_key, model=to_model),
        )
    assert requeued == "queued", requeued
    completed = await _await_job_status(
        helper,
        job_id=job_id,
        statuses={"completed", "failed"},
        timeout_seconds=REQUEUED_JOB_WAIT_SECONDS,
    )
    assert completed == "completed", completed
    return (
        f"job={job_id} control={elapsed * 1000:.0f}ms requeued={requeued} "
        f"then={completed} model={to_model}"
    )


async def _await_running_background_job(helper: HelperHandle) -> str:
    deadline = time.monotonic() + RUNNING_BACKGROUND_JOB_WAIT_SECONDS
    while time.monotonic() < deadline:
        rows = _query(
            helper,
            "SELECT job_id FROM jobs WHERE job_type = ? AND status = 'running'",
            (ACTIVITY_SUMMARY_JOB_TYPE,),
        )
        if rows:
            return str(rows[0][0])
        await asyncio.sleep(0.05)
    raise TimeoutError("no activity summary job was claimed in time")


async def _await_job_status(
    helper: HelperHandle, *, job_id: str, statuses: set[str], timeout_seconds: float
) -> str:
    deadline = time.monotonic() + timeout_seconds
    status: str | None = None
    while time.monotonic() < deadline:
        rows = _query(helper, "SELECT status FROM jobs WHERE job_id = ?", (job_id,))
        status = None if not rows else str(rows[0][0])
        if status in statuses:
            return status
        await asyncio.sleep(0.05)
    raise TimeoutError(f"job {job_id} stayed {status!r}, never reached {statuses}")


async def run_restart_scenario(
    helper: HelperHandle, report: Report, *, openai: str, tavily: str
) -> HelperHandle:
    """Kill the helper mid-Action, bring it back, and make sure `configure` waits.

    An Action left `processing` by the previous process is waiting to resume --
    startup recovery re-queues its job and leaves the row alone -- so the first
    `configure` of the new process must not read it as in-flight work to cancel.
    """
    async with open_live_session(helper) as session, open_api_client(helper) as client:
        action_id = await start_long_action(client, session, owner_id=helper.owner_id)
    helper.process.kill()
    helper.process.wait(timeout=30)
    restarted = start_helper(
        root=helper.root,
        agents_root=AGENTS_ROOT,
        log_path=helper.root / "helper-restart.log",
    )
    assert restarted.owner_id == helper.owner_id, restarted.owner_id
    interrupted = read_action_status(restarted, action_id=action_id)
    response, elapsed = await timed_control(
        restarted,
        "configure",
        {
            "cloud_session": {"state": "absent"},
            "llm_connection": llm_connection_payload(openai),
            "web_search_credential": web_search_payload(tavily),
        },
    )
    assert elapsed <= CONTROL_RESPONSE_BUDGET_SECONDS, elapsed
    assert response["llm_route"] == "direct", response
    after_configure = read_action_status(restarted, action_id=action_id)
    if after_configure == "canceled":
        report.failed(
            "restart_does_not_cancel",
            AssertionError(
                f"the first configure after a restart canceled the resuming Action: "
                f"action={action_id} before={interrupted!r}"
            ),
        )
    else:
        report.passed(
            "restart_does_not_cancel",
            f"action={action_id} before_configure={interrupted!r} "
            f"after_configure={after_configure!r} control={elapsed * 1000:.0f}ms",
        )
    async with (
        open_live_session(restarted) as session,
        open_api_client(restarted) as client,
    ):
        await report.run(
            "restart_resumes_to_terminal",
            _report_restart_terminal(restarted, client, session, action_id=action_id),
        )
    return restarted


async def _report_restart_terminal(
    helper: HelperHandle,
    client: httpx.AsyncClient,
    session: LiveSession,
    *,
    action_id: str,
) -> str:
    outcome = await await_action_terminal(
        client,
        session,
        owner_id=helper.owner_id,
        action_id=action_id,
        timeout_seconds=RESTART_TERMINAL_TIMEOUT_SECONDS,
    )
    assert outcome.status != "canceled", (outcome.status, outcome.error)
    return (
        f"action={action_id} status={outcome.status} "
        f"final={(outcome.final_output or '')[:100]!r} error={outcome.error}"
    )


async def run_barrier_checks(
    helper: HelperHandle, report: Report, *, openai: str, tavily: str
) -> HelperHandle:
    report.passed("helper_started", check_helper_started(helper))
    report.passed(
        "configure_direct", check_configure_direct(helper, openai=openai, tavily=tavily)
    )
    async with open_live_session(helper) as session, open_api_client(helper) as client:
        owner_id = helper.owner_id
        # First, while the startup activity summaries are still being claimed.
        await report.run(
            "background_job_requeues_on_credential_clear",
            check_background_job_requeues_on_credential_clear(
                helper, api_key=openai, to_model=ALTERNATE_MODEL
            ),
        )
        await report.run(
            "model_switch_keeps_running_action",
            check_model_switch_keeps_running_action(
                helper,
                client,
                session,
                owner_id=owner_id,
                api_key=openai,
                to_model=MODEL,
            ),
        )
        await report.run(
            "next_action_uses_the_new_connection",
            check_next_action_uses_the_new_connection(
                client, session, owner_id=owner_id
            ),
        )
        await report.run(
            "same_identity_stops_nothing",
            check_same_identity_stops_nothing(
                helper, client, session, owner_id=owner_id, api_key=openai, model=MODEL
            ),
        )
        await report.run(
            "credential_clear_cancels_running_action",
            check_credential_clear_cancels_running_action(
                helper,
                client,
                session,
                owner_id=owner_id,
                api_key=openai,
                model=MODEL,
            ),
        )
        await report.run(
            "owner_switch_closes_sockets",
            check_owner_switch_closes_sockets(
                helper, client, session, owner_id=owner_id, report=report
            ),
        )
    return await run_restart_scenario(helper, report, openai=openai, tavily=tavily)


# --- driver -----------------------------------------------------------------


class Report:
    def __init__(self, secrets: tuple[str, ...]) -> None:
        self.secrets = secrets
        self.failures = 0

    def passed(self, name: str, detail: str) -> None:
        print(f"PASS {name}: {_redact(detail, self.secrets)}", flush=True)

    def failed(self, name: str, error: BaseException) -> None:
        self.failures += 1
        text = "".join(traceback.format_exception(error))
        print(f"FAIL {name}: {_redact(text, self.secrets)[-3000:]}", flush=True)

    async def run(self, name: str, check: Any) -> None:
        try:
            self.passed(name, await check)
        except Exception as error:
            self.failed(name, error)


async def run_checks(
    helper: HelperHandle, report: Report, *, openai: str, tavily: str
) -> None:
    report.passed("helper_started", check_helper_started(helper))
    report.passed(
        "configure_direct", check_configure_direct(helper, openai=openai, tavily=tavily)
    )
    async with open_live_session(helper) as session, open_api_client(helper) as client:
        await _run_api_checks(helper, client, session, report)


async def _run_api_checks(
    helper: HelperHandle,
    client: httpx.AsyncClient,
    session: LiveSession,
    report: Report,
) -> None:
    owner_id = helper.owner_id
    succeeded: ActionOutcome | None = None
    try:
        detail, succeeded = await check_action_with_web_search(
            client, session, owner_id=owner_id
        )
        report.passed("action_with_web_search", detail)
    except Exception as error:
        report.failed("action_with_web_search", error)

    if succeeded is None:
        report.failed(
            "history_page", AssertionError("no successful action to read back")
        )
    else:
        await report.run(
            "history_page",
            check_history_page(client, owner_id=owner_id, outcome=succeeded),
        )

    await report.run(
        "background_activity_summary", check_background_activity_summary(helper)
    )
    await _report_failure_route(
        report,
        "missing_connection",
        observe_missing_connection(helper, client, session, owner_id=owner_id),
        expected_failure_code="ACTION_CONNECTION_NOT_CONFIGURED",
    )
    await _report_failure_route(
        report,
        "invalid_key",
        observe_invalid_key(helper, client, session, owner_id=owner_id),
        expected_failure_code="ACTION_CONNECTION_REJECTED",
    )


async def _report_failure_route(
    report: Report, name: str, observation: Any, *, expected_failure_code: str
) -> None:
    """Report the two things a refused route owes the user, separately.

    Reaching a visible terminal is one promise; saying what the user must fix is
    the other, and they fail for different reasons.
    """
    try:
        observed: FailureObservation = await observation
    except Exception as error:
        report.failed(f"{name}_reaches_terminal_error", error)
        report.failed(f"{name}_failure_is_actionable", error)
        return
    outcome = observed.outcome
    if outcome.status == "error":
        report.passed(
            f"{name}_reaches_terminal_error",
            f"action={outcome.action_id} route={observed.route} "
            f"status={outcome.status} error={outcome.error}",
        )
    else:
        report.failed(
            f"{name}_reaches_terminal_error",
            AssertionError(f"action ended {outcome.status!r}: {outcome.error}"),
        )
    # The public terminal carries an Action failure code and one sentence; the
    # proxy code and suggested action stay in the durable diagnostic payload.
    error = outcome.error or {}
    if error.get("code") == expected_failure_code and error.get("message") not in (
        None,
        GENERIC_FAILURE_MESSAGE,
    ):
        report.passed(
            f"{name}_failure_is_actionable",
            f"action={outcome.action_id} code={error['code']} "
            f"message={error['message']!r}",
        )
        return
    report.failed(
        f"{name}_failure_is_actionable",
        AssertionError(
            f"expected a {expected_failure_code} terminal naming the remedy; "
            f"run_error={outcome.error}; "
            f"durable_terminal={observed.durable[:900]}"
        ),
    )


async def main() -> int:
    barrier = "--barrier" in sys.argv[1:]
    openai_key = _require_key("OPENAI_API_KEY")
    tavily_key = _require_key("TAVILY_API_KEY")
    report = Report((openai_key, tavily_key))
    root = Path(tempfile.mkdtemp(prefix="pnt-live-", dir=RUNTIME_ROOT_PARENT))
    helper: HelperHandle | None = None
    try:
        helper = start_helper(
            root=root, agents_root=AGENTS_ROOT, log_path=root / "helper.log"
        )
        if barrier:
            # The restart scenario replaces the process this has to stop.
            helper = await run_barrier_checks(
                helper, report, openai=openai_key, tavily=tavily_key
            )
        else:
            await run_checks(helper, report, openai=openai_key, tavily=tavily_key)
    except Exception as error:
        report.failed("harness", error)
    finally:
        if helper is not None:
            terminate_helper(helper.process)
    if report.failures:
        print(f"failures={report.failures} runtime_root={root}", flush=True)
        return 1
    shutil.rmtree(root, ignore_errors=True)
    print("failures=0", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
