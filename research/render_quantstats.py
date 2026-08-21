from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import pandas as pd
import quantstats as qs

from okx_demo_lab.ml.research import RESEARCH_SCHEMA_VERSION
from okx_demo_lab.ml.strategy import canonical_json, sha256_hex


class ReportError(RuntimeError):
    pass


def _verified_report(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportError("benchmark report cannot be read") from exc
    if not isinstance(value, dict) or value.get("schemaVersion") != RESEARCH_SCHEMA_VERSION:
        raise ReportError("benchmark report schema is unsupported")
    expected = value.get("reportSha256")
    if not isinstance(expected, str):
        raise ReportError("benchmark report hash is missing")
    payload = dict(value)
    payload.pop("reportSha256", None)
    if sha256_hex(canonical_json(payload)) != expected:
        raise ReportError("benchmark report hash does not match its content")
    return value


def render(report_path: Path, output_dir: Path) -> list[Path]:
    report = _verified_report(report_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    for result in report.get("results", []):
        if not isinstance(result, dict) or not isinstance(result.get("folds"), list):
            raise ReportError("benchmark model result is malformed")
        family = str(result.get("family", "unknown"))
        slug = re.sub(r"[^a-z0-9_-]+", "-", family.lower()).strip("-")
        if not slug:
            raise ReportError("benchmark family name is invalid")
        index = pd.DatetimeIndex(
            [item["test_stop_at"] for item in result["folds"]],
            tz="UTC",
        )
        returns = pd.Series(
            [float(item["net_return"]) for item in result["folds"]],
            index=index,
            name=family,
            dtype="float64",
        )
        destination = output_dir / f"{slug}.html"
        qs.reports.html(
            returns,
            benchmark=None,
            periods_per_year=4,
            output=str(destination),
            title=f"MOHENG research diagnostic — {family}",
        )
        outputs.append(destination)
    return outputs


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render optional quarterly-fold QuantStats diagnostics."
    )
    parser.add_argument("report", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    outputs = render(args.report, args.output_dir)
    print(json.dumps({"rendered": [str(path.resolve()) for path in outputs]}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReportError as exc:
        print(json.dumps({"error": str(exc), "errorType": type(exc).__name__}))
        raise SystemExit(2) from exc
