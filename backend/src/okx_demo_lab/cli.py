from __future__ import annotations

import argparse
import getpass
import json
import sys
from datetime import datetime, timezone

from .audit import AuditStore
from .config import app_data_dir
from .ml.autonomy import AutonomyPolicy, AutonomyStore, SupervisorDenied
from .ml.long_run import LONG_RUN_PROMOTION_POLICY
from .ml.registry import ModelRegistry, PromotionDenied, RegistryError
from .ml.supervisor import CodexSupervisor
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


def _supervisor() -> CodexSupervisor:
    data_dir = app_data_dir()
    return CodexSupervisor(
        registry=ModelRegistry(data_dir / "ml-registry.sqlite3"),
        autonomy=AutonomyStore(data_dir / "autonomy.sqlite3"),
        audit=AuditStore(data_dir / "state.sqlite3"),
        promotion_policy=LONG_RUN_PROMOTION_POLICY,
        autonomy_policy=AutonomyPolicy(),
    )


def _json_output(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(prog="tideguard")
    sub = parser.add_subparsers(dest="command", required=True)
    credentials = sub.add_parser("credentials", help="管理 Windows Credential Manager 凭证")
    credential_sub = credentials.add_subparsers(dest="credential_command", required=True)
    credential_sub.add_parser("set")
    credential_sub.add_parser("status")
    credential_sub.add_parser("delete")
    supervisor = sub.add_parser(
        "supervisor", help="Codex 脱敏模型审查与长期 Demo lease"
    )
    supervisor_sub = supervisor.add_subparsers(
        dest="supervisor_command", required=True
    )
    supervisor_sub.add_parser("review")
    approve = supervisor_sub.add_parser("approve")
    approve.add_argument("--model-id", required=True)
    approve.add_argument("--evidence", required=True)
    approve.add_argument("--rationale", required=True)
    lease = supervisor_sub.add_parser("lease")
    lease.add_argument("--evidence", required=True)
    lease.add_argument("--rationale", required=True)
    rollback = supervisor_sub.add_parser("rollback")
    rollback.add_argument("--model-id", required=True)
    rollback.add_argument("--evidence", required=True)
    rollback.add_argument("--rationale", required=True)
    reject = supervisor_sub.add_parser("reject")
    reject.add_argument("--model-id", required=True)
    reject.add_argument("--evidence", required=True)
    reject.add_argument("--rationale", required=True)
    suspend = supervisor_sub.add_parser("suspend")
    suspend.add_argument("--rationale", required=True)
    args = parser.parse_args()

    try:
        if args.command == "credentials":
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
        if args.command == "supervisor":
            workflow = _supervisor()
            now = datetime.now(timezone.utc)
            if args.supervisor_command == "review":
                _json_output(workflow.review_pack(now=now))
                return 0
            if args.supervisor_command == "approve":
                _json_output(
                    workflow.approve_candidate(
                        args.model_id,
                        expected_evidence_sha256=args.evidence,
                        rationale=args.rationale,
                        now=now,
                    )
                )
                return 0
            if args.supervisor_command == "lease":
                _json_output(
                    workflow.issue_execution_lease(
                        expected_evidence_sha256=args.evidence,
                        rationale=args.rationale,
                        now=now,
                    )
                )
                return 0
            if args.supervisor_command == "rollback":
                _json_output(
                    workflow.rollback_champion(
                        args.model_id,
                        expected_evidence_sha256=args.evidence,
                        rationale=args.rationale,
                        now=now,
                    )
                )
                return 0
            if args.supervisor_command == "reject":
                _json_output(
                    {
                        "decisionId": workflow.reject_candidate(
                            args.model_id,
                            expected_evidence_sha256=args.evidence,
                            rationale=args.rationale,
                            now=now,
                        )
                    }
                )
                return 0
            if args.supervisor_command == "suspend":
                _json_output(
                    {"decisionId": workflow.suspend(rationale=args.rationale, now=now)}
                )
                return 0
    except (SecretStoreError, SupervisorDenied, PromotionDenied, RegistryError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
