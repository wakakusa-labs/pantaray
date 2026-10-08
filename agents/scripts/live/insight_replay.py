"""Compare the short Insight before and after a change, on the real OpenAI API.

One invocation checks the ``--before`` revision out into a temporary worktree,
runs ``InsightAgent.generate`` from it and from this tree over the same fixed
Zanei timeline, served from memory, prints the two side by side and removes the
worktree. The default timeline is synthetic and holds no private data;
``--fixture`` takes a JSON file of the same shape (``pages``: page responses,
``evidence``: ``"<event id>:<field>"`` to text). The memory tools search an
empty, freshly migrated database. The model defaults to the one the insight
purpose runs on in production.

Reads the key from OPENAI_API_KEY and never prints it. Each run writes
``run-<n>.json`` (the outputs, request count, tool calls and token usage) under
``--out``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from uuid import UUID

from pantaray_agents.agents.insight_agent.agent import InsightAgent
from pantaray_agents.local_runtime.context.source_gate import ActiveSource
from pantaray_agents.local_runtime.context.source_protocol import (
    EvidenceReadRequest,
    EvidenceResponse,
    PageReadRequest,
    PageResponse,
)
from pantaray_agents.local_runtime.llm_proxy.client import LocalLlmProxyClient
from pantaray_agents.local_runtime.runtime.connection_store import (
    ApiKeyConnection,
    ChatGptConnection,
    ChatGptCredential,
    set_llm_connection,
)
from pantaray_agents.local_runtime.runtime.identity import register_logged_out_owner
from pantaray_agents.local_runtime.runtime.session_store import mark_configured
from pantaray_agents.local_runtime.storage.migrations import (
    apply_migrations,
    load_default_migrations,
)
from pantaray_agents.schema.context_source import SourceBinding
from pantaray_agents.tools.zanei import ZaneiTools
from pantaray_agents.utils.trace_context import TraceContextManager
from pantaray_llm.profiles import OPENAI_GPT_6_LUNA_MODEL

AGENTS_DIR = Path(__file__).resolve().parents[2]

OWNER = "insight-replay-owner"
STORE_ID = "replay-store"
_ABSENT = {"kind": "absent"}

# A synthetic hour of work: (app, window title, text, url).
_SESSION = (
    (
        "Code",
        "report.md — quarterly-review",
        "## 第3四半期レビュー\n売上は前年比 12% 増。解約率の説明が未記入。",
        None,
    ),
    (
        "Slack",
        "#proj-alpha",
        "田中: レビュー資料は金曜 17 時までに共有をお願いします\n自分: 了解です、解約率の節を足して出します",
        None,
    ),
    (
        "Terminal",
        "pytest — billing",
        "FAILED tests/test_invoice.py::test_rounding - assert 1000 == 999\n2 failed, 48 passed",
        None,
    ),
    (
        "Chrome",
        "decimal — Decimal fixed point — Python docs",
        "quantize() rounds a number to a fixed exponent.",
        "https://docs.python.org/3/library/decimal.html",
    ),
    (
        "Code",
        "invoice.py — billing",
        "total = amount.quantize(Decimal('1'), rounding=ROUND_HALF_UP)",
        None,
    ),
    ("Terminal", "pytest — billing", "50 passed in 3.2s", None),
    (
        "Code",
        "report.md — quarterly-review",
        "### 解約率\n解約率は 2.1%（前期 2.6%）。オンボーディング改善の効果と見られるが未検証。",
        None,
    ),
    (
        "Mail",
        "Re: 請求書の端数について — 佐藤様",
        "佐藤様\n端数処理を四捨五入に統一しました。来月分から反映されます。",
        None,
    ),
)


def _default_fixture() -> dict[str, Any]:
    observations, evidence = [], {}
    for sequence, (app, title, text, url) in enumerate(_SESSION, start=1):
        event_id = f"replay-event-{sequence}"
        observations.append(
            {
                "append_sequence": sequence,
                "id": event_id,
                "ts": f"2026-10-08T01:{sequence * 6:02d}:00Z",
                "source": {"kind": "value", "text": "macos.accessibility"},
                "event_type": {"kind": "value", "text": "content.snapshot"},
                "bundle_id": _ABSENT,
                "app_name": {"kind": "value", "text": app},
                "pid": None,
                "window_title": {"kind": "value", "text": title},
                "window_id": None,
            }
        )
        evidence[f"{event_id}:text"] = text
        if url:
            evidence[f"{event_id}:url"] = url
    half = len(observations) // 2
    pages = [
        _page(observations[:half], cursor="replay-1", has_more=True),
        _page(observations[half:], cursor="replay-2", has_more=False),
    ]
    return {"pages": pages, "evidence": evidence}


def _page(observations: list[Any], *, cursor: str, has_more: bool) -> dict[str, Any]:
    return {
        "protocol_version": 1,
        "kind": "page",
        "store_identity": STORE_ID,
        "observations": observations,
        "next_cursor": cursor,
        "upper_bound": "replay-upper",
        "has_more": has_more,
        "coverage": {
            "after": observations[0]["append_sequence"] - 1,
            "through": observations[-1]["append_sequence"],
        },
    }


class _Reader:
    """Serves the fixture's pages in order and its evidence by event and field."""

    def __init__(self, fixture: dict[str, Any]) -> None:
        self.pages = [
            PageResponse.model_validate_json(json.dumps(page))
            for page in fixture["pages"]
        ]
        self.evidence: dict[str, str] = fixture["evidence"]

    async def read_page(self, _source: object, _request: PageReadRequest) -> object:
        return self.pages.pop(0)

    async def read_evidence(
        self, _source: object, request: EvidenceReadRequest
    ) -> object:
        origin = request.origin
        text = self.evidence.get(f"{origin.event_id}:{origin.field}")
        data = (text or "").encode("utf-8")[request.start :]
        chunk = data.decode("utf-8", errors="ignore")
        total = request.start + len(data)
        content: dict[str, Any] = (
            {"kind": "absent"}
            if text is None
            else {
                "kind": "text",
                "text": chunk,
                "start": request.start,
                "end": total,
                "total_bytes": total,
                "remaining": None,
            }
        )
        return EvidenceResponse.model_validate(
            {
                "protocol_version": 1,
                "kind": "evidence",
                "origin": origin,
                "content": content,
                "metadata": {
                    "event_type": "content.snapshot",
                    "payload_without_text": {},
                    "redaction_applied": False,
                    "truncated": False,
                },
            }
        )


