from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import re
import socket
import sys
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from okx_demo_lab.config import ALLOWED_INSTRUMENTS
from okx_demo_lab.ml.multi_asset_market import (
    FIVE_MINUTES_MS,
    MultiAssetMarketError,
    MultiAssetMarketStore,
    OriginProbe,
)
from okx_demo_lab.ml.strategy import canonical_json, sha256_hex
from okx_demo_lab.ml.universe import MAX_TICKER_FUTURE_SKEW, UNIVERSE_SCHEMA_VERSION
from okx_demo_lab.public_market import OkxPublicMarketClient, PublicMarketError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESEARCH_DATA_ROOT = PROJECT_ROOT / ".research-data"
DEFAULT_UNIVERSE_PATH = RESEARCH_DATA_ROOT / "universes" / "universe-latest.json"
DEFAULT_DATABASE_PATH = RESEARCH_DATA_ROOT / "multi-asset-market.sqlite3"
DEFAULT_PROGRESS_PATH = RESEARCH_DATA_ROOT / "multi-asset-history-progress.json"
DEFAULT_LOCK_PATH = RESEARCH_DATA_ROOT / "multi-asset-history.lock"
PROGRESS_SCHEMA = "moheng.multi-asset-history-progress.v1"
MAX_FROZEN_UNIVERSE_BYTES = 1_000_000
MAX_PAGE_BUDGET = 20_000
STALE_LOCK_AFTER = timedelta(minutes=15)
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_INSTRUMENT_PATTERN = re.compile(r"^[A-Z0-9]{2,24}-USDT$")


class HistoryCoordinatorError(RuntimeError):
    pass


class HistoryCoordinatorBusy(HistoryCoordinatorError):
    pass


class PublicHistoryClient(Protocol):
    async def iter_history_candle_pages(
        self,
        inst_id: str,
        *,
        bar: str = "5m",
        after: int | None = None,
        page_limit: int = 300,
    ) -> AsyncIterator[tuple[list[list[Any]], int | None]]: ...


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise HistoryCoordinatorError("coordinator timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _runtime_path(
    value: Path,
    *,
    project_root: Path = PROJECT_ROOT,
    must_exist: bool = False,
) -> Path:
    root = project_root.resolve()
    candidate = value if value.is_absolute() else root / value
    resolved = candidate.resolve(strict=must_exist)
    allowed_root = (root / ".research-data").resolve()
    try:
        resolved.relative_to(allowed_root)
    except ValueError as exc:
        raise HistoryCoordinatorError(
            "runtime paths must stay inside project .research-data"
        ) from exc
    return resolved


