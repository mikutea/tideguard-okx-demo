from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence


PROTOCOL_VERSION = "moheng.nautilus-offline-sidecar.v1"
REQUEST_SCHEMA_VERSION = "moheng.nautilus-offline-request.v1"
RESPONSE_SCHEMA_VERSION = "moheng.nautilus-offline-response.v1"
SELF_CHECK_OPERATION = "protocol_self_check"
BAR_CATALOG_OPERATION = "validate_offline_bar_catalog"

EXPECTED_NAUTILUS_VERSION = "2.0.0rc3"
AUDITED_UPSTREAM_COMMIT = "648970ce64a304d93da0a29320cb6e19b905fa39"
REVIEWED_DEVELOP_COMMIT = "2114cf6f761429e0adb5ca9596fcd7b895b16011"

FIVE_MINUTES_NS = 300_000_000_000
MAX_REQUEST_BYTES = 16 * 1024 * 1024
MAX_BARS = 100_000
INSTRUMENT_ID = "BTC-USDT.OKX"
BAR_SPECIFICATION = "5-MINUTE-LAST-EXTERNAL"

SAFETY_CONTRACT: dict[str, Any] = {
    "credentialsAllowed": False,
    "executionAllowed": False,
    "mode": "offline_research_only",
    "networkAdaptersImported": False,
    "networkUseAuthorized": False,
    "orderCapability": False,
    "osNetworkIsolationEnforced": False,
    "privateApi": False,
    "promotable": False,
    "shadowDaysCredited": 0,
}

V6_EXECUTION_CONTRACT: dict[str, Any] = {
    "decisionAt": "confirmed_bar_close_next_bar_open_boundary",
    "entryAt": "next_bar_open_same_timestamp",
    "exitAt": "entry_plus_12_bars_open",
    "holdingBars": 12,
    "labelHorizonBars": 12,
    "protocolVersion": "moheng.execution.corrected-next-open-boundary.v1",
    "sameSourceBarFillAllowed": False,
    "sameTimestampFillAllowed": True,
}

SEMANTIC_BOUNDARY: dict[str, Any] = {
    "canonicalContract": V6_EXECUTION_CONTRACT,
    "canonicalExecutionVersion": "V6",
    "comparisonPolicy": "do_not_compare_pnl_as_execution_equivalent",
    "nativeNautilusExecutionParityValidated": False,
    "reasonCode": "NATIVE_BAR_FILL_PARITY_NOT_VALIDATED",
    "requiredForEquivalence": (
        "validated_parity_with_the_v6_corrected_next_open_boundary_contract"
    ),
}

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DECIMAL_RE = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_FORBIDDEN_KEYS = frozenset(
    {
        "apikey",
        "apipassphrase",
        "apisecret",
        "credential",
        "credentials",
        "passphrase",
        "privatekey",
        "secret",
        "secretkey",
        "token",
    }
)


class ProtocolError(ValueError):
    """A deterministic, non-secret-bearing protocol rejection."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def canonical_json(value: Any) -> str:
    """Return the sole JSON representation accepted by this sidecar."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ProtocolError("NON_CANONICAL_VALUE", "value is not canonical JSON") from exc


def sha256_hex(value: bytes | str) -> str:
    material = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(material).hexdigest()


