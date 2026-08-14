from __future__ import annotations

import argparse
import getpass
import sys

from .secrets import (
    Credentials,
    SecretStoreError,
    credentials_configured,
    delete_credentials,
    set_credentials,
)


def _credentials_set() -> int:
    print("凭证只会写入当前 Windows 用户的 Credential Manager。")
    print("输入不会回显；不要在聊天、截图或项目文件中保存这些值。")
    api_key = getpass.getpass("OKX Demo API Key: ")
    api_secret = getpass.getpass("OKX Demo Secret: ")
    passphrase = getpass.getpass("OKX Demo Passphrase: ")
    set_credentials(Credentials(api_key, api_secret, passphrase))
    print("已安全保存。程序只会以模拟盘标头使用这些凭证。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="tideguard")
    sub = parser.add_subparsers(dest="command", required=True)
    credentials = sub.add_parser("credentials", help="管理 Windows Credential Manager 凭证")
    credential_sub = credentials.add_subparsers(dest="credential_command", required=True)
    credential_sub.add_parser("set")
    credential_sub.add_parser("status")
    credential_sub.add_parser("delete")
    args = parser.parse_args()

    try:
        if args.credential_command == "set":
            return _credentials_set()
        if args.credential_command == "status":
            print("已配置" if credentials_configured() else "未配置")
            return 0
        if args.credential_command == "delete":
            phrase = input("输入 DELETE-DEMO-CREDENTIALS 确认删除: ")
            if phrase != "DELETE-DEMO-CREDENTIALS":
                print("未删除。")
                return 2
            delete_credentials()
            print("已从 Windows Credential Manager 删除。")
            return 0
    except SecretStoreError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
