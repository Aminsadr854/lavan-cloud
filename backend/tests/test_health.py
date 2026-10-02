from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

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


def test_liveness_does_not_probe(configured, monkeypatch):
    from unittest.mock import AsyncMock

    probe = AsyncMock(return_value=True)
    monkeypatch.setattr("lavan.api.run_probe", probe)
    with TestClient(create_app()) as client:
        assert client.get("/health/live").json() == {"status": "alive"}
        probe.assert_not_called()


def test_readiness_failure_and_recovery(configured, monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        "lavan.api.run_probe", AsyncMock(side_effect=[True, False, True])
    )
    with TestClient(create_app()) as client:
        assert client.get("/health/ready").status_code == 200
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json() == {"status": "unavailable"}
        assert client.get("/health/live").status_code == 200
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
    monkeypatch.setattr("lavan.probe.create_engine", factory)
    from lavan.probe import check_database

    check_database()
    factory.return_value.dispose.assert_called_once()
    options = factory.call_args.kwargs
    assert options["poolclass"].__name__ == "NullPool"
    assert options["connect_args"]["connect_timeout"] == 2
    assert options["connect_args"]["options"] == "-c statement_timeout=1000"
    assert options["connect_args"]["tcp_user_timeout"] == 1500


def test_stalled_probe_is_killed_reaped_and_recovers(configured, monkeypatch):
    import asyncio
    import sys
    import time

    import httpx

    import lavan.api as api

    async def scenario():
        spawned = []
        spawn = asyncio.create_subprocess_exec

        async def record_spawn(*args, **kwargs):
            process = await spawn(*args, **kwargs)
            spawned.append(process)
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", record_spawn)
        monkeypatch.setattr(api, "READINESS_TIMEOUT", 0.15)
        monkeypatch.setattr(
            api, "PROBE_COMMAND", (sys.executable, "-c", "import time; time.sleep(60)")
        )
        app = create_app()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                for _ in range(3):
                    started = time.monotonic()
                    pending = asyncio.create_task(client.get("/health/ready"))
                    while not app.state.probe_lock.locked():
                        await asyncio.sleep(0)
                    live = await client.get("/health/live")
                    assert live.status_code == 200
                    overlapping = await client.get("/health/ready")
                    assert overlapping.status_code == 503
                    response = await pending
                    assert response.status_code == 503
                    assert response.json() == {"status": "unavailable"}
                    assert time.monotonic() - started < 1
                    assert spawned[-1].returncode is not None
                    assert not app.state.probe_lock.locked()
                monkeypatch.setattr(
                    api, "PROBE_COMMAND", (sys.executable, "-c", "pass")
                )
                assert (await client.get("/health/ready")).status_code == 200
                assert (await client.get("/health/live")).status_code == 200
        assert len(spawned) == 4
        assert all(process.returncode is not None for process in spawned)

    asyncio.run(scenario())


def test_cancelled_probe_reaps_child(configured, monkeypatch):
    import asyncio
    import sys

    import lavan.api as api

    async def scenario():
        spawned = asyncio.Event()
        children = []
        spawn = asyncio.create_subprocess_exec

        async def record_spawn(*args, **kwargs):
            process = await spawn(*args, **kwargs)
            children.append(process)
            spawned.set()
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", record_spawn)
        monkeypatch.setattr(
            api, "PROBE_COMMAND", (sys.executable, "-c", "import time; time.sleep(60)")
        )
        task = asyncio.create_task(api.run_probe())
        await asyncio.wait_for(spawned.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert children[0].returncode is not None

    asyncio.run(scenario())


def test_database_failure_closes_connection_and_disposes(configured, monkeypatch):
    from sqlalchemy.exc import OperationalError

    from lavan.probe import check_database

    engine = MagicMock()
    monkeypatch.setattr("lavan.probe.create_engine", lambda *a, **kw: engine)
    connection = engine.connect.return_value.__enter__.return_value
    connection.execute.side_effect = OperationalError(
        "SELECT 1", {}, Exception("private connection details")
    )
    with pytest.raises(OperationalError):
        check_database()
    engine.connect.return_value.__exit__.assert_called_once()
    engine.dispose.assert_called_once()
