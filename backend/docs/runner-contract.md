# Runner contract v1: simulated execution only

The backend owns scheduling, leases, fencing, lifecycle transitions, and immutable
creation replays. A runner adapter owns execution reconciliation. The current
SimulatedRunner never opens an artifact, installs dependencies, spawns uploaded
commands, makes network calls, or creates a listener. It simulates a short-lived
test, not a deployed FastAPI application.

`RunnerRequest` contains contract_version=1, run_id, artifact_id, artifact_sha256,
runtime_template, operation_id, fence, attempt, accepted_at, scenario, and bounded
startup/duration seconds. Artifact identity and checksum are references, never a
backend-local path. A future remote implementation must arrange authenticated
private artifact delivery separately; no download URL is introduced here.

The start identity is `run:<run UUID>:start:v1`, stable across attempts. Attempts
and fences increase when a lease is reclaimed; they are ownership epochs, not
new logical executions. The simulator freezes scenario/timing on first claim and
persists accepted_at when entering starting. `start(request, now)` and
`inspect(request, now)` are immediate, nonblocking reconciliation operations:

- Before accepted_at + startup delay: starting.
- At/after that instant for startup_failure: startup_failed (application failure).
- Otherwise until the configured duration elapses: running.
- After duration: completed.

Success and slow_startup differ only in bounded startup delay. Repeated start or
inspect for the same operation/anchor gives the same logical execution timeline.
If recovery observes completion while the run is still starting, it persists
running before completed, with one ordered event for each transition. Event times
are observation times; no missing historical execution times are invented.

Operations run outside database transactions. The worker renews its database
lease before each immediate runner operation, then re-locks and verifies its
current unexpired lease/fence before applying a result. Late results from stale
workers are discarded. Shared transition rules permit queued → preparing →
starting → running → completed, or failed from any nonterminal state; terminal
states cannot be overwritten. Runner transport/infrastructure exceptions retry;
simulated startup failure is terminal. Exhausted attempts are terminal failed.

A future real runner must implement durable start-operation deduplication,
status reconciliation after ambiguous timeouts, and fencing of stale commands.
It must provide bounded RPCs/nonblocking inspect so heartbeats can continue; the
current serial adapter is suitable for this nonblocking simulator, not arbitrary
long calls. Leases and at-least-once delivery alone do not establish exactly-once
physical execution. Actual extraction containment, decompressed-byte/CRC limits,
sandboxing, resource isolation, networking policy, and payload integrity remain
separate future runner obligations. Metadata validation does not prove safety.

POST replays return the original queued creation snapshot with 201 and original
Location. GET returns mutable current state. Replay never starts or enqueues work.
This contract intentionally replaces milestone 6B's immutable-run assumption
without changing request fingerprint v1 or expiring keys.