class _CountingModels:
    def __init__(self, models: Any) -> None:
        self.models = models
        self.requests = 0
        self.calls = 0
        self.usage: dict[str, int] = {}
        # (input tokens, of which read from the prompt cache) per request.
        self.per_request: list[tuple[int, int]] = []
        self.last_turn = False
        self.extra_last_turn = False

    async def generate_content(self, **kwargs: Any) -> Any:
        self.requests += 1
        # What the request said, to see whether a run reached its last turn
        # (either loop's wording) and whether it took the extra one.
        sent = str(kwargs["contents"]) + kwargs["config"].tool_use.model_dump_json()
        self.last_turn |= any(
            mark in sent for mark in ("This is the last turn", "tool budget is spent")
        )
        self.extra_last_turn |= "came on the last turn" in sent
        response = await self.models.generate_content(**kwargs)
        turn = getattr(response, "action_turn", None)
        calls = turn.calls if turn is not None else response.tool_calls
        self.calls += len(calls)
        usage = response.usage_metadata or {}
        for key, value in usage.items():
            if value is not None:
                self.usage[key] = self.usage.get(key, 0) + value
        self.per_request.append(
            (usage.get("prompt_tokens") or 0, usage.get("cached_prompt_tokens") or 0)
        )
        return response


async def _run(fixture: dict[str, Any], db_path: Path, index: int) -> dict[str, Any]:
    client = LocalLlmProxyClient(proxy_url="https://unused.invalid/v1/llm/proxy")
    counting = _CountingModels(client.aio.models)
    client.aio.models = counting  # type: ignore[assignment]
    reader = _Reader(fixture)
    first = reader.pages.pop(0)
    zanei = ZaneiTools(
        reader=reader,  # type: ignore[arg-type]
        source=ActiveSource(
            binding=SourceBinding(
                user_id=OWNER,
                epoch=UUID(int=1),
                policy_revision="replay",
                store_id=STORE_ID,
                protocol_version=1,
            )
        ),
        cursor=None,
        upper_bound=first.upper_bound,
        first_page=first,
    )
    started = time.monotonic()
    with TraceContextManager(user_id=OWNER, local_job_id=f"insight-replay-{index}"):
        output = await InsightAgent(client=client, llm_config={}).generate(
            run_id=f"insight-replay-{index}",
            user_id=OWNER,
            zanei=zanei,
            workspace_context=fixture.get("workspace_context", "(none)"),
            previous_insight=fixture.get("previous_insight", "(none)"),
            db_path=db_path,
            busy_timeout_ms=5_000,
        )
    return {
        "output": output.model_dump(mode="json"),
        "requests": counting.requests,
        "tool_calls": counting.calls,
        "usage": counting.usage,
        "per_request": counting.per_request,
        "events_read": zanei.events_read,
        "range_drained": not zanei.has_more,
        "last_turn_reached": counting.last_turn,
        "extra_last_turn": counting.extra_last_turn,
        "seconds": round(time.monotonic() - started, 1),
    }


