"""Auth dependency behaviour."""

import os

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient


def _build_app_with_token(token: str | None, environment: str = "production"):
    # Reload modules so config picks up the patched env. importlib.reload
    # is fine here because the modules are otherwise stateless.
    import importlib

    if token is None:
        os.environ.pop("API_TOKEN", None)
    else:
        os.environ["API_TOKEN"] = token
    os.environ["ENVIRONMENT"] = environment

    import app.config as cfg
    importlib.reload(cfg)
    import app.auth as auth
    importlib.reload(auth)

    app = FastAPI()

    @app.get("/secure", dependencies=[__import__("fastapi").Depends(auth.require_api_key)])
    def secure():
        return {"ok": True}

    return app, auth


def test_missing_token_rejected_in_production():
    app, _ = _build_app_with_token("s3cret", environment="production")
    client = TestClient(app)
    assert client.get("/secure").status_code == 401


def test_x_api_key_accepted():
    app, _ = _build_app_with_token("s3cret", environment="production")
    client = TestClient(app)
    assert client.get("/secure", headers={"X-API-Key": "s3cret"}).status_code == 200


def test_bearer_accepted():
    app, _ = _build_app_with_token("s3cret", environment="production")
    client = TestClient(app)
    assert client.get(
        "/secure", headers={"Authorization": "Bearer s3cret"}
    ).status_code == 200


def test_wrong_token_rejected():
    app, _ = _build_app_with_token("s3cret", environment="production")
    client = TestClient(app)
    assert client.get("/secure", headers={"X-API-Key": "nope"}).status_code == 401


def test_dev_open_when_no_token_set():
    app, _ = _build_app_with_token(None, environment="development")
    client = TestClient(app)
    # No token configured + ENVIRONMENT=development -> auth fails open
    assert client.get("/secure").status_code == 200


@pytest.fixture(autouse=True)
def _restore_env():
    yield
    # Restore the canonical app modules so subsequent test files see the
    # config they expect.
    import importlib
    import app.config
    import app.auth
    importlib.reload(app.config)
    importlib.reload(app.auth)
