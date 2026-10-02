# Scope
Milestone 1 only: FastAPI, environment settings, PostgreSQL connectivity,
and local Compose infrastructure. Do not add business models, migrations,
authentication, uploads, workers, runners, or CI without a new milestone request.
Keep secrets and runtime data outside Git. Preserve unrelated work.

# Verification
From backend/: `uv sync --locked`, `uv run ruff check .`,
`uv run ruff format --check .`, and `uv run pytest`.
Follow README.md for real Compose health, outage, and recovery checks.
Commit coherent verified checkpoints. Normal shutdown must preserve database data.
