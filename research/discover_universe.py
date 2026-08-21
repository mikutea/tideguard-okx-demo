from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from okx_demo_lab.config import ALLOWED_INSTRUMENTS
from okx_demo_lab.ml.strategy import canonical_json, sha256_hex
from okx_demo_lab.ml.universe import UniversePolicy, select_research_universe
from okx_demo_lab.public_market import OkxPublicMarketClient, PublicMarketError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / ".research-data" / "universes" / "universe-latest.json"


def _project_output(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    data_root = (PROJECT_ROOT / ".research-data").resolve()
    try:
        resolved.relative_to(data_root)
    except ValueError as exc:
        raise ValueError(
            "universe output must stay under project .research-data"
        ) from exc
    return resolved


def _write_atomic(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


async def discover(output: Path) -> dict[str, object]:
    client = OkxPublicMarketClient()
    try:
        raw = await client.get_spot_universe_inputs()
    finally:
        await client.close()
    snapshot = select_research_universe(
        raw["instruments"],
        raw["tickers"],
        now=datetime.now(timezone.utc),
        policy=UniversePolicy(),
    )
    report: dict[str, object] = {
        "executionAllowlistChanged": False,
        "executionAllowlist": sorted(ALLOWED_INSTRUMENTS),
        "nextGate": "complete_history_and_aligned_portfolio_oos",
        "snapshot": {**snapshot.to_dict(), "sha256": snapshot.sha256},
    }
    report["reportSha256"] = sha256_hex(canonical_json(report))
    _write_atomic(output, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Discover a provisional public-only OKX SPOT research universe."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
    )
    args = parser.parse_args()
    try:
        output = _project_output(args.output)
        report = asyncio.run(discover(output))
    except (PublicMarketError, ValueError) as exc:
        print(json.dumps({"errorType": type(exc).__name__, "message": str(exc)}))
        return 2
    print(
        json.dumps(
            {
                "members": [
                    item["instrument"]
                    for item in report["snapshot"]["members"]  # type: ignore[index]
                ],
                "output": str(output),
                "reportSha256": report["reportSha256"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
