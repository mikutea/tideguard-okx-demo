from __future__ import annotations

import asyncio
import logging
import secrets as token_secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .audit import AuditStore
from .config import (
    APP_NAME,
    APP_VERSION,
    ENVIRONMENT_SELECTOR_FILE,
    OKX_BASE_URL,
    PUBLIC_APP_NAME,
    SIMULATED_HEADER,
    app_data_dir,
)
from .environment import (
    EnvironmentConfirmRequest,
    EnvironmentSelectionStore,
    EnvironmentSwitchError,
    EnvironmentTargetRequest,
    RuntimeEnvironment,
)
from .ml.execution import AutomationDenied
from .ml.autonomy import AutonomyError, AutonomyStore, PositionStateError
from .ml.long_run import LongRunCoordinator, LongRunError
from .ml.pipeline import DatasetError
from .ml.registry import PromotionDenied, RegistryError
from .ml.runtime import MLCoordinator, MLRuntimeError
from .models import (
    ArmRequest,
    AutonomyDisableRequest,
    AutonomyEnableRequest,
    AutomationAuthorizeRequest,
    CommitRequest,
    ModelPromoteRequest,
    ModelTrainRequest,
    OrderDraft,
    ResetKillRequest,
)
from .okx_client import OkxClient, OkxClientError
from .research_monitor import ResearchMonitor
from .secrets import credentials_configured
from .service import (
    EnvironmentSwitchService,
    TradingService,
    validate_profile_config,
)
from .state import SafetyController, SafetyError


ALLOWED_HOSTS = {
    "127.0.0.1:5173",
    "localhost:5173",
    "127.0.0.1:8791",
    "localhost:8791",
    "testserver",
}
ALLOWED_ORIGINS = {
    "http://127.0.0.1:5173",
    "http://localhost:5173",
    "http://127.0.0.1:8791",
    "http://localhost:8791",
}


SAFE_AUTONOMY_RECOVERY_INTENT_STATES = frozenset(
    {"accepted", "reconciled", "terminal_verified"}
)
PROVEN_NO_DISPATCH_INTENT_STATES = frozenset(
    {"blocked_before_dispatch", "rejected"}
)


