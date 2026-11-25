# Pair — timed Python interview practice

A minimal practice workspace built with FastAPI and vanilla HTML/CSS/JavaScript. Create an account, choose NeetCode 150 topics and difficulty, pick 1–5 problems and a 5–180 minute session, then practice solo, create an unranked two-player lobby, or find a ranked opponent. The light/dark interface uses muted accents for difficulty, results, and the timer.

## Run locally

Python 3.11+ is required. The pinned Pyodide runtime is bundled with the app. The first Run loads about 13 MB from your FastAPI server, with visible progress; later runs reuse those files. No external runtime CDN, paid API, API key, or frontend build is required.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --reload-dir app --reload-dir static --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>, create a username/password account, and start practice. Accounts, drafts, login sessions, and history live in `var/pair.db`, ignored by Git. Username accounts do not currently support password recovery or email verification.

Pin `--reload-dir` to `app`/`static` rather than letting `--reload` watch the whole working directory (its default): unscoped, it also watches `var/pair.db`, and the file gets rewritten on every request that touches the database — a login, a queue join, a submission — which restarts the whole worker mid-session. That's silent and easy to misdiagnose as a backend bug (ranked mode's in-process matchmaking queue, in particular, is wiped by any such restart, since it's a plain in-memory list with no Phase 1 persistence by design).

For another device on your network, use `uvicorn app.main:app --host 0.0.0.0 --port 8000` and open `http://<computer-LAN-IP>:8000`. Each player needs a separate account. Each device runs its own Python interpreter in its browser.

## Play together

Choose **Play together** on the setup screen. Topic, difficulty, problem count, and duration configure a new lobby or ranked search; joining an existing lobby uses its host's settings.

- **Create lobby** opens an unranked lobby with a six-character code. Enable **Private lobby** to hide it from Open lobbies; invited players can still join by code. Public is the default. The host starts ready; both players must be ready before the host can start. Start then waits for both browsers to load and uses a shared 3–2–1 countdown before revealing either workspace. The full session duration begins at the common server start, so guest polling does not give the host a head start. Leaving releases the seat; closing the host's waiting lobby returns the guest to setup.
- **Find ranked match** uses the existing Elo queue. Players need identical settings and a compatible rating; the rating window widens while waiting. The search clock updates every second independently of network polling. A new match shows Connecting, player names/Elo/tier/ranked-match counts, then a synchronized 3–2–1 countdown. Both browsers must load the problems and display versus before acknowledging readiness. Only then does the server schedule the shared start (five seconds later), preserving the full configured duration. A slow/polling-only player cannot miss the intro while the other starts early. Refresh continues the current countdown or resumes an already-running match. If both accounts do not acknowledge within 60 seconds of pairing, the match cancels without changing Elo or matches played. Search can be cancelled.
- Both modes use the same randomized problems and server deadline, with separate private code drafts. Opponents see only aggregate correctness and solved counts. Submit saves a score; Finish match locks your answers. Results appear when both finish, the deadline passes, or someone forfeits. Leaving a ranked match counts as a loss. A temporary disconnect preserves the match and deadline.
- Friend lobbies never change Elo or ranked matches played. Ranked results use correctness, then elapsed time to last submission as the tiebreaker, following the existing backend rules. Runtime comparisons remain device-local and do not determine ranked outcomes.

WebSockets carry match/progress/result notifications, with HTTP polling for recovery. The green-dot header counter counts unique signed-in accounts with connected sockets, not tabs, and refreshes about every five seconds. Redis-backed counts are shared across workers and expire stale connections after 90 seconds. Waiting lobby seats expire after two minutes without a heartbeat. Restart FastAPI after updating: startup creates new tables and applies the idempotent `is_private` column migration, preserving existing lobbies as public and keeping existing account/work data.

Without `REDIS_URL`, run **one worker**: the ranked queue and live socket registry are process-local. For multiple workers/instances, configure the existing Redis backend plus a shared database (PostgreSQL for deployment). No Redis or paid service is needed to try both modes locally.

## Workspace

- Browse topic nodes, open a topic to see its problem nodes, and select with a click or Enter/Space. Selections stay in a removable basket across topic, search, and difficulty changes; choose up to five problems across different categories. Selection updates the count automatically. The session uses the chosen IDs and their categories/difficulties; filters only narrow the browsing view while selecting. Random explicitly clears the basket. Lines show category membership, not prerequisites. Animations respect reduced-motion settings. The real catalog still has 150 problems.
- Python editor with line numbers, Tab indentation, automatic indentation, and Ctrl/Cmd+Enter to run examples. This is a lightweight editor, not a full IDE or language server.
- **Run examples** checks the first two fixtures without recording a score. **Submit** checks every practice fixture and saves the result with that code version.
- Autosave writes drafts to the account and keeps a local fallback when storage is available. Revisions reject stale saves from another tab; refresh to resolve a conflict.
- The timer uses the server's session deadline. Overtime is enabled by default. Disable it to stop editing at expiry; there is no automatic submission. A final unsaved local draft is retained if time runs out.
- Finish a session to see its report. Resume active sessions or review completed sessions in Recent sessions (latest 30).

