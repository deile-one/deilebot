"""Bearer-token authentication middleware for the control-plane.

Compares tokens with hmac.compare_digest to avoid timing attacks. The
token never appears in logs.
"""

from __future__ import annotations

import hmac
from typing import Awaitable, Callable

from aiohttp import web

from .errors import json_error

_PUBLIC_PATHS = {"/v1/health"}


def make_auth_middleware(expected_token: str) -> web.middleware:
    """Return an aiohttp middleware that enforces Bearer auth on non-public paths."""

    if not expected_token:
        # Misconfiguration: refuse to start instead of running unprotected.
        raise ValueError("control-plane auth_token must be configured")

    @web.middleware
    async def auth_mw(request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]) -> web.StreamResponse:
        if request.path in _PUBLIC_PATHS:
            return await handler(request)

        header = request.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return json_error(
                "UNAUTHORIZED",
                "missing or malformed Authorization header",
                status=401,
            )
        provided = header[len(prefix):]
        if not hmac.compare_digest(provided, expected_token):
            return json_error("UNAUTHORIZED", "invalid token", status=401)
        return await handler(request)

    return auth_mw
