"""Auth endpoint: exchange a registered API key for a short-lived JWT."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session

from app.api.deps import get_session
from app.api.schemas import TokenRequest, TokenResponse
from app.auth import exchange_api_key, mint_jwt
from app.logging import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/token", response_model=TokenResponse, summary="API key → JWT")
async def issue_token(body: TokenRequest, session: Session = Depends(get_session)) -> TokenResponse:
    api_key = exchange_api_key(session, body.api_key)
    if api_key is None:
        # Do not distinguish unknown vs inactive — avoid leaking key validity.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid api key")
    scopes = [s.strip() for s in api_key.scopes.split(",") if s.strip()]
    token, expires_in = mint_jwt(api_key.owner, scopes)
    log.info("auth.token_issued", owner=api_key.owner)
    return TokenResponse(access_token=token, expires_in=expires_in)
