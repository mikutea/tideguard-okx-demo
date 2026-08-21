from __future__ import annotations

"""Collect a small, public-only news weak signal for offline research.

The collector intentionally exposes no credential, exchange-private, order, or
LLM/tool surface.  Publisher text is treated as untrusted data and is passed
only to the frozen VADER lexicon baseline.
"""

import argparse
import ctypes
import email.utils
import json
import os
import re
import sqlite3
import socket
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence
from urllib.parse import urlsplit, urlunsplit

import httpx

from okx_demo_lab.ml.alternative_data import (
    AlternativeDataError,
    PublicTextEvent,
    SourcePolicy,
)
from okx_demo_lab.ml.signal_store import SignalStore
from okx_demo_lab.ml.strategy import canonical_json, sha256_hex

try:
    from research.vader_adapter import VaderSentimentAdapter
except ModuleNotFoundError as exc:
    if exc.name != "research":
        raise
    # `python research/collect_public_signals.py` puts research/, rather than
    # the repository root, on sys.path.  Keep that supported without writing a
    # path shim or environment file outside the project.
    from vader_adapter import VaderSentimentAdapter


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESEARCH_DATA_ROOT = PROJECT_ROOT / ".research-data"
DEFAULT_DATABASE = RESEARCH_DATA_ROOT / "public-signals.sqlite3"
GDELT_DOC_ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_ATTRIBUTION_URL = "https://www.gdeltproject.org/"
GDELT_DATA_POLICY_URL = "https://www.gdeltproject.org/data.html"
SOURCE_ID = "gdelt-2"
COLLECTOR_SCHEMA_VERSION = "moheng.gdelt-public-signals.v1"
HISTORICAL_BACKFILL_AFTER = timedelta(minutes=30)
LOCK_STALE_AFTER = timedelta(minutes=10)
_LOCK_METADATA_LIMIT = 4_096

# This is an intentionally frozen local use-policy snapshot.  It records the
# exact boundary implemented here; it is not a claim that article copyright is
# granted by GDELT.  Article bodies are never requested or stored.
GDELT_POLICY_SNAPSHOT: Mapping[str, object] = {
    "allowedUse": "local metadata/title/url weak-signal research with attribution",
    "articleBodyStorage": False,
    "attribution": "The GDELT Project",
    "attributionUrl": GDELT_ATTRIBUTION_URL,
    "capturedAt": "2026-08-21T00:00:00Z",
    "dataPolicyUrl": GDELT_DATA_POLICY_URL,
    "publishedAtField": (
        "GDELT seendate; source observation time, not asserted publisher publication"
    ),
    "redistribution": False,
    "sourceId": SOURCE_ID,
}
GDELT_POLICY_TERMS_SHA256 = sha256_hex(canonical_json(GDELT_POLICY_SNAPSHOT))
EXPECTED_GDELT_POLICY_TERMS_SHA256 = (
    "c281054fa0c72ad65552a5ae26f84c0def8a6dd54cd31265032abcabf39f6b51"
)
if GDELT_POLICY_TERMS_SHA256 != EXPECTED_GDELT_POLICY_TERMS_SHA256:
    raise RuntimeError("frozen GDELT use-policy snapshot hash drifted")
GDELT_SOURCE_POLICY = SourcePolicy(
    source_id=SOURCE_ID,
    license_id=f"gdelt-policy-snapshot-sha256-{GDELT_POLICY_TERMS_SHA256}",
    headline_storage_allowed=True,
    full_text_storage_allowed=False,
    redistribution_allowed=False,
    reliability_weight=0.55,
)

# The production trading allowlist is deliberately not imported.  These base
# assets are a frozen research entity map and confer no execution permission.
RESEARCH_ASSETS = ("BTC", "ETH", "XRP", "SOL", "DOGE", "PEPE")
_ASSET_PATTERNS: Mapping[str, tuple[re.Pattern[str], ...]] = {
    "BTC": (
        re.compile(r"(?<![\w$])bitcoin(?!\s+cash\b)", re.IGNORECASE),
        re.compile(r"(?<!\w)\$?btc(?!\w)", re.IGNORECASE),
    ),
    "ETH": (
        re.compile(r"(?<!\w)ethereum(?!\w)", re.IGNORECASE),
        re.compile(r"(?<!\w)\$eth(?!\w)", re.IGNORECASE),
    ),
    "XRP": (
        re.compile(r"(?<!\w)\$?xrp(?!\w)", re.IGNORECASE),
        re.compile(r"(?<!\w)ripple\s+labs?(?!\w)", re.IGNORECASE),
    ),
    "SOL": (
        re.compile(r"(?<!\w)solana(?!\w)", re.IGNORECASE),
        re.compile(r"(?<!\w)\$sol(?!\w)", re.IGNORECASE),
        re.compile(r"(?<!\w)sol\s+(?:coin|token)(?!\w)", re.IGNORECASE),
    ),
    "DOGE": (
        re.compile(r"(?<!\w)dogecoin(?!\w)", re.IGNORECASE),
        re.compile(r"(?<!\w)\$doge(?!\w)", re.IGNORECASE),
        re.compile(r"(?<!\w)doge\s+(?:coin|token)(?!\w)", re.IGNORECASE),
    ),
    "PEPE": (
        re.compile(r"(?<!\w)\$pepe(?!\w)", re.IGNORECASE),
        re.compile(r"(?<!\w)pepe\s+(?:coin|token)(?!\w)", re.IGNORECASE),
    ),
}
GDELT_QUERY = (
    '"bitcoin" OR "BTC" OR "ethereum" OR "XRP" OR "solana" '
    'OR "dogecoin" OR "pepe coin"'
)


