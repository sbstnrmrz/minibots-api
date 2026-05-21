"""Per-tenant API auth.

Each tenant has a unique `api_token` stored in the DB. `require_api_key`
resolves the token to a Tenant object and returns it so any route can
scope queries to `current_tenant.id` without a second DB round-trip.

Token is read from, in priority order:
  1. `X-API-Key` header
  2. `Authorization: Bearer <token>` header

Dev fallback: if `ENVIRONMENT == "development"` and no token is sent,
the dependency resolves to the `DEFAULT_TENANT_ID` tenant so local work
is never blocked by missing credentials.
"""

import hmac
import logging

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.config import API_TOKEN, DEFAULT_TENANT_ID, ENVIRONMENT
from app.database import get_db

logger = logging.getLogger("auth")


def _extract_token(
    x_api_key: str | None,
    authorization: str | None,
) -> str | None:
    if x_api_key:
        return x_api_key
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer":
            return value.strip() or None
    return None


def get_tenant_by_token(token: str | None, db: Session):
    """Return the Tenant whose api_token matches, or None."""
    if not token:
        return None
    from app import models
    return db.query(models.Tenant).filter(models.Tenant.api_token == token).first()


def validate_api_token(token: str | None) -> bool:
    """Socket-compatible check against the env API_TOKEN (legacy path).

    Used by socket.io connect before the DB session is available.
    Per-tenant socket auth is handled separately via get_tenant_by_token.
    """
    if not API_TOKEN:
        return ENVIRONMENT == "development"
    if not token:
        return False
    return hmac.compare_digest(token.encode("utf-8"), API_TOKEN.encode("utf-8"))


def require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    """FastAPI dependency. Resolves the request token to a Tenant.

    Raises 401 if no valid token is found. Returns the Tenant object so
    routes can filter by `current_tenant.id` directly.
    """
    from app import models

    token = _extract_token(x_api_key, authorization)
    tenant = get_tenant_by_token(token, db)
    if tenant:
        return tenant

    # Dev fallback: env API_TOKEN match → resolve DEFAULT_TENANT_ID
    if token and API_TOKEN and hmac.compare_digest(
        token.encode("utf-8"), API_TOKEN.encode("utf-8")
    ):
        tenant = db.query(models.Tenant).filter(
            models.Tenant.id == DEFAULT_TENANT_ID
        ).first()
        if tenant:
            return tenant

    # Development with no token configured — only open when no token was sent
    if not token and not API_TOKEN and ENVIRONMENT == "development":
        tenant = db.query(models.Tenant).filter(
            models.Tenant.id == DEFAULT_TENANT_ID
        ).first()
        if tenant:
            return tenant

    logger.warning("rejected request: no tenant matched the provided token")
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid or missing API token",
        headers={"WWW-Authenticate": "Bearer"},
    )
