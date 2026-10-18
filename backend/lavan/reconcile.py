"""Conservative local orphan reconciliation; dry-run unless --delete is explicit."""

import argparse
import json
import os
import re
import stat
import time

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from lavan.config import Settings
from lavan.db import database_engine
from lavan.models import Artifact
from lavan.storage import storage_lock, storage_root

UUID_NAME = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
NAME = re.compile(rf"(?:({UUID_NAME})\.zip|\.({UUID_NAME})\.part)")
MINIMUM_AGE = 3600


def reconcile(settings, engine, *, delete=False, min_age=86400):
    if min_age < MINIMUM_AGE:
        raise ValueError("Minimum age must be at least 3600 seconds")
    root = storage_root(settings.artifact_storage_dir)
    if not root.exists():
        return []
    if root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise ValueError("Storage must be private and owned by the service user")
    lock = storage_lock(root, exclusive=True)  # Busy => no snapshot or deletion.
    try:
        with Session(engine) as session:
            referenced = set(session.scalars(select(Artifact.storage_key)))
        cutoff = time.time() - min_age
        results = []
        for path in root.iterdir():
            match = NAME.fullmatch(path.name)
            if not match or path.is_symlink() or path.resolve().parent != root:
                continue
            info = path.lstat()
            key = (match[1] or match[2]) + ".zip"
            if (
                not stat.S_ISREG(info.st_mode)
                or key in referenced
                or info.st_mtime > cutoff
            ):
                continue
            if delete:
                path.unlink()
            results.append(
                {
                    "id": match[1] or match[2],
                    "kind": "published" if match[1] else "temporary",
                    "action": "deleted" if delete else "would_delete",
                }
            )
        return results
    finally:
        os.close(lock)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delete", action="store_true")
    parser.add_argument("--min-age-seconds", type=int, default=86400)
    args = parser.parse_args(argv)
    if args.min_age_seconds < MINIMUM_AGE:
        parser.error("minimum age must be at least 3600 seconds")
    engine = database_engine()
    try:
        results = reconcile(
            Settings(), engine, delete=args.delete, min_age=args.min_age_seconds
        )
        print(json.dumps(results))
        return 0
    except (OSError, SQLAlchemyError, ValueError):
        print(
            "Reconciliation unavailable: verify storage and database; "
            "active uploads may hold the lock."
        )
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
