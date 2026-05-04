"""BotControlClient — async HTTP client for the deile-bot control-plane.

Design notes:

- httpx.AsyncClient is reused across calls; the user MUST call
  `await close()` (or use `async with`) to release the pool.
- Retries via tenacity on 5xx and transport errors only. 4xx (auth,
  bad request, not found) are NOT retried — they are deterministic.
- 429 carries `Retry-After` from the server; the client honours it
  for *one* automatic retry, then raises BotClientRateLimited.
- Tokens never appear in log lines or exception messages.
- Body sizes are capped before parse to avoid memory blowups when a
  daemon misbehaves.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping, Optional, Type, TypeVar

import httpx
from pydantic import BaseModel
from tenacity import (AsyncRetrying, retry_if_exception_type,
                      stop_after_attempt, wait_exponential)

try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
    _HAVE_PYDANTIC_SETTINGS = True
except ImportError:  # pragma: no cover
    from pydantic import BaseSettings  # type: ignore[attr-defined]
    SettingsConfigDict = None  # type: ignore[assignment]
    _HAVE_PYDANTIC_SETTINGS = False

from .errors import (BotClientError, BotClientRateLimited,
                     BotClientTimeoutError, BotClientUpstreamError,
                     exception_for_code)
from .models import (ChannelPostRequest, ChannelPostResponse, DMSendRequest,
                     DMSendResponse, HealthResponse, MessagePinRequest,
                     MessagePinResponse, ReactionAddRequest,
                     ReactionAddResponse, RoleMentionRequest,
                     RoleMentionResponse, ThreadStartRequest,
                     ThreadStartResponse, UserProfileResponse)

logger = logging.getLogger("deile_bot_client")

_MAX_RESPONSE_BYTES = 256 * 1024  # 256 KiB safety cap for control-plane responses
_DEFAULT_TIMEOUT_S = 10.0
_RETRY_ATTEMPTS = 3

R = TypeVar("R", bound=BaseModel)


class BotControlSettings(BaseSettings):
    """Configuration for BotControlClient.

    Loaded from env vars (prefix `DEILE_BOT_CONTROL_`) or instantiated
    explicitly. The auth token is a SecretStr-equivalent — repr never
    shows it.
    """

    endpoint: str = "http://127.0.0.1:8765"
    auth_token: str = ""
    timeout_s: float = _DEFAULT_TIMEOUT_S
    retry_attempts: int = _RETRY_ATTEMPTS
    user_agent: str = "deile-bot-client/0.1"

    if _HAVE_PYDANTIC_SETTINGS:
        model_config = SettingsConfigDict(
            env_prefix="DEILE_BOT_CONTROL_",
            env_file=".env",
            extra="ignore",
        )
    else:  # pragma: no cover - legacy fallback
        class Config:
            env_prefix = "DEILE_BOT_CONTROL_"
            env_file = ".env"

    def __repr__(self) -> str:  # pragma: no cover
        token_preview = "***" if self.auth_token else "<unset>"
        return (
            f"BotControlSettings(endpoint={self.endpoint!r}, auth_token={token_preview}, "
            f"timeout_s={self.timeout_s})"
        )


class BotControlClient:
    """Async client for the deile-bot control-plane.

    Lifecycle:

        client = BotControlClient(BotControlSettings(endpoint=..., auth_token=...))
        try:
            res = await client.discord_channel_post(channel_id="...", text="hi")
        finally:
            await client.aclose()

    Or as context manager:

        async with BotControlClient(settings) as client:
            await client.health()
    """

    def __init__(
        self,
        settings: Optional[BotControlSettings] = None,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.settings = settings or BotControlSettings()
        if not self.settings.endpoint:
            raise ValueError("BotControlSettings.endpoint must be non-empty")
        headers = {
            "User-Agent": self.settings.user_agent,
            "Accept": "application/json",
        }
        if self.settings.auth_token:
            headers["Authorization"] = f"Bearer {self.settings.auth_token}"
        self._http = httpx.AsyncClient(
            base_url=self.settings.endpoint.rstrip("/"),
            timeout=httpx.Timeout(self.settings.timeout_s),
            headers=headers,
            transport=transport,
        )
        self._closed = False

    async def __aenter__(self) -> "BotControlClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if not self._closed:
            await self._http.aclose()
            self._closed = True

    # ---- Public endpoints ---------------------------------------------------

    async def health(self) -> HealthResponse:
        return await self._request("GET", "/v1/health", None, HealthResponse, retry=False)

    async def discord_channel_post(
        self, *, channel_id: str, text: str, reply_to: Optional[str] = None
    ) -> ChannelPostResponse:
        return await self._request(
            "POST",
            "/v1/outbound/discord/channel.post",
            ChannelPostRequest(channel_id=channel_id, text=text, reply_to=reply_to),
            ChannelPostResponse,
        )

    async def discord_dm_send(
        self,
        *,
        text: str,
        user_id: Optional[str] = None,
        bot_user_id: Optional[str] = None,
    ) -> DMSendResponse:
        return await self._request(
            "POST",
            "/v1/outbound/discord/dm.send",
            DMSendRequest(user_id=user_id, bot_user_id=bot_user_id, text=text),
            DMSendResponse,
        )

    async def discord_reaction_add(
        self, *, channel_id: str, message_id: str, emoji: str
    ) -> ReactionAddResponse:
        return await self._request(
            "POST",
            "/v1/outbound/discord/reaction.add",
            ReactionAddRequest(channel_id=channel_id, message_id=message_id, emoji=emoji),
            ReactionAddResponse,
        )

    async def discord_thread_start(
        self,
        *,
        channel_id: str,
        name: str,
        parent_message_id: Optional[str] = None,
    ) -> ThreadStartResponse:
        return await self._request(
            "POST",
            "/v1/outbound/discord/thread.start",
            ThreadStartRequest(channel_id=channel_id, name=name, parent_message_id=parent_message_id),
            ThreadStartResponse,
        )

    async def discord_message_pin(
        self, *, channel_id: str, message_id: str
    ) -> MessagePinResponse:
        return await self._request(
            "POST",
            "/v1/outbound/discord/message.pin",
            MessagePinRequest(channel_id=channel_id, message_id=message_id),
            MessagePinResponse,
        )

    async def discord_role_mention(
        self, *, channel_id: str, role_id: str, text: str = ""
    ) -> RoleMentionResponse:
        return await self._request(
            "POST",
            "/v1/outbound/discord/role.mention",
            RoleMentionRequest(channel_id=channel_id, role_id=role_id, text=text),
            RoleMentionResponse,
        )

    async def get_user_profile(self, user_id: str) -> UserProfileResponse:
        if not user_id or "/" in user_id:
            raise ValueError("invalid user_id")
        return await self._request(
            "GET", f"/v1/users/{user_id}", None, UserProfileResponse
        )

    # ---- Internal: request + retry plumbing ---------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        body: Optional[BaseModel],
        response_model: Type[R],
        *,
        retry: bool = True,
    ) -> R:
        if self._closed:
            raise BotClientError("client is closed", code="CLIENT_CLOSED")

        json_body = body.model_dump(mode="json") if body is not None else None

        async def _do() -> httpx.Response:
            try:
                return await self._http.request(method, path, json=json_body)
            except httpx.TimeoutException as e:
                raise BotClientTimeoutError(
                    f"control-plane timeout for {method} {path}"
                ) from e
            except httpx.HTTPError as e:
                raise BotClientUpstreamError(
                    f"transport error: {type(e).__name__}"
                ) from e

        if retry:
            attempts = max(1, int(self.settings.retry_attempts))
            response = None
            try:
                async for attempt in AsyncRetrying(
                    stop=stop_after_attempt(attempts),
                    wait=wait_exponential(multiplier=0.5, min=0.5, max=2.0),
                    retry=retry_if_exception_type(
                        (BotClientTimeoutError, _RetryableUpstream)
                    ),
                    reraise=True,
                ):
                    with attempt:
                        response = await _do()
                        if response.status_code >= 500:
                            raise _RetryableUpstream(
                                f"server returned {response.status_code}"
                            )
            except _RetryableUpstream:
                # Retries exhausted on a 5xx — fall through to _handle_response
                # so the caller sees the canonical typed error envelope mapping.
                if response is None:
                    raise BotClientUpstreamError("retries exhausted; no response captured")
            assert response is not None
        else:
            response = await _do()

        return self._handle_response(response, response_model, method=method, path=path)

    def _handle_response(
        self,
        response: httpx.Response,
        response_model: Type[R],
        *,
        method: str,
        path: str,
    ) -> R:
        status = response.status_code
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise BotClientUpstreamError(
                f"response from {path} exceeded {_MAX_RESPONSE_BYTES} bytes",
                status_code=status,
            )
        try:
            data = response.json()
        except json.JSONDecodeError as e:
            raise BotClientUpstreamError(
                f"non-JSON response from {path}",
                status_code=status,
            ) from e

        if 200 <= status < 300:
            try:
                return response_model.model_validate(data)
            except Exception as e:
                raise BotClientUpstreamError(
                    f"response did not match {response_model.__name__}: {e}",
                    status_code=status,
                ) from e

        # Error path — try to parse the canonical envelope.
        envelope: Mapping[str, Any] = (
            data.get("error") if isinstance(data, dict) else {}
        ) or {}
        code = str(envelope.get("code") or self._fallback_code(status))
        message = str(envelope.get("message") or f"HTTP {status}")
        details = envelope.get("details") if isinstance(envelope.get("details"), dict) else {}

        exc_cls = exception_for_code(code)
        if exc_cls is BotClientRateLimited:
            retry_after_header = response.headers.get("Retry-After")
            try:
                retry_after_s = float(retry_after_header) if retry_after_header else None
            except ValueError:
                retry_after_s = None
            raise BotClientRateLimited(
                message,
                status_code=status,
                code=code,
                details=details,
                retry_after_s=retry_after_s,
            )
        raise exc_cls(message, status_code=status, code=code, details=details)

    @staticmethod
    def _fallback_code(status: int) -> str:
        if status == 401:
            return "UNAUTHORIZED"
        if status == 403:
            return "FORBIDDEN"
        if status == 404:
            return "NOT_FOUND"
        if status == 429:
            return "RATE_LIMITED"
        if status == 503:
            return "NOT_READY"
        if 500 <= status < 600:
            return "INTERNAL_ERROR"
        return "BAD_REQUEST"


class _RetryableUpstream(Exception):
    """Internal sentinel — used by tenacity to know which 5xx triggers a retry."""
