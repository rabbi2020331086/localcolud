import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.router import router
from app.core.config import settings
from app.core.security import decode_jwt
from app.infrastructure.database import close_database, database, from_document, initialize_database
from app.infrastructure.metrics import CPU_PERCENT_COMMAND
from app.infrastructure.models import Instance, InstanceStatus, LoadBalancer
from app.infrastructure.terminal import create_terminal
from app.infrastructure.vm import provider
from app.services import autoscaling as autoscaling_service
from app.services import load_balancing as load_balancing_service
from app.services import operations as operation_service
from app.services import reconciliation as reconciliation_service

log = logging.getLogger("localec2")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    try:
        await initialize_database()
    except Exception as exc:
        log.warning("MongoDB initialization failed: %s", exc)
    operation_service.worker_task = asyncio.create_task(operation_service.operation_loop())
    autoscaling_service.worker_task = asyncio.create_task(autoscaling_service.autoscaling_loop())
    load_balancing_service.worker_task = asyncio.create_task(load_balancing_service.health_loop())
    reconciliation_service.worker_task = asyncio.create_task(
        reconciliation_service.reconciliation_loop()
    )
    yield
    operation_service.worker_task.cancel()
    autoscaling_service.worker_task.cancel()
    load_balancing_service.worker_task.cancel()
    reconciliation_service.worker_task.cancel()
    try:
        await operation_service.worker_task
    except asyncio.CancelledError:
        pass
    try:
        await reconciliation_service.worker_task
    except asyncio.CancelledError:
        pass
    try:
        await autoscaling_service.worker_task
    except asyncio.CancelledError:
        pass
    try:
        await load_balancing_service.worker_task
    except asyncio.CancelledError:
        pass
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
    try:
        vm = await provider.health_check()
    except Exception as exc:
        log.warning("Multipass health check failed: %s", exc)
        vm = False
    return {
        "status": "ready" if database_ready and vm else "degraded",
        "database": database_ready,
        "multipass": vm,
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


@app.websocket("/ws/load-balancers/{load_balancer_id}/terminal")
async def load_balancer_terminal(websocket: WebSocket) -> None:
    identity = ws_identity(websocket)
    if not identity:
        await websocket.close(4401)
        return
    user_id, protocol = identity
    try:
        load_balancer_id = uuid.UUID(websocket.path_params["load_balancer_id"])
    except ValueError:
        await websocket.close(4404)
        return
    load_balancer = from_document(
        LoadBalancer,
        await database.load_balancers.find_one(
            {"_id": str(load_balancer_id), "user_id": str(user_id), "enabled": True}
        ),
    )
    if not load_balancer:
        await websocket.close(4404)
        return
    await websocket.accept(subprotocol=protocol)
    await websocket.send_json({"type": "ready", "message": "Load-aware command terminal ready"})
    try:
        while True:
            payload = json.loads(await websocket.receive_text())
            command = str(payload.get("command", "")).strip()
            if not command:
                continue
            if len(command) > 4000:
                await websocket.send_json({"type": "error", "message": "Command is too long"})
                continue
            selected = await load_balancing_service.select_lowest_load(load_balancer)
            if not selected:
                await websocket.send_json(
                    {"type": "error", "message": "No running target can execute commands"}
                )
                continue
            target, cpu = selected
            await websocket.send_json(
                {"type": "selected", "target": target.display_name, "cpu_percent": cpu}
            )
            try:
                output = await provider.exec(
                    target.multipass_name, ["sh", "-lc", command], timeout=300
                )
                await websocket.send_json({"type": "output", "data": output})
                await websocket.send_json({"type": "done", "target": target.display_name})
            except Exception as exc:
                await websocket.send_json({"type": "error", "message": str(exc)})
            finally:
                await load_balancing_service.release_target(load_balancer.id, target.id)
    except (WebSocketDisconnect, json.JSONDecodeError):
        return


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
            output = await provider.exec(
                item.multipass_name,
                [
                    "sh",
                    "-lc",
                    CPU_PERCENT_COMMAND + "; echo; "
                    "free -m | awk 'NR==2{printf \"%.1f\",$3*100/$2}'; echo; "
                    "df -P / | awk 'NR==2{gsub(/%/,\"\",$5);print $5}'; "
                    "awk 'NR>2{rx+=$2;tx+=$10} END{print rx;print tx}' /proc/net/dev",
                ],
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
    process = await create_terminal(settings.multipass_executable, item.multipass_name)

    async def output() -> None:
        while True:
            data = await process.read(4096)
            if not data:
                break
            await websocket.send_text(data)

    task = asyncio.create_task(output())
    try:
        while True:
            message = await websocket.receive()
            if "bytes" in message and message["bytes"]:
                await process.write(message["bytes"].decode(errors="replace"))
            elif "text" in message and message["text"]:
                data = json.loads(message["text"])
                if data.get("type") == "input":
                    await process.write(str(data.get("data", "")))
                elif data.get("type") == "resize":
                    await process.resize(int(data.get("rows", 30)), int(data.get("cols", 120)))
    except WebSocketDisconnect:
        pass
    finally:
        await process.close()
        task.cancel()
