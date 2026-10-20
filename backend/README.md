# Lavan backend: milestones 1–7B

Local FastAPI infrastructure with minimal users/projects models and explicit
Alembic migrations. Python 3.12, PostgreSQL 17, SQLAlchemy, Pydantic settings,
and three Compose services (API, PostgreSQL, simulated worker). Opaque API-token
authentication, projects, private ZIP artifacts, and durable simulated execution
requests. No uploaded code is read or executed.
Run commands in WSL from `/home/agent/lavan/backend`.

## Setup

Install Python 3.12 with venv support and Docker Desktop with integration enabled
for your WSL distribution (or Docker Engine and its Compose plugin in WSL).
`docker info` must succeed as your user. If it reports socket permission denied,
configure Docker access first; for a WSL Docker Engine installation an administrator
can run `sudo usermod -aG docker "$USER"`, then reopen the WSL session.

```bash
cd /home/agent/lavan/backend
python3 -m venv /tmp/lavan-uv
/tmp/lavan-uv/bin/pip install uv==0.12.22
export PATH="/tmp/lavan-uv/bin:$PATH"
uv sync --locked
cp .env.example .env
python3 - <<'PY'
from pathlib import Path
import secrets
path = Path('.env')
path.write_text(path.read_text().replace(
    'replace-with-a-generated-local-password', secrets.token_hex(32)))
path.chmod(0o600)
PY
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Generate `.env` once; retain its credentials when reusing an existing database
volume. Changing the environment does not change an initialized database password.
Do not commit `.env`. Required settings fail validation at API startup without
printing input values. Compose uses the internal PostgreSQL hostname; `.env` uses
localhost for optional host execution (`uv run uvicorn lavan.api:app --host
127.0.0.1 --port 8000 --no-access-log` while only `docker compose up -d postgres` is running).

## Run and verify against real PostgreSQL

```bash
docker compose up --build -d --wait
curl --fail-with-body --max-time 10 http://127.0.0.1:8000/health/live
curl --fail-with-body --max-time 10 http://127.0.0.1:8000/health/ready
docker compose stop postgres
curl --fail-with-body --max-time 10 http://127.0.0.1:8000/health/live
curl --max-time 10 -sS -w '\nHTTP %{http_code}; seconds %{time_total}\n' \
  http://127.0.0.1:8000/health/ready
docker compose up -d --wait postgres
curl --fail-with-body --max-time 10 http://127.0.0.1:8000/health/ready
```

Expected: live 200/alive, ready 200/ready, live remains 200 during the outage,
ready becomes 503/unavailable within 10 seconds, then ready recovers to 200.
The complete probe (DNS, connection, query, and cleanup) has a 2-second deadline.
A disposable Python subprocess performs blocking SQLAlchemy and psycopg calls;
the API asynchronously waits for it. On timeout it kills and reaps the child,
targeting an HTTP response within 3 seconds under normal local scheduling.
This closes sockets even if DNS or the driver stalls, without abandoning threads.
Only one probe per API process runs at a time; overlapping readiness requests
return 503 immediately instead of queuing. Liveness never waits for a probe.

Driver safeguards: connection timeout 2 seconds per address, statement timeout
1 second, TCP user timeout 1.5 seconds. NullPool has no acquisition queue and
retains no stale sockets. Completed probes close connections and dispose engines;
each new probe uses a fresh connection so recovery needs no API restart.
Probe output is discarded to avoid exposing credentials or internal errors.

Published ports are localhost only (8000 and 5432). PostgreSQL has a health check
and uses the project-scoped `lavan-local_postgres_data` volume.

## Shutdown

```bash
docker compose down
```

Normal shutdown preserves the database. **`docker compose down --volumes` is
destructive: it deletes this project's development database.** Never use global
Docker pruning for this project.

## Readiness regression verification

Keep the existing `.env` and volume. Record initial service state with
`docker compose ps --all`, rebuild the API, and run bounded assertions:

```bash
docker compose up --build -d --wait --wait-timeout 120
api_before=$(docker compose ps -q api)
check_health() {
  python3 - "$1" "$2" <<'PY'
import json
import sys
import time
import urllib.error
import urllib.request
path, expected = sys.argv[1], int(sys.argv[2])
started = time.monotonic()
try:
    response = urllib.request.urlopen('http://127.0.0.1:8000' + path, timeout=3)
except urllib.error.HTTPError as error:
    response = error
with response:
    status = response.status
    body = json.loads(response.read())
elapsed = time.monotonic() - started
print(f'{path}: HTTP {status}, {elapsed:.6f}s, {body}')
assert status == expected
assert elapsed < 3
assert body == {'status': 'alive' if path.endswith('live') else
                'ready' if expected == 200 else 'unavailable'}
PY
}
check_health /health/live 200
check_health /health/ready 200
docker compose stop --timeout 10 postgres
for attempt in 1 2 3; do
  check_health /health/ready 503
  check_health /health/live 200
