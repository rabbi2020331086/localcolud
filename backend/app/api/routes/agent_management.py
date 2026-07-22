import hashlib
import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from app.api.dependencies import CurrentUser, PlatformAdmin, has_platform_admin_access
from app.core.config import settings
from app.core.security import verify_password
from app.infrastructure.database import Database, from_document
from app.infrastructure.models import User

router = APIRouter(prefix="/admin/agent", tags=["Agent releases"])


class AgentCodeIn(BaseModel):
    code: str = Field(min_length=1, max_length=2_000_000)


class AgentPermissionIn(BaseModel):
    can_view: bool = False
    can_edit: bool = False


class DeployAuthorizationIn(BaseModel):
    authorization_code: str = Field(min_length=8, max_length=128)


class DeployCodeIn(BaseModel):
    admin_password: str = Field(min_length=1, max_length=1024)


def release_paths() -> tuple[Path, Path, Path]:
    root = Path(settings.agent_release_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root, root / "draft.py", root / "published.py"


def archive_root() -> Path:
    root, _, _ = release_paths()
    archive = root / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    return archive


def source_path() -> Path:
    return Path(__file__).resolve().parents[2] / "agent.py"


def published_agent_path() -> Path:
    _, _, published = release_paths()
    return published if published.is_file() else source_path()


def agent_version(code: str) -> str:
    match = re.search(r'^AGENT_VERSION\s*=\s*"([^"]+)"', code, re.MULTILINE)
    if not match:
        match = re.search(r'"agent_version"\s*:\s*"([^"]+)"', code)
    if not match:
        raise HTTPException(422, 'Agent code must contain AGENT_VERSION = "x.y.z"')
    return match.group(1)


def with_compatibility_marker(code: str) -> str:
    marker = (
        '# AUTO-GENERATED COMPATIBILITY MARKER: '
        f'"agent_version": "{agent_version(code)}"'
    )
    pattern = r'^# AUTO-GENERATED COMPATIBILITY MARKER:.*$'
    if re.search(pattern, code, re.MULTILINE):
        return re.sub(pattern, marker, code, count=1, flags=re.MULTILINE)
    assignment = re.search(r'^AGENT_VERSION\s*=.*$', code, re.MULTILINE)
    if not assignment:
        return code
    return code[: assignment.end()] + "\n" + marker + code[assignment.end() :]


def atomic_write(path: Path, code: str) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(code, encoding="utf-8")
    temporary.replace(path)


def atomic_json(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(path)


def code_digest(admin_id: UUID, value: str) -> str:
    protected = f"agent-deploy:{settings.jwt_secret}:{admin_id}:{value}"
    return hashlib.sha256(protected.encode()).hexdigest()


def archived_releases() -> list[dict[str, object]]:
    releases: list[dict[str, object]] = []
    for metadata_path in archive_root().glob("*.json"):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            release_id = str(metadata["id"])
            source = archive_root() / f"{release_id}.py"
            if source.is_file():
                releases.append(metadata)
        except (KeyError, OSError, ValueError, TypeError):
            continue
    return sorted(releases, key=lambda item: str(item.get("created_at", "")), reverse=True)


def archive_release(code: str, admin: User, action: str) -> dict[str, object]:
    release_id = str(uuid4())
    now = datetime.now(UTC).isoformat()
    source = with_compatibility_marker(code)
    metadata: dict[str, object] = {
        "id": release_id,
        "version": agent_version(source),
        "sha256": hashlib.sha256(source.encode()).hexdigest(),
        "created_at": now,
        "created_by": admin.email,
        "action": action,
    }
    atomic_write(archive_root() / f"{release_id}.py", source)
    atomic_json(archive_root() / f"{release_id}.json", metadata)
    return metadata


def set_live_release(release: dict[str, object], code: str) -> None:
    root, _, published = release_paths()
    atomic_write(published, with_compatibility_marker(code))
    atomic_json(
        root / "live.json",
        {
            "release_id": release["id"],
            "version": release["version"],
            "deployed_at": datetime.now(UTC).isoformat(),
        },
    )


def live_release() -> dict[str, object] | None:
    root, _, _ = release_paths()
    try:
        value = json.loads((root / "live.json").read_text(encoding="utf-8"))
        return cast(dict[str, object], value) if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


async def consume_deploy_code(database: Any, admin: User, value: str) -> None:
    if settings.app_env != "production" and verify_password(value, admin.password_hash):
        return
    now = datetime.now(UTC)
    result = await database.agent_deploy_codes.find_one_and_update(
        {
            "admin_id": str(admin.id),
            "code_hash": code_digest(admin.id, value),
            "consumed_at": None,
            "expires_at": {"$gt": now},
        },
        {"$set": {"consumed_at": now}},
    )
    if not result:
        raise HTTPException(
            403, "Deployment authorization code is invalid, expired, or already used"
        )


def require_view(user: User) -> None:
    if not has_platform_admin_access(user) and not user.can_view_agent_code:
        raise HTTPException(403, "Agent source viewing permission is required")


@router.get("")
async def get_agent_workspace(user: CurrentUser) -> dict[str, object]:
    require_view(user)
    _, draft, published = release_paths()
    published_code = published_agent_path().read_text(encoding="utf-8")
    bundled_code = source_path().read_text(encoding="utf-8")
    draft_code = draft.read_text(encoding="utf-8") if draft.is_file() else published_code
    is_admin = has_platform_admin_access(user)
    live = live_release()
    return {
        "draft": draft_code,
        "draft_version": agent_version(draft_code),
        "bundled_source": bundled_code,
        "bundled_version": agent_version(bundled_code),
        "published_version": agent_version(published_code),
        "published_at": (
            datetime.fromtimestamp(published.stat().st_mtime, UTC).isoformat()
            if published.is_file() else None
        ),
        "can_edit": is_admin or user.can_edit_agent_code,
        "can_deploy": is_admin,
        "live_release_id": live.get("release_id") if live else None,
        "releases": archived_releases() if is_admin else [],
    }


@router.put("/draft")
async def save_agent_draft(data: AgentCodeIn, user: CurrentUser) -> dict[str, str]:
    require_view(user)
    if not has_platform_admin_access(user) and not user.can_edit_agent_code:
        raise HTTPException(403, "Agent source editing permission is required")
    try:
        compile(data.code, "agent.py", "exec")
    except SyntaxError as exc:
        raise HTTPException(422, f"Python syntax error on line {exc.lineno}: {exc.msg}") from exc
    _, draft, _ = release_paths()
    atomic_write(draft, data.code)
    return {"status": "DRAFT_SAVED", "version": agent_version(data.code)}


@router.post("/deploy")
async def deploy_agent(
    data: DeployAuthorizationIn, admin: PlatformAdmin, database: Database
) -> dict[str, str]:
    _, draft, _ = release_paths()
    if not draft.is_file():
        raise HTTPException(409, "Save an agent draft before deployment")
    code = draft.read_text(encoding="utf-8")
    try:
        compile(code, "agent.py", "exec")
    except SyntaxError as exc:
        raise HTTPException(422, f"Python syntax error on line {exc.lineno}: {exc.msg}") from exc
    current = published_agent_path().read_text(encoding="utf-8")
    if code != current and agent_version(code) == agent_version(current):
        raise HTTPException(409, "Increase agent_version before deploying changed code")
    await consume_deploy_code(database, admin, data.authorization_code)
    current_source = with_compatibility_marker(current)
    current_hash = hashlib.sha256(current_source.encode()).hexdigest()
    if code != current and not any(
        release.get("sha256") == current_hash for release in archived_releases()
    ):
        archive_release(current, admin, "PREVIOUS")
    release = archive_release(code, admin, "DEPLOY")
    set_live_release(release, code)
    return {"status": "DEPLOYED", "version": agent_version(code), "release_id": str(release["id"])}


@router.post("/deploy-code")
async def create_deploy_code(
    data: DeployCodeIn, admin: PlatformAdmin, database: Database, response: Response
) -> dict[str, object]:
    if not verify_password(data.admin_password, admin.password_hash):
        raise HTTPException(403, "Administrator password is incorrect")
    expires_at = datetime.now(UTC) + timedelta(hours=settings.agent_deploy_code_hours)
    raw_codes = ["-".join(secrets.token_hex(3).upper() for _ in range(3)) for _ in range(5)]
    now = datetime.now(UTC)
    await database.agent_deploy_codes.update_many(
        {"admin_id": str(admin.id), "consumed_at": None},
        {"$set": {"consumed_at": now, "invalidated": True}},
    )
    await database.agent_deploy_codes.insert_many(
        [
            {
                "_id": str(uuid4()),
                "admin_id": str(admin.id),
                "code_hash": code_digest(admin.id, raw),
                "created_at": now,
                "expires_at": expires_at,
                "consumed_at": None,
            }
            for raw in raw_codes
        ]
    )
    response.headers["Cache-Control"] = "no-store"
    return {"codes": raw_codes, "expires_at": expires_at.isoformat()}


@router.post("/releases/{release_id}/rollback")
async def rollback_agent(
    release_id: UUID,
    data: DeployAuthorizationIn,
    admin: PlatformAdmin,
    database: Database,
) -> dict[str, str]:
    metadata_path = archive_root() / f"{release_id}.json"
    source_path = archive_root() / f"{release_id}.py"
    if not metadata_path.is_file() or not source_path.is_file():
        raise HTTPException(404, "Archived agent release not found")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    code = source_path.read_text(encoding="utf-8")
    if hashlib.sha256(code.encode()).hexdigest() != metadata.get("sha256"):
        raise HTTPException(409, "Archived release integrity check failed")
    await consume_deploy_code(database, admin, data.authorization_code)
    set_live_release(metadata, code)
    return {"status": "ROLLED_BACK", "version": agent_version(code), "release_id": str(release_id)}


@router.patch("/permissions/{user_id}")
async def update_agent_permission(
    user_id: UUID, data: AgentPermissionIn, admin: PlatformAdmin, database: Database
) -> dict[str, object]:
    user = from_document(User, await database.users.find_one({"_id": str(user_id)}))
    if not user:
        raise HTTPException(404, "Team member not found")
    can_edit = data.can_edit
    can_view = data.can_view or can_edit
    await database.users.update_one(
        {"_id": str(user_id)},
        {"$set": {"can_view_agent_code": can_view, "can_edit_agent_code": can_edit}},
    )
    return {"user_id": str(user_id), "can_view": can_view, "can_edit": can_edit}
