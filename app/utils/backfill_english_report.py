"""Persist exclusive rollback evidence before either English backfill mutates DB."""
import json
import os
from pathlib import Path


def persist_report(path: Path, report: dict) -> None:
    # Require an existing host-persisted directory; do not create an unflushed
    # directory ancestry and mistake it for durable recovery evidence.
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.fsync(directory)
    finally:
        os.close(directory)
