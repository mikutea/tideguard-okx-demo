from __future__ import annotations

import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_user_credentials_and_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Tests must never discover the developer's real selector or Credential Manager."""

    monkeypatch.setenv("TIDEGUARD_DATA_DIR", str(tmp_path / "app-data"))

    import okx_demo_lab.okx_client as client_module
    import okx_demo_lab.secrets as secrets_module

    monkeypatch.setattr(client_module, "get_credentials", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        secrets_module, "credentials_configured", lambda *args, **kwargs: False
    )
    for module_name in (
        "okx_demo_lab.main",
        "okx_demo_lab.service",
        "okx_demo_lab.ml.runtime",
    ):
        module = sys.modules.get(module_name)
        if module is not None and hasattr(module, "credentials_configured"):
            monkeypatch.setattr(
                module, "credentials_configured", lambda *args, **kwargs: False
            )

    yield