def _require_hash(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _HASH_PATTERN.fullmatch(value):
        raise HistoryCoordinatorError(f"{name} is not a lowercase SHA-256")
    return value


def _require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HistoryCoordinatorError(f"{name} must be a JSON object")
    return dict(value)


def _parse_iso(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise HistoryCoordinatorError(f"{name} is not an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HistoryCoordinatorError(f"{name} is not an ISO-8601 timestamp") from exc
    return _utc(parsed)


def _decimal(value: Any, name: str, *, allow_zero: bool = False) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise HistoryCoordinatorError(f"{name} is not a valid decimal") from exc
    if not parsed.is_finite() or parsed < 0 or (parsed == 0 and not allow_zero):
        raise HistoryCoordinatorError(f"{name} is outside its valid range")
    return parsed


@dataclass(frozen=True)
class FrozenUniverse:
    path: Path
    file_sha256: str
    report_sha256: str
    snapshot_sha256: str
    instruments: tuple[str, ...]

    def assert_unchanged(self) -> None:
        try:
            material = self.path.read_bytes()
        except OSError as exc:
            raise HistoryCoordinatorError("frozen universe can no longer be read") from exc
        if not hmac.compare_digest(hashlib.sha256(material).hexdigest(), self.file_sha256):
            raise HistoryCoordinatorError("frozen universe changed during the run")


def load_frozen_universe(
    path: Path,
    *,
    project_root: Path = PROJECT_ROOT,
) -> FrozenUniverse:
    frozen_path = _runtime_path(path, project_root=project_root, must_exist=True)
    if not frozen_path.is_file():
        raise HistoryCoordinatorError("frozen universe path is not a regular file")
    try:
        material = frozen_path.read_bytes()
    except OSError as exc:
        raise HistoryCoordinatorError("frozen universe cannot be read") from exc
    if not material or len(material) > MAX_FROZEN_UNIVERSE_BYTES:
        raise HistoryCoordinatorError("frozen universe file size is invalid")
    try:
        parsed = json.loads(material)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HistoryCoordinatorError("frozen universe is not valid UTF-8 JSON") from exc
    report = _require_mapping(parsed, "frozen universe report")
    stored_report_hash = _require_hash(report.get("reportSha256"), "reportSha256")
    report_body = {key: value for key, value in report.items() if key != "reportSha256"}
    expected_report_hash = sha256_hex(canonical_json(report_body))
    if not hmac.compare_digest(stored_report_hash, expected_report_hash):
        raise HistoryCoordinatorError("frozen universe report hash mismatch")
    if report.get("executionAllowlistChanged") is not False:
        raise HistoryCoordinatorError("frozen universe attempted to change execution policy")
    if report.get("executionAllowlist") != ["BTC-USDT"]:
        raise HistoryCoordinatorError("execution allowlist must remain BTC-USDT-only")
    if ALLOWED_INSTRUMENTS != frozenset({"BTC-USDT"}):
        raise HistoryCoordinatorError("application execution allowlist is not BTC-USDT-only")

    snapshot = _require_mapping(report.get("snapshot"), "frozen universe snapshot")
    stored_snapshot_hash = _require_hash(snapshot.get("sha256"), "snapshot.sha256")
    snapshot_body = {key: value for key, value in snapshot.items() if key != "sha256"}
    expected_snapshot_hash = sha256_hex(canonical_json(snapshot_body))
    if not hmac.compare_digest(stored_snapshot_hash, expected_snapshot_hash):
        raise HistoryCoordinatorError("frozen universe snapshot hash mismatch")
    if snapshot.get("schemaVersion") != UNIVERSE_SCHEMA_VERSION:
        raise HistoryCoordinatorError("frozen universe schema is unsupported")
    _require_hash(snapshot.get("policySha256"), "snapshot.policySha256")
    created_at = _parse_iso(snapshot.get("createdAt"), "snapshot.createdAt")
    members = snapshot.get("members")
    if not isinstance(members, list) or not 1 <= len(members) <= 20:
        raise HistoryCoordinatorError("frozen universe member count is invalid")
    for count_name in ("instrumentRows", "tickerRows"):
        count = snapshot.get(count_name)
        if isinstance(count, bool) or not isinstance(count, int) or count < len(members):
            raise HistoryCoordinatorError(f"snapshot.{count_name} is invalid")
    instruments: list[str] = []
    for member_value in members:
        member = _require_mapping(member_value, "frozen universe member")
        instrument = member.get("instrument")
        if not isinstance(instrument, str) or not _INSTRUMENT_PATTERN.fullmatch(instrument):
            raise HistoryCoordinatorError("frozen universe contains an invalid instrument")
        if member.get("quoteCurrency") != "USDT" or member.get("baseCurrency") != instrument[:-5]:
            raise HistoryCoordinatorError("frozen universe member identity is inconsistent")
        listed_at = _parse_iso(member.get("listedAt"), "member.listedAt")
        ticker_at = _parse_iso(member.get("tickerAt"), "member.tickerAt")
        if listed_at >= created_at or ticker_at > created_at + MAX_TICKER_FUTURE_SKEW:
            raise HistoryCoordinatorError("frozen universe member timestamps are inconsistent")
        bid = _decimal(member.get("bid"), "member.bid")
        ask = _decimal(member.get("ask"), "member.ask")
        _decimal(member.get("last"), "member.last")
        _decimal(member.get("quoteVolume24h"), "member.quoteVolume24h", allow_zero=True)
        _decimal(member.get("spreadBps"), "member.spreadBps", allow_zero=True)
        _decimal(member.get("tickSize"), "member.tickSize")
        _decimal(member.get("lotSize"), "member.lotSize")
        _decimal(member.get("minSize"), "member.minSize")
        if bid > ask:
            raise HistoryCoordinatorError("frozen universe member bid exceeds ask")
        instruments.append(instrument)
    if len(instruments) != len(set(instruments)):
        raise HistoryCoordinatorError("frozen universe contains duplicate instruments")
    return FrozenUniverse(
        path=frozen_path,
        file_sha256=hashlib.sha256(material).hexdigest(),
        report_sha256=stored_report_hash,
        snapshot_sha256=stored_snapshot_hash,
        instruments=tuple(instruments),
    )


def _windows_pid_exists(
    pid: int,
    *,
    kernel32: Any | None = None,
    get_last_error: Callable[[], int] | None = None,
) -> bool:
    import ctypes

    api = kernel32 or ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = api.OpenProcess
    open_process.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    open_process.restype = ctypes.c_void_p
    close_handle = api.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    handle = open_process(0x1000, False, pid)
    if handle:
        close_handle(handle)
        return True
    error = (get_last_error or ctypes.get_last_error)()
    # Only ERROR_INVALID_PARAMETER proves that the PID is absent. Access
    # denied and every unknown failure stay fail-closed as "possibly live".
    return error != 87


def _local_pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_pid_exists(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _replace_with_retry(source: Path, destination: Path) -> None:
    last_error: PermissionError | None = None
    for attempt in range(8):
        try:
            os.replace(source, destination)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(min(0.05 * (2**attempt), 1.0))
    raise HistoryCoordinatorError("atomic file replacement remained unavailable") from last_error


class ExclusiveFileLock:
    """O_EXCL single-writer lock with conservative, auditable stale isolation."""

    def __init__(
        self,
        path: Path,
        *,
        run_id: str,
        now: Callable[[], datetime] | None = None,
        pid_exists: Callable[[int], bool] | None = None,
        stale_after: timedelta = STALE_LOCK_AFTER,
        hostname: str | None = None,
    ):
        if stale_after < timedelta(minutes=1):
            raise HistoryCoordinatorError("stale lock timeout must be at least one minute")
        self.path = path
        self.run_id = run_id
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.pid_exists = pid_exists or _local_pid_exists
        self.stale_after = stale_after
        self.hostname = hostname or socket.gethostname()
        self.nonce = uuid.uuid4().hex
        self.recovery_path = path.with_name(f".{path.name}.recovery")
        self._fd: int | None = None

    def _owner_body(self, *, nonce: str | None = None, run_id: str | None = None) -> bytes:
        return canonical_json(
            {
                "host": self.hostname,
                "nonce": nonce or self.nonce,
                "pid": os.getpid(),
                "runId": run_id or self.run_id,
                "startedAt": _iso(self.now()),
            }
        ).encode("utf-8")

    @staticmethod
    def _read_bounded(path: Path) -> bytes:
        if path.is_symlink():
            raise HistoryCoordinatorBusy("coordinator lock is a symbolic link")
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise HistoryCoordinatorBusy("coordinator lock cannot be inspected") from exc
        if not 1 <= size <= 4_096:
            raise HistoryCoordinatorBusy("coordinator lock body is invalid")
        try:
            return path.read_bytes()
        except OSError as exc:
            raise HistoryCoordinatorBusy("coordinator lock cannot be read") from exc

    @staticmethod
    def _parse_owner(material: bytes) -> dict[str, Any]:
        try:
            owner = json.loads(material)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HistoryCoordinatorBusy("coordinator lock body is invalid") from exc
        if (
            not isinstance(owner, dict)
            or not isinstance(owner.get("host"), str)
            or not isinstance(owner.get("nonce"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", owner["nonce"])
            or isinstance(owner.get("pid"), bool)
            or not isinstance(owner.get("pid"), int)
            or owner["pid"] <= 0
            or not isinstance(owner.get("runId"), str)
        ):
            raise HistoryCoordinatorBusy("coordinator lock owner is invalid")
        _parse_iso(owner.get("startedAt"), "lock.startedAt")
        return owner

    def _is_stale_local_owner(self, material: bytes) -> tuple[bool, dict[str, Any]]:
        owner = self._parse_owner(material)
        started_at = _parse_iso(owner["startedAt"], "lock.startedAt")
        age = _utc(self.now()) - started_at
        same_host = owner["host"].casefold() == self.hostname.casefold()
        stale = same_host and age >= self.stale_after and not self.pid_exists(owner["pid"])
        return stale, owner

    @staticmethod
    def _create_exclusive(path: Path, body: bytes) -> int:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        fd = os.open(path, flags, 0o600)
        try:
            os.write(fd, body)
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            path.unlink(missing_ok=True)
            raise
        return fd

    @staticmethod
    def _unlink_if_nonce(path: Path, nonce: str) -> None:
        material = ExclusiveFileLock._read_bounded(path)
        owner = ExclusiveFileLock._parse_owner(material)
        if not hmac.compare_digest(owner["nonce"], nonce):
            raise HistoryCoordinatorError("coordinator lock ownership changed; it was not removed")
        path.unlink()

    def _claim_recovery(self) -> tuple[int, str]:
        recovery_nonce = uuid.uuid4().hex
        body = self._owner_body(nonce=recovery_nonce, run_id=f"{self.run_id}:recovery")
        try:
            return self._create_exclusive(self.recovery_path, body), recovery_nonce
        except FileExistsError:
            material = self._read_bounded(self.recovery_path)
            stale, owner = self._is_stale_local_owner(material)
            if not stale:
                raise HistoryCoordinatorBusy("another process is inspecting a stale lock")
            if not hmac.compare_digest(material, self._read_bounded(self.recovery_path)):
                raise HistoryCoordinatorBusy("recovery lock changed while inspected")
            quarantined = self.recovery_path.with_name(
                f".{self.path.name}.stale-recovery-{owner['nonce']}.json"
            )
            _replace_with_retry(self.recovery_path, quarantined)
            try:
                return self._create_exclusive(self.recovery_path, body), recovery_nonce
            except FileExistsError as exc:
                raise HistoryCoordinatorBusy("stale-lock recovery raced another process") from exc

    def _quarantine_stale_owner(self) -> bool:
        material = self._read_bounded(self.path)
        stale, owner = self._is_stale_local_owner(material)
        if not stale:
            return False
        recovery_fd, recovery_nonce = self._claim_recovery()
        try:
            current = self._read_bounded(self.path)
            current_stale, current_owner = self._is_stale_local_owner(current)
            if (
                not current_stale
                or not hmac.compare_digest(material, current)
                or not hmac.compare_digest(owner["nonce"], current_owner["nonce"])
            ):
                return False
            quarantined = self.path.with_name(
                f".{self.path.name}.stale-{owner['nonce']}.json"
            )
            _replace_with_retry(self.path, quarantined)
            return True
        finally:
            os.close(recovery_fd)
            self._unlink_if_nonce(self.recovery_path, recovery_nonce)

    def acquire(self) -> None:
        if self._fd is not None:
            raise HistoryCoordinatorError("history coordinator lock is already held")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _attempt in range(3):
            try:
                fd = self._create_exclusive(self.path, self._owner_body())
            except FileExistsError:
                if self._quarantine_stale_owner():
                    continue
                raise HistoryCoordinatorBusy(
                    f"another history coordinator owns {self.path.name}"
                )
            if self.recovery_path.exists():
                os.close(fd)
                self._unlink_if_nonce(self.path, self.nonce)
                raise HistoryCoordinatorBusy("stale-lock recovery is in progress")
            self._fd = fd
            return
        raise HistoryCoordinatorBusy("coordinator lock could not be claimed after stale isolation")

    def release(self) -> None:
        if self._fd is None:
            return
        os.close(self._fd)
        self._fd = None
        self._unlink_if_nonce(self.path, self.nonce)

    def __enter__(self) -> ExclusiveFileLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()


class AtomicProgress:
    def __init__(self, path: Path, initial: dict[str, Any]):
        self.path = path
        self.value = initial

    def write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(self.value, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            last_error: OSError | None = None
            for attempt in range(8):
                try:
                    os.replace(temporary, self.path)
                    last_error = None
                    break
                except PermissionError as exc:
                    # Windows SMB may briefly retain a sharing handle after the
                    # flush. Retrying the same atomic rename never exposes a
                    # partially written destination.
                    last_error = exc
                    time.sleep(min(0.05 * (2**attempt), 1.0))
            if last_error is not None:
                raise HistoryCoordinatorError(
                    "atomic progress replacement remained unavailable"
                ) from last_error
        finally:
            temporary.unlink(missing_ok=True)


class PageBudget:
    def __init__(self, limit: int):
        if isinstance(limit, bool) or not 1 <= limit <= MAX_PAGE_BUDGET:
            raise HistoryCoordinatorError(
                f"page budget must be between 1 and {MAX_PAGE_BUDGET}"
            )
        self.limit = limit
        self.used = 0

    @property
    def remaining(self) -> int:
        return self.limit - self.used

    def consume(self) -> None:
        if self.remaining < 1:
            raise HistoryCoordinatorError("page budget is exhausted")
        self.used += 1


def _run_id(now: datetime) -> str:
    stamp = _utc(now).strftime("%Y%m%dT%H%M%SZ")
    return f"history-{stamp}-{uuid.uuid4().hex[:12]}"


def _status_integrity(status: Mapping[str, Any], instrument: str) -> None:
    if (
        status.get("instrument") != instrument
        or status.get("bar") != "5m"
        or status.get("source") != "okx-public-v5"
    ):
        raise HistoryCoordinatorError("market status escaped its frozen series identity")
    integer_names = ("storedRows", "expectedRows", "missingBars", "unresolvedConflicts")
    if any(
        isinstance(status.get(name), bool)
        or not isinstance(status.get(name), int)
        or int(status[name]) < 0
        for name in integer_names
    ):
        raise HistoryCoordinatorError("market status counters are invalid")
    if status["unresolvedConflicts"]:
        raise HistoryCoordinatorError(f"{instrument} has unresolved immutable-data conflicts")
    if status["storedRows"]:
        first = status.get("firstOpenTsMs")
        last = status.get("lastOpenTsMs")
        if (
            isinstance(first, bool)
            or not isinstance(first, int)
            or isinstance(last, bool)
            or not isinstance(last, int)
            or first <= 0
            or last < first
            or first % FIVE_MINUTES_MS
            or last % FIVE_MINUTES_MS
            or status["expectedRows"] - status["storedRows"] != status["missingBars"]
        ):
            raise HistoryCoordinatorError("market status grid accounting is inconsistent")


class MultiAssetHistoryCoordinator:
    def __init__(
        self,
        *,
        store: MultiAssetMarketStore,
        client: PublicHistoryClient,
        universe: FrozenUniverse,
        progress_path: Path,
        page_budget: int,
        run_id: str,
        now: Callable[[], datetime] | None = None,
        feature_contract_sha256: str | None = None,
    ) -> None:
        self.store = store
        self.client = client
        self.universe = universe
        self.budget = PageBudget(page_budget)
        self.run_id = run_id
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.feature_contract_sha256 = feature_contract_sha256
        started_at = _utc(self.now())
        self.progress = AtomicProgress(
            progress_path,
            {
                "database": str(store.path),
                "instruments": {},
                "pageBudget": page_budget,
                "pagesConsumed": 0,
                "runId": run_id,
                "schemaVersion": PROGRESS_SCHEMA,
                "startedAt": _iso(started_at),
                "state": "running",
                "universeFileSha256": universe.file_sha256,
                "universeReportSha256": universe.report_sha256,
                "universeSnapshotSha256": universe.snapshot_sha256,
                "updatedAt": _iso(started_at),
            },
        )

    def _status(self, instrument: str) -> dict[str, Any]:
        status = self.store.status(instrument)
        _status_integrity(status, instrument)
        return status

    def _write_progress(
        self,
        instrument: str,
        stage: str,
        *,
        cursor: int | None = None,
        page: Sequence[Sequence[Any]] | None = None,
        ingest: Mapping[str, Any] | None = None,
        note: str | None = None,
        status: Mapping[str, Any] | None = None,
    ) -> None:
        instruments = self.progress.value["instruments"]
        entry = dict(instruments.get(instrument, {}))
        entry.update({"stage": stage})
        if status is not None:
            _status_integrity(status, instrument)
            entry.update({
                "backfillComplete": status["backfillComplete"],
                "firstOpenTsMs": status["firstOpenTsMs"],
                "lastOpenTsMs": status["lastOpenTsMs"],
                "missingBars": status["missingBars"],
                "storedRows": status["storedRows"],
                "unresolvedConflicts": status["unresolvedConflicts"],
            })
        if page is not None:
            entry["pagesConsumed"] = int(entry.get("pagesConsumed", 0)) + 1
            entry["lastPageRows"] = len(page)
            entry["lastTerminalCursor"] = cursor
        if ingest is not None:
            for source_name, progress_name in (
                ("inserted", "rowsInserted"),
                ("duplicates", "rowsDuplicate"),
                ("unconfirmed", "rowsUnconfirmedSkipped"),
            ):
                value = ingest.get(source_name, 0)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise HistoryCoordinatorError("market ingest counters are invalid")
                entry[progress_name] = int(entry.get(progress_name, 0)) + value
        if note is not None:
            entry["note"] = note
        instruments[instrument] = entry
        self.progress.value["pagesConsumed"] = self.budget.used
        self.progress.value["updatedAt"] = _iso(self.now())
        self.progress.write()

    def _ingest_page(
        self, instrument: str, page: Sequence[Sequence[Any]]
    ) -> dict[str, Any]:
        confirmed: list[Sequence[Any]] = []
        skipped = 0
        for row in page:
            if not isinstance(row, (list, tuple)) or len(row) != 9:
                raise HistoryCoordinatorError("public history candle shape is invalid")
            flag = str(row[8]).strip()
            if flag == "1":
                confirmed.append(row)
            elif flag == "0":
                skipped += 1
            else:
                raise HistoryCoordinatorError("public history confirm flag is invalid")
        if confirmed:
            result = self.store.ingest(
                confirmed, instrument=instrument, observed_at=_utc(self.now())
            )
        else:
            result = {
                "conflicts": 0,
                "duplicates": 0,
                "inserted": 0,
                "unconfirmed": 0,
            }
        result = dict(result)
        result["unconfirmed"] = int(result.get("unconfirmed", 0)) + skipped
        conflicts = result.get("conflicts")
        if isinstance(conflicts, bool) or not isinstance(conflicts, int):
            raise HistoryCoordinatorError("market ingest conflict count is invalid")
        if conflicts:
            raise HistoryCoordinatorError(
                f"{instrument} produced an immutable-data conflict"
            )
        return result

    async def _first_page(
        self,
        instrument: str,
        *,
        after: int | None,
        page_limit: int,
        stage: str,
    ) -> tuple[list[list[Any]], int | None] | None:
        if self.budget.remaining < 1:
            return None
        iterator = self.client.iter_history_candle_pages(
            instrument, bar="5m", after=after, page_limit=page_limit
        )
        try:
            try:
                page, cursor = await anext(iterator)
            except StopAsyncIteration as exc:
                raise HistoryCoordinatorError("public history iterator ended without evidence") from exc
        finally:
            await iterator.aclose()
        self.budget.consume()
        ingest: Mapping[str, Any] | None = None
        if page:
            ingest = self._ingest_page(instrument, page)
        self._write_progress(
            instrument, stage, cursor=cursor, page=page, ingest=ingest
        )
        return page, cursor

    async def _head_refresh(self, instrument: str) -> None:
        result = await self._first_page(
            instrument, after=None, page_limit=300, stage="headRefresh"
        )
        if result is not None:
            self._write_progress(
                instrument, "headRefreshed", status=self._status(instrument)
            )

    def _record_origin(
        self,
        instrument: str,
        *,
        requested_after: int,
        terminal_cursor: int,
        page_limit: int,
        anchor_open_ts_ms: int,
        anchor_payload_sha256: str,
    ) -> None:
        result: Mapping[str, Any] | None = None
        note: str
        try:
            result = self.store.record_origin_probe(
                OriginProbe(
                    run_id=self.run_id,
                    instrument=instrument,
                    requested_after=requested_after,
                    terminal_cursor=terminal_cursor,
                    page_limit=page_limit,
                    empty_probe_count=3,
                    anchor_open_ts_ms=anchor_open_ts_ms,
                    anchor_payload_sha256=anchor_payload_sha256,
                    observed_at=_utc(self.now()),
                )
            )
        except MultiAssetMarketError as exc:
            if "at least 60 seconds" not in str(exc) and "different baseline" not in str(exc):
                raise
            note = "confirmation requires a later distinct run at least 60 seconds after baseline"
            stage = "originPending"
        else:
            note = str(result["role"])
            stage = "originConfirmed" if result["backfillComplete"] else "originBaseline"
        self._write_progress(
            instrument,
            stage,
            note=note,
            status=self._status(instrument),
        )

    async def _origin_anchor(
        self, instrument: str, boundary_open_ts_ms: int
    ) -> tuple[int, str] | None:
        """Require a same-run non-empty overlap before accepting empty probes."""

        result = await self._first_page(
            instrument,
            after=boundary_open_ts_ms + FIVE_MINUTES_MS,
            page_limit=100,
            stage="originPositiveAnchor",
        )
        if result is None:
            return None
        page, _cursor = result
        if not page:
            self._write_progress(
                instrument,
                "originAnchorMissing",
                note="all-empty history responses cannot prove an exchange origin",
            )
            return None
        anchor_row: Sequence[Any] | None = None
        for row in page:
            if self._timestamp(row) == boundary_open_ts_ms and str(row[8]).strip() == "1":
                anchor_row = row
                break
        if anchor_row is None:
            self._write_progress(
                instrument,
                "originAnchorMismatch",
                note="positive overlap did not contain the current oldest confirmed candle",
                status=self._status(instrument),
            )
            return None
        status = self._status(instrument)
        if status["firstOpenTsMs"] != boundary_open_ts_ms:
            self._write_progress(
                instrument,
                "olderHistoryAppeared",
                note="positive overlap revealed earlier confirmed history",
                status=status,
            )
            return None
        normalized = [str(value).strip() for value in anchor_row]
        return boundary_open_ts_ms, sha256_hex(canonical_json(normalized))

    async def _oldest_backfill(self, instrument: str) -> None:
        status = self._status(instrument)
        if not status["storedRows"] or status["backfillComplete"] or self.budget.remaining < 1:
            return
        oldest = int(status["firstOpenTsMs"])
        if status["originProbeCount"]:
            requested_after = oldest - 1
            result = await self._first_page(
                instrument,
                after=requested_after,
                page_limit=100,
                stage="originConfirmationProbe",
            )
            if result is None:
                return
            page, terminal_cursor = result
            if not page:
                if terminal_cursor != requested_after:
                    raise HistoryCoordinatorError("origin confirmation cursor changed unexpectedly")
                anchor = await self._origin_anchor(instrument, oldest)
                if anchor is not None:
                    self._record_origin(
                        instrument,
                        requested_after=requested_after,
                        terminal_cursor=requested_after,
                        page_limit=100,
                        anchor_open_ts_ms=anchor[0],
                        anchor_payload_sha256=anchor[1],
                    )
                return
            # Older history appeared after the baseline. Its insertion invalidates
            # the old probes; continue from the database's new actual MIN.
            status = self._status(instrument)
            self._write_progress(
                instrument, "olderHistoryAppeared", status=status
            )
            oldest = int(status["firstOpenTsMs"])

        iterator = self.client.iter_history_candle_pages(
            instrument, bar="5m", after=oldest, page_limit=300
        )
        reached_terminal = False
        try:
            while self.budget.remaining:
                try:
                    page, terminal_cursor = await anext(iterator)
                except StopAsyncIteration as exc:
                    raise HistoryCoordinatorError("public history iterator ended without evidence") from exc
                self.budget.consume()
                ingest: Mapping[str, Any] | None = None
                if page:
                    ingest = self._ingest_page(instrument, page)
                self._write_progress(
                    instrument,
                    "oldestBackfill" if page else "originBaselineProbe",
                    cursor=terminal_cursor,
                    page=page,
                    ingest=ingest,
                )
                if not page:
                    boundary = int(terminal_cursor)
                    anchor = await self._origin_anchor(instrument, boundary)
                    if anchor is not None:
                        self._record_origin(
                            instrument,
                            requested_after=boundary,
                            terminal_cursor=boundary,
                            page_limit=300,
                            anchor_open_ts_ms=anchor[0],
                            anchor_payload_sha256=anchor[1],
                        )
                    reached_terminal = True
                    break
        finally:
            await iterator.aclose()
        if not reached_terminal:
            self._write_progress(
                instrument, "oldestBackfillPaused", status=self._status(instrument)
            )

    @staticmethod
    def _timestamp(row: Sequence[Any]) -> int:
        if not row:
            raise HistoryCoordinatorError("gap repair received an empty candle row")
        value = str(row[0]).strip()
        if not value.isdigit():
            raise HistoryCoordinatorError("gap repair received an invalid candle timestamp")
        return int(value)

    async def _repair_gaps(self, instrument: str) -> None:
        while self.budget.remaining:
            status = self._status(instrument)
            gaps = status["gapRanges"]
            if not gaps:
                return
            before = int(status["missingBars"])
            made_progress = False
            for gap in gaps:
                if self.budget.remaining < 1:
                    break
                first = int(gap["firstOpenTsMs"])
                last = int(gap["lastOpenTsMs"])
                iterator = self.client.iter_history_candle_pages(
                    instrument,
                    bar="5m",
                    after=last + FIVE_MINUTES_MS,
                    page_limit=300,
                )
                try:
                    while self.budget.remaining:
                        try:
                            page, cursor = await anext(iterator)
                        except StopAsyncIteration as exc:
                            raise HistoryCoordinatorError(
                                "gap history iterator ended without evidence"
                            ) from exc
                        self.budget.consume()
                        ingest: Mapping[str, Any] | None = None
                        if page:
                            ingest = self._ingest_page(instrument, page)
                        self._write_progress(
                            instrument,
                            "gapRepair",
                            cursor=cursor,
                            page=page,
                            ingest=ingest,
                        )
                        selected = [
                            row for row in page if first <= self._timestamp(row) <= last
                        ]
                        made_progress = made_progress or bool(selected)
                        if not page or cursor is None or cursor <= first:
                            break
                finally:
                    await iterator.aclose()
            repaired_status = self._status(instrument)
            self._write_progress(
                instrument, "gapRepairChecked", status=repaired_status
            )
            after = int(repaired_status["missingBars"])
            if after >= before or not made_progress:
                self._write_progress(
                    instrument,
                    "gapBlocked",
                    note="public history did not reduce the missing-bar count; no forward fill was used",
                    status=repaired_status,
                )
                return

    def _feature_hash(self) -> str:
        if self.feature_contract_sha256 is not None:
            return _require_hash(self.feature_contract_sha256, "feature contract hash")
        # Delayed import binds snapshots to the cohort builder's canonical raw
        # candle contract without making this transport coordinator own it.
        from okx_demo_lab.ml.multi_asset_cohort import (  # type: ignore[import-not-found]
            RAW_CANDLE_FEATURE_CONTRACT_SHA256,
        )

        return _require_hash(
            RAW_CANDLE_FEATURE_CONTRACT_SHA256, "raw candle feature contract hash"
        )

    def _snapshot_if_ready(self, instrument: str) -> None:
        status = self._status(instrument)
        if not status["readyForSnapshot"]:
            self._write_progress(instrument, "incomplete", status=status)
            return
        snapshot = self.store.create_snapshot(
            instrument,
            feature_contract_sha256=self._feature_hash(),
            now=_utc(self.now()),
        )
        self._write_progress(
            instrument,
            "snapshotReady",
            note=f"{snapshot.snapshot_id}:{snapshot.content_sha256}",
            status=status,
        )

    async def run(self) -> dict[str, Any]:
        self.universe.assert_unchanged()
        self.progress.write()
        for instrument in self.universe.instruments:
            self.universe.assert_unchanged()
            if self.budget.remaining < 1:
                self._write_progress(
                    instrument,
                    "pageBudgetExhausted",
                    status=self._status(instrument),
                )
                break
            await self._head_refresh(instrument)
            await self._oldest_backfill(instrument)
            await self._repair_gaps(instrument)
            self._snapshot_if_ready(instrument)
        self.universe.assert_unchanged()
        statuses = {instrument: self._status(instrument) for instrument in self.universe.instruments}
        entries = self.progress.value["instruments"]
        complete = all(
            status["readyForSnapshot"]
            and entries.get(instrument, {}).get("stage") == "snapshotReady"
            for instrument, status in statuses.items()
        )
        self.progress.value["state"] = "complete" if complete else "partial"
        self.progress.value["pagesConsumed"] = self.budget.used
        self.progress.value["updatedAt"] = _iso(self.now())
        self.progress.write()
        return {
            "instruments": statuses,
            "pageBudget": self.budget.limit,
            "pagesConsumed": self.budget.used,
            "progress": str(self.progress.path),
            "runId": self.run_id,
            "state": self.progress.value["state"],
        }


def _public_client(universe: FrozenUniverse) -> OkxPublicMarketClient:
    return OkxPublicMarketClient(history_instruments=universe.instruments)


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    project_root = PROJECT_ROOT
    universe_path = _runtime_path(args.universe, project_root=project_root, must_exist=True)
    database_path = _runtime_path(args.database, project_root=project_root)
    progress_path = _runtime_path(args.progress, project_root=project_root)
    lock_path = _runtime_path(args.lock, project_root=project_root)
    universe = load_frozen_universe(universe_path, project_root=project_root)
    current = datetime.now(timezone.utc)
    run_id = _run_id(current)
    with ExclusiveFileLock(lock_path, run_id=run_id):
        store = MultiAssetMarketStore(database_path)
        if args.status_only:
            statuses = {instrument: store.status(instrument) for instrument in universe.instruments}
            for instrument, status in statuses.items():
                _status_integrity(status, instrument)
            return {
                "instruments": statuses,
                "runId": run_id,
                "state": "statusOnly",
                "universeReportSha256": universe.report_sha256,
            }
        client = _public_client(universe)
        try:
            coordinator = MultiAssetHistoryCoordinator(
                store=store,
                client=client,
                universe=universe,
                progress_path=progress_path,
                page_budget=args.page_budget,
                run_id=run_id,
            )
            return await coordinator.run()
        finally:
            await client.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Serial, credential-free OKX 5m history coordinator for a frozen research universe. "
            "It never calls private, account, or order APIs."
        )
    )
    parser.add_argument("--universe", type=Path, default=DEFAULT_UNIVERSE_PATH)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--progress", type=Path, default=DEFAULT_PROGRESS_PATH)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK_PATH)
    parser.add_argument("--page-budget", type=int, default=600)
    parser.add_argument("--status-only", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        result = asyncio.run(_run(args))
    except (HistoryCoordinatorError, HistoryCoordinatorBusy, MultiAssetMarketError, PublicMarketError) as exc:
        print(
            json.dumps(
                {"errorType": type(exc).__name__, "message": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
