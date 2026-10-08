#!/usr/bin/env bash
# Compare Suggestion output of two git refs on the same recent Insights.
#
#   OPENAI_API_KEY=... agents/scripts/live/suggestion_replay.sh [BEFORE_REF] [AFTER_REF]
#   REPLAY_PROVIDER=chatgpt agents/scripts/live/suggestion_replay.sh ...
#
# Copies the app's database and artifacts under /tmp, replays the latest
# $LATEST (5) Insights on each ref with suggestion_replay.py and prints the
# comparison. Refs default to origin/develop and HEAD. PYTHON names the
# interpreter (default: agents/.venv/bin/python); PANTARAY_APP_DIR the app's
# data folder. REPLAY_PROVIDER=chatgpt sends on the app's ChatGPT route with the
# Codex CLI's sign-in (~/.codex/auth.json) instead of an API key. The copy and
# outputs stay in the printed /tmp folder.
set -euo pipefail

provider=${REPLAY_PROVIDER:-openai}
[ "$provider" = chatgpt ] || : "${OPENAI_API_KEY:?OPENAI_API_KEY is not set}"
before_ref=${1:-origin/develop}
after_ref=${2:-HEAD}
live=$(cd "$(dirname "$0")" && pwd)
agents=$(dirname "$(dirname "$live")")
python=${PYTHON:-$agents/.venv/bin/python}
app=${PANTARAY_APP_DIR:-"$HOME/Library/Application Support/Pantaray"}
work=$(mktemp -d /tmp/suggestion-replay.XXXXXX)

sqlite3 "$app/local-backend.sqlite3" ".backup '$work/local-backend.sqlite3'"
cp -R "$app/local-backend-artifacts" "$work/artifacts"
for side in before after; do
  ref=$([ "$side" = before ] && echo "$before_ref" || echo "$after_ref")
  mkdir "$work/$side"
  git -C "$agents/.." archive "$ref" agents/src agents/packages | tar -x -C "$work/$side"
  tree="$work/$side/agents"
  status=0
  PYTHONPATH="$tree/src:$tree/packages/pantaray-llm/src" "$python" \
    "$live/suggestion_replay.py" run --db "$work/local-backend.sqlite3" \
    --artifact-root "$work/artifacts" --label "$side" --out "$work/$side.jsonl" \
    --latest "${LATEST:-5}" --provider "$provider" || status=$?
  # 1 means some replay failed, which the comparison shows; anything else stops.
  [ "$status" -le 1 ] || exit "$status"
done
PYTHONPATH="$tree/src:$tree/packages/pantaray-llm/src" "$python" \
  "$live/suggestion_replay.py" compare "$work/before.jsonl" "$work/after.jsonl" \
  | tee "$work/comparison.md"
echo "Outputs: $work"
