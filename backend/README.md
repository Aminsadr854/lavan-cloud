# Lavan backend: milestones 1–3

Local FastAPI infrastructure with minimal users/projects models and explicit
Alembic migrations. Python 3.12, PostgreSQL 17, SQLAlchemy, Pydantic settings,
and two Compose services. Opaque API-token authentication and /v1/me; no project endpoints.
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
