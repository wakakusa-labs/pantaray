"""Replay Suggestion decisions on a copy of a local database, for before/after checks.

`run` decides a Suggestion for each chosen Insight of the copy the way the job
does -- the same prompt, lenses, research tools, selector and writer -- over the
direct route with the OpenAI key in OPENAI_API_KEY, or with `--provider
chatgpt` on the ChatGPT route the app uses, signed in with the Codex CLI's
token (and TAVILY_API_KEY for web search when set), and writes one JSON line per Insight. Nothing is published:
the run's steps land in the copy only. `compare` pairs two such files by
Insight. `suggestion_replay.sh` runs both sides on two git refs and compares.

The copy and the output hold private data: keep both outside the repository.
The key is read from the environment and never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import random
import sqlite3
import sys
import time
import traceback
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from pantaray_llm.profiles import OPENAI_GPT_6_LUNA_MODEL

# The model Pantaray Cloud serves the regular purposes, `suggestion` included, with.
MODEL = os.environ.get("SMOKE_MODEL", OPENAI_GPT_6_LUNA_MODEL)
BUSY_TIMEOUT_MS = 5000
# The live store; a replay writes steps and rows, so it only runs on a copy.
LIVE_STORE_MARKER = "Application Support"
UNUSED_PROXY_URL = "https://unused.invalid/v1/llm/proxy"


def _configure_env(*, db: Path, artifact_root: Path, log_dir: Path) -> None:
    os.environ.update(
        {
            # What the app's settings module requires; this process serves nothing.
            "NODE_ENV": "development",
            "LOG_LEVEL": "INFO",
            "ALLOWED_ORIGINS": "http://localhost",
            "ALLOWED_HOSTS": "localhost",
            "USE_MOCKS": "false",
            "LOCAL_DB_PATH": str(db),
            "LOCAL_DB_BUSY_TIMEOUT_MS": str(BUSY_TIMEOUT_MS),
            "LOCAL_ARTIFACT_ROOT": str(artifact_root),
            "LOG_FILE_PATH": str(log_dir / "replay.log"),
        }
    )
    os.environ.setdefault("LLM_PROXY_URL", UNUSED_PROXY_URL)


def _insights(db: Path, ids: list[str], latest: int) -> list[tuple[str, str]]:
    """(user_id, insight_id) pairs: the named ones, else the latest non-empty."""
    with sqlite3.connect(db) as connection:
        if ids:
            marks = ",".join("?" * len(ids))
            rows = connection.execute(
                f"SELECT user_id, insight_id FROM agent_insights "
                f"WHERE insight_id IN ({marks})",
                ids,
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT user_id, insight_id FROM agent_insights "
                "WHERE short_term_insight_data IS NOT NULL "
                "AND short_term_insight_data != '' "
                "ORDER BY created_at DESC LIMIT ?",
                (latest,),
            ).fetchall()
    return [(str(user_id), str(insight_id)) for user_id, insight_id in rows]


def _step_summary(db: Path, suggestion_id: str) -> dict[str, object]:
    # Lens run i records its steps from i * stride; the tree under test sets both.
    from pantaray_agents.agents.suggestion_agent.lenses import (
        LENS_STEP_STRIDE,
        LENSES_PER_SUGGESTION,
    )

    with sqlite3.connect(db) as connection:
        rows = connection.execute(
            "SELECT step_number, step_kind, status, tool_name, llm_response_text "
            "FROM agent_suggestion_run_steps WHERE suggestion_id = ? "
            "ORDER BY step_number",
            (suggestion_id,),
        ).fetchall()
    runs = [
        {"turns": 0, "failed_sends": 0, "tool_calls": 0, "tools": Counter()}
        for _ in range(LENSES_PER_SUGGESTION)
    ]
    selector = None
    for number, kind, status, tool, response in rows:
        index = (number - 1) // LENS_STEP_STRIDE
        if index >= LENSES_PER_SUGGESTION:  # the selector's step, then the writer's
            if response and response.startswith("choice="):
                selector = response
            continue
        run = runs[index]
        if kind == "tool":
            run["tool_calls"] += 1
            run["tools"][tool] += 1
        elif status == "error":
            run["failed_sends"] += 1
        else:
            run["turns"] += 1
    return {
        "lens_runs": [{**run, "tools": dict(run["tools"])} for run in runs],
        "selector": selector,
    }


USAGE_FIELDS = (
    "prompt_tokens",
    "cached_prompt_tokens",
    "completion_tokens",
    "reasoning_tokens",
)


class _UsageRecorder:
    """Usage and wall time per call site: each lens run, the selector, the writer.

    It wraps the agent's model client, so it reads what the provider reported
    whichever loop the tree under test runs.
    """

    def __init__(self, models: object, lens_headings: dict[str, str]) -> None:
        self._models = models
        self._headings = lens_headings
        self.sites: dict[str, dict[str, float]] = {}

    def __getattr__(self, name: str) -> object:
        return getattr(self._models, name)

    async def generate_content(self, **kwargs: object) -> object:
        started = time.monotonic()
        try:
            response = await self._models.generate_content(**kwargs)  # type: ignore[attr-defined]
        except Exception as error:
            self._add(kwargs, getattr(error, "usage_metadata", None), started, True)
            raise
        self._add(kwargs, getattr(response, "usage_metadata", None), started, False)
        return response

    def _add(
        self, kwargs: dict[str, object], usage: object, started: float, failed: bool
    ) -> None:
        site = self.sites.setdefault(
            self._site(kwargs),
            {"requests": 0, "failed": 0, **dict.fromkeys(USAGE_FIELDS, 0)},
        )
        site["requests"] += 1
        site["failed"] += failed
        for field in USAGE_FIELDS:
            site[field] += int((usage or {}).get(field) or 0)  # type: ignore[attr-defined]
        site["first"] = min(site.get("first", started), started)
        site["last"] = max(site.get("last", 0.0), time.monotonic())

    def _site(self, kwargs: dict[str, object]) -> str:
        tool_use = getattr(kwargs.get("config"), "tool_use", None)
        if tool_use is None:
            return "writer"
        if any(tool.name == "select_suggestion" for tool in tool_use.tools):
            return "selector"
        # The lens text leads the history on the shared loop and follows the
        # shared prompt on the native one; tool output after it may quote others.
        conversation = getattr(tool_use, "conversation", None)
        text = (
            conversation[0].model_dump_json()
            if conversation
            else str(kwargs.get("contents"))
        )
        found = {
            lens: text.find(heading)
            for lens, heading in self._headings.items()
            if heading in text
        }
        return f"lens:{min(found, key=found.__getitem__)}" if found else "unknown"

    def report(self) -> dict[str, dict[str, float]]:
        return {
            site: {
                **{
                    key: value
                    for key, value in usage.items()
                    if key not in ("first", "last")
                },
                "elapsed_s": round(usage["last"] - usage["first"], 1),
            }
            for site, usage in sorted(self.sites.items())
        }


class _RecordingClient:
    def __init__(self, client: object, recorder: _UsageRecorder) -> None:
        self._client = client
        self.aio = type("Aio", (), {"models": recorder})()

    def __getattr__(self, name: str) -> object:
        return getattr(self._client, name)


async def _replay_one(
    *, db: Path, artifact_root: Path, user_id: str, insight_id: str, label: str
) -> dict[str, object]:
    import pantaray_agents.dependencies as deps
    from pantaray_agents.local_runtime.runtime.suggestion_from_insight import (
        read_reconsidered_insight,
    )
    from pantaray_agents.local_runtime.tooling.repository.workspace_context import (
        build_workspace_context_prompt,
    )
    from pantaray_agents.local_runtime.tooling.repository.workspace_settings import (
        list_workspace_settings,
    )
    from pantaray_agents.local_runtime.tooling.suggestion_research import (
        build_suggestion_research_snapshot,
        discard_suggestion_tool_results,
    )
    from pantaray_agents.schema.agent.suggestion import SuggestionAgentRequest
    from pantaray_agents.utils.trace_context import TraceContextManager

    suggestion_id = f"replay-{label}-{uuid.uuid4().hex[:12]}"
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO agent_suggestions(suggestion_id, user_id, status, "
            "created_at, updated_at) VALUES (?, ?, 'processing', "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now'), strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
            (suggestion_id, user_id),
        )
    insight = read_reconsidered_insight(
        db_path=db,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id=user_id,
        insight_id=insight_id,
    )
    settings = list_workspace_settings(
        db_path=db, busy_timeout_ms=BUSY_TIMEOUT_MS, user_id=user_id
    )
    snapshot = build_suggestion_research_snapshot(
        db_path=db,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        artifact_root=artifact_root,
        user_id=user_id,
        workspace_settings=settings,
    )
    # Raw activity reads need a live permit, which a replay never holds.
    agent = await deps.get_suggestion_agent(
        research_snapshot=snapshot, activity_start=None
    )
    from pantaray_agents.agents.suggestion_agent.lenses import (
        SUGGESTION_LENS_PROMPT_NAME,
    )
    from pantaray_agents.utils.prompt_loader import PromptLoader

    lenses = PromptLoader().load_config(SUGGESTION_LENS_PROMPT_NAME).role_rules
    recorder = _UsageRecorder(
        agent.client.aio.models,
        {lens: text.strip().splitlines()[0] for lens, text in lenses.items()},
    )
    agent.client = _RecordingClient(agent.client, recorder)
    # Both sides look through the same lenses for one Insight.
    agent._lens_rng = random.Random(insight_id)
    languages = await deps.get_user_settings_repository()
    request = SuggestionAgentRequest(
        user_id=user_id,
        suggestion_id=suggestion_id,
        short_term_insight=insight.short_term_insight,
        reconsideration_reason=insight.reconsideration_reason,
        language=(await languages.get_ui_language(user_id)).data,
        workspace_context_prompt=build_workspace_context_prompt(settings) or None,
    )
    started = time.monotonic()
    try:
        with TraceContextManager(user_id=user_id, local_job_id=suggestion_id):
            response = await agent.process(request)
    finally:
        discard_suggestion_tool_results(db_path=db, run_id=suggestion_id)
    error = response.error
    return {
        "label": label,
        "model": MODEL,
        "insight_id": insight_id,
        "suggestion_id": suggestion_id,
        "elapsed_s": round(time.monotonic() - started, 1),
        "status": str(response.status),
        "error": None
        if error is None
        else f"{error.error_code}: {error.error_message}",
        "usage": recorder.report(),
        "has_suggestion": response.has_suggestion,
        "interaction_contract": response.interaction_contract,
        "answer": response.answer,
        "suggestion_summary": response.suggestion_summary,
        **_step_summary(db, suggestion_id),
    }


def _chatgpt_connection(auth_path: Path) -> tuple[str, object]:
    """The app's ChatGPT route, signed in with the Codex CLI's token.

    The token is read here and handed to the connection store only; it is
    never printed or written.
    """
    from pantaray_agents.local_runtime.runtime.connection_store import (
        ChatGptConnection,
        ChatGptCredential,
    )

    tokens = json.loads(auth_path.read_text(encoding="utf-8"))["tokens"]
    access_token = str(tokens["access_token"])
    # The JWT's exp claim, read without verifying: the backend verifies it.
    claims = access_token.split(".")[1]
    payload = json.loads(base64.urlsafe_b64decode(claims + "=" * (-len(claims) % 4)))
    expires_at = datetime.fromtimestamp(int(payload["exp"]), UTC).isoformat()
    return access_token, ChatGptConnection(
        model=MODEL,
        credential=ChatGptCredential(
            access_token=access_token,
            expires_at=expires_at,
            account_id=str(tokens["account_id"]),
        ),
    )


async def _run(args: argparse.Namespace) -> int:
    if args.provider == "chatgpt":
        key, connection = _chatgpt_connection(args.codex_auth)
    else:
        key = os.environ.get("OPENAI_API_KEY", "").strip().strip("'\"")
        if not key:
            print("OPENAI_API_KEY is not set")
            return 2
        connection = None
    db, artifact_root = args.db.resolve(), args.artifact_root.resolve()
    if LIVE_STORE_MARKER in str(db) or LIVE_STORE_MARKER in str(artifact_root):
        print("Refusing to replay on the live store: pass a copy.")
        return 2
    _configure_env(db=db, artifact_root=artifact_root, log_dir=args.out.parent)

    from pantaray_agents.local_runtime.runtime.connection_store import (
        ApiKeyConnection,
        WebSearchCredential,
        set_llm_connection,
        set_web_search_credential,
    )
    from pantaray_agents.local_runtime.runtime.identity import (
        register_logged_out_owner,
    )
    from pantaray_agents.local_runtime.runtime.session_store import mark_configured
    from pantaray_agents.local_runtime.storage.migrations import (
        apply_migrations,
        load_default_migrations,
    )
    from pantaray_agents.settings_loader import (
        LOCAL_RUNTIME_SETTINGS_MODULE,
        register_active_settings_module,
    )

    register_active_settings_module(LOCAL_RUNTIME_SETTINGS_MODULE)

    apply_migrations(
        db_path=db,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        migrations=load_default_migrations(),
    )
    pairs = _insights(db, args.insight_id, args.latest)
    if not pairs:
        print("No Insight to replay.")
        return 2
    mark_configured()
    set_llm_connection(
        connection or ApiKeyConnection(provider="openai", model=MODEL, api_key=key)
    )
    tavily = os.environ.get("TAVILY_API_KEY", "").strip().strip("'\"")
    if tavily:
        set_web_search_credential(WebSearchCredential(api_key=tavily))
    failures = 0
    with args.out.open("a", encoding="utf-8") as out:
        for user_id, insight_id in pairs:
            register_logged_out_owner(user_id)
            try:
                record = await _replay_one(
                    db=db,
                    artifact_root=artifact_root,
                    user_id=user_id,
                    insight_id=insight_id,
                    label=args.label,
                )
            except Exception as error:  # a harness: report each replay's failure
                text = "".join(traceback.format_exception(error))
                for secret in (key, tavily):
                    if secret:
                        text = text.replace(secret, "<redacted>")
                record = {
                    "label": args.label,
                    "insight_id": insight_id,
                    "status": "error",
                    "error": text[-3000:],
                }
            failures += record["status"] == "error"
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            out.flush()
            print(f"{insight_id}: {record.get('status')} {record.get('error') or ''}")
    print(f"label={args.label} model={MODEL} replays={len(pairs)} failures={failures}")
    return 1 if failures else 0


def _compare(args: argparse.Namespace) -> int:
    def load(path: Path) -> dict[str, dict[str, object]]:
        lines = path.read_text(encoding="utf-8").splitlines()
        return {
            record["insight_id"]: record
            for record in (json.loads(line) for line in lines if line.strip())
        }

    before, after = load(args.before), load(args.after)
    totals = {label: Counter() for label in ("before", "after")}
    for insight_id in [i for i in before if i in after]:
        print(f"## Insight {insight_id}\n")
        for record in (before[insight_id], after[insight_id]):
            runs = record.get("lens_runs") or []
            shape = ", ".join(
                f"{run['turns']} turns/{run['tool_calls']} calls"  # type: ignore[index]
                for run in runs  # type: ignore[union-attr]
            )
            print(f"### {record['label']} ({record.get('elapsed_s')}s; {shape})")
            total = totals.setdefault(str(record["label"]), Counter())
            total["elapsed_s"] += float(record.get("elapsed_s") or 0)  # type: ignore[arg-type]
            for site, usage in (record.get("usage") or {}).items():  # type: ignore[union-attr]
                print(
                    f"- {site}: {usage['requests']} requests, "
                    f"input {usage['prompt_tokens']} "
                    f"(cached {usage['cached_prompt_tokens']}), "
                    f"output {usage['completion_tokens']} "
                    f"(reasoning {usage['reasoning_tokens']}), {usage['elapsed_s']}s"
                )
                for field in ("requests", *USAGE_FIELDS):
                    total[field] += usage[field]
            if record.get("error"):
                print(f"error: {record['error']}\n")
                continue
            print(
                f"\nhas_suggestion={record['has_suggestion']} "
                f"kind={record['interaction_contract']} "
                f"selector={record.get('selector')}\n"
            )
            print(f"{record['answer'] or '(no suggestion)'}\n")
    print("## Totals\n")
    for label, total in totals.items():
        print(
            f"- {label}: " + ", ".join(f"{k} {round(v, 1)}" for k, v in total.items())
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="replay Insights of a database copy")
    run.add_argument("--db", type=Path, required=True)
    run.add_argument("--artifact-root", type=Path, required=True)
    run.add_argument("--label", required=True)
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--insight-id", action="append", default=[])
    run.add_argument("--latest", type=int, default=5)
    run.add_argument(
        "--provider",
        choices=("openai", "chatgpt"),
        default="openai",
        help="openai: OPENAI_API_KEY; chatgpt: the ChatGPT route, Codex CLI sign-in",
    )
    run.add_argument(
        "--codex-auth", type=Path, default=Path.home() / ".codex" / "auth.json"
    )
    compare = commands.add_parser("compare", help="pair two replay outputs")
    compare.add_argument("before", type=Path)
    compare.add_argument("after", type=Path)
    args = parser.parse_args()
    if args.command == "compare":
        return _compare(args)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