class PublicSignalCollectorError(RuntimeError):
    pass


class CollectorAlreadyRunning(PublicSignalCollectorError):
    pass


class HeadlineScorer(Protocol):
    model_sha256: str

    def score(
        self,
        event: PublicTextEvent,
        *,
        scored_at: datetime,
        asset_relevance: float,
    ) -> object: ...


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_gdelt_timestamp(value: object) -> datetime:
    """Parse the documented compact GDELT time and conservative ISO variants."""

    if not isinstance(value, str) or not value.strip():
        raise PublicSignalCollectorError("GDELT article timestamp is missing")
    raw = value.strip()
    for pattern in ("%Y%m%dT%H%M%SZ", "%Y%m%d%H%M%S"):
        try:
            return datetime.strptime(raw, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PublicSignalCollectorError("GDELT article timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PublicSignalCollectorError("GDELT article timestamp has no timezone")
    return parsed.astimezone(timezone.utc)


def map_headline_to_asset(headline: object) -> tuple[str, float] | None:
    """Return exactly one unambiguous frozen entity match, otherwise reject."""

    if not isinstance(headline, str):
        return None
    matches = [
        asset
        for asset, patterns in _ASSET_PATTERNS.items()
        if any(pattern.search(headline) for pattern in patterns)
    ]
    if len(matches) != 1:
        return None
    # A weak relevance value is deliberate: an entity mention is not evidence
    # that the article is primarily about the asset.
    return matches[0], 0.75


def _normalized_url(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise PublicSignalCollectorError("GDELT article URL is missing")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise PublicSignalCollectorError("GDELT article URL is invalid")
    # Fragments are client-side and must not create a second source identity.
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, "")
    )


def _language(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized in {"en", "eng", "english"}:
        return "en"
    # VADER is an English lexicon; other languages are not silently scored.
    return None


def article_to_event(
    article: Mapping[str, object], *, first_seen_at: datetime, fetched_at: datetime
) -> tuple[PublicTextEvent, float] | None:
    """Validate GDELT metadata and convert it to untrusted event text."""

    headline_value = article.get("title")
    if not isinstance(headline_value, str):
        return None
    mapped = map_headline_to_asset(headline_value)
    language = _language(article.get("language"))
    if mapped is None or language is None:
        return None
    try:
        published_at = parse_gdelt_timestamp(article.get("seendate"))
        url = _normalized_url(article.get("url"))
    except PublicSignalCollectorError:
        return None
    if (
        first_seen_at.tzinfo is None
        or first_seen_at.utcoffset() is None
        or fetched_at.tzinfo is None
        or fetched_at.utcoffset() is None
    ):
        raise PublicSignalCollectorError("collector timestamps must be timezone-aware")
    first_seen = first_seen_at.astimezone(timezone.utc)
    fetched = fetched_at.astimezone(timezone.utc)
    historical = first_seen - published_at > HISTORICAL_BACKFILL_AFTER
    event = PublicTextEvent(
        source_id=SOURCE_ID,
        source_event_id=f"url-sha256-{sha256_hex(url)}",
        asset=mapped[0],
        published_at=published_at,
        first_seen_at=first_seen,
        fetched_at=fetched,
        headline=headline_value,
        url=url,
        language=language,
        revision=0,
        historical_backfill=historical,
    )
    return event, mapped[1]


def _retry_after_seconds(value: str | None, now: datetime) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (parsed.astimezone(timezone.utc) - now).total_seconds())


class GdeltDocClient:
    """Credential-free GDELT DOC metadata client with bounded retries."""

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = _utc_now,
        max_attempts: int = 5,
    ) -> None:
        if max_attempts < 1 or max_attempts > 8:
            raise ValueError("max_attempts must be between 1 and 8")
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(20.0),
            follow_redirects=False,
            headers={
                "Accept": "application/json",
                "User-Agent": "MOHENG-public-research/0.4 (GDELT attributed metadata)",
            },
        )
        self._sleep = sleep
        self._clock = clock
        self._max_attempts = max_attempts

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> GdeltDocClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def fetch_articles(
        self, *, max_records: int = 250, timespan: str = "15min"
    ) -> list[Mapping[str, object]]:
        if not 1 <= max_records <= 250:
            raise ValueError("max_records must be between 1 and 250")
        if not re.fullmatch(r"[1-9][0-9]{0,2}(?:min|h|d)", timespan):
            raise ValueError("timespan is invalid")
        params = {
            "format": "json",
            "maxrecords": str(max_records),
            "mode": "ArtList",
            "query": GDELT_QUERY,
            "sort": "DateDesc",
            "timespan": timespan,
        }
        response: httpx.Response | None = None
        for attempt in range(self._max_attempts):
            try:
                response = self._client.get(GDELT_DOC_ENDPOINT, params=params)
            except (httpx.TimeoutException, httpx.RequestError) as exc:
                if attempt + 1 == self._max_attempts:
                    raise PublicSignalCollectorError(
                        "GDELT request failed after bounded retries"
                    ) from exc
                self._sleep(min(30.0, 2.0**attempt))
                continue
            if response.status_code == 200:
                break
            if response.status_code != 429 and response.status_code < 500:
                raise PublicSignalCollectorError(
                    f"GDELT request returned HTTP {response.status_code}"
                )
            if attempt + 1 == self._max_attempts:
                raise PublicSignalCollectorError(
                    f"GDELT request returned HTTP {response.status_code} after retries"
                )
            retry_after = _retry_after_seconds(
                response.headers.get("Retry-After"), self._clock()
            )
            delay = max(2.0**attempt, retry_after or 0.0)
            self._sleep(min(60.0, delay))
        if response is None:
            raise PublicSignalCollectorError("GDELT request did not produce a response")
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise PublicSignalCollectorError("GDELT response is not valid JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("articles"), list):
            raise PublicSignalCollectorError("GDELT response schema is invalid")
        return [item for item in payload["articles"] if isinstance(item, dict)]


@dataclass(frozen=True)
class CollectionReport:
    fetched: int
    mapped: int
    accepted_events: int
    accepted_scores: int
    duplicates: int
    conflicts: int
    rejected: int
    historical_backfills: int
    prospective_events: int

    def to_dict(self) -> dict[str, object]:
        return {
            "acceptedEvents": self.accepted_events,
            "acceptedScores": self.accepted_scores,
            "collectorSchemaVersion": COLLECTOR_SCHEMA_VERSION,
            "conflicts": self.conflicts,
            "duplicates": self.duplicates,
            "fetched": self.fetched,
            "historicalBackfills": self.historical_backfills,
            "mapped": self.mapped,
            "prospectiveEvents": self.prospective_events,
            "rejected": self.rejected,
            "sourcePolicySha256": GDELT_SOURCE_POLICY.sha256,
            "termsSnapshotSha256": GDELT_POLICY_TERMS_SHA256,
        }


class PublicSignalCollector:
    def __init__(
        self,
        store: SignalStore,
        client: GdeltDocClient,
        scorer: HeadlineScorer,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.store = store
        self.client = client
        self.scorer = scorer
        self.clock = clock

    def collect_once(
        self, *, max_records: int = 250, timespan: str = "15min"
    ) -> CollectionReport:
        with single_writer_lock(self.store.path):
            return self._collect_once_unlocked(
                max_records=max_records, timespan=timespan
            )

    def _collect_once_unlocked(
        self, *, max_records: int, timespan: str
    ) -> CollectionReport:
        articles = self.client.fetch_articles(
            max_records=max_records, timespan=timespan
        )
        fetched_at = self.clock().astimezone(timezone.utc)
        accepted_events = accepted_scores = duplicates = conflicts = 0
        rejected = historical = prospective = mapped_count = 0
        for article in articles:
            try:
                converted = article_to_event(
                    article, first_seen_at=fetched_at, fetched_at=fetched_at
                )
            except (AlternativeDataError, PublicSignalCollectorError):
                rejected += 1
                continue
            if converted is None:
                rejected += 1
                continue
            mapped_count += 1
            event, relevance = converted
            canonical_provenance = _existing_event_provenance(self.store, event)
            if canonical_provenance is not None:
                # The first local observation is immutable.  A later polling
                # run must never relabel that original row as historical merely
                # because wall-clock time has since advanced beyond 30 minutes.
                event = replace(
                    event,
                    first_seen_at=canonical_provenance[0],
                    historical_backfill=canonical_provenance[1],
                )
            accepted_at = self.clock().astimezone(timezone.utc)
            event_result = self.store.append_event(
                event, GDELT_SOURCE_POLICY, observed_at=accepted_at
            )
            status = event_result["status"]
            if status == "duplicate":
                duplicates += 1
                continue
            if status == "conflict":
                conflicts += 1
                continue
            accepted_events += 1
            historical += int(event.historical_backfill)
            prospective += int(not event.historical_backfill)
            scored_at = self.clock().astimezone(timezone.utc)
            score = self.scorer.score(
                event, scored_at=scored_at, asset_relevance=relevance
            )
            score_result = self.store.append_score(score, observed_at=scored_at)  # type: ignore[arg-type]
            if score_result["status"] == "accepted":
                accepted_scores += 1
            elif score_result["status"] == "duplicate":
                duplicates += 1
            else:
                conflicts += 1
        return CollectionReport(
            fetched=len(articles),
            mapped=mapped_count,
            accepted_events=accepted_events,
            accepted_scores=accepted_scores,
            duplicates=duplicates,
            conflicts=conflicts,
            rejected=rejected,
            historical_backfills=historical,
            prospective_events=prospective,
        )


def _existing_event_provenance(
    store: SignalStore, event: PublicTextEvent
) -> tuple[datetime, bool] | None:
    """Read only immutable provenance needed to classify a repeated poll."""

    try:
        with sqlite3.connect(store.path) as db:
            row = db.execute(
                """
                SELECT first_seen_at, historical_backfill
                FROM signal_events
                WHERE source_id = ? AND source_event_id = ? AND revision = ?
                """,
                (event.source_id, event.source_event_id, event.revision),
            ).fetchone()
    except sqlite3.Error as exc:
        raise PublicSignalCollectorError(
            "signal store provenance lookup failed"
        ) from exc
    if row is None:
        return None
    try:
        first_seen = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
    except ValueError as exc:
        raise PublicSignalCollectorError(
            "stored signal first_seen_at is invalid"
        ) from exc
    if first_seen.tzinfo is None or first_seen.utcoffset() is None:
        raise PublicSignalCollectorError(
            "stored signal first_seen_at has no timezone"
        )
    return first_seen.astimezone(timezone.utc), bool(row[1])


def _lock_metadata(path: Path) -> dict[str, object] | None:
    try:
        if path.stat().st_size > _LOCK_METADATA_LIMIT:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    return value


def _pid_exists_on_this_host(pid: object) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return True
    if sys.platform == "win32":
        process_query_limited_information = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
        open_process.restype = ctypes.c_void_p
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (ctypes.c_void_p,)
        close_handle.restype = ctypes.c_int
        handle = open_process(process_query_limited_information, False, pid)
        if handle:
            close_handle(handle)
            return True
        # ERROR_INVALID_PARAMETER is the documented nonexistent-PID result.
        # Access denied and unknown failures are treated as live, fail closed.
        return ctypes.get_last_error() != 87
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _is_reclaimable_lock(
    metadata: Mapping[str, object] | None,
    *,
    now: datetime,
    stale_after: timedelta,
) -> bool:
    if metadata is None:
        return False
    if str(metadata.get("hostname", "")).casefold() != socket.gethostname().casefold():
        return False
    started_value = metadata.get("startedAt")
    if not isinstance(started_value, str):
        return False
    try:
        started_at = datetime.fromisoformat(started_value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if started_at.tzinfo is None or started_at.utcoffset() is None:
        return False
    age = now.astimezone(timezone.utc) - started_at.astimezone(timezone.utc)
    return age >= stale_after and not _pid_exists_on_this_host(metadata.get("pid"))


@contextmanager
def _acquisition_gate(path: Path, *, blocking: bool) -> Iterator[None]:
    """Serialize create/reclaim/release; the OS drops this lock after a crash."""

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
                msvcrt.locking(handle.fileno(), mode, 1)
            else:
                import fcntl

                mode = fcntl.LOCK_EX
                if not blocking:
                    mode |= fcntl.LOCK_NB
                fcntl.flock(handle.fileno(), mode)
        except OSError as exc:
            raise CollectorAlreadyRunning(
                f"collector lock acquisition is already in progress: {path}"
            ) from exc
        try:
            yield
        finally:
            handle.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _remove_lock_if_owned(path: Path, owner_nonce: str) -> bool:
    metadata = _lock_metadata(path)
    if metadata is None or metadata.get("nonce") != owner_nonce:
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


@contextmanager
def single_writer_lock(
    database: Path,
    *,
    clock: Callable[[], datetime] = _utc_now,
    stale_after: timedelta = LOCK_STALE_AFTER,
) -> Iterator[None]:
    """Hold an owner lease and reclaim only provably dead, old local locks."""

    if stale_after < timedelta(minutes=1):
        raise ValueError("stale_after must be at least one minute")
    lock_path = Path(f"{database}.writer.lock")
    gate_path = Path(f"{lock_path}.gate")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    owner_nonce = uuid.uuid4().hex
    metadata = {
        "hostname": socket.gethostname(),
        "nonce": owner_nonce,
        "pid": os.getpid(),
        "startedAt": clock().astimezone(timezone.utc).isoformat(),
    }
    descriptor: int | None = None
    owner_handle = None
    created_owner_lock = False
    quarantine: Path | None = None
    with _acquisition_gate(gate_path, blocking=False):
        try:
            try:
                descriptor = os.open(
                    lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR
                )
                created_owner_lock = True
            except FileExistsError as exc:
                existing = _lock_metadata(lock_path)
                if not _is_reclaimable_lock(
                    existing, now=clock(), stale_after=stale_after
                ):
                    raise CollectorAlreadyRunning(
                        f"collector writer lock is active or unverifiable: {lock_path}"
                    ) from exc
                quarantine = Path(f"{lock_path}.stale-{uuid.uuid4().hex}")
                os.replace(lock_path, quarantine)
                descriptor = os.open(
                    lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR
                )
                created_owner_lock = True
            owner_handle = os.fdopen(descriptor, "r+", encoding="utf-8")
            owner_handle.write(json.dumps(metadata, sort_keys=True))
            owner_handle.flush()
            os.fsync(owner_handle.fileno())
        except BaseException:
            if owner_handle is not None:
                owner_handle.close()
            elif descriptor is not None:
                os.close(descriptor)
            if created_owner_lock:
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
            raise
        finally:
            if quarantine is not None:
                try:
                    quarantine.unlink()
                except FileNotFoundError:
                    pass
    try:
        # Keep the owner descriptor open for the entire collection run.
        yield
    finally:
        with _acquisition_gate(gate_path, blocking=True):
            if owner_handle is not None:
                owner_handle.close()
            _remove_lock_if_owned(lock_path, owner_nonce)


def _within_project(path: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(RESEARCH_DATA_ROOT.resolve())
    except ValueError as exc:
        raise PublicSignalCollectorError(
            "signal database must stay inside the project .research-data directory"
        ) from exc
    return resolved


def _status_payload(store: SignalStore, database: Path) -> dict[str, object]:
    return {
        "attribution": "The GDELT Project",
        "attributionUrl": GDELT_ATTRIBUTION_URL,
        "collectorSchemaVersion": COLLECTOR_SCHEMA_VERSION,
        "database": str(database),
        "executionCapabilities": [],
        "fullTextStored": False,
        "researchAssets": list(RESEARCH_ASSETS),
        "sourcePolicy": GDELT_SOURCE_POLICY.to_dict(),
        "sourcePolicySha256": GDELT_SOURCE_POLICY.sha256,
        "store": store.status(),
        "termsSnapshot": dict(GDELT_POLICY_SNAPSHOT),
        "termsSnapshotSha256": GDELT_POLICY_TERMS_SHA256,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Collect public GDELT title/URL metadata as an offline weak signal."
    )
    parser.add_argument("command", choices=("status", "once"))
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--max-records", type=int, default=250)
    parser.add_argument("--timespan", default="15min")
    args = parser.parse_args(argv)
    try:
        database = _within_project(args.database)
        if args.command == "status":
            payload = _status_payload(SignalStore(database), database)
        else:
            store = SignalStore(database)
            scorer = VaderSentimentAdapter()
            with GdeltDocClient() as client:
                report = PublicSignalCollector(store, client, scorer).collect_once(
                    max_records=args.max_records, timespan=args.timespan
                )
            # A collection tick stays O(batch). Full append-only integrity and
            # snapshot replay are intentionally reserved for the explicit
            # `status` command instead of becoming O(total-history) every poll.
            payload = {
                **report.to_dict(),
                "database": str(database),
                "fullIntegrityAudit": "not-run-use-status-command",
            }
    except (PublicSignalCollectorError, CollectorAlreadyRunning, RuntimeError, ValueError) as exc:
        print(
            json.dumps(
                {"errorType": type(exc).__name__, "message": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
