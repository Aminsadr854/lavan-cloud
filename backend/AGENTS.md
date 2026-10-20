# Scope
Milestones 1–7B: local FastAPI/PostgreSQL infrastructure, users/projects models,
explicit migrations, local user provisioning, API-token authentication, and
authenticated projects, private ZIP artifacts, durable jobs, and simulated lifecycle.
Never extract/decompress uploaded payloads. Do not add listing, editing, deletion,
downloads, real execution, SSE, or CI without a new
milestone request. Only the simulated runner is permitted. Never read uploaded
payloads in the worker. Runner calls must remain outside database transactions;
worker mutations require current fencing identity and an unexpired lease.
Creation replays use immutable persisted snapshots; GET shows current state.
PostgreSQL decides run deadlines. Shared stop arbitration selects the first intent:
cancellation means stopped, expiration means expired. Both require runner stop
acknowledgement unless execution is proven never claimed. Preserve terminal outcomes
and old creation snapshots. Old runs with null deadlines do not expire.
Keep secrets and runtime data outside Git. Preserve unrelated work.

# Verification
From backend/: `uv sync --locked`, `uv run ruff check .`,
`uv run ruff format --check .`, and `uv run pytest`.
Follow README.md for real Compose health, outage, and recovery checks.
Commit coherent verified checkpoints. Normal shutdown must preserve database data.

Run real schema tests with `LAVAN_POSTGRES_TESTS=1 uv run pytest`. They create
and remove a uniquely named disposable database; never downgrade development data.
Read docs/schema.md for the supported timestamp write path.

Never log raw API tokens, authorization headers, idempotency keys, or credentials.
Token output is allowed only after successful issuance commit through the local management command.
