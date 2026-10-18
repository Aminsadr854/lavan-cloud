import hashlib
import json
from datetime import timedelta
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from alembic import command
from conftest import migration
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from lavan.api import create_app
from lavan.manage import main
from lavan.models import ApiToken, User
from lavan.tokens import new_token, token_digest


@pytest.fixture
def client(migrated, monkeypatch):
    _, name = migrated
    monkeypatch.setenv("POSTGRES_DB", name)
    with TestClient(create_app()) as client:
        yield client


def invoke(capsys, *args):
    code = main(list(args))
    out = capsys.readouterr()
    # Consume secret-bearing stdout before assertions; never echo it in failures.
    payload = json.loads(out.out) if out.out else None
    return code, payload, out.err


def provision(capsys):
    code, user, error = invoke(capsys, "create-user")
    assert code == 0 and not error
    code, token, error = invoke(capsys, "issue-token", user["user_id"])
    assert code == 0 and not error
    return user, token


def test_provision_issue_authenticate_and_storage(client, migrated, capsys):
    user, issued = provision(capsys)
    with Session(migrated[0]) as session:
        token = session.get(ApiToken, UUID(issued["token_id"]))
        assert token.digest == hashlib.sha256(issued["token"].encode()).hexdigest()
        assert token.user_id == UUID(user["user_id"])
        assert token.created_at.utcoffset() == timedelta(0)
        assert token.revoked_at is None
        assert set(
            c["name"] for c in inspect(session.connection()).get_columns("api_tokens")
        ) == {"id", "user_id", "digest", "created_at", "revoked_at"}
    response = client.get(
        "/v1/me", headers={"Authorization": "Bearer " + issued["token"]}
    )
    assert response.status_code == 200
    assert response.json() == {
        "id": user["user_id"],
        "created_at": user["created_at"].replace("+00:00", "Z"),
    }
    assert set(response.json()) == {"id", "created_at"}


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Basic placeholder",
        "Bearer",
        "Bearer invalid",
        "Bearer lv_",
        "Bearer  extra",
        "Bearer " + "x" * 100,
    ],
)
def test_missing_and_malformed(client, header):
    headers = {} if header is None else {"Authorization": header}
    response = client.get("/v1/me", headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"detail": "Invalid credentials"}


def test_unknown_and_query_credentials_rejected(client, capsys):
    response = client.get("/v1/me", headers={"Authorization": "Bearer " + new_token()})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    _, issued = provision(capsys)
    response = client.get("/v1/me", params={"token": issued["token"]})
    assert response.status_code == 401


def test_revoke_idempotent_and_other_token_stays_valid(client, migrated, capsys):
    user, first = provision(capsys)
    code, second, _ = invoke(capsys, "issue-token", user["user_id"])
    assert code == 0
    code, _, _ = invoke(capsys, "revoke-token", first["token_id"])
    assert code == 0
    with Session(migrated[0]) as session:
        revoked_at = session.get(ApiToken, UUID(first["token_id"])).revoked_at
        assert revoked_at.utcoffset() == timedelta(0)
    code, _, _ = invoke(capsys, "revoke-token", first["token_id"])
    assert code == 0
    with Session(migrated[0]) as session:
        assert session.get(ApiToken, UUID(first["token_id"])).revoked_at == revoked_at
    revoked = client.get(
        "/v1/me", headers={"Authorization": "Bearer " + first["token"]}
    )
    assert revoked.status_code == 401
    assert revoked.headers["www-authenticate"] == "Bearer"
    assert (
        client.get(
            "/v1/me", headers={"Authorization": "bearer " + second["token"]}
        ).status_code
        == 200
    )


def test_unknown_management_targets_fail(client, capsys):
    code, output, error = invoke(capsys, "issue-token", str(uuid4()))
    assert code == 1 and output is None and error == "User not found.\n"
    code, output, error = invoke(capsys, "revoke-token", str(uuid4()))
    assert code == 1 and output is None and error == "Token not found.\n"


def test_failed_commit_does_not_print_token(client, capsys):
    code, user, _ = invoke(capsys, "create-user")
    assert code == 0
    with patch(
        "lavan.manage.Session.commit",
        side_effect=OperationalError("", {}, Exception("internal")),
    ):
        code, output, error = invoke(capsys, "issue-token", user["user_id"])
    assert code == 1 and output is None and error == "Database operation failed.\n"


def test_unexpected_database_failure_is_503(client, capsys):
    _, issued = provision(capsys)
    with patch(
        "lavan.auth.Session.scalar",
        side_effect=OperationalError("", {}, Exception("private")),
    ):
        response = client.get(
            "/v1/me", headers={"Authorization": "Bearer " + issued["token"]}
        )
    assert response.status_code == 503
    assert response.json() == {"detail": "Service unavailable"}
    assert "www-authenticate" not in response.headers
    assert client.get("/health/live").status_code == 200


def test_token_constraints(client, migrated, capsys):
    user, issued = provision(capsys)
    with Session(migrated[0]) as session:
        session.add(
            ApiToken(
                user_id=UUID(user["user_id"]), digest=token_digest(issued["token"])
            )
        )
        with pytest.raises(IntegrityError) as error:
            session.commit()
        assert error.value.orig.sqlstate == "23505"
        session.rollback()
        session.add(ApiToken(user_id=uuid4(), digest=token_digest(new_token())))
        with pytest.raises(IntegrityError) as error:
            session.commit()
        assert error.value.orig.sqlstate == "23503"
        session.rollback()
        session.delete(session.get(User, UUID(user["user_id"])))
        with pytest.raises(IntegrityError) as error:
            session.commit()
        assert error.value.orig.sqlstate == "23503"


def test_second_migration_preserves_users_on_downgrade(migrated):
    engine, name = migrated
    with Session(engine) as session:
        user = User()
        session.add(user)
        session.commit()
        user_id = user.id
    migration(engine, name, command.downgrade, "0001_users_projects")
    with engine.connect() as c:
        assert "api_tokens" not in inspect(c).get_table_names()
        assert c.scalar(select(User.id).where(User.id == user_id)) == user_id
    migration(engine, name, command.upgrade, "head")
    with engine.connect() as c:
        assert "api_tokens" in inspect(c).get_table_names()
        assert (
            c.scalar(text("SELECT version_num FROM alembic_version"))
            == "0005_run_idempotency"
        )


def test_auth_database_work_is_off_loop_and_session_closes(client, capsys, monkeypatch):
    import asyncio

    import lavan.auth as auth

    _, issued = provision(capsys)
    closed = []

    class TrackedSession(Session):
        def scalar(self, *args, **kwargs):
            with pytest.raises(RuntimeError):
                asyncio.get_running_loop()
            return super().scalar(*args, **kwargs)

        def close(self):
            super().close()
            closed.append(True)

    monkeypatch.setattr(auth, "Session", TrackedSession)
    response = client.get(
        "/v1/me", headers={"Authorization": "Bearer " + issued["token"]}
    )
    assert response.status_code == 200 and closed == [True]
    response = client.get("/v1/me", headers={"Authorization": "Bearer " + new_token()})
    assert response.status_code == 401 and closed == [True, True]
