"""Runner contract v1: nonblocking start/inspect, no uploaded bytes or local paths."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID


@dataclass(frozen=True)
class RunnerRequest:
    run_id: UUID
    artifact_id: UUID
    artifact_sha256: str
    runtime_template: str
    operation_id: str
    fence: int
    attempt: int
    accepted_at: datetime | None
    scenario: str
    startup_seconds: float
    duration_seconds: float
    contract_version: int = 1
    stop_requested_at: datetime | None = None


RunnerState = Literal["starting", "running", "completed", "startup_failed", "stopped"]


class Runner(Protocol):
    def start(self, request: RunnerRequest, now: datetime) -> RunnerState: ...
    def inspect(self, request: RunnerRequest, now: datetime) -> RunnerState: ...
    def stop(self, request: RunnerRequest, now: datetime) -> RunnerState: ...


class SimulatedRunner:
    def start(self, request: RunnerRequest, now: datetime) -> RunnerState:
        # Same operation/anchor always describes the same logical execution.
        return self.inspect(request, now)

    def inspect(self, request: RunnerRequest, now: datetime) -> RunnerState:
        if request.stop_requested_at is not None:
            return "stopped"
        if request.accepted_at is None:
            raise ValueError("Start anchor required")
        elapsed = (now - request.accepted_at).total_seconds()
        if elapsed < request.startup_seconds:
            return "starting"
        if request.scenario == "startup_failure":
            return "startup_failed"
        if elapsed < request.startup_seconds + request.duration_seconds:
            return "running"
        return "completed"

    def stop(self, request: RunnerRequest, now: datetime) -> RunnerState:
        if request.stop_requested_at is None:
            raise ValueError("Stop intent required")
        # Virtual execution has no external side effects. Persisted intent is its
        # deterministic termination anchor; repeat stop/inspect confirms it.
        return "stopped"