done
docker compose up -d --wait --wait-timeout 60 postgres
test "$api_before" = "$(docker compose ps -q api)"
check_health /health/ready 200
check_health /health/live 200
```

The API ID must remain unchanged across outage and recovery. If a check fails,
restart PostgreSQL with the bounded command above. Restore prior service state:
if both were stopped, use `docker compose stop --timeout 10`; if both were running,
leave them running. For a mixed initial state, stop only initially stopped
services. Never delete volumes or database data.

## Milestone 2: users/projects and explicit migrations

See [docs/schema.md](docs/schema.md) for columns, ownership constraints, deletion
policy, and UTC timestamp behavior. Milestone 2 added no authentication or business endpoints; token authentication is described below.
Neither startup nor readiness applies migrations automatically.

From WSL, with PostgreSQL running and the existing `.env` retained:

```bash
cd /home/agent/lavan/backend
export PATH="/tmp/lavan-uv/bin:$PATH"  # Or your existing uv installation.
uv sync --locked
docker compose up -d --wait --wait-timeout 60 postgres
uv run alembic upgrade head
uv run alembic current
uv run alembic check
```

The WSL command uses the existing POSTGRES_* settings (`POSTGRES_HOST=127.0.0.1`
for host execution). Credentials stay in environment/.env and never in alembic.ini.
For Docker execution, rebuild the API image first; Compose supplies the internal
`postgres` hostname and the same configured credentials:

```bash
docker compose build api
docker compose run --rm --no-deps api /app/.venv/bin/alembic upgrade head
docker compose run --rm --no-deps api /app/.venv/bin/alembic current
```

Choose either migration path. Upgrading to head is idempotent. Never downgrade or
reset the development database to test migrations, and never delete its volume.

### Real PostgreSQL integration tests

```bash
uv sync --locked
uv run ruff check .
uv run ruff format --check .
LAVAN_POSTGRES_TESTS=1 uv run pytest
```

The test role (from existing settings) needs CREATEDB permission. The suite
creates a new `lavan_m2_test_<random UUID>` database using template0, verifies the
actual connected database before downgrade, and drops only the database it just
created in fixture cleanup. It does not migrate, truncate, or downgrade the
configured development database. Connection/query waits are bounded. Failed
connections fail tests; PostgreSQL is never replaced by SQLite. Without the
explicit flag, schema tests are skipped; those skips are not integration passes.
Tests cover schema upgrade/idempotence, constraints, timestamps, metadata drift,
and downgrade/re-upgrade. No separate test database credentials are committed.

After changes, rebuild/start the API and recheck health using the earlier README
commands, then restore the initial service state without deleting volumes.

## Milestone 3: development users and API tokens

Run migrations first. There is no registration endpoint and no automatic user or
token creation. Tokens are opaque `lv_` values with 32 cryptographically random
bytes (256 bits). Only SHA-256 of the complete token is stored. Tokens have **no
expiration in this initial policy**: they remain valid until explicitly revoked.
Revocation is idempotent; it does not delete the token or its user. A user with
any associated token, including revoked tokens, cannot be deleted.

Host commands from backend/ (using the existing `.env` and locked environment):

```bash
uv run python -m lavan.manage create-user
USER_ID="<returned-user-uuid>"
set +x  # Keep secret-bearing variable expansions out of shell tracing.
ISSUED=$(uv run python -m lavan.manage issue-token "$USER_ID")
TOKEN_ID=$(printf '%s' "$ISSUED" | python3 -c 'import json,sys; print(json.load(sys.stdin)["token_id"])')
API_TOKEN=$(printf '%s' "$ISSUED" | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
unset ISSUED
```

Each command emits JSON on stdout. Issuance emits the raw token **only after the
transaction commits**. Capture it securely; do not echo the variable, paste its
literal value into shell history, or commit it. Unknown users/tokens produce a
sanitized error on stderr and a nonzero exit status. Invalid UUID arguments fail
argument validation. Database failures never print credentials or internal errors.

Docker equivalents (after `docker compose build api`); choose host or Docker
commands, rather than issuing twice unintentionally:

```bash
docker compose run --rm --no-deps -T api /app/.venv/bin/python -m lavan.manage create-user
# Capture this command's stdout as ISSUED, then parse it as above:
ISSUED=$(docker compose run --rm --no-deps -T api /app/.venv/bin/python -m lavan.manage issue-token "$USER_ID")
docker compose run --rm --no-deps -T api /app/.venv/bin/python -m lavan.manage revoke-token "<token-uuid>"
```

Use only the Authorization header, conceptually `Authorization: Bearer <token>`.
The following sends it via curl stdin so the token is absent from command history
and curl process arguments. API responses include only the user's `id` and
`created_at`, never token identifiers, raw tokens, or digests.

```bash
docker compose up -d --wait --wait-timeout 60
printf 'header = "Authorization: Bearer %s"\n' "$API_TOKEN" |
  curl --config - --fail-with-body --max-time 5 http://127.0.0.1:8000/v1/me
uv run python -m lavan.manage revoke-token "$TOKEN_ID"
# Repeat revocation safely:
uv run python -m lavan.manage revoke-token "$TOKEN_ID"
# Expected HTTP 401 with WWW-Authenticate: Bearer after revocation:
printf 'header = "Authorization: Bearer %s"\n' "$API_TOKEN" |
  curl --config - -i --max-time 5 http://127.0.0.1:8000/v1/me
unset API_TOKEN TOKEN_ID
curl --fail-with-body --max-time 3 http://127.0.0.1:8000/health/live
curl --fail-with-body --max-time 3 http://127.0.0.1:8000/health/ready
```

Missing, malformed, unknown, and revoked tokens return the same sanitized 401.
Query parameters are never credentials. Unexpected database errors return a
sanitized 503, not 401. Health endpoints remain unauthenticated. Synchronous
SQLAlchemy dependencies and /v1/me run in FastAPI's worker pool, using scoped
sessions closed after requests. The separate bounded readiness probe is unchanged.
The Docker command disables Uvicorn access logs to avoid recording query strings;
use `--no-access-log` for host Uvicorn too. Never enable header/token logging.

Run `LAVAN_POSTGRES_TESTS=1 uv run pytest` for real disposable-database tests,
plus the existing Ruff checks. The new revision is `0002_api_tokens`, following
`0001_users_projects`. Only disposable tests downgrade. Restore your prior API
service state after manual verification, preserve volumes, and retain explicitly
identified verification users/revoked tokens rather than deleting unrelated data.

## Milestone 4: authenticated project creation and retrieval

`POST /v1/projects` accepts only `{"name": "..."}`. Names are trimmed before
validation and must contain 1–100 characters after trimming. Duplicate names are
allowed. Extra fields (including id and owner_id), missing/non-string names, and
invalid lengths return 422. Ownership always comes from the Bearer token.

Successful creation returns 201 with a relative `Location: /v1/projects/<uuid>`
header. Project responses contain exactly id, name, created_at, and updated_at.
`GET /v1/projects/<uuid>` returns 200 only for the owner. Another user's project
and a nonexistent UUID return the same 404 `{"detail":"Project not found"}`.
Malformed UUIDs return 422 for authenticated callers. Both endpoints require
valid, unrevoked tokens (otherwise sanitized 401 with WWW-Authenticate: Bearer).
Database failures return sanitized 503. No listing/editing/deletion is available.

Use the milestone-3 issuance instructions to hold API_TOKEN and TOKEN_ID in shell
variables without placing a literal secret in history. Then:

```bash
docker compose up --build -d --wait --wait-timeout 120
set +x
printf 'header = "Authorization: Bearer %s"\n' "$API_TOKEN" |
  curl --config - --include --fail-with-body --max-time 5 \
    --header 'Content-Type: application/json' \
    --data '{"name":"  My project  "}' \
    http://127.0.0.1:8000/v1/projects
PROJECT_ID="<project-uuid-from-response>"
printf 'header = "Authorization: Bearer %s"\n' "$API_TOKEN" |
  curl --config - --include --fail-with-body --max-time 5 \
    "http://127.0.0.1:8000/v1/projects/$PROJECT_ID"
uv run python -m lavan.manage revoke-token "$TOKEN_ID"
unset API_TOKEN TOKEN_ID
```

Do not echo tokens or put them in URLs. For ownership verification, repeat the GET
with a second user's token and expect the identical sanitized 404 response used
for nonexistent projects. Revoke both verification tokens afterward. Retain
explicitly identified local verification users/projects and restore the prior
service state without deleting data or volumes.

Milestone 4 required no schema or dependency change; its head was
`0002_api_tokens`. Milestone 5 below advances it to `0003_artifacts`.
Run `LAVAN_POSTGRES_TESTS=1 uv run pytest` and the existing Ruff/lock checks.

## Milestone 5: private ZIP artifacts

Only these endpoints are added:
- `POST /v1/projects/<project-uuid>/artifacts`, raw `application/zip` body.
- `GET /v1/projects/<project-uuid>/artifacts/<artifact-uuid>`, metadata only.

Both require a valid Bearer token and project ownership. Ownership is checked
before reading the upload body, and that short database session closes before
transfer. Metadata lookups also verify that the artifact belongs to the project.
Missing, mismatched, and inaccessible resources return the same sanitized 404.
No download/static route, extraction, decompression, installation, or execution
exists. Responses expose only id, project_id, size_bytes, sha256, file_count,
declared_uncompressed_bytes, created_at. Creation returns 201 and Location only
after validation and metadata commit. file_count excludes directories; entry
limits include directories. SHA-256 covers the complete uploaded ZIP bytes.

Apply the explicit migration to head using the existing migration instructions
(`0003_artifacts`). Do not downgrade development. With API_TOKEN held securely as
in milestone 3, PROJECT_ID set to an owned project's UUID, and a local ZIP file:

```bash
set +x
printf 'header = "Authorization: Bearer %s"\n' "$API_TOKEN" |
  curl --config - --include --fail-with-body --max-time 70 \
    --header 'Content-Type: application/zip' --data-binary @/path/to/archive.zip \
    "http://127.0.0.1:8000/v1/projects/$PROJECT_ID/artifacts"
ARTIFACT_ID="<artifact-uuid-from-response>"
printf 'header = "Authorization: Bearer %s"\n' "$API_TOKEN" |
  curl --config - --fail-with-body --max-time 5 \
    "http://127.0.0.1:8000/v1/projects/$PROJECT_ID/artifacts/$ARTIFACT_ID"
# Revoke verification tokens afterward using their TOKEN_ID; never print secrets.
```

HTTP responses: 401 invalid credentials; 404 resource unavailable; 415 unsupported
Content-Type; 413 actual received bytes exceed compressed limit; 422 invalid,
empty, unsafe, or unsupported ZIP; 408 upload deadline; 400 interrupted upload
(if the client can still receive a response); 503 capacity/storage/database
unavailable. Client filenames, storage keys/paths, and internal errors are never
returned. Content-Length is not trusted. Requests are processed in at most
64-KiB write/hash chunks; no multipart parser or full-body buffering is used.

### Limits and private storage

Environment settings (bytes unless stated); Compose forwards all limit settings
from `.env` using the same defaults:

| Setting | Default |
| --- | --- |
| ZIP_MAX_COMPRESSED_BYTES | 52428800 (50 MiB) |
| ZIP_MAX_UNCOMPRESSED_BYTES | 209715200 (200 MiB declared total) |
| ZIP_MAX_ENTRIES | 2000 (files plus directories) |
| ZIP_MAX_FILE_BYTES | 52428800 (50 MiB declared per file) |
| ZIP_MAX_FILENAME_CHARS | 512 decoded characters |
| UPLOAD_MAX_SECONDS | 60 |
| UPLOAD_MAX_ACTIVE | 2 per API process |

The deadline starts when an upload acquires capacity and bounds receiving the
body, with checks around bounded filesystem/validation work. Interrupted or
cancelled blocking work is drained before cleanup, never left in the background;
filesystem/kernel stalls and final database commit/cleanup can extend wall time.
One capacity slot is held through validation and persistence. Excess concurrent
uploads return 503 instead of waiting. These are **per-process** limits: multiple
workers/containers multiply concurrency and require a future coordinated/global
limit if deployed that way. Validation is also limited by these same slots.

Compose persists files in `lavan-local_artifact_data`, mounted privately at
`/var/lib/lavan/artifacts`, owned by the non-root API user. Host execution defaults
to `~/.local/share/lavan/artifacts`; ARTIFACT_STORAGE_DIR can select another
absolute dedicated directory outside the source repository. Do not use a source
bind mount. Directories use mode 0700 and files 0600. Do not manually change volume
ownership or publish it through a web server. Normal compose stop/down preserves
both volumes; down --volumes deletes data and must not be used for verification.

Uploads stage into server-generated `.UUID.part` files. After validation and
fsync, an atomic same-filesystem hard-link publishes UUID.zip without overwriting
an existing file; the temporary link is removed. Metadata then commits. Rejected,
oversize, interrupted, timed-out, and ordinary failed uploads clean their files.
Known metadata rollback removes the newly published file. A lost COMMIT
acknowledgement is checked using a fresh session: a referenced file is preserved;
if the database is unreachable, the file is conservatively retained for later
reconciliation rather than risking deletion of referenced data. Such an uncertain
request may return 503 even though the metadata transaction committed.

### ZIP metadata policy and limitations

Only ordinary single-disk, non-ZIP64 ZIPs using STORED or DEFLATED are supported.
Central and local header metadata, declared byte ranges, and optional data
descriptors must agree; truncated, overlapping, prefixed/self-extracting, gapped,
trailing-data, encrypted, or unsupported layouts are rejected. Parsing checks the
entry count and record/name bounds while scanning rather than first building an
unbounded directory list. Payload bytes are never inflated or read to verify CRC.

Original raw filename bytes are inspected before library filename truncation can
hide NULs, then decoded using UTF-8 when flagged, otherwise CP437. Names are POSIX
relative paths. NFC-normalized paths are compared case-sensitively for duplicates.
Backslashes, NUL/control/format characters, absolute/UNC/drive paths, empty, dot or
dot-dot components, repeated slashes, Windows-reserved names/punctuation, and
components ending in dots/spaces are rejected. A single terminal slash denotes a
directory. Explicit duplicate directories, file/directory conflicts (including
implied parent directories), symlinks and special-file modes are rejected. Nested
regular files and ordinary directories are accepted; at least one file is required.

This is **preliminary metadata validation**, not proof of payload integrity or
sandbox safety. A future runner must independently enforce actual decompressed
byte counts, safe extraction containment, CRC/integrity, resource limits, and safe
handling of filenames/permissions on its target filesystem. This milestone does
not perform any of those future execution steps.

### Conservative local reconciliation

Filesystem publication and PostgreSQL commit are not a single transaction. A
crash before publication can leave a .part file; a crash after publication but
before commit can leave an unreferenced .zip (or both hard links). A committed
row/file may exist even if the client never received its response. Keep storage
and database backups consistent. Reconciliation does not repair missing referenced
files or prove payload integrity.

Use the SAME database and storage configuration as the API. Host and Compose
storage roots differ; use Compose for its volume. Ensure PostgreSQL is running
(`docker compose up -d --wait --wait-timeout 60 postgres`) before reconciliation:

```bash
# Default: dry-run, default minimum age 86400 seconds (one day).
docker compose run --rm --no-deps -T api /app/.venv/bin/python -m lavan.reconcile
# Explicit deletion after reviewing dry-run; hard minimum age is 3600 seconds.
docker compose run --rm --no-deps -T api /app/.venv/bin/python -m lavan.reconcile \
  --delete --min-age-seconds 86400
# Host alternative, only for the host API's configured storage root/database:
uv run python -m lavan.reconcile
```

The command holds an exclusive storage-wide advisory lock from metadata snapshot
through deletion. Uploads hold a shared lock through commit and cleanup. If any
upload is active, reconciliation refuses without deleting anything; if reconciliation
holds the lock, new uploads fail safely with 503. This works across cooperating
processes sharing the same volume and lock file. Dry-run never changes archive
files; it may initialize the reserved coordination lock file. Missing roots return
an empty result. Only recognized UUID .zip/.part regular files contained within
the verified root are eligible. Referenced, recent, unknown, symlink, and active
files are preserved. No recursive deletion or database modification occurs. Never
point this command at an unrelated directory or remove the lock file manually.

`LAVAN_POSTGRES_TESTS=1 uv run pytest` uses disposable databases and isolated
pytest storage. No test uploads or real tokens are committed to Git.

## Milestone 6A: persisted execution requests (queued only)

`POST /v1/projects/<project-uuid>/runs` accepts exactly:

```json
{"artifact_id":"<artifact-uuid>","runtime_template":"python-fastapi-v1"}
```

Use an artifact from that same owned project. The selected runtime template is
explicit, **not proof that the uploaded source is a compatible FastAPI app**.
No source is read, inspected, extracted, installed, or executed to check it.
Only python-fastapi-v1 is accepted. Status, owner_id, resource limits, and all
other fields are rejected. Malformed UUIDs/missing fields/invalid templates return
422. Invalid credentials return 401. Inaccessible/missing projects and missing or
mismatched artifacts all return the same sanitized 404 `{"detail":"Not found"}`.

Successful creation returns **201**, not 202: it creates a resource without any
asynchronous dispatch. Location is `/v1/projects/<project-uuid>/runs/<run-uuid>`.
`GET` of that URL returns the owned run, with fields id, project_id, artifact_id,
status, runtime_template, created_at. Missing runs, URL/project mismatches, and
other-owner access return the same sanitized 404. No filesystem paths, storage
keys, source, or user ownership details are returned.

After explicit `alembic upgrade head` using the existing WSL or Compose commands,
head is `0007_run_cancellation`. With an API_TOKEN held securely as described
in milestone 3:

```bash
PROJECT_ID="<owned-project-uuid>"
ARTIFACT_ID="<artifact-uuid-in-that-project>"
set +x
IDEMPOTENCY_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(16))')
printf 'header = "Authorization: Bearer %s"\nheader = "Idempotency-Key: %s"\n' \
  "$API_TOKEN" "$IDEMPOTENCY_KEY" |
  curl --config - --include --fail-with-body --max-time 5 \
    --header 'Content-Type: application/json' \
    --data "{\"artifact_id\":\"$ARTIFACT_ID\",\"runtime_template\":\"python-fastapi-v1\"}" \
    "http://127.0.0.1:8000/v1/projects/$PROJECT_ID/runs"
