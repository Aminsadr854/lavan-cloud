import asyncio
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alembic import command
from conftest import migration
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, Table, func, inspect, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

import lavan.auth as auth
from lavan.api import create_app
from lavan.models import ApiToken, Artifact, ExecutionJob, Project, Run, User, utc_now
from lavan.tokens import new_token, token_digest


class Harness:
    def __repr__(self):
        return "<private run test harness>"

    def headers(self, index=0):
        return {
            "Authorization": "Bearer " + self.tokens[index],
            "Idempotency-Key": uuid4().hex,
        }

    def create(self, project=None, artifact=None, headers=None, payload=None):
        return self.client.post(
            f"/v1/projects/{project or self.projects[0]}/runs",
            json=payload
            if payload is not None
            else {
                "artifact_id": str(artifact or self.artifacts[0]),
                "runtime_template": "python-fastapi-v1",
            },
            headers=self.headers()
            if headers is None
            else {"Idempotency-Key": uuid4().hex, **headers},
        )

    def count(self):
        with Session(self.engine) as session:
            return session.scalar(select(func.count()).select_from(Run))


@pytest.fixture
def h(migrated, monkeypatch):
    h = Harness()
    h.engine, name = migrated
    h.tokens = [new_token(), new_token(), new_token()]
    with Session(h.engine) as session:
        users = [User(), User()]
        session.add_all(users)
        session.flush()
        projects = [Project(owner_id=users[i].id, name="Run test") for i in (0, 0, 1)]
        session.add_all(projects)
        session.flush()
        artifacts = [
            Artifact(
                project_id=p.id,
                storage_key=str(uuid4()) + ".zip",
                size_bytes=0,
                sha256="0" * 64,
                file_count=1,
                declared_uncompressed_bytes=0,
            )
            for p in projects
        ]
        session.add_all(artifacts)
        session.add_all(
            [
                ApiToken(user_id=users[0].id, digest=token_digest(h.tokens[0])),
                ApiToken(user_id=users[1].id, digest=token_digest(h.tokens[1])),
                ApiToken(
                    user_id=users[0].id,
                    digest=token_digest(h.tokens[2]),
                    revoked_at=utc_now(),
                ),
            ]
        )
        session.flush()
        h.projects = [p.id for p in projects]
        h.artifacts = [a.id for a in artifacts]
        session.commit()
    monkeypatch.setenv("POSTGRES_DB", name)
    h.before = h.count()
    with TestClient(create_app()) as client:
        h.client = client
        yield h


def test_create_persist_and_retrieve_queued_run(h):
    response = h.create()
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {
        "id",
        "project_id",
        "artifact_id",
        "status",
        "runtime_template",
        "created_at",
    }
    assert body["project_id"] == str(h.projects[0]) and body["artifact_id"] == str(
        h.artifacts[0]
    )
    assert (
        body["status"] == "queued" and body["runtime_template"] == "python-fastapi-v1"
    )
    assert datetime.fromisoformat(body["created_at"]).utcoffset() == timedelta(0)
    assert (
        response.headers["location"]
        == f"/v1/projects/{h.projects[0]}/runs/{body['id']}"
    )
    with Session(h.engine) as session:
        run = session.get(Run, UUID(body["id"]))
        assert run.project_id == h.projects[0] and run.artifact_id == h.artifacts[0]
        assert run.status == "queued" and not hasattr(run, "user_id")
        assert (
            session.scalar(
                select(func.count())
                .select_from(ExecutionJob)
                .where(ExecutionJob.run_id == run.id)
            )
            == 1
        )
    fetched = h.client.get(response.headers["location"], headers=h.headers())
    assert fetched.status_code == 200 and fetched.json() == {
        **body,
        "cancellation_requested_at": None,
    }


def test_repeated_posts_create_separate_queued_records_without_quotas(h):
    responses = [h.create() for _ in range(3)]
    assert [r.status_code for r in responses] == [201, 201, 201]
    assert len({r.json()["id"] for r in responses}) == 3
    assert all(r.json()["status"] == "queued" for r in responses)
    assert h.count() == h.before + 3


