# Lavan: milestone 1

Local FastAPI infrastructure only. Python 3.12, PostgreSQL 17, SQLAlchemy,
Pydantic settings, and two Compose services. No business tables or migrations.
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
127.0.0.1 --port 8000` while only `docker compose up -d postgres` is running).

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
