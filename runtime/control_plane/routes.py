"""Route handlers for the deile-bot control-plane.

Each route maps a wire request (Pydantic-validated) onto an adapter
method. Errors from the adapter are translated to the canonical envelope.
A reference to a `ProviderAdapter` (by name) is fetched from the
`ControlPlaneServer.adapters` dict on each call so the daemon can rotate
adapters without restarting the server.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from aiohttp import web
from deile_bot_client.models import (ChannelPostRequest, ChannelPostResponse,
                                     DMSendRequest, DMSendResponse,
                                     HealthResponse, MessagePinRequest,
                                     MessagePinResponse, ReactionAddRequest,
                                     ReactionAddResponse, RoleMentionRequest,
                                     RoleMentionResponse, ThreadStartRequest,
                                     ThreadStartResponse, UserProfileResponse)
from pydantic import BaseModel, ValidationError

from deile_bot.foundation.envelope import BotUser, Channel, ChannelScope
from deile_bot.foundation.exceptions import (BotFoundationError,
                                             CapabilityNotSupported,
                                             PermissionDenied, ProviderError)

from .errors import json_error

logger = logging.getLogger("deile_bot.control_plane.routes")


def _get_adapter(request: web.Request, provider_name: str = "discord"):
    server = request.app["server"]
    adapter = server.adapters.get(provider_name)
    if adapter is None:
        return None
    return adapter


async def _parse_body(request: web.Request, model: type[BaseModel]) -> Optional[BaseModel]:
    """Validate JSON body against pydantic model. Returns None and raises
    a 400 response when invalid."""
    max_bytes = request.app["server"].settings.request_max_bytes
    raw = await request.content.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise _ResponseError(
            json_error("BAD_REQUEST", f"body exceeds {max_bytes} bytes", status=413)
        )
    if not raw:
        try:
            return model()  # all-optional models can succeed
        except ValidationError as e:
            raise _ResponseError(
                json_error("BAD_REQUEST", "missing body", status=400, details={"errors": e.errors()})
            )
    try:
        return model.model_validate_json(raw)
    except ValidationError as e:
        raise _ResponseError(
            json_error(
                "BAD_REQUEST",
                "invalid request body",
                status=400,
                details={"errors": e.errors(include_url=False)},
            )
        )


class _ResponseError(Exception):
    """Internal sentinel — wraps a prepared aiohttp response."""

    def __init__(self, response: web.Response):
        self.response = response


def _adapter_unavailable(request: web.Request) -> web.Response:
    return json_error(
        "NOT_READY",
        "discord adapter not registered or not ready",
        status=503,
    )


def _audit_outbound(request: web.Request, op: str, *, ok: bool, **payload: Any) -> None:
    server = request.app["server"]
    if not server.settings.log_requests:
        return
    extra = {"op": op, "ok": ok, **payload}
    if ok:
        logger.info("control_plane outbound", extra=extra)
    else:
        logger.warning("control_plane outbound failed", extra=extra)


async def _emit_audit(request: web.Request, event: str, payload: Mapping[str, Any]) -> None:
    """Hook into the BotAuditLogger if the daemon registered one on the server."""
    server = request.app["server"]
    audit = server.audit_logger
    if audit is None:
        return
    try:
        from deile_bot.foundation.audit import AuditEventType

        # Map control-plane event names to AuditEventType values; fall back
        # to OUTBOUND_SENT/OUTBOUND_FAILED so we always emit something.
        try:
            evt = AuditEventType(event)
        except ValueError:
            evt = AuditEventType.OUTBOUND_SENT if payload.get("ok") else AuditEventType.OUTBOUND_FAILED
        await audit.log(evt, payload=dict(payload))
    except Exception:  # pragma: no cover - audit must never break the request path
        logger.exception("audit emit failed")


def _channel(adapter, channel_id: str, scope: ChannelScope = ChannelScope.GROUP) -> Channel:
    return Channel(
        provider=adapter.name,
        provider_channel_id=str(channel_id),
        scope=scope,
    )


# ---- Handlers ---------------------------------------------------------------


async def health(request: web.Request) -> web.Response:
    server = request.app["server"]
    providers = list(server.adapters.keys())
    is_ready = all(
        getattr(a, "_client", None) is not None for a in server.adapters.values()
    )
    payload = HealthResponse(
        ok=True,
        version=server.version,
        providers=providers,
        is_ready=is_ready or not providers,
    )
    return web.json_response(payload.model_dump(mode="json"))


async def channel_post(request: web.Request) -> web.Response:
    try:
        body: ChannelPostRequest = await _parse_body(request, ChannelPostRequest)
    except _ResponseError as e:
        return e.response
    adapter = _get_adapter(request)
    if adapter is None:
        return _adapter_unavailable(request)
    try:
        msg_id = await adapter.send_message(
            _channel(adapter, body.channel_id),
            body.text,
            reply_to=body.reply_to,
        )
    except CapabilityNotSupported as e:
        await _emit_audit(request, "outbound_failed", {"op": "channel.post", "reason": "capability"})
        return json_error("UPSTREAM_ERROR", e.message, status=502)
    except (PermissionDenied, BotFoundationError) as e:
        await _emit_audit(request, "outbound_failed", {"op": "channel.post", "reason": "denied"})
        return json_error("FORBIDDEN", e.message, status=403)
    except ProviderError as e:
        await _emit_audit(request, "outbound_failed", {"op": "channel.post", "reason": "upstream"})
        return json_error("UPSTREAM_ERROR", e.message, status=502)
    except Exception:
        logger.exception("channel.post failed")
        return json_error("INTERNAL_ERROR", "unexpected failure", status=500)
    payload = ChannelPostResponse(
        message_id=str(msg_id),
        channel_id=str(body.channel_id),
        sent_at=datetime.now(timezone.utc),
    )
    await _emit_audit(
        request,
        "outbound_sent",
        {"op": "channel.post", "channel_id": body.channel_id, "message_id": str(msg_id)},
    )
    _audit_outbound(request, "channel.post", ok=True, channel_id=body.channel_id, message_id=str(msg_id))
    return web.json_response(payload.model_dump(mode="json"))


async def dm_send(request: web.Request) -> web.Response:
    try:
        body: DMSendRequest = await _parse_body(request, DMSendRequest)
    except _ResponseError as e:
        return e.response
    adapter = _get_adapter(request)
    if adapter is None:
        return _adapter_unavailable(request)

    # Resolve target user — either a provider id (snowflake) or our internal bot_user_id.
    target_provider_id: str
    if body.user_id:
        target_provider_id = str(body.user_id)
    else:
        identity = request.app["server"].identity
        if identity is None:
            return json_error(
                "BAD_REQUEST",
                "bot_user_id resolution not available (identity not wired)",
                status=400,
            )
        user = await identity.by_bot_user_id(str(body.bot_user_id))
        if user is None:
            return json_error("NOT_FOUND", "user not found", status=404)
        target_provider_id = user.provider_user_id

    user = BotUser(
        bot_user_id=body.bot_user_id or f"transient-{target_provider_id}",
        provider=adapter.name,
        provider_user_id=target_provider_id,
        display_name="(unknown)",
    )
    try:
        msg_id = await adapter.send_dm(user, body.text)
    except CapabilityNotSupported as e:
        await _emit_audit(request, "outbound_failed", {"op": "dm.send", "reason": "capability"})
        return json_error("UPSTREAM_ERROR", e.message, status=502)
    except ProviderError as e:
        await _emit_audit(request, "outbound_failed", {"op": "dm.send", "reason": "upstream"})
        return json_error("UPSTREAM_ERROR", e.message, status=502)
    except (PermissionDenied, BotFoundationError) as e:
        await _emit_audit(request, "outbound_failed", {"op": "dm.send", "reason": "denied"})
        return json_error("FORBIDDEN", e.message, status=403)
    except Exception:
        logger.exception("dm.send failed")
        return json_error("INTERNAL_ERROR", "unexpected failure", status=500)
    payload = DMSendResponse(
        message_id=str(msg_id),
        user_id=target_provider_id,
        sent_at=datetime.now(timezone.utc),
    )
    await _emit_audit(
        request,
        "outbound_sent",
        {"op": "dm.send", "user_id": target_provider_id, "message_id": str(msg_id)},
    )
    _audit_outbound(request, "dm.send", ok=True, user_id=target_provider_id, message_id=str(msg_id))
    return web.json_response(payload.model_dump(mode="json"))


async def reaction_add(request: web.Request) -> web.Response:
    try:
        body: ReactionAddRequest = await _parse_body(request, ReactionAddRequest)
    except _ResponseError as e:
        return e.response
    adapter = _get_adapter(request)
    if adapter is None:
        return _adapter_unavailable(request)
    try:
        await adapter.react(_channel(adapter, body.channel_id), body.message_id, body.emoji)
    except CapabilityNotSupported as e:
        return json_error("UPSTREAM_ERROR", e.message, status=502)
    except ProviderError as e:
        return json_error("UPSTREAM_ERROR", e.message, status=502)
    except Exception:
        logger.exception("reaction.add failed")
        return json_error("INTERNAL_ERROR", "unexpected failure", status=500)
    await _emit_audit(
        request,
        "outbound_sent",
        {"op": "reaction.add", "channel_id": body.channel_id, "message_id": body.message_id},
    )
    _audit_outbound(request, "reaction.add", ok=True, channel_id=body.channel_id)
    return web.json_response(ReactionAddResponse().model_dump(mode="json"))


async def thread_start(request: web.Request) -> web.Response:
    try:
        body: ThreadStartRequest = await _parse_body(request, ThreadStartRequest)
    except _ResponseError as e:
        return e.response
    adapter = _get_adapter(request)
    if adapter is None:
        return _adapter_unavailable(request)
    try:
        client = getattr(adapter, "_client", None)
        if client is None:
            return _adapter_unavailable(request)
        ch = client.get_channel(int(body.channel_id))
        if ch is None:
            ch = await client.fetch_channel(int(body.channel_id))
        if body.parent_message_id:
            msg = await ch.fetch_message(int(body.parent_message_id))
            thread = await msg.create_thread(name=body.name[:100])
        else:
            # Public thread without an anchor message — discord.py exposes
            # `create_thread` on TextChannel for this purpose.
            create_thread = getattr(ch, "create_thread", None)
            if create_thread is None:
                return json_error(
                    "UPSTREAM_ERROR",
                    "channel does not support thread creation without parent message",
                    status=502,
                )
            thread = await create_thread(name=body.name[:100])
    except Exception as e:
        logger.exception("thread.start failed")
        return json_error("UPSTREAM_ERROR", f"thread.start failed: {e}", status=502)
    payload = ThreadStartResponse(thread_id=str(thread.id), name=body.name)
    await _emit_audit(
        request,
        "outbound_sent",
        {"op": "thread.start", "channel_id": body.channel_id, "thread_id": str(thread.id)},
    )
    return web.json_response(payload.model_dump(mode="json"))


async def message_pin(request: web.Request) -> web.Response:
    try:
        body: MessagePinRequest = await _parse_body(request, MessagePinRequest)
    except _ResponseError as e:
        return e.response
    adapter = _get_adapter(request)
    if adapter is None:
        return _adapter_unavailable(request)
    try:
        client = getattr(adapter, "_client", None)
        if client is None:
            return _adapter_unavailable(request)
        ch = client.get_channel(int(body.channel_id))
        if ch is None:
            ch = await client.fetch_channel(int(body.channel_id))
        msg = await ch.fetch_message(int(body.message_id))
        await msg.pin()
    except Exception as e:
        logger.exception("message.pin failed")
        return json_error("UPSTREAM_ERROR", f"message.pin failed: {e}", status=502)
    await _emit_audit(
        request,
        "outbound_sent",
        {"op": "message.pin", "channel_id": body.channel_id, "message_id": body.message_id},
    )
    return web.json_response(MessagePinResponse().model_dump(mode="json"))


async def role_mention(request: web.Request) -> web.Response:
    try:
        body: RoleMentionRequest = await _parse_body(request, RoleMentionRequest)
    except _ResponseError as e:
        return e.response
    adapter = _get_adapter(request)
    if adapter is None:
        return _adapter_unavailable(request)
    try:
        client = getattr(adapter, "_client", None)
        if client is None:
            return _adapter_unavailable(request)
        ch = client.get_channel(int(body.channel_id))
        if ch is None:
            ch = await client.fetch_channel(int(body.channel_id))
        content = f"<@&{int(body.role_id)}> {body.text}".strip()
        msg = await ch.send(content=content)
    except Exception as e:
        logger.exception("role.mention failed")
        return json_error("UPSTREAM_ERROR", f"role.mention failed: {e}", status=502)
    payload = RoleMentionResponse(message_id=str(msg.id))
    await _emit_audit(
        request,
        "outbound_sent",
        {"op": "role.mention", "channel_id": body.channel_id, "message_id": str(msg.id)},
    )
    return web.json_response(payload.model_dump(mode="json"))


async def get_user(request: web.Request) -> web.Response:
    user_id = request.match_info.get("user_id", "")
    if not user_id:
        return json_error("BAD_REQUEST", "user_id required", status=400)
    adapter = _get_adapter(request)
    if adapter is None:
        return _adapter_unavailable(request)
    try:
        client = getattr(adapter, "_client", None)
        if client is None:
            return _adapter_unavailable(request)
        u = await client.fetch_user(int(user_id))
    except Exception as e:
        return json_error("NOT_FOUND", f"user lookup failed: {e}", status=404)
    payload = UserProfileResponse(
        user_id=str(u.id),
        username=str(getattr(u, "name", "")),
        display_name=getattr(u, "global_name", None),
        avatar_url=str(u.display_avatar.url) if getattr(u, "display_avatar", None) else None,
        is_bot=bool(getattr(u, "bot", False)),
    )
    return web.json_response(payload.model_dump(mode="json"))


def register_routes(app: web.Application) -> None:
    app.router.add_get("/v1/health", health)
    app.router.add_post("/v1/outbound/discord/channel.post", channel_post)
    app.router.add_post("/v1/outbound/discord/dm.send", dm_send)
    app.router.add_post("/v1/outbound/discord/reaction.add", reaction_add)
    app.router.add_post("/v1/outbound/discord/thread.start", thread_start)
    app.router.add_post("/v1/outbound/discord/message.pin", message_pin)
    app.router.add_post("/v1/outbound/discord/role.mention", role_mention)
    app.router.add_get("/v1/users/{user_id}", get_user)
