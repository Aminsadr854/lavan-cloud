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
    accepted_at: datetime
    scenario: str
    startup_seconds: float
    duration_seconds: float
    contract_version: int = 1


RunnerState = Literal["starting", "running", "completed", "startup_failed"]


class Runner(Protocol):
    def start(self, request: RunnerRequest, now: datetime) -> RunnerState: ...
    def inspect(self, request: RunnerRequest, now: datetime) -> RunnerState: ...


class SimulatedRunner:
    def start(self, request: RunnerRequest, now: datetime) -> RunnerState:
        # Same operation/anchor always describes the same logical execution.
        return self.inspect(request, now)

    def inspect(self, request: RunnerRequest, now: datetime) -> RunnerState:
        elapsed = (now - request.accepted_at).total_seconds()
        if elapsed < request.startup_seconds:
            return "starting"
        if request.scenario == "startup_failure":
            return "startup_failed"
        if elapsed < request.startup_seconds + request.duration_seconds:
            return "running"
        return "completed"
