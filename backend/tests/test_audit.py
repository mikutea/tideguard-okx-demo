import sqlite3
from pathlib import Path

import pytest

from okx_demo_lab.audit import AuditStore, PersistentStateError


def test_audit_chain_and_redaction(tmp_path: Path) -> None:
    store = AuditStore(tmp_path / "audit.sqlite3")
    event = store.append(
        "test",
        {"api_key": "must-not-survive", "nested": {"passphrase": "also-secret"}, "safe": "ok"},
    )
    assert event["payload"]["api_key"] == "[REDACTED]"
    assert event["payload"]["nested"]["passphrase"] == "[REDACTED]"
    assert event["payload"]["safe"] == "ok"
    assert store.verify_chain()


def test_store_explicitly_closes_connections(tmp_path: Path, monkeypatch) -> None:
    store = AuditStore(tmp_path / "connections.sqlite3")

    class TrackedConnection(sqlite3.Connection):
        explicitly_closed = False

        def close(self) -> None:
            self.explicitly_closed = True
            super().close()

    connections: list[TrackedConnection] = []

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(
            store.path, timeout=5, factory=TrackedConnection
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connections.append(connection)
        return connection

    monkeypatch.setattr(store, "_connect", connect)
    store.get_flag("kill_active")
    store.append("connection.checked")

    assert len(connections) == 2
    assert all(connection.explicitly_closed for connection in connections)


@pytest.mark.parametrize(
    ("name", "value"),
    [("kill_active", "CORRUPT"), ("kill_generation", "-1")],
)
def test_malformed_safety_flags_are_rejected(
    tmp_path: Path, name: str, value: str
) -> None:
    store = AuditStore(tmp_path / "flags.sqlite3")
    assert store.get_flag("kill_active") == "false"
    assert store.get_kill_generation() == 0

    store.set_flag(name, value)

    with pytest.raises(PersistentStateError):
        store.get_kill_state()