def recover_autonomy_position_before_loop(
    autonomy: AutonomyStore,
    store: AuditStore,
    long_run: LongRunCoordinator,
) -> str | None:
    """Recover only states that prove no dispatch occurred, else return a kill reason."""

    position = autonomy.active_position()
    if position is None or position["status"] == "long":
        return None
    status = str(position["status"])
    if status == "manual_review":
        return "检测到重启前需要核对的自动订单"
    position_id = str(position["positionId"])
    intent_id = str(
        (
            position.get("entryIntentId")
            if status == "entry_submitted"
            else position.get("exitIntentId")
        )
        or ""
    )
    if status == "entry_submitted" and not intent_id:
        autonomy.resolve_entry(
            position_id,
            filled_size=Decimal("0"),
            average_price=None,
            terminal_state="canceled",
            policy=long_run.policy,
            now=long_run._now(),
        )
        store.append(
            "autonomy.startup_abandoned_pre_dispatch_entry",
            {"positionId": position_id},
            actor="system",
            correlation_id=position_id,
        )
        return None
    if not intent_id:
        return "自动退出状态缺少订单意图，无法安全恢复"
    intent = store.get_intent(intent_id)
    if intent is None:
        return "自动订单意图缺失，无法安全恢复"
    if not intent.get("commit_key"):
        if status == "entry_submitted":
            autonomy.resolve_entry(
                position_id,
                filled_size=Decimal("0"),
                average_price=None,
                terminal_state="canceled",
                policy=long_run.policy,
                now=long_run._now(),
            )
        else:
            autonomy.abandon_exit_before_dispatch(position_id, now=long_run._now())
        store.append(
            "autonomy.startup_abandoned_pre_dispatch_preview",
            {"intentId": intent_id, "positionId": position_id, "side": "buy" if status == "entry_submitted" else "sell"},
            actor="system",
            correlation_id=position_id,
        )
        return None
    intent_status = str(intent.get("status") or "")
    if intent_status in PROVEN_NO_DISPATCH_INTENT_STATES:
        if status == "entry_submitted":
            autonomy.resolve_entry(
                position_id,
                filled_size=Decimal("0"),
                average_price=None,
                terminal_state="canceled",
                policy=long_run.policy,
                now=long_run._now(),
            )
        else:
            autonomy.resolve_exit(
                position_id,
                filled_size=Decimal("0"),
                average_price=None,
                terminal_state="canceled",
                max_exit_attempts=long_run.policy.max_exit_attempts,
                now=long_run._now(),
            )
        store.append(
            "autonomy.startup_resolved_proven_rejection",
            {"intentId": intent_id, "positionId": position_id, "status": intent_status},
            actor="system",
            correlation_id=position_id,
        )
        return None
    if intent_status not in SAFE_AUTONOMY_RECOVERY_INTENT_STATES:
        return "自动订单派发终态不确定，无法安全恢复"
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    startup_logger = logging.getLogger("tideguard.startup")
    startup_logger.info("Startup stage: resolve_environment")
    data_root = app_data_dir()
    runtime_environment = RuntimeEnvironment(
        EnvironmentSelectionStore(data_root / ENVIRONMENT_SELECTOR_FILE)
    )
    profile = runtime_environment.active_profile
    data_dir = profile.runtime_data_dir(data_root)
    startup_logger.info("Startup stage: audit_store")
    store = AuditStore(data_dir / "state.sqlite3")
    if not store.verify_chain():
        store.engage_kill_latch()
    if not runtime_environment.status()["selectorValid"]:
        store.engage_kill_latch()
    if profile.name == "live" and not store.get_kill_state()[0]:
        store.engage_kill_latch()
        store.append(
            "environment.live_startup_locked",
            {"environment": "live", "independentAuthorizationRequired": True},
            actor="system",
        )
    client = OkxClient(profile=profile)
    safety = SafetyController(store, profile)
    service = TradingService(
        client,
        store,
        safety,
        execution_environment_guard=runtime_environment.assert_execution_allowed,
    )
    unresolved = store.recover_unresolved_intents()
    if unresolved:
        safety.acknowledge_persisted_kill(
            "检测到上次运行遗留的未决下单，需人工核对", actor="system"
        )
        store.append(
            "order.recovery_required",
            {"count": len(unresolved), "statuses": sorted({row["status"] for row in unresolved})},
        )
    app.state.store = store
    app.state.client = client
    app.state.safety = safety
    app.state.service = service
    app.state.environment_runtime = runtime_environment
    app.state.data_root = data_root
    startup_logger.info("Startup stage: ml_registry")
    ml = MLCoordinator(data_dir=data_dir, client=client, service=service, store=store)
    startup_logger.info("Startup stage: autonomy_store")
    autonomy = AutonomyStore(data_dir / "autonomy.sqlite3")
    recovered_training_runs = autonomy.recover_running_training(
        now=datetime.now(timezone.utc)
    )
    if recovered_training_runs:
        store.append(
            "ml.training_recovered_after_restart",
            {"count": recovered_training_runs},
            actor="system",
        )
    startup_logger.info("Startup stage: market_data_store")
    long_run = LongRunCoordinator(
        client=client,
        service=service,
        audit=store,
        registry=ml.registry,
        autonomy=autonomy,
        market_data_path=data_root / "market-data.sqlite3",
    )
    startup_logger.info("Startup stage: recovery")
    try:
        autonomy_recovery_reason = recover_autonomy_position_before_loop(
            autonomy, store, long_run
        )
    except PositionStateError:
        autonomy_recovery_reason = "自动持仓持久状态完整性异常，已锁定"
    if autonomy_recovery_reason:
        binding = autonomy.bound_identity()
        store.engage_kill_latch(*(binding or (None, None)))
        safety.acknowledge_persisted_kill(
            autonomy_recovery_reason, actor="system"
        )
    app.state.ml = ml
    app.state.autonomy = autonomy
    app.state.long_run = long_run
    app.state.research_monitor = ResearchMonitor()

    async def stop_legacy_automation(reason: str) -> dict[str, Any]:
        return await ml.stop_automation(
            reason=reason,
            actor="user",
            emergency=False,
        )

    app.state.environment_switch = EnvironmentSwitchService(
        runtime=runtime_environment,
        data_root=data_root,
        trading=service,
        audit=store,
        autonomy=autonomy,
        stop_legacy_automation=stop_legacy_automation,
    )
    app.state.csrf_token = token_secrets.token_urlsafe(32)
    app.state.deadman_task = None
    app.state.ml_task = None
    store.append(
        "system.started",
        {
            "environment": profile.name,
            "bind": "127.0.0.1",
            "armed": False,
            "selectorValid": runtime_environment.status()["selectorValid"],
        },
    )

    async def deadman_loop() -> None:
        while True:
            await asyncio.sleep(7)
            try:
                if safety.status()["mode"] != "armed":
                    continue
                await service.renew_deadman()
            except Exception as exc:  # fail closed; never include raw headers or request objects
                try:
                    await service.emergency_stop("Cancel-All-After 心跳失败", actor="system")
                except Exception:
                    pass
                try:
                    store.append(
                        "safety.deadman_failed",
                        {"errorType": type(exc).__name__},
                    )
                except Exception:
                    pass

    app.state.deadman_task = asyncio.create_task(deadman_loop())
    app.state.ml_task = asyncio.create_task(long_run.run())
    startup_logger.info("Startup stage: ready")
    try:
        yield
    finally:
        ml_task = app.state.ml_task
        if ml_task:
            ml_task.cancel()
            try:
                await ml_task
            except asyncio.CancelledError:
                pass
        await long_run.close()
        await ml.close()
        task = app.state.deadman_task
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await service.disarm("server_shutdown")
        await client.close()


