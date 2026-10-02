from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from lavan.api import create_app
from lavan.config import Settings


@pytest.fixture
def configured(monkeypatch):
    for key, value in {
        "POSTGRES_HOST": "127.0.0.1",
        "POSTGRES_DB": "lavan_test",
        "POSTGRES_USER": "test",
        "POSTGRES_PASSWORD": "unit-test-password",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(Path(__file__).resolve().parent)


def test_liveness_does_not_connect_and_shutdown_disposes(configured, monkeypatch):
    engine = MagicMock()
    monkeypatch.setattr("lavan.api.create_engine", lambda *a, **kw: engine)
    with TestClient(create_app()) as client:
        assert client.get("/health/live").json() == {"status": "alive"}
        engine.connect.assert_not_called()
    engine.dispose.assert_called_once()


def test_readiness_query_cleanup_failure_and_recovery(configured, monkeypatch):
    engine = MagicMock()
    monkeypatch.setattr("lavan.api.create_engine", lambda *a, **kw: engine)
    with TestClient(create_app()) as client:
        response = client.get("/health/ready")
        assert response.status_code == 200
        assert response.json() == {"status": "ready"}
        connection = engine.connect.return_value.__enter__.return_value
        assert str(connection.execute.call_args.args[0]) == "SELECT 1"
        engine.connect.return_value.__exit__.assert_called_once()
        engine.connect.side_effect = OperationalError(
            "SELECT 1", {}, Exception("private credentials")
        )
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json() == {"status": "unavailable"}
        assert client.get("/health/live").status_code == 200
        engine.connect.side_effect = None
        assert client.get("/health/ready").status_code == 200


def test_missing_settings_fail_startup_without_secret_values(configured, monkeypatch):
    monkeypatch.delenv("POSTGRES_HOST")
    with pytest.raises(ValidationError) as error:
        with TestClient(create_app()):
            pass
    assert "postgres_host" in str(error.value)
    assert "unit-test-password" not in str(error.value)


def test_credentials_are_encoded_and_no_sqlite_fallback(configured):
    settings = Settings(postgres_password="p@ss:/word")
    assert settings.database_url.drivername == "postgresql+psycopg"
    assert settings.database_url.password == "p@ss:/word"
    assert "p@ss:/word" not in str(settings.database_url)


@pytest.mark.parametrize(
    "field", ["postgres_host", "postgres_db", "postgres_user", "postgres_password"]
)
def test_empty_required_settings_are_rejected(configured, field):
    with pytest.raises(ValidationError):
        Settings(**{field: ""})


def test_engine_has_bounded_timeouts(configured, monkeypatch):
    factory = MagicMock()
    monkeypatch.setattr("lavan.api.create_engine", factory)
    with TestClient(create_app()):
        pass
    options = factory.call_args.kwargs
    assert options["poolclass"].__name__ == "NullPool"
    assert options["connect_args"]["connect_timeout"] == 2
    assert options["connect_args"]["options"] == "-c statement_timeout=2000"
    assert options["connect_args"]["tcp_user_timeout"] == 4000
