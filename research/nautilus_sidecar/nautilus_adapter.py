from __future__ import annotations

import base64
import csv
import hashlib
import importlib.metadata as metadata
import io
import json
import os
import sys
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence
from zipfile import ZipFile

from .protocol import EXPECTED_NAUTILUS_VERSION, ProtocolError, canonical_json


_FORBIDDEN_RUNTIME_MODULE_PREFIXES = (
    "nautilus_trader.adapters",
    "nautilus_trader.live",
    "nautilus_trader.system",
)
_INSTALLER_METADATA_FILES = frozenset(
    {
        "INSTALLER",
        "REQUESTED",
        "direct_url.json",
        "uv_cache.json",
    }
)


class RuntimeIntegrityError(RuntimeError):
    """The isolated optional runtime no longer matches its immutable evidence."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _record_sha256(path: Path) -> str:
    raw = bytes.fromhex(_sha256_file(path))
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _record_path(value: str, *, name: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
    ):
        raise RuntimeIntegrityError(f"{name} contains an unsafe path")
    return path


def _record_rows(text: str, *, name: str) -> dict[str, tuple[str, str]]:
    rows: dict[str, tuple[str, str]] = {}
    try:
        parsed = csv.reader(io.StringIO(text), strict=True)
        for row in parsed:
            if len(row) != 3:
                raise RuntimeIntegrityError(f"{name} row shape is invalid")
            relative, digest, size = row
            _record_path(relative, name=name)
            if relative in rows:
                raise RuntimeIntegrityError(f"{name} contains a duplicate path")
            rows[relative] = (digest, size)
    except (csv.Error, UnicodeError) as exc:
        raise RuntimeIntegrityError(f"{name} is invalid") from exc
    return rows


def verify_canonical_setup_state(
    state_path: str | Path,
    state_hash_path: str | Path,
    *,
    runtime_root: str | Path,
    expected_wheel_name: str,
    expected_wheel_sha256: str,
    expected_wheel_size: int,
) -> dict[str, Any]:
    """Verify setup-v3 canonical bytes, sidecar hash, and exact runtime policy."""

    state_file = Path(state_path).resolve()
    hash_file = Path(state_hash_path).resolve()
    runtime = Path(runtime_root).resolve()
    try:
        data = state_file.read_bytes()
        if data.startswith(b"\xef\xbb\xbf"):
            raise RuntimeIntegrityError("setup state must not contain a UTF-8 BOM")

        def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            value: dict[str, Any] = {}
            for key, item in pairs:
                if key in value:
                    raise RuntimeIntegrityError("setup state contains duplicate keys")
                value[key] = item
            return value

        state = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                RuntimeIntegrityError(f"setup state contains {value}")
            ),
        )
    except RuntimeIntegrityError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeIntegrityError("setup state is not valid UTF-8 JSON") from exc
    if not isinstance(state, dict):
        raise RuntimeIntegrityError("setup state root must be an object")
    canonical = (canonical_json(state) + "\n").encode("utf-8")
    if data != canonical:
        raise RuntimeIntegrityError("setup state is not canonical JSON")
    digest = hashlib.sha256(data).hexdigest()
    try:
        expected_hash_record = f"{digest}  {state_file.name}\n".encode("ascii")
        if hash_file.read_bytes() != expected_hash_record:
            raise RuntimeIntegrityError("setup state SHA256 record does not match")
    except OSError as exc:
        raise RuntimeIntegrityError("setup state SHA256 record is unavailable") from exc

    expected_managed_python = (
        runtime
        / "python"
        / "cpython-3.12.13-windows-x86_64-none"
        / "python.exe"
    ).resolve()
    package = state.get("package")
    policy = state.get("policy")
    python = state.get("python")
    expected_package = {
        "distribution": "nautilus-trader",
        "version": EXPECTED_NAUTILUS_VERSION,
        "wheel": expected_wheel_name,
        "wheelSha256": expected_wheel_sha256,
        "wheelSizeBytes": expected_wheel_size,
    }
    expected_policy = {
        "credentialsLoaded": False,
        "liveExecutionAllowed": False,
        "managedPythonArchiveHashLocked": False,
        "networkAdaptersImported": False,
        "networkUseAuthorized": False,
        "orderCalls": 0,
        "osNetworkIsolationEnforced": False,
        "privateApiCalls": 0,
        "purpose": "offline-research-only",
    }
    try:
        prepared = datetime.fromisoformat(
            str(state.get("preparedAtUtc", "")).replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise RuntimeIntegrityError("setup state timestamp is invalid") from exc
    if (
        set(state)
        != {
            "managedPython",
            "package",
            "policy",
            "preparedAtUtc",
            "python",
            "runtimeRoot",
            "schema",
            "status",
            "uv",
        }
        or state.get("schema") != "moheng.nautilus-setup-state.v3"
        or state.get("status") != "ready"
        or os.path.normcase(str(Path(str(state.get("runtimeRoot"))).resolve()))
        != os.path.normcase(str(runtime))
        or os.path.normcase(str(Path(str(state.get("managedPython"))).resolve()))
        != os.path.normcase(str(expected_managed_python))
        or not expected_managed_python.is_file()
        or not isinstance(state.get("uv"), str)
        or not str(state["uv"]).startswith("uv ")
        or prepared.tzinfo is None
        or not isinstance(package, dict)
        or canonical_json(package) != canonical_json(expected_package)
        or not isinstance(python, dict)
        or python.get("version") != "3.12.13"
        or python.get("implementation") != "CPython"
        or str(python.get("architecture", "")).lower() not in {"amd64", "x86_64"}
        or type(python.get("pointerBits")) is not int
        or python.get("pointerBits") != 64
        or not isinstance(policy, dict)
        or canonical_json(policy) != canonical_json(expected_policy)
    ):
        raise RuntimeIntegrityError("setup state does not match the offline PoC")
    return {
        "schema": state["schema"],
        "setupStateSha256": digest,
        "uv": state["uv"],
    }


def verify_installed_wheel_files(
    wheel_path: str | Path,
    site_packages_root: str | Path,
    *,
    expected_wheel_sha256: str,
    expected_wheel_size: int,
) -> dict[str, Any]:
    """Verify installed Nautilus files against the pinned wheel and RECORD."""

    supplied_wheel = Path(wheel_path).absolute()
    wheel = supplied_wheel.resolve()
    site_packages = Path(site_packages_root).resolve()
    if (
        not wheel.is_file()
        or wheel.stat().st_size != expected_wheel_size
        or _sha256_file(wheel) != expected_wheel_sha256
        or not site_packages.is_dir()
    ):
        raise RuntimeIntegrityError("pinned wheel or site-packages root is invalid")
    dist_info_name = f"nautilus_trader-{EXPECTED_NAUTILUS_VERSION}.dist-info"
    wheel_record_name = f"{dist_info_name}/RECORD"
    try:
        with ZipFile(wheel) as archive:
            member_names: set[str] = set()
            for member in archive.infolist():
                if member.is_dir():
                    continue
                relative = str(_record_path(member.filename, name="wheel"))
                if relative in member_names:
                    raise RuntimeIntegrityError("wheel contains a duplicate path")
                member_names.add(relative)
            if wheel_record_name not in member_names:
                raise RuntimeIntegrityError("wheel RECORD is missing")
            wheel_record = archive.read(wheel_record_name).decode("utf-8")
    except RuntimeIntegrityError:
        raise
    except (OSError, UnicodeError, KeyError) as exc:
        raise RuntimeIntegrityError("pinned wheel cannot be inspected") from exc
    wheel_rows = _record_rows(wheel_record, name="wheel RECORD")
    if set(wheel_rows) != member_names:
        raise RuntimeIntegrityError("wheel members and RECORD do not match")

    installed_record_path = site_packages / dist_info_name / "RECORD"
    try:
        installed_record = installed_record_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeIntegrityError("installed RECORD is unavailable") from exc
    installed_rows = _record_rows(installed_record, name="installed RECORD")
    allowed_installer_paths = {
        f"{dist_info_name}/{name}" for name in _INSTALLER_METADATA_FILES
    }
    allowed_paths = member_names | allowed_installer_paths
    if (
        not set(installed_rows).issubset(allowed_paths)
        or not allowed_installer_paths.issubset(installed_rows)
    ):
        raise RuntimeIntegrityError("installed RECORD contains unexpected files")

    digest_cache: dict[Path, str] = {}

    def checked_digest(path: Path) -> str:
        if path not in digest_cache:
            digest_cache[path] = _record_sha256(path)
        return digest_cache[path]

    for relative, (wheel_digest, wheel_size) in wheel_rows.items():
        target = site_packages.joinpath(*PurePosixPath(relative).parts)
        if relative == wheel_record_name:
            if wheel_digest or wheel_size:
                raise RuntimeIntegrityError("wheel RECORD self-entry must be unhashed")
            continue
        if (
            not target.is_file()
            or target.is_symlink()
            or not wheel_digest.startswith("sha256=")
            or not wheel_size.isdigit()
            or target.stat().st_size != int(wheel_size)
            or checked_digest(target) != wheel_digest.removeprefix("sha256=")
        ):
            raise RuntimeIntegrityError(
                f"installed file does not match pinned wheel: {relative}"
            )
        if installed_rows.get(relative) != (wheel_digest, wheel_size):
            raise RuntimeIntegrityError(
                f"installed RECORD does not match pinned wheel: {relative}"
            )

    actual_paths: set[str] = set()
    for root in (site_packages / "nautilus_trader", site_packages / dist_info_name):
        if not root.is_dir():
            raise RuntimeIntegrityError("installed Nautilus package root is missing")
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix == ".pyc" or "__pycache__" in path.parts:
                continue
            actual_paths.add(path.relative_to(site_packages).as_posix())
    if actual_paths != set(installed_rows):
        raise RuntimeIntegrityError("installed files and RECORD do not match")

    for relative, (record_digest, record_size) in installed_rows.items():
        target = site_packages.joinpath(*PurePosixPath(relative).parts)
        if relative == wheel_record_name:
            if (
                record_digest
                or record_size
                or target != installed_record_path
                or target.is_symlink()
            ):
                raise RuntimeIntegrityError("installed RECORD self-entry is invalid")
            continue
        if (
            not target.is_file()
            or target.is_symlink()
            or not record_digest.startswith("sha256=")
            or not record_size.isdigit()
            or target.stat().st_size != int(record_size)
            or checked_digest(target) != record_digest.removeprefix("sha256=")
        ):
            raise RuntimeIntegrityError(
                f"installed RECORD hash does not match: {relative}"
            )

    direct_url_path = site_packages / dist_info_name / "direct_url.json"
    try:
        direct_url = json.loads(direct_url_path.read_text(encoding="utf-8"))["url"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeIntegrityError("installed direct_url metadata is invalid") from exc
    if direct_url not in {supplied_wheel.as_uri(), wheel.as_uri()}:
        raise RuntimeIntegrityError("installed package does not reference the pinned wheel")
    return {
        "installedFileCount": len(actual_paths),
        "installedRecordVerified": True,
        "wheelSha256": expected_wheel_sha256,
    }


def verify_isolated_runtime_integrity(
    *,
    state_path: str | Path,
    state_hash_path: str | Path,
    wheel_path: str | Path,
    runtime_root: str | Path,
    python_executable: str | Path,
    expected_wheel_name: str,
    expected_wheel_sha256: str,
    expected_wheel_size: int,
) -> dict[str, Any]:
    """Verify the complete setup-state, wheel, and installed-file chain."""

    runtime = Path(runtime_root).resolve()
    expected_python = (runtime / "venv" / "Scripts" / "python.exe").resolve()
    current_python = Path(sys.executable).resolve()
    supplied_python = Path(python_executable).resolve()
    if (
        os.path.normcase(str(current_python)) != os.path.normcase(str(expected_python))
        or os.path.normcase(str(supplied_python)) != os.path.normcase(str(expected_python))
    ):
        raise RuntimeIntegrityError("integrity check is not running in the isolated venv")
    state = verify_canonical_setup_state(
        state_path,
        state_hash_path,
        runtime_root=runtime,
        expected_wheel_name=expected_wheel_name,
        expected_wheel_sha256=expected_wheel_sha256,
        expected_wheel_size=expected_wheel_size,
    )
    installed = verify_installed_wheel_files(
        wheel_path,
        runtime / "venv" / "Lib" / "site-packages",
        expected_wheel_sha256=expected_wheel_sha256,
        expected_wheel_size=expected_wheel_size,
    )
    return {**state, **installed, "status": "verified"}


def _package_imported() -> bool:
    return any(
        name == "nautilus_trader" or name.startswith("nautilus_trader.")
        for name in sys.modules
    )


def forbidden_runtime_modules_loaded() -> tuple[str, ...]:
    """Return loaded Nautilus adapter/live/system modules in stable order."""

    return tuple(
        sorted(
            name
            for name in sys.modules
            if any(
                name == prefix or name.startswith(f"{prefix}.")
                for prefix in _FORBIDDEN_RUNTIME_MODULE_PREFIXES
            )
        )
    )


def require_offline_module_boundary() -> None:
    """Fail closed if the host process already loaded a live-capable facade."""

    if forbidden_runtime_modules_loaded():
        raise ProtocolError(
            "FORBIDDEN_NAUTILUS_RUNTIME_LOADED",
            "Nautilus adapter, live, or system modules are already loaded",
        )


def dependency_status() -> dict[str, Any]:
    """Inspect distribution metadata without importing ``nautilus_trader``."""

    forbidden_modules = forbidden_runtime_modules_loaded()
    try:
        version = metadata.version("nautilus-trader")
    except metadata.PackageNotFoundError:
        return {
            "available": False,
            "distributionMetadataAvailable": False,
            "expectedVersion": EXPECTED_NAUTILUS_VERSION,
            "forbiddenRuntimeModulesLoaded": list(forbidden_modules),
            "importErrorType": "PackageNotFoundError",
            "materializationAllowed": False,
            "packageImportAttempted": False,
            "packageImported": _package_imported(),
            "status": "protocol_only_dependency_unavailable",
            "version": None,
        }
    matches = version == EXPECTED_NAUTILUS_VERSION
    offline_safe = not forbidden_modules
    return {
        "available": True,
        "distributionMetadataAvailable": True,
        "expectedVersion": EXPECTED_NAUTILUS_VERSION,
        "forbiddenRuntimeModulesLoaded": list(forbidden_modules),
        "importErrorType": None,
        "materializationAllowed": matches and offline_safe,
        "packageImportAttempted": False,
        "packageImported": _package_imported(),
        "status": (
            "blocked_forbidden_runtime_loaded"
            if not offline_safe
            else "metadata_ready"
            if matches
            else "blocked_version_mismatch"
        ),
        "version": version,
    }


def validate_local_bar_materialization(
    normalized_bars: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Materialize local Nautilus Bar values; never create an engine or order."""

    require_offline_module_boundary()
    status = dependency_status()
    if not status["materializationAllowed"]:
        return {**status, "materializedBars": 0}
    # Load the optional package only for an explicitly requested local value-
    # object operation. Protocol self-checks remain import-free.
    try:  # pragma: no cover - exercised only in the isolated optional runtime
        from nautilus_trader.model import Bar
        from nautilus_trader.model import BarType
        from nautilus_trader.model import Price
        from nautilus_trader.model import Quantity
    except (ImportError, OSError) as import_error:
        return {
            **status,
            "importErrorType": type(import_error).__name__,
            "materializationAllowed": False,
            "materializedBars": 0,
            "packageImportAttempted": True,
            "packageImported": _package_imported(),
            "status": "protocol_only_dependency_import_failed",
        }
    bar_type = BarType.from_str("BTC-USDT.OKX-5-MINUTE-LAST-EXTERNAL")
    materialized = []
    for raw in normalized_bars:
        materialized.append(
            Bar(
                bar_type=bar_type,
                open=Price.from_str(raw["open"]),
                high=Price.from_str(raw["high"]),
                low=Price.from_str(raw["low"]),
                close=Price.from_str(raw["close"]),
                volume=Quantity.from_str(raw["volume"]),
                ts_event=raw["tsEventNs"],
                ts_init=raw["tsInitNs"],
            )
        )
    if any(item.ts_event != item.ts_init for item in materialized):
        raise RuntimeError("offline close-timestamp bars must have ts_event == ts_init")
    return {
        **status,
        "materializedBars": len(materialized),
        "packageImportAttempted": True,
        "packageImported": _package_imported(),
        "status": "ready",
    }
