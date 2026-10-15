# Scope
Milestones 1–2: local FastAPI/PostgreSQL infrastructure, users/projects models,
and explicit Alembic migrations. Do not add authentication, provisioning, uploads,
workers, runners, or CI without a new milestone request.
Keep secrets and runtime data outside Git. Preserve unrelated work.

# Verification
From backend/: `uv sync --locked`, `uv run ruff check .`,
`uv run ruff format --check .`, and `uv run pytest`.
Follow README.md for real Compose health, outage, and recovery checks.
Commit coherent verified checkpoints. Normal shutdown must preserve database data.

Run real schema tests with `LAVAN_POSTGRES_TESTS=1 uv run pytest`. They create
and remove a uniquely named disposable database; never downgrade development data.
Read docs/schema.md for the supported timestamp write path.
