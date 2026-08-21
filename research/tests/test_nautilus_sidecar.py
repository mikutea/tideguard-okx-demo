from __future__ import annotations

import ast
import copy
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

import research.nautilus_sidecar.nautilus_adapter as nautilus_adapter
import research.nautilus_sidecar.service as sidecar_service
from research.nautilus_sidecar import (
    BAR_CATALOG_OPERATION,
    REQUEST_SCHEMA_VERSION,
    ProtocolError,
    build_self_check_request,
    canonical_json,
    decode_canonical_request,
    handle_request,
    seal_request,
    sha256_hex,
)
from research.nautilus_sidecar.protocol import (
    FIVE_MINUTES_NS,
    SAFETY_CONTRACT,
    validate_sealed_response,
)
from research.nautilus_sidecar.service import build_error_response


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SIDECAR_ROOT = PROJECT_ROOT / "research" / "nautilus_sidecar"


def _bar(ts_open_ns: int, *, confirmed: bool = True) -> dict[str, object]:
    return {
        "close": "60100.5",
        "confirmed": confirmed,
        "high": "60200",
        "low": "59900",
        "open": "60000",
        "tsOpenNs": ts_open_ns,
        "volume": "12.50",
    }


def _catalog_request() -> dict[str, object]:
    start = 1_800_000_000_000_000_000
    return seal_request(
        {
            "operation": BAR_CATALOG_OPERATION,
            "payload": {
                "barSpecification": "5-MINUTE-LAST-EXTERNAL",
                "bars": [_bar(start), _bar(start + FIVE_MINUTES_NS)],
                "instrumentId": "BTC-USDT.OKX",
                "source": {
                    "contentSha256": "a" * 64,
                    "kind": "okx_public_frozen_snapshot",
                },
            },
            "safety": dict(SAFETY_CONTRACT),
            "schemaVersion": REQUEST_SCHEMA_VERSION,
        }
    )


def test_self_check_is_dependency_free_canonical_and_deterministic() -> None:
    request = build_self_check_request()
    first = handle_request(request)
    second = handle_request(copy.deepcopy(request))

    assert canonical_json(first) == canonical_json(second)
    assert first["result"]["offlineProtocolReady"] is True
    assert first["result"]["nautilusRequiredForSelfCheck"] is False
    assert first["semanticBoundary"]["canonicalExecutionVersion"] == "V6"
    assert (
        first["semanticBoundary"]["nativeNautilusExecutionParityValidated"]
        is False
    )
    assert (
        first["semanticBoundary"]["reasonCode"]
        == "NATIVE_BAR_FILL_PARITY_NOT_VALIDATED"
    )
    assert first["semanticBoundary"]["canonicalContract"] == {
        "decisionAt": "confirmed_bar_close_next_bar_open_boundary",
        "entryAt": "next_bar_open_same_timestamp",
        "exitAt": "entry_plus_12_bars_open",
        "holdingBars": 12,
        "labelHorizonBars": 12,
        "protocolVersion": "moheng.execution.corrected-next-open-boundary.v1",
        "sameSourceBarFillAllowed": False,
        "sameTimestampFillAllowed": True,
    }
    assert first["safety"]["credentialsAllowed"] is False
    assert first["safety"]["networkAdaptersImported"] is False
    assert first["safety"]["networkUseAuthorized"] is False
    assert first["safety"]["orderCapability"] is False
    assert first["safety"]["osNetworkIsolationEnforced"] is False
    assert first["nautilus"]["packageImportAttempted"] is False
    assert first["nautilus"]["packageImported"] is False
    assert first["summarySha256"] == sha256_hex(canonical_json(first["summary"]))
    validate_sealed_response(first)


def test_semantic_contract_is_deep_copied_between_responses() -> None:
    first = handle_request(build_self_check_request())
    first["semanticBoundary"]["canonicalContract"]["holdingBars"] = 999

    second = handle_request(build_self_check_request())

    assert second["semanticBoundary"]["canonicalContract"]["holdingBars"] == 12
    validate_sealed_response(second)


