from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from .nautilus_adapter import (
    dependency_status,
    require_offline_module_boundary,
    validate_local_bar_materialization,
)
from .protocol import (
    AUDITED_UPSTREAM_COMMIT,
    BAR_CATALOG_OPERATION,
    EXPECTED_NAUTILUS_VERSION,
    PROTOCOL_VERSION,
    REQUEST_SCHEMA_VERSION,
    REVIEWED_DEVELOP_COMMIT,
    RESPONSE_SCHEMA_VERSION,
    SAFETY_CONTRACT,
    SELF_CHECK_OPERATION,
    SEMANTIC_BOUNDARY,
    ProtocolError,
    canonical_json,
    deterministic_catalog_summary,
    seal_request,
    seal_response,
    sha256_hex,
    validate_bar_catalog_payload,
    validate_sealed_request,
)


def build_self_check_request() -> dict[str, Any]:
    return seal_request(
        {
            "operation": SELF_CHECK_OPERATION,
            "payload": {},
            "safety": dict(SAFETY_CONTRACT),
            "schemaVersion": REQUEST_SCHEMA_VERSION,
        }
    )


def _base_response(request: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "decision": "research_only",
        "nautilus": {
            **dependency_status(),
            "auditedUpstreamCommit": AUDITED_UPSTREAM_COMMIT,
            "integrationBoundary": "local_model_values_only",
            "reviewedDevelopCommit": REVIEWED_DEVELOP_COMMIT,
        },
        "operation": request["operation"],
        "promotable": False,
        "protocolVersion": PROTOCOL_VERSION,
        "requestSha256": request["requestSha256"],
        "safety": dict(SAFETY_CONTRACT),
        "schemaVersion": RESPONSE_SCHEMA_VERSION,
        "semanticBoundary": deepcopy(SEMANTIC_BOUNDARY),
        "shadowDaysCredited": 0,
        "status": "accepted",
    }


def handle_request(request: Mapping[str, Any]) -> dict[str, Any]:
    """Handle a validated offline request with no I/O beyond the caller."""

    validate_sealed_request(request)
    require_offline_module_boundary()
    response = _base_response(request)
    if request["operation"] == SELF_CHECK_OPERATION:
        summary = {
            "nativeCanonicalExecutionParityValidated": False,
            "operation": SELF_CHECK_OPERATION,
            "protocolVersion": PROTOCOL_VERSION,
            "requestSha256": request["requestSha256"],
            "safetyContractSha256": sha256_hex(canonical_json(SAFETY_CONTRACT)),
        }
        response["result"] = {
            "nautilusRequiredForSelfCheck": False,
            "offlineProtocolReady": True,
            "supportedOperations": [SELF_CHECK_OPERATION, BAR_CATALOG_OPERATION],
        }
    else:
        payload = request["payload"]
        normalized = validate_bar_catalog_payload(payload)
        summary = deterministic_catalog_summary(
            request["requestSha256"],
            payload,
            normalized,
        )
        response["result"] = {
            "catalog": {
                "barCount": summary["barCount"],
                "barDataSha256": summary["barDataSha256"],
                "barType": "BTC-USDT.OKX-5-MINUTE-LAST-EXTERNAL",
                "firstTsOpenNs": summary["firstTsOpenNs"],
                "lastTsOpenNs": summary["lastTsOpenNs"],
                "timestampsRepresentCloseAvailability": True,
            },
            "materialization": validate_local_bar_materialization(normalized),
            "simulationExecuted": False,
            "tradesProduced": 0,
        }
    response["summary"] = summary
    response["summarySha256"] = sha256_hex(canonical_json(summary))
    require_offline_module_boundary()
    return seal_response(response)


def build_error_response(error: ProtocolError) -> dict[str, Any]:
    """Return a deterministic error without echoing untrusted request content."""

    return seal_response(
        {
            "decision": "rejected",
            "error": {"code": error.code, "message": error.message},
            "expectedNautilusVersion": EXPECTED_NAUTILUS_VERSION,
            "promotable": False,
            "protocolVersion": PROTOCOL_VERSION,
            "schemaVersion": RESPONSE_SCHEMA_VERSION,
            "shadowDaysCredited": 0,
            "status": "rejected",
        }
    )
