"""Replay Suggestion decisions on a copy of a local database, for before/after checks.

`run` decides a Suggestion for each chosen Insight of the copy the way the job
does -- the same prompt, lenses, research tools, selector and writer -- over the
direct route with the OpenAI key in OPENAI_API_KEY (and TAVILY_API_KEY for web
search when set), and writes one JSON line per Insight. Nothing is published:
the run's steps land in the copy only. `compare` pairs two such files by
Insight. Run the same files against two code trees by pointing PYTHONPATH at
each tree's `agents/src` and `agents/packages/pantaray-llm/src`.

The copy and the output hold private data: keep both outside the repository.
The key is read from the environment and never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time
import traceback
import uuid
from collections import Counter
from pathlib import Path

MODEL = os.environ.get("SMOKE_MODEL", "gpt-5.6-luna")
BUSY_TIMEOUT_MS = 5000
# The live store; a replay writes steps and rows, so it only runs on a copy.
LIVE_STORE_MARKER = "Application Support"
UNUSED_PROXY_URL = "https://unused.invalid/v1/llm/proxy"
# Two urgent lenses and one exploration lens (lenses.sample_lenses).
LENS_RUNS = 3


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
    # Lens run i records its steps from i * stride, a value the tree under test sets.
    from pantaray_agents.agents.suggestion_agent.lenses import LENS_STEP_STRIDE

    with sqlite3.connect(db) as connection:
        rows = connection.execute(
            "SELECT step_number, step_kind, status, tool_name, llm_response_text "
            "FROM agent_suggestion_run_steps WHERE suggestion_id = ? "
            "ORDER BY step_number",
            (suggestion_id,),
        ).fetchall()
    runs = [
        {"turns": 0, "failed_sends": 0, "tool_calls": 0, "tools": Counter()}
        for _ in range(LENS_RUNS)
    ]
    selector = None
    for number, kind, status, tool, response in rows:
        index = (number - 1) // LENS_STEP_STRIDE
        if index >= LENS_RUNS:  # the selector's step, then the writer's
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
        "has_suggestion": response.has_suggestion,
        "interaction_contract": response.interaction_contract,
        "answer": response.answer,
        "suggestion_summary": response.suggestion_summary,
        **_step_summary(db, suggestion_id),
    }


async def _run(args: argparse.Namespace) -> int:
    key = os.environ.get("OPENAI_API_KEY", "").strip().strip("'\"")
    if not key:
        print("OPENAI_API_KEY is not set")
        return 2
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
    set_llm_connection(ApiKeyConnection(provider="openai", model=MODEL, api_key=key))
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
    for insight_id in [i for i in before if i in after]:
        print(f"## Insight {insight_id}\n")
        for record in (before[insight_id], after[insight_id]):
            runs = record.get("lens_runs") or []
            shape = ", ".join(
                f"{run['turns']} turns/{run['tool_calls']} calls"  # type: ignore[index]
                for run in runs  # type: ignore[union-attr]
            )
            print(f"### {record['label']} ({record.get('elapsed_s')}s; {shape})")
            if record.get("error"):
                print(f"error: {record['error']}\n")
                continue
            print(
                f"has_suggestion={record['has_suggestion']} "
                f"kind={record['interaction_contract']} "
                f"selector={record.get('selector')}\n"
            )
            print(f"{record['answer'] or '(no suggestion)'}\n")
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
    compare = commands.add_parser("compare", help="pair two replay outputs")
    compare.add_argument("before", type=Path)
    compare.add_argument("after", type=Path)
    args = parser.parse_args()
    if args.command == "compare":
        return _compare(args)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
