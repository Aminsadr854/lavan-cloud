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

## Real PostgreSQL verification completed (2026-10-15)

Overall result: PASS for the milestone-1 real runtime sequence on commit 90a200b.
Approved outside-sandbox execution has Docker access (`agent sudo users docker`);
the restricted sandbox drops supplementary groups. This explains the earlier
agent Docker access failures, rather than a missing host group membership.

Initial state: PostgreSQL running and healthy, API stopped. Built the current
API using `docker compose up --build -d --wait --wait-timeout 120`; locked
production dependency installation succeeded. Used the existing database volume
and local configuration. The earlier healthy-database 503 did not reproduce:
initial live 200 in 0.002759s, ready 200 in 1.141963s. No code change or timeout
increase was justified. The exact cause of the earlier user-reported recovery
failure remains unproven; successful SELECT 1 now establishes API-to-database
connectivity and authentication with the existing configuration.

Explicit HTTP status, exact sanitized JSON body, and <3-second duration assertions
passed in the full sequence (seconds measured with a monotonic clock):

| Scenario | Endpoint | Expected | Actual | Seconds |
| --- | --- | --- | --- | --- |
| Healthy database | live | 200 | 200 | 0.016552 |
| Healthy database | ready | 200 | 200 | 1.008189 |
| Outage 1 | live | 200 | 200 | 0.001870 |
| Outage 1 | ready | 503 | 503 | 1.022716 |
| Outage 2 | live | 200 | 200 | 0.002625 |
| Outage 2 | ready | 503 | 503 | 0.967921 |
| Outage 3 | live | 200 | 200 | 0.002032 |
| Outage 3 | ready | 503 | 503 | 1.053794 |
| Recovered database | ready | 200 | 200 | 1.039492 |
| Recovered database | live | 200 | 200 | 0.001833 |

Outage liveness requests ran while readiness requests were in flight. Only
PostgreSQL was stopped for the outage. After its bounded health-check wait,
readiness recovered without restarting or replacing the API: container ID,
StartedAt, and RestartCount all remained unchanged. Recent API/PostgreSQL logs
had no traceback, fatal, error, or warning entries; raw logs were not exposed.

Finally restored the initial state: PostgreSQL running/healthy, API stopped
(exit 0). No database volumes or data were deleted or reset. No application
changes were necessary; prior automated test results remain as documented above
and were not rerun for this documentation-only update. The previously pending
real database verification is now complete. Milestone 2 was not started.

## Milestone 2 completed (2026-10-15)

Added SQLAlchemy User and Project models, named ownership/name constraints and
owner index, settings-driven Alembic configuration, and initial revision
`0001_users_projects` with upgrade/downgrade. Application-generated UUIDs and
UTC-aware timestamps are documented in schema.md. SQLAlchemy insert/onupdate
maintains updated_at; raw SQL writers must supply it. No business endpoints,
authentication, provisioning, or automatic schema creation/migration were added.
Milestone-1 API and readiness code are unchanged.

Verified:
- `uv sync --locked` succeeded with Alembic 1.20.0 added to the lockfile.
- Ruff lint and formatting checks passed.
- `LAVAN_POSTGRES_TESTS=1 uv run pytest`: 25 passed, zero skipped/failed.
  Includes 12 existing health tests and 13 real PostgreSQL schema tests.
- Tests verified empty upgrade, idempotence, persistence, FK rejection,
  blank/whitespace and overlength rejection, duplicate/max-length names,
  restricted owner deletion, UTC defaults, ORM/Core updated_at behavior,
  no pending metadata changes, and downgrade/re-upgrade. Target database names
  and actual current_database() were checked before destructive migration calls.
- Docker image build and quiet Compose configuration validation passed.
- Container Alembic upgrade, repeat upgrade, check, downgrade, and re-upgrade
  passed against another newly created disposable database.
- All generated `lavan_m2_test_<UUID>` databases were dropped; remaining count 0.
- The existing upstream HTTPX TestClient deprecation warning remains. No checks
  are blocked; no PostgreSQL integration tests were substituted with SQLite.

Real runtime results after rebuilding:

| Scenario | Endpoint | HTTP | Seconds |
| --- | --- | --- | --- |
| Healthy | live | 200 | 0.018253 |
| Healthy | ready | 200 | 1.017610 |
| PostgreSQL stopped | live | 200 | 0.001352 |
| Outage probe 1 | ready | 503 | 1.009448 |
| Outage probe 2 | ready | 503 | 1.042481 |
| Outage probe 3 | ready | 503 | 1.137331 |
| Recovered | ready | 200 | 1.123477 |
| Recovered | live | 200 | 0.001134 |

Statuses, exact sanitized response bodies, and <3-second timings were asserted.
Recovery used the unchanged API container. Restored initial state: PostgreSQL
running/healthy, API stopped (exit 0). Development data/volume preserved; migrations
were executed only against newly created disposable databases, not development.
Apply the explicit README migration command when ready to upgrade development.

Next milestone: development user provisioning and API-token authentication.
It has not been started.

## Milestone 3 completed (2026-10-16)

Added explicit `python -m lavan.manage` commands: create-user, issue-token for an
existing UUID user, and idempotent revoke-token by UUID identifier. Tokens contain
32 cryptographically random bytes; only SHA-256 of the complete token is stored.
Raw issuance output occurs only after successful commit. Tokens have no expiration
in this initial policy and remain valid until revoked. No default users/tokens,
public registration, passwords, JWTs, or project endpoints were added.

