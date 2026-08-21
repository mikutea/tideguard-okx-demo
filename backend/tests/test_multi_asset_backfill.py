from __future__ import annotations

import json
import socket
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from research.backfill_universe import (  # noqa: E402
    ExclusiveFileLock,
    FrozenUniverse,
    HistoryCoordinatorBusy,
    HistoryCoordinatorError,
    MultiAssetHistoryCoordinator,
    _public_client,
    _windows_pid_exists,
    load_frozen_universe,
)
from okx_demo_lab.config import ALLOWED_INSTRUMENTS  # noqa: E402
from okx_demo_lab.ml.multi_asset_market import (  # noqa: E402
    FIVE_MINUTES_MS,
    MultiAssetMarketStore,
    OriginProbe,
)
from okx_demo_lab.ml.strategy import canonical_json, sha256_hex  # noqa: E402
from okx_demo_lab.ml.universe import UNIVERSE_SCHEMA_VERSION  # noqa: E402


NOW = datetime(2026, 8, 21, 3, 0, tzinfo=timezone.utc)
FEATURE_HASH = "9" * 64


class FakeWinFunction:
    def __init__(self, result: int) -> None:
        self.result = result
        self.argtypes: object = None
        self.restype: object = None
        self.calls: list[tuple[object, ...]] = []

    def __call__(self, *args: object) -> int:
        self.calls.append(args)
        return self.result


class FakeKernel32:
    def __init__(self, handle: int = 0) -> None:
        self.OpenProcess = FakeWinFunction(handle)
        self.CloseHandle = FakeWinFunction(1)


def candle(timestamp: int, *, price: str = "100", confirm: str = "1") -> list[str]:
    numeric = float(price)
    return [
        str(timestamp),
        price,
        str(numeric + 2),
        str(numeric - 2),
        price,
        "10",
        "20",
        "20",
        confirm,
    ]


def _member(
    instrument: str,
    *,
    ticker_at: str = "2026-08-21T03:00:00.000Z",
) -> dict[str, object]:
    base = instrument.removesuffix("-USDT")
    return {
        "ask": "101",
        "baseCurrency": base,
        "bid": "99",
        "instrument": instrument,
        "last": "100",
        "listedAt": "2020-01-01T00:00:00.000Z",
        "lotSize": "0.0001",
        "minSize": "0.001",
        "quoteCurrency": "USDT",
        "quoteVolume24h": "50000000",
        "spreadBps": "2",
        "tickSize": "0.01",
        "tickerAt": ticker_at,
    }


def write_frozen_universe(path: Path, instruments: tuple[str, ...]) -> Path:
    snapshot: dict[str, object] = {
        "createdAt": "2026-08-21T03:00:00.000Z",
        "instrumentRows": len(instruments),
        "members": [_member(instrument) for instrument in instruments],
        "policySha256": "8" * 64,
        "schemaVersion": UNIVERSE_SCHEMA_VERSION,
        "tickerRows": len(instruments),
    }
    snapshot["sha256"] = sha256_hex(canonical_json(snapshot))
    report: dict[str, object] = {
        "executionAllowlist": ["BTC-USDT"],
        "executionAllowlistChanged": False,
        "nextGate": "complete_history_and_aligned_portfolio_oos",
        "snapshot": snapshot,
    }
    report["reportSha256"] = sha256_hex(canonical_json(report))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def data_dir(tmp_path: Path) -> Path:
    # scripts/check.ps1 pins pytest's base temp below the project. The injected
    # root keeps the same .research-data boundary in each isolated test.
    root = tmp_path / f"workspace-{uuid.uuid4().hex}"
    (root / ".research-data").mkdir(parents=True)
    return root


class FakeHistoryClient:
    def __init__(
        self,
        responses: dict[
            tuple[str, int | None, int], list[tuple[list[list[Any]], int | None]]
        ],
    ) -> None:
        self.responses = responses
        self.calls: list[tuple[str, int | None, int]] = []

    async def iter_history_candle_pages(
        self,
        inst_id: str,
        *,
        bar: str = "5m",
        after: int | None = None,
        page_limit: int = 300,
    ) -> AsyncIterator[tuple[list[list[Any]], int | None]]:
        assert bar == "5m"
        key = (inst_id, after, page_limit)
        self.calls.append(key)
        for value in self.responses.get(key, [([], after)]):
            yield value


class CountingStatusStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.timestamps: set[int] = set()
        self.status_calls = 0
        self.origin_probes = 0

    def ingest(
        self,
        rows: list[list[Any]],
        *,
        instrument: str,
        observed_at: datetime,
    ) -> dict[str, Any]:
        assert instrument == "BTC-USDT"
        assert observed_at.tzinfo is not None
        before = len(self.timestamps)
        self.timestamps.update(int(row[0]) for row in rows if str(row[8]) == "1")
        inserted = len(self.timestamps) - before
        return {
            "conflicts": 0,
            "duplicates": len(rows) - inserted,
            "inserted": inserted,
            "unconfirmed": 0,
        }

    def status(self, instrument: str) -> dict[str, Any]:
        assert instrument == "BTC-USDT"
        self.status_calls += 1
        first = min(self.timestamps) if self.timestamps else None
        last = max(self.timestamps) if self.timestamps else None
        rows = len(self.timestamps)
        expected = ((last - first) // FIVE_MINUTES_MS + 1) if rows else 0
        missing = expected - rows
        return {
            "backfillComplete": False,
            "bar": "5m",
            "expectedRows": expected,
            "firstOpenTsMs": first,
            "gapRanges": [],
            "instrument": instrument,
            "lastOpenTsMs": last,
            "missingBars": missing,
            "originProbeCount": self.origin_probes,
            "readyForSnapshot": False,
            "source": "okx-public-v5",
            "storedRows": rows,
            "unresolvedConflicts": 0,
        }

    def record_origin_probe(self, probe: object) -> dict[str, Any]:
        self.origin_probes += 1
        return {"backfillComplete": False, "role": "baseline"}


def coordinator(
    *,
    root: Path,
    client: FakeHistoryClient,
    run_id: str,
    now: datetime,
    page_budget: int = 20,
) -> tuple[MultiAssetHistoryCoordinator, MultiAssetMarketStore, FrozenUniverse]:
    data = root / ".research-data"
    universe = load_frozen_universe(
        write_frozen_universe(data / "universe.json", ("BTC-USDT",)),
        project_root=root,
    )
    store = MultiAssetMarketStore(data / "market.sqlite3")
    task = MultiAssetHistoryCoordinator(
        store=store,
        client=client,
        universe=universe,
        progress_path=data / "progress.json",
        page_budget=page_budget,
        run_id=run_id,
        now=lambda: now,
        feature_contract_sha256=FEATURE_HASH,
    )
    return task, store, universe


def test_o_excl_lock_rejects_a_concurrent_writer_without_deleting_owner(tmp_path) -> None:
    root = data_dir(tmp_path)
    path = root / ".research-data" / "history.lock"
    first = ExclusiveFileLock(path, run_id="run-one")
    second = ExclusiveFileLock(path, run_id="run-two")

    first.acquire()
    owner = path.read_text(encoding="utf-8")
    with pytest.raises(HistoryCoordinatorBusy, match="another history coordinator"):
        second.acquire()
    assert path.read_text(encoding="utf-8") == owner
    first.release()

    second.acquire()
    second.release()
    assert not path.exists()


@pytest.mark.parametrize(
    ("windows_error", "expected"),
    ((87, False), (5, True), (6, True), (0, True)),
)
def test_windows_pid_check_only_treats_invalid_parameter_as_dead(
    windows_error: int, expected: bool
) -> None:
    api = FakeKernel32()

    assert _windows_pid_exists(
        12345, kernel32=api, get_last_error=lambda: windows_error
    ) is expected
    assert api.OpenProcess.argtypes is not None
    assert api.OpenProcess.restype is not None
    assert api.CloseHandle.argtypes is not None
    assert api.CloseHandle.restype is not None


def test_stale_lock_needs_same_host_dead_pid_and_timeout_before_quarantine(tmp_path) -> None:
    root = data_dir(tmp_path)
    path = root / ".research-data" / "history.lock"
    old_owner = {
        "host": socket.gethostname(),
        "nonce": "a" * 32,
        "pid": 2_000_000_000,
        "runId": "crashed-run",
        "startedAt": "2026-08-21T02:00:00.000Z",
    }
    material = canonical_json(old_owner)
    path.write_text(material, encoding="utf-8")
    recovering = ExclusiveFileLock(
        path,
        run_id="replacement-run",
        now=lambda: NOW,
        pid_exists=lambda _pid: False,
        stale_after=timedelta(minutes=15),
    )

    recovering.acquire()
    current = json.loads(path.read_text(encoding="utf-8"))
    assert current["runId"] == "replacement-run"
    assert current["nonce"] == recovering.nonce
    quarantined = list(path.parent.glob(".history.lock.stale-*.json"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == material
    recovering.release()

    path.write_text(material, encoding="utf-8")
    live = ExclusiveFileLock(
        path,
        run_id="must-not-steal",
        now=lambda: NOW,
        pid_exists=lambda _pid: True,
        stale_after=timedelta(minutes=15),
    )
    with pytest.raises(HistoryCoordinatorBusy, match="another history coordinator"):
        live.acquire()
    assert path.read_text(encoding="utf-8") == material


def test_frozen_universe_rejects_hash_tampering_and_runtime_escape(tmp_path) -> None:
    root = data_dir(tmp_path)
    path = write_frozen_universe(
        root / ".research-data" / "universe.json", ("BTC-USDT", "ETH-USDT")
    )
    frozen = load_frozen_universe(path, project_root=root)
    parsed = json.loads(path.read_text(encoding="utf-8"))
    parsed["snapshot"]["members"][1]["instrument"] = "SOL-USDT"
    path.write_text(json.dumps(parsed), encoding="utf-8")

    with pytest.raises(HistoryCoordinatorError, match="report hash mismatch"):
        load_frozen_universe(path, project_root=root)
    with pytest.raises(HistoryCoordinatorError, match="changed during the run"):
        frozen.assert_unchanged()
    outside = root / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    with pytest.raises(HistoryCoordinatorError, match="runtime paths"):
        load_frozen_universe(outside, project_root=root)


def test_frozen_universe_accepts_exchange_clock_skew_within_discovery_window(
    tmp_path,
) -> None:
    root = data_dir(tmp_path)
    path = write_frozen_universe(
        root / ".research-data" / "universe.json", ("BTC-USDT",)
    )
    report = json.loads(path.read_text(encoding="utf-8"))
    report["snapshot"]["members"][0]["tickerAt"] = "2026-08-21T03:00:04.999Z"
    snapshot = report["snapshot"]
    snapshot["sha256"] = sha256_hex(
        canonical_json({key: value for key, value in snapshot.items() if key != "sha256"})
    )
    report["reportSha256"] = sha256_hex(
        canonical_json(
            {key: value for key, value in report.items() if key != "reportSha256"}
        )
    )
    path.write_text(json.dumps(report), encoding="utf-8")

    frozen = load_frozen_universe(path, project_root=root)

    assert frozen.instruments == ("BTC-USDT",)


def test_frozen_universe_rejects_exchange_clock_skew_beyond_discovery_window(
    tmp_path,
) -> None:
    root = data_dir(tmp_path)
    path = write_frozen_universe(
        root / ".research-data" / "universe.json", ("BTC-USDT",)
    )
    report = json.loads(path.read_text(encoding="utf-8"))
    report["snapshot"]["members"][0]["tickerAt"] = "2026-08-21T03:00:05.001Z"
    snapshot = report["snapshot"]
    snapshot["sha256"] = sha256_hex(
        canonical_json({key: value for key, value in snapshot.items() if key != "sha256"})
    )
    report["reportSha256"] = sha256_hex(
        canonical_json(
            {key: value for key, value in report.items() if key != "reportSha256"}
        )
    )
    path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(HistoryCoordinatorError, match="timestamps are inconsistent"):
        load_frozen_universe(path, project_root=root)


@pytest.mark.asyncio
async def test_per_page_progress_does_not_run_full_status_scans(tmp_path) -> None:
    root = data_dir(tmp_path)
    data = root / ".research-data"
    universe = load_frozen_universe(
        write_frozen_universe(data / "universe.json", ("BTC-USDT",)),
        project_root=root,
    )
    newest = FIVE_MINUTES_MS * 50
    oldest_pages = [
        ([candle(FIVE_MINUTES_MS * index)], FIVE_MINUTES_MS * index)
        for index in range(49, 9, -1)
    ]
    oldest_pages.append(([], FIVE_MINUTES_MS * 10))
    client = FakeHistoryClient(
        {
            ("BTC-USDT", None, 300): [([candle(newest)], newest)],
            ("BTC-USDT", newest, 300): oldest_pages,
            (
                "BTC-USDT",
                FIVE_MINUTES_MS * 11,
                100,
            ): [([candle(FIVE_MINUTES_MS * 10)], FIVE_MINUTES_MS * 10)],
        }
    )
    store = CountingStatusStore(data / "counting.sqlite3")
    task = MultiAssetHistoryCoordinator(
        store=store,  # type: ignore[arg-type]
        client=client,
        universe=universe,
        progress_path=data / "progress.json",
        page_budget=50,
        run_id="bounded-status-scans",
        now=lambda: NOW,
        feature_contract_sha256=FEATURE_HASH,
    )

    result = await task.run()

    assert result["pagesConsumed"] == 43
    assert store.status_calls <= 7
    progress = json.loads((data / "progress.json").read_text(encoding="utf-8"))
    assert progress["instruments"]["BTC-USDT"]["pagesConsumed"] == 43


@pytest.mark.asyncio
async def test_origin_is_baselined_then_confirmed_by_a_later_distinct_run(tmp_path) -> None:
    root = data_dir(tmp_path)
    newest = FIVE_MINUTES_MS * 5
    middle = FIVE_MINUTES_MS * 4
    oldest = FIVE_MINUTES_MS * 3
    first_client = FakeHistoryClient(
        {
            ("BTC-USDT", None, 300): [([candle(newest), candle(middle)], middle)],
            ("BTC-USDT", middle, 300): [([candle(oldest)], oldest), ([], oldest)],
            (
                "BTC-USDT",
                oldest + FIVE_MINUTES_MS,
                100,
            ): [([candle(oldest)], oldest)],
        }
    )
    first_task, first_store, _ = coordinator(
        root=root, client=first_client, run_id="origin-first", now=NOW
    )

    first_result = await first_task.run()
    first_status = first_store.status("BTC-USDT")
    assert first_result["state"] == "partial"
    assert first_status["firstOpenTsMs"] == oldest
    assert first_status["originProbeCount"] == 1
    assert first_status["backfillComplete"] is False

    second_client = FakeHistoryClient(
        {
            ("BTC-USDT", None, 300): [([candle(newest), candle(middle)], middle)],
            ("BTC-USDT", oldest - 1, 100): [([], oldest - 1)],
            (
                "BTC-USDT",
                oldest + FIVE_MINUTES_MS,
                100,
            ): [([candle(oldest)], oldest)],
        }
    )
    second_universe = load_frozen_universe(
        root / ".research-data" / "universe.json", project_root=root
    )
    second_task = MultiAssetHistoryCoordinator(
        store=first_store,
        client=second_client,
        universe=second_universe,
        progress_path=root / ".research-data" / "progress.json",
        page_budget=20,
        run_id="origin-second",
        now=lambda: NOW + timedelta(seconds=61),
        feature_contract_sha256=FEATURE_HASH,
    )
    second_result = await second_task.run()

    assert ("BTC-USDT", oldest - 1, 100) in second_client.calls
    assert second_result["state"] == "complete"
    assert first_store.status("BTC-USDT")["backfillComplete"] is True
    latest = first_store.status("BTC-USDT")["latestSnapshot"]
    assert latest["featureContractSha256"] == FEATURE_HASH
    progress = json.loads(
        (root / ".research-data" / "progress.json").read_text(encoding="utf-8")
    )
    assert progress["state"] == "complete"
    assert progress["instruments"]["BTC-USDT"]["stage"] == "snapshotReady"


@pytest.mark.asyncio
async def test_all_empty_confirmation_run_cannot_complete_without_positive_anchor(
    tmp_path,
) -> None:
    root = data_dir(tmp_path)
    data = root / ".research-data"
    universe = load_frozen_universe(
        write_frozen_universe(data / "universe.json", ("BTC-USDT",)),
        project_root=root,
    )
    oldest = FIVE_MINUTES_MS * 3
    row = candle(oldest)
    store = MultiAssetMarketStore(data / "market.sqlite3")
    store.ingest([row], instrument="BTC-USDT", observed_at=NOW)
    store.record_origin_probe(
        OriginProbe(
            run_id="baseline-with-anchor",
            instrument="BTC-USDT",
            requested_after=oldest,
            terminal_cursor=oldest,
            page_limit=300,
            empty_probe_count=3,
            anchor_open_ts_ms=oldest,
            anchor_payload_sha256=sha256_hex(canonical_json(row)),
            observed_at=NOW,
        )
    )
    task = MultiAssetHistoryCoordinator(
        store=store,
        client=FakeHistoryClient({}),
        universe=universe,
        progress_path=data / "progress.json",
        page_budget=10,
        run_id="all-empty-confirmation",
        now=lambda: NOW + timedelta(seconds=61),
        feature_contract_sha256=FEATURE_HASH,
    )
    result = await task.run()
    assert result["state"] == "partial"
    assert store.status("BTC-USDT")["originProbeCount"] == 1
    assert store.status("BTC-USDT")["backfillComplete"] is False


@pytest.mark.asyncio
async def test_resume_uses_database_min_not_a_stale_progress_cursor(tmp_path) -> None:
    root = data_dir(tmp_path)
    data = root / ".research-data"
    universe = load_frozen_universe(
        write_frozen_universe(data / "universe.json", ("BTC-USDT",)),
        project_root=root,
    )
    store = MultiAssetMarketStore(data / "market.sqlite3")
    database_min = FIVE_MINUTES_MS * 10
    database_max = FIVE_MINUTES_MS * 11
    store.ingest(
        [candle(database_min), candle(database_max)],
        instrument="BTC-USDT",
        observed_at=NOW,
    )
    (data / "progress.json").write_text(
        json.dumps({"untrustedCursor": FIVE_MINUTES_MS * 999}), encoding="utf-8"
    )
    older = FIVE_MINUTES_MS * 9
    client = FakeHistoryClient(
        {
            ("BTC-USDT", None, 300): [([candle(database_max)], database_max)],
            ("BTC-USDT", database_min, 300): [([candle(older)], older)],
        }
    )
    task = MultiAssetHistoryCoordinator(
        store=store,
        client=client,
        universe=universe,
        progress_path=data / "progress.json",
        page_budget=2,
        run_id="resume-from-database",
        now=lambda: NOW,
        feature_contract_sha256=FEATURE_HASH,
    )

    result = await task.run()

    assert ("BTC-USDT", database_min, 300) in client.calls
    assert all(call[1] != FIVE_MINUTES_MS * 999 for call in client.calls)
    assert store.status("BTC-USDT")["firstOpenTsMs"] == older
    assert result["pagesConsumed"] == 2


def test_public_client_is_bound_to_frozen_research_members_and_execution_stays_btc_only(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = data_dir(tmp_path)
    frozen = load_frozen_universe(
        write_frozen_universe(
            root / ".research-data" / "universe.json",
            ("BTC-USDT", "ETH-USDT", "SOL-USDT"),
        ),
        project_root=root,
    )
    captured: dict[str, object] = {}

    class CapturingClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(
        "research.backfill_universe.OkxPublicMarketClient", CapturingClient
    )
    _public_client(frozen)

    assert captured["history_instruments"] == frozen.instruments
    assert ALLOWED_INSTRUMENTS == frozenset({"BTC-USDT"})
