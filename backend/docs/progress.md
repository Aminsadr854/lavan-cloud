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

## Milestone 5 completed (2026-10-18)

Added private raw application/zip upload and authenticated artifact metadata:
POST /v1/projects/{project_id}/artifacts and
GET /v1/projects/{project_id}/artifacts/{artifact_id}. Ownership is checked before
body consumption in a short session closed before transfer; it is rechecked
before persistence. All inaccessible/missing/mismatched artifact resources return
sanitized 404. Metadata exposes only the seven documented public fields.

Actual bytes are counted and SHA-256 hashed during streaming, independent of
Content-Length. Writes/hash operations use <=64-KiB pieces. Upload/validation
capacity is bounded per process (default 2), with a 60-second receive/processing
budget and cancellation-safe draining of blocking work before cleanup. Blocking
work runs off the event loop. Filesystem stalls/final commit/cleanup can extend
wall time; this is not a hard kernel-I/O deadline. No DB session/transaction is
held during body transfer. Staging files and archive files use 0600 in 0700
private storage outside the source tree; Compose adds lavan-local_artifact_data.
Atomic same-filesystem hard-link publication precedes DB commit. Ordinary failed
uploads roll back/clean up; uncertain commit acknowledgement preserves potentially
referenced files and may require later reconciliation. See README for crash windows.

Added Artifact model and migration 0003_artifacts after unchanged 0002_api_tokens:
UUID identifiers, indexed project FK with RESTRICT, unique private storage key,
nonnegative counts, SHA-256, and UTC timestamp. No extraction, decompression,
installation, execution, download, queue, runner, or public URL was introduced.
The ZIP parser validates bounded central/local directory structures and original
filenames, permitted methods, modes, sizes, normalized paths/conflicts and optional
data descriptors. ZIP64/multi-disk/prefixed and other unsupported layouts fail
closed. This does NOT prove CRC/payload integrity or safe future extraction.

Added conservative `python -m lavan.reconcile`: dry-run default, explicit --delete,
minimum age >=3600s (default 86400), verified root containment, only recognized
regular UUID files, no symlink/unknown/referenced/recent removal. Shared upload
locks and exclusive reconciliation lock prevent deletion racing with active
uploads; reconciliation refuses while uploads are active. No global pruning.

Verification:
- `LAVAN_POSTGRES_TESTS=1 uv run pytest`: 128 passed, zero failed/skipped.
  Includes 65 existing tests and 63 artifact/storage tests using real PostgreSQL
  disposable databases and isolated pytest storage. Covers valid nested files,
  matching stored bytes/hash, MIME/auth/ownership/mismatch, missing/misleading
  Content-Length, invalid/truncated/encrypted/unsupported archives, original NUL
  names, paths, symlinks/special files, duplicates/conflicts, all configured limits,
  timeouts/disconnect/cancellation, capacity, no abandoned validation, DB rollback
  compensation, uncertain commit acknowledgement, reconciliation safety, project
  deletion restriction, nonnegative constraints and migration downgrade/re-upgrade.
- Metadata comparison, Ruff lint/format and locked installation passed. No new
  dependencies were required. Existing upstream HTTPX TestClient warning remains.
- Docker build passed on retry after a transient ghcr.io TLS handshake timeout.
  Quiet Compose validation passed; exactly two services remain.
- Confirmed local lavan_dev at 0002_api_tokens with no artifacts table conflict,
  then applied upgrade head only. Current/head are 0003_artifacts; Alembic check
  reports no pending operations. No old migration was edited.

Actual HTTP verification against PostgreSQL:

| Scenario | HTTP | Seconds |
| --- | --- | --- |
| Create verification project | 201 | 0.114896 |
| Upload generated valid ZIP | 201 | 0.034904 |
| Retrieve owned metadata | 200 | 0.019145 |
| Another user requests metadata | 404 | 0.010699 |
| Reject invalid ZIP | 422 | 0.017859 |
| Unauthenticated metadata | 401 | 0.002320 |
| Liveness | 200 | 0.001262 |
| Readiness | 200 | 1.166832 |

