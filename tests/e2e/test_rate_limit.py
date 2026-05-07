"""E2E-4 — rate limit scenarios."""

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
from deilebot.foundation.settings import BotSettings, FoundationSettings
from tests.e2e.conftest import CapturingFakeBridge

pytestmark = pytest.mark.e2e


async def test_e2e4_burst_then_blocked(store):
    settings = BotSettings(
        foundation=FoundationSettings(rate_limit_user_burst=3)
    )
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
    pipeline = IngressPipeline(
        identity=identity, permissions=perms, rate_limit=rl, store=store,
        intent=HeuristicIntentClassifier(), bridge=bridge,
        capability_catalog=CapabilityCatalog(),
        persona_selector=PersonaSelector(settings, identity),
        audit=audit, event_bus=bus, metrics=metrics, egress=egress,
        agent_meta=FakeAgentMetaProvider(),
    )
    user = make_user(provider="fake", provider_user_id="rl1")
    for i in range(8):
        env = make_envelope(
            channel=make_channel(provider="fake", scope=ChannelScope.DM),
            author=user,
            text="oi DEILE",
            message_id=f"rl-{i}",
        )
        await pipeline.handle(env, adapter)
    snap = metrics.snapshot()
    rate_limited = snap["counters"].get("bot_rate_limited_total", [])
    audit_rl = await store.query_audit(event_type="rate_limited")
    assert (rate_limited and any(s["value"] >= 1 for s in rate_limited)) or audit_rl