RUN_ID="<run-uuid-from-response>"
printf 'header = "Authorization: Bearer %s"\n' "$API_TOKEN" |
  curl --config - --fail-with-body --max-time 5 \
    "http://127.0.0.1:8000/v1/projects/$PROJECT_ID/runs/$RUN_ID"
# Revoke verification tokens afterward using their TOKEN_ID, then unset secrets.
```

**Milestones 6A/6B originally left runs queued. Milestone 6C adds the simulated
worker described below.** Existing runs remain unscheduled until explicitly
enqueued. There is still no real execution or expiration processing,
logging stream, listing, or active-run quota.
Milestone 6B requires Idempotency-Key on creation, as described below.
Different keys intentionally create separate records; keep the original key and
payload when retrying an uncertain submission (a timeout can occur after commit).

The database also enforces artifact/project consistency via a composite FK and
rejects deletion of referenced artifacts/projects without cascades. Status and
template constraints allow only the values above; later migrations will extend
them as lifecycle processing is introduced. Real PostgreSQL tests include direct
constraint violations and disposable-only downgrade/re-upgrade checks. Restore
service state after verification and preserve all development volumes/records.
Milestone 6C below implements simulated lifecycle processing.


## Milestone 6B: idempotent run creation

POST run creation requires exactly one `Idempotency-Key` header: 1–128 ASCII
letters, digits, hyphens, underscores, or periods. Keys are case-sensitive and
are never trimmed. Missing, empty, invalid, or duplicate headers return sanitized
422. Keep keys private; do not enable shell tracing or log header values.

Keys are scoped to the authenticated user and the `run.create` operation across
all their projects. They are retained indefinitely; no expiry or cleanup exists.
The same key and semantic payload return **201**, the same Location, and the
same stored creation response, even after lifecycle completion. JSON spacing/order
and UUID letter case do not matter.
A different valid project, artifact, or template with that key returns sanitized
409. Unsupported templates still return 422. Authentication and resource
ownership/artifact membership are checked before replay/conflict decisions, so
inaccessible resources remain 404 and revoked tokens remain 401.

Reuse both the original key and payload after a timeout or uncertain outcome.
The example above generates a key without echoing it and supplies it through
curl's configuration input; keep the variable unchanged for retries. Use a new
key only when a separate run is intended. Do not put literal tokens or keys in
shell history. After verification, revoke the token and unset both variables.

A PostgreSQL scope primary key arbitrates concurrent writers. Run creation and
its retry record commit atomically: identical concurrent submissions resolve to
one persisted run; conflicting submissions have one winner and a 409 loser.
Lock waits are bounded at 1.5 seconds (statements at 2 seconds); database outages
or contention may return sanitized 503. Retry uncertain results with the same
key/payload. No in-memory lock is used. Fingerprints use versioned canonical JSON
and SHA-256, not raw request bytes. Existing runs remain readable without keys.

Milestone 6C makes status mutable: replay now returns the immutable queued
creation snapshot, while GET returns current lifecycle state. A replay never
schedules another job or restarts processing.

Apply explicitly with `docker compose run --rm --no-deps api /app/.venv/bin/alembic upgrade head`
after rebuilding the API. Current head is `0007_run_cancellation`. Integration
checks use `LAVAN_POSTGRES_TESTS=1 uv run pytest` and disposable PostgreSQL
databases, including synchronized concurrent writers and bounded lock contention.


## Milestone 6C: durable simulated lifecycle

New run creation commits the run, one `execution_jobs` row, idempotency record,
and original six-field queued response snapshot together. POST/replay still
returns 201 and the original Location/body after completion; GET at that Location
shows current status. Fingerprint v1 is unchanged. Authentication, ownership,
artifact membership, and conflict checks still apply on every replay.

The migration verifies that all old runs are queued and backfills their creation
snapshots. **It does not enqueue old runs.** Apply explicitly before starting the
worker; never downgrade or reset development data:

```bash
docker compose up -d --wait --wait-timeout 60 postgres
docker compose build api worker
docker compose run --rm --no-deps api /app/.venv/bin/alembic upgrade head
docker compose run --rm --no-deps api /app/.venv/bin/alembic current
docker compose run --rm --no-deps api /app/.venv/bin/alembic heads
docker compose up -d --wait --wait-timeout 60 api worker
# Inspect service state and identifier-only worker messages:
docker compose ps
docker compose logs --tail 30 worker
```

The only forward path is `queued → preparing → starting → running → completed`.
Any nonterminal state may become `failed`. Terminal states cannot be revived.
Transitions lock the job and run, verify the live lease and fencing identity,
and commit the run, ordered UUID transition event, and job result atomically.
Events are internal transition records, with no streaming/logging API. Run
updated_at and sanitized error_code are persisted internally; creation/replay keeps its existing six fields. Milestone 7A adds the nullable
intent timestamp only to GET/cancellation responses.

`running` means a simulated test execution started; `completed` means it finished.
**No app listens on a port. No upload is opened, extracted, installed, or executed.**
A versioned runner request contains UUIDs, checksum, runtime template, stable
operation ID, attempt, and fence; see [runner contract](docs/runner-contract.md).

Workers claim one job at a time using `FOR UPDATE SKIP LOCKED`. Processing is
at-least-once. Each processing claim increases attempts and fence, with a 5-second default
lease renewed every tick; polling is 0.1 seconds by default and is capped to a
third of the lease during processing. Database time determines expiry. Runner
calls and waits occur outside transactions. Paused/stale owners cannot renew
expired leases or apply results after reclamation. A worker crash leaves a lease
that another worker can reclaim; SIGTERM stops claims and leaves in-flight work
recoverable. There is no claim of exactly-once physical execution.

Three attempts are allowed per job, including crashed claims. Infrastructure
errors schedule a durable retry after `min(30, WORKER_RETRY_SECONDS * attempts)`
seconds (base 1 second). Application startup failure is terminal immediately.
An exhausted lease is finalized with a higher fence without another execution
attempt. Exhausted errors/crash leases produce failed with retries_exhausted. Other stored
codes are simulated_startup_failure and worker_error; no exception details are
stored or logged. Connect/statement/lock waits are bounded; database outages
leave the lease for later recovery.

Local configuration selects `SIMULATOR_SCENARIO=success`, `startup_failure`, or
`slow_startup`; no request can select a command or scenario. Startup (default
0.1s, maximum 10s), slow startup (3s, maximum 30s), and duration (0.2s, maximum
10s) are configurable. The scenario and timing are frozen on first claim, along
with a persisted logical start anchor. Recovery reconciles that same execution
instead of restarting its timer. Lease range is 0.2–60s; poll 0.01–1s; retry base
0.05–30s. Defaults and variable names are in .env.example.

To demonstrate failure for **new, unclaimed jobs**, stop the worker, recreate it
with a local environment override, then create a new run using the authenticated
POST example above and poll its Location with authenticated GET:

```bash
docker compose stop worker
SIMULATOR_SCENARIO=startup_failure docker compose up -d --no-deps --force-recreate worker
# After the new run reaches failed, restore the default success configuration:
docker compose stop worker
SIMULATOR_SCENARIO=success docker compose up -d --no-deps --force-recreate worker
# For slow startup instead, use SIMULATOR_SCENARIO=slow_startup above.
```

Old queued runs require explicit local administration; this schedules simulation:

```bash
RUN_ID="<existing-queued-run-uuid>"
docker compose run --rm --no-deps api /app/.venv/bin/python -m lavan.manage enqueue-run "$RUN_ID"
# Repeating while queued returns the same job. Unknown/nonqueued runs are errors.
```

Run `LAVAN_POSTGRES_TESTS=1 uv run pytest` for disposable PostgreSQL tests,
including real worker SIGKILL recovery, lease renewal, stale-worker rejection,
retry exhaustion, snapshot backfill, and unchanged authentication constraints.
Run Ruff checks and `uv sync --locked` as before. To stop after verification,
`docker compose stop worker api postgres` preserves both development volumes.
Milestone 7A adds cancellation below. No expiration, SSE, quotas, remote runner,
or real execution exists.


## Milestone 7A: authenticated cancellation

`POST /v1/projects/<project-uuid>/runs/<run-uuid>/cancel` requires Bearer auth.
It accepts no state/ownership fields; send no body or an empty JSON object.
Extra fields and malformed UUIDs return 422. Missing/invalid/revoked credentials
return 401; nonexistent, mismatched, and other-owner resources share sanitized 404.
No Idempotency-Key is needed: cancellation is intrinsically safe to repeat.

- **200**: the run stopped safely in this request, was already stopped, or was
  already completed/failed (unchanged). Never-claimed queued jobs and old unscheduled
  queued runs stop atomically without execution. Associated jobs are retained as
  stopped with zero attempts; no job is created for an unscheduled queued run.
- **202**: cancellation intent committed, but worker/runner acknowledgement is
  still required. Location is the existing GET URL. **202 does not mean stopped.**
  A queued run with prior attempts or a lease also requires acknowledgement.
- GET and cancellation responses add nullable UTC `cancellation_requested_at`.
  The creation response and its immutable stored snapshot remain the original six
  fields: replay after stopped still returns original queued body/Location and 201.

Example (API_TOKEN held in a shell variable as documented above; no literal token
in history or process arguments):

```bash
set +x
RUN_URL="http://127.0.0.1:8000/v1/projects/<project-uuid>/runs/<run-uuid>"
printf 'header = "Authorization: Bearer %s"\n' "$API_TOKEN" |
  curl --config - --request POST --include --fail-with-body --max-time 5 "$RUN_URL/cancel"
