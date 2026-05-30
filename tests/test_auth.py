"""Auth dependency behaviour.

require_api_key validates a shared service token (constant-time) and
resolves the tenant from the X-Tenant-ID header. These tests exercise the
pure helpers; the DB lookup is covered by the live route tests.
"""

import uuid

import pytest
from fastapi import HTTPException

import app.auth as auth_mod
from app.auth import _extract_token, _resolve_tenant_id, _token_ok, validate_api_token


def _set(monkeypatch, *, token: str, env: str, default_tenant: str = ""):
    monkeypatch.setattr(auth_mod, "API_TOKEN", token)
    monkeypatch.setattr(auth_mod, "ENVIRONMENT", env)
    monkeypatch.setattr(auth_mod, "DEFAULT_TENANT_ID", default_tenant)


def test_extract_token_prefers_x_api_key():
    assert _extract_token("k1", "Bearer k2") == "k1"


def test_extract_token_reads_bearer():
    assert _extract_token(None, "Bearer abc") == "abc"
    assert _extract_token(None, "bearer abc") == "abc"


def test_extract_token_none_when_absent():
    assert _extract_token(None, None) is None
    assert _extract_token(None, "Basic xyz") is None


def test_token_ok_constant_time_match(monkeypatch):
    _set(monkeypatch, token="secret", env="production")
    assert _token_ok("secret") is True
    assert _token_ok("wrong") is False
    assert _token_ok(None) is False


def test_token_rejected_in_production_when_unset(monkeypatch):
    _set(monkeypatch, token="", env="production")
    assert _token_ok("anything") is False
    assert _token_ok(None) is False


def test_dev_open_when_no_token_set(monkeypatch):
    _set(monkeypatch, token="", env="development")
    assert _token_ok(None) is True
    assert _token_ok("anything") is True


def test_validate_api_token_matches_token_ok(monkeypatch):
    _set(monkeypatch, token="secret", env="production")
    assert validate_api_token("secret") is True
    assert validate_api_token("nope") is False


def test_resolve_tenant_id_parses_uuid(monkeypatch):
    _set(monkeypatch, token="x", env="production")
    tid = uuid.uuid4()
    assert _resolve_tenant_id(str(tid)) == tid


def test_resolve_tenant_id_missing_header(monkeypatch):
    _set(monkeypatch, token="x", env="production")
    with pytest.raises(HTTPException) as exc:
        _resolve_tenant_id(None)
    assert exc.value.status_code == 400


def test_resolve_tenant_id_bad_uuid(monkeypatch):
    _set(monkeypatch, token="x", env="production")
    with pytest.raises(HTTPException) as exc:
        _resolve_tenant_id("not-a-uuid")
    assert exc.value.status_code == 400


def test_resolve_tenant_id_dev_fallback(monkeypatch):
    fallback = str(uuid.uuid4())
    _set(monkeypatch, token="", env="development", default_tenant=fallback)
    assert str(_resolve_tenant_id(None)) == fallback