app = FastAPI(
    title=f"{PUBLIC_APP_NAME} OKX Local API",
    version=APP_VERSION,
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(ALLOWED_ORIGINS),
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Tideguard-CSRF", "Idempotency-Key"],
)


@app.middleware("http")
async def local_request_guard(request: Request, call_next):
    host = request.headers.get("host", "")
    if request.url.path.startswith("/api/") and host not in ALLOWED_HOSTS:
        return HTMLResponse("Invalid local host", status_code=400)
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path.startswith("/api/"):
        origin = request.headers.get("origin")
        if origin and origin not in ALLOWED_ORIGINS:
            return HTMLResponse("Invalid origin", status_code=403)
        supplied = request.headers.get("x-tideguard-csrf")
        if supplied != getattr(request.app.state, "csrf_token", None):
            return HTMLResponse("Missing local CSRF token", status_code=403)
    return await call_next(request)


def service(request: Request) -> TradingService:
    return request.app.state.service


@app.get("/api/v1/system/status")
async def system_status(request: Request) -> dict[str, Any]:
    environment_status = request.app.state.environment_switch.status()
    policy = service(request).policy
    profile = request.app.state.environment_runtime.active_profile
    return {
        "app": APP_NAME,
        "version": APP_VERSION,
        "environment": profile.display_name,
        "environmentProfile": environment_status,
        "demoHeader": SIMULATED_HEADER if profile.simulated_trading else None,
        "baseUrl": OKX_BASE_URL,
        "bind": "127.0.0.1",
        "credentialConfigured": environment_status["credentialConfigured"],
        "credentialStore": "Windows Credential Manager",
        "safety": request.app.state.safety.status(),
        "auditChainValid": request.app.state.store.verify_chain(),
        "csrfToken": request.app.state.csrf_token,
        "policy": {
            "version": policy.policy_version,
            "maxOrderNotionalUsdt": str(policy.max_order_notional_usdt),
            "maxOrderEquityFraction": str(policy.max_order_equity_fraction),
            "maxPriceDeviation": str(policy.max_price_deviation),
            "maxOpenOrders": policy.max_open_orders,
            "staleMarketSeconds": policy.stale_market_seconds,
            "automationArmTtlSeconds": policy.automation_arm_ttl_seconds,
        },
    }


