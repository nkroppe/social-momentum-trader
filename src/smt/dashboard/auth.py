"""Bearer-token auth for the dashboard API."""

from __future__ import annotations

import hmac
import ipaddress
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_bearer = HTTPBearer(auto_error=False)


def is_loopback_host(host: str) -> bool:
    raw = host.strip().lower()
    if raw in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ipaddress.ip_address(raw).is_loopback
    except ValueError:
        return False


def auth_required(*, require_auth: bool, bind_host: str) -> bool:
    return require_auth or not is_loopback_host(bind_host)


def verify_dashboard_token(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    if not getattr(request.app.state, "require_auth", True):
        return
    expected = str(getattr(request.app.state, "dashboard_token", "") or "")
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="dashboard auth is not configured",
        )
    got = credentials.credentials if credentials is not None else ""
    if not got or not hmac.compare_digest(got, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )
