# Contributing to Pantaray

Thanks for your interest in Pantaray. Bug reports are welcome in
[GitHub Issues](https://github.com/wakakusa-labs/pantaray/issues). We are not accepting pull requests
at this time. The rest of this guide covers building and running Pantaray from source.

## Reporting bugs

Please include:

- The Pantaray version (**Pantaray → About Pantaray** in the menu bar), and your macOS version
- Which AI connection you were using — your own API key or a ChatGPT sign-in
- What you expected, what happened, and the steps to reproduce

**Privacy note:** Pantaray works on your own activity, so logs, screenshots, and conversations
contain your personal data. Redact before pasting into a public issue, and never paste an API key,
an access token, or the contents of your local database.

Security and privacy vulnerabilities go through [SECURITY.md](SECURITY.md), not public issues.

## Repository layout

| Directory | Contents |
| --- | --- |
| `frontend/electron/` | The Electron main and preload processes |
| `frontend/src/` | The renderer: React, and the message catalogs under `src/i18n/` |
| `frontend/scripts/` | Build and packaging helpers, including the bundled Python runtime |
| `agents/src/pantaray_agents/` | The local Python runtime and the agents |
| `agents/packages/pantaray-llm/` | The shared provider contracts and adapters |
| `scripts/` | CI helpers |

Requests from the renderer always go through Electron main; the renderer never talks to the Python
runtime directly. The runtime listens on loopback on a port chosen at startup and authenticates
requests with a token it generates per run, which main holds and the renderer never sees.

## Development setup

You need a Mac with Apple silicon. The packaging scripts refuse to run on anything else, and the
app is macOS arm64 only.

| Tool | Version | Pinned by |
| --- | --- | --- |
| Node.js | 24 | `.node-version` |
| pnpm | 11.20.0 | `frontend/package.json` (`packageManager`) |
| Python | 3.12 | `agents/.python-version`, `agents/pyproject.toml` |
| uv | 0.12.0 or newer | `agents/pyproject.toml` (`[tool.uv] required-version`) |

### 1. Environment files

Both runtimes are fail-closed: a missing required variable stops startup with an explicit error
rather than falling back to a default. Create both files and fill in the keys below.

```bash
cp frontend/.env.example frontend/.env.local
cp agents/.env.local.backend.example agents/.env.local.backend.local
```

`frontend/.env.local` needs `BACKEND_URL` and `VITE_API_HOST` — the same loopback origin with an
explicit port, such as `http://127.0.0.1:8005`. `VITE_API_HOST` is what the packaged app's content
security policy allows. The other keys in the frontend example are optional.

`agents/.env.local.backend.local` needs every uncommented key in the example. Electron fills in the
machine-specific paths — the database, the artifact directory, and the log file — when it starts
the runtime.

Pantaray account login is off (`PANTARAY_ACCOUNT_LOGIN_ENABLED` in
`frontend/electron/src/auth/accountLoginFeature.ts`), so the account and Cloud keys that are
commented out in the examples — `VITE_SUPABASE_URL`, `VITE_SUPABASE_PUBLISHABLE_KEY`,
`VITE_WEB_APP_URL`, `LLM_PROXY_URL`, `WEB_TOOLS_PROXY_URL` — are neither required nor read until
that flag is turned on. Inference and web search go straight to the provider you configure in the
app.

**Never put a provider secret in these files.** `OPENAI_API_KEY`, `TAVILY_API_KEY` and friends do
not belong there; the app takes provider credentials through its own settings screen, encrypts
them, and keeps them out of the runtime's configuration entirely. The runtime refuses to start
while `OPENAI_API_KEY`, `TAVILY_API_KEY`, or `GEMINI_API_KEY` is set, including in the shell you
start the app from.

### 2. Run the app

```bash
cd frontend
pnpm install
pnpm run electron:dev
```

The development UI uses port 3001 by default. To change it, set `FRONTEND_PORT=3002`
in `frontend/.env.local` (or choose another available port). An explicit shell environment
variable takes precedence. Vite and Electron use the same port; an invalid value or an
occupied port stops startup with an error. Packaged desktop builds load bundled HTML
and do not use this setting.

On macOS the downloaded Electron is only ad-hoc signed, so System Settings forgets its Screen
Recording and Accessibility grants whenever the Electron version or your checkout changes. To keep
them, export a signing identity before `electron:dev`; the runner re-signs the dev Electron with it
(`security find-identity -v -p codesigning` lists yours):

```bash
export PANTARAY_DEV_SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)"
```

That one command fetches the pinned activity recorder and local embedding model, then starts Vite,
Electron main, and the local Python runtime. The runtime is launched by Electron with `uv` on
a loopback port chosen at startup, so you do not need to start it yourself.

The runtime is not meant to run on its own: it hands the token its API requests need only to
Electron main. To work on the Python side, set up its environment and use the checks below:

```bash
cd agents
uv sync --python 3.12 --extra dev --frozen
```

## Running the checks

`scripts/ci/run_local_ci.py` runs the CI workflows' lint, test, and build commands, and only for
the targets your change touches. The default `--mode staged` looks at staged changes; use
`--mode worktree` while you are still working, and `--mode all` to run everything. It stops at the
first failure.

```bash
python3 scripts/ci/run_local_ci.py --mode worktree
```

The repository ships a pre-push hook that runs it in `staged` mode. Enable it once:

```bash
git config core.hooksPath .githooks
```

The commands CI runs, if you prefer to run them directly from the repository root:

```bash
# Renderer and web
cd frontend && node scripts/check-icon-policy.js
cd frontend && pnpm run lint && pnpm run typecheck && pnpm run test:vitest
cd frontend && node --test ./tests/vite_renderer_build.test.js && pnpm run build:web

# Electron main and packaging scripts
cd frontend && pnpm run test:node

# Python runtime
cd agents && uv run ruff check . && uv run ruff format --check .
cd agents && uv run mypy
cd agents && uv run pytest -q -n auto --dist loadfile tests/unit tests/ws tests/integration \
  --ignore=tests/integration/local_runtime
```

Icons come from `lucide-react` only; `check-icon-policy.js` enforces that.

## Writing tests

`agents/tests/TESTING_GUIDELINES.md` is the reference. The points that catch people out:

- **Verify that fail-closed paths actually fail.** A missing required variable, a tool that is not
  allowed, or malformed model output must raise, not get quietly filled in.
- **Keep tests hermetic.** No real network, no real account.
- **Share fixtures through a `fixtures.py`**, not by importing one `conftest.py` from another.
- `MockRepository` keeps a shared store — call `clear_data()` so tests stay independent.
- The Python suites run with `USE_MOCKS=true`, set in `agents/tests/conftest.py`. It is read when
  `pantaray_agents.config` is imported, so set it before the import rather than inside a test body.

**Tests must not call a real provider.** Anything that spends money or depends on someone's account
— a real OpenAI, Anthropic, Fireworks, ChatGPT, or Tavily request — stays out of CI and out of the
unit suites. Cover the provider adapters with fixed responses.

## Never commit secrets

API keys, access tokens, `.env` files, and database contents do not belong in the repository. The
environment files above are all git-ignored; keep it that way. The only checked-in exceptions are
the desktop packaging inputs `frontend/.env.production` and `agents/.env.local.backend.production`,
which hold public values that ship in the app. If you believe a secret has been
committed, treat it as a security report and follow [SECURITY.md](SECURITY.md).