def _reject_constant(value: str) -> None:
    raise ProtocolError("NON_FINITE_NUMBER", f"non-finite JSON number is forbidden: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ProtocolError("DUPLICATE_KEY", "duplicate JSON object keys are forbidden")
        value[key] = item
    return value


def decode_canonical_json(raw: bytes | str) -> Any:
    """Decode canonical UTF-8 JSON, permitting only one terminal line ending."""

    material = raw.encode("utf-8") if isinstance(raw, str) else raw
    if len(material) > MAX_REQUEST_BYTES:
        raise ProtocolError("REQUEST_TOO_LARGE", "canonical request exceeds the byte limit")
    try:
        text = material.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolError("INVALID_UTF8", "request must be UTF-8") from exc
    if text.startswith("\ufeff"):
        raise ProtocolError("NON_CANONICAL_JSON", "UTF-8 BOM is forbidden")
    if text.endswith("\r\n"):
        document = text[:-2]
    elif text.endswith("\n"):
        document = text[:-1]
    else:
        document = text
    try:
        value = json.loads(
            document,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except ProtocolError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ProtocolError("INVALID_JSON", "request is not valid JSON") from exc
    if canonical_json(value) != document:
        raise ProtocolError("NON_CANONICAL_JSON", "request is not canonical JSON")
    return value


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    actual = set(value)
    if actual != expected:
        raise ProtocolError(
            "SCHEMA_MISMATCH",
            f"{name} keys must be exactly {sorted(expected)}",
        )


def _require_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolError("SCHEMA_MISMATCH", f"{name} must be an object")
    return value


def _require_sha256(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ProtocolError("SCHEMA_MISMATCH", f"{name} must be a lowercase SHA-256")
    return value


def _require_integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError("SCHEMA_MISMATCH", f"{name} must be an integer")
    return value


def _decimal(value: Any, name: str, *, allow_zero: bool) -> Decimal:
    if not isinstance(value, str) or not _DECIMAL_RE.fullmatch(value):
        raise ProtocolError(
            "INVALID_BAR",
            f"{name} must be a non-negative plain decimal string",
        )
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ProtocolError("INVALID_BAR", f"{name} is not a decimal") from exc
    if parsed < 0 or (not allow_zero and parsed == 0):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ProtocolError("INVALID_BAR", f"{name} must be {qualifier}")
    return parsed


def _reject_forbidden_keys(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if normalized in _FORBIDDEN_KEYS:
                raise ProtocolError(
                    "CREDENTIAL_FIELD_FORBIDDEN",
                    "credential-shaped fields are forbidden from the offline protocol",
                )
            _reject_forbidden_keys(item)
    elif isinstance(value, list):
        for item in value:
            _reject_forbidden_keys(item)


def seal_request(body: Mapping[str, Any]) -> dict[str, Any]:
    """Return a content-addressed request after validating its unhashed body."""

    material = deepcopy(dict(body))
    if "requestSha256" in material:
        raise ProtocolError("SCHEMA_MISMATCH", "request body is already sealed")
    validate_request_body(material)
    return {**material, "requestSha256": sha256_hex(canonical_json(material))}


def decode_canonical_request(raw: bytes | str) -> dict[str, Any]:
    request = decode_canonical_json(raw)
    if not isinstance(request, dict):
        raise ProtocolError("SCHEMA_MISMATCH", "request must be an object")
    validate_sealed_request(request)
    return request


def validate_request_body(body: Mapping[str, Any]) -> None:
    _require_exact_keys(
        body,
        {"operation", "payload", "safety", "schemaVersion"},
        "request",
    )
    if body.get("schemaVersion") != REQUEST_SCHEMA_VERSION:
        raise ProtocolError("UNSUPPORTED_SCHEMA", "request schema version is unsupported")
    operation = body.get("operation")
    if operation not in {SELF_CHECK_OPERATION, BAR_CATALOG_OPERATION}:
        raise ProtocolError("UNSUPPORTED_OPERATION", "operation is not offline-safe")
    safety = _require_mapping(body.get("safety"), "safety")
    if canonical_json(safety) != canonical_json(SAFETY_CONTRACT):
        raise ProtocolError("SAFETY_CONTRACT_MISMATCH", "offline safety contract is not exact")
    payload = _require_mapping(body.get("payload"), "payload")
    _reject_forbidden_keys(payload)
    if operation == SELF_CHECK_OPERATION:
        _require_exact_keys(payload, set(), "self-check payload")
    else:
        validate_bar_catalog_payload(payload)


def validate_sealed_request(request: Mapping[str, Any]) -> None:
    _require_exact_keys(
        request,
        {"operation", "payload", "requestSha256", "safety", "schemaVersion"},
        "sealed request",
    )
    stored = _require_sha256(request.get("requestSha256"), "requestSha256")
    body = {key: value for key, value in request.items() if key != "requestSha256"}
    validate_request_body(body)
    if stored != sha256_hex(canonical_json(body)):
        raise ProtocolError("REQUEST_HASH_MISMATCH", "canonical request hash does not match")


def validate_bar_catalog_payload(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    _require_exact_keys(
        payload,
        {"barSpecification", "bars", "instrumentId", "source"},
        "bar catalog payload",
    )
    if payload.get("instrumentId") != INSTRUMENT_ID:
        raise ProtocolError(
            "INSTRUMENT_NOT_ALLOWED",
            f"offline PoC is fixed to {INSTRUMENT_ID}",
        )
    if payload.get("barSpecification") != BAR_SPECIFICATION:
        raise ProtocolError("BAR_TYPE_NOT_ALLOWED", "offline PoC requires confirmed 5-minute bars")
    source = _require_mapping(payload.get("source"), "source")
    _require_exact_keys(source, {"contentSha256", "kind"}, "source")
    if source.get("kind") != "okx_public_frozen_snapshot":
        raise ProtocolError("SOURCE_NOT_ALLOWED", "only a frozen OKX public snapshot is allowed")
    _require_sha256(source.get("contentSha256"), "source.contentSha256")
    bars = payload.get("bars")
    if not isinstance(bars, list) or not bars or len(bars) > MAX_BARS:
        raise ProtocolError("INVALID_BAR_CATALOG", f"bars must contain 1..{MAX_BARS} rows")

    normalized: list[dict[str, Any]] = []
    previous_open: int | None = None
    for index, raw_bar in enumerate(bars):
        bar = _require_mapping(raw_bar, f"bars[{index}]")
        _require_exact_keys(
            bar,
            {"close", "confirmed", "high", "low", "open", "tsOpenNs", "volume"},
            f"bars[{index}]",
        )
        ts_open = _require_integer(bar.get("tsOpenNs"), f"bars[{index}].tsOpenNs")
        if ts_open <= 0 or ts_open % FIVE_MINUTES_NS != 0:
            raise ProtocolError("INVALID_BAR", "bar open timestamp must align to a 5-minute grid")
        if previous_open is not None and ts_open - previous_open != FIVE_MINUTES_NS:
            raise ProtocolError("NON_CONTIGUOUS_BARS", "bars must be strictly contiguous")
        if bar.get("confirmed") is not True:
            raise ProtocolError("UNCONFIRMED_BAR", "every offline bar must be exchange-confirmed")
        open_price = _decimal(bar.get("open"), f"bars[{index}].open", allow_zero=False)
        high_price = _decimal(bar.get("high"), f"bars[{index}].high", allow_zero=False)
        low_price = _decimal(bar.get("low"), f"bars[{index}].low", allow_zero=False)
        close_price = _decimal(bar.get("close"), f"bars[{index}].close", allow_zero=False)
        _decimal(bar.get("volume"), f"bars[{index}].volume", allow_zero=True)
        if high_price < max(open_price, low_price, close_price):
            raise ProtocolError("INVALID_BAR", "bar high is below an OHLC value")
        if low_price > min(open_price, high_price, close_price):
            raise ProtocolError("INVALID_BAR", "bar low is above an OHLC value")
        normalized.append(
            {
                "barSpecification": BAR_SPECIFICATION,
                "close": bar["close"],
                "high": bar["high"],
                "instrumentId": INSTRUMENT_ID,
                "low": bar["low"],
                "open": bar["open"],
                "tsEventNs": ts_open + FIVE_MINUTES_NS,
                "tsInitNs": ts_open + FIVE_MINUTES_NS,
                "tsOpenNs": ts_open,
                "volume": bar["volume"],
            }
        )
        previous_open = ts_open
    return normalized


def seal_response(body: Mapping[str, Any]) -> dict[str, Any]:
    material = dict(body)
    return {**material, "responseSha256": sha256_hex(canonical_json(material))}


def validate_sealed_response(response: Mapping[str, Any]) -> None:
    stored = _require_sha256(response.get("responseSha256"), "responseSha256")
    body = {key: value for key, value in response.items() if key != "responseSha256"}
    if response.get("schemaVersion") != RESPONSE_SCHEMA_VERSION:
        raise ProtocolError("UNSUPPORTED_SCHEMA", "response schema version is unsupported")
    if stored != sha256_hex(canonical_json(body)):
        raise ProtocolError("RESPONSE_HASH_MISMATCH", "canonical response hash does not match")


def deterministic_catalog_summary(
    request_sha256: str,
    payload: Mapping[str, Any],
    normalized_bars: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    source = _require_mapping(payload["source"], "source")
    return {
        "barCount": len(normalized_bars),
        "barDataSha256": sha256_hex(canonical_json(list(normalized_bars))),
        "firstTsOpenNs": normalized_bars[0]["tsOpenNs"],
        "instrumentId": payload["instrumentId"],
        "lastTsOpenNs": normalized_bars[-1]["tsOpenNs"],
        "nativeCanonicalExecutionParityValidated": False,
        "operation": BAR_CATALOG_OPERATION,
        "protocolVersion": PROTOCOL_VERSION,
        "requestSha256": request_sha256,
        "sourceSha256": source["contentSha256"],
    }
