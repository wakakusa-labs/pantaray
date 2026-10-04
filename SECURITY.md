# Security Policy

Pantaray watches what you do on your own Mac and can edit files and run commands on it, so we
treat a broken privacy or approval boundary with the same severity as a classic vulnerability.

## Reporting a vulnerability

Please **do not open a public issue**. Use
[GitHub private vulnerability reporting](https://github.com/wakakusa-labs/pantaray/security/advisories/new)
instead, and include the version, your macOS version, and the steps to reproduce.

Never attach raw logs, a copy of your local database, or an Action transcript to a report: they
contain your own activity. A minimal reproduction is enough.

## In scope

- A stored credential — a provider API key, a Tavily key, a ChatGPT token, or the local API
  token — reaching disk in plaintext, a log file, a prompt, an error message, or the renderer
- A request going somewhere other than the route in **Settings → AI connection**, such as a
  request sent to a provider you did not configure
- The local HTTP or WebSocket API answering a request that carries no valid per-process token, or
  returning one owner's history, memory, or artifacts to another
- The recorder capturing from an app or website that the recording filters exclude, or from an app
  on the always-denied list (password managers and credential stores)
- A tool that edits files or runs commands doing so without the approval it requires, or reaching
  outside the configured read and write scope
- An Action reading or changing internal data under `~/Library/Application Support/Pantaray/`
  that is not exposed to it, such as the local database, stored credentials, or another Action's
  folders, through any file, search, or command tool. The folders the app sets up there for that
  Action's own work are usable by design.
- The updater accepting a build whose signature does not verify

## Known and accepted

These are design limits rather than vulnerabilities, and a report about them will be closed as
such:

- **Anything running as your user can read your data.** The local database, the artifact
  directory, and the logs are protected by file permissions, not by encryption at rest. Disk theft
  is covered by FileVault.
- **`/health` and `/metrics` on the local runtime are unauthenticated.** They are bound to
  loopback on a port chosen at startup and expose liveness and counters, not your content. Every
  other route and the WebSocket require the per-process token.
- **A command run with your login information can act as you.** An Action can run a command with
  access to your login information so that CLIs you signed into in your terminal, such as `gh`,
  work. That command can do what those CLIs can do. It follows the conversation's approval setting
  like any other command, and it can write only where other commands can: the workspace folders,
  the folders you approved, and temporary folders.
- **Your recorded activity is sent to the provider you chose.** This is what the product does, not
  a leak: the parts of the recording that suggestions and Actions need — screen text, typed input,
  URLs — go to the connection configured in settings, and Pantaray does this on its own while
  recording is on. What that provider does with it is governed by your agreement with them. A
  report is in scope when the data goes somewhere other than the configured route, or when it
  includes something the recording filters should have excluded.

## How credentials are handled

- Provider API keys, the Tavily key, ChatGPT tokens, and the Pantaray session are encrypted with
  Electron's `safeStorage` — Keychain-backed on macOS — and written with mode `0600` under
  `~/Library/Application Support/Pantaray/`. If `safeStorage` reports that encryption is
  unavailable, Pantaray refuses to store the value rather than falling back to plaintext.
- The renderer never receives them. Its IPC surface can save, replace, and remove a credential and
  read back whether one is stored; there is no operation that returns a key, and nothing is shown
  beyond a "saved" indicator.
- Credentials reach the local Python runtime over a `0600` UNIX socket under the same directory and
  stay in that process's memory. The runtime does not persist them, log them, or return them in a
  status response. The ChatGPT refresh token is never sent to it at all — only a short-lived access
  token, its expiry, and the account id.
- The local runtime listens on loopback on a port chosen at startup, and authenticates every
  request with a 32-byte token generated per process and compared in constant time.
- The recorder's store is encrypted, keeps 48 hours of events, and its key is held in the macOS
  Keychain by the recorder itself.
- Logs are written only to `~/Library/Logs/Pantaray/`, are redacted for tokens and credential-like
  query parameters before being written, and are never uploaded anywhere.

## Supported versions

Only the latest release receives fixes.
