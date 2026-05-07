"""E2E-10 — persistence survives ConversationStore close+reopen."""

from __future__ import annotations

import pytest

from deilebot._testing import (FakeAgentMetaProvider, FakeProviderAdapter,
                                make_channel, make_envelope, make_user)
from deilebot.foundation.audit import BotAuditLogger
from deilebot.foundation.capabilities import CapabilityCatalog
from deilebot.foundation.conversation_store import ConversationStore
from deilebot.foundation.dlq import DeadLetterQueue
from deilebot.foundation.envelope import ChannelScope
from deilebot.foundation.event_bus import BotEventBus
from deilebot.foundation.identity import IdentityResolver
from deilebot.foundation.intent import HeuristicIntentClassifier
from deilebot.foundation.metrics import MetricsCollector
from deilebot.foundation.output_formatter import PlainTextFormatter
from deilebot.foundation.permissions import PermissionGate
from deilebot.foundation.persona_selector import PersonaSelector
from deilebot.foundation.pipeline import EgressPipeline, IngressPipeline
from deilebot.foundation.rate_limit import RateLimiter
from deilebot.foundation.settings import BotSettings
from tests.e2e.conftest import CapturingFakeBridge

pytestmark = pytest.mark.e2e


async def _build(store):
    settings = BotSettings()
    adapter = FakeProviderAdapter()
    identity = IdentityResolver(store)
    perms = PermissionGate(settings, identity)
    rl = RateLimiter(settings)
    audit = BotAuditLogger(store)
    bus = BotEventBus()
    metrics = MetricsCollector()
    dlq = DeadLetterQueue(store, settings)
    bridge = CapturingFakeBridge()
    egress = EgressPipeline(
        formatters={adapter.name: PlainTextFormatter()},
        rate_limit=rl, store=store, audit=audit, event_bus=bus, metrics=metrics, dlq=dlq,
    )
    p = IngressPipeline(
        identity=identity, permissions=perms, rate_limit=rl, store=store,
        intent=HeuristicIntentClassifier(), bridge=bridge,
        capability_catalog=CapabilityCatalog(),
        persona_selector=PersonaSelector(settings, identity),
        audit=audit, event_bus=bus, metrics=metrics, egress=egress,
        agent_meta=FakeAgentMetaProvider(),
    )
    return p, adapter


async def test_e2e10_persistence_across_restart(tmp_path):
    db_path = tmp_path / "restart.sqlite"
    user = make_user(provider="fake", provider_user_id="alice", display_name="Alice")
    # First run
    s1 = ConversationStore(db_path)
    await s1.init()
    p1, adapter = await _build(s1)
    for i in range(5):
        env = make_envelope(
            channel=make_channel(provider="fake", scope=ChannelScope.DM),
            author=user, text=f"msg-{i}", message_id=f"r1-{i}",
        )
        await p1.handle(env, adapter)
    user_after = await s1.get_user_by_provider_id("fake", "alice")
    assert user_after is not None
    bot_user_id_run1 = user_after.bot_user_id
    await s1.close()
    # Reopen
    s2 = ConversationStore(db_path)
    await s2.init()
    user_again = await s2.get_user_by_provider_id("fake", "alice")
    assert user_again is not None
    assert user_again.bot_user_id == bot_user_id_run1
    msgs = await s2.get_recent_messages("fake", make_channel(provider="fake", scope=ChannelScope.DM), limit=100)
    assert len(msgs) >= 5
    await s2.close()
