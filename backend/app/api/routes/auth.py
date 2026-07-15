from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
from fastapi import APIRouter, HTTPException, Request, Response
from pymongo.errors import DuplicateKeyError

from app.api.dependencies import CurrentUser, has_platform_admin_access
from app.core.config import settings
from app.core.security import create_jwt, decode_jwt, hash_password, token_hash, verify_password
from app.infrastructure.database import Database, from_document, to_document
from app.infrastructure.models import RefreshToken, User
from app.schemas import LoginIn, PasswordChangeIn, RegisterIn, TokenOut, UserOut

router = APIRouter(prefix="/auth", tags=["Authentication"])


def cookie(response: Response, value: str) -> None:
    response.set_cookie(
        "localec2_refresh",
        value,
        httponly=True,
        secure=settings.app_env == "production",
        samesite="lax",
        max_age=settings.refresh_token_days * 86400,
        path=f"{settings.api_prefix}/auth",
    )


async def issue(database: Database, user: User, response: Response) -> TokenOut:
    jti = str(uuid4())
    raw = create_jwt(str(user.id), "refresh", timedelta(days=settings.refresh_token_days), jti)
    refresh = RefreshToken(
        user_id=user.id,
        jti=jti,
        token_hash=token_hash(raw),
        expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_days),
    )
    await database.refresh_tokens.insert_one(to_document(refresh))
    cookie(response, raw)
    return TokenOut(
        access_token=create_jwt(
            str(user.id), "access", timedelta(minutes=settings.access_token_minutes)
        ),
        user=UserOut.model_validate(
            {**user.model_dump(), "is_platform_admin": has_platform_admin_access(user)}
        ),
    )


@router.post("/register", response_model=TokenOut, status_code=201)
async def register(data: RegisterIn, response: Response, database: Database) -> TokenOut:
    user = User(
        email=data.email.lower(),
        display_name=data.display_name.strip(),
        password_hash=hash_password(data.password),
        is_platform_admin=bool(
            settings.platform_admin_email
            and data.email.lower() == settings.platform_admin_email.strip().lower()
        ),
    )
    try:
        await database.users.insert_one(to_document(user))
    except DuplicateKeyError:
        raise HTTPException(409, "An account with this email already exists") from None
    return await issue(database, user, response)


@router.post("/login", response_model=TokenOut)
async def login(data: LoginIn, response: Response, database: Database) -> TokenOut:
    identifier = data.email.strip().lower()
    email = "admin@localec2.local" if identifier == "admin" else identifier
    user = from_document(User, await database.users.find_one({"email": email}))
    if not user or not verify_password(data.password, user.password_hash):
        raise HTTPException(401, "Invalid email or password")
    return await issue(database, user, response)


@router.post("/change-password", status_code=204)
async def change_password(data: PasswordChangeIn, user: CurrentUser, database: Database) -> None:
    if not verify_password(data.current_password, user.password_hash):
        raise HTTPException(401, "Current password is incorrect")
    await database.users.update_one(
        {"_id": str(user.id)},
        {
            "$set": {
                "password_hash": hash_password(data.new_password),
                "must_change_password": False,
            }
        },
    )


@router.post("/refresh", response_model=TokenOut)
async def refresh(request: Request, response: Response, database: Database) -> TokenOut:
    raw = request.cookies.get("localec2_refresh")
    try:
        payload = decode_jwt(raw or "", "refresh")
        row = from_document(
            RefreshToken,
            await database.refresh_tokens.find_one(
                {"jti": payload["jti"], "token_hash": token_hash(raw or "")}
            ),
        )
    except (jwt.PyJWTError, KeyError):
        row = None
    if not row or row.revoked_at or row.expires_at < datetime.now(UTC):
        raise HTTPException(401, "Refresh session is invalid")
    await database.refresh_tokens.update_one(
        {"_id": str(row.id), "revoked_at": None},
        {"$set": {"revoked_at": datetime.now(UTC)}},
    )
    user = from_document(User, await database.users.find_one({"_id": str(row.user_id)}))
    if not user:
        raise HTTPException(401, "Refresh session is invalid")
    return await issue(database, user, response)


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response, database: Database) -> None:
    raw = request.cookies.get("localec2_refresh")
    if raw:
        await database.refresh_tokens.update_one(
            {"token_hash": token_hash(raw), "revoked_at": None},
            {"$set": {"revoked_at": datetime.now(UTC)}},
        )
    response.delete_cookie("localec2_refresh", path=f"{settings.api_prefix}/auth")


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> User:
    return User.model_validate(
        {**user.model_dump(), "is_platform_admin": has_platform_admin_access(user)}
    )
