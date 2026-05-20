"""Single-token API auth.

Stop-gap until per-tenant credentials land. Every protected route depends
on `require_api_key`; the socket.io `connect` handler calls
`validate_api_token` against the `auth` arg sent by the client.

Token is read from one of, in priority order:
  1. `X-API-Key` header
  2. `Authorization: Bearer <token>` header

A missing `API_TOKEN` env var fails closed in production and open in
development to avoid blocking local work.
"""

import hmac
import logging

from fastapi import Header, HTTPException, status

from app.config import API_TOKEN, ENVIRONMENT

logger = logging.getLogger("auth")


def _token_configured() -> bool:
    return bool(API_TOKEN)


def _constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def validate_api_token(token: str | None) -> bool:
    """Return True if `token` matches the configured API_TOKEN.

    In development with no API_TOKEN configured, return True so the dev
    server stays usable. In any other case a missing token is rejected.
    """
    if not _token_configured():
        if ENVIRONMENT == "development":
            return True
        return False
    if not token:
        return False
    return _constant_time_eq(token, API_TOKEN)


def require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> None:
    """FastAPI dependency. Raises 401 unless the request carries a valid token."""
    token = x_api_key
    if not token and authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer":
            token = value.strip() or None
    if not validate_api_token(token):
        logger.warning("rejected request: invalid or missing API token")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing API token",
            headers={"WWW-Authenticate": "Bearer"},
        )
