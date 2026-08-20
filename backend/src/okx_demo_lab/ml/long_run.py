from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any, Callable

from ..audit import AuditStore
from ..models import OrderDraft
from ..okx_client import OkxApiError, OkxClient, OkxClientError
from ..service import (
    LIVE_OKX_ORDER_STATES,
    TERMINAL_OKX_ORDER_STATES,
    CommitBlockedBeforeDispatch,
    TradingService,
)
from .autonomy import AutonomyError, AutonomyPolicy, AutonomyStore, PositionStateError
from .pipeline import (
    BAR_MILLISECONDS,
    DEFAULT_LABEL_HORIZON,
    ParsedCandle,
    latest_features,
    parse_completed_candles,
    train_and_register_candidate,
)
from .registry import ModelRegistry, PromotionPolicy, RegistryError
from .strategy import canonical_json, sha256_hex
from .supervisor import CodexSupervisor
from .walk_forward import TrainingConfig, WalkForwardSpec


LONG_RUN_LOOP_SECONDS = 5
MODEL_HISTORY_CANDLES = 10_000
SHADOW_RECOVERY_CANDLES = 2_000
LONG_RUN_PROMOTION_POLICY = PromotionPolicy(
    min_folds=5,
    min_oos_rows=1_000,
    min_trades=20,
    min_round_trip_cost_bps=24.0,
    min_aggregate_accuracy=0.52,
    min_aggregate_net_return=0.005,
    min_worst_fold_net_return=-0.03,
    max_drawdown=0.10,
)
LONG_RUN_WALK_FORWARD_SPEC = WalkForwardSpec(
    train_size=5_000,
    test_size=500,
    step_size=500,
    label_horizon=DEFAULT_LABEL_HORIZON,
    embargo_size=1,
    expanding=True,
)


def _long_run_training_configs(policy: AutonomyPolicy) -> tuple[TrainingConfig, ...]:
    common = {
        "round_trip_cost_bps": float(policy.round_trip_cost_bps),
        "stop_loss_fraction": float(policy.stop_loss_fraction),
        "take_profit_fraction": float(policy.take_profit_fraction),
    }
    return (
        TrainingConfig(
            learning_rate=0.05,
            epochs=60,
            l2=0.001,
            buy_threshold=0.52,
            sell_threshold=0.40,
            **common,
        ),
        TrainingConfig(
            learning_rate=0.05,
            epochs=80,
            l2=0.01,
            buy_threshold=0.56,
            sell_threshold=0.40,
            **common,
        ),
        TrainingConfig(
            learning_rate=0.03,
            epochs=100,
            l2=0.05,
            buy_threshold=0.60,
            sell_threshold=0.38,
            **common,
        ),
    )


def _train_candidate_family(
    raw: list[list[Any]],
    registry: ModelRegistry,
    *,
    now: datetime,
    promotion_policy: PromotionPolicy,
    autonomy_policy: AutonomyPolicy,
) -> list[Any]:
    return [
        train_and_register_candidate(
            raw,
            registry,
            now=now,
            code_revision="tideguard-v0.3-long-run",
            promotion_policy=promotion_policy,
            training_config=config,
            walk_forward_spec=LONG_RUN_WALK_FORWARD_SPEC,
        )
        for config in _long_run_training_configs(autonomy_policy)
    ]


class LongRunError(RuntimeError):
    pass


