"""API auth — single-tenant stub.

AUTH IS CURRENTLY DISABLED. All requests resolve to DEFAULT_TENANT_ID.

To enable per-tenant token auth later, replace `require_api_key` with
the commented-out implementation below and set AUTH_ENABLED=true in env.
The rest of the codebase (routes, socket, schema) is already wired for
multi-tenant — only this file needs to change.
"""

import hmac
import logging

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.config import DEFAULT_TENANT_ID
from app.database import get_db

logger = logging.getLogger("auth")


def get_tenant_by_token(token: str | None, db: Session):
    """Return the Tenant whose api_token matches, or None."""
    if not token:
        return None
    from app import models
    return db.query(models.Tenant).filter(models.Tenant.api_token == token).first()


def validate_api_token(token: str | None) -> bool:
    """Always valid while auth is disabled."""
    return True


def require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    """FastAPI dependency. Returns the active Tenant.

    Currently returns DEFAULT_TENANT_ID tenant unconditionally.
    To enable auth: set AUTH_ENABLED=true in env and swap this body
    for the per-tenant token lookup (get_tenant_by_token).
    """
    from app import models
    # TODO: enable per-tenant auth — replace this with token lookup
    tenant = db.query(models.Tenant).filter(
        models.Tenant.id == DEFAULT_TENANT_ID
    ).first()
    if not tenant:
        raise HTTPException(status_code=500, detail="default tenant not configured")
    return tenant
