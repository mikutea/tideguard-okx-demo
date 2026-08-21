from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import okx_demo_lab.sqlite_runtime as runtime
from okx_demo_lab.sqlite_runtime import SQLiteRuntimeError, configure_sqlite_connection


def test_remote_storage_requires_persistent_rollback_journal_and_full_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.sqlite3"
    monkeypatch.setattr(runtime, "is_remote_storage", lambda _path: True)
    with sqlite3.connect(path) as db:
        assert configure_sqlite_connection(db, path) == "persist"
        assert str(db.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "persist"
        assert int(db.execute("PRAGMA journal_size_limit").fetchone()[0]) == 8_388_608
        assert int(db.execute("PRAGMA synchronous").fetchone()[0]) == 2
        assert int(db.execute("PRAGMA foreign_keys").fetchone()[0]) == 1


def test_local_storage_retains_wal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    monkeypatch.setattr(runtime, "is_remote_storage", lambda _path: False)
    with sqlite3.connect(path) as db:
        assert configure_sqlite_connection(db, path) == "wal"
        assert str(db.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"


def test_invalid_timeout_is_rejected(tmp_path: Path) -> None:
    with sqlite3.connect(tmp_path / "state.sqlite3") as db:
        with pytest.raises(SQLiteRuntimeError, match="timeout"):
            configure_sqlite_connection(db, tmp_path, busy_timeout_ms=0)
