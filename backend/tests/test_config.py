from pathlib import Path

from okx_demo_lab.config import app_data_dir


def test_app_data_dir_respects_explicit_workspace_override(
    tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "workspace-data"
    monkeypatch.setenv("TIDEGUARD_DATA_DIR", str(target))

    assert app_data_dir() == target
    assert target.is_dir()
