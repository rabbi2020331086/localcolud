import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.router import router
from app.core.config import settings
from app.core.security import decode_jwt
from app.infrastructure.database import close_database, database, from_document, initialize_database
from app.infrastructure.metrics import CPU_PERCENT_COMMAND
from app.infrastructure.models import Instance, InstanceStatus
from app.services import autoscaling as autoscaling_service
from app.services import load_balancing as load_balancing_service
from app.services import node_jobs
from app.services import operations as operation_service
from app.services import reconciliation as reconciliation_service

log = logging.getLogger("localec2")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    for attempt in range(1, 4):
        try:
            await initialize_database()
            break
        except Exception:
            if attempt == 3:
                raise
            log.warning("MongoDB initialization failed; retrying (%s/3)", attempt)
            await asyncio.sleep(2)
    operation_service.worker_task = asyncio.create_task(operation_service.operation_loop())
    autoscaling_service.worker_task = asyncio.create_task(autoscaling_service.autoscaling_loop())
    load_balancing_service.worker_task = asyncio.create_task(load_balancing_service.health_loop())
    reconciliation_service.worker_task = asyncio.create_task(
        reconciliation_service.reconciliation_loop()
    )
    yield
    tasks = [
        operation_service.worker_task,
        autoscaling_service.worker_task,
        load_balancing_service.worker_task,
        reconciliation_service.worker_task,
    ]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await close_database()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin, settings.admin_frontend_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_id(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    rid = request.headers.get("x-request-id", str(uuid.uuid4()))
    request.state.request_id = rid
    response = await call_next(request)
    response.headers["x-request-id"] = rid
    return response


@app.exception_handler(HTTPException)
async def problem(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        {
            "type": "HTTP_ERROR",
            "title": "Request failed",
            "status": exc.status_code,
            "detail": exc.detail,
            "request_id": getattr(request.state, "request_id", str(uuid.uuid4())),
        },
        status_code=exc.status_code,
        headers=exc.headers,
    )


@app.get("/health")
async def health() -> dict[str, object]:
    return {"status": "ok", "api": True}


@app.get("/health/readiness")
async def readiness() -> dict[str, object]:
    database_ready = True
    try:
        await database.command("ping")
    except Exception:
        database_ready = False
    deadline = datetime.now(UTC) - timedelta(seconds=settings.node_offline_seconds)
    online_nodes = await database.cloud_nodes.count_documents(
        {
            "approved": True,
            "schedulable": True,
            "last_seen_at": {"$gte": deadline},
            "provider_name": "vagrant-libvirt",
            "capabilities": {"$all": ["jobs", "vagrant", "libvirt", "kvm"]},
        }
    )
    return {
        "status": "ready" if database_ready and online_nodes else "degraded",
        "database": database_ready,
        "compute_capacity_ready": bool(online_nodes),
        "worker": bool(operation_service.worker_task and not operation_service.worker_task.done()),
    }


app.include_router(router, prefix=settings.api_prefix)


def ws_identity(websocket: WebSocket) -> tuple[uuid.UUID, str | None] | None:
    for protocol in websocket.headers.get("sec-websocket-protocol", "").split(","):
        protocol = protocol.strip()
        if protocol.startswith("localec2-token."):
            try:
                token = protocol.removeprefix("localec2-token.")
                user_id = uuid.UUID(str(decode_jwt(token, "access")["sub"]))
                return user_id, protocol
            except Exception:
                return None
    return None


async def ws_instance(websocket: WebSocket) -> Instance | None:
    identity = ws_identity(websocket)
    try:
        if not identity:
            raise ValueError("Invalid identity")
        user_id, auth_protocol = identity
        instance_id = uuid.UUID(websocket.path_params["instance_id"])
    except Exception:
        await websocket.close(4401)
        return None
    item = from_document(Instance, await database.instances.find_one({"_id": str(instance_id)}))
    if not item or item.user_id != user_id:
        await websocket.close(4404)
        return None
    websocket.state.auth_protocol = auth_protocol
    return item


@app.websocket("/ws/instances/{instance_id}/metrics")
async def metrics_ws(websocket: WebSocket) -> None:
    item = await ws_instance(websocket)
    if not item:
        return
    await websocket.accept(subprotocol=getattr(websocket.state, "auth_protocol", None))
    try:
        while True:
            if item.status != InstanceStatus.RUNNING:
                await websocket.send_json({"type": "stopped"})
                break
            output = await node_jobs.remote_exec(
                item,
                CPU_PERCENT_COMMAND
                + "; echo; free -m | awk 'NR==2{printf \"%.1f\",$3*100/$2}'; echo; "
                "df -P / | awk 'NR==2{gsub(/%/,\"\",$5);print $5}'; "
                "awk 'NR>2{rx+=$2;tx+=$10} END{print rx;print tx}' /proc/net/dev",
            )
            v = output.splitlines()
            await websocket.send_json(
                {
                    "type": "metrics",
                    "timestamp": datetime.now(UTC).isoformat(),
                    "cpu_percent": float(v[0] or 0),
                    "memory_percent": float(v[1] or 0),
                    "disk_percent": float(v[2] or 0),
                    "network_rx_bytes": int(v[3] or 0),
                    "network_tx_bytes": int(v[4] or 0),
                }
            )
            await asyncio.sleep(2)
    except (WebSocketDisconnect, Exception):
        pass


@app.websocket("/ws/instances/{instance_id}/terminal")
async def terminal_ws(websocket: WebSocket) -> None:
    item = await ws_instance(websocket)
    if not item:
        return
    if item.status != InstanceStatus.RUNNING:
        await websocket.close(4409)
        return
    await websocket.accept(subprotocol=getattr(websocket.state, "auth_protocol", None))
    await websocket.send_text(
        "Interactive PTY is disabled for distributed nodes. Use the instance SSH address and "
        "downloaded key; reverse agent terminal streaming is not configured yet.\r\n"
    )
    await websocket.close(4410)
