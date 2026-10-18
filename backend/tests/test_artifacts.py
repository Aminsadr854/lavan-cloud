import asyncio
import hashlib
import io
import os
import stat
import struct
import time
import zipfile
from uuid import UUID, uuid4

import httpx
import pytest
from alembic import command
from conftest import migration
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

import lavan.artifacts as api
from lavan.api import create_app
from lavan.models import ApiToken, Artifact, Project, User
from lavan.reconcile import reconcile
from lavan.storage import UploadFile, storage_root
from lavan.tokens import new_token, token_digest


def archive(entries=None, method=zipfile.ZIP_DEFLATED):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=method) as z:
        for name, data in (
            entries if entries is not None else [("src/hello.py", b"print('inert')")]
        ):
            z.writestr(name, data)
    return buffer.getvalue()


class Harness:
    def __repr__(self):
        return "<private artifact test harness>"

    def headers(self, owner=0):
        return {
            "Authorization": "Bearer " + self.tokens[owner],
            "Content-Type": "application/zip",
        }

    def upload(self, data, project=None, owner=0, headers=None):
        return self.client.post(
            f"/v1/projects/{project or self.projects[0]}/artifacts",
            content=data,
            headers=headers if headers is not None else self.headers(owner),
        )

    def files(self):
        return (
            {p.name for p in self.root.iterdir() if p.name != ".uploads.lock"}
            if self.root.exists()
            else set()
        )

    def count(self):
        with Session(self.engine) as session:
            return session.scalar(select(func.count()).select_from(Artifact))


@pytest.fixture
def h(migrated, monkeypatch, tmp_path):
    h = Harness()
    h.engine, name = migrated
    h.root = tmp_path / "private"
    h.tokens = [new_token(), new_token()]
    with Session(h.engine) as session:
        users = [User(), User()]
        session.add_all(users)
        session.flush()
        projects = [Project(owner_id=user.id, name="Artifact test") for user in users]
        session.add_all(projects)
        session.add_all(
            [
                ApiToken(user_id=user.id, digest=token_digest(token))
                for user, token in zip(users, h.tokens)
            ]
        )
        session.flush()
        h.projects = [p.id for p in projects]
        session.commit()
    monkeypatch.setenv("POSTGRES_DB", name)
    monkeypatch.setenv("ARTIFACT_STORAGE_DIR", str(h.root))
    h.app = create_app()
    with TestClient(h.app) as client:
        h.client = client
        # Isolate metadata rows as well as storage for each test, without deleting
        # anything: count deltas, not global counts, in the disposable module DB.
        h.before = h.count()
        yield h


def test_valid_upload_metadata_bytes_and_restrictions(h):
    data = archive([("src/", b""), ("src/main.py", b"inert"), ("readme.txt", b"hello")])
    response = h.upload(data)
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {
        "id",
        "project_id",
        "size_bytes",
        "sha256",
        "file_count",
        "declared_uncompressed_bytes",
        "created_at",
    }
    assert body["project_id"] == str(h.projects[0])
    assert (
        body["size_bytes"] == len(data)
        and body["sha256"] == hashlib.sha256(data).hexdigest()
    )
    assert body["file_count"] == 2 and body["declared_uncompressed_bytes"] == 10
    assert response.headers["location"].endswith("/artifacts/" + body["id"])
    assert (
        h.client.get(response.headers["location"], headers=h.headers()).json() == body
    )
    with Session(h.engine) as session:
        row = session.get(Artifact, UUID(body["id"]))
        path = h.root / row.storage_key
        assert path.read_bytes() == data
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(h.root.stat().st_mode) == 0o700
        session.delete(session.get(Project, h.projects[0]))
        with pytest.raises(IntegrityError):
            session.commit()
    other = h.client.get(response.headers["location"], headers=h.headers(1))
    missing = h.client.get(
        f"/v1/projects/{h.projects[1]}/artifacts/{uuid4()}", headers=h.headers(1)
    )
    mismatch = h.client.get(
        f"/v1/projects/{h.projects[1]}/artifacts/{body['id']}", headers=h.headers(1)
    )
    assert other.status_code == missing.status_code == mismatch.status_code == 404
    assert other.content == missing.content == mismatch.content


