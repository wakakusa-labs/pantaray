"""Replay one short Insight run against the real OpenAI API, for before/after.

Runs ``InsightAgent.generate`` from whatever tree ``PYTHONPATH`` points at over
a fixed Zanei timeline served from memory, so two trees can be compared on the
same input with the production model. The default timeline is synthetic and
holds no private data; ``--fixture`` takes a JSON file of the same shape
(``pages``: page responses, ``evidence``: ``"<event id>:<field>"`` to text).
The memory tools search an empty, freshly migrated database.

Reads the key from OPENAI_API_KEY and never prints it. Each run writes
``run-<n>.json`` (the outputs, request count, tool calls and token usage) to
``--out``; ``--compare BEFORE AFTER`` prints the two directories side by side.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
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

OWNER = "insight-replay-owner"
MODEL = os.environ.get("SMOKE_MODEL", "gpt-6-luna")
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

    async def generate_content(self, **kwargs: Any) -> Any:
        self.requests += 1
        response = await self.models.generate_content(**kwargs)
        turn = getattr(response, "action_turn", None)
        calls = turn.calls if turn is not None else response.tool_calls
        self.calls += len(calls)
        for key, value in (response.usage_metadata or {}).items():
            self.usage[key] = self.usage.get(key, 0) + value
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
        "events_read": zanei.events_read,
        "range_drained": not zanei.has_more,
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
                f"records={len(run['output']['records'])} usage={run['usage']}"
            )
            print(f"-- activity\n{run['output']['activity']}")
            print(f"-- insight\n{run['output']['insight']}")
            print(
                f"-- reconsideration_reason: {run['output']['reconsideration_reason']}"
            )
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--compare", nargs=2, type=Path)
    args = parser.parse_args()
    if args.compare:
        return _compare(*args.compare)
    key = os.environ.get("OPENAI_API_KEY", "").strip().strip("'\"")
    if not key or args.out is None:
        print("Set OPENAI_API_KEY and pass --out DIR")
        return 2
    fixture = (
        json.loads(args.fixture.read_text("utf-8"))
        if args.fixture
        else _default_fixture()
    )
    register_logged_out_owner(OWNER)
    mark_configured()
    set_llm_connection(ApiKeyConnection(provider="openai", model=MODEL, api_key=key))
    # A fresh directory per replay, so no older result stands in for a run.
    args.out.mkdir(parents=True, exist_ok=False)
    failures = 0
    with tempfile.TemporaryDirectory() as raw_tmp:
        db_path = Path(raw_tmp) / "runtime.sqlite3"
        apply_migrations(db_path, 5_000, load_default_migrations())
        for index in range(1, args.runs + 1):
            try:
                run = await _run(fixture, db_path, index)
            except Exception as error:  # replay harness: report every failure kind
                failures += 1
                print(f"FAIL run-{index}: {str(error).replace(key, '<redacted>')}")
                continue
            (args.out / f"run-{index}.json").write_text(
                json.dumps(run, ensure_ascii=False, indent=2), "utf-8"
            )
            print(f"PASS run-{index}: requests={run['requests']} usage={run['usage']}")
    if failures:
        # Half a replay cannot be compared run for run.
        for path in args.out.glob("run-*.json"):
            path.unlink()
    print(f"model={MODEL} failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
