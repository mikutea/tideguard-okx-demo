from __future__ import annotations

import hashlib
import platform
from dataclasses import dataclass

import keyring


SERVICE_NAME = "Tideguard.OKX.Demo"
KEY_NAMES = ("api_key", "api_secret", "passphrase")


class SecretStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class Credentials:
    api_key: str
    api_secret: str
    passphrase: str


def credential_fingerprint(credentials: Credentials) -> str:
    """Return a non-secret, domain-separated identifier for one API key."""
    material = f"{SERVICE_NAME}\0{credentials.api_key}".encode("utf-8")
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


def set_credentials(credentials: Credentials) -> None:
    _assert_windows_native_backend()
    values = {
        "api_key": credentials.api_key,
        "api_secret": credentials.api_secret,
        "passphrase": credentials.passphrase,
    }
    if any(not value.strip() for value in values.values()):
        raise SecretStoreError("三个凭证字段都必须填写。")
    for name, value in values.items():
        keyring.set_password(SERVICE_NAME, name, value.strip())


def get_credentials() -> Credentials | None:
    _assert_windows_native_backend()
    values = {name: keyring.get_password(SERVICE_NAME, name) for name in KEY_NAMES}
    if not any(values.values()):
        return None
    if not all(values.values()):
        raise SecretStoreError("Windows Credential Manager 中的凭证不完整，请重新设置。")
    return Credentials(
        api_key=values["api_key"] or "",
        api_secret=values["api_secret"] or "",
        passphrase=values["passphrase"] or "",
    )


def credentials_configured() -> bool:
    try:
        return get_credentials() is not None
    except SecretStoreError:
        return False


def delete_credentials() -> None:
    _assert_windows_native_backend()
    for name in KEY_NAMES:
        try:
            keyring.delete_password(SERVICE_NAME, name)
        except keyring.errors.PasswordDeleteError:
            pass