def test_inaccessible_missing_and_cross_project_artifacts_identical_404(h):
    responses = [
        h.create(headers=h.headers(1)),
        h.create(project=uuid4()),
        h.create(artifact=uuid4()),
        h.create(artifact=h.artifacts[1]),  # Different project, SAME owner.
        h.create(artifact=h.artifacts[2]),  # Different owner too.
        h.create(project=h.projects[2], artifact=h.artifacts[2]),
    ]
    assert all(r.status_code == 404 for r in responses)
    assert all(r.content == responses[0].content for r in responses)
    assert responses[0].json() == {"detail": "Not found"}
    assert h.count() == h.before


def test_get_missing_mismatched_or_inaccessible_run_identical_404(h):
    created = h.create()
    assert created.status_code == 201
    run_id = created.json()["id"]
    responses = [
        h.client.get(created.headers["location"], headers=h.headers(1)),
        h.client.get(
            f"/v1/projects/{h.projects[1]}/runs/{run_id}", headers=h.headers()
        ),
        h.client.get(f"/v1/projects/{uuid4()}/runs/{run_id}", headers=h.headers()),
        h.client.get(
            f"/v1/projects/{h.projects[0]}/runs/{uuid4()}", headers=h.headers()
        ),
    ]
    assert all(r.status_code == 404 for r in responses)
    assert all(r.content == responses[0].content for r in responses)
    assert responses[0].json() == {"detail": "Not found"}


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"artifact_id": str(uuid4())},
        {"runtime_template": "python-fastapi-v1"},
        {"artifact_id": "not-a-uuid", "runtime_template": "python-fastapi-v1"},
        {"artifact_id": None, "runtime_template": "python-fastapi-v1"},
        {"artifact_id": 123, "runtime_template": "python-fastapi-v1"},
        {"artifact_id": str(uuid4()), "runtime_template": "unsupported"},
        {"artifact_id": str(uuid4()), "runtime_template": None},
        {"artifact_id": str(uuid4()), "runtime_template": 123},
        [],
        "not-an-object",
    ],
)
def test_invalid_structures_and_values(h, payload):
    assert h.create(payload=payload).status_code == 422
    assert h.count() == h.before


@pytest.mark.parametrize(
    "field", ["id", "status", "owner_id", "user_id", "resource_limits", "extra"]
)
def test_unexpected_fields_rejected(h, field):
    payload = {
        "artifact_id": str(h.artifacts[0]),
        "runtime_template": "python-fastapi-v1",
        field: "queued",
    }
    assert h.create(payload=payload).status_code == 422
    assert h.count() == h.before


def test_malformed_path_uuids(h):
    payload = {
        "artifact_id": str(h.artifacts[0]),
        "runtime_template": "python-fastapi-v1",
    }
    assert (
        h.client.post(
            "/v1/projects/invalid/runs", headers=h.headers(), json=payload
        ).status_code
        == 422
    )
    assert (
        h.client.get(
            f"/v1/projects/{h.projects[0]}/runs/invalid", headers=h.headers()
        ).status_code
        == 422
    )
    assert (
        h.client.get(
            f"/v1/projects/invalid/runs/{uuid4()}", headers=h.headers()
        ).status_code
        == 422
    )


@pytest.mark.parametrize("credential", ["missing", "unknown", "revoked"])
@pytest.mark.parametrize("method", ["post", "get"])
def test_both_endpoints_require_valid_token(h, credential, method):
    headers = (
        {}
        if credential == "missing"
        else {
            "Authorization": "Bearer "
            + (new_token() if credential == "unknown" else h.tokens[2])
        }
    )
    response = (
        h.create(headers=headers)
        if method == "post"
        else h.client.get(
            f"/v1/projects/{h.projects[0]}/runs/{uuid4()}", headers=headers
        )
    )
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"detail": "Invalid credentials"}


def test_database_rejects_artifact_project_mismatch(h):
    with Session(h.engine) as session:
        session.add(
            Run(
                project_id=h.projects[0],
                artifact_id=h.artifacts[1],
                runtime_template="python-fastapi-v1",
            )
        )
        with pytest.raises(IntegrityError) as error:
            session.commit()
        assert error.value.orig.sqlstate == "23503"
        assert error.value.orig.diag.constraint_name == "fk_runs_artifact_project"


@pytest.mark.parametrize(
    "field,value",
    [("status", "unsupported"), ("status", ""), ("runtime_template", "unsupported")],
)
def test_database_status_and_template_constraints(h, field, value):
    values = {
        "project_id": h.projects[0],
        "artifact_id": h.artifacts[0],
        "runtime_template": "python-fastapi-v1",
        field: value,
    }
    with Session(h.engine) as session:
        session.add(Run(**values))
        with pytest.raises(IntegrityError) as error:
            session.commit()
        assert error.value.orig.sqlstate == "23514"


