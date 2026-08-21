from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def remove_okx_credentials_from_research_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Public research tests must never inherit OKX account credentials."""

    for variable in (
        "OKX_API_KEY",
        "OKX_API_SECRET",
        "OKX_API_PASSPHRASE",
        "OKX_SECRET_KEY",
        "OKX_PASSPHRASE",
    ):
        monkeypatch.delenv(variable, raising=False)