## Correctness and efficiency

All 150 problems have original concise prompts, starter interfaces, and at least two independently specified practice fixtures. Larger deterministic cases supplement selected problems. These are **our practice tests**, not LeetCode/NeetCode private judge tests, and passing them does not prove correctness on every valid input or compliance with an algorithmic constraint such as constant space.

Adapters support linked lists, trees, graphs, random pointers, in-place mutation, codec round-trips, and stateful design classes. Comparators accept alternative valid outputs where appropriate, including course orders, alien alphabets, palindrome ties, and unordered collections.

Correctness = passed tests / all tests across the selected session, including unsubmitted problems as zero. Editing a saved solution invalidates its previous result until resubmission.

Efficiency is a separate **device-local runtime comparison**, available after every test passes. The harness times invocation batches (three samples, median per fixture, normalized per invocation) for your implementation and a pinned reference, then sums the fixture timings. Inputs are rebuilt before measurement; source compilation, runtime download, input adaptation, and output serialization are excluded. Object construction and a small invocation-loop overhead are included. Small reference measurements are labeled too small for a stable ratio. This is not Big-O inference, a cross-user percentile, a memory measurement, or a guaranteed optimal baseline.

## Execution boundary

FastAPI **never executes submitted code**. The workspace downloads the bundled runtime through its own origin and caches its bytes. Code runs in a fresh Web Worker inside an opaque-origin iframe (`sandbox="allow-scripts"`, without `allow-same-origin`). Only runtime bytes, the harness, and the exercise/submission are sent into that worker. Runtime module imports use blob URLs and file fetches resolve in memory; CSP denies all real network connections. No account cookie or CSRF token is passed into the runner. A fresh worker is created per run, so globals and mutations do not leak across runs.

The workspace can terminate execution with Stop; a 10-second execution deadline also terminates the worker. Downloading has a 90-second timeout with visible byte progress; opening the iframe has a 10-second timeout, and interpreter startup has a 30-second timeout. Download failures release the controls for retry; Stop also cancels runtime downloads. API and harness requests have 20-second timeouts. Bootstrap scripts and the iframe use versioned URLs and a matching protocol so Chrome cannot combine an old cached CDN runner with the bundled runtime. There is no hard browser-memory quota. Browser-reported tests/timings are inspectable and tamperable, suitable for personal practice rather than trusted assessments or competitive leaderboards. Result validation on the API checks shape/counts, not independent execution. A future trusted judge should implement a separate isolated server-side runner.

## Accounts and database

Passwords use salted scrypt hashes. Login cookies are HttpOnly and SameSite=Strict; only hashes of opaque login tokens are stored. Logins expire after seven days and logout revokes the token. Writes check a CSRF token and request origin. Authentication attempts have database-backed limits. Private API responses use `Cache-Control: no-store`.

The default database is SQLite. To use PostgreSQL, set `DATABASE_URL` before startup:

```sh
export DATABASE_URL='postgresql://USER:PASSWORD@HOST/DATABASE?sslmode=require'
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

`postgres://` and `postgresql://` URLs are normalized to the psycopg driver. Initial tables and explicit upgrades in `app/practice/migrations.py` run under a startup database lock. PostgreSQL compatibility is implemented but was not tested against a live hosted database during this change.

Optional environment settings:

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | PostgreSQL connection; omit for local SQLite. |
| `PAIR_STATE_DIR` | Default SQLite directory, normally `var`. |
| `PUBLIC_ORIGIN` | Exact external origin, such as `https://pair-example.onrender.com`, for origin checks behind a proxy. No trailing slash. |
| `COOKIE_SECURE` | Set `true` for HTTPS hosting. Secure cookies are also inferred from HTTPS requests. |
| `REDIS_URL` | Optional shared WebSocket transport and ranked queue for multiple workers. |

`.env.example` lists settings; plain startup does not automatically load `.env`. Use environment exports or Uvicorn's `--env-file .env` option. Never commit real connection strings.

## Free Render deployment and persistent accounts

