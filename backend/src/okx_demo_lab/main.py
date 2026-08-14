from __future__ import annotations

import asyncio
import secrets as token_secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .audit import AuditStore
from .config import APP_NAME, OKX_BASE_URL, POLICY, SIMULATED_HEADER, app_data_dir
from .ml.execution import AutomationDenied
from .ml.pipeline import DatasetError
from .ml.registry import PromotionDenied, RegistryError
from .ml.runtime import MLCoordinator, MLRuntimeError
from .models import (
    ArmRequest,
    AutomationAuthorizeRequest,
    CommitRequest,
    ModelPromoteRequest,
    ModelTrainRequest,
    OrderDraft,
    ResetKillRequest,
)
from .okx_client import OkxClient, OkxClientError
from .secrets import credentials_configured
from .service import TradingService
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    data_dir = app_data_dir()
    store = AuditStore(data_dir / "state.sqlite3")
    if not store.verify_chain():
        store.engage_kill_latch()
    client = OkxClient()
    safety = SafetyController(store)
    service = TradingService(client, store, safety)
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
    ml = MLCoordinator(data_dir=data_dir, client=client, service=service, store=store)
    app.state.ml = ml
    app.state.csrf_token = token_secrets.token_urlsafe(32)
    app.state.deadman_task = None
    app.state.ml_task = None
    store.append(
        "system.started",
        {"environment": "demo", "bind": "127.0.0.1", "armed": False},
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
    app.state.ml_task = asyncio.create_task(ml.run())
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
    title="Tideguard OKX Demo API",
    version="0.2.0",
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
    return {
        "app": APP_NAME,
        "version": "0.2.0",
        "environment": "OKX 模拟盘",
        "demoHeader": SIMULATED_HEADER,
        "baseUrl": OKX_BASE_URL,
        "bind": "127.0.0.1",
        "credentialConfigured": credentials_configured(),
        "credentialStore": "Windows Credential Manager",
        "safety": request.app.state.safety.status(),
        "auditChainValid": request.app.state.store.verify_chain(),
        "csrfToken": request.app.state.csrf_token,
        "policy": {
            "version": POLICY.policy_version,
            "maxOrderNotionalUsdt": str(POLICY.max_order_notional_usdt),
            "maxOrderEquityFraction": str(POLICY.max_order_equity_fraction),
            "maxPriceDeviation": str(POLICY.max_price_deviation),
            "maxOpenOrders": POLICY.max_open_orders,
            "staleMarketSeconds": POLICY.stale_market_seconds,
        },
    }


@app.post("/api/v1/connection/test")
async def connection_test(request: Request) -> dict[str, Any]:
    client: OkxClient = request.app.state.client
    try:
        public_time = await client.public_get("/api/v5/public/time")
        private_config = await client.get_account_config() if credentials_configured() else []
        request.app.state.store.append(
            "connection.tested",
            {"public": bool(public_time), "private": bool(private_config), "environment": "demo"},
            actor="user",
        )
        return {"public": bool(public_time), "private": bool(private_config), "environment": "demo"}
    except OkxClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


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
        return request.app.state.ml.status()
    except (RegistryError, AutomationDenied, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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
    try:
        return await request.app.state.ml.promote(
            body.modelId,
            reviewer=body.reviewer,
            rationale=body.rationale,
            confirmation=body.confirmation,
            expected_generation=body.expectedGeneration,
        )
    except (PromotionDenied, RegistryError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v1/ml/automation/authorize")
async def ml_automation_authorize(
    request: Request, body: AutomationAuthorizeRequest
) -> dict[str, Any]:
    try:
        return await request.app.state.ml.authorize(
            issued_by=body.issuedBy,
            confirmation=body.confirmation,
            ttl_seconds=body.ttlSeconds,
            max_orders=body.maxOrders,
            max_total_notional_usdt=body.maxTotalNotionalUsdt,
        )
    except (AutomationDenied, RegistryError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v1/ml/automation/stop")
async def ml_automation_stop(request: Request) -> dict[str, Any]:
    try:
        return await request.app.state.ml.stop_automation()
    except (AutomationDenied, RegistryError, ValueError, OkxClientError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "environment": "demo"}


PROJECT_ROOT = Path(__file__).resolve().parents[3]
FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"
if FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
else:
    @app.get("/", response_class=HTMLResponse)
    async def development_root() -> str:
        return "<h1>Tideguard API</h1><p>前端尚未构建。开发时请打开 http://127.0.0.1:5173。</p>"