@pytest.mark.parametrize("method", ["post", "get"])
@pytest.mark.parametrize("credential", ["missing", "unknown", "revoked"])
def test_authentication(h, method, credential):
    token = new_token()
    if credential == "revoked":
        with Session(h.engine) as session:
            row = session.scalar(
                select(ApiToken).where(ApiToken.digest == token_digest(h.tokens[0]))
            )
            from lavan.models import utc_now

            row.revoked_at = utc_now()
            session.commit()
        token = h.tokens[0]
    headers = {} if credential == "missing" else {"Authorization": "Bearer " + token}
    path = f"/v1/projects/{h.projects[0]}/artifacts"
    response = (
        h.client.post(path, content=archive(), headers=headers)
        if method == "post"
        else h.client.get(path + "/" + str(uuid4()), headers=headers)
    )
    assert (
        response.status_code == 401 and response.headers["www-authenticate"] == "Bearer"
    )


def test_ownership_before_body_and_short_sessions(h, monkeypatch):
    closed = []

    class Tracked(Session):
        def close(self):
            super().close()
            closed.append(True)

    monkeypatch.setattr(api, "Session", Tracked)

    def body():
        assert closed  # Authorization session closed before first byte.
        yield archive()

    response = h.upload(body())
    assert response.status_code == 201
    before = len(closed)

    def forbidden_body():
        pytest.fail("Inaccessible upload body was consumed")
        yield b""

    other = h.upload(forbidden_body(), owner=1)
    absent = h.upload(forbidden_body(), project=uuid4())
    assert other.status_code == absent.status_code == 404
    assert other.content == absent.content
    assert len(closed) == before + 2


@pytest.mark.parametrize(
    "headers",
    [{}, {"Content-Type": "text/plain"}, {"Content-Type": "multipart/form-data"}],
)
def test_content_type(h, headers):
    headers["Authorization"] = h.headers()["Authorization"]
    assert h.upload(b"bad", headers=headers).status_code == 415
    assert h.count() == h.before and not h.files()


@pytest.mark.parametrize("length", [None, "1", "999999999"])
def test_actual_byte_limit_not_content_length(h, length):
    h.app.state.settings.zip_max_compressed_bytes = 50
    headers = h.headers()
    if length is not None:
        headers["Content-Length"] = length
    response = h.upload(iter([b"a" * 30, b"b" * 30]), headers=headers)
    assert response.status_code == 413
    assert h.count() == h.before and not h.files()
    assert h.app.state.active_uploads == 0


@pytest.mark.parametrize(
    "name",
    [
        "/absolute",
        "C:drive",
        "C:/drive",
        "//unc/file",
        "a\\b",
        "../outside",
        "a/../b",
        "a/./b",
        "a//b",
        "a/",
        "CON.txt",
        "a. ",
        "a:name",
        "bad\x01name",
    ],
)
def test_unsafe_names(h, name):
    data = archive([(name, b"x")])
    response = h.upload(data)
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid or unsupported ZIP"}
    assert h.count() == h.before and not h.files()


@pytest.mark.parametrize(
    "entries",
    [
        [("a", b"x"), ("a", b"y")],
        [("a", b"x"), ("a/b", b"y")],
        [("a/b", b"x"), ("a", b"y")],
        [("a/", b""), ("a", b"x")],
        [("e\u0301", b"x"), ("é", b"y")],
    ],
)
def test_duplicates_and_conflicts(h, entries):
    with (
        pytest.warns(UserWarning)
        if entries[0][0] == entries[1][0]
        else __import__("contextlib").nullcontext()
    ):
        data = archive(entries)
    assert h.upload(data).status_code == 422
    assert h.count() == h.before and not h.files()


@pytest.mark.parametrize(
    "kind",
    [
        "empty",
        "invalid",
        "empty_archive",
        "directory_only",
        "truncated",
        "encrypted",
        "method",
        "nul",
        "local_mismatch",
        "overlap",
        "zip64",
    ],
)
def test_invalid_structures(h, kind):
    data = archive()
    if kind == "empty":
        data = b""
    elif kind == "invalid":
        data = b"not zip"
    elif kind == "empty_archive":
        data = archive([])
    elif kind == "directory_only":
        data = archive([("a/", b"")])
    elif kind == "truncated":
        data = data[:-8]
    else:
        data = bytearray(data)
        central = data.index(b"PK\x01\x02")
        if kind == "encrypted":
            struct.pack_into("<H", data, 6, 1)
            struct.pack_into("<H", data, central + 8, 1)
        elif kind == "method":
            struct.pack_into("<H", data, 8, 99)
            struct.pack_into("<H", data, central + 10, 99)
        elif kind == "nul":
            data[30] = data[central + 46] = (
                0  # Original name, not ZipInfo's truncated name.
            )
        elif kind == "local_mismatch":
            data[30] = ord("z")
        elif kind == "overlap":
            struct.pack_into("<I", data, central + 20, len(data))
        elif kind == "zip64":
            struct.pack_into("<I", data, central + 24, 0xFFFFFFFF)
    assert h.upload(bytes(data)).status_code == 422
    assert h.count() == h.before and not h.files()


