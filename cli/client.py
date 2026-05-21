"""Network layer — HTTP REST client + socket.io client.

No imports from app/. All config comes from env vars.
"""

import os
from typing import Any

import httpx
import socketio
import socketio.exceptions


class APIError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail
        super().__init__(f"HTTP {status}: {detail}")


class APIClient:
    """Thin httpx wrapper for all REST calls."""

    def __init__(self, base_url: str, token: str) -> None:
        headers = {"X-API-Key": token} if token else {}
        self._http = httpx.Client(
            base_url=base_url,
            headers=headers,
            timeout=10.0,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get(self, path: str) -> Any:
        try:
            r = self._http.get(path)
        except httpx.TimeoutException:
            raise APIError(408, f"Request to {path} timed out (10s)")
        except httpx.ConnectError:
            raise APIError(503, "Cannot reach server — is it running?")
        if not r.is_success:
            try:
                detail = r.json().get("detail", r.text)
            except Exception:
                detail = r.text or r.reason_phrase
            raise APIError(r.status_code, str(detail))
        return r.json()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def health(self) -> bool:
        try:
            r = self._http.get("/healthz", timeout=3.0)
            return r.status_code == 200
        except Exception:
            return False

    def list_bots(self) -> list[dict]:
        return self._get("/bots")

    def list_chats(self) -> list[dict]:
        return self._get("/chats")

    def get_chat_history(self, chat_id: str) -> dict:
        return self._get(f"/chats/{chat_id}/messages")

    def close(self) -> None:
        self._http.close()


class SocketClient:
    """Synchronous socket.io client wrapping python-socketio SimpleClient."""

    def __init__(self, base_url: str, token: str) -> None:
        self._base_url = base_url
        self._token = token
        self._sio = socketio.SimpleClient()

    def connect(self) -> None:
        self._sio.connect(
            self._base_url,
            auth={"token": self._token},
        )

    def send(self, content: str, bot_id: int, chat_id: str) -> None:
        self._sio.emit("send_message", {
            "content": content,
            "role": "user",
            "bot_id": bot_id,
            "chat_id": chat_id,
        })

    def receive_reply(self, timeout: int = 90) -> tuple[str, str]:
        """Block until new_message or error arrives. Returns (role, content)."""
        while True:
            try:
                event = self._sio.receive(timeout=timeout)
            except socketio.exceptions.TimeoutError:
                raise TimeoutError("No reply received within timeout — server may be busy.")
            name, data = event[0], event[1]
            if name == "new_message":
                return data.get("role", "agent"), data.get("content", "")
            if name == "error":
                raise APIError(500, data.get("detail", "unknown socket error"))
            # Ignore any other events and keep waiting

    def disconnect(self) -> None:
        try:
            self._sio.disconnect()
        except Exception:
            pass
