"""Private Linux storage with cooperative upload/reconciliation locking."""

import fcntl
import os
from pathlib import Path
from uuid import uuid4


def storage_root(configured):
    root = configured.expanduser().resolve()
    source = Path(__file__).resolve().parents[1]
    repository = next((p for p in source.parents if (p / ".git").exists()), source)
    if not configured.is_absolute() or root == repository or repository in root.parents:
        raise ValueError(
            "Artifact storage must be absolute and outside the source tree"
        )
    if root == Path(root.anchor) or root == Path.home():
        raise ValueError("Use a dedicated private storage directory")
    return root


def prepare_root(configured):
    root = storage_root(configured)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.stat().st_uid != os.getuid():
        raise OSError("Private storage must belong to the service user")
    root.chmod(0o700)
    return root


def storage_lock(root, exclusive=False):
    fd = os.open(root / ".uploads.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
    except BaseException:
        os.close(fd)
        raise
    return fd


class UploadFile:
    def __init__(self, configured):
        self.configured = configured
        self.id = uuid4()
        self.key = f"{self.id}.zip"
        self.lock = self.file = self.root = None
        self.published = self.persisted = self.created = False
        self.keep_published = False

    def open(self):
        self.root = prepare_root(self.configured)
        self.lock = storage_lock(self.root)
        self.temporary = self.root / f".{self.id}.part"
        self.destination = self.root / self.key
        fd = os.open(
            self.temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        self.created = True
        self.file = os.fdopen(fd, "wb")

    def write(self, chunk):
        self.file.write(chunk)

    def finish(self):
        self.file.flush()
        os.fsync(self.file.fileno())
        self.file.close()
        self.file = None

    def publish(self):
        # Atomic, same-filesystem publication without overwriting any existing file.
        os.link(self.temporary, self.destination)
        self.published = True
        self.temporary.unlink()
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def cleanup(self):
        failed = False
        operations = []
        if self.file is not None:
            operations.append(self.file.close)
        if self.created:
            operations.append(lambda: self.temporary.unlink(missing_ok=True))
        if self.published and not (self.persisted or self.keep_published):
            operations.append(lambda: self.destination.unlink(missing_ok=True))
        try:
            for operation in operations:
                try:
                    operation()
                except OSError:
                    # A close/flush error must not skip unlinking or lock release.
                    failed = True
        finally:
            if self.lock is not None:
                os.close(self.lock)
                self.lock = None
        if failed:
            raise OSError("Artifact cleanup failed")
