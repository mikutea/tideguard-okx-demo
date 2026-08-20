from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from .profile import DEMO_PROFILE, EnvironmentName, EnvironmentProfile, profile_for


ENVIRONMENT_SCHEMA_VERSION = 1
SWITCH_CHALLENGE_TTL_SECONDS = 300
SWITCH_READY_DELAY_SECONDS = 10
SWITCH_PHRASES: dict[EnvironmentName, str] = {
    "demo": "切换到 OKX 模拟盘",
    "live": "切换到 OKX 实盘",
}


class EnvironmentSwitchError(RuntimeError):
    pass


class StrictEnvironmentModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EnvironmentTargetRequest(StrictEnvironmentModel):
    target: Literal["demo", "live"]


class EnvironmentAcknowledgements(StrictEnvironmentModel):
    automationStopped: bool
    noOutstandingState: bool
    restartRequired: bool
    liveFundsAtRisk: bool


class EnvironmentConfirmRequest(EnvironmentTargetRequest):
    nonce: str = Field(min_length=32, max_length=256)
    confirmation: str = Field(min_length=1, max_length=64)
    acknowledgements: EnvironmentAcknowledgements


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class EnvironmentSelection:
    profile: EnvironmentProfile
    valid: bool
    updated_at: str | None
    switch_id: str | None
    error: str | None = None


