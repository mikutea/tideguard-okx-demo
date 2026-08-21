from __future__ import annotations

import os
import sqlite3
from pathlib import Path


class SQLiteRuntimeError(RuntimeError):
    pass


def is_remote_storage(path: Path) -> bool:
    """Return whether SQLite would live on a Windows UNC or mapped drive."""

    if os.name != "nt":
        return False
    value = os.path.abspath(os.fspath(path))
    if value.startswith("\\\\"):
        return True
    drive, _tail = os.path.splitdrive(value)
    if not drive:
        return False
    try:
        import ctypes

        drive_type = int(ctypes.windll.kernel32.GetDriveTypeW(f"{drive}\\"))
        # UNKNOWN, NO_ROOT_DIR and REMOTE do not provide affirmative evidence
        # that SQLite shared-memory WAL locking is safe.
        return drive_type in {0, 1, 4}
    except (AttributeError, OSError, ValueError):
        # Unknown drive semantics are not sufficient evidence that WAL shared
        # memory locking is safe. Prefer rollback journaling on Windows.
        return True


def configure_sqlite_connection(
    connection: sqlite3.Connection,
    path: Path,
    *,
    busy_timeout_ms: int = 10_000,
) -> str:
    """Keep WAL locally, but require persistent rollback journal remotely.

    PERSIST keeps SQLite's crash-safe rollback protocol while avoiding a
    create/delete cycle for every commit.  That directory churn is unreliable
    on some SMB appliances when sibling databases share a basename.
    """

    if (
        not isinstance(busy_timeout_ms, int)
        or isinstance(busy_timeout_ms, bool)
        or not 1_000 <= busy_timeout_ms <= 60_000
    ):
        raise SQLiteRuntimeError("SQLite busy timeout is invalid")
    connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
    expected = "persist" if is_remote_storage(path) else "wal"
    actual = str(
        connection.execute(f"PRAGMA journal_mode={expected.upper()}").fetchone()[0]
    ).lower()
    if actual != expected:
        raise SQLiteRuntimeError(
            f"SQLite refused the required {expected.upper()} journal mode"
        )
    if expected == "persist":
        connection.execute("PRAGMA journal_size_limit=8388608")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA foreign_keys=ON")
    return actual


__all__ = [
    "SQLiteRuntimeError",
    "configure_sqlite_connection",
    "is_remote_storage",
]