@pytest.mark.parametrize(
    "module_name",
    [
        "nautilus_trader.adapters",
        "nautilus_trader.adapters.okx",
        "nautilus_trader.live",
        "nautilus_trader.live.node",
        "nautilus_trader.system",
        "nautilus_trader.system.kernel",
    ],
)
def test_preloaded_live_capable_nautilus_module_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
) -> None:
    monkeypatch.setitem(sys.modules, module_name, types.ModuleType(module_name))

    with pytest.raises(ProtocolError) as error:
        handle_request(build_self_check_request())

    assert error.value.code == "FORBIDDEN_NAUTILUS_RUNTIME_LOADED"
    rejected = build_error_response(error.value)
    assert "safety" not in rejected
    assert "networkAdaptersImported" not in canonical_json(rejected)
    validate_sealed_response(rejected)


def test_runtime_module_loaded_during_materialization_is_not_sealed_as_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unsafe_materialization(_normalized_bars):
        module_name = "nautilus_trader.adapters.okx"
        monkeypatch.setitem(sys.modules, module_name, types.ModuleType(module_name))
        return {"status": "ready"}

    monkeypatch.setattr(
        sidecar_service,
        "validate_local_bar_materialization",
        unsafe_materialization,
    )

    with pytest.raises(ProtocolError) as error:
        handle_request(_catalog_request())

    assert error.value.code == "FORBIDDEN_NAUTILUS_RUNTIME_LOADED"
    rejected = build_error_response(error.value)
    assert "safety" not in rejected
    assert "networkAdaptersImported" not in canonical_json(rejected)
    validate_sealed_response(rejected)


def test_research_tests_remove_every_supported_okx_environment_alias() -> None:
    assert all(
        os.environ.get(name) is None
        for name in (
            "OKX_API_KEY",
            "OKX_API_SECRET",
            "OKX_API_PASSPHRASE",
            "OKX_SECRET_KEY",
            "OKX_PASSPHRASE",
        )
    )


def test_catalog_summary_is_content_addressed_and_executes_no_simulation() -> None:
    request = _catalog_request()
    response = handle_request(request)

    assert response["result"]["catalog"]["barCount"] == 2
    assert response["result"]["simulationExecuted"] is False
    assert response["result"]["tradesProduced"] == 0
    assert response["summary"]["nativeCanonicalExecutionParityValidated"] is False
    assert response["summarySha256"] == sha256_hex(canonical_json(response["summary"]))
    assert len(response["result"]["catalog"]["barDataSha256"]) == 64
    validate_sealed_response(response)


def test_local_model_materialization_ready_branch_without_optional_wheel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeValue:
        @classmethod
        def from_str(cls, value: str) -> str:
            return value

    class FakeBarType(FakeValue):
        pass

    class FakeBar:
        def __init__(self, **values) -> None:
            self.ts_event = values["ts_event"]
            self.ts_init = values["ts_init"]

    package = types.ModuleType("nautilus_trader")
    package.__path__ = []  # type: ignore[attr-defined]
    model = types.ModuleType("nautilus_trader.model")
    model.Bar = FakeBar
    model.BarType = FakeBarType
    model.Price = FakeValue
    model.Quantity = FakeValue
    monkeypatch.setitem(sys.modules, "nautilus_trader", package)
    monkeypatch.setitem(sys.modules, "nautilus_trader.model", model)
    monkeypatch.setattr(
        nautilus_adapter.metadata,
        "version",
        lambda distribution: "2.0.0rc3",
    )

    response = handle_request(_catalog_request())
    materialization = response["result"]["materialization"]

    assert materialization["status"] == "ready"
    assert materialization["materializedBars"] == 2
    assert materialization["forbiddenRuntimeModulesLoaded"] == []
    assert response["safety"]["networkAdaptersImported"] is False
    validate_sealed_response(response)


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (
            lambda request: request["safety"].update(
                {"networkUseAuthorized": True}
            ),
            "SAFETY_CONTRACT_MISMATCH",
        ),
        (lambda request: request["safety"].update({"promotable": 0}), "SAFETY_CONTRACT_MISMATCH"),
        (lambda request: request["payload"].update({"apiKey": "x"}), "CREDENTIAL_FIELD_FORBIDDEN"),
        (lambda request: request["payload"]["bars"][0].update({"confirmed": False}), "UNCONFIRMED_BAR"),
        (
            lambda request: request["payload"]["bars"][1].update(
                {"tsOpenNs": request["payload"]["bars"][0]["tsOpenNs"] + 2 * FIVE_MINUTES_NS}
            ),
            "NON_CONTIGUOUS_BARS",
        ),
        (lambda request: request["payload"]["bars"][0].update({"high": "59000"}), "INVALID_BAR"),
    ],
)
def test_catalog_fails_closed(mutation, code: str) -> None:
    request = _catalog_request()
    body = {key: value for key, value in request.items() if key != "requestSha256"}
    mutation(body)
    with pytest.raises(ProtocolError) as error:
        seal_request(body)
    assert error.value.code == code


