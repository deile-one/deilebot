"""E2E-5 — DLQ enqueue + replay."""

from __future__ import annotations

import pytest

from deilebot._testing import (FakeAgentMetaProvider, FakeProviderAdapter,
                                make_channel, make_envelope)
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
from deilebot.foundation.pipeline import (EgressPipeline, IngressPipeline,
                                           RetryPolicy)
from deilebot.foundation.rate_limit import RateLimiter
from deilebot.foundation.settings import BotSettings
from tests.e2e.conftest import CapturingFakeBridge

pytestmark = pytest.mark.e2e


async def test_e2e5_dlq_enqueue_then_replay(store):
    """Force send_message to fail; verify DLQ; cure adapter; replay; verify drained."""
    settings = BotSettings()
    adapter = FakeProviderAdapter()
    adapter.fail_send_n_times(20)  # all egress attempts fail
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
        rate_limit=rl, store=store, audit=audit, event_bus=bus, metrics=metrics,
        dlq=dlq, retry_policy=RetryPolicy(max_attempts=2, base_seconds=0.0),
    )
    pipeline = IngressPipeline(
        identity=identity, permissions=perms, rate_limit=rl, store=store,
        intent=HeuristicIntentClassifier(), bridge=bridge,
        capability_catalog=CapabilityCatalog(),
        persona_selector=PersonaSelector(settings, identity),
        audit=audit, event_bus=bus, metrics=metrics, egress=egress,
        agent_meta=FakeAgentMetaProvider(),
    )
    env = make_envelope(
        channel=make_channel(provider="fake", scope=ChannelScope.DM),
        text="oi DEILE",
    )
    await pipeline.handle(env, adapter)
    assert await dlq.count("fake") >= 1

    # Cure adapter and replay
    adapter._fail_until_call = 0
    captured = []

    async def sender(payload):
        captured.append(payload)

    results = await dlq.replay(sender, provider="fake")
    assert any(r["result"] == "ok" for r in results)
    assert await dlq.count("fake") == 0