Added api_tokens and revision `0002_api_tokens` after unchanged
`0001_users_projects`: UUID identifiers, restricted user FK, unique indexed digest,
UTC created_at and nullable revoked_at. Revoked tokens also prevent user deletion.
Reusable authenticated_user reads only Authorization: Bearer credentials and
returns sanitized 401 with WWW-Authenticate for missing/malformed/unknown/revoked
tokens. GET /v1/me exposes only id and created_at. Unexpected database failures
produce sanitized 503 rather than 401. Scoped synchronous sessions and handlers
run off the event loop; sessions close on request completion. Health probe logic
is unchanged. Docker Uvicorn access logging is disabled to avoid query-string
credential leakage, and no authorization headers or secrets are logged.

Verification:
- Locked dependency synchronization, Ruff lint and formatting passed; no dependency
  upgrade was needed.
- `LAVAN_POSTGRES_TESTS=1 uv run pytest`: 42 passed, zero failed/skipped.
  Includes existing user/project and health checks, real PostgreSQL provisioning,
  token digest/no-raw-column verification, valid/invalid authentication, independent
  and idempotent revocation, unknown-ID errors, unique/FK/deletion constraints,
  failed-commit output suppression, session cleanup/off-loop execution, migration
  downgrade/re-upgrade, and metadata drift checks. All downgrade targets were
  uniquely created disposable databases. The existing HTTPX warning remains.
- Docker image build passed. Confirmed the Compose target was local lavan_dev at
  0001_users_projects with no token table conflict before upgrade. Applied only
  upgrade head to development. Alembic current/heads both report 0002_api_tokens;
  Alembic check reports no pending operations.

Actual API smoke test against PostgreSQL (token captured in memory, never printed):

| Request | HTTP | Seconds |
| --- | --- | --- |
| /v1/me with valid token | 200 | 0.114379 |
| /v1/me after revocation | 401 | 0.014290 |
| /health/live without credentials | 200 | 0.001832 |
| /health/ready without credentials | 200 | 1.179435 |

The 200 response matched the provisioned user and contained only id/created_at;
the 401 response and WWW-Authenticate: Bearer header were asserted.
Retained explicitly identified local verification records in lavan_dev:
- User ID: 8f61a1ef-dffa-4d65-87a0-04d5d4076b6f
- Revoked token ID: 8757af5c-0a46-4ce7-a572-f8da98fa5e61

No raw token is retained in this document or any tracked file. Development data
and volumes were preserved; no development downgrade/reset/stamp or unrelated
record deletion occurred. Initial/final service state: PostgreSQL running/healthy,
API stopped (exit 0). No verification checks remain blocked.

Next milestone: authenticated project creation and retrieval. It has not started.

## Milestone 4 completed (2026-10-16)

Added exactly authenticated POST /v1/projects and GET /v1/projects/{project_id}.
Creation accepts only a strict string name, strips surrounding whitespace, and
validates 1–100 trimmed characters. Extra fields, including ownership and IDs,
are forbidden. Ownership is derived from authenticated_user. Duplicate names
remain allowed. Creation commits the project and returns 201 with a relative
Location header. Responses expose only id, name, created_at, and updated_at.
Retrieval filters both project ID and authenticated owner ID in PostgreSQL;
nonexistent and other-owner records produce identical sanitized 404 responses.
Malformed UUIDs produce 422 for authenticated callers.

The existing cached session/authentication dependencies are reused; synchronous
handlers run in worker threads. Sessions close on success and error, and failed
transactions roll back. Database errors remain sanitized 503; invalid credentials
remain 401. The database constraints and bounded health implementation are unchanged.
No schema, migration, or dependency changes; head remains 0002_api_tokens.

Verified:
- `LAVAN_POSTGRES_TESTS=1 uv run pytest`: 65 passed, zero failed/skipped.
  Includes 23 new real PostgreSQL project tests, all existing auth/health/schema
  tests, ownership isolation, validation, duplicate/max-length names, Location and
  timestamp fields, failed-commit rollback, off-loop I/O and session cleanup.
  Alembic metadata comparison remains clean. Existing HTTPX warning remains.
- Ruff lint/format, locked dependency installation, quiet Compose validation,
  and Docker image build passed.
- Real HTTP smoke test used two explicitly provisioned users against lavan_dev;
  raw tokens were captured only in memory and never printed or written to disk.

| HTTP smoke scenario | Status | Seconds |
| --- | --- | --- |
| A creates a trimmed-name project | 201 | 0.105166 |
| A retrieves the project | 200 | 0.013046 |
| B requests A's project | 404 | 0.013733 |
| Nonexistent project | 404 | 0.012297 |
| Unauthenticated retrieval | 401 | 0.002551 |
| Unauthenticated creation | 401 | 0.001825 |
| Liveness | 200 | 0.001253 |
| Readiness | 200 | 1.034009 |

Asserted Location, exact response fields, trimmed name, ownership and nonexistent
404 body equality, authentication header, and healthy endpoint bodies.
Retained explicitly identified local verification records:
- User A: 2a2aa976-cdda-4519-8dd5-25b42c65774b
- User B: 51d98b7f-ee27-4e37-a243-c41b8ec6024d
- Project owned by A: 70529a14-64a6-4d4f-a9f5-a7c154e4380c
- Revoked A token ID: b67b655b-8bb8-44c7-bab7-65036fa40e23
- Revoked B token ID: 8e18df7b-e07a-4b03-86b1-15f2a0d861af

Both smoke-test tokens were revoked in finally cleanup. Development data/volumes
and unrelated records were preserved. Disposable test databases were removed.
Initial/final service state: PostgreSQL running/healthy, API stopped (exit 0).
No checks are blocked. Next milestone: private ZIP artifact upload and validation.
It has not been started.
