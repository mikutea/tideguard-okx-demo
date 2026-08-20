from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ..audit import AuditStore
from ..okx_client import OkxClient
from ..secrets import credentials_configured
from ..service import TradingService
from .execution import (
    AUTO_SESSION_CONFIRMATION,
    AutomationDenied,
    AutomationLedger,
    DemoAutomationPermit,
    DemoAutoExecutor,
    ManualReviewRequired,
    MAX_SESSION_NOTIONAL_USDT,
    MAX_SESSION_ORDERS,
    MAX_SESSION_SECONDS,
    authorize_demo_session,
)
from .pipeline import (
    feature_contract_sha256,
    latest_features,
    parse_completed_candles,
    train_and_register_candidate,
)
from .registry import (
    PROMOTION_CONFIRMATION,
    ModelRegistry,
    PromotionDenied,
    PromotionPolicy,
    RegistryError,
)
from .strategy import DemoStrategyPolicy, MarketSnapshot, build_order_proposal
from .walk_forward import ValidationReport


class MLRuntimeError(RuntimeError):
    pass


class _TradingServicePort:
    def __init__(self, service: TradingService):
        self._service = service

    async def preview(self, draft):
        return await self._service.preview(draft)

    async def commit(self, intent_id: str, digest: str, idempotency_key: str):
        return await self._service.commit(intent_id, digest, idempotency_key)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _decimal(value: Any, name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise MLRuntimeError(f"market {name} is invalid") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise MLRuntimeError(f"market {name} is invalid")
    return parsed


class MLCoordinator:
    """Owns offline training and explicit, short-lived OKX Demo automation sessions."""

    def __init__(
        self,
        *,
        data_dir: Path,
        client: OkxClient,
        service: TradingService,
        store: AuditStore,
    ):
        self.client = client
        self.service = service
        self.store = store
        self.registry = ModelRegistry(data_dir / "ml-registry.sqlite3")
        self.ledger = AutomationLedger(data_dir / "ml-automation.sqlite3")
        self.promotion_policy = PromotionPolicy()
        self.strategy_policy = DemoStrategyPolicy()
        self.executor = DemoAutoExecutor(self.ledger)
        self.port = _TradingServicePort(service)
        self._training_lock = asyncio.Lock()
        self._permit_lock = asyncio.Lock()
        self._active_permit: DemoAutomationPermit | None = None
        self._last_candle_closed_at: datetime | None = None
        self._last_decision: dict[str, Any] | None = None
        self._closed = False

    def _models(self) -> list[dict[str, Any]]:
        models = self.registry.list_models()
        for model in models:
            run_id = model.get("validationRunId")
            validation = self.registry.get_validation(str(run_id)) if run_id else None
            if validation:
                report = ValidationReport.from_dict(validation["report"])
                model["metrics"] = {
                    "folds": len(report.folds),
                    "oosRows": report.oos_rows,
                    "trades": report.trades,
                    "accuracy": report.aggregate_accuracy,
                    "netReturn": report.aggregate_net_return,
                    "maxDrawdown": report.max_drawdown,
                    "worstFoldNetReturn": report.worst_fold_net_return,
                    "roundTripCostBps": report.round_trip_cost_bps,
                    "evaluationMode": report.evaluation_mode,
                }
                model["gateFailures"] = list(self.promotion_policy.failures(report))
            else:
                model["metrics"] = None
                model["gateFailures"] = ["validation_missing"]
        return models

    def status(self) -> dict[str, Any]:
        permit = self._active_permit
        permit_status = self.ledger.permit_status(permit.permit_id) if permit else None
        now = _utc_now()
        if permit_status:
            permit_status["active"] = (
                permit.expires_at > now and permit_status.get("revokedAt") is None
            )
            permit_status["remainingSeconds"] = max(
                0, int((permit.expires_at - now).total_seconds())
            )
        return {
            "engine": {
                "name": "Tideguard Native Linear Logit",
                "artifactFormat": "canonical-json-data-only",
                "featureContractSha256": feature_contract_sha256(),
                "trainingMode": "offline-on-demand",
                "profitGuarantee": False,
            },
            "training": {"running": self._training_lock.locked(), "publicDataOnly": True},
            "models": self._models(),
            "champion": self.registry.champion_summary(),
            "generation": self.registry.get_generation(),
            "promotionPolicy": {
                **self.promotion_policy.to_dict(),
                "policySha256": self.promotion_policy.policy_sha256,
                "confirmation": PROMOTION_CONFIRMATION,
            },
            "automation": {
                "permit": permit_status,
                "confirmation": AUTO_SESSION_CONFIRMATION,
                "demoOnly": True,
                "instrument": "BTC-USDT",
                "maxSessionSeconds": MAX_SESSION_SECONDS,
                "maxSessionOrders": MAX_SESSION_ORDERS,
                "maxSessionNotionalUsdt": str(MAX_SESSION_NOTIONAL_USDT),
                "entryOnly": True,
                "automaticExit": False,
                "manualReviews": self.ledger.pending_manual_review(),
                "recentExecutions": self.ledger.recent_executions(20),
                "lastDecision": self._last_decision,
            },
            "freqai": {
                "bundled": False,
                "mode": "optional-localhost-signal-adapter",
                "directOkxDemoExecution": False,
                "pinnedReferenceVersion": "2026.7",
            },
        }

    async def train_candidate(self, candle_limit: int) -> dict[str, Any]:
        if not 1_600 <= candle_limit <= 5_000:
            raise MLRuntimeError("training requires 1600-5000 completed 5m candles")
        if self._training_lock.locked():
            raise MLRuntimeError("a model training run is already active")
        async with self._training_lock:
            raw = await self.client.get_history_candles(limit=candle_limit)
            if len(raw) < candle_limit:
                raise MLRuntimeError("OKX did not return the requested completed candle history")
            now = _utc_now()
            revision = os.environ.get("TIDEGUARD_BUILD_REVISION", "tideguard-0.3.0")
            result = await asyncio.to_thread(
                train_and_register_candidate,
                raw,
                self.registry,
                now=now,
                code_revision=revision,
                promotion_policy=self.promotion_policy,
            )
            self.store.append(
                "ml.candidate_trained",
                {
                    "modelId": result.model_id,
                    "artifactSha256": result.artifact_sha256,
                    "validationRunId": result.validation_run_id,
                    "candleRows": result.candle_rows,
                    "observationRows": result.observation_rows,
                    "gateFailures": list(result.gate_failures),
                },
                actor="user",
            )
            return result.to_dict()

    async def promote(
        self,
        model_id: str,
        *,
        reviewer: str,
        rationale: str,
        confirmation: str,
        expected_generation: int,
    ) -> dict[str, Any]:
        async with self._permit_lock:
            if self._active_permit is not None:
                raise PromotionDenied("stop the active automation permit before promoting a model")
            champion = self.registry.promote(
                model_id,
                policy=self.promotion_policy,
                reviewer=reviewer,
                rationale=rationale,
                confirmation=confirmation,
                expected_generation=expected_generation,
                approved_at=_utc_now(),
            )
        self.store.append(
            "ml.champion_promoted",
            {
                "modelId": champion.model_id,
                "artifactSha256": champion.artifact_sha256,
                "generation": champion.generation,
                "policySha256": self.promotion_policy.policy_sha256,
            },
            actor=reviewer.strip(),
        )
        return self.registry.champion_summary() or {}

    async def authorize(
        self,
        *,
        issued_by: str,
        confirmation: str,
        ttl_seconds: int,
        max_orders: int,
        max_total_notional_usdt: Decimal,
    ) -> dict[str, Any]:
        async with self._permit_lock:
            if self.service.safety.status()["mode"] != "armed":
                raise AutomationDenied("arm the OKX Demo safety session before enabling automation")
            if not credentials_configured():
                raise AutomationDenied("OKX Demo credentials are not configured")
            if not self.store.verify_chain():
                raise AutomationDenied("audit chain validation failed")
            champion = self.registry.load_champion()
            if champion is None:
                raise AutomationDenied("no manually promoted champion is available")
            now = _utc_now()
            permit = authorize_demo_session(
                champion,
                self.strategy_policy,
                issued_by=issued_by,
                confirmation=confirmation,
                issued_at=now,
                ttl_seconds=ttl_seconds,
                max_orders=max_orders,
                max_total_notional_usdt=max_total_notional_usdt,
            )
            if self._active_permit and self._active_permit.expires_at > now:
                raise AutomationDenied("another demo automation permit is already active")
            try:
                self.ledger.register_permit(permit, now=now)
                self.store.append(
                    "ml.automation_authorized",
                    {
                        "permitId": permit.permit_id,
                        "modelId": permit.model_id,
                        "generation": permit.champion_generation,
                        "expiresAt": permit.expires_at.isoformat(),
                        "maxOrders": permit.max_orders,
                        "maxTotalNotionalUsdt": str(permit.max_total_notional_usdt),
                    },
                    actor=issued_by.strip(),
                )
                permit_response = self.ledger.permit_status(permit.permit_id)
                if permit_response is None:
                    raise AutomationDenied("authorized permit was not persisted")
            except BaseException:
                try:
                    self.ledger.revoke_permit(permit.permit_id, now=_utc_now())
                except Exception:
                    pass
                raise
            self._active_permit = permit
            self._last_candle_closed_at = None
            self._last_decision = {
                "at": now.isoformat(),
                "status": "authorized_waiting_for_completed_candle",
            }
        return permit_response

    async def stop_automation(
        self, *, reason: str = "用户停止模型自动执行", actor: str = "user", emergency: bool = True
    ) -> dict[str, Any]:
        async with self._permit_lock:
            permit = self._active_permit
            self._active_permit = None
            self._last_candle_closed_at = None
        self._last_decision = {"at": _utc_now().isoformat(), "status": "stopped", "reason": reason}
        result: dict[str, Any] = {
            "stopped": True,
            "permitId": permit.permit_id if permit else None,
        }
        persistence_failures = 0
        try:
            if emergency:
                result = await self.service.emergency_stop(reason, actor=actor)
        finally:
            if permit:
                try:
                    self.ledger.revoke_permit(permit.permit_id, now=_utc_now())
                except Exception:
                    persistence_failures += 1
            try:
                self.store.append(
                    "ml.automation_stopped",
                    {"permitId": permit.permit_id if permit else None, "reason": reason},
                    actor=actor,
                )
            except Exception:
                persistence_failures += 1
        if persistence_failures:
            result = {**result, "localPersistenceFailures": persistence_failures}
        return result

    @staticmethod
    def _snapshot(bundle: dict[str, Any], *, now: datetime) -> tuple[dict[str, float], MarketSnapshot]:
        confirmed = [row for row in reversed(bundle["candles"]) if str(row[8]) == "1"]
        candles = parse_completed_candles(confirmed, now=now)
        features, candle_closed_at = latest_features(candles)
        ticker = bundle["ticker"]
        instrument = bundle["instrument"]
        timestamp_text = str(ticker.get("ts", ""))
        if not timestamp_text.isdigit():
            raise MLRuntimeError("ticker timestamp is invalid")
        observed_at = datetime.fromtimestamp(int(timestamp_text) / 1_000, tz=timezone.utc)
        snapshot = MarketSnapshot(
            observed_at=observed_at,
            candle_closed_at=candle_closed_at,
            candle_confirmed=True,
            instrument="BTC-USDT",
            bid=_decimal(ticker.get("bidPx"), "bid"),
            ask=_decimal(ticker.get("askPx"), "ask"),
            tick_size=_decimal(instrument.get("tickSz"), "tick size"),
            lot_size=_decimal(instrument.get("lotSz"), "lot size"),
            min_size=_decimal(instrument.get("minSz"), "minimum size"),
        )
        return features, snapshot

    async def tick(self) -> None:
        async with self._permit_lock:
            permit = self._active_permit
        if permit is None:
            return
        now = _utc_now()
        if now >= permit.expires_at:
            await self.stop_automation(reason="模型自动会话已到期", actor="system")
            return
        if self.service.safety.status()["mode"] != "armed":
            await self.stop_automation(reason="确定性风控已离开演练状态", actor="system")
            return
        champion = self.registry.load_champion()
        if champion is None or (
            champion.model_id,
            champion.artifact_sha256,
            champion.generation,
        ) != (permit.model_id, permit.artifact_sha256, permit.champion_generation):
            await self.stop_automation(reason="champion 已变化或不可用", actor="system")
            return
        market_bundle = await self.client.get_market_bundle("BTC-USDT")
        now = _utc_now()
        features, market = self._snapshot(market_bundle, now=now)
        if self._last_candle_closed_at == market.candle_closed_at:
            return
        self._last_candle_closed_at = market.candle_closed_at
        proposal = build_order_proposal(
            champion.bundle,
            features=features,
            market=market,
            policy=self.strategy_policy,
            now=now,
        )
        if proposal is None:
            action, score = champion.bundle.model.action(features)
            self._last_decision = {
                "at": now.isoformat(),
                "candleClosedAt": market.candle_closed_at.isoformat(),
                "status": action,
                "score": score,
                "modelId": champion.model_id,
            }
            return
        async with self._permit_lock:
            if self._active_permit is not permit:
                raise AutomationDenied("automation permit changed before execution")
            current_champion = self.registry.load_champion()
            if current_champion is None or (
                current_champion.model_id,
                current_champion.artifact_sha256,
                current_champion.generation,
            ) != (permit.model_id, permit.artifact_sha256, permit.champion_generation):
                raise AutomationDenied("champion changed before execution")
        result = await self.executor.execute(
            proposal, permit, champion, self.port, now=now
        )
        self._last_decision = {
            "at": _utc_now().isoformat(),
            "candleClosedAt": market.candle_closed_at.isoformat(),
            "status": result.status,
            "signalId": result.signal_id,
            "intentId": result.intent_id,
            "ordId": result.ord_id,
            "modelId": champion.model_id,
            "side": proposal.side,
            "score": proposal.score,
        }

    async def run(self) -> None:
        while not self._closed:
            await asyncio.sleep(5)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except ManualReviewRequired as exc:
                try:
                    await self.stop_automation(
                        reason="模型提交结果需要人工核对", actor="system"
                    )
                except Exception:
                    pass
                self._last_decision = {
                    "at": _utc_now().isoformat(),
                    "status": "manual_review",
                    "errorType": type(exc).__name__,
                }
            except Exception as exc:
                try:
                    await self.stop_automation(
                        reason="模型执行器发生故障并已停止", actor="system"
                    )
                except Exception:
                    pass
                self._last_decision = {
                    "at": _utc_now().isoformat(),
                    "status": "failed_closed",
                    "errorType": type(exc).__name__,
                }

    async def close(self) -> None:
        self._closed = True
        if self._active_permit is not None:
            try:
                await self.stop_automation(
                    reason="应用关闭，模型自动会话失效", actor="system", emergency=False
                )
            except Exception:
                self._active_permit = None
