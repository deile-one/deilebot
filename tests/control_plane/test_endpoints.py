"""End-to-end checks for the deile-bot control-plane server.

We boot the *real* `ControlPlaneServer` against a fake provider adapter
and exercise it through the *real* `BotControlClient`. The fake adapter
only stubs the few methods the routes call.
"""

from __future__ import annotations

import pytest
from deile_bot_client import (BotClientAuthError, BotControlClient,
                              BotControlSettings)

from deile_bot.foundation.capabilities import ProviderCapabilities
from deile_bot.foundation.envelope import AttachmentKind
from deile_bot.runtime.control_plane import (ControlPlaneServer,
                                             ControlPlaneSettings)


class FakeAdapter:
    name = "discord"
    capabilities = ProviderCapabilities(
        can_edit_message=True, can_react=True, can_send_dm=True,
        can_threads=True, can_polls=False, can_inline_keyboards=False,
        can_slash_commands=True, can_voice_messages=False,
        can_send_typing=True, can_fetch_user_profile=True,
        has_conversation_window=True,
        max_message_chars=2000, max_attachments_per_message=10,
        supported_attachment_kinds=frozenset({AttachmentKind.IMAGE, AttachmentKind.FILE}),
    )
    _client = object()

    def __init__(self):
        self.calls = []

    async def send_message(self, channel, text, reply_to=None, attachments=()):
        self.calls.append(("send_message", channel.provider_channel_id, text))
        return "msg-99"

    async def react(self, channel, message_id, emoji):
        self.calls.append(("react", channel.provider_channel_id, message_id, emoji))

    async def send_dm(self, user, text, attachments=()):
        self.calls.append(("send_dm", user.provider_user_id, text))
        return "dm-99"


@pytest.fixture
async def daemon():
    settings = ControlPlaneSettings(host="127.0.0.1", port=0, auth_token="cp-test")
    srv = ControlPlaneServer(settings, version="cp-test")
    adapter = FakeAdapter()
    srv.register_adapter("discord", adapter)
    port = await srv.start()
    yield srv, adapter, port
    await srv.stop()


async def test_health_no_auth_required(daemon):
    srv, _adapter, port = daemon
    # No token at all — health is the one public endpoint
    settings = BotControlSettings(endpoint=f"http://127.0.0.1:{port}", auth_token="")
    async with BotControlClient(settings) as cli:
        h = await cli.health()
        assert h.ok is True
        assert h.providers == ["discord"]


async def test_protected_endpoint_requires_token(daemon):
    _, _, port = daemon
    settings = BotControlSettings(endpoint=f"http://127.0.0.1:{port}", auth_token="")
    async with BotControlClient(settings) as cli:
        with pytest.raises(BotClientAuthError):
            await cli.discord_channel_post(channel_id="1", text="x")


async def test_round_trip_post_react_dm(daemon):
    _, adapter, port = daemon
    settings = BotControlSettings(endpoint=f"http://127.0.0.1:{port}", auth_token="cp-test")
    async with BotControlClient(settings) as cli:
        post = await cli.discord_channel_post(channel_id="1", text="hi")
        assert post.message_id == "msg-99"
        await cli.discord_reaction_add(channel_id="1", message_id="msg-99", emoji="🚀")
        dm = await cli.discord_dm_send(user_id="42", text="psst")
        assert dm.message_id == "dm-99"
    assert ("send_message", "1", "hi") in adapter.calls
    assert ("react", "1", "msg-99", "🚀") in adapter.calls
    assert ("send_dm", "42", "psst") in adapter.calls


async def test_dm_validates_payload(daemon):
    _, _adapter, port = daemon
    settings = BotControlSettings(endpoint=f"http://127.0.0.1:{port}", auth_token="cp-test")
    async with BotControlClient(settings) as cli:
        # Local pydantic validation — never reaches the network
        with pytest.raises(Exception):
            await cli.discord_dm_send(user_id="42", bot_user_id="ulid", text="hi")