@app.post("/api/v1/connection/test")
async def connection_test(request: Request) -> dict[str, Any]:
    client: OkxClient = request.app.state.client
    profile = request.app.state.environment_runtime.active_profile
    try:
        public_time = await client.public_get("/api/v5/public/time")
        configured = (
            credentials_configured()
            if profile.name == "demo"
            else credentials_configured(profile)
        )
        if configured:
            fingerprint = client.current_credential_fingerprint()
            private_config = await client.get_account_config(
                expected_credential_fingerprint=fingerprint
            )
        else:
            private_config = []
        private_reachable = bool(private_config)
        policy_valid = False
        policy_reason: str | None = None
        if private_reachable:
            try:
                validate_profile_config(profile, private_config)
                policy_valid = True
            except EnvironmentSwitchError as exc:
                policy_reason = str(exc)
        else:
            policy_reason = "当前环境未配置凭证或账户配置不可达"
        request.app.state.store.append(
            "connection.tested",
            {
                "public": bool(public_time),
                "privateReachable": private_reachable,
                "policyValid": policy_valid,
                "environment": profile.name,
            },
            actor="user",
        )
        return {
            "public": bool(public_time),
            # Keep ``private`` for older clients while exposing the policy result
            # separately. Reachability must never be presented as authorization.
            "private": private_reachable,
            "privateReachable": private_reachable,
            "policyValid": policy_valid,
            "policyReason": policy_reason,
            "environment": profile.name,
        }
    except OkxClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except (httpx.RequestError, httpx.HTTPStatusError) as exc:
        raise HTTPException(
            status_code=502,
            detail="OKX 网络连接失败，请检查本机网络后重试",
        ) from exc


@app.get("/api/v1/environment/status")
async def environment_status(request: Request) -> dict[str, Any]:
    return request.app.state.environment_switch.status()


@app.post("/api/v1/environment/preflight")
async def environment_preflight(
    request: Request, body: EnvironmentTargetRequest
) -> dict[str, Any]:
    try:
        return await request.app.state.environment_switch.preflight(body.target)
    except EnvironmentSwitchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v1/environment/challenge")
async def environment_challenge(
    request: Request, body: EnvironmentTargetRequest
) -> dict[str, Any]:
    try:
        return await request.app.state.environment_switch.challenge(body.target)
    except EnvironmentSwitchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v1/environment/confirm")
async def environment_confirm(
    request: Request, body: EnvironmentConfirmRequest
) -> dict[str, Any]:
    try:
        return await request.app.state.environment_switch.confirm(
            target_name=body.target,
            nonce=body.nonce,
            confirmation=body.confirmation,
            acknowledgements=body.acknowledgements,
        )
    except EnvironmentSwitchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/v1/market")
async def market(request: Request) -> dict[str, Any]:
    try:
        return await service(request).market()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"公共行情暂不可用：{type(exc).__name__}") from exc


@app.get("/api/v1/account")
async def account(request: Request) -> dict[str, Any]:
    try:
        return await service(request).account()
    except OkxClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/v1/orders")
async def orders(request: Request) -> list[dict[str, Any]]:
    try:
        return await service(request).pending_orders()
    except OkxClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/v1/audit")
async def audit(request: Request, limit: int = 50) -> dict[str, Any]:
    return {
        "chainValid": request.app.state.store.verify_chain(),
        "events": request.app.state.store.recent(limit),
    }


