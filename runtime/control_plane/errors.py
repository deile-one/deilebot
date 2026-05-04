"""Canonical error envelope for the control-plane.

All non-2xx responses match:

    {"error": {"code": "...", "message": "...", "details": {...}}}

Codes: UNAUTHORIZED, FORBIDDEN, NOT_FOUND, BAD_REQUEST, RATE_LIMITED,
UPSTREAM_ERROR, NOT_READY, INTERNAL_ERROR.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from aiohttp import web


def json_error(
    code: str,
    message: str,
    *,
    status: int,
    details: Optional[Mapping[str, Any]] = None,
    headers: Optional[Mapping[str, str]] = None,
) -> web.Response:
    payload = {
        "error": {
            "code": code,
            "message": message,
            "details": dict(details) if details else {},
        }
    }
    return web.json_response(payload, status=status, headers=dict(headers) if headers else None)