class PreviewRejected(LongRunError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LongRunError("persisted timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LongRunError("persisted timestamp lacks a timezone")
    return parsed.astimezone(timezone.utc)


def _decimal(value: Any, name: str, *, allow_zero: bool = False) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise LongRunError(f"{name} is invalid") from exc
    if not parsed.is_finite() or parsed < 0 or (parsed == 0 and not allow_zero):
        raise LongRunError(f"{name} is invalid")
    return parsed


def _signed_decimal(value: Any, name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise LongRunError(f"{name} is invalid") from exc
    if not parsed.is_finite():
        raise LongRunError(f"{name} is invalid")
    return parsed


def _align_price(value: Decimal, tick: Decimal, *, round_up: bool) -> Decimal:
    if tick <= 0:
        raise LongRunError("instrument tick size is invalid")
    rounding = ROUND_CEILING if round_up else ROUND_FLOOR
    return (value / tick).to_integral_value(rounding=rounding) * tick


def _align_size(value: Decimal, lot: Decimal) -> Decimal:
    if lot <= 0:
        raise LongRunError("instrument lot size is invalid")
    return (value / lot).to_integral_value(rounding=ROUND_FLOOR) * lot


class LongRunCoordinator:
    """Persistent public research and closed-loop Demo execution coordinator."""

    def __init__(
        self,
        *,
        client: OkxClient,
        service: TradingService,
        audit: AuditStore,
        registry: ModelRegistry,
        autonomy: AutonomyStore,
        promotion_policy: PromotionPolicy | None = None,
        policy: AutonomyPolicy | None = None,
    ) -> None:
        self.client = client
        self.service = service
        self.audit = audit
        self.registry = registry
        self.autonomy = autonomy
        self.promotion_policy = promotion_policy or LONG_RUN_PROMOTION_POLICY
        self.policy = policy or AutonomyPolicy()
        self.supervisor = CodexSupervisor(
            registry=registry,
            autonomy=autonomy,
            audit=audit,
            promotion_policy=self.promotion_policy,
            autonomy_policy=self.policy,
        )
        self._cycle_lock = asyncio.Lock()
        self._closed = False
        self._last_error: dict[str, Any] | None = None
        self._sleep = asyncio.sleep
        self._now = _utc_now

    def status(self) -> dict[str, Any]:
        summary = self.autonomy.summary()
        champion = self.registry.champion_summary()
        lease = None
        if champion:
            lease = self.autonomy.active_lease(
                model_id=str(champion["modelId"]),
                artifact_sha256=str(champion["artifactSha256"]),
                generation=int(champion["generation"]),
                policy_sha256=self.policy.policy_sha256,
                now=self._now(),
            )
        return {
            **summary,
            "policy": {**self.policy.to_dict(), "policySha256": self.policy.policy_sha256},
            "champion": champion,
            "activeSupervisorLease": lease,
            "review": self.supervisor.review_pack(now=self._now()),
            "lastError": self._last_error,
        }

    async def _finish_training_after_cancellation(
        self,
        task: asyncio.Task[Any],
        run_id: str,
    ) -> None:
        try:
            results = await task
        except BaseException as exc:
            self.autonomy.finish_training(
                run_id,
                model_id=None,
                result=None,
                error_type=type(exc).__name__,
                now=self._now(),
            )
            raise
        payload = {"candidates": [result.to_dict() for result in results]}
        self.autonomy.finish_training(
            run_id,
            model_id=results[0].model_id,
            result=payload,
            error_type=None,
            now=self._now(),
        )

    async def train_if_due(
        self, *, now: datetime, force: bool = False
    ) -> dict[str, Any] | None:
        if not force and not self.autonomy.training_due(
            now=now,
            interval_hours=self.policy.train_interval_hours,
            retry_hours=self.policy.training_retry_hours,
        ):
            return None
        run_id = self.autonomy.start_training(now=now)
        try:
            raw = await self.client.get_history_candles(limit=MODEL_HISTORY_CANDLES)
            if len(raw) < MODEL_HISTORY_CANDLES:
                raise LongRunError("public history did not return the requested training window")
            task = asyncio.create_task(
                asyncio.to_thread(
                    _train_candidate_family,
                    raw,
                    self.registry,
                    now=now,
                    promotion_policy=self.promotion_policy,
                    autonomy_policy=self.policy,
                )
            )
            try:
                results = await asyncio.shield(task)
            except asyncio.CancelledError:
                await self._finish_training_after_cancellation(task, run_id)
                raise
            payload = {"candidates": [result.to_dict() for result in results]}
            self.autonomy.finish_training(
                run_id,
                model_id=results[0].model_id,
                result=payload,
                error_type=None,
                now=self._now(),
            )
            self.audit.append(
                "ml.scheduled_candidate_trained",
                {
                    "candidateCount": len(results),
                    "modelIds": [result.model_id for result in results],
                    "runId": run_id,
                    "validationRunIds": [result.validation_run_id for result in results],
                },
                actor="system",
            )
            return payload
        except asyncio.CancelledError:
            raise

        except BaseException as exc:
            try:
                self.autonomy.finish_training(
                    run_id,
                    model_id=None,
                    result=None,
                    error_type=type(exc).__name__,
                    now=self._now(),
                )
            except Exception:
                pass
            try:
                self.audit.append(
                    "ml.scheduled_training_failed",
                    {"errorType": type(exc).__name__, "runId": run_id},
                    actor="system",
                )
            except Exception:
                pass
            raise

    async def train_now(self) -> dict[str, Any]:
        async with self._cycle_lock:
            if self.autonomy.active_position() is not None:
                raise LongRunError("cannot train while an automatic position needs monitoring")
            result = await self.train_if_due(now=self._now(), force=True)
        if result is None:
            raise LongRunError("forced training did not produce a candidate")
        return result

    @staticmethod
    def _completed_candles(bundle: dict[str, Any], *, now: datetime) -> tuple[ParsedCandle, ...]:
        rows = [
            row
            for row in reversed(bundle["candles"])
            if isinstance(row, (list, tuple)) and len(row) == 9 and str(row[8]) == "1"
        ]
        parsed = parse_completed_candles(rows, now=now)
        age_seconds = (now - parsed[-1].closed_at).total_seconds()
        if age_seconds < -2 or age_seconds > 360:
            raise LongRunError("latest completed candle is stale or from the future")
        return parsed

    async def _settle_shadow(
        self,
        candles: tuple[ParsedCandle, ...],
        *,
        now: datetime,
    ) -> None:
        latest_close = candles[-1].closed_at
        pending = self.autonomy.unsettled_shadow(due_at_or_before=latest_close)
        if not pending:
            return
        candle_by_time = {row.closed_at: row for row in candles}
        missing = [
            row
            for row in pending
            if _parse_iso(str(row["due_at"])) not in candle_by_time
            or _parse_iso(str(row["candle_closed_at"])) not in candle_by_time
        ]
        if missing:
            history = await self.client.get_history_candles(limit=SHADOW_RECOVERY_CANDLES)
            recovered = parse_completed_candles(history, now=now)
            candle_by_time.update({row.closed_at: row for row in recovered})
        for row in pending:
            entry_at = _parse_iso(str(row["candle_closed_at"]))
            due_at = _parse_iso(str(row["due_at"]))
            path = [
                candle_by_time[entry_at + timedelta(milliseconds=BAR_MILLISECONDS * offset)]
                for offset in range(1, self.policy.hold_bars + 1)
                if entry_at + timedelta(milliseconds=BAR_MILLISECONDS * offset)
                in candle_by_time
            ]
            if len(path) != self.policy.hold_bars or path[-1].closed_at != due_at:
                raise LongRunError("a due shadow signal has an incomplete holding path")
            entry_close = _decimal(row["entry_close"], "shadow entry close")
            stop_price = entry_close * (Decimal("1") - self.policy.stop_loss_fraction)
            take_price = entry_close * (Decimal("1") + self.policy.take_profit_fraction)
            exit_close: Decimal | None = None
            for candle in path:
                if Decimal(str(candle.low)) <= stop_price:
                    exit_close = stop_price
                    break
                if Decimal(str(candle.high)) >= take_price:
                    exit_close = take_price
                    break
            if exit_close is None:
                exit_close = Decimal(str(path[-1].close))
            self.autonomy.settle_shadow(
                str(row["signal_id"]),
                exit_close=exit_close,
                round_trip_cost_bps=float(self.policy.round_trip_cost_bps),
                now=now,
            )

    def _record_shadow_signals(
        self,
        candles: tuple[ParsedCandle, ...],
        *,
        now: datetime,
    ) -> None:
        features, candle_closed_at = latest_features(candles)
        entry_close = Decimal(str(candles[-1].close))
        due_at = candle_closed_at + timedelta(
            milliseconds=BAR_MILLISECONDS * DEFAULT_LABEL_HORIZON
        )
        for metadata in self.registry.list_models(limit=100):
            if metadata["state"] not in {"validated", "champion", "retired"}:
                continue
            model_id = str(metadata["modelId"])
            if self.autonomy.model_has_open_shadow_buy(model_id):
                continue
            bundle = self.registry.load_model(model_id)
            action, score = bundle.model.action(features)
            if action != "buy":
                continue
            self.autonomy.record_shadow_signal(
                model_id=model_id,
                artifact_sha256=bundle.artifact_sha256,
                candle_closed_at=candle_closed_at,
                due_at=due_at,
                action="buy",
                score=score,
                entry_close=entry_close,
            )

    def _market_values(self, bundle: dict[str, Any], *, now: datetime) -> dict[str, Decimal]:
        ticker = bundle["ticker"]
        instrument = bundle["instrument"]
        timestamp_text = str(ticker.get("ts") or "")
        if not timestamp_text.isdigit():
            raise LongRunError("ticker timestamp is invalid")
        observed = datetime.fromtimestamp(int(timestamp_text) / 1_000, tz=timezone.utc)
        age = (now - observed).total_seconds()
        if age < 0 or age > 8:
            raise LongRunError("ticker is stale or from the future")
        return {
            "last": _decimal(ticker.get("last"), "last"),
            "bid": _decimal(ticker.get("bidPx"), "bid"),
            "ask": _decimal(ticker.get("askPx"), "ask"),
            "tick": _decimal(instrument.get("tickSz"), "tick"),
            "lot": _decimal(instrument.get("lotSz"), "lot"),
            "minimum": _decimal(instrument.get("minSz"), "minimum"),
        }

    async def _preview_order(
        self,
        draft: OrderDraft,
        *,
        supervisor_decision_id: str,
        supervisor_purpose: str,
    ) -> dict[str, Any]:
        preview = await self.service.preview(
            draft,
            supervisor_decision_id=supervisor_decision_id,
            supervisor_purpose=supervisor_purpose,
        )
        decision = preview.get("decision")
        if not isinstance(decision, dict) or decision.get("allowed") is not True:
            reasons = decision.get("reasonCodes") if isinstance(decision, dict) else []
            raise PreviewRejected("autonomy preview rejected: " + ",".join(reasons or []))
        intent_id = str(preview.get("intentId") or "")
        digest = str(preview.get("digest") or "")
        if not intent_id or len(digest) != 64:
            raise LongRunError("autonomy preview has no valid intent identity")
        intent = self.audit.get_intent(intent_id)
        if intent is None or not intent.get("cl_ord_id"):
            raise LongRunError("autonomy preview is absent from the intent ledger")
        return {
            "intentId": intent_id,
            "digest": digest,
            "clOrdId": str(intent["cl_ord_id"]),
        }

    async def _commit_order(
        self,
        prepared: dict[str, Any],
        *,
        idempotency_key: str,
        dispatch_guard: Callable[[], None],
    ) -> dict[str, Any]:
        result = await self.service.commit(
            str(prepared["intentId"]),
            str(prepared["digest"]),
            idempotency_key,
            additional_dispatch_guard=dispatch_guard,
        )
        if result.get("status") not in {"accepted", "reconciled"} or not result.get("ordId"):
            raise LongRunError("autonomy commit did not return an accepted order identity")
        return {
            **prepared,
            "ordId": str(result["ordId"]),
            "status": str(result["status"]),
        }

    def _entry_dispatch_guard(
        self,
        *,
        position_id: str,
        intent_id: str,
        champion: Any,
        lease: dict[str, Any],
    ) -> None:
        state = self.autonomy.state()
        if state["desiredMode"] != "demo":
            raise LongRunError("long-run Demo master was disabled before dispatch")
        position = self.autonomy.active_position()
        if (
            position is None
            or position["positionId"] != position_id
            or position["status"] != "entry_submitted"
            or position["entryIntentId"] != intent_id
            or position["modelId"] != champion.model_id
            or position["artifactSha256"] != champion.artifact_sha256
            or int(position["championGeneration"]) != champion.generation
            or position["policySha256"] != self.policy.policy_sha256
            or position["supervisorDecisionId"] != lease["decisionId"]
        ):
            raise LongRunError("entry position identity changed before dispatch")
        current = self.registry.load_champion()
        if (
            current is None
            or current.model_id != champion.model_id
            or current.artifact_sha256 != champion.artifact_sha256
            or current.generation != champion.generation
        ):
            raise LongRunError("champion changed before entry dispatch")
        active_lease = self.autonomy.active_lease(
            model_id=champion.model_id,
            artifact_sha256=champion.artifact_sha256,
            generation=champion.generation,
            policy_sha256=self.policy.policy_sha256,
            now=self._now(),
        )
        if active_lease is None or active_lease["decisionId"] != lease["decisionId"]:
            raise LongRunError("Codex execution lease expired before dispatch")
        if float(self.autonomy.demo_performance()["maxDrawdown"]) > float(
            self.policy.max_demo_drawdown
        ):
            raise LongRunError("Demo drawdown gate opened before dispatch")

    def _exit_dispatch_guard(self, *, position_id: str, intent_id: str) -> None:
        position = self.autonomy.active_position()
        if (
            position is None
            or position["positionId"] != position_id
            or position["status"] != "exit_submitted"
            or position["exitIntentId"] != intent_id
            or _decimal(position["remainingSize"], "remainingSize") <= 0
        ):
            raise LongRunError("exit position identity changed before dispatch")

    async def _inspect_and_apply(self, position: dict[str, Any], *, now: datetime) -> None:
        binding = self.autonomy.bound_identity()
        if binding is None:
            raise LongRunError("active model position has no bound Demo identity")
        position_id = str(position["positionId"])
        status = str(position["status"])
        intent_id = (
            str(position.get("entryIntentId") or "")
            if status == "entry_submitted"
            else str(position.get("exitIntentId") or "")
        )
        if not intent_id:
            raise LongRunError("submitted model position has no order intent")
        order = await self.service.inspect_intent_order(intent_id, binding)
        order_state = str(order["state"])
        if order_state in LIVE_OKX_ORDER_STATES:
            return
        if order_state not in TERMINAL_OKX_ORDER_STATES:
            raise LongRunError("model order is not in a supported state")
        filled = _decimal(order["filledSize"], "filledSize", allow_zero=True)
        average = (
            _decimal(order["averagePrice"], "averagePrice")
            if filled > 0
            else None
        )
        fee = _signed_decimal(order.get("fee", "0"), "fee")
        fee_currency = str(order.get("feeCurrency") or "")
        if status == "entry_submitted":
            updated = self.autonomy.resolve_entry(
                position_id,
                filled_size=filled,
                average_price=average,
                terminal_state=order_state,
                policy=self.policy,
                fee=fee,
                fee_currency=fee_currency,
                now=now,
            )
            self.audit.append(
                "autonomy.entry_resolved",
                {
                    "filledSize": updated["filledSize"],
                    "positionId": position_id,
                    "remainingSize": updated["remainingSize"],
                    "status": updated["status"],
                },
                actor="system",
                correlation_id=position_id,
            )
        else:
            updated = self.autonomy.resolve_exit(
                position_id,
                filled_size=filled,
                average_price=average,
                terminal_state=order_state,
                max_exit_attempts=self.policy.max_exit_attempts,
                fee=fee,
                fee_currency=fee_currency,
                now=now,
            )
            self.audit.append(
                "autonomy.exit_resolved",
                {
                    "positionId": position_id,
                    "remainingSize": updated["remainingSize"],
                    "status": updated["status"],
                },
                actor="system",
                correlation_id=position_id,
            )
            if updated["status"] == "manual_review":
                await self.service.emergency_stop(
                    "自动退出尝试耗尽，需人工核对", actor="system"
                )

    async def _enter(
        self,
        *,
        champion: Any,
        lease: dict[str, Any],
        binding: tuple[str, str],
        candles: tuple[ParsedCandle, ...],
        market: dict[str, Decimal],
        now: datetime,
    ) -> None:
        features, candle_closed_at = latest_features(candles)
        action, score = champion.bundle.model.action(features)
        if action != "buy":
            return
        buy_price = _align_price(
            market["ask"] * (Decimal("1") + self.policy.ioc_slippage_fraction),
            market["tick"],
            round_up=True,
        )
        size = _align_size(
            self.policy.fixed_notional_usdt / buy_price,
            market["lot"],
        )
        if size < market["minimum"] or size <= 0:
            raise LongRunError("fixed autonomy notional is below the instrument minimum")
        evidence = canonical_json(
            {
                "artifact_sha256": champion.artifact_sha256,
                "candle_closed_at": candle_closed_at.isoformat(),
                "features": features,
                "generation": champion.generation,
                "policy_sha256": self.policy.policy_sha256,
                "score": score,
            }
        )
        signal_id = f"auto_{sha256_hex(evidence)[:28]}"
        if self.autonomy.position_for_signal(signal_id) is not None:
            return
        self.autonomy.claim_daily_entry(now=now, maximum=self.policy.max_daily_entries)
        position_id = self.autonomy.create_entry_position(
            model_id=champion.model_id,
            artifact_sha256=champion.artifact_sha256,
            champion_generation=champion.generation,
            policy_sha256=self.policy.policy_sha256,
            credential_fingerprint=binding[0],
            account_fingerprint=binding[1],
            entry_signal_id=signal_id,
            supervisor_decision_id=str(lease["decisionId"]),
            requested_size=size,
            entry_candle_at=candle_closed_at,
            exit_due_at=candle_closed_at
            + timedelta(milliseconds=BAR_MILLISECONDS * self.policy.hold_bars),
            hard_exit_at=candle_closed_at
            + timedelta(milliseconds=BAR_MILLISECONDS * self.policy.max_holding_bars),
            now=now,
        )
        dispatch_started = False
        try:
            await self.service.arm_supervised(
                str(lease["decisionId"]), binding, purpose="entry"
            )
            draft = OrderDraft(
                instId=self.policy.instrument,
                side="buy",
                ordType="ioc",
                price=buy_price,
                size=size,
            )
            prepared = await self._preview_order(
                draft,
                supervisor_decision_id=str(lease["decisionId"]),
                supervisor_purpose="entry",
            )
            self.autonomy.attach_entry_intent(
                position_id,
                intent_id=prepared["intentId"],
                cl_ord_id=prepared["clOrdId"],
                now=self._now(),
            )
            dispatch_started = True
            result = await self._commit_order(
                prepared,
                idempotency_key=f"tg-auto-entry-{sha256_hex(position_id)[:20]}",
                dispatch_guard=lambda: self._entry_dispatch_guard(
                    position_id=position_id,
                    intent_id=str(prepared["intentId"]),
                    champion=champion,
                    lease=lease,
                ),
            )
            self.autonomy.confirm_entry_order(
                position_id,
                ord_id=result["ordId"],
                now=self._now(),
            )
            self.autonomy.mark_decision_applied(str(lease["decisionId"]), now=now)
            self.audit.append(
                "autonomy.entry_submitted",
                {
                    "modelId": champion.model_id,
                    "positionId": position_id,
                    "score": score,
                    "size": str(size),
                },
                actor="system",
                correlation_id=position_id,
            )
            position = self.autonomy.get_position(position_id)
            if position:
                await self._inspect_and_apply(position, now=self._now())
        except PreviewRejected:
            self.autonomy.resolve_entry(
                position_id,
                filled_size=Decimal("0"),
                average_price=None,
                terminal_state="canceled",
                policy=self.policy,
                now=self._now(),
            )
            raise
        except CommitBlockedBeforeDispatch as exc:
            self.autonomy.resolve_entry(
                position_id,
                filled_size=Decimal("0"),
                average_price=None,
                terminal_state="canceled",
                policy=self.policy,
                now=self._now(),
            )
            raise LongRunError("autonomy entry was blocked before HTTP dispatch") from exc
        except OkxApiError as exc:
            self.autonomy.resolve_entry(
                position_id,
                filled_size=Decimal("0"),
                average_price=None,
                terminal_state="canceled",
                policy=self.policy,
                now=self._now(),
            )
            raise LongRunError("OKX definitively rejected the autonomy entry") from exc
        except BaseException as exc:
            if dispatch_started:
                try:
                    current = self.autonomy.get_position(position_id)
                    if current and current["status"] == "entry_submitted":
                        self.autonomy.require_manual_review(
                            position_id, type(exc).__name__, now=self._now()
                        )
                except Exception:
                    pass
                try:
                    await self.service.emergency_stop(
                        "自动入场结果不确定", actor="system"
                    )
                except Exception:
                    pass
            else:
                try:
                    current = self.autonomy.get_position(position_id)
                    if current and current["status"] == "entry_submitted":
                        self.autonomy.resolve_entry(
                            position_id,
                            filled_size=Decimal("0"),
                            average_price=None,
                            terminal_state="canceled",
                            policy=self.policy,
                            now=self._now(),
                        )
                except Exception:
                    pass
            raise

    async def _exit(
        self,
        position: dict[str, Any],
        *,
        market: dict[str, Decimal],
        now: datetime,
    ) -> None:
        remaining = _decimal(position["remainingSize"], "remainingSize")
        sell_size = _align_size(remaining, market["lot"])
        if sell_size < market["minimum"] or sell_size <= 0:
            updated = self.autonomy.close_dust(
                str(position["positionId"]),
                dust_size=remaining,
                reason="model-owned remainder is below the exchange minimum",
                now=now,
            )
            self.audit.append(
                "autonomy.position_dust_manual_review",
                {
                    "dustSize": updated["remainingSize"],
                    "positionId": updated["positionId"],
                },
                actor="system",
                correlation_id=str(position["positionId"]),
            )
            await self.service.emergency_stop(
                "模型净库存低于交易所最小卖出量，需核对残余资产",
                actor="system",
            )
            return
        binding = self.autonomy.bound_identity()
        if binding is None:
            raise LongRunError("model exit has no bound Demo identity")
        decision_id = str(position["supervisorDecisionId"])
        await self.service.arm_supervised(decision_id, binding, purpose="exit")
        sell_price = _align_price(
            market["bid"] * (Decimal("1") - self.policy.ioc_slippage_fraction),
            market["tick"],
            round_up=False,
        )
        draft = OrderDraft(
            instId=self.policy.instrument,
            side="sell",
            ordType="ioc",
            price=sell_price,
            size=sell_size,
        )
        attempt = int(position["exitAttempts"]) + 1
        dispatch_started = False
        try:
            prepared = await self._preview_order(
                draft,
                supervisor_decision_id=decision_id,
                supervisor_purpose="exit",
            )
            self.autonomy.begin_exit_dispatch(
                str(position["positionId"]),
                intent_id=prepared["intentId"],
                cl_ord_id=prepared["clOrdId"],
                now=self._now(),
            )
            dispatch_started = True
            result = await self._commit_order(
                prepared,
                idempotency_key=(
                    f"tg-auto-exit-{sha256_hex(str(position['positionId']) + ':' + str(attempt))[:20]}"
                ),
                dispatch_guard=lambda: self._exit_dispatch_guard(
                    position_id=str(position["positionId"]),
                    intent_id=str(prepared["intentId"]),
                ),
            )
            self.autonomy.confirm_exit_order(
                str(position["positionId"]),
                ord_id=result["ordId"],
                now=self._now(),
            )
        except CommitBlockedBeforeDispatch as exc:
            self.autonomy.abandon_exit_before_dispatch(
                str(position["positionId"]), now=self._now()
            )
            raise LongRunError("autonomy exit was blocked before HTTP dispatch") from exc
        except OkxApiError as exc:
            updated = self.autonomy.resolve_exit(
                str(position["positionId"]),
                filled_size=Decimal("0"),
                average_price=None,
                terminal_state="canceled",
                max_exit_attempts=self.policy.max_exit_attempts,
                now=self._now(),
            )
            if updated["status"] == "manual_review":
                await self.service.emergency_stop(
                    "自动退出被交易所连续拒绝，需核对模型净库存",
                    actor="system",
                )
            raise LongRunError("OKX definitively rejected the autonomy exit") from exc
        except PreviewRejected:
            try:
                self.autonomy.require_manual_review(
                    str(position["positionId"]),
                    "exit_preview_rejected",
                    now=self._now(),
                )
            except Exception:
                pass
            try:
                await self.service.emergency_stop(
                    "自动退出预检被拒绝，需人工核对", actor="system"
                )
            except Exception:
                pass
            raise
        except BaseException as exc:
            if dispatch_started:
                try:
                    self.autonomy.require_manual_review(
                        str(position["positionId"]),
                        type(exc).__name__,
                        now=self._now(),
                    )
                except Exception:
                    pass
                try:
                    await self.service.emergency_stop(
                        "自动退出结果不确定", actor="system"
                    )
                except Exception:
                    pass
            raise
        self.audit.append(
            "autonomy.exit_submitted",
            {
                "attempt": attempt,
                "positionId": position["positionId"],
                "size": str(sell_size),
            },
            actor="system",
            correlation_id=str(position["positionId"]),
        )
        current = self.autonomy.get_position(str(position["positionId"]))
        if current:
            await self._inspect_and_apply(current, now=self._now())

    async def _manage_position(
        self,
        position: dict[str, Any],
        *,
        market: dict[str, Decimal],
        now: datetime,
    ) -> None:
        status = str(position["status"])
        if status in {"entry_submitted", "exit_submitted"}:
            await self._inspect_and_apply(position, now=now)
            return
        if status == "manual_review":
            self.autonomy.set_runtime_status(
                "manual_review",
                reason="模型持仓需要人工核对交易所终态",
                now=now,
            )
            return
        if status != "long":
            return
        last = market["last"]
        stop = _decimal(position["stopPrice"], "stopPrice")
        take = _decimal(position["takeProfitPrice"], "takeProfitPrice")
        due = _parse_iso(str(position["exitDueAt"]))
        hard = _parse_iso(str(position["hardExitAt"]))
        if last <= stop or last >= take or now >= due or now >= hard:
            await self._exit(position, market=market, now=now)

    async def tick(self) -> None:
        async with self._cycle_lock:
            now = self._now()
            active_before_research = self.autonomy.active_position()
            if active_before_research is None:
                await self.train_if_due(now=now)
            bundle = await self.client.get_market_bundle(self.policy.instrument)
            # Public calls are intentionally sequential and can take seconds.
            # Freshness must be measured when the bundle has arrived, not from
            # the timestamp captured before network I/O.
            now = self._now()
            candles = self._completed_candles(bundle, now=now)
            market = self._market_values(bundle, now=now)
            await self._settle_shadow(candles, now=now)
            self._record_shadow_signals(candles, now=now)

            performance = self.autonomy.demo_performance()
            if float(performance["maxDrawdown"]) > float(self.policy.max_demo_drawdown):
                self.autonomy.disable_master("Demo 回撤超过硬上限", now=now)

            active = self.autonomy.active_position()
            if active is not None:
                self.autonomy.set_runtime_status(
                    "manual_review" if active["status"] == "manual_review" else "exit_only",
                    reason=(
                        "模型持仓需要人工核对"
                        if active["status"] == "manual_review"
                        else None
                    ),
                    now=now,
                )
                await self._manage_position(active, market=market, now=now)
                return

            state = self.autonomy.state()
            if state["desiredMode"] == "disabled":
                self.autonomy.set_runtime_status("disabled", reason=None, now=now)
                return
            if state["desiredMode"] == "shadow":
                self.autonomy.set_runtime_status("shadow", reason=None, now=now)
                return
            champion = self.registry.load_champion()
            if champion is None:
                self.autonomy.set_runtime_status(
                    "waiting_champion", reason="尚无通过 Codex 审查的 champion", now=now
                )
                return
            if self.autonomy.applied_champion_decision(
                model_id=champion.model_id,
                artifact_sha256=champion.artifact_sha256,
                generation=champion.generation,
            ) is None:
                self.autonomy.set_runtime_status(
                    "waiting_supervisor",
                    reason="champion 尚无完整落盘的 Codex 晋级决策",
                    now=now,
                )
                return
            binding = self.autonomy.bound_identity()
            if binding is None:
                self.autonomy.set_runtime_status(
                    "suspended", reason="长期 Demo 账户身份未绑定", now=now
                )
                return
            lease = self.autonomy.active_lease(
                model_id=champion.model_id,
                artifact_sha256=champion.artifact_sha256,
                generation=champion.generation,
                policy_sha256=self.policy.policy_sha256,
                now=now,
            )
            if lease is None:
                self.autonomy.set_runtime_status(
                    "waiting_supervisor", reason="等待 Codex 监督 lease", now=now
                )
                return
            recent_positions = self.autonomy.recent_positions(limit=1)
            if (
                recent_positions
                and recent_positions[0]["status"] == "closed"
                and now < _parse_iso(str(recent_positions[0]["exitDueAt"]))
            ):
                self.autonomy.set_runtime_status(
                    "running",
                    reason="固定周期资本冷却中，不提前重叠入场",
                    now=now,
                )
                return
            self.autonomy.set_runtime_status("running", reason=None, now=now)
            await self._enter(
                champion=champion,
                lease=lease,
                binding=binding,
                candles=candles,
                market=market,
                now=now,
            )
            self.autonomy.claim_candle(candles[-1].closed_at, now=now)

    async def run(self) -> None:
        while not self._closed:
            await self._sleep(LONG_RUN_LOOP_SECONDS)
            try:
                await self.tick()
                self._last_error = None
            except asyncio.CancelledError:
                raise
            except PreviewRejected as exc:
                self._last_error = {
                    "at": self._now().isoformat(),
                    "errorType": type(exc).__name__,
                    "failClosed": False,
                }
            except PositionStateError as exc:
                self._last_error = {
                    "at": self._now().isoformat(),
                    "errorType": type(exc).__name__,
                    "failClosed": True,
                }
                try:
                    await self.service.emergency_stop(
                        "自动持仓持久状态完整性异常", actor="system"
                    )
                except Exception:
                    pass
                try:
                    self.autonomy.set_runtime_status(
                        "manual_review",
                        reason="自动持仓持久状态完整性异常",
                        now=self._now(),
                    )
                except Exception:
                    pass
            except (AutonomyError, RegistryError, OkxClientError, LongRunError) as exc:
                self._last_error = {
                    "at": self._now().isoformat(),
                    "errorType": type(exc).__name__,
                    "failClosed": True,
                }
                try:
                    self.autonomy.set_runtime_status(
                        "suspended",
                        reason=f"长期运行故障：{type(exc).__name__}",
                        now=self._now(),
                    )
                except Exception:
                    pass
                try:
                    self.audit.append(
                        "autonomy.cycle_failed",
                        {"errorType": type(exc).__name__},
                        actor="system",
                    )
                except Exception:
                    pass
            except Exception as exc:
                self._last_error = {
                    "at": self._now().isoformat(),
                    "errorType": type(exc).__name__,
                    "failClosed": True,
                }
                try:
                    self.autonomy.set_runtime_status(
                        "suspended",
                        reason="长期运行发生未分类故障",
                        now=self._now(),
                    )
                except Exception:
                    pass

    async def close(self) -> None:
        self._closed = True


__all__ = [
    "LONG_RUN_PROMOTION_POLICY",
    "LongRunCoordinator",
    "LongRunError",
    "PreviewRejected",
]
