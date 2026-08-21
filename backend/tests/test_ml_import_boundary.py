from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


BACKEND_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = BACKEND_ROOT / "src"


def test_historical_replay_import_does_not_load_order_or_credential_graph() -> None:
    code = """
import json
import sys

import okx_demo_lab.ml.historical_replay

forbidden = (
    "okx_demo_lab.service",
    "okx_demo_lab.okx_client",
    "okx_demo_lab.ml.execution",
    "okx_demo_lab.ml.long_run",
)
loaded = [name for name in forbidden if name in sys.modules]
print(json.dumps({"loaded": loaded}, sort_keys=True))
raise SystemExit(1 if loaded else 0)
"""
    environment = os.environ.copy()
    existing_python_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        item for item in (str(SOURCE_ROOT), existing_python_path) if item
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=BACKEND_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert json.loads(completed.stdout) == {"loaded": []}


def test_package_level_public_exports_remain_compatible() -> None:
    import okx_demo_lab.ml as ml

    expected_exports = set(ml.__all__)
    assert expected_exports
    assert expected_exports == set(ml._LAZY_EXPORTS)
    for name in expected_exports:
        assert getattr(ml, name) is not None