Asserted checksum/byte count, metadata equality, Location, ownership, sanitized
errors, and health bodies. A read-only private-storage inspection confirmed stored
bytes/hash, 0600 mode and no rejected-upload staging leftovers. Development-volume
reconciliation dry-run returned an empty candidate list and deleted nothing.
Raw tokens were captured only in memory, never printed or written to a file.

Retained local verification records and successful private artifact:
- User A: adb63c97-05fb-4091-9347-844aa48e9b04
- User B: 0d08ea83-0b1d-4052-a0c6-aa3c62c12c51
- Project: 1ef74813-64bd-42e7-8f84-d290c1920bd0
- Artifact: 70b2feef-0239-4180-87a5-c1eff0762bcf
- Revoked A token ID: 7faf3fd2-c862-44e7-bf94-868cf87e40ad
- Revoked B token ID: fcec29ad-bc8e-4c56-9797-b38db190c90a

Initial inspection found BOTH services stopped (different from the prior report).
Both were started temporarily; both are now stopped again, matching initial state.
Verification tokens were revoked in finally cleanup. Existing development data,
volumes and unrelated records were preserved. No development downgrade/reset/stamp
or volume deletion occurred. No checks remain blocked.

Next milestone: run lifecycle with a simulated runner. It has not been started.

## Milestone 6A completed (2026-10-18)

Added Run persistence and exactly POST /v1/projects/{project_id}/runs and
GET /v1/projects/{project_id}/runs/{run_id}. Both reuse Bearer authentication and
short, scoped synchronous sessions off the event loop. Queries filter ownership
through Project. POST validates that the selected artifact is in that same owned
project, forbids unknown fields, and accepts only explicit python-fastapi-v1.
Creation commits before returning 201 and Location. Responses expose only id,
project_id, artifact_id, status, runtime_template, created_at. Resource absence,
mismatched project/artifact/run IDs, and other-owner access use sanitized 404;
invalid UUIDs/body/template use 422, credentials 401, database failures 503.

Run contains only the six specified columns: application UUID, direct indexed
project FK, artifact FK via the composite (artifact_id, project_id) relation,
queued-only status/default, supported-template constraint, UTC created_at default.
New uq_artifacts_id_project_id is required by PostgreSQL as the explicit unique
target for that composite FK, despite globally unique artifact IDs. Both run FKs
use RESTRICT; no cascade deletion or duplicate user_id exists.

Every request remains queued. There is no dispatch, worker, job queue, simulated
runner, background lifecycle task, execution, source inspection, cancellation,
expiration, log streaming, run listing or active-run quota. Explicit template
selection is not compatibility validation. Idempotency is deferred; repeated POSTs
create independent records and clients must not blindly retry uncertain requests.

Verified:
- LAVAN_POSTGRES_TESTS=1 uv run pytest: 164 passed, zero failed/skipped.
  Includes 36 new real PostgreSQL run tests plus all 128 previous tests. Verified
  response/Location/persistence, owner-only retrieval, same-owner cross-project
  artifact rejection, direct DB consistency/status/template/delete constraints,
  identical missing/inaccessible 404s, malformed UUID/body/unknown fields,
  missing/unknown/revoked credentials for both endpoints, repeated submissions,
  off-loop I/O, cleanup/rollback, disposable-only downgrade/re-upgrade preserving
  artifacts, and clean metadata comparison. Existing HTTPX warning remains.
- Ruff lint/format, uv sync --locked, Docker build and quiet Compose validation
  passed. No dependency changes were required.
- Confirmed local Compose lavan_dev at 0003_artifacts with no runs table before
  applying only upgrade head. Current/heads now report 0004_runs; Alembic check
  reports no pending changes. Both restrictive run FKs were inspected. Existing
  counts (5 users, 2 projects, 1 artifact, 5 tokens) were unchanged by migration
  before explicitly adding the new smoke-test records. No old migration edited.

Actual HTTP smoke test:

| Check | HTTP | Seconds |
| --- | --- | --- |
| Create verification project | 201 | 0.095488 |
| Upload inert ZIP | 201 | 0.033274 |
| Create queued run | 201 | 0.030373 |
| Retrieve owned queued run | 200 | 0.012006 |
| Another user retrieves run | 404 | 0.014398 |
| Another user submits run | 404 | 0.010940 |
| Unauthenticated run retrieval | 401 | 0.002330 |
| Liveness | 200 | 0.006689 |
| Readiness | 200 | 1.087306 |

