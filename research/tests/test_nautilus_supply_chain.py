from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from research.nautilus_sidecar.nautilus_adapter import (
    RuntimeIntegrityError,
    verify_canonical_setup_state,
    verify_installed_wheel_files,
)
from research.nautilus_sidecar.protocol import canonical_json


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOCK_PATH = PROJECT_ROOT / "research" / "nautilus-lock.json"
SETUP_PATH = PROJECT_ROOT / "scripts" / "setup-nautilus-poc.ps1"
RUN_PATH = PROJECT_ROOT / "scripts" / "run-nautilus-poc.ps1"
HISTORICAL_RUN_PATH = PROJECT_ROOT / "scripts" / "run-historical-replay.ps1"
CHECK_PATH = PROJECT_ROOT / "scripts" / "check.ps1"
EXPECTED_VERSION = "2.0.0rc3"
EXPECTED_WHEEL = "nautilus_trader-2.0.0rc3-cp312-cp312-win_amd64.whl"
EXPECTED_SHA256 = (
    "8a90b01ccf66d78946c565bca08b7758bc7f312caf1ded1c2c2c710013a7c092"
)


def _record_digest(data: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode(
        "ascii"
    ).rstrip("=")


def _write_fake_wheel_install(
    root: Path,
) -> tuple[Path, Path, Path, str, int]:
    site_packages = root / "site-packages"
    dist_info = f"nautilus_trader-{EXPECTED_VERSION}.dist-info"
    package_file = site_packages / "nautilus_trader" / "__init__.py"
    wheel = root / EXPECTED_WHEEL
    wheel_members = {
        "nautilus_trader/__init__.py": b"VERSION = '2.0.0rc3'\n",
        f"{dist_info}/METADATA": b"Name: nautilus-trader\nVersion: 2.0.0rc3\n",
        f"{dist_info}/WHEEL": b"Wheel-Version: 1.0\nTag: cp312-cp312-win_amd64\n",
    }
    wheel_record_name = f"{dist_info}/RECORD"
    wheel_record = "".join(
        f"{name},sha256={_record_digest(data)},{len(data)}\n"
        for name, data in wheel_members.items()
    ) + f"{wheel_record_name},,\n"
    with ZipFile(wheel, "w", compression=ZIP_DEFLATED) as archive:
        for name, data in wheel_members.items():
            archive.writestr(name, data)
        archive.writestr(wheel_record_name, wheel_record.encode("utf-8"))

    installed = dict(wheel_members)
    installed.update(
        {
            f"{dist_info}/INSTALLER": b"uv\n",
            f"{dist_info}/REQUESTED": b"",
            f"{dist_info}/direct_url.json": json.dumps(
                {"archive_info": {}, "url": wheel.resolve().as_uri()},
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8"),
            f"{dist_info}/uv_cache.json": b"{}",
        }
    )
    for name, data in installed.items():
        target = site_packages.joinpath(*name.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    installed_record = "".join(
        f"{name},sha256={_record_digest(data)},{len(data)}\n"
        for name, data in installed.items()
    ) + f"{wheel_record_name},,\n"
    record_path = site_packages.joinpath(*wheel_record_name.split("/"))
    record_path.write_text(installed_record, encoding="utf-8")
    return (
        wheel,
        site_packages,
        package_file,
        hashlib.sha256(wheel.read_bytes()).hexdigest(),
        wheel.stat().st_size,
    )


def _write_setup_state(
    runtime: Path,
    state_path: Path,
    hash_path: Path,
    *,
    network_use_authorized: bool = False,
) -> None:
    managed_python = (
        runtime
        / "python"
        / "cpython-3.12.13-windows-x86_64-none"
        / "python.exe"
    )
    managed_python.parent.mkdir(parents=True, exist_ok=True)
    managed_python.touch()
    state = {
        "managedPython": str(managed_python.resolve()),
        "package": {
            "distribution": "nautilus-trader",
            "version": EXPECTED_VERSION,
            "wheel": EXPECTED_WHEEL,
            "wheelSha256": EXPECTED_SHA256,
            "wheelSizeBytes": 61_222_443,
        },
        "policy": {
            "credentialsLoaded": False,
            "liveExecutionAllowed": False,
            "managedPythonArchiveHashLocked": False,
            "networkAdaptersImported": False,
            "networkUseAuthorized": network_use_authorized,
            "orderCalls": 0,
            "osNetworkIsolationEnforced": False,
            "privateApiCalls": 0,
            "purpose": "offline-research-only",
        },
        "preparedAtUtc": "2026-08-21T22:12:40.4101679Z",
        "python": {
            "architecture": "AMD64",
            "implementation": "CPython",
            "pointerBits": 64,
            "version": "3.12.13",
        },
        "runtimeRoot": str(runtime.resolve()),
        "schema": "moheng.nautilus-setup-state.v3",
        "status": "ready",
        "uv": "uv 0.11.7 (test build)",
    }
    data = (canonical_json(state) + "\n").encode("utf-8")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    hash_path.write_bytes(f"{digest}  {state_path.name}\n".encode("ascii"))


def _immutable_writer(source: str) -> str:
    match = re.search(r"(?s)\$writer = @'\r?\n(.*?)\r?\n'@", source)
    assert match is not None
    return match.group(1)


def test_nautilus_lock_is_exactly_scoped_to_offline_windows_research() -> None:
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))

    assert lock["schema_version"] == 1
    assert lock["package"]["distribution"] == "nautilus-trader"
    assert lock["package"]["version"] == EXPECTED_VERSION
    assert lock["package"]["license"] == "LGPL-3.0-only"
    assert lock["artifact"] == {
        "abi_tag": "cp312",
        "filename": EXPECTED_WHEEL,
        "platform_tag": "win_amd64",
        "python_tag": "cp312",
        "sha256": EXPECTED_SHA256,
        "size_bytes": 61_222_443,
        "url": (
            "https://github.com/nautechsystems/nautilus_trader/releases/"
            f"download/v{EXPECTED_VERSION}/{EXPECTED_WHEEL}"
        ),
    }
    assert lock["runtime"]["project_relative_root"] == (
        ".research-data/nautilus-poc"
    )
    assert lock["runtime"] == {
        "architecture": "x86_64",
        "pointer_bits": 64,
        "project_relative_root": ".research-data/nautilus-poc",
        "python_implementation": "CPython",
        "python_request": "cpython-3.12.13-windows-x86_64",
        "python_version": "3.12.13",
    }
    assert lock["policy"] == {
        "credentials_allowed": False,
        "live_execution_allowed": False,
        "managed_python_archive_hash_locked": False,
        "network_adapters_imported": False,
        "os_network_isolation_enforced": False,
        "orders_allowed": False,
        "private_api_allowed": False,
        "purpose": "offline-research-only",
        "run_network_use_authorized": False,
        "setup_network_scope": (
            "uv-managed-exact-python-version-and-pinned-nautilus-wheel-only"
        ),
    }


def test_setup_and_run_scripts_share_the_same_pin_and_fail_closed_fields() -> None:
    setup = SETUP_PATH.read_text(encoding="utf-8")
    run = RUN_PATH.read_text(encoding="utf-8")

    for source in (setup, run):
        assert EXPECTED_VERSION in source
        assert EXPECTED_WHEEL in source
        assert EXPECTED_SHA256 in source
        assert ".research-data\\nautilus-poc" in source
        assert "OKX_API_KEY = $null" in source
        assert "OKX_API_SECRET = $null" in source
        assert "OKX_API_PASSPHRASE = $null" in source
        assert "OKX_SECRET_KEY = $null" in source
        assert "OKX_PASSPHRASE = $null" in source
        assert "cpython-3.12.13-windows-x86_64" in source
        assert "sys.version_info[:3] == (3, 12, 13)" in source
        assert "os.O_CREAT | os.O_EXCL | os.O_WRONLY" in source
        assert "os.rename(temp_target, target)" in source
        assert "os.rename(temp_hash, hash_target)" in source
        assert "os.replace(" not in source
    assert "--offline --no-deps" in setup
    assert "Invoke-WebRequest" in setup
    assert "Invoke-WebRequest" not in run
    assert "$existingState.managedPython -ne $ManagedPythonPath" in setup
    assert "$existingState.python.version -ne $currentPython.version" in setup
    assert "$existingState.python.implementation -ne $currentPython.implementation" in setup
    assert "$existingState.python.architecture -ne $currentPython.architecture" in setup
    assert "$existingState.python.pointerBits -ne $currentPython.pointerBits" in setup
    assert "$existingState.uv -ne $UvVersion" in setup
    assert "managedPythonArchiveHashLocked = $false" in setup
    assert "NATIVE_BAR_FILL_PARITY_NOT_VALIDATED" in run
    assert "verify_isolated_runtime_integrity" in run
    assert "setup-v3.json" in run
    assert "installedRecordVerified" in run
    assert _immutable_writer(setup) == _immutable_writer(run)


@pytest.mark.skipif(os.name != "nt", reason="writer requires Windows rename semantics")
def test_immutable_evidence_writer_fails_closed_under_concurrency(
    tmp_path: Path,
) -> None:
    writer = _immutable_writer(RUN_PATH.read_text(encoding="utf-8"))
    target = tmp_path / "immutable.json"
    payload = {"schema": "test.immutable.v1", "value": 7}
    encoded = base64.b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    arguments = [sys.executable, "-c", writer, str(target), encoded]

    first = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    second = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    first_stdout, first_stderr = first.communicate(timeout=30)
    second_stdout, second_stderr = second.communicate(timeout=30)

    assert sorted((first.returncode, second.returncode)) == [0, 1], (
        first_stderr,
        second_stderr,
    )
    successful = first_stdout if first.returncode == 0 else second_stdout
    digest = successful.decode("ascii").strip()
    expected_data = (canonical_json(payload) + "\n").encode("utf-8")
    assert target.read_bytes() == expected_data
    assert digest == hashlib.sha256(expected_data).hexdigest()
    assert (tmp_path / "immutable.json.sha256").read_bytes() == (
        f"{digest}  immutable.json\n".encode("ascii")
    )
    assert list(tmp_path.glob("*.tmp")) == []
    assert list(tmp_path.glob("*.lock")) == []


def test_installed_distribution_is_verified_against_wheel_and_record(
    tmp_path: Path,
) -> None:
    wheel, site_packages, _, wheel_sha256, wheel_size = _write_fake_wheel_install(
        tmp_path
    )

    result = verify_installed_wheel_files(
        wheel,
        site_packages,
        expected_wheel_sha256=wheel_sha256,
        expected_wheel_size=wheel_size,
    )

    assert result["installedRecordVerified"] is True
    assert result["installedFileCount"] == 8


def test_installed_distribution_tampering_fails_closed(tmp_path: Path) -> None:
    wheel, site_packages, package_file, wheel_sha256, wheel_size = (
        _write_fake_wheel_install(tmp_path)
    )
    package_file.write_text("TAMPERED = True\n", encoding="utf-8")

    with pytest.raises(RuntimeIntegrityError, match="pinned wheel"):
        verify_installed_wheel_files(
            wheel,
            site_packages,
            expected_wheel_sha256=wheel_sha256,
            expected_wheel_size=wheel_size,
        )


def test_setup_state_policy_tampering_fails_even_when_rehashed(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "nautilus-poc"
    state_path = runtime / "state" / "setup-v3.json"
    hash_path = runtime / "state" / "setup-v3.json.sha256"
    _write_setup_state(runtime, state_path, hash_path)
    assert verify_canonical_setup_state(
        state_path,
        hash_path,
        runtime_root=runtime,
        expected_wheel_name=EXPECTED_WHEEL,
        expected_wheel_sha256=EXPECTED_SHA256,
        expected_wheel_size=61_222_443,
    )["schema"] == "moheng.nautilus-setup-state.v3"

    _write_setup_state(
        runtime,
        state_path,
        hash_path,
        network_use_authorized=True,
    )
    with pytest.raises(RuntimeIntegrityError, match="offline PoC"):
        verify_canonical_setup_state(
            state_path,
            hash_path,
            runtime_root=runtime,
            expected_wheel_name=EXPECTED_WHEEL,
            expected_wheel_sha256=EXPECTED_SHA256,
            expected_wheel_size=61_222_443,
        )


def test_research_runner_and_secret_scan_cover_all_okx_aliases() -> None:
    runner = HISTORICAL_RUN_PATH.read_text(encoding="utf-8")
    check = CHECK_PATH.read_text(encoding="utf-8")
    for variable in (
        "OKX_API_KEY",
        "OKX_API_SECRET",
        "OKX_API_PASSPHRASE",
        "OKX_SECRET_KEY",
        "OKX_PASSPHRASE",
    ):
        assert f"{variable} = $null" in runner
        assert variable in check
    assert "TEMP = Join-Path $DataRoot \"runtime-tmp\"" in runner
    assert "TMP = Join-Path $DataRoot \"runtime-tmp\"" in runner
    assert "$SavedEnvironment" in runner
    assert "SetEnvironmentVariable" in runner
    assert "'run-historical-replay.ps1'" in check