class EnvironmentSelectionStore:
    """Persist only the profile to load on the next process start."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @staticmethod
    def _material(payload: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in payload.items() if key != "sha256"}

    def load(self) -> EnvironmentSelection:
        with self._lock:
            if not self.path.exists():
                return EnvironmentSelection(DEMO_PROFILE, True, None, None)
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("environment selector is not an object")
                expected_keys = {
                    "schemaVersion",
                    "environment",
                    "updatedAt",
                    "switchId",
                    "sha256",
                }
                if set(payload) != expected_keys:
                    raise ValueError("environment selector fields are invalid")
                if payload["schemaVersion"] != ENVIRONMENT_SCHEMA_VERSION:
                    raise ValueError("environment selector schema is unsupported")
                material = self._material(payload)
                if not secrets.compare_digest(
                    str(payload["sha256"]), canonical_sha256(material)
                ):
                    raise ValueError("environment selector checksum is invalid")
                profile = profile_for(str(payload["environment"]))
                updated_at = str(payload["updatedAt"])
                switch_id = str(payload["switchId"])
                if not updated_at or not switch_id.startswith("env_"):
                    raise ValueError("environment selector metadata is invalid")
                return EnvironmentSelection(
                    profile=profile,
                    valid=True,
                    updated_at=updated_at,
                    switch_id=switch_id,
                )
            except Exception as exc:
                return EnvironmentSelection(
                    profile=DEMO_PROFILE,
                    valid=False,
                    updated_at=None,
                    switch_id=None,
                    error=type(exc).__name__,
                )

    def persist(self, target: EnvironmentProfile, *, switch_id: str) -> EnvironmentSelection:
        timestamp = _iso(_utc_now())
        material: dict[str, Any] = {
            "schemaVersion": ENVIRONMENT_SCHEMA_VERSION,
            "environment": target.name,
            "updatedAt": timestamp,
            "switchId": switch_id,
        }
        payload = {**material, "sha256": canonical_sha256(material)}
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
        )
        with self._lock:
            try:
                temporary.write_text(encoded, encoding="utf-8")
                os.replace(temporary, self.path)
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        return EnvironmentSelection(target, True, timestamp, switch_id)


class RuntimeEnvironment:
    """The active profile is immutable for the lifetime of one process."""

    def __init__(self, selector: EnvironmentSelectionStore):
        self.selector = selector
        self._lock = threading.RLock()
        self._transition_pending = False
        self._transition_target: EnvironmentName | None = None
        startup = selector.load()
        self.active_profile = startup.profile
        self.startup_selection_valid = startup.valid
        self.startup_selection_error = startup.error

    def status(self) -> dict[str, Any]:
        configured = self.selector.load()
        with self._lock:
            transition_pending = self._transition_pending
            transition_target = self._transition_target
        restart_required = (
            not configured.valid or configured.profile.name != self.active_profile.name
        )
        return {
            "activeEnvironment": self.active_profile.name,
            "activeDisplayName": self.active_profile.display_name,
            "configuredEnvironment": configured.profile.name,
            "restartRequired": restart_required,
            "selectorValid": configured.valid and self.startup_selection_valid,
            "selectorError": configured.error or self.startup_selection_error,
            "switchId": configured.switch_id,
            "updatedAt": configured.updated_at,
            "operatingMode": (
                "transition_locked"
                if transition_pending
                else "observe"
                if restart_required
                else "runtime"
            ),
            "transitionPending": transition_pending,
            "transitionTarget": transition_target,
        }

    def begin_transition(self, target: str | EnvironmentProfile) -> dict[str, Any]:
        """Latch an in-process dispatch gate before any final switch recheck.

        The latch is intentionally monotonic for this process.  If confirmation
        later fails because exchange or disk state changed, execution remains
        blocked until restart instead of reopening a TOCTOU window.
        """

        target_profile = profile_for(target)
        configured = self.selector.load()
        with self._lock:
            if self._transition_pending:
                raise EnvironmentSwitchError("已有环境切换正在进行；执行保持锁定")
            if target_profile.name == self.active_profile.name:
                raise EnvironmentSwitchError("目标环境必须与当前进程环境不同")
            if (
                not configured.valid
                or configured.profile.name != self.active_profile.name
            ):
                raise EnvironmentSwitchError("环境选择状态尚未稳定；执行保持锁定")
            self._transition_pending = True
            self._transition_target = target_profile.name
        return self.status()

    def assert_execution_allowed(self) -> None:
        with self._lock:
            if self._transition_pending:
                raise EnvironmentSwitchError("环境切换已进入最终核对；交易执行保持锁定")
        status = self.status()
        if not status["selectorValid"]:
            raise EnvironmentSwitchError("环境选择状态无效；交易执行保持锁定")
        if status["restartRequired"]:
            raise EnvironmentSwitchError("环境切换已确认；必须重启进程后才能执行交易")


@dataclass
class SwitchChallenge:
    nonce_sha256: str
    source: EnvironmentName
    target: EnvironmentName
    preflight_sha256: str
    issued_at: datetime
    ready_at: datetime
    expires_at: datetime
    consumed: bool = False


class SwitchChallengeStore:
    def __init__(self, now: Callable[[], datetime] = _utc_now):
        self._now = now
        self._lock = threading.RLock()
        self._challenges: dict[str, SwitchChallenge] = {}

    @staticmethod
    def _nonce_hash(nonce: str) -> str:
        return hashlib.sha256(nonce.encode("utf-8")).hexdigest()

    def issue(
        self,
        *,
        source: EnvironmentName,
        target: EnvironmentName,
        preflight_sha256: str,
    ) -> tuple[str, SwitchChallenge]:
        now = self._now().astimezone(timezone.utc)
        nonce = secrets.token_urlsafe(32)
        challenge = SwitchChallenge(
            nonce_sha256=self._nonce_hash(nonce),
            source=source,
            target=target,
            preflight_sha256=preflight_sha256,
            issued_at=now,
            ready_at=now + timedelta(seconds=SWITCH_READY_DELAY_SECONDS),
            expires_at=now + timedelta(seconds=SWITCH_CHALLENGE_TTL_SECONDS),
        )
        with self._lock:
            self._challenges[challenge.nonce_sha256] = challenge
        return nonce, challenge

    def consume(
        self,
        nonce: str,
        *,
        source: EnvironmentName,
        target: EnvironmentName,
    ) -> SwitchChallenge:
        digest = self._nonce_hash(nonce)
        now = self._now().astimezone(timezone.utc)
        with self._lock:
            challenge = self._challenges.get(digest)
            if challenge is None or challenge.consumed:
                raise EnvironmentSwitchError("环境切换 challenge 不存在或已使用")
            if challenge.source != source or challenge.target != target:
                challenge.consumed = True
                raise EnvironmentSwitchError("环境切换 challenge 与源/目标环境不匹配")
            if now < challenge.ready_at:
                raise EnvironmentSwitchError("环境切换冷静期尚未结束")
            if now >= challenge.expires_at:
                challenge.consumed = True
                raise EnvironmentSwitchError("环境切换 challenge 已过期")
            challenge.consumed = True
            return challenge


def challenge_public(challenge: SwitchChallenge, nonce: str) -> dict[str, Any]:
    return {
        "nonce": nonce,
        "source": challenge.source,
        "target": challenge.target,
        "confirmationPhrase": SWITCH_PHRASES[challenge.target],
        "issuedAt": _iso(challenge.issued_at),
        "readyAt": _iso(challenge.ready_at),
        "expiresAt": _iso(challenge.expires_at),
        "requiredAcknowledgements": [
            "automationStopped",
            "noOutstandingState",
            "restartRequired",
            "liveFundsAtRisk",
        ],
    }
