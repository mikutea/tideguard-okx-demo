from __future__ import annotations

import hashlib
import platform
from dataclasses import dataclass

import keyring

from .profile import DEMO_PROFILE, EnvironmentProfile, profile_for

SERVICE_NAME = DEMO_PROFILE.credential_service
LIVE_SERVICE_NAME = "Tideguard.OKX.Live"
KEY_NAMES = ("api_key", "api_secret", "passphrase")


class SecretStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class Credentials:
    api_key: str
    api_secret: str
    passphrase: str


def credential_fingerprint(
    credentials: Credentials,
    environment: str | EnvironmentProfile = DEMO_PROFILE,
) -> str:
    """Return a non-secret, domain-separated identifier for one API key."""
    profile = profile_for(environment)
    material = f"{profile.credential_service}\0{credentials.api_key}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _assert_windows_native_backend() -> None:
    if platform.system() != "Windows":
        raise SecretStoreError("凭证设置仅支持 Windows Credential Manager。")
    backend = keyring.get_keyring()
    identity = f"{backend.__class__.__module__}.{backend.__class__.__name__}".lower()
    if "windows" not in identity and "winvault" not in identity:
        raise SecretStoreError(
            "未检测到 Windows 原生凭证后端；为避免明文降级，程序已拒绝读写凭证。"
        )


def set_credentials(
    credentials: Credentials,
    environment: str | EnvironmentProfile = DEMO_PROFILE,
) -> None:
    _assert_windows_native_backend()
    profile = profile_for(environment)
    values = {
        "api_key": credentials.api_key,
        "api_secret": credentials.api_secret,
        "passphrase": credentials.passphrase,
    }
    if any(not value.strip() for value in values.values()):
        raise SecretStoreError("三个凭证字段都必须填写。")
    for name, value in values.items():
        keyring.set_password(profile.credential_service, name, value.strip())


def get_credentials(
    environment: str | EnvironmentProfile = DEMO_PROFILE,
) -> Credentials | None:
    _assert_windows_native_backend()
    profile = profile_for(environment)
    values = {
        name: keyring.get_password(profile.credential_service, name) for name in KEY_NAMES
    }
    if not any(values.values()):
        return None
    if not all(values.values()):
        raise SecretStoreError("Windows Credential Manager 中的凭证不完整，请重新设置。")
    return Credentials(
        api_key=values["api_key"] or "",
        api_secret=values["api_secret"] or "",
        passphrase=values["passphrase"] or "",
    )


def credentials_configured(
    environment: str | EnvironmentProfile = DEMO_PROFILE,
) -> bool:
    try:
        return get_credentials(environment) is not None
    except SecretStoreError:
        return False


def delete_credentials(
    environment: str | EnvironmentProfile = DEMO_PROFILE,
) -> None:
    _assert_windows_native_backend()
    profile = profile_for(environment)
    for name in KEY_NAMES:
        try:
            keyring.delete_password(profile.credential_service, name)
        except keyring.errors.PasswordDeleteError:
            pass
