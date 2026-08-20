from __future__ import annotations

import argparse
import getpass
import json
import sys
from datetime import datetime, timezone

from .audit import AuditStore
from .config import ENVIRONMENT_SELECTOR_FILE, app_data_dir
from .environment import EnvironmentSelectionStore, RuntimeEnvironment
from .ml.autonomy import AutonomyPolicy, AutonomyStore, SupervisorDenied
from .ml.long_run import LONG_RUN_PROMOTION_POLICY
from .ml.market_data import MarketDataStore
from .ml.registry import ModelRegistry, PromotionDenied, RegistryError
from .ml.supervisor import CodexSupervisor
from .secrets import (
    Credentials,
    SecretStoreError,
    credentials_configured,
    delete_credentials,
    set_credentials,
)
from .profile import EnvironmentProfile, profile_for


def _credentials_set(profile: EnvironmentProfile) -> int:
    print("凭证只会写入当前 Windows 用户的 Credential Manager。")
    print("输入不会回显；不要在聊天、截图或项目文件中保存这些值。")
    api_key = getpass.getpass(f"{profile.display_name} API Key: ")
    api_secret = getpass.getpass(f"{profile.display_name} Secret: ")
    passphrase = getpass.getpass(f"{profile.display_name} Passphrase: ")
    set_credentials(Credentials(api_key, api_secret, passphrase), profile)
    header_note = "模拟盘标头" if profile.simulated_trading else "无模拟盘标头"
    print(f"已安全保存到 {profile.credential_service}；该 profile 固定使用{header_note}。")
    return 0


def _supervisor() -> CodexSupervisor:
    data_root = app_data_dir()
    runtime = RuntimeEnvironment(
        EnvironmentSelectionStore(data_root / ENVIRONMENT_SELECTOR_FILE)
    )
    if runtime.active_profile.name == "live":
        raise SupervisorDenied("Live 不能复用 Demo Codex Supervisor 授权")
    data_dir = runtime.active_profile.runtime_data_dir(data_root)
    market_data = MarketDataStore(data_root / "market-data.sqlite3")
    return CodexSupervisor(
        registry=ModelRegistry(data_dir / "ml-registry.sqlite3"),
        autonomy=AutonomyStore(data_dir / "autonomy.sqlite3"),
        audit=AuditStore(data_dir / "state.sqlite3"),
        promotion_policy=LONG_RUN_PROMOTION_POLICY,
        autonomy_policy=AutonomyPolicy(),
        market_snapshot_validator=market_data.snapshot_is_current,
    )


def _json_output(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(prog="tideguard")
    sub = parser.add_subparsers(dest="command", required=True)
    credentials = sub.add_parser("credentials", help="管理 Windows Credential Manager 凭证")
    credential_sub = credentials.add_subparsers(dest="credential_command", required=True)
    for command in ("set", "status", "delete"):
        credential_parser = credential_sub.add_parser(command)
        credential_parser.add_argument(
            "--environment", choices=("demo", "live"), default="demo"
        )
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
            profile = profile_for(args.environment)
            if args.credential_command == "set":
                return _credentials_set(profile)
            if args.credential_command == "status":
                state = "已配置" if credentials_configured(profile) else "未配置"
                print(f"{profile.display_name}：{state}")
                return 0
            if args.credential_command == "delete":
                expected = f"DELETE-{profile.name.upper()}-CREDENTIALS"
                phrase = input(f"输入 {expected} 确认删除: ")
                if phrase != expected:
                    print("未删除。")
                    return 2
                delete_credentials(profile)
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
