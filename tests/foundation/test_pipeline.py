"""Tests for IngressPipeline + EgressPipeline (unit-level)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from deilebot._testing import (FakeAgentMetaProvider, FakeProviderAdapter,
                                make_channel, make_envelope, make_user)
from deilebot.foundation.agent_bridge import (AgentBridge, AgentInvocation,
                                               AgentResponse)
from deilebot.foundation.audit import BotAuditLogger
from deilebot.foundation.capabilities import CapabilityCatalog
from deilebot.foundation.conversation_store import (ConversationStore,
                                                     StoredMessage)
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
                                            render_history_for_worker)
from deilebot.foundation.rate_limit import RateLimiter
from deilebot.foundation.settings import BotSettings, PermissionsSettings


class FakeBridge(AgentBridge):
    def __init__(self, response_text="canned response", *, raise_exc=None):
        self.response_text = response_text
        self.invocations = []
        self._raise = raise_exc

    async def invoke(self, inv: AgentInvocation) -> AgentResponse:
        self.invocations.append(inv)
        if self._raise:
            raise self._raise
        from deile.common.markup_ast import MarkupAST

        return AgentResponse(
            text=self.response_text,
            markup=MarkupAST.from_plain(self.response_text),
            elapsed_ms=10,
            model_used="fake",
        )


@pytest.fixture
async def store(tmp_path):
    s = ConversationStore(tmp_path / "p.sqlite")
    await s.init()
    yield s
    await s.close()


def _wire(store, *, settings=None, bridge=None, adapter=None):
    settings = settings or BotSettings()
    bridge = bridge or FakeBridge()
    adapter = adapter or FakeProviderAdapter()
    identity = IdentityResolver(store)
    perms = PermissionGate(settings, identity)
    rl = RateLimiter(settings)
    audit = BotAuditLogger(store)
    bus = BotEventBus()
    metrics = MetricsCollector()
    dlq = DeadLetterQueue(store, settings)
    egress = EgressPipeline(
        formatters={adapter.name: PlainTextFormatter()},
        rate_limit=rl,
        store=store,
        audit=audit,
        event_bus=bus,
        metrics=metrics,
        dlq=dlq,
    )
    catalog = CapabilityCatalog()
    persona = PersonaSelector(settings, identity)
    intent = HeuristicIntentClassifier()
    meta = FakeAgentMetaProvider()
    pipeline = IngressPipeline(
        identity=identity,
        permissions=perms,
        rate_limit=rl,
        store=store,
        intent=intent,
        bridge=bridge,
        capability_catalog=catalog,
        persona_selector=persona,
        audit=audit,
        event_bus=bus,
        metrics=metrics,
        egress=egress,
        agent_meta=meta,
    )
    return pipeline, adapter, bridge, store, metrics


class TestHappyPath:
    async def test_dm_response_lands_in_inbox(self, store):
        pipeline, adapter, bridge, _, metrics = _wire(store)
        env = make_envelope(
            channel=make_channel(scope=ChannelScope.DM), text="oi DEILE"
        )
        await pipeline.handle(env, adapter)
        assert len(adapter.inbox) == 1
        assert adapter.inbox[0]["text"] == "canned response"
        snap = metrics.snapshot()
        assert any(s["value"] >= 1 for s in snap["counters"].get("bot_inbound_total", []))

    async def test_history_persists(self, store):
        pipeline, adapter, bridge, _, _ = _wire(store)
        env1 = make_envelope(
            channel=make_channel(scope=ChannelScope.DM, provider_channel_id="dm1"),
            text="primeiro",
            message_id="m1",
        )
        env2 = make_envelope(
            channel=make_channel(scope=ChannelScope.DM, provider_channel_id="dm1"),
            author=env1.author,
            text="segundo",
            message_id="m2",
        )
        await pipeline.handle(env1, adapter)
        await pipeline.handle(env2, adapter)
        ch = make_channel(scope=ChannelScope.DM, provider_channel_id="dm1")
        msgs = await store.get_recent_messages("fake", ch)
        assert len(msgs) >= 4  # 2 in + 2 out


class TestIntent:
    async def test_noise_in_group_skipped(self, store):
        pipeline, adapter, bridge, _, _ = _wire(store)
        env = make_envelope(
            channel=make_channel(scope=ChannelScope.GROUP),
            text="random talk no one cares",
        )
        await pipeline.handle(env, adapter)
        assert adapter.inbox == []
        assert bridge.invocations == []


class TestPermissionDenied:
    async def test_denied_user_no_invoke(self, store):
        # Pre-resolve user so its bot_user_id is stable across the test.
        u = make_user(bot_user_id="BLOCK", provider_user_id="999")
        await store.upsert_user(u)
        settings = BotSettings(permissions=PermissionsSettings(blocklist=["BLOCK"]))
        pipeline, adapter, bridge, _, _ = _wire(store, settings=settings)
        env = make_envelope(
            channel=make_channel(scope=ChannelScope.DM), author=u, text="oi"
        )
        await pipeline.handle(env, adapter)
        assert adapter.inbox == []
        rows = await store.query_audit(event_type="permission_denied")
        assert any(r["bot_user_id"] for r in rows)


class TestAgentFailure:
    async def test_agent_error_sends_fallback(self, store):
        from deilebot.foundation.exceptions import AgentInvocationError

        bridge = FakeBridge(raise_exc=AgentInvocationError("nope"))
        pipeline, adapter, _, _, _ = _wire(store, bridge=bridge)
        env = make_envelope(channel=make_channel(scope=ChannelScope.DM), text="hi")
        await pipeline.handle(env, adapter)
        # fallback message lands in inbox (warn icon + the failure reason)
        assert len(adapter.inbox) == 1
        assert "⚠️" in adapter.inbox[0]["text"]
        assert "não foi possível processar" in adapter.inbox[0]["text"]
        rows = await store.query_audit(event_type="agent_failed")
        assert len(rows) >= 1


class TestEgressDLQ:
    async def test_send_failure_lands_in_dlq(self, store):
        adapter = FakeProviderAdapter()
        adapter.fail_send_n_times(10)
        pipeline, adapter, bridge, _, metrics = _wire(store, adapter=adapter)
        env = make_envelope(channel=make_channel(scope=ChannelScope.DM), text="hi")
        await pipeline.handle(env, adapter)
        assert await DLQ_count(store) >= 1


async def DLQ_count(store):
    return await store.count_dlq()


# ---------------------------------------------------------------------------
# TestEgressTemplate (Phase 2 — WhatsApp template send through pipeline)
# ---------------------------------------------------------------------------


class _FakeTemplateAdapter:
    """Minimal adapter shim that implements `send_template` for unit tests."""

    name = "whatsapp"

    def __init__(self, *, raise_exc=None, msg_id="wamid.OK"):
        self.calls = []
        self._raise = raise_exc
        self._msg_id = msg_id

    async def send_template(self, user, template):
        self.calls.append({"user": user, "template": template})
        if self._raise is not None:
            raise self._raise
        return self._msg_id


def _bare_egress(store) -> EgressPipeline:
    settings = BotSettings()
    rl = RateLimiter(settings)
    audit = BotAuditLogger(store)
    bus = BotEventBus()
    metrics = MetricsCollector()
    dlq = DeadLetterQueue(store, settings)
    return EgressPipeline(
        formatters={},
        rate_limit=rl,
        store=store,
        audit=audit,
        event_bus=bus,
        metrics=metrics,
        dlq=dlq,
    )


class TestEgressTemplate:
    async def test_send_template_records_outbound_and_metric(self, store):
        from deilebot.foundation.envelope import TemplateMessage
        eg = _bare_egress(store)
        adapter = _FakeTemplateAdapter()
        u = make_user(provider="whatsapp", provider_user_id="5511999")
        ch = make_channel(provider="whatsapp", provider_channel_id="100:5511999")
        await store.upsert_user(u)
        await store.upsert_channel(ch)
        msg_id = await eg.send_template(
            adapter, u, ch,
            TemplateMessage(name="hi", language="en_US"),
            category="utility",
        )
        assert msg_id == "wamid.OK"
        assert len(adapter.calls) == 1
        snap = eg._metrics.snapshot()
        # Metric was emitted for the right category
        wa_series = snap["counters"].get("bot_whatsapp_conversations_total", [])
        assert wa_series, "bot_whatsapp_conversations_total counter missing"
        ok_entries = [
            s for s in wa_series
            if s["labels"].get("category") == "utility"
            and s["labels"].get("status") == "ok"
        ]
        assert ok_entries and ok_entries[0]["value"] == 1
        # And outbound persistence happened
        rows = await store.get_recent_messages("whatsapp", ch)
        assert any(r.text == "[template:hi]" for r in rows)

    async def test_send_template_failure_metric_and_audit(self, store):
        from deilebot.foundation.envelope import TemplateMessage
        from deilebot.foundation.exceptions import ProviderError
        eg = _bare_egress(store)
        adapter = _FakeTemplateAdapter(raise_exc=ProviderError("upstream 132001"))
        u = make_user(provider="whatsapp", provider_user_id="5511999")
        ch = make_channel(provider="whatsapp", provider_channel_id="100:5511999")
        await store.upsert_user(u)
        await store.upsert_channel(ch)
        with pytest.raises(ProviderError):
            await eg.send_template(
                adapter, u, ch,
                TemplateMessage(name="ghost", language="pt_BR"),
                category="marketing",
            )
        snap = eg._metrics.snapshot()
        wa_series = snap["counters"].get("bot_whatsapp_conversations_total", [])
        # At least one fail counter must exist
        assert any(s["labels"].get("status") == "fail" for s in wa_series)

    async def test_send_template_adapter_without_method_raises(self, store):
        from deilebot.foundation.envelope import TemplateMessage
        from deilebot.foundation.exceptions import ProviderError

        class _NoTemplate:
            name = "telegram"

        eg = _bare_egress(store)
        u = make_user(provider="telegram", provider_user_id="100")
        ch = make_channel(provider="telegram", provider_channel_id="100")
        with pytest.raises(ProviderError, match="does not implement send_template"):
            await eg.send_template(
                _NoTemplate(), u, ch,
                TemplateMessage(name="x", language="en_US"),
            )


class TestRenderHistoryForWorker:
    """render_history_for_worker — compact context block for the worker.

    The bot-mediated path forwards this to the one-shot worker so it can
    resolve follow-ups; the /deile passthrough sends none.
    """

    @staticmethod
    def _msg(mid: str, direction: str, text: str) -> StoredMessage:
        now = datetime.now(timezone.utc)
        return StoredMessage(
            id=0, provider="discord", provider_channel_id="c",
            provider_message_id=mid, direction=direction, bot_user_id="u",
            text=text, reply_to_message_id=None, sent_at=now, persisted_at=now,
        )

    def test_renders_oldest_first_labelled(self):
        # get_recent_messages returns newest-first; render flips to oldest-first.
        history = [
            self._msg("m3", "inbound", "terceira"),
            self._msg("m2", "outbound", "segunda"),
            self._msg("m1", "inbound", "primeira"),
        ]
        assert render_history_for_worker(history).splitlines() == [
            "[user] primeira", "[deile] segunda", "[user] terceira",
        ]

    def test_excludes_current_message(self):
        history = [
            self._msg("cur", "inbound", "mensagem atual"),
            self._msg("old", "inbound", "antiga"),
        ]
        assert render_history_for_worker(
            history, exclude_message_id="cur"
        ) == "[user] antiga"

    def test_empty_history_yields_empty_string(self):
        assert render_history_for_worker([]) == ""

    def test_per_message_truncation(self):
        line = render_history_for_worker([self._msg("m1", "inbound", "x" * 1000)])
        assert line.endswith("…")
        assert len(line) < 450

    def test_total_char_cap(self):
        history = [self._msg(f"m{i}", "inbound", "y" * 300) for i in range(40)]
        out = render_history_for_worker(history, limit=40, max_chars=1000)
        assert len(out) <= 1000

    def test_limit_keeps_most_recent(self):
        # 30 messages newest-first; limit=5 → the 5 newest, rendered oldest-first.
        history = [self._msg(f"m{i}", "inbound", f"msg{i}") for i in range(30)]
        assert render_history_for_worker(history, limit=5).splitlines() == [
            "[user] msg4", "[user] msg3", "[user] msg2",
            "[user] msg1", "[user] msg0",
        ]