def test_referenced_projects_and_artifacts_cannot_be_deleted(h):
    assert h.create().status_code == 201
    for model, key in ((Artifact, h.artifacts[0]), (Project, h.projects[0])):
        with Session(h.engine) as session:
            session.delete(session.get(model, key))
            with pytest.raises(IntegrityError) as error:
                session.commit()
            assert error.value.orig.sqlstate == "23503"
    with Session(h.engine) as session:
        assert session.get(Artifact, h.artifacts[0]) is not None
        assert session.get(Project, h.projects[0]) is not None


def test_failed_commit_sanitized_and_session_rolled_back(h, monkeypatch):
    closed = []

    class FailingSession(Session):
        def commit(self):
            with pytest.raises(RuntimeError):
                asyncio.get_running_loop()
            self.flush()
            raise OperationalError("private", {}, Exception("private details"))

        def close(self):
            super().close()
            closed.append(True)

    monkeypatch.setattr(auth, "Session", FailingSession)
    response = h.create()
    assert response.status_code == 503 and response.json() == {
        "detail": "Service unavailable"
    }
    assert closed == [True] and h.count() == h.before


def test_successful_io_off_loop_and_session_cleanup(h, monkeypatch):
    closed = []

    class TrackedSession(Session):
        def commit(self):
            with pytest.raises(RuntimeError):
                asyncio.get_running_loop()
            return super().commit()

        def scalar(self, *args, **kwargs):
            with pytest.raises(RuntimeError):
                asyncio.get_running_loop()
            return super().scalar(*args, **kwargs)

        def close(self):
            super().close()
            closed.append(True)

    monkeypatch.setattr(auth, "Session", TrackedSession)
    response = h.create()
    assert response.status_code == 201 and closed == [True]
    assert (
        h.client.get(response.headers["location"], headers=h.headers()).status_code
        == 200
    )
    assert closed == [True, True]
    assert (
        h.client.get(
            f"/v1/projects/{h.projects[0]}/runs/{uuid4()}", headers=h.headers()
        ).status_code
        == 404
    )
    assert closed == [True, True, True]


def test_migration_downgrade_preserves_artifacts_then_reupgrade(h, migrated):
    assert h.create().status_code == 201
    engine, name = migrated
    migration(engine, name, command.downgrade, "0003_artifacts")
    with Session(engine) as session:
        assert "runs" not in inspect(session.connection()).get_table_names()
        assert session.get(Artifact, h.artifacts[0]) is not None
        assert "uq_artifacts_id_project_id" not in {
            u["name"]
            for u in inspect(session.connection()).get_unique_constraints("artifacts")
        }
    migration(engine, name, command.upgrade, "head")
    with engine.connect() as c:
        assert "runs" in inspect(c).get_table_names()
        assert "uq_artifacts_id_project_id" in {
            u["name"] for u in inspect(c).get_unique_constraints("artifacts")
        }


def test_idempotent_replay_canonical_json_and_conflict(h):
    headers = h.headers()
    first = h.create(headers=headers)
    replay = h.client.post(
        f"/v1/projects/{h.projects[0]}/runs",
        headers={**headers, "Content-Type": "application/json"},
        content=' { "runtime_template": "python-fastapi-v1", "artifact_id": "'
        + str(h.artifacts[0]).upper()
        + '" } ',
    )
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    assert first.headers["location"] == replay.headers["location"]
    assert h.count() == h.before + 1
    assert (
        h.create(
            project=h.projects[1], artifact=h.artifacts[1], headers=headers
        ).status_code
        == 409
    )
    assert h.create(artifact=h.artifacts[1], headers=headers).status_code == 404
    assert (
        h.create(
            project=h.projects[2], artifact=h.artifacts[2], headers=headers
        ).status_code
        == 404
    )
    from lavan.models import RunIdempotency

    with Session(h.engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(RunIdempotency)
                .where(RunIdempotency.run_id == UUID(first.json()["id"]))
            )
            == 1
        )


