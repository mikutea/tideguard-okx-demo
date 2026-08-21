from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from okx_demo_lab.ml.multi_asset_cohort import (
    MultiAssetCohortError,
    build_aligned_cohort,
)
from okx_demo_lab.ml.multi_asset_market import MultiAssetMarketStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = PROJECT_ROOT / ".research-data"


def _inside_data_root(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(DEFAULT_DATA_ROOT.resolve())
    except ValueError as exc:
        raise MultiAssetCohortError(
            "all cohort inputs and outputs must stay under project .research-data"
        ) from exc
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a strict no-fill multi-asset public research cohort."
    )
    parser.add_argument(
        "--universe",
        type=Path,
        default=DEFAULT_DATA_ROOT / "universes" / "universe-latest.json",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATA_ROOT / "multi-asset-market.sqlite3",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_DATA_ROOT / "cohorts",
    )
    args = parser.parse_args()
    try:
        universe = _inside_data_root(args.universe)
        database = _inside_data_root(args.database)
        output_root = _inside_data_root(args.output_root)
        result = build_aligned_cohort(
            store=MultiAssetMarketStore(database),
            universe_path=universe,
            output_root=output_root,
            now=datetime.now(timezone.utc),
        )
    except MultiAssetCohortError as exc:
        print(json.dumps({"errorType": type(exc).__name__, "message": str(exc)}))
        return 2
    print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