printf 'header = "Authorization: Bearer %s"\n' "$API_TOKEN" |
  curl --config - --fail-with-body --max-time 5 "$RUN_URL"
# Repeat POST safely; poll GET with bounded requests until stopped/another terminal.
# Revoke the verification token afterward and unset API_TOKEN.
```

Race policy is database commit order under the common job-then-run locks. If
completed or failed commits first, later cancellation leaves it untouched without
adding intent. If intent commits first, later normal progression, failure, and
retry exhaustion cannot supersede it: the worker must reconcile stop. One ordered
cancel_requested event records the first intent; one stopped event records the
acknowledged transition. Repeated requests preserve the original timestamp/event IDs.
No terminal state can be revived by stale or delayed workers.

The worker checks intent before starting, between phases, on each bounded poll,
and again before applying runner results. It calls idempotent stop on the same
logical operation and rechecks its current unexpired lease/fence before recording
stopped. Stop calls and waits hold no database transaction. The simulator confirms
termination immediately from its durable logical cancellation anchor, even during
slow startup; it never reads/extracts/executes the artifact. SIGKILL between stop
acknowledgement and database commit is recovered by repeating stop with that same
identity. A future real runner **must confirm termination**: changing a database
state alone cannot stop physical execution.

Pending retry/backoff jobs are made eligible immediately. Cancellation transport
failures reschedule reconciliation after at most 0.25s (plus worker polling),
without being relabeled as failed by the normal three-attempt budget. That normal
counter is capped, while fencing epochs continue to increase. Cancellation intent
is retained and reconciliation continues until acknowledgement; no cleanup exists. Normal runs retain their existing bounded retry policy.

A healthy available worker normally acknowledges the simulator within two seconds.
With no worker or unavailable infrastructure, GET may remain pending; recovery of
a crashed owner's lease can additionally wait for its expiry. Never interpret 202
as physical termination. Startup failure committed before cancellation remains failed.

To verify active cancellation, select slow startup locally **before creating a
fresh run** (first-claim scenario settings are persisted):

```bash
docker compose stop worker
SIMULATOR_SCENARIO=slow_startup SIMULATOR_SLOW_STARTUP_SECONDS=10 \
  docker compose up -d --no-deps --force-recreate worker
