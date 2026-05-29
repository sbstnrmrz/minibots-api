"""API auth — service-token + tenant resolution.

This service sits behind a trusted main API. Every request must carry:

  1. A shared service token (X-API-Key or Authorization: Bearer …) that is
     compared in constant time against the API_TOKEN env var.
  2. An X-Tenant-ID header identifying which tenant the request is acting on.
     The value is the crazyagents organization ID. The main API is responsible
     for authenticating the end user and setting this header.

`require_api_key` validates the token, looks the tenant up by id, and
returns the Tenant ORM object — the same return type the rest of the
codebase already depends on, so no router changes are required.

`require_service_token` validates only the token — used by provisioning
endpoints (e.g. POST /tenants) that run before a tenant row exists.

Local-dev escape hatch: with ENVIRONMENT=development AND an empty
API_TOKEN, the token check falls open so local work isn't blocked. The
tenant header is still required (it identifies the row to operate on);
DEFAULT_TENANT_ID is used as a fallback only in that mode.
"""

import hmac
import logging

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.config import API_TOKEN, DEFAULT_TENANT_ID, ENVIRONMENT
from app.database import get_db

logger = logging.getLogger("auth")


def _extract_token(x_api_key: str | None, authorization: str | None) -> str | None:
    """Pull the service token from X-API-Key or 'Authorization: Bearer …'."""
    if x_api_key:
        return x_api_key
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def _token_ok(token: str | None) -> bool:
    """Constant-time comparison of the presented token against API_TOKEN.

    Dev fall-open: when ENVIRONMENT=development and API_TOKEN is empty, any
    (or no) token is accepted so local work isn't blocked.
    """
    if not API_TOKEN:
        if ENVIRONMENT == "development":
            return True
        # Misconfiguration in production: refuse rather than fail open.
        logger.error("API_TOKEN is empty outside development; refusing all requests")
        return False
    if not token:
        return False
    return hmac.compare_digest(token, API_TOKEN)


def validate_api_token(token: str | None) -> bool:
    """Real token validation. Used by non-FastAPI callers (e.g. socket connect)."""
    return _token_ok(token)


def _resolve_tenant_id(x_tenant_id: str | None) -> str:
    """Extract the X-Tenant-ID header (the crazyagents organization ID).

    In development with no header we fall back to DEFAULT_TENANT_ID (if set)
    so local tooling doesn't need to pass the header on every call.
    """
    raw = x_tenant_id
    if not raw and ENVIRONMENT == "development" and DEFAULT_TENANT_ID:
        raw = DEFAULT_TENANT_ID
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="missing X-Tenant-ID header",
        )
    return raw.strip()


def require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-ID"),
    db: Session = Depends(get_db),
):
    """FastAPI dependency. Authenticate the service call and return the Tenant.

    - 401 if the service token is missing or wrong.
    - 400 if X-Tenant-ID is absent.
    - 403 if the tenant id resolves to no row.
    """
    from app import models

    token = _extract_token(x_api_key, authorization)
    if not _token_ok(token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing API token",
        )

    tenant_id = _resolve_tenant_id(x_tenant_id)
    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="unknown tenant",
        )
    return tenant


def require_service_token(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
):
    """FastAPI dependency. Validates only the service token, no tenant lookup.

    Used by provisioning endpoints (e.g. POST /tenants) that run before
    a tenant row exists.

    - 401 if the service token is missing or wrong.
    """
    token = _extract_token(x_api_key, authorization)
    if not _token_ok(token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing API token",
        )
