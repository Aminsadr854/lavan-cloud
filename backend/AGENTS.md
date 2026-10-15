# Scope
Milestones 1–4: local FastAPI/PostgreSQL infrastructure, users/projects models,
explicit migrations, local user provisioning, API-token authentication, and
authenticated project creation/retrieval. Do not add listing, editing, deletion,
uploads, workers, runners, or CI without a new milestone request.
Keep secrets and runtime data outside Git. Preserve unrelated work.

# Verification
From backend/: `uv sync --locked`, `uv run ruff check .`,
`uv run ruff format --check .`, and `uv run pytest`.
Follow README.md for real Compose health, outage, and recovery checks.
Commit coherent verified checkpoints. Normal shutdown must preserve database data.

Run real schema tests with `LAVAN_POSTGRES_TESTS=1 uv run pytest`. They create
and remove a uniquely named disposable database; never downgrade development data.
Read docs/schema.md for the supported timestamp write path.

Never log raw API tokens, authorization headers, or credentials. Token output is
allowed only after successful issuance commit through the local management command.