# Create using the POST /runs example above. Poll its GET until starting, then
# POST /cancel and poll GET with a bounded timeout (e.g. 20 requests, 0.1s apart).
# Repeat cancellation and replay the original creation key/payload; snapshot stays queued.
docker compose stop worker
SIMULATOR_SCENARIO=success docker compose up -d --no-deps --force-recreate worker
```

Build API/worker and explicitly run Alembic upgrade head before starting these
services after updating. Revision is 0007_run_cancellation. Run
`LAVAN_POSTGRES_TESTS=1 uv run pytest` for real PostgreSQL races, SIGKILL recovery,
backoff, snapshot, and migration checks; Ruff and locked checks are unchanged.
Restore the prior worker configuration/service states, and preserve both volumes.
The following milestone adds expiration; SSE, log streaming, and real sandbox
integration remain out of scope.

## Milestone 7B: automatic run expiration

New runs receive a server-assigned `expires_at`, exactly
`RUN_MAX_LIFETIME_SECONDS` after their PostgreSQL creation timestamp (default 900s,
positive and at most 86400s). **Lifetime includes queue time.** Requests cannot
supply/extend deadlines; claims, retries, recovery, and creation replays never
extend them. PostgreSQL `clock_timestamp()` is the deadline authority, independent
of worker clocks. No token or artifact retention policy changes.

GET and cancellation responses expose nullable `expires_at` and
`expiration_requested_at`. New creation bodies/snapshots include the deadline and
null expiration intent. Existing stored creation snapshots are untouched and replay
with their original fields, HTTP 201, and Location. Replays do not use mutable GET
state. Existing runs retain null deadlines, are not automatically expired or
scheduled, and can still be explicitly enqueued unless stopped/terminal.

Apply `0008_run_expiration` using the explicit upgrade commands above before
starting rebuilt API/worker images. Never downgrade development data. The worker
finds due active runs using a partial deadline index and bounded batches
(`WORKER_EXPIRATION_BATCH_SIZE=50`, range 1–200), including after restart and while
polling a slow simulated operation. `WORKER_EXPIRATION_POLL_SECONDS=0.25` (range
0.02–5) sets discovery cadence. Multiple workers use the same row locks, fencing,
and unique event ordering. Busy rows are skipped and retried in later scans.

Stop arbitration is shared by cancellation, discovery, claims, and transitions:

- Existing committed terminal outcomes never change.
- Under the run lock, an existing intent wins. A cancellation committed before
  expiry selects `stopped`; a due deadline selects `expired`, including when a
  late cancellation is the first observer. Only the selected timestamp is set.
- One `expire_requested` event records expiration intent. Normal progression or
  completion cannot commit after the deadline; a conditional SQL update and a
  deferred PostgreSQL commit guard enforce the boundary. The worker then
  reconciles the selected stop reason, rather than reporting a late completion.
- A never-attempted queued run can expire inline during discovery without runner
  start. Leased/previously attempted jobs require the existing idempotent runner
  stop acknowledgement, even if a lease expired or retries were exhausted.
- Backoff is made promptly eligible. Stop failures retain intent and retry with
  bounded delay; they cannot justify claiming termination. A stale worker cannot
  change an expired run. Explicit enqueue rejects an already due run.

Expiration requires an available worker; absent workers, database outages, or
lease recovery delay observation and acknowledgement. `expired` means the
simulator acknowledged termination (or start was provably impossible), not that
PostgreSQL stopped physical execution. A future real runner must confirm actual
termination. Expiration never deletes artifact bytes, metadata, or other records.

### Bounded local expiration verification

Record prior service/configuration state and use a fresh project/artifact and the
safe token-handling examples above. Do not change existing development deadlines.
Use a short positive API lifetime and a slower simulated startup:

```bash
docker compose build api worker
docker compose up -d --wait --wait-timeout 60 postgres
docker compose run --rm --no-deps -T api /app/.venv/bin/alembic upgrade head
RUN_MAX_LIFETIME_SECONDS=3 docker compose up -d --no-deps --force-recreate api
SIMULATOR_SCENARIO=slow_startup SIMULATOR_SLOW_STARTUP_SECONDS=10 \
  docker compose up -d --no-deps --force-recreate worker
