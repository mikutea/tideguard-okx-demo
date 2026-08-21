from __future__ import annotations

import pytest

from okx_demo_lab.ml.strategy import canonical_json, sha256_hex
from research.verify_historical_replay import (
    ReplayVerificationError,
    verify_report,
)


def test_verifier_rejects_hash_tampering_before_reading_metrics() -> None:
    body = {"decision": "research_only"}
    report = {**body, "reportSha256": sha256_hex(canonical_json(body))}
    report["decision"] = "trade"

    with pytest.raises(ReplayVerificationError, match="canonical report hash"):
        verify_report(report)


def test_verifier_requires_research_only_execution_contract_even_with_valid_hash() -> None:
    body = {
        "decision": "research_only",
        "promotable": False,
        "schemaVersion": "moheng.historical-replay-report.v1",
        "shadowDaysCredited": 0,
    }
    report = {**body, "reportSha256": sha256_hex(canonical_json(body))}

    with pytest.raises(ReplayVerificationError, match="execution contract"):
        verify_report(report)