@pytest.mark.parametrize(
    "mode", [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR, stat.S_IFSOCK]
)
def test_special_files(h, mode):
    entry = zipfile.ZipInfo("special")
    entry.create_system = 3
    entry.external_attr = (mode | 0o600) << 16
    assert h.upload(archive([(entry, b"target")])).status_code == 422
    assert not h.files()


@pytest.mark.parametrize(
    "setting,value,entries",
    [
        ("zip_max_entries", 1, [("a/", b""), ("a/b", b"x")]),
        ("zip_max_file_bytes", 2, [("a", b"xxx")]),
        ("zip_max_uncompressed_bytes", 3, [("a", b"xx"), ("b", b"xx")]),
        ("zip_max_filename_chars", 2, [("abc", b"x")]),
    ],
)
def test_configured_limits(h, setting, value, entries):
    setattr(h.app.state.settings, setting, value)
    assert h.upload(archive(entries)).status_code == 422
    assert h.count() == h.before and not h.files()


def test_payload_is_never_decompressed(h, monkeypatch):
    data = archive()

    def forbidden(*args, **kwargs):
        pytest.fail("Payload decompressed")

    monkeypatch.setattr(zipfile.ZipFile, "open", forbidden)
    assert h.upload(data).status_code == 201


def test_persistence_failure_removes_published_file(h, monkeypatch):
    original = api.persist

    def failed(*args):
        assert args[1].destination.exists()
        raise OperationalError("private", {}, Exception("secret details"))

    monkeypatch.setattr(api, "persist", failed)
    response = h.upload(archive())
    assert response.status_code == 503 and response.json() == {
        "detail": "Service unavailable"
    }
    assert not h.files() and h.count() == h.before
    monkeypatch.setattr(api, "persist", original)
    assert h.upload(archive()).status_code == 201


def test_timeout_disconnect_capacity_and_cancellation(h):
    async def scenario():
        app = h.app
        app.state.settings.upload_max_active = 1
        app.state.settings.upload_max_seconds = 0.08
        event = asyncio.Event()

        async def slow():
            yield b"initial"
            event.set()
            await asyncio.sleep(10)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            path = f"/v1/projects/{h.projects[0]}/artifacts"
            pending = asyncio.create_task(
                client.post(path, headers=h.headers(), content=slow())
            )
            await asyncio.wait_for(event.wait(), 2)
            assert (
                await client.post(path, headers=h.headers(), content=archive())
            ).status_code == 503
            assert (await client.get("/health/live")).status_code == 200
            assert (await pending).status_code == 408
            assert app.state.active_uploads == 0 and not h.files()
            # Direct ASGI disconnect after a partial chunk.
            messages = iter(
                [
                    {"type": "http.request", "body": b"partial", "more_body": True},
                    {"type": "http.disconnect"},
                ]
            )
            sent = []

            async def receive():
                return next(messages)

            async def send(message):
                sent.append(message)

            scope = {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": path,
                "raw_path": path.encode(),
                "query_string": b"",
                "root_path": "",
                "server": ("test", 80),
                "client": ("local", 1),
                "headers": [
                    (k.lower().encode(), v.encode()) for k, v in h.headers().items()
                ],
            }
            await app(scope, receive, send)
            assert sent[0]["status"] == 400 and not h.files()
            event.clear()
            pending = asyncio.create_task(
                client.post(path, headers=h.headers(), content=slow())
            )
            await asyncio.wait_for(event.wait(), 2)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert app.state.active_uploads == 0 and not h.files()
            app.state.settings.upload_max_seconds = 60
            assert (
                await client.post(path, headers=h.headers(), content=archive())
            ).status_code == 201

    asyncio.run(scenario())


