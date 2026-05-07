"""E2E-3 — permission scenarios."""

from __future__ import annotations

import pytest

from deilebot._testing import (FakeAgentMetaProvider, FakeProviderAdapter,
                                make_channel, make_envelope, make_user)
from deilebot.foundation.audit import BotAuditLogger
from deilebot.foundation.capabilities import CapabilityCatalog
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
from deilebot.foundation.settings import BotSettings, PermissionsSettings
from tests.e2e.conftest import CapturingFakeBridge

pytestmark = pytest.mark.e2e


async def _make_pipeline(store, settings, fake_adapter):
    identity = IdentityResolver(store)
    perms = PermissionGate(settings, identity)
    rl = RateLimiter(settings)
    audit = BotAuditLogger(store)
    bus = BotEventBus()
    metrics = MetricsCollector()
    dlq = DeadLetterQueue(store, settings)
    bridge = CapturingFakeBridge()
    egress = EgressPipeline(
        formatters={fake_adapter.name: PlainTextFormatter()},
        rate_limit=rl,
        store=store,
        audit=audit,
        event_bus=bus,
        metrics=metrics,
        dlq=dlq,
    )
    p = IngressPipeline(
        identity=identity,
        permissions=perms,
        rate_limit=rl,
        store=store,
        intent=HeuristicIntentClassifier(),
        bridge=bridge,
        capability_catalog=CapabilityCatalog(),
        persona_selector=PersonaSelector(settings, identity),
        audit=audit,
        event_bus=bus,
        metrics=metrics,
        egress=egress,
        agent_meta=FakeAgentMetaProvider(),
    )
    return p, bridge, metrics


async def test_e2e3_blocklist_user_ignored(store):
    blocked = make_user(bot_user_id="BLOCK_ME", provider="fake", provider_user_id="999")
    await store.upsert_user(blocked)
    settings = BotSettings(permissions=PermissionsSettings(blocklist=["BLOCK_ME"]))
    adapter = FakeProviderAdapter()
    pipeline, bridge, _ = await _make_pipeline(store, settings, adapter)
    env = make_envelope(
        channel=make_channel(provider="fake", scope=ChannelScope.DM),
        author=blocked, text="hi",
    )
    await pipeline.handle(env, adapter)
    assert adapter.inbox == []
    assert bridge.invocations == []
    rows = await store.query_audit(event_type="permission_denied")
    assert any(r["bot_user_id"] == "BLOCK_ME" for r in rows)


async def test_e2e3_owner_allowed(store):
    owner = make_user(bot_user_id="OWNER_X", provider="fake", provider_user_id="42")
    await store.upsert_user(owner)
    settings = BotSettings(permissions=PermissionsSettings(owners=["OWNER_X"]))
    adapter = FakeProviderAdapter()
    pipeline, bridge, _ = await _make_pipeline(store, settings, adapter)
    env = make_envelope(
        channel=make_channel(provider="fake", scope=ChannelScope.DM),
        author=owner, text="executa algo proativo",
    )
    await pipeline.handle(env, adapter)
    assert len(bridge.invocations) == 1
    assert len(adapter.inbox) == 1


async def test_e2e3_default_wildcard_allows(store):
    settings = BotSettings()  # default allowlist_invoke_agent=["*"]
    adapter = FakeProviderAdapter()
    pipeline, bridge, _ = await _make_pipeline(store, settings, adapter)
    env = make_envelope(
        channel=make_channel(provider="fake", scope=ChannelScope.DM),
        text="oi",
    )
    await pipeline.handle(env, adapter)
    assert len(bridge.invocations) == 1
