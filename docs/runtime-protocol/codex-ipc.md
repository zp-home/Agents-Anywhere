# Codex App IPC follower

Status: active since connector `feat/codex-ipc-follower`. Replaces the
"SDK-only, no IPC" decision recorded in
[behavior-migration-targets T12](behavior-migration-targets.md#t12-codex-ipc-optional-side-channel).
The historical design in `docs/_deprecated/codex-ipc-integration.md` and the
code under `connector/connector/_reference/codex/` were **not** used; this
implementation was written from the installed Codex App only.

## What it gives users

A conversation that is open in the Codex desktop App (or the Codex VS Code
extension) is mirrored live in AA, like a DSH session:

- token-level streaming, turn status and approvals/questions reach AA as they
  happen instead of on the 30 s rollout poll;
- messages, steer, stop and approval answers sent from AA are executed **by the
  App**, so the App window shows them immediately and there is no second writer
  (`already has an active writer`).

Conversations no App/IDE owns keep the existing SDK path unchanged.

Not covered:

- Interactive `codex` CLI sessions. The CLI binary never publishes conversation
  state over IPC (`codex.exe` 0.154 contains `\\.\pipe\codex-ipc` only for IDE
  context requests; `thread-stream-state-changed` and `thread-follower-` occur
  0 times).
- Conversations AA starts are not published to the App (AA never becomes an
  owner).

## Topology

```text
            Codex IPC router (first App/IDE window that starts)
            Windows: \\.\pipe\codex-ipc   Unix: $CODEX_HOME/ipc/ipc.sock
               ▲             ▲                 ▲
          Codex App     VS Code ext.     AA connector (clientType "agents-anywhere")
          (owner of the     (follower)       (follower only)
           conversations
           it has open)
```

- The router only routes; it has no conversation logic.
- Each conversation has at most one **owner** (the App/IDE window executing it).
  Others are **followers**: they receive the owner's state and send it
  `thread-follower-*` requests.
- AA is a client only: it never listens on, unlinks or replaces the endpoint,
  never takes part in router election, and answers every
  `client-discovery-request` with `canHandle: false`.

## Wire contract (Codex App 26.930 / 26.1007)

- Frame: `uint32` little-endian byte length + UTF-8 JSON. Length `0` or
  `> 268435456` closes the connection.
- `initialize` request with `sourceClientId: "initializing-client"`, version 0,
  `params: {clientType}`; result `{clientId}`.
- Broadcasts are delivered only when `version` equals the receiver's table
  version; requests with another version get `request-version-mismatch`.
- A request with `targetClientId` is still preceded by router discovery; an
  owner that cannot handle it (including version mismatch) makes the router
  answer `no-client-found`.
- Router forward timeout is the request's `timeoutMs` (default 10 s).

Method versions live in `connector/connector/runtimes/codex/ipc/wire.py`
(`METHOD_VERSIONS`). Between App 26.930 and 26.1007 only
`thread-follower-read-model-settings` (v1, unused by AA) was added.

### Following

1. Follower broadcasts `thread-stream-following-changed`
   `{conversationId, hostId: "local", following: true}`.
2. Owner replies with a targeted `thread-stream-state-changed`
   `change: {type: "snapshot", revision, conversationState}`.
3. Every later state change: `change: {type: "patches", baseRevision, revision,
   patches}` with Immer patches (`add` / `replace` / `remove`, array paths,
   `length` truncation).
4. When an App window starts owning a conversation without followers it
   broadcasts `thread-stream-following-status-requested`; followers re-announce.

The App's own follower silently ignores a revision gap. AA instead re-sends
`following: true` to the owner, which answers with a fresh snapshot.

### Conversation state used by AA

- Turns: `turnHistory.kind == "canonical"` →
  `history.islands[].entries[].value` keys `history.entitiesByKey`. The
  top-level `turns` may be empty.
- Turn entity: `turnId`, `status` (`inProgress` / `completed` / `interrupted` /
  `failed`), `items[]` (app-server `ThreadItem`s plus `steeringUserMessage`).
- `requests[]`: pending app-server server requests
  (`item/commandExecution/requestApproval`, `item/fileChange/requestApproval`,
  `item/permissions/requestApproval`, `item/tool/requestUserInput`, ...).
- `title`, `cwd`.

### Follower requests sent by AA

| AA operation | Method (version) | Params besides `conversationId` |
| --- | --- | --- |
| start turn | `thread-follower-start-turn` (2) | `turnStart: {request: <app-server turn/start params>, context: {}}` |
| steer | `thread-follower-steer-turn` (1) | `input` (with `text_elements: []`), `clientUserMessageId`, `restoreMessage` |
| stop | `thread-follower-interrupt-turn` (4; 3 without `expectedTurnId`) | `mode: "user-stop"`, `expectedTurnId` |
| compact | `thread-follower-compact-thread` (1) | — |
| command / file approval | `thread-follower-{command,file}-approval-decision` (1) | `requestId`, `decision` |
| permissions approval | `thread-follower-permissions-request-approval-response` (1) | `requestId`, `response` |
| question | `thread-follower-submit-user-input` (1) | `requestId`, `response` |

`restoreMessage` is the App composer message the owner stores on the pending
`steeringUserMessage`. The App reads `restoreMessage.id`, `.cwd` and
`.context.workspaceRoots`; without it the owner fails with
`Cannot read properties of undefined (reading 'cwd')`, and without
`text_elements` on the input with `... (reading 'length')`.

## Connector implementation

`connector/connector/runtimes/codex/ipc/`:

| Module | Role |
| --- | --- |
| `wire.py` | framing, envelopes, method versions |
| `transport.py` | client-only connection, Windows named pipe / Unix socket, reconnect, quiet retry when no App runs |
| `patches.py` | atomic Immer patch application |
| `mirror.py` | per-conversation follower state, revision checks, resnapshot, owner loss |
| `projection.py` | conversation state → app-server-shaped thread for the existing Codex timeline mapping |
| `session_sync.py` | 50 ms coalesced publication: first full `timeline.sync`, then changed-item upserts, turn ends, notices, session status |
| `routing_client.py` | `CodexRuntimeClient` wrapper: owned threads go to the App, others to the SDK client |

Wiring: `CodexProvider.create_runtime` always creates the IPC connection (no
switch); `CodexRuntime` wraps the SDK client with `CodexIpcRoutingClient`.
Inventory (`list_threads`, first active page) follows the 64 most recent
threads; `read_thread` pins a thread. While an App owns a thread:

- SDK notifications for that thread are dropped (single source);
- `CodexSessionReader` reports `requires_timeline_sync = false` and keeps the
  old marker, so the first poll after the App releases the thread reconciles
  the final rollout once;
- timeline item ids are identical to the SDK `thread/read` path (same native
  item ids → same AA ids), so switching sources does not duplicate items.

Operation results:

| Owner/router answer | Meaning | AA behaviour |
| --- | --- | --- |
| `no-owner`, `not-connected`, `no-client-found`, `client-not-found`, `client-cannot-handle-request`, `request-version-mismatch`, `no-handler-for-request` | not executed | start/steer/stop/compact fall back to the SDK client; approval answers report "未生效" |
| `timeout`, `request-timeout`, `connection-closed`, `client-disconnected`, `server-closed` | outcome unknown | error, never resent |
| any other string | App handler threw | error with the App message |

Session status: pending approval → `waiting_approval`; pending question →
`waiting`; active turn → `running`; else `idle`. When the owner disappears,
AA clears the active turn and expires open IPC notices (`owner_unavailable`).

## Known differences and limits

- Interrupted turns: the App state keeps the partial assistant reply, the
  rollout written to disk does not. AA shows the partial reply while the App
  owns the thread; the reconciling poll after release removes it, matching the
  SDK path.
- Several Codex runtime instances on one Windows machine share the single
  `\\.\pipe\codex-ipc` router and would mirror the same App conversations.
- Not routed yet: `load-complete-history`, thread settings, queue, edit-last,
  daybreak, MCP elicitation responses, read-model-settings.
- The protocol is internal to the Codex App and changes with App releases; the
  method version table must be rechecked against the installed App when an
  App update is reported broken.

## Excluded directions

- `origin/codex/codex-ide-ipc` (2026-09-28) and `_reference/codex/ipc_*.py`:
  judged incorrect by their author; not read for this implementation.
- Shared CLI daemon (`codex app-server daemon start`): requires the standalone
  install (`managed standalone Codex install not found at
  ~/.codex/packages/standalone/current/bin/codex.exe` with an npm install), and
  the App only uses the daemon when `process.platform !== 'win32'` and
  `CODEX_APP_SERVER_USE_LOCAL_DAEMON=1`. It does not replace IPC for the App;
  it is a separate option for CLI sessions.

## Verification

Automated (`connector/`):

```bash
uv run pytest tests/test_codex_ipc_wire.py tests/test_codex_ipc_mirror.py \
  tests/test_codex_ipc_session_sync.py tests/test_codex_ipc_routing_client.py \
  tests/test_codex_ipc_windows_pipe.py tests/test_connector_architecture.py -q
```

The mirror tests run against an in-process fake router and App owner that
follow the App's routing rules; the Windows pipe tests use a real named pipe.

Live (2026-10-10, Windows 11, Codex App 26.1007.2314.0, CLI 0.154.0), full
`CodexRuntime` with a recording host against a dedicated test conversation:

| Case | Result |
| --- | --- |
| discover owner, follow, publish snapshot | owned; 9 timeline items; same AA ids as SDK `thread/read` for all 18 shared native items |
| poll while owned | `requires_timeline_sync = false` |
| `runtime.start_turn` | ack 797 ms, `clientId` round-trips, `idle → running → idle`, turn end `completed` |
| `runtime.steer_turn` | accepted, `steeringUserMessage` with `clientUserMessageId`, projected as user message + `steered` |
| `runtime.interrupt_session` | turn `interrupted`, turn end `interrupted` |
| approval (`untrusted`, read-only) | notice `waiting_approval`; AA decline → App request cleared, notice `resolved`, command `declined` |

Not yet verified live: App closing while followed (T7, fallback to the SDK
path), approval "allow", questions, image attachments, long-running sessions and
reconnect after an App update.

## Manual acceptance on another machine

The checks above drove `CodexRuntime` directly with a recording host. To try
it through the real AA UI, run an isolated local stack on a machine that has
the Codex App.

### Requirements

- Windows 10/11 or macOS with the **Codex desktop App** installed and signed in
  (tested: Windows, App 26.1007.2314.0).
- **No other AA Connector for this OS user.** Quit AA Desktop completely
  (including the tray icon). One Connector per user is enforced through
  `~/.agents-anywhere/connector-runtime.json`; a second one exits with
  `Error: Another Connector is running (desktop-workbench, PID ...)`. The
  mutex path cannot be relocated with `AGENT_CONNECTOR_DATA_DIR`.
- Docker (Compose v2), `uv`, Node with `corepack`, git.

### Pitfalls seen on Windows

- `./local-up.sh` needs `nc` and `pgrep`, which Git Bash lacks
  (`missing command: nc`). Start the services by hand as below.
- Shells started from AA Desktop export `UV_PROJECT_ENVIRONMENT` and
  `VIRTUAL_ENV` pointing at the Desktop Connector venv. `uv sync` then modifies
  **that** venv. Unset both before any `uv` command.
- Port `5174` (the `web-next` default) may already be taken by another AA
  container; it answers every path with the login page. Use another port.
- Plain `yarn` is Yarn 1 and refuses the project; use `corepack yarn`.
- `next dev` rewrites the heading in `web-next/AGENTS.md`; revert it with
  `git checkout -- web-next/AGENTS.md` before committing.

### Steps

```bash
git clone -b feat/codex-ipc-follower https://github.com/zp-home/Agents-Anywhere.git
cd Agents-Anywhere
unset UV_PROJECT_ENVIRONMENT VIRTUAL_ENV
ROOT=$(pwd); mkdir -p .local-dev/logs .local-dev/files .local-dev/connector-data
DB=postgresql+asyncpg://agents_anywhere:agents_anywhere_dev_password@127.0.0.1:55432/agents_anywhere
REDIS=redis://127.0.0.1:56379/0

# 1. PostgreSQL + Redis in a separate compose project
AGENTS_ANYWHERE_POSTGRES_PORT=55432 AGENTS_ANYWHERE_REDIS_PORT=56379 \
  docker compose -p aa-ipc-local -f docker/docker-compose.local.yml up -d --wait

# 2. Server on 127.0.0.1:8010
cd "$ROOT/server" && uv sync
AGENT_SERVER_DB_BACKEND=postgres AGENT_SERVER_DB_URL=$DB AGENT_SERVER_REDIS_URL=$REDIS \
  uv run python -m agent_server.infra.db.migrations upgrade
AGENT_SERVER_DB_BACKEND=postgres AGENT_SERVER_DB_URL=$DB AGENT_SERVER_REDIS_URL=$REDIS \
AGENT_SERVER_FILES_LOCAL_ROOT="$ROOT/.local-dev/files" \
AGENT_SERVER_PUBLIC_ORIGIN=http://127.0.0.1:5184 \
AGENT_SERVER_CORS_ORIGINS=http://127.0.0.1:5184,http://localhost:5184 \
  uv run uvicorn agent_server.app:create_app --factory --host 127.0.0.1 --port 8010 \
  --no-access-log > "$ROOT/.local-dev/logs/server.log" 2>&1 &
curl -s http://127.0.0.1:8010/api/v2/health   # {"status":"ok",...}

# 3. Web on http://127.0.0.1:5184
cd "$ROOT/web-next" && corepack yarn install
AGENTS_ANYWHERE_API=http://127.0.0.1:8010 \
  corepack yarn next dev --hostname 127.0.0.1 --port 5184 > "$ROOT/.local-dev/logs/web.log" 2>&1 &

# 4. Register an account at http://127.0.0.1:5184, then pair the Connector
cd "$ROOT/connector" && uv sync
AGENT_CONNECTOR_DATA_DIR="$ROOT/.local-dev/connector-data" PYTHONUNBUFFERED=1 \
  uv run anywhere-cli pair http://127.0.0.1:8010 \
  --config "$ROOT/.local-dev/connector-data/connector.json" --timeout 1800 \
  > "$ROOT/.local-dev/logs/connector.log" 2>&1 &
# read the pairing code from connector.log and approve it in the Web UI
```

Then, in the Web device page, add a **Codex** runtime (default Codex home).
In the Codex App, open a dedicated test conversation and send one message:
only conversations the App currently has open are published. The conversation
appears in AA after the next inventory pass (at most 30 s) and is live from
then on.

### Scenarios

| # | Do | Expect |
| --- | --- | --- |
| 1 | Type in the Codex App | AA shows the reply streaming |
| 2 | Send from AA | The message appears in the App window immediately and the App runs it |
| 3 | Send a long task from AA, then a follow-up while it runs | Follow-up appears in the App as a steer |
| 4 | Stop from AA | The App turn stops (`interrupted`) |
| 5 | Run a command that needs approval (e.g. App permission "ask" / untrusted) | AA shows the approval; answering in AA resolves it in the App and vice versa |
| 6 | Quit the Codex App | AA stops showing the thread as live; the next poll reconciles it; sending from AA uses the SDK path |

Useful Connector log lines: `codex ipc connected client_id=`,
`codex ipc resync requested`, `codex ipc owner call failed ... outcome=`,
`codex ipc owner released`.

Cleanup: stop the three background processes, then
`docker compose -p aa-ipc-local -f docker/docker-compose.local.yml down -v`.
