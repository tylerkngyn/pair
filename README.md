# Pair — timed Python interview practice

A minimal, grayscale practice workspace built with FastAPI and vanilla HTML/CSS/JavaScript. Create an account, choose NeetCode 150 topics and difficulty, pick 1–5 problems and a 5–180 minute session, then run Python, submit practice tests, and review correctness and execution time.

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

For another device on your network, use `uvicorn app.main:app --host 0.0.0.0 --port 8000` and open `http://<computer-LAN-IP>:8000`. Each device runs its own Python interpreter in its browser. Multiplayer interview sessions are not implemented.

## Workspace

- Filter the real 150-entry catalog by topic and difficulty. Select particular problems or leave selection empty for random selection. The selected count must match the session's problem count.
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

`postgres://` and `postgresql://` URLs are normalized to the psycopg driver. Initial tables are created at startup with a database lock; subsequent schema changes will need explicit migrations. PostgreSQL compatibility is implemented but was not tested against a live hosted database during this change.

Optional environment settings:

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | PostgreSQL connection; omit for local SQLite. |
| `PAIR_STATE_DIR` | Default SQLite directory, normally `var`. |
| `PUBLIC_ORIGIN` | Exact external origin, such as `https://pair-example.onrender.com`, for origin checks behind a proxy. No trailing slash. |
| `COOKIE_SECURE` | Set `true` for HTTPS hosting. Secure cookies are also inferred from HTTPS requests. |

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
app/practice/routes.py      Session configuration, ownership, drafts, reports
app/practice/catalog.json   Pinned NeetCode 150 metadata/reference implementations
app/practice/catalog.py     Catalog assembly
app/practice/exercises.py   Original prompts, fixtures, comparators/adapters metadata
static/index.html           Main practice views
static/practice/app.js      Workspace and runner orchestration
static/practice/runtime-loader.js  App-hosted runtime download, progress, caching
static/practice/runtime/314.0.8/   Bundled Pyodide assets, manifest, and notices
static/practice/styles.css  Responsive light/dark grayscale styling
static/practice/runner.html Opaque iframe and disposable Python worker
static/practice/harness.py  Browser-executed correctness/runtime harness
static/chat.html            Retained two-person chat UI at /chat
app/room_manager.py         Retained local chat connection lifecycle
app/models.py               Retained chat WebSocket schemas
tools/import_neetcode.py    Explicit catalog refresh, never runs on app startup
tools/browser_smoke.py      Optional real-browser workflow check
docs/architecture.md        Current architecture and two-person ownership plan
```

The old word/image association game, model cache, datasets, images, dependencies file, and prototype tests have been removed. The old chat remains at `/chat` with `/ws` for later multiplayer work. It still uses in-memory rooms on this branch; **do not run multiple chat workers** without integrating the Redis manager from GitHub `main`. Redis is not needed by the single-player practice flow.

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
```

Set `PAIR_TEST_URL` for another local URL, or `PAIR_CHROME` to an installed Chrome executable. The script creates a disposable test account and checks registration, catalog selection, Python execution, submission, history, login, themes, mobile layout, interruption, and runtime isolation.

The browser test blocks all HTTP requests to external origins. Set `PAIR_TEST_ALL=1` to additionally verify all 150 reference suites in real browser Python. Set `PAIR_TEST_RUNTIME_FAILURES=1` to verify a failed runtime download, cancellation during download, an incompatible cached runner, and successful retry. The test also confirms that Run followed by Submit does not download runtime files twice.

## Attribution

Catalog metadata and Python reference implementations come from [neetcode-gh/leetcode](https://github.com/neetcode-gh/leetcode), pinned to commit `2736633bf3d8a81115b10f299306be91cf822f39`, under its [MIT license](app/practice/NEETCODE_LICENSE.txt). Some upstream files contain multiple approaches; the importer keeps the first complete class per name, includes helper classes, and adapts the standalone Meeting Rooms II function to a Solution class. Prompts and fixtures in `exercises.py` were authored for this app. Private problem statements and private judge tests were not imported. Pair is not affiliated with NeetCode or LeetCode. Bundled Pyodide assets and source/license notices are documented in [the runtime README](static/practice/runtime/314.0.8/README.md).