# Create a fresh run with the documented POST payload and Idempotency-Key.
# Observe expires_at in its 201 response. Save the original body and Location.
# Poll GET at most 100 times, 0.1s apart, with a 3s timeout per request.
# Expect expired, expiration_requested_at set, cancellation_requested_at null.
# Replay using the SAME key/payload: original queued body/deadline and HTTP 201.
curl --fail-with-body --max-time 3 http://127.0.0.1:8000/health/live
curl --fail-with-body --max-time 3 http://127.0.0.1:8000/health/ready
# Revoke verification tokens; retain successful verification records/artifact.
docker compose stop --timeout 10 api worker
RUN_MAX_LIFETIME_SECONDS=900 docker compose up --no-start --no-deps --force-recreate api
SIMULATOR_SCENARIO=success SIMULATOR_SLOW_STARTUP_SECONDS=3 \
  docker compose up --no-start --no-deps --force-recreate worker
# Restore any other original overrides and initial service states.
# If PostgreSQL was initially stopped: docker compose stop --timeout 10 postgres
```

No source is read or executed. Run `LAVAN_POSTGRES_TESTS=1 uv run pytest` for
real PostgreSQL deadline/race, local-clock independence, bounded discovery,
SIGKILL recovery, snapshot-preservation, and migration tests. Destructive tests
only target fixture-verified disposable databases, never lavan_dev.
