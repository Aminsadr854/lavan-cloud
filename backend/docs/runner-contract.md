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
simulated startup failure is terminal absent committed cancellation intent.
Exhausted normal attempts are terminal failed; cancellation reconciliation is
covered by the extension below.

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


## Version 1 cancellation extension (milestone 7A)

The additive request field stop_requested_at is the persisted run cancellation
instant. accepted_at may be absent for a previously claimed run cancelled before
start was persisted. Existing start operation identities and request fingerprints
are unchanged. `stop(request, now)` targets the same `run:<UUID>:start:v1` logical
execution; repeated calls are idempotent. The result vocabulary adds stopped.

The simulator is a pure logical timeline with no external side effects. Persisted
stop_requested_at anchors termination; stop acknowledges stopped immediately,
and inspect with that same anchor reconciles stopped. Slow-start delays are
cooperative polls, not blocking sleeps. Start is never invoked by a worker that
observes intent; a stale request/result without the anchor is fenced from mutation.
A crash after stop acknowledgement and before state commit is recovered by another
stop/inspect on the same logical identity, not a second execution. No extra
filesystem or runner-local memory is needed to preserve simulated cancellation.

Only the first owner request appends cancel_requested. The worker may append
stopped only after runner acknowledgement under its current live lease/fence.
The exception is an inline API transition when a locked queued run has no job,
or a pending job has zero attempts/fence and no start anchor: no operation could
have started. Job metadata is retained. Previously attempted queued runs always
require reconciliation, including exhausted/backoff jobs.

Lock/commit order defines the race: terminal completion/failure first is final;
intent first blocks all later normal state progression and normal terminal
failure, including infrastructure retry exhaustion. Pending cancellation is
promptly claimable. Stop transport failures retain intent and retry with bounded
backoff until acknowledged; the ordinary execution-attempt budget cannot justify
false termination. Every ownership recovery increments the fence. No transaction
is held across stop/inspect/start calls or runner waits.

Future physical runners must implement stop deduplication, status reconciliation,
and confirmed termination (including already-exited operations) with bounded RPCs
and fencing. A database flag or expired lease cannot prove physical termination.
The current serial worker assumes nonblocking operations; arbitrary long RPCs
would need heartbeat-capable adapters. Exactly-once physical execution, real
sandbox control and streaming remain outside this implementation. Expiration is
covered by the following extension.

## Version 1 expiration reuse (milestone 7B)

`stop_requested_at` now carries the single selected durable stop instant, either
cancellation or expiration. The same stable operation identity, idempotent `stop`
and `inspect` acknowledgement, and fencing apply. The runner reports `stopped` as
the execution acknowledgement; the backend maps the selected reason to terminal
run/job `stopped` or `expired`. A deadline/expired lease alone is never a runner
termination acknowledgement. No artifact payloads or new runner operations exist.

PostgreSQL, not worker wall clocks, assigns and evaluates the run deadline.
Workers check the shared intent/deadline selector before dispatch, on cooperative
polls, and after results; normal writes and deferred transaction-end validation
prevent completion/progression after expiry. Intent serialized first under the run
lock selects the reason; a late cancellation observes a due deadline and selects
expiration. Existing cancellation wins even if its reconciliation crosses expiry.
Already committed terminal outcomes are final. The wrong intent timestamp is never
written. Stop acknowledgement lost to SIGKILL is safely repeated with the same
persisted anchor, including after lease recovery; delayed/stale results are fenced.

Discovery continues during cooperative slow stop polls. Adapters must keep
operations bounded/nonblocking to allow lease renewal and discovery; arbitrary
blocking remote calls require a heartbeat-capable future adapter. Worker downtime
can delay expiration, and stop transport failures remain pending until confirmed.
No guarantee of physical termination is made without a real runner's confirmation.
Old null-deadline runs retain their prior behavior; snapshot replays never refresh
deadlines or restart execution. Expiration never deletes artifacts.
