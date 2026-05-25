"""Route handlers for the deilebot control-plane.

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
from deilebot_client.models import (ChannelPostRequest, ChannelPostResponse,
                                     DMSendRequest, DMSendResponse,
                                     HealthResponse, MessageEditRequest,
                                     MessageEditResponse, MessagePinRequest,
                                     MessagePinResponse, ReactionAddRequest,
                                     ReactionAddResponse, RoleMentionRequest,
                                     RoleMentionResponse, ThreadStartRequest,
                                     ThreadStartResponse, UserProfileResponse,
                                     WhatsAppSendTemplateRequest,
                                     WhatsAppSendTemplateResponse)
from pydantic import BaseModel, ValidationError

from deilebot.foundation.envelope import (BotUser, Channel, ChannelScope,
                                           TemplateMessage)
from deilebot.foundation.exceptions import (BotFoundationError,
                                             CapabilityNotSupported,
                                             PermissionDenied, ProviderError)

from .errors import json_error

logger = logging.getLogger("deilebot.control_plane.routes")


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


def _adapter_unavailable(
    request: web.Request, provider: str = "discord"
) -> web.Response:
    return json_error(
        "NOT_READY",
        f"{provider} adapter not registered or not ready",
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
        from deilebot.foundation.audit import AuditEventType

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


async def _persist_outbound(
    request: web.Request,
    *,
    provider: str,
    channel_id: str,
    message_id: str,
    text: str,
    reply_to: Optional[str] = None,
) -> None:
    """Persist a control-plane-posted message into the ConversationStore.

    The deile-worker posts its live status message through the control
    plane; without this the message the user sees on Discord would never
    enter the ``message`` table — it would be missing from ``/historico``
    and from the agent's conversation window. Best-effort: a store failure
    must never break the already-completed send.
    """
    server = request.app["server"]
    store = getattr(server, "store", None)
    if store is None:
        return
    try:
        bot_user_id = await store.get_last_inbound_bot_user_id(
            provider, str(channel_id)
        )
        if not bot_user_id:
            # No inbound in this channel → no conversation partner to
            # attribute the row to (the FK to bot_user would fail).
            logger.debug(
                "control_plane: skip outbound persist, no inbound user for channel %s",
                channel_id,
            )
            return
        await store.record_outbound(
            provider=provider,
            channel=Channel(
                provider=provider,
                provider_channel_id=str(channel_id),
                scope=ChannelScope.GROUP,
            ),
            provider_message_id=str(message_id),
            bot_user_id=bot_user_id,
            text=text,
            reply_to=reply_to,
            sent_at=datetime.now(timezone.utc),
        )
    except Exception:  # pragma: no cover - persistence must never break a send
        logger.exception("control_plane: outbound persist failed")


async def _persist_message_edit(
    request: web.Request,
    *,
    provider: str,
    channel_id: str,
    message_id: str,
    text: str,
) -> None:
    """Sync an edited message's text into the ConversationStore.

    The worker edits its status message live (progress, then the final
    result); each edit updates the stored row so the DB mirrors what the
    user sees on screen. A no-op when the message is not stored.
    Best-effort.
    """
    server = request.app["server"]
    store = getattr(server, "store", None)
    if store is None:
        return
    try:
        await store.update_message_text(
            provider, str(channel_id), str(message_id), text
        )
    except Exception:  # pragma: no cover - persistence must never break an edit
        logger.exception("control_plane: message-edit persist failed")


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
    except ProviderError as e:
        # ORDEM: ProviderError ANTES do catch-all (PermissionDenied, BotFoundationError).
        # ProviderError herda de BotFoundationError, então o catch-all pegaria primeiro
        # e classificaria upstream como "denied" + 403 — escondendo o erro real.
        await _emit_audit(request, "outbound_failed", {"op": "channel.post", "reason": "upstream"})
        return json_error("UPSTREAM_ERROR", e.message, status=502)
    except (PermissionDenied, BotFoundationError) as e:
        await _emit_audit(request, "outbound_failed", {"op": "channel.post", "reason": "denied"})
        return json_error("FORBIDDEN", e.message, status=403)
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
    await _persist_outbound(
        request,
        provider=adapter.name,
        channel_id=body.channel_id,
        message_id=str(msg_id),
        text=body.text,
        reply_to=body.reply_to,
    )
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
    import discord  # lazy import
    try:
        client = getattr(adapter, "_client", None)
        if client is None:
            return _adapter_unavailable(request)
        ch = client.get_channel(int(body.channel_id))
        if ch is None:
            ch = await client.fetch_channel(int(body.channel_id))
        # Pin in DM is NOT supported by the Discord API for bot accounts —
        # discord.py 2.x DMChannel doesn't even define .pin(). Detect early
        # and return a clear, actionable error so the agent doesn't show
        # "upstream error" misterious to the user.
        if isinstance(ch, discord.DMChannel):
            await _emit_audit(
                request,
                "outbound_failed",
                {"op": "message.pin", "reason": "dm_not_supported"},
            )
            return json_error(
                "PIN_NOT_SUPPORTED_IN_DM",
                "Discord DMs não suportam pin de mensagem (limitação da API). "
                "Use /agendar ou marque manualmente — pin só funciona em canais de servidor.",
                status=400,
            )
        msg = await ch.fetch_message(int(body.message_id))
        await msg.pin()
    except discord.NotFound as e:
        await _emit_audit(request, "outbound_failed", {"op": "message.pin", "reason": "not_found"})
        return json_error(
            "UNKNOWN_MESSAGE",
            f"Mensagem {body.message_id!r} não encontrada no canal {body.channel_id!r} "
            "(Discord 10008).",
            status=404,
        )
    except discord.Forbidden as e:
        await _emit_audit(request, "outbound_failed", {"op": "message.pin", "reason": "forbidden"})
        return json_error(
            "FORBIDDEN_PIN",
            "Bot não tem permissão Manage Messages neste canal (Discord 50013).",
            status=403,
        )
    except discord.HTTPException as e:
        await _emit_audit(request, "outbound_failed", {"op": "message.pin", "reason": "http"})
        return json_error(
            "UPSTREAM_ERROR",
            f"Discord rejeitou pin (HTTP {e.status} code {getattr(e, 'code', '?')}).",
            status=502,
        )
    except Exception as e:
        logger.exception("message.pin failed")
        return json_error("UPSTREAM_ERROR", f"message.pin failed: {e}", status=502)
    await _emit_audit(
        request,
        "outbound_sent",
        {"op": "message.pin", "channel_id": body.channel_id, "message_id": body.message_id},
    )
    return web.json_response(MessagePinResponse().model_dump(mode="json"))


async def message_edit(request: web.Request) -> web.Response:
    try:
        body: MessageEditRequest = await _parse_body(request, MessageEditRequest)
    except _ResponseError as e:
        return e.response
    adapter = _get_adapter(request)
    if adapter is None:
        return _adapter_unavailable(request)
    try:
        await adapter.edit_message(
            _channel(adapter, body.channel_id),
            str(body.message_id),
            body.text,
        )
    except CapabilityNotSupported as e:
        await _emit_audit(request, "outbound_failed", {"op": "message.edit", "reason": "capability"})
        return json_error("UPSTREAM_ERROR", e.message, status=502)
    except ProviderError as e:
        # ProviderError antes do catch-all — ver comentário em channel.post.
        await _emit_audit(request, "outbound_failed", {"op": "message.edit", "reason": "upstream"})
        return json_error("UPSTREAM_ERROR", e.message, status=502)
    except (PermissionDenied, BotFoundationError) as e:
        await _emit_audit(request, "outbound_failed", {"op": "message.edit", "reason": "denied"})
        return json_error("FORBIDDEN", e.message, status=403)
    except Exception:
        logger.exception("message.edit failed")
        return json_error("INTERNAL_ERROR", "unexpected failure", status=500)
    payload = MessageEditResponse(
        message_id=str(body.message_id),
        channel_id=str(body.channel_id),
        edited_at=datetime.now(timezone.utc),
    )
    await _emit_audit(
        request,
        "outbound_sent",
        {"op": "message.edit", "channel_id": body.channel_id, "message_id": body.message_id},
    )
    _audit_outbound(request, "message.edit", ok=True, channel_id=body.channel_id, message_id=body.message_id)
    await _persist_message_edit(
        request,
        provider=adapter.name,
        channel_id=body.channel_id,
        message_id=body.message_id,
        text=body.text,
    )
    return web.json_response(payload.model_dump(mode="json"))


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
    await _persist_outbound(
        request,
        provider=adapter.name,
        channel_id=body.channel_id,
        message_id=str(msg.id),
        text=content,
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


async def whatsapp_send_template(request: web.Request) -> web.Response:
    try:
        body: WhatsAppSendTemplateRequest = await _parse_body(
            request, WhatsAppSendTemplateRequest
        )
    except _ResponseError as e:
        return e.response
    adapter = _get_adapter(request, provider_name="whatsapp")
    if adapter is None:
        return _adapter_unavailable(request, provider="whatsapp")
    user = BotUser(
        bot_user_id=f"transient-whatsapp-{body.to}",
        provider="whatsapp",
        provider_user_id=str(body.to),
        display_name=str(body.to),
    )
    template = TemplateMessage(
        name=body.template_name,
        language=body.language,
        body_params=tuple(body.body_params),
        header_params=tuple(body.header_params),
    )
    server = request.app["server"]
    metrics = getattr(server, "metrics", None)

    def _emit_metric(status: str) -> None:
        if metrics is not None:
            metrics.inc(
                "bot_whatsapp_conversations_total",
                {"category": body.category, "status": status},
            )

    try:
        msg_id = await adapter.send_template(user, template)
    except CapabilityNotSupported as e:
        _emit_metric("fail")
        await _emit_audit(
            request,
            "outbound_failed",
            {"op": "whatsapp.send_template", "reason": "capability"},
        )
        return json_error("UPSTREAM_ERROR", e.message, status=502)
    except ProviderError as e:
        # ProviderError antes do catch-all — ver comentário em channel.post.
        _emit_metric("fail")
        await _emit_audit(
            request,
            "outbound_failed",
            {"op": "whatsapp.send_template", "reason": "upstream"},
        )
        return json_error("UPSTREAM_ERROR", e.message, status=502)
    except (PermissionDenied, BotFoundationError) as e:
        _emit_metric("fail")
        await _emit_audit(
            request,
            "outbound_failed",
            {"op": "whatsapp.send_template", "reason": "denied"},
        )
        return json_error("FORBIDDEN", e.message, status=403)
    except Exception:
        _emit_metric("fail")
        logger.exception("whatsapp.send_template failed")
        return json_error("INTERNAL_ERROR", "unexpected failure", status=500)
    # Success path — track conversation cost via metric (operator-visible).
    _emit_metric("ok")
    payload = WhatsAppSendTemplateResponse(
        message_id=str(msg_id),
        to=str(body.to),
        template_name=body.template_name,
        language=body.language,
        sent_at=datetime.now(timezone.utc),
    )
    await _emit_audit(
        request,
        "outbound_sent",
        {
            "op": "whatsapp.send_template",
            "to": body.to,
            "template": body.template_name,
            "language": body.language,
            "category": body.category,
            "message_id": str(msg_id),
        },
    )
    _audit_outbound(
        request,
        "whatsapp.send_template",
        ok=True,
        to=body.to,
        template=body.template_name,
        message_id=str(msg_id),
    )
    return web.json_response(payload.model_dump(mode="json"))


async def test_simulate(request: web.Request) -> web.Response:
    """Simulate an inbound message as if Discord delivered it.

    Body::
        {
          "prompt": "<text the user would have typed>",
          "source": "dm" | "slash" | "forget",  # default "dm"
          "user_id": "<discord snowflake>",     # required
          "display_name": "<name>",     # default "test-user"
          "channel_id": "<discord snowflake>",  # required (not for "forget")
          "channel_scope": "DM" | "GROUP" | "THREAD",   # default "DM"
        }

    ``source="forget"`` runs the shared ``forget_user`` privacy wipe for
    ``user_id`` (no prompt/channel needed) and returns the deletion
    breakdown — used to regression-test ``/forget_me``.

    Returns the canonical pipeline result envelope::
        {
          "ok": bool,
          "elapsed_ms": int,
          "error": "<str|null>",
        }

    The handler builds a synthetic :class:`MessageEnvelope` and feeds it
    to ``pipeline.handle`` (for DM mode) or simulates ``/deile`` by
    setting ``raw.source = "slash:/deile"`` (the pipeline already
    short-circuits some side effects for that source).

    Bearer auth is required (same token as outbound). The endpoint is
    safe for the operator harness but should not be exposed publicly —
    a malicious caller with the token could inject arbitrary prompts
    impersonating any user_id.
    """
    server = request.app["server"]
    pipeline = getattr(server, "pipeline", None)
    if pipeline is None:
        return json_error("UNAVAILABLE", "pipeline not wired", status=503)
    adapter = _get_adapter(request)
    if adapter is None:
        return _adapter_unavailable(request)

    try:
        body = await request.json()
    except Exception:
        return json_error("BAD_REQUEST", "invalid JSON", status=400)

    prompt = str(body.get("prompt") or "").strip()
    user_id = str(body.get("user_id") or "").strip()
    channel_id = str(body.get("channel_id") or "").strip()
    source = str(body.get("source") or "dm").lower()
    # ``forget`` exercises the privacy wipe — it needs only ``user_id``.
    # Every other source replays a message, so prompt + channel are required.
    if source == "forget":
        if not user_id:
            return json_error("BAD_REQUEST", "user_id is required", status=400)
    elif not prompt or not user_id or not channel_id:
        return json_error(
            "BAD_REQUEST",
            "prompt, user_id and channel_id are all required",
            status=400,
        )
    display_name = str(body.get("display_name") or "test-user")
    scope_str = str(body.get("channel_scope") or "DM").upper()
    try:
        scope = ChannelScope(scope_str)
    except ValueError:
        scope = ChannelScope.DM

    import asyncio
    import time

    start = time.monotonic()
    error: Optional[str] = None
    extra: dict = {}

    if source == "forget":
        # Exercise the EXACT shared forget_user core that /forget_me runs.
        from deilebot.foundation.memory_ops import forget_user

        identity = pipeline.identity
        try:
            real_user = await identity.resolve(
                provider="discord",
                provider_user_id=user_id,
                display_name=display_name,
            )
            result = await asyncio.wait_for(
                forget_user(
                    store=pipeline.store,
                    bridge=pipeline.bridge,
                    bot_user_id=real_user.bot_user_id,
                ),
                timeout=60.0,
            )
            extra["bot_user_id"] = real_user.bot_user_id
            extra["messages_deleted"] = result.messages_deleted
            extra["private_channels"] = result.private_channels
            extra["session_in_memory_evicted"] = result.session_in_memory_evicted
            extra["session_persisted_deleted"] = result.session_persisted_deleted
            extra["forget_errors"] = result.errors
            if result.errors:
                error = "; ".join(result.errors)
        except asyncio.TimeoutError:
            error = "timeout"
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            logger.exception("test_simulate(forget): raised")
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return web.json_response({
            "ok": error is None,
            "elapsed_ms": elapsed_ms,
            "error": error,
            "user_id": user_id,
            "source": source,
            **extra,
        })

    if source == "slash":
        # Exercise the EXACT same code path as the real /deile cog —
        # via the shared run_slash_dispatch core.
        from deilebot.foundation.slash_dispatch import run_slash_dispatch

        store = pipeline.store
        identity = pipeline.identity
        try:
            result = await asyncio.wait_for(
                run_slash_dispatch(
                    prompt=prompt,
                    user_id=user_id,
                    display_name=display_name,
                    channel_id=channel_id,
                    store=store,
                    identity=identity,
                    channel_scope=scope,
                    channel_name=None if scope == ChannelScope.DM else "test-channel",
                ),
                timeout=120.0,
            )
            extra["kind"] = result.kind
            extra["reason"] = result.reason
            extra["message_id"] = result.message_id
            if result.kind == "error":
                error = result.reason
        except asyncio.TimeoutError:
            error = "timeout"
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            logger.exception("test_simulate(slash): raised")
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return web.json_response({
            "ok": error is None,
            "elapsed_ms": elapsed_ms,
            "error": error,
            "channel_id": channel_id,
            "user_id": user_id,
            "source": source,
            **extra,
        })

    # source == "dm" → drive the full ingress pipeline as if the
    # DiscordAdapter had received a normalized envelope.
    from types import MappingProxyType

    from deile.common.markup_ast import MarkupAST
    from deilebot.foundation.envelope import MessageEnvelope

    msg_id = f"test-{int(datetime.now(timezone.utc).timestamp() * 1000)}"
    raw_payload: dict = {"test_harness": True}
    channel = Channel(
        provider="discord",
        provider_channel_id=channel_id,
        name=None if scope == ChannelScope.DM else "test-channel",
        scope=scope,
    )
    author = BotUser(
        bot_user_id=f"discord-{user_id}",
        provider="discord",
        provider_user_id=user_id,
        display_name=display_name,
    )
    env = MessageEnvelope(
        message_id=msg_id,
        channel=channel,
        author=author,
        sent_at=datetime.now(timezone.utc),
        text=prompt,
        markup=MarkupAST.from_plain(prompt),
        raw=MappingProxyType(raw_payload),
    )
    try:
        await asyncio.wait_for(pipeline.handle(env, adapter), timeout=120.0)
    except asyncio.TimeoutError:
        error = "timeout"
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        logger.exception("test_simulate(dm): pipeline raised")
    elapsed_ms = int((time.monotonic() - start) * 1000)
    return web.json_response({
        "ok": error is None,
        "elapsed_ms": elapsed_ms,
        "error": error,
        "message_id": msg_id,
        "channel_id": channel_id,
        "user_id": user_id,
        "source": source,
    })


def register_routes(app: web.Application) -> None:
    app.router.add_get("/v1/health", health)
    app.router.add_post("/v1/outbound/discord/channel.post", channel_post)
    app.router.add_post("/v1/outbound/discord/dm.send", dm_send)
    app.router.add_post("/v1/outbound/discord/reaction.add", reaction_add)
    app.router.add_post("/v1/outbound/discord/thread.start", thread_start)
    app.router.add_post("/v1/outbound/discord/message.pin", message_pin)
    app.router.add_post("/v1/outbound/discord/message.edit", message_edit)
    app.router.add_post("/v1/outbound/discord/role.mention", role_mention)
    app.router.add_post("/v1/outbound/whatsapp/send_template", whatsapp_send_template)
    app.router.add_get("/v1/users/{user_id}", get_user)
    # Test harness — operator-only; same Bearer auth as outbound.
    app.router.add_post("/v1/test/simulate", test_simulate)