`render.yaml` keeps a single free web instance and enables secure cookies. It **does not provision a database**. Render's free filesystem loses local SQLite files on restarts, redeploys, and spin-down, so configure external PostgreSQL before relying on hosted accounts. See [Render's free-service limitations](https://render.com/docs/free).

One zero-cost option within its usage allowance is [Neon Free](https://neon.com/pricing): create a free PostgreSQL project, copy its SSL connection string, and set it as `DATABASE_URL` in Render's environment settings. Also set `PUBLIC_ORIGIN` to the Render service's actual HTTPS origin. Keep the database on its free plan; no paid service is required by this implementation. This project has not provisioned a Neon project or changed your live Render service.

Existing local accounts are not automatically copied into a newly configured PostgreSQL database. Account migration is a separate operation.

## Project layout

```text
app/main.py                 HTTP/static routes, lifecycle, security headers, legacy WS
app/practice/auth.py        Accounts, sessions, CSRF, auth limits
app/practice/database.py    SQLAlchemy storage models and SQLite/Postgres setup
app/practice/migrations.py  Idempotent schema upgrades under the startup lock
app/practice/routes.py      Session configuration, ownership, drafts, reports
app/practice/lobby_routes.py Public two-seat lobbies, readiness, host start, seat leases
app/practice/ranked_routes.py Ranked queue, shared matches, private work, finish/results
app/practice/redis_ranked_queue.py Redis Lua queue, pending notifications, finalization locks
app/practice/catalog.json   Pinned NeetCode 150 metadata/reference implementations
app/practice/catalog.py     Catalog assembly
app/practice/exercises.py   Original prompts, fixtures, comparators/adapters metadata
static/index.html           Main practice views
static/practice/app.js      Workspace and runner orchestration
static/practice/problem-map.js Topic/problem node selection and SVG connections
static/practice/multiplayer.js Lobby browser, room codes, ranked search, match recovery
static/practice/runtime-loader.js  App-hosted runtime download, progress, caching
static/practice/runtime/314.0.8/   Bundled Pyodide assets, manifest, and notices
static/practice/styles.css  Responsive light/dark styling with muted accents
static/practice/runner.html Opaque iframe and disposable Python worker
static/practice/harness.py  Browser-executed correctness/runtime harness
static/chat.html            Retained two-person chat UI at /chat
app/room_manager.py         Retained local chat connection lifecycle
app/redis_room_manager.py   Shared Redis room/account notifications
app/models.py               Retained chat WebSocket schemas
tools/import_neetcode.py    Explicit catalog refresh, never runs on app startup
tools/browser_smoke.py      Optional real-browser workflow check
tools/browser_multiplayer_smoke.py Two-account lobby/ranked browser workflow
tools/browser_ranked_sync_smoke.py Delayed/poll-only client shared-start regression
docs/architecture.md        Current architecture and two-person ownership plan
```

The old word/image association game, model cache, datasets, images, dependencies file, and prototype tests have been removed. The old chat remains at `/chat`; `/ws` also delivers authenticated match notifications. The local and Redis transport implementations are both integrated, selected by `REDIS_URL`. `/ranked` remains a diagnostic client; the product flow is at `/`.

## Verification

```sh
pip install -r requirements-dev.txt
python -m pytest -q
```

Tests verify all 150 references against 460 fixtures, starter failures, account security, ownership, draft revision conflicts, score invalidation, filtering, deadlines, persistence across restarts, and retained chat lifecycle. Native harness tests execute trusted reference/test snippets, never arbitrary web submissions.

Optional real-browser workflow (run a local server separately):

```sh
pip install playwright
playwright install chromium
python tools/browser_smoke.py
python tools/browser_multiplayer_smoke.py
python tools/browser_ranked_sync_smoke.py
python tools/browser_ranked_sync_smoke.py --lobby
```

Set `PAIR_TEST_URL` for another local URL, or `PAIR_CHROME` to an installed Chrome executable. The script creates a disposable test account and checks registration, catalog selection, Python execution, submission, history, login, themes, mobile layout, interruption, and runtime isolation.

The multiplayer script uses two separate accounts/browser contexts and checks unique-account counting (including an extra tab and disconnect), private-code/public-list joins, capacity/readiness/shared start, private drafts, Python submission, refresh recovery, opponent progress, unranked results, ranked search/cancel/results, mobile layout, and footer placement. Run it against an otherwise idle test server so the online totals are deterministic. The synchronization regression runs in ranked or `--lobby` mode with a ten-second delayed, polling-only peer; it checks countdowns, absence of flashing waiting text, and workspace reveal times within 500 ms. These scripts write only disposable test-account data to the server you point them at. Real multi-process Redis tests are opt-in with `python -m pytest -m integration` and require Docker or `PAIR_TEST_REDIS_URL`.

The browser test blocks all HTTP requests to external origins. Set `PAIR_TEST_ALL=1` to additionally verify all 150 reference suites in real browser Python. Set `PAIR_TEST_RUNTIME_FAILURES=1` to verify a failed runtime download, cancellation during download, an incompatible cached runner, and successful retry. The test also confirms that Run followed by Submit does not download runtime files twice.

## Attribution

Catalog metadata and Python reference implementations come from [neetcode-gh/leetcode](https://github.com/neetcode-gh/leetcode), pinned to commit `2736633bf3d8a81115b10f299306be91cf822f39`, under its [MIT license](app/practice/NEETCODE_LICENSE.txt). Some upstream files contain multiple approaches; the importer keeps the first complete class per name, includes helper classes, and adapts the standalone Meeting Rooms II function to a Solution class. Prompts and fixtures in `exercises.py` were authored for this app. Private problem statements and private judge tests were not imported. Pair is not affiliated with NeetCode or LeetCode. Bundled Pyodide assets and source/license notices are documented in [the runtime README](static/practice/runtime/314.0.8/README.md).
