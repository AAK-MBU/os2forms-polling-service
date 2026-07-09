"""FastAPI dependencies: DB session, JWT principal, scope guards."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel import Session

from app.auth import SCOPE_READ, SCOPE_WRITE, AuthError, Principal, decode_jwt
from app.db import get_sessionmaker

_bearer = HTTPBearer(auto_error=False)


def get_session() -> Iterator[Session]:
    """Yield a session; commit on success, roll back on error."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_principal(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Principal:
    if creds is None or not creds.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return decode_jwt(creds.credentials)
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def require_read(principal: Principal = Depends(get_principal)) -> Principal:
    if not principal.has_scope(SCOPE_READ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="requires polling:read")
    return principal


def require_write(principal: Principal = Depends(get_principal)) -> Principal:
    if not principal.has_scope(SCOPE_WRITE):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="requires polling:write")
    return principal
