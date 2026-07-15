from typing import Annotated
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import settings
from app.core.security import decode_jwt
from app.infrastructure.database import Database, from_document
from app.infrastructure.models import PlatformRole, User

bearer = HTTPBearer(auto_error=False)


async def current_user(
    database: Database,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> User:
    if not credentials:
        raise HTTPException(401, "Authentication required")
    try:
        user_id = UUID(str(decode_jwt(credentials.credentials, "access")["sub"]))
    except (jwt.PyJWTError, ValueError, KeyError):
        raise HTTPException(401, "Invalid access token") from None
    user = from_document(User, await database.users.find_one({"_id": str(user_id)}))
    if not user or not user.is_active:
        raise HTTPException(401, "Invalid access token")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def has_platform_admin_access(user: User) -> bool:
    configured = settings.platform_admin_email.strip().lower()
    return (
        user.is_platform_admin
        or PlatformRole.ADMINISTRATOR in user.platform_roles
        or bool(configured and user.email.lower() == configured)
    )


async def platform_admin(user: CurrentUser) -> User:
    if not has_platform_admin_access(user):
        raise HTTPException(403, "Platform administrator access is required")
    return user


PlatformAdmin = Annotated[User, Depends(platform_admin)]


async def platform_operator(user: CurrentUser) -> User:
    if not has_platform_admin_access(user) and PlatformRole.OPERATIONS not in user.platform_roles:
        raise HTTPException(403, "Platform operations access is required")
    return user


PlatformOperator = Annotated[User, Depends(platform_operator)]