@pytest.mark.parametrize(
    "key", [None, "", "a" * 129, "with space", "a,b", "a/b", "a\t", "\xe9"]
)
def test_invalid_or_missing_key_is_sanitized(h, key):
    headers = [(b"authorization", ("Bearer " + h.tokens[0]).encode())]
    if key is not None:
        headers.append((b"idempotency-key", key.encode("latin1")))
    response = h.client.post(
        f"/v1/projects/{h.projects[0]}/runs",
        headers=headers,
        json={
            "artifact_id": str(h.artifacts[0]),
            "runtime_template": "python-fastapi-v1",
        },
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid Idempotency-Key"}
    assert h.count() == h.before


def test_duplicate_key_headers_rejected(h):
    response = h.client.post(
        f"/v1/projects/{h.projects[0]}/runs",
        headers=[*h.headers().items(), ("Idempotency-Key", uuid4().hex)],
        json={
            "artifact_id": str(h.artifacts[0]),
            "runtime_template": "python-fastapi-v1",
        },
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid Idempotency-Key"}


def test_key_scope_users_case_and_revocation(h):
    headers = h.headers()
    headers["Idempotency-Key"] = "A" + uuid4().hex
    first = h.create(headers=headers)
    assert first.status_code == 201
    assert (
        h.create(
            headers={**headers, "Idempotency-Key": headers["Idempotency-Key"].lower()}
        ).json()["id"]
        != first.json()["id"]
    )
    assert (
        h.create(
            project=h.projects[2],
            artifact=h.artifacts[2],
            headers={**h.headers(1), "Idempotency-Key": headers["Idempotency-Key"]},
        ).status_code
        == 201
    )
    with Session(h.engine) as session:
        token = session.scalar(
            select(ApiToken).where(ApiToken.digest == token_digest(h.tokens[0]))
        )
        token.revoked_at = utc_now()
        session.commit()
    assert h.create(headers=headers).status_code == 401


@pytest.mark.parametrize("conflicting", [False, True])
def test_concurrent_scoped_keys_separate_connections(h, monkeypatch, conflicting):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Lock

    from sqlalchemy import text

    from lavan.models import RunIdempotency

    barrier = Barrier(2, timeout=10)
    pids = []
    lock = Lock()

    class SynchronizedSession(Session):
        def execute(self, statement, *args, **kwargs):
            if (
                getattr(statement, "is_insert", False)
                and statement.table.name == "run_idempotency"
            ):
                pid = self.connection().scalar(text("SELECT pg_backend_pid()"))
                with lock:
                    pids.append(pid)
                barrier.wait()
            return super().execute(statement, *args, **kwargs)

    monkeypatch.setattr(auth, "Session", SynchronizedSession)
    headers = h.headers()

    def submit(index):
        project_index = index if conflicting else 0
        return h.create(
            project=h.projects[project_index],
            artifact=h.artifacts[project_index],
            headers=headers,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(submit, range(2)))
    assert len(set(pids)) == 2
    assert sorted(r.status_code for r in responses) == (
        [201, 409] if conflicting else [201, 201]
    )
    successful = [r for r in responses if r.status_code == 201]
    assert len({r.json()["id"] for r in successful}) == 1
    assert len({r.headers["location"] for r in successful}) == 1
    assert h.count() == h.before + 1
    with Session(h.engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(ExecutionJob)
                .where(ExecutionJob.run_id == UUID(successful[0].json()["id"]))
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(RunIdempotency)
                .where(RunIdempotency.key == headers["Idempotency-Key"])
            )
            == 1
        )


def test_atomic_failure_leaves_neither_record(h, monkeypatch):
    from lavan.models import RunIdempotency

    class FailingSession(Session):
        def commit(self):
            self.flush()
            raise OperationalError("private", {}, Exception("private"))

    monkeypatch.setattr(auth, "Session", FailingSession)
    headers = h.headers()
    assert h.create(headers=headers).status_code == 503
    assert h.count() == h.before
    with Session(h.engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(ExecutionJob)
                .join(Run, ExecutionJob.run_id == Run.id)
                .where(Run.project_id == h.projects[0])
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(RunIdempotency)
                .where(RunIdempotency.key == headers["Idempotency-Key"])
            )
            == 0
        )


