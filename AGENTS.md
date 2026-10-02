# Conventions
Keep uploaded code inert; never extract or execute it. PostgreSQL is required.
Use `uv sync --locked`, `uv run ruff check .`, `uv run ruff format --check .`,
and `TEST_DATABASE_URL=postgresql+psycopg://... uv run pytest`.
Run migrations using `uv run alembic upgrade head`. Tests reset only a database
whose name ends in `_test`. Keep secrets and runtime data outside Git.
Commit coherent verified checkpoints; preserve unrelated work.
