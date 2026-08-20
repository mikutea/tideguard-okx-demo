from __future__ import annotations

import threading
import re
from datetime import datetime, timedelta, timezone

from .audit import AuditStore
from .config import policy_for_profile
from .profile import DEMO_PROFILE, EnvironmentProfile


class SafetyError(RuntimeError):
    pass


class SafetyController:
    """Arming is deliberately memory-only; the kill latch is deliberately persistent."""

    def __init__(
        self, store: AuditStore, profile: EnvironmentProfile = DEMO_PROFILE
    ):
        self.store = store
        self.profile = profile
        self.policy = policy_for_profile(profile)
        self._lock = threading.RLock()
        self._armed_until: datetime | None = None
        self._credential_fingerprint: str | None = None
        self._account_fingerprint: str | None = None
        self._supervisor_decision_id: str | None = None
        self._supervisor_purpose: str | None = None

    def _clear_arming(self) -> None:
        self._armed_until = None
        self._credential_fingerprint = None
        self._account_fingerprint = None
        self._supervisor_decision_id = None
        self._supervisor_purpose = None

    def _persistent_kill_active(self) -> bool:
        try:
            active, _ = self.store.get_kill_state()
            return active
        except Exception:
            return True

    def _append_best_effort(
        self,
        event_type: str,
        payload: dict[str, object],
        *,
        actor: str,
    ) -> None:
        try:
            self.store.append(event_type, payload, actor=actor)
        except Exception:
            pass

    def status(self) -> dict[str, str | int | bool | None]:
        with self._lock:
            killed = self._persistent_kill_active()
            now = datetime.now(timezone.utc)
            if killed:
                self._clear_arming()
                mode = "killed"
                remaining = 0
            elif self._armed_until and self._armed_until > now:
                mode = "armed"
                remaining = max(0, int((self._armed_until - now).total_seconds()))
            else:
                self._clear_arming()
                mode = "observe"
                remaining = 0
            return {
                "mode": mode,
                "operatingMode": "observe" if killed else mode,
                "environment": self.profile.name,
                "armedRemainingSeconds": remaining,
                "killActive": killed,
                "identityBound": bool(
                    mode == "armed"
                    and self._credential_fingerprint
                    and self._account_fingerprint
                ),
                "armedUntil": self._armed_until.isoformat().replace("+00:00", "Z")
                if self._armed_until
                else None,
            }

    def arm(
        self,
        confirmation: str,
        credential_fingerprint: str,
        account_fingerprint: str,
    ) -> dict[str, str | int | bool | None]:
        expected = "DEMO" if self.profile.name == "demo" else "我确认使用真实资金"
        if confirmation != expected:
            if self.profile.name == "demo":
                raise SafetyError("请输入 DEMO 以启用限时模拟下单。")
            raise SafetyError("请输入“我确认使用真实资金”以启用 60 秒实盘手工下单。")
        if not credential_fingerprint or not account_fingerprint:
            raise SafetyError("模拟账户身份未完成绑定。")
        with self._lock:
            if self._persistent_kill_active():
                raise SafetyError("急停仍处于锁定状态。")
            self._armed_until = datetime.now(timezone.utc) + timedelta(
                seconds=self.policy.arm_ttl_seconds
            )
            self._credential_fingerprint = credential_fingerprint
            self._account_fingerprint = account_fingerprint
            self._supervisor_decision_id = None
            self._supervisor_purpose = None
        self.store.append(
            "safety.armed",
            {
                "ttlSeconds": self.policy.arm_ttl_seconds,
                "environment": self.profile.name,
                "identityBound": True,
            },
            actor="user",
        )
        return self.status()

    def arm_supervised(
        self,
        decision_id: str,
        credential_fingerprint: str,
        account_fingerprint: str,
        *,
        purpose: str,
    ) -> dict[str, str | int | bool | None]:
        """Create a short in-memory arm from a verified external supervisor lease.

        The method is intentionally not exposed as an HTTP route.  The autonomy
        coordinator must first validate the content-addressed Codex decision and
        the account binding, while this controller keeps the final persistent
        kill-latch check.
        """

        if not re.fullmatch(r"sup_[0-9a-f]{28}", decision_id):
            raise SafetyError("Codex 监督决策标识无效。")
        if self.profile.name != "demo":
            raise SafetyError("实盘不能复用 Demo Codex lease；需要独立实盘授权。")
        if purpose not in {"entry", "exit"}:
            raise SafetyError("监督授权用途无效。")
        if not credential_fingerprint or not account_fingerprint:
            raise SafetyError("模拟账户身份未完成绑定。")
        with self._lock:
            if self._persistent_kill_active():
                raise SafetyError("急停仍处于锁定状态。")
            self._armed_until = datetime.now(timezone.utc) + timedelta(
                seconds=self.policy.automation_arm_ttl_seconds
            )
            self._credential_fingerprint = credential_fingerprint
            self._account_fingerprint = account_fingerprint
            self._supervisor_decision_id = decision_id
            self._supervisor_purpose = purpose
        self.store.append(
            "safety.supervisor_armed",
            {
                "decisionId": decision_id,
                "purpose": purpose,
                "ttlSeconds": self.policy.automation_arm_ttl_seconds,
                "environment": self.profile.name,
                "identityBound": True,
            },
            actor="codex-supervisor",
        )
        return self.status()

    def supervised_context(self) -> tuple[str, str] | None:
        with self._lock:
            if self.status()["mode"] != "armed":
                return None
            if self._supervisor_decision_id and self._supervisor_purpose:
                return self._supervisor_decision_id, self._supervisor_purpose
            return None

    def disarm(self, reason: str = "user") -> dict[str, str | int | bool | None]:
        with self._lock:
            self._clear_arming()
        self._append_best_effort("safety.disarmed", {"reason": reason}, actor="user")
        return self.status()

    def abort_arm_in_memory(self) -> None:
        """Best-effort fallback when persistence itself failed during arming."""
        with self._lock:
            self._clear_arming()

    def engage_kill(self, reason: str, actor: str = "user") -> dict[str, str | int | bool | None]:
        with self._lock:
            credential_fingerprint = self._credential_fingerprint
            account_fingerprint = self._account_fingerprint
            self._clear_arming()
            generation = self.store.engage_kill_latch(
                credential_fingerprint, account_fingerprint
            )
        self._append_best_effort(
            "safety.kill_engaged",
            {"reason": reason, "generation": generation},
            actor=actor,
        )
        return self.status()

    def acknowledge_persisted_kill(
        self, reason: str, actor: str = "system"
    ) -> dict[str, str | int | bool | None]:
        with self._lock:
            self._clear_arming()
        self._append_best_effort(
            "safety.kill_engaged",
            {"reason": reason, "persisted": True},
            actor=actor,
        )
        return self.status()

    def armed_identity(self) -> tuple[str, str]:
        with self._lock:
            if self.status()["mode"] != "armed":
                raise SafetyError("本地演练授权已失效。")
            if not self._credential_fingerprint or not self._account_fingerprint:
                raise SafetyError("演练授权缺少账户身份绑定。")
            return self._credential_fingerprint, self._account_fingerprint

    def armed_identity_or_none(self) -> tuple[str, str] | None:
        with self._lock:
            if self.status()["mode"] != "armed":
                return None
            if not self._credential_fingerprint or not self._account_fingerprint:
                return None
            return self._credential_fingerprint, self._account_fingerprint

    def reset_kill(
        self, confirmation: str, expected_generation: int
    ) -> dict[str, str | int | bool | None]:
        expected_phrase = (
            "解除模拟盘急停" if self.profile.name == "demo" else "解除实盘急停"
        )
        if confirmation != expected_phrase:
            raise SafetyError("确认短语不匹配。")
        with self._lock:
            self._clear_arming()
            cleared = self.store.close_manual_reviews_and_clear_kill(expected_generation)
            if not cleared:
                raise SafetyError("核对期间出现了新的急停事件；急停保持锁定")
        self._append_best_effort(
            "safety.kill_reset", {"environment": self.profile.name}, actor="user"
        )
        return self.status()