def _compare(before: Path, after: Path) -> int:
    runs = [sorted(path.name for path in d.glob("run-*.json")) for d in (before, after)]
    if not runs[0] or runs[0] != runs[1]:
        print(f"The two directories do not hold the same runs: {runs}")
        return 1
    for label, directory in (("before", before), ("after", after)):
        for path in sorted(directory.glob("run-*.json")):
            run = json.loads(path.read_text("utf-8"))
            print(
                f"== {label} {path.stem}: requests={run['requests']} "
                f"tool_calls={run['tool_calls']} drained={run['range_drained']} "
                f"last_turn={run['last_turn_reached']} "
                f"extra_last_turn={run['extra_last_turn']} "
                f"records={len(run['output']['records'])} usage={run['usage']}"
            )
            print(f"-- activity\n{run['output']['activity']}")
            print(f"-- insight\n{run['output']['insight']}")
            print(
                f"-- reconsideration_reason: {run['output']['reconsideration_reason']}"
            )
    return 0


def _git(*args: str, cwd: Path = AGENTS_DIR) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _replay_both(args: argparse.Namespace) -> int:
    """Replay the before revision and this tree, each in its own interpreter."""

    before_rev = args.before or _git("merge-base", "HEAD", "origin/develop")
    # Worktrees are added from the main checkout, whichever one this runs in.
    main = Path(_git("rev-parse", "--path-format=absolute", "--git-common-dir")).parent
    args.out.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="insight-before-") as raw_tree:
        tree = Path(raw_tree) / "tree"
        _git("worktree", "add", "--detach", str(tree), before_rev, cwd=main)
        try:
            for side, agents in (("before", tree / "agents"), ("after", AGENTS_DIR)):
                print(f"== replaying {side} ({agents})")
                command = [sys.executable, __file__, "--side", side]
                command += ["--out", str(args.out), "--runs", str(args.runs)]
                command += ["--model", args.model, "--route", args.route]
                if args.fixture:
                    command += ["--fixture", str(args.fixture.resolve())]
                source = f"{agents}/src:{agents}/packages/pantaray-llm/src"
                env = {**os.environ, "PYTHONPATH": source}
                if subprocess.run(command, env=env, check=False).returncode:
                    return 1
        finally:
            _git("worktree", "remove", "--force", str(tree), cwd=main)
    print(f"before={before_rev} model={args.model} results={args.out}")
    return _compare(args.out / "before", args.out / "after")


def _codex_connection(model: str) -> ChatGptConnection:
    """The ChatGPT sign-in the Codex CLI keeps, read in-process and never shown.

    The production app sends the same account's token to the same backend.
    """

    tokens = json.loads((Path.home() / ".codex" / "auth.json").read_text("utf-8"))[
        "tokens"
    ]
    return ChatGptConnection(
        model=model,
        credential=ChatGptCredential(
            access_token=tokens["access_token"],
            # Not checked on this path; the backend refuses an expired token.
            expires_at="2099-01-01T00:00:00Z",
            account_id=tokens["account_id"],
        ),
    )


async def _replay_side(args: argparse.Namespace, key: str) -> int:
    fixture = (
        json.loads(args.fixture.read_text("utf-8"))
        if args.fixture
        else _default_fixture()
    )
    register_logged_out_owner(OWNER)
    mark_configured()
    connection = (
        _codex_connection(args.model)
        if args.route == "codex"
        else ApiKeyConnection(provider="openai", model=args.model, api_key=key)
    )
    secret = (
        connection.credential.access_token
        if isinstance(connection, ChatGptConnection)
        else connection.api_key
    )
    set_llm_connection(connection)
    out = args.out / args.side
    out.mkdir(parents=True, exist_ok=False)
    failures = 0
    with tempfile.TemporaryDirectory() as raw_tmp:
        db_path = Path(raw_tmp) / "runtime.sqlite3"
        apply_migrations(db_path, 5_000, load_default_migrations())
        for index in range(1, args.runs + 1):
            try:
                run = await _run(fixture, db_path, index)
            except Exception as error:  # replay harness: report every failure kind
                failures += 1
                print(f"FAIL run-{index}: {str(error).replace(secret, '<redacted>')}")
                continue
            (out / f"run-{index}.json").write_text(
                json.dumps(run, ensure_ascii=False, indent=2), "utf-8"
            )
            print(f"PASS run-{index}: requests={run['requests']} usage={run['usage']}")
    if failures:
        # Half a replay cannot be compared run for run.
        for path in out.glob("run-*.json"):
            path.unlink()
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--before", help="revision to compare against")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--model", default=OPENAI_GPT_6_LUNA_MODEL)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(tempfile.gettempdir()) / f"insight-replay-{int(time.time())}",
    )
    parser.add_argument(
        "--route",
        choices=("openai", "codex"),
        default="openai",
        help="openai: OPENAI_API_KEY; codex: the ChatGPT sign-in in ~/.codex/auth.json",
    )
    parser.add_argument("--side", choices=("before", "after"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    key = os.environ.get("OPENAI_API_KEY", "").strip().strip("'\"")
    if args.route == "openai" and not key:
        print("Set OPENAI_API_KEY")
        return 2
    if args.side:
        return asyncio.run(_replay_side(args, key))
    return _replay_both(args)


if __name__ == "__main__":
    sys.exit(main())