Asserted exact response fields, IDs, queued status, explicit runtime template,
Location, owner retrieval equality, sanitized cross-user errors, and health bodies.
The ZIP contained an inert text fixture; nothing inspected/executed it as an app.
Tokens stayed only in memory and were never printed or written to tracked files.
Retained explicitly identified local verification records:
- User A: 3cda3d8f-c491-4690-942d-1eb0bfdf4b74
- User B: 41b37b98-dd2a-4dd0-80d7-08649019a36b
- Project: 990646cf-ad47-4b98-a7e6-f611ced35a6e
- Artifact (private file retained): 68efbac4-477e-4aaa-84ff-bf347835cd9d
- Queued run: cb469352-2edd-46c4-868e-a514fad336e2
- Revoked A token ID: 226390f9-293d-4fb8-a16f-f201ff71a703
- Revoked B token ID: f168f80c-3828-410e-aeb7-0162ec7e5bee

Finally cleanup revoked both verification tokens and restored both services to
stopped (initial state). Existing development data, private artifact files, and
both volumes were preserved. No development downgrade/reset/stamp or unrelated
record deletion occurred. All automated destructive checks used newly created
UUID-named disposable databases. No checks remain blocked.

Next milestone: idempotency and lifecycle processing with a simulated runner.
Milestone 6B/lifecycle processing has not started.


## Milestone 6B — durable idempotent run creation (verified)

Implemented required Idempotency-Key validation (exactly one header, 1–128 ASCII
letters/digits/period/underscore/hyphen, case-sensitive, no normalization).
The durable user/operation/key composite primary key scopes retries across the
user's projects. Canonical versioned semantic fingerprints use SHA-256. Run and
retry record commit atomically; PostgreSQL ON CONFLICT targets only that primary
key, rolls back a losing run, and reads the committed winner. Other database
failures remain sanitized 503. Authorization and artifact membership precede
conflict/replay decisions. Locks wait at most 1500ms; statement timeout remains
2000ms. No lifecycle processing, execution, quotas, expiration, or key cleanup.

Verification:
- `uv sync --locked`, Ruff lint and formatting checks passed; no dependency changes.
- **185 tests passed, none skipped**, with LAVAN_POSTGRES_TESTS=1 against real
  PostgreSQL and isolated disposable databases. The existing HTTPX/Starlette
  deprecation warning remains; no dependency upgrade was made for it.
- Synchronized separate PostgreSQL connections verified identical requests yield
  one run/record and conflicting requests yield one 201 and one 409. Backend PID
  assertions confirm separate connections. Atomic failure leaves neither record;
  unrelated unique errors remain 503. A deliberately held key lock produces
  bounded 503, releases resources, and permits a subsequent successful retry.
- Header boundaries/duplicates, semantic JSON/UUID equivalence, fingerprint
  template sensitivity, user/case scopes, resource authorization before conflicts,
  revoked replay, legacy run retrieval, existing constraints, disposable-only
  downgrade/re-upgrade with pre-existing runs, and metadata comparison passed.
- Docker build and quiet Compose configuration validation passed.
- Confirmed Compose target postgres:5432 / lavan_dev at 0004_runs before explicit
  upgrade. Existing counts (7 users, 3 projects, 2 artifacts, 1 run, 7 tokens)
  were unchanged by migration. Current and head now equal 0005_run_idempotency;
  Alembic check reports no pending operations. No applied migration was edited.

Actual HTTP checks against the rebuilt Compose API and real development PostgreSQL:

| Check | HTTP | Seconds |
| --- | --- | --- |
| Create project | 201 | 0.123325 |
| Upload artifact 1 | 201 | 0.055966 |
| Upload artifact 2 | 201 | 0.030609 |
| Create run | 201 | 0.036582 |
| Replay with reordered/formatted JSON | 201 | 0.015283 |
| Conflicting valid artifact, same key | 409 | 0.012318 |
| Other user attempts create/replay | 404 | 0.013028 |
| Concurrent identical request 1 | 201 | 0.032519 |
| Concurrent identical request 2 | 201 | 0.032012 |
| Retrieve owned run | 200 | 0.012559 |
| Revoked-token replay | 401 | 0.013116 |
| Liveness | 200 | 0.001445 |
| Readiness | 200 | 1.411961 |

