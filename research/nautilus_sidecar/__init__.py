"""Network-use-unauthorized NautilusTrader research sidecar protocol.

This package intentionally exposes no network, credential, exchange-adapter, or
order APIs. It does not claim operating-system-level network isolation. It can
validate its canonical protocol without NautilusTrader being installed and can
optionally materialize local ``Bar`` value objects when the exact audited
NautilusTrader build is available.
"""

from .protocol import (
    BAR_CATALOG_OPERATION,
    PROTOCOL_VERSION,
    REQUEST_SCHEMA_VERSION,
    RESPONSE_SCHEMA_VERSION,
    SELF_CHECK_OPERATION,
    ProtocolError,
    canonical_json,
    decode_canonical_request,
    seal_request,
    sha256_hex,
)
from .service import build_self_check_request, handle_request

__all__ = [
    "BAR_CATALOG_OPERATION",
    "PROTOCOL_VERSION",
    "REQUEST_SCHEMA_VERSION",
    "RESPONSE_SCHEMA_VERSION",
    "SELF_CHECK_OPERATION",
    "ProtocolError",
    "build_self_check_request",
    "canonical_json",
    "decode_canonical_request",
    "handle_request",
    "seal_request",
    "sha256_hex",
]