@app.post("/api/v1/safety/arm")
async def arm(request: Request, body: ArmRequest) -> dict[str, Any]:
    try:
        return await service(request).arm(body.confirmation)
    except (SafetyError, ValueError, OkxClientError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v1/safety/disarm")
async def disarm(request: Request) -> dict[str, Any]:
    return await service(request).disarm()


@app.post("/api/v1/safety/kill")
async def kill(request: Request) -> dict[str, Any]:
    return await service(request).emergency_stop()


@app.post("/api/v1/safety/reset-kill")
async def reset_kill(request: Request, body: ResetKillRequest) -> dict[str, Any]:
    try:
        return await service(request).reset_kill(body.confirmation)
    except (SafetyError, ValueError, OkxClientError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v1/intents/preview")
async def preview(request: Request, draft: OrderDraft) -> dict[str, Any]:
    try:
        return await service(request).preview(draft)
    except SafetyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OkxClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/v1/intents/{intent_id}/commit")
async def commit(
    request: Request,
    intent_id: str,
    body: CommitRequest,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=16, max_length=128),
) -> dict[str, Any]:
    try:
        return await service(request).commit(intent_id, body.digest, idempotency_key)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OkxClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/v1/ml/status")
async def ml_status(request: Request) -> dict[str, Any]:
    try:
        return {
            **request.app.state.ml.status(),
            "longRun": request.app.state.long_run.status(),
        }
    except (RegistryError, AutomationDenied, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/v1/research/status")
async def research_status(request: Request) -> dict[str, Any]:
    return request.app.state.research_monitor.status()


@app.post("/api/v1/ml/train")
async def ml_train(request: Request, body: ModelTrainRequest) -> dict[str, Any]:
    try:
        return await request.app.state.ml.train_candidate(body.candleLimit)
    except (MLRuntimeError, DatasetError, RegistryError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OkxClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/v1/ml/promote")
async def ml_promote(request: Request, body: ModelPromoteRequest) -> dict[str, Any]:
    del request, body
    raise HTTPException(
        status_code=410,
        detail="v0.3 已关闭浏览器人工晋级；只能由本机 Codex Supervisor 审查脱敏证据后晋级",
    )


@app.post("/api/v1/ml/automation/authorize")
async def ml_automation_authorize(
    request: Request, body: AutomationAuthorizeRequest
) -> dict[str, Any]:
    del request, body
    raise HTTPException(
        status_code=410,
        detail="v0.3 已停用旧版单次 BUY 许可；请使用 Codex 监督的长期 Demo master switch",
    )


@app.post("/api/v1/ml/automation/stop")
async def ml_automation_stop(request: Request) -> dict[str, Any]:
    try:
        return await request.app.state.ml.stop_automation()
    except (AutomationDenied, RegistryError, ValueError, OkxClientError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/v1/autonomy/status")
async def autonomy_status(request: Request) -> dict[str, Any]:
    try:
        return request.app.state.long_run.status()
    except (AutonomyError, RegistryError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/v1/autonomy/review-pack")
async def autonomy_review_pack(request: Request) -> dict[str, Any]:
    try:
        return request.app.state.long_run.supervisor.review_pack(
            now=request.app.state.long_run._now()
        )
    except (AutonomyError, RegistryError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v1/autonomy/train")
async def autonomy_train(request: Request) -> dict[str, Any]:
    try:
        return await request.app.state.long_run.train_now()
    except (LongRunError, DatasetError, RegistryError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OkxClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/v1/autonomy/master/enable")
async def autonomy_enable(
    request: Request, body: AutonomyEnableRequest
) -> dict[str, Any]:
    try:
        if request.app.state.environment_runtime.active_profile.name == "live":
            raise AutonomyError(
                "Live 长期自动化尚未提供独立授权流程；不能复用 Demo master。"
            )
        binding = await service(request).current_identity_binding()
        state = request.app.state.autonomy.enable_master(
            mode=body.mode,
            credential_fingerprint=binding[0],
            account_fingerprint=binding[1],
            confirmation=body.confirmation,
            now=request.app.state.long_run._now(),
        )
        request.app.state.store.append(
            "autonomy.master_enabled",
            {"identityBound": True, "mode": body.mode},
            actor="user",
        )
        return state
    except (AutonomyError, SafetyError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OkxClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/v1/autonomy/master/disable")
async def autonomy_disable(
    request: Request, body: AutonomyDisableRequest
) -> dict[str, Any]:
    try:
        state = request.app.state.autonomy.disable_master(
            body.reason,
            now=request.app.state.long_run._now(),
        )
        request.app.state.store.append(
            "autonomy.master_disabled",
            {"reason": body.reason},
            actor="user",
        )
        return state
    except (AutonomyError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/healthz")
async def healthz(request: Request) -> dict[str, str]:
    return {
        "status": "ok",
        "app": APP_NAME,
        "environment": request.app.state.environment_runtime.active_profile.name,
        "version": APP_VERSION,
    }


PROJECT_ROOT = Path(__file__).resolve().parents[3]
FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"
if FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
else:
    @app.get("/", response_class=HTMLResponse)
    async def development_root() -> str:
        return f"<h1>{PUBLIC_APP_NAME} API</h1><p>前端尚未构建。开发时请打开 http://127.0.0.1:5173。</p>"