Asserted exact replay body and Location equality, concurrent response equality,
and exactly two persisted runs plus two retry records for the two successful
keys; the conflict left no extra run. Tokens and raw keys stayed in memory and
were not printed or stored in verification files. Retained local records:
- User A: de4f5fa9-c359-4902-a2ed-ca412ceef206
- User B: 92b7dfa7-4466-46bc-a1d5-409e09580d65
- Project: 8705ca91-9b63-46d6-9901-a454287aaafc
- Artifacts (private files retained): 22ed435a-774c-43ce-946e-a5a02a752984,
  11e444d6-42c2-48dc-8fbc-fc7680182fd8
- Sequential/replayed run: ea850f7a-7e47-4fa8-a8f5-0761f7b2432d
- Concurrent run: dcbf07c5-d936-4b46-8ef1-0cbd8005c9fa
- Revoked token IDs: 860f0ddb-3db8-485a-8e12-ff186ffbc540,
  571beb74-3aaa-4af9-93bd-0ab5fe8b622d

Finally revoked both verification tokens and restored API and PostgreSQL to
stopped, matching initial state. Development data, artifact files, and both
project volumes were preserved; no development downgrade/reset/stamp occurred.
No checks remain blocked. Keys remain valid while retained; future lifecycle
work must explicitly revisit replay representation before making status mutable.
Next milestone: lifecycle processing with a simulated runner. It has not started.


## Milestone 6C — durable worker and simulated lifecycle (verified)

Implemented PostgreSQL execution_jobs, a separate Compose worker, versioned runner
interface, and nonblocking SimulatedRunner. Success follows queued → preparing →
starting → running → completed; any nonterminal state can fail. Shared transitions
lock job then run, verify lease owner/fence/expiry, allocate per-run event ordering,
and atomically persist state/event/job result. Terminal runs cannot be revived.
Events have stable UUIDs; there is no log stream or event endpoint.

New creation commits run, one job, idempotency record, and immutable queued response
snapshot together. Creation replay remains 201 with the original Location/body,
while GET returns current state. Fingerprint v1 is unchanged. Old queued runs and
snapshots are preserved without automatic job backfill; explicit local enqueue-run
is required to schedule their simulation, idempotently while queued.

Claims use FOR UPDATE SKIP LOCKED. Processing claims increment attempts/fence; the lease
is 5s, renewed each nonblocking processing tick. Renewal's SQL predicate rejects
already expired leases. Runner operations/waits occur outside transactions.
Infrastructure failures use durable bounded retry scheduling, with three claims
per job including crashes; exhaustion produces failed/retries_exhausted. Application
startup failure is immediately terminal. First-claim scenario/timing and the
persisted start anchor reconcile the same stable logical operation after crashes.
No exactly-once physical execution is claimed. Graceful shutdown leaves leased
work recoverable. The worker has no artifact-volume mount and never opens uploads.

Verification results:
- **208 tests passed, none skipped**, using LAVAN_POSTGRES_TESTS=1 and real
  PostgreSQL disposable databases. One existing Starlette/HTTPX deprecation
  warning remains. No dependency change was needed.
- Success, startup_failure, slow_startup; all failure origins and terminal
  protection; concurrent separate-connection claimers and two independent worker
  processes; transition/event/job rollback; atomic run/job/key creation; idempotent
  replay without extra jobs; unchanged ownership/token behavior passed.
- A real Python worker was SIGKILLed after persisting starting; a second process
  reclaimed after lease expiry, completed on attempt 2, retained the original
  start anchor/scenario despite different restart configuration, and preserved
  existing event IDs/order without duplicate phases.
- A separate slow-start process renewed beyond the original lease and completed
  on attempt 1; another claimant could not acquire it. A deliberately delayed
  runner result was rejected after a higher-fence worker completed the job.
  SIGTERM recovery, stale transition/renewal/retry rejection, retry scheduling,
  infrastructure and crash exhaustion, and explicit management enqueue passed.