def test_request_hash_tampering_and_noncanonical_input_are_rejected() -> None:
    request = _catalog_request()
    tampered = copy.deepcopy(request)
    tampered["payload"]["bars"][0]["close"] = "60101"
    with pytest.raises(ProtocolError) as error:
        decode_canonical_request(canonical_json(tampered))
    assert error.value.code == "REQUEST_HASH_MISMATCH"

    pretty = json.dumps(request, ensure_ascii=False, sort_keys=True, indent=2)
    with pytest.raises(ProtocolError) as error:
        decode_canonical_request(pretty)
    assert error.value.code == "NON_CANONICAL_JSON"

    duplicate = '{"operation":"protocol_self_check","operation":"protocol_self_check"}'
    with pytest.raises(ProtocolError) as error:
        decode_canonical_request(duplicate)
    assert error.value.code == "DUPLICATE_KEY"


def test_cli_self_check_outputs_one_canonical_response() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "research.nautilus_sidecar", "--self-test"],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert completed.stdout.endswith("\n")
    document = completed.stdout[:-1]
    payload = json.loads(document)
    assert document == canonical_json(payload)
    assert payload["status"] == "accepted"
    validate_sealed_response(payload)


def test_dependency_status_does_not_import_nautilus_package() -> None:
    probe = """
import json
import sys
from research.nautilus_sidecar.nautilus_adapter import dependency_status

assert not any(
    name == "nautilus_trader" or name.startswith("nautilus_trader.")
    for name in sys.modules
)
status = dependency_status()
assert status["packageImported"] is False, status
assert status["packageImportAttempted"] is False, status
assert not any(
    name == "nautilus_trader" or name.startswith("nautilus_trader.")
    for name in sys.modules
)
print(json.dumps(status, sort_keys=True))
"""
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["packageImported"] is False


def test_source_tree_has_no_network_exchange_or_order_imports() -> None:
    forbidden_roots = {"httpx", "requests", "socket", "urllib", "websockets"}
    forbidden_nautilus_segments = {
        "adapters",
        "execution",
        "live",
        "system",
        "trading",
    }
    for path in SIDECAR_ROOT.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            for module in modules:
                assert module.split(".")[0] not in forbidden_roots
                if module.startswith("nautilus_trader."):
                    segments = set(module.split("."))
                    assert not segments.intersection(forbidden_nautilus_segments)


def test_unpublished_protocol_uses_only_v6_corrected_semantics() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(SIDECAR_ROOT.glob("*.py"))
    )
    assert "V5" not in source
    assert "nativeV5" not in source
    assert "NAUTILUS_NATIVE_BARS_HAVE_NO_NEXT_BAR_OPEN_FILL_MODE" not in source
