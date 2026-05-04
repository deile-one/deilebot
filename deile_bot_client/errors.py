"""Typed exceptions raised by BotControlClient.

Server returns the canonical error envelope:

    {"error": {"code": "...", "message": "...", "details": {...}}}

Codes are mapped to subclasses below. Unknown codes fall back to
BotClientUpstreamError so callers always see a typed exception.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional


class BotClientError(Exception):
    """Base for every error raised by the control-plane client.

    `code` mirrors the server's error envelope code (or a synthetic one
    for transport errors). `details` is the structured `details` object
    when present, or an empty mapping. `status_code` is the HTTP status
    when applicable, or None for transport-only failures.
    """

    code: str = "BOT_CLIENT_ERROR"

    def __init__(
        self,
        message: str,
        *,
        code: Optional[str] = None,
        status_code: Optional[int] = None,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.status_code = status_code
        self.details: Mapping[str, Any] = dict(details) if details else {}

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"{type(self).__name__}(code={self.code!r}, status={self.status_code!r}, "
            f"message={self.message!r})"
        )


class BotClientAuthError(BotClientError):
    """401 / 403 — token missing, wrong, or insufficient."""

    code = "UNAUTHORIZED"


class BotClientNotReady(BotClientError):
    """503 — adapter not yet connected (e.g. discord client still booting)."""

    code = "NOT_READY"


class BotClientRateLimited(BotClientError):
    """429 — server-side rate limit hit. `retry_after_s` from headers when present."""

    code = "RATE_LIMITED"

    def __init__(
        self,
        message: str,
        *,
        retry_after_s: Optional[float] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.retry_after_s = retry_after_s


class BotClientTimeoutError(BotClientError):
    """The HTTP call exceeded the configured timeout."""

    code = "TIMEOUT"


class BotClientUpstreamError(BotClientError):
    """5xx, network errors, or server-reported UPSTREAM_ERROR (Discord side)."""

    code = "UPSTREAM_ERROR"


_CODE_TO_EXC: dict[str, type[BotClientError]] = {
    "UNAUTHORIZED": BotClientAuthError,
    "FORBIDDEN": BotClientAuthError,
    "NOT_READY": BotClientNotReady,
    "RATE_LIMITED": BotClientRateLimited,
    "UPSTREAM_ERROR": BotClientUpstreamError,
    "INTERNAL_ERROR": BotClientUpstreamError,
    "BAD_REQUEST": BotClientError,
    "NOT_FOUND": BotClientError,
    "TIMEOUT": BotClientTimeoutError,
}


def exception_for_code(code: str) -> type[BotClientError]:
    """Resolve an error envelope code into the matching exception class.

    Unknown codes return BotClientUpstreamError, so callers always have
    a typed exception to catch instead of a generic Exception.
    """
    return _CODE_TO_EXC.get(code, BotClientUpstreamError)