- Empty upgrade/downgrade/re-upgrade, legacy queued runs/key snapshot backfill
  (including zero/nonzero timestamp microseconds), no job backfill, guard rejection
  of a false queued assumption, existing constraints, and metadata comparison
  passed. All destructive migration checks used verified disposable databases.
- Ruff lint/format, uv sync --locked, quiet Compose validation, and API/worker
  Docker builds passed. The retry timing assertion was tightened to inspect the
  durable schedule rather than assume immediate host scheduling; its focused
  regression passed after the full suite.
- Confirmed Compose postgres:5432 / lavan_dev at 0005_run_idempotency. Pre-migration
  counts (9 users, 4 projects, 4 artifacts, 3 queued runs, 9 tokens, 2 retry records)
  were unchanged. Verified all backfilled snapshots and zero jobs before worker
  startup. Applied only upgrade head to 0006_simulated_lifecycle; current equals
  head and Alembic check reports no pending operations. Applied migrations were
  not edited. No development downgrade/reset/stamp occurred.

Actual HTTP/Compose worker smoke checks:

| Check | HTTP | Seconds | Result |
| --- | --- | --- | --- |
| Create project | 201 | 0.182051 |  |
| Upload inert ZIP | 201 | 0.050274 |  |
| Create success run | 201 | 0.045561 |  |
| Poll success to completed | 200 | 0.677344 | completed; observed queued → preparing → starting → running → completed |
| Replay after completed | 201 | 0.016514 |  |
| Cross-user GET | 404 | 0.015915 |  |
| Cross-user replay attempt | 404 | 0.019397 |  |
| Create startup-failure run | 201 | 0.037370 |  |
| Poll startup failure | 200 | 1.845684 | failed; observed queued → preparing → starting → failed |
| Replay after failed | 201 | 0.019707 |  |
| Revoked-token replay | 401 | 0.013880 |  |
| Liveness | 200 | 0.002113 |  |
| Readiness | 200 | 1.408607 |  |

Success polling observed every forward state; failure polling observed queued →
preparing → starting → failed. Replays after both terminal states matched their
original queued creation bodies exactly. Each run had exactly one job and one
attempt; success had four ordered events, failure three. Startup failure used a
local Compose environment override; no scenario was accepted in an API request.
The three existing queued runs stayed unscheduled. Both health bodies were checked.

Retained local verification records (tokens and raw keys were never printed):
- users: c82d4cb8-fabe-4db0-8e67-e901a34ee83e, 0564d809-2226-4565-bd09-64ec734d06d9
- project: 7f902e91-9348-4232-b481-e0066599ceed
- artifact: d9b0626a-3f55-4d3a-bbb4-8b80ec90acd5
- success_run: 9e5d5143-faf5-4493-82be-8e9b1569df7d
- failed_run: f4b5ffb9-597d-469a-9c83-6dfaf65b6ce1
- revoked_token_ids: 16b71ebb-1777-49d6-97bb-866b124870af, baf3e1dd-e810-4695-b453-e623e98e2086
- Job 51491498-4784-4036-b2d9-795a0e396628: run 9e5d5143-faf5-4493-82be-8e9b1569df7d, 1 attempt, events preparing → starting → running → completed.
- Job e115b333-284e-4bb9-b2bb-0520b5baf4c3: run f4b5ffb9-597d-469a-9c83-6dfaf65b6ce1, 1 attempt, events preparing → starting → failed.

Both verification tokens were revoked in final cleanup. Successful project,
artifact file, runs, jobs, retry records, and events are retained as explicit local
test records. Existing data and both project volumes were preserved. API and
PostgreSQL were restored to stopped; the new worker is also stopped, with its
container configuration restored to the default success scenario without starting
it. API/worker logs had no unexpected errors. No checks remain blocked.

Limitations: simulated execution only; no app listener, source reads, extraction,
install, real execution, networking, preview URLs, cancellation, expiration, SSE,
or frontend. The serial runner adapter requires immediate nonblocking operations;
a future remote runner needs operation deduplication, fencing, bounded RPCs, and
status reconciliation. Processing is at-least-once, not exactly-once physical
execution. Internal events and error codes have no new public endpoint.
Milestone 6C is complete; no later milestone or real-runner work has started.
