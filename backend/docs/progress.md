# Milestone 1: local backend infrastructure

Implemented a minimal FastAPI application, validated environment settings,
PostgreSQL-only SQLAlchemy connectivity, and independent liveness/readiness.
Compose defines exactly API and PostgreSQL, localhost ports, a database health
check, and persistent project-scoped storage. Dependencies are locked in uv.lock.

Verified on 2026-10-02:
- `uv sync --locked` succeeded on Python 3.12.3.
- Ruff lint and formatting checks passed; all 9 focused tests passed.
- `docker compose config --quiet` passed.
- A real host Uvicorn process returned live 200, readiness 503 against an
  unreachable database port within 10 seconds, and live remained 200.

Blocked: `docker compose up --build -d --wait` failed with permission denied on
`/var/run/docker.sock`. Passwordless sudo is unavailable. No real PostgreSQL
success, Compose outage, or recovery checks were performed. Enable Docker access
for this WSL user, then run the README verification sequence. Unit tests are not
substitutes for these checks. pytest emits an upstream Starlette warning about
HTTPX TestClient deprecation; the requested HTTPX tests still pass.

The prior generated implementation was removed in cleanup commit c0a432f.
No business features or CI were added. Next milestone: business data models and
migrations, after completing the outstanding real PostgreSQL verification.

## Verification follow-up (2026-10-02)

Rechecked from `/home/agent/lavan/backend` on branch `feat/backend-mvp`, starting
at commit 8a0bc62 with a clean working tree. Applicable AGENTS.md and README
instructions were reviewed. Compose still defines services `api` and `postgres`,
with API published on `127.0.0.1:8000`.

Passed again using uv 0.12.22 and the existing lockfile:
- `uv sync --locked`
- `uv run ruff check .`
- `uv run ruff format --check .`
- `uv run pytest`: 9 passed, one upstream HTTPX TestClient deprecation warning
- `docker compose config --quiet`

Overall result: BLOCKED. `docker info` without sudo reports permission denied
on `/var/run/docker.sock`. The CLI is installed, context is `default`, and Docker
connection override variables are unset. The socket is owned by `root:docker`
with mode 0660; the current user `agent` is not a member of `docker`.
No system configuration, contexts, permissions, or group memberships were changed.
For this local Docker Engine socket, an administrator can grant access using
`sudo usermod -aG docker agent`, followed by a fresh login/session and `docker info`.

All real Compose HTTP scenarios are NOT RUN: database available (live/ready),
database stopped (live and repeated readiness with durations), and database
recovered (ready/live). Recovery without restarting the API is unverified.
The prior host-only check is historical evidence, not a PostgreSQL runtime pass.
Service status and logs could not be inspected through the inaccessible daemon.
No services were started, stopped, or restarted; their initial state is unknown
and was left unchanged. No volumes or database data were modified or deleted.
No application defect was demonstrated. Milestone 1 is not ready to close until
the README real PostgreSQL sequence passes. Milestone 2 was not started.

## Docker group refresh follow-up (2026-10-02)

The user reports Docker works in their fresh WSL terminal. Read-only checks in
this agent execution environment confirm `getent group docker` now lists `agent`,
but `id` still shows only groups `agent`, `sudo`, and `users`. The existing agent
process has stale supplementary groups. `docker info` and Compose service listing
still fail with socket permission denied. Restart the agent execution session
from a fresh WSL login that has Docker access, then repeat the README sequence.
No group memberships, socket permissions, contexts, or system settings were changed.

Locked installation, Ruff lint, Ruff formatting, all 9 tests, and quiet Compose
validation passed again. The existing HTTPX TestClient deprecation warning remains.
All real PostgreSQL HTTP scenarios and their durations remain NOT RUN; recovery
without restarting the API is unverified. Service state could not be listed.
No services or volumes were changed. Overall verification remains BLOCKED.

## Readiness deadline defect and fix (2026-10-02)

User-reported real Docker evidence before this fix:
- Database running: live 200 in 0.084358s; ready 200 in 0.051103s.
- PostgreSQL stopped: live 200 in 0.002993s; readiness timed out after
  10.020181s, curl exit 28 / HTTP 000 (no HTTP response).

Confirmed defect: no end-to-end readiness deadline. Synchronous psycopg connection
timeouts apply per resolved address and do not bound DNS; server statement
timeouts do not bound client-side network waits. NullPool means no acquisition
queue or stale pooled connection was involved. The sync FastAPI handler ran in a
worker thread, but hung checks could retain workers. The precise stalled phase
in the user's run is untraced; Docker DNS after stopping postgres is plausible,
not proven by a trace.

Fix: disposable subprocess with a 2-second whole-operation deadline, kill/reap
on timeout or task cancellation, and one in-flight probe per API process.
Overlapping checks return sanitized 503. This bounds DNS and driver stalls
without pretending thread cancellation stops blocking I/O. Driver connect_timeout
is 2s, statement_timeout 1s, tcp_user_timeout 1.5s; NullPool retains no sockets or
acquisition queue. Liveness stays independent; recovery uses a fresh connection.
The local HTTP target is under 3 seconds including cleanup and scheduling.

Verification: 12 tests passed, including actual stalled subprocess kill/reap,
repeated checks, concurrent liveness, overlap rejection, cancellation, recovery,
and connection/engine cleanup. Ruff lint and formatting passed. The existing
HTTPX TestClient warning remains; dependencies unchanged.
Docker info still fails in this agent session with socket permission denied;
process groups are agent/sudo/users. Real PostgreSQL checks after the fix are
NOT RUN and pending using README's bounded assertion commands. No services,
volumes, or data were changed. Milestone 2 was not started.
