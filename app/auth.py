"""JWT auth and API-key exchange.

Model: a consumer holds a raw **API key** (stored only as a SHA-256 hash in
[polling].[ApiKey]) and exchanges it at ``POST /auth/token`` for a short-lived **JWT**. Every
other request presents the JWT as a bearer token. Authorization is scope-based, with
ownership isolation (a non-admin token only touches jobs whose ``owner`` equals its ``sub``).

Signing: HS256 with a shared secret by default; RS256 via a configured public key (PEM) or a
remote JWKS is supported for validation when an external IdP mints the tokens (then
``/auth/token`` is not used).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from sqlmodel import Session, select

from app.config import Settings, get_settings
from app.logging import get_logger
from app.models import ApiKey
from app.state import utcnow

log = get_logger(__name__)

SCOPE_READ = "polling:read"
SCOPE_WRITE = "polling:write"
SCOPE_ADMIN = "admin"


class AuthError(Exception):
    """Token missing, invalid, expired, or lacking a required scope."""


def hash_key(raw_key: str) -> str:
    """SHA-256 hex of a raw API key. Keys are high-entropy, so a plain hash is sufficient."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Principal:
    sub: str
    scopes: frozenset[str]

    @property
    def is_admin(self) -> bool:
        return SCOPE_ADMIN in self.scopes

    def has_scope(self, scope: str) -> bool:
        return self.is_admin or scope in self.scopes

    def can_manage(self, owner: str) -> bool:
        """Admins manage any owner; others only their own."""
        return self.is_admin or self.sub == owner


# --- JWT -----------------------------------------------------------------------


def mint_jwt(sub: str, scopes: list[str], settings: Settings | None = None) -> tuple[str, int]:
    """Mint an HS256 JWT for a consumer. Returns (token, expires_in_seconds)."""
    settings = settings or get_settings()
    if settings.jwt_alg != "HS256" or not settings.jwt_secret:
        raise AuthError("token minting requires HS256 with a configured secret")
    ttl = settings.jwt_ttl_seconds
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": sub,
        "scopes": scopes,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "exp": now + timedelta(seconds=ttl),
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm="HS256")
    return token, ttl


def _signing_key(settings: Settings, token: str) -> str:
    if settings.jwt_alg == "HS256":
        if not settings.jwt_secret:
            raise AuthError("JWT secret not configured")
        return settings.jwt_secret
    # RS256/ES256: prefer a configured PEM, else fetch from JWKS.
    if settings.jwt_public_key:
        return settings.jwt_public_key
    if settings.jwt_jwks_url:
        jwks = jwt.PyJWKClient(settings.jwt_jwks_url)
        return jwks.get_signing_key_from_jwt(token).key
    raise AuthError("no verification key configured for asymmetric JWT")


def decode_jwt(token: str, settings: Settings | None = None) -> Principal:
    """Validate a JWT and return the Principal, or raise AuthError."""
    settings = settings or get_settings()
    options = {"require": ["exp"]}
    try:
        claims = jwt.decode(
            token,
            _signing_key(settings, token),
            algorithms=[settings.jwt_alg],
            audience=settings.jwt_audience or None,
            issuer=settings.jwt_issuer or None,
            options=options,
        )
    except jwt.PyJWTError as exc:
        raise AuthError(f"invalid token: {exc}") from exc
    sub = claims.get("sub")
    if not sub:
        raise AuthError("token missing sub")
    raw_scopes = claims.get("scopes") or claims.get("scope") or []
    if isinstance(raw_scopes, str):
        raw_scopes = raw_scopes.split()
    return Principal(sub=str(sub), scopes=frozenset(raw_scopes))


# --- API keys ------------------------------------------------------------------


def exchange_api_key(session: Session, raw_key: str) -> ApiKey | None:
    """Return the active ApiKey matching a raw key, or None."""
    key_hash = hash_key(raw_key)
    stmt = select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.isActive == True)  # noqa: E712
    return session.exec(stmt).first()


def ensure_bootstrap_admin(session: Session, settings: Settings | None = None) -> None:
    """Seed an admin API key from POLLING_BOOTSTRAP_ADMIN_KEY if no admin key exists yet."""
    settings = settings or get_settings()
    if not settings.bootstrap_admin_key:
        return
    key_hash = hash_key(settings.bootstrap_admin_key)
    existing = session.exec(select(ApiKey).where(ApiKey.key_hash == key_hash)).first()
    if existing is not None:
        return
    session.add(
        ApiKey(
            owner="admin",
            key_hash=key_hash,
            scopes=SCOPE_ADMIN,
            isActive=True,
            created_at=utcnow(),
        )
    )
    log.info("auth.bootstrap_admin_seeded")
