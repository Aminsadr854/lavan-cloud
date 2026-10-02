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
Connection establishment is limited to 2 seconds per resolved address; queries
have a 2-second server timeout, with TCP failure detection configured for stalled
connections. The development Compose hostname resolves to one database service.
Readiness creates and closes a connection on every request, so recovery does not
reuse stale connections. API shutdown disposes the engine. No credentials or
internal exceptions appear in health responses.

Published ports are localhost only (8000 and 5432). PostgreSQL has a health check
and uses the project-scoped `lavan-local_postgres_data` volume.

## Shutdown

```bash
docker compose down
```

Normal shutdown preserves the database. **`docker compose down --volumes` is
destructive: it deletes this project's development database.** Never use global
Docker pruning for this project.