def test_upgrade_existing_runs_and_downgrade_only_disposable(h, migrated):
    from lavan.models import RunIdempotency

    engine, name = migrated
    migration(engine, name, command.downgrade, "0004_runs")
    # Use the reflected historical table, not the current ORM mapping.
    old_runs = Table("runs", MetaData(), autoload_with=engine)
    run_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            old_runs.insert().values(
                id=run_id,
                project_id=h.projects[0],
                artifact_id=h.artifacts[0],
                runtime_template="python-fastapi-v1",
            )
        )
    migration(engine, name, command.upgrade, "head")
    response = h.client.get(
        f"/v1/projects/{h.projects[0]}/runs/{run_id}", headers=h.headers()
    )
    assert response.status_code == 200
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(RunIdempotency)) == 0
    migration(engine, name, command.downgrade, "0004_runs")
    with Session(engine) as session:
        assert (
            session.scalar(select(old_runs.c.id).where(old_runs.c.id == run_id))
            == run_id
        )
        assert "run_idempotency" not in inspect(session.connection()).get_table_names()
    migration(engine, name, command.upgrade, "head")


def test_same_project_different_artifact_conflicts_after_authorization(h):
    with Session(h.engine) as session:
        artifact = Artifact(
            project_id=h.projects[0],
            storage_key=uuid4().hex,
            size_bytes=0,
            sha256="0" * 64,
            file_count=1,
            declared_uncompressed_bytes=0,
        )
        session.add(artifact)
        session.commit()
        artifact_id = artifact.id
    headers = h.headers()
    assert h.create(headers=headers).status_code == 201
    assert h.create(headers=headers, artifact=artifact_id).status_code == 409
    assert (
        h.create(
            headers=headers,
            payload={
                "artifact_id": str(artifact_id),
                "runtime_template": "unsupported",
            },
        ).status_code
        == 422
    )
    with Session(h.engine) as session:
        project = session.get(Project, h.projects[0])
        project.owner_id = session.get(Project, h.projects[2]).owner_id
        session.commit()
    assert h.create(headers=headers).status_code == 404


def test_unrelated_unique_failure_is_not_treated_as_replay(h, monkeypatch):

    class FailingSession(Session):
        def commit(self):
            # A real, unrelated unique violation in the same transaction.
            record = next(
                obj for obj in self.identity_map.values() if isinstance(obj, Run)
            )
            self.connection().execute(
                Run.__table__.insert().values(
                    id=record.id,
                    project_id=h.projects[0],
                    artifact_id=h.artifacts[0],
                    runtime_template="python-fastapi-v1",
                )
            )
            return super().commit()

    monkeypatch.setattr(auth, "Session", FailingSession)
    assert h.create().status_code == 503
    assert h.count() == h.before


def test_scoped_key_lock_wait_is_bounded_and_rollback_is_atomic(h):
    from concurrent.futures import ThreadPoolExecutor
    from time import monotonic

    from lavan.models import RunIdempotency
    from lavan.runs import OPERATION, request_fingerprint

    headers = h.headers()
    # Hold the exact scope unique key uncommitted on a separate connection.
    with Session(h.engine) as blocker:
        user_id = blocker.get(Project, h.projects[0]).owner_id
        run = Run(
            project_id=h.projects[0],
            artifact_id=h.artifacts[0],
            runtime_template="python-fastapi-v1",
        )
        blocker.add(run)
        blocker.flush()
        blocker.add(
            RunIdempotency(
                user_id=user_id,
                operation=OPERATION,
                key=headers["Idempotency-Key"],
                creation_response={},
                fingerprint=request_fingerprint(
                    h.projects[0], h.artifacts[0], "python-fastapi-v1"
                ),
                run_id=run.id,
            )
        )
        blocker.flush()
        with ThreadPoolExecutor(max_workers=1) as executor:
            start = monotonic()
            response = executor.submit(h.create, headers=headers).result(timeout=5)
            elapsed = monotonic() - start
        assert response.status_code == 503
        assert response.json() == {"detail": "Service unavailable"}
        assert 1 <= elapsed < 3
        blocker.rollback()
    assert h.count() == h.before
    assert h.create(headers=headers).status_code == 201


@pytest.mark.parametrize("key", ["a", "A_Z-09." * 18 + "Az"])
def test_valid_key_boundaries(h, key):
    # Second case is exactly 128 ASCII characters.
    headers = {**h.headers(), "Idempotency-Key": key}
    first = h.create(headers=headers)
    assert first.status_code == 201
    assert h.create(headers=headers).json() == first.json()