def test_reconciliation_conservative(h):
    result = h.upload(archive()).json()
    old = time.time() - 90000
    with Session(h.engine) as session:
        referenced = h.root / session.get(Artifact, UUID(result["id"])).storage_key
    os.utime(referenced, (old, old))
    orphan = h.root / (str(uuid4()) + ".zip")
    staging = h.root / ("." + str(uuid4()) + ".part")
    recent = h.root / (str(uuid4()) + ".zip")
    unrelated = h.root / "unknown-file"
    for p in (orphan, staging, recent, unrelated):
        p.write_bytes(b"local test")
    for p in (orphan, staging, unrelated):
        os.utime(p, (old, old))
    symlink = h.root / (str(uuid4()) + ".zip")
    symlink.symlink_to(orphan)
    before = h.files()
    results = reconcile(h.app.state.settings, h.engine)
    assert len(results) == 2 and all(r["action"] == "would_delete" for r in results)
    assert h.files() == before
    active = UploadFile(h.root)
    active.open()
    try:
        with pytest.raises(BlockingIOError):
            reconcile(h.app.state.settings, h.engine, delete=True)
        assert orphan.exists() and active.temporary.exists()
    finally:
        active.cleanup()
    with pytest.raises(ValueError):
        reconcile(h.app.state.settings, h.engine, delete=True, min_age=1)
    assert len(reconcile(h.app.state.settings, h.engine, delete=True)) == 2
    assert not orphan.exists() and not staging.exists()
    assert (
        referenced.exists()
        and recent.exists()
        and unrelated.exists()
        and symlink.is_symlink()
    )
    with pytest.raises(ValueError):
        storage_root(__import__("pathlib").Path.cwd() / "unsafe-storage")


def test_migration_and_constraints(h, migrated):
    data = h.upload(archive()).json()
    with Session(h.engine) as session:
        row = session.get(Artifact, UUID(data["id"]))
        row.size_bytes = -1
        with pytest.raises(IntegrityError):
            session.commit()
    engine, name = migrated
    migration(engine, name, command.downgrade, "0002_api_tokens")
    with engine.connect() as c:
        assert "artifacts" not in inspect(c).get_table_names()
        assert {"users", "projects", "api_tokens"} <= set(inspect(c).get_table_names())
    migration(engine, name, command.upgrade, "head")


def test_validation_is_bounded_and_not_abandoned_on_cancel(h, monkeypatch):
    import threading

    entered, release = threading.Event(), threading.Event()
    original = api.validate_zip

    def slow_validation(path, settings):
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        entered.set()
        assert release.wait(3)
        return original(path, settings)

    monkeypatch.setattr(api, "validate_zip", slow_validation)
    h.app.state.settings.upload_max_active = 1

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=h.app), base_url="http://test"
        ) as client:
            path = f"/v1/projects/{h.projects[0]}/artifacts"
            pending = asyncio.create_task(
                client.post(path, content=archive(), headers=h.headers())
            )
            assert await asyncio.to_thread(entered.wait, 2)
            assert (
                await client.post(path, content=archive(), headers=h.headers())
            ).status_code == 503
            pending.cancel()
            await asyncio.sleep(0.01)
            assert not pending.done() and h.app.state.active_uploads == 1
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert h.app.state.active_uploads == 0 and not h.files()

    try:
        asyncio.run(scenario())
    finally:
        release.set()
    assert h.count() == h.before


def test_commit_failure_rolls_back_and_removes_published_file(h, monkeypatch):
    class FailingSession(Session):
        def commit(self):
            self.flush()
            raise OperationalError("private", {}, Exception("private"))

    monkeypatch.setattr(api, "Session", FailingSession)
    assert h.upload(archive()).status_code == 503
    assert h.count() == h.before and not h.files()


def test_commit_acknowledgement_loss_preserves_referenced_file(h, monkeypatch):
    class UncertainSession(Session):
        def commit(self):
            super().commit()
            raise OperationalError("private", {}, Exception("lost acknowledgement"))

    monkeypatch.setattr(api, "Session", UncertainSession)
    assert h.upload(archive()).status_code == 503
    assert h.count() == h.before + 1 and len(h.files()) == 1


def test_stored_and_data_descriptor_archives(h):
    assert h.upload(archive(method=zipfile.ZIP_STORED)).status_code == 201

    class Nonseekable(io.BytesIO):
        def seekable(self):
            return False

        def seek(self, *args):
            raise io.UnsupportedOperation()

    buffer = Nonseekable()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("nested/file", b"hello")
    assert h.upload(buffer.getvalue()).status_code == 201


def test_each_artifact_nonnegative_constraint(h):
    response = h.upload(archive())
    assert response.status_code == 201
    for field in ("size_bytes", "file_count", "declared_uncompressed_bytes"):
        with Session(h.engine) as session:
            row = session.get(Artifact, UUID(response.json()["id"]))
            setattr(row, field, -1)
            with pytest.raises(IntegrityError):
                session.commit()


def test_close_error_still_unlinks_and_releases_storage_lock(h):
    upload = UploadFile(h.root)
    upload.open()
    actual_file = upload.file

    class CloseError:
        def close(self):
            actual_file.close()
            raise OSError("simulated flush failure")

    upload.file = CloseError()
    with pytest.raises(OSError):
        upload.cleanup()
    assert not upload.temporary.exists() and upload.lock is None
    assert reconcile(h.app.state.settings, h.engine) == []
