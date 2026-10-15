import asyncio
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from lavan.api import create_app
from lavan.models import ApiToken, Project, User, utc_now
from lavan.tokens import new_token, token_digest


@pytest.fixture
def client(migrated, monkeypatch):
    monkeypatch.setenv("POSTGRES_DB", migrated[1])
    with TestClient(create_app()) as client:
        yield client


@pytest.fixture
def identities(migrated):
    with Session(migrated[0]) as session:
        users = [User(), User()]
        session.add_all(users)
        session.flush()
        raw = [new_token(), new_token(), new_token()]
        session.add_all(
            [
                ApiToken(user_id=users[0].id, digest=token_digest(raw[0])),
                ApiToken(user_id=users[1].id, digest=token_digest(raw[1])),
                ApiToken(
                    user_id=users[0].id,
                    digest=token_digest(raw[2]),
                    revoked_at=utc_now(),
                ),
            ]
        )
        ids = [user.id for user in users]
        session.commit()
    return ids, raw


def bearer(token):
    return {"Authorization": "Bearer " + token}


def test_create_persist_trim_and_retrieve(client, identities, migrated):
    ids, tokens = identities
    response = client.post(
        "/v1/projects",
        json={"name": " \t Sample project \n"},
        headers=bearer(tokens[0]),
    )
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "name", "created_at", "updated_at"}
    project_id = UUID(body["id"])
    assert body["name"] == "Sample project"
    assert response.headers["location"] == f"/v1/projects/{project_id}"
    for field in ("created_at", "updated_at"):
        assert datetime.fromisoformat(body[field]).utcoffset() == timedelta(0)
    with Session(migrated[0]) as session:
        project = session.get(Project, project_id)
        assert project.owner_id == ids[0] and project.name == "Sample project"
    retrieved = client.get(response.headers["location"], headers=bearer(tokens[0]))
    assert retrieved.status_code == 200
    assert retrieved.json() == body


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"name": ""},
        {"name": " \t\n\r"},
        {"name": "x" * 101},
        {"name": None},
        {"name": 123},
        {"name": True},
        {"name": []},
        {"name": {}},
        {"name": "valid", "owner_id": str(uuid4())},
        {"name": "valid", "id": str(uuid4())},
        {"name": "valid", "extra": "value"},
    ],
)
def test_invalid_payloads_do_not_persist(client, identities, migrated, payload):
    ids, tokens = identities
    with Session(migrated[0]) as session:
        before = session.scalar(
            select(func.count()).select_from(Project).where(Project.owner_id == ids[0])
        )
    response = client.post("/v1/projects", json=payload, headers=bearer(tokens[0]))
    assert response.status_code == 422
    with Session(migrated[0]) as session:
        after = session.scalar(
            select(func.count()).select_from(Project).where(Project.owner_id == ids[0])
        )
        assert before == after


def test_duplicate_and_trimmed_maximum_length(client, identities):
    _, tokens = identities
    responses = [
        client.post(
            "/v1/projects",
            json={"name": " " + "x" * 100 + " "},
            headers=bearer(tokens[0]),
        )
        for _ in range(2)
    ]
    assert [response.status_code for response in responses] == [201, 201]
    first, second = [response.json() for response in responses]
    assert first["name"] == second["name"] == "x" * 100
    assert first["id"] != second["id"]


def test_owner_filter_and_identical_not_found(client, identities):
    _, tokens = identities
    created = client.post(
        "/v1/projects", json={"name": "Private"}, headers=bearer(tokens[0])
    )
    assert created.status_code == 201
    other = client.get(created.headers["location"], headers=bearer(tokens[1]))
    missing = client.get(f"/v1/projects/{uuid4()}", headers=bearer(tokens[1]))
    assert other.status_code == missing.status_code == 404
    assert other.content == missing.content
    assert other.json() == {"detail": "Project not found"}
    assert dict(other.headers) == dict(missing.headers)
    assert (
        client.get("/v1/projects/not-a-uuid", headers=bearer(tokens[0])).status_code
        == 422
    )


@pytest.mark.parametrize("credential", ["missing", "unknown", "revoked"])
@pytest.mark.parametrize("method", ["post", "get"])
def test_both_endpoints_require_valid_token(client, identities, credential, method):
    _, tokens = identities
    headers = (
        {}
        if credential == "missing"
        else bearer(new_token() if credential == "unknown" else tokens[2])
    )
    if method == "post":
        response = client.post("/v1/projects", json={"name": "Denied"}, headers=headers)
    else:
        response = client.get(f"/v1/projects/{uuid4()}", headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"detail": "Invalid credentials"}


def test_commit_failure_rolls_back_and_sanitizes(
    client, identities, migrated, monkeypatch
):
    import lavan.auth as auth

    ids, tokens = identities
    closed = []

    class FailingSession(Session):
        def commit(self):
            with pytest.raises(RuntimeError):
                asyncio.get_running_loop()
            self.flush()  # Exercise rollback of a real PostgreSQL INSERT.
            raise OperationalError("private SQL", {}, Exception("private details"))

        def close(self):
            super().close()
            closed.append(True)

    monkeypatch.setattr(auth, "Session", FailingSession)
    response = client.post(
        "/v1/projects", json={"name": "Rollback"}, headers=bearer(tokens[0])
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "Service unavailable"}
    assert closed == [True]
    with Session(migrated[0]) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(Project)
                .where(Project.owner_id == ids[0])
            )
            == 0
        )


def test_successful_project_io_is_off_loop_and_closes_sessions(
    client, identities, monkeypatch
):
    import lavan.auth as auth

    _, tokens = identities
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
    created = client.post(
        "/v1/projects", json={"name": "Closed"}, headers=bearer(tokens[0])
    )
    assert created.status_code == 201 and closed == [True]
    assert (
        client.get(created.headers["location"], headers=bearer(tokens[0])).status_code
        == 200
    )
    assert closed == [True, True]
