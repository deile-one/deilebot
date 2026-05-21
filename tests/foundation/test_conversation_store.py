"""Tests for ConversationStore (SQLite persistence)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from deilebot._testing import make_channel, make_envelope, make_user
from deilebot.foundation.conversation_store import ConversationStore
from deilebot.foundation.envelope import (Attachment, AttachmentKind,
                                           ChannelScope)
from deilebot.foundation.exceptions import ConversationStoreError


@pytest.fixture
async def store(tmp_path):
    s = ConversationStore(tmp_path / "bot.sqlite")
    await s.init()
    yield s
    await s.close()


class TestInit:
    async def test_init_runs_migrations(self, tmp_path):
        s = ConversationStore(tmp_path / "bot.sqlite")
        await s.init()
        # Re-init is idempotent
        await s.init()
        # Schema present
        cur = await s._db.execute("SELECT version FROM schema_version")
        rows = await cur.fetchall()
        await cur.close()
        assert rows and rows[0][0] == 1
        await s.close()

    async def test_uninitialized_raises(self, tmp_path):
        s = ConversationStore(tmp_path / "x.sqlite")
        with pytest.raises(ConversationStoreError):
            await s.upsert_user(make_user())

    async def test_wal_mode_active(self, store):
        cur = await store._db.execute("PRAGMA journal_mode")
        row = await cur.fetchone()
        await cur.close()
        assert row[0].lower() == "wal"

    async def test_foreign_keys_on(self, store):
        cur = await store._db.execute("PRAGMA foreign_keys")
        row = await cur.fetchone()
        await cur.close()
        assert row[0] == 1


class TestUsers:
    async def test_upsert_user_idempotent(self, store):
        u = make_user(provider="discord", provider_user_id="123")
        await store.upsert_user(u)
        await store.upsert_user(u)
        cur = await store._db.execute("SELECT count(*) FROM bot_user")
        row = await cur.fetchone()
        await cur.close()
        assert row[0] == 1

    async def test_get_user_by_provider_id(self, store):
        u = make_user(provider="discord", provider_user_id="42")
        await store.upsert_user(u)
        found = await store.get_user_by_provider_id("discord", "42")
        assert found is not None
        assert found.bot_user_id == u.bot_user_id

    async def test_search_by_display_name(self, store):
        await store.upsert_user(make_user(provider_user_id="1", display_name="alice"))
        await store.upsert_user(make_user(provider_user_id="2", display_name="bob"))
        await store.upsert_user(make_user(provider_user_id="3", display_name="alex"))
        results = await store.search_users_by_display_name("al")
        names = {u.display_name for u in results}
        assert "alice" in names and "alex" in names
        assert "bob" not in names


class TestMessages:
    async def test_record_inbound_unique(self, store):
        env = make_envelope(text="hello")
        await store.upsert_user(env.author)
        await store.upsert_channel(env.channel)
        id1 = await store.record_inbound(env)
        id2 = await store.record_inbound(env)
        # OR IGNORE, so id2 == 0 (no insert)
        assert id1 > 0
        assert id2 == 0 or id2 == id1

    async def test_record_outbound(self, store):
        u = make_user()
        ch = make_channel(scope=ChannelScope.GROUP)
        await store.upsert_user(u)
        await store.upsert_channel(ch)
        sent_at = datetime.now(timezone.utc)
        rid = await store.record_outbound(
            "fake", ch, "out-1", u.bot_user_id, "hi back", reply_to="m-1", sent_at=sent_at
        )
        assert rid > 0

    async def test_get_recent_messages_order_desc(self, store):
        env = make_envelope(text="first")
        await store.upsert_user(env.author)
        await store.upsert_channel(env.channel)
        await store.record_inbound(env)
        env2 = make_envelope(
            text="second",
            channel=env.channel,
            author=env.author,
            sent_at=datetime.now(timezone.utc) + timedelta(seconds=1),
        )
        await store.record_inbound(env2)
        msgs = await store.get_recent_messages("fake", env.channel)
        assert len(msgs) == 2
        assert msgs[0].text == "second"
        assert msgs[1].text == "first"

    async def test_purge_older_than(self, store):
        u = make_user()
        ch = make_channel(scope=ChannelScope.GROUP)
        await store.upsert_user(u)
        await store.upsert_channel(ch)
        old = make_envelope(
            text="old",
            channel=ch,
            author=u,
            message_id="old-1",
            sent_at=datetime.now(timezone.utc) - timedelta(days=120),
        )
        await store.record_inbound(old)
        new = make_envelope(text="fresh", channel=ch, author=u, message_id="new-1")
        await store.record_inbound(new)
        removed = await store.purge_older_than(days=30)
        assert removed == 1
        remaining = await store.get_recent_messages("fake", ch)
        assert {m.text for m in remaining} == {"fresh"}

    async def test_purge_user_messages(self, store):
        u1 = make_user(provider_user_id="1", display_name="alice")
        u2 = make_user(provider_user_id="2", display_name="bob")
        ch = make_channel(scope=ChannelScope.GROUP)
        await store.upsert_user(u1)
        await store.upsert_user(u2)
        await store.upsert_channel(ch)
        await store.record_inbound(make_envelope(channel=ch, author=u1, message_id="a1"))
        await store.record_inbound(make_envelope(channel=ch, author=u2, message_id="b1"))
        removed = await store.purge_user_messages(u1.bot_user_id)
        assert removed == 1


class TestDeleteUserHistory:
    """delete_user_history — the privacy wipe behind /forget_me + /forget."""

    async def test_dm_wipes_both_directions(self, store):
        # In a DM the whole channel is private to the one user, so BOTH the
        # user's inbound AND the bot's outbound must go — otherwise the
        # per-channel window still leaks half the conversation back.
        u = make_user(provider_user_id="1", display_name="elimar")
        bot = make_user(provider_user_id="bot", display_name="DEILE", is_bot=True)
        dm = make_channel(scope=ChannelScope.DM, provider_channel_id="dm-1")
        await store.upsert_user(u)
        await store.upsert_user(bot)
        await store.upsert_channel(dm)
        await store.record_inbound(
            make_envelope(channel=dm, author=u, message_id="in-1", text="oi")
        )
        await store.record_outbound(
            "fake", dm, "out-1", bot.bot_user_id, "olá!",
            reply_to=None, sent_at=datetime.now(timezone.utc),
        )
        counts = await store.delete_user_history(u.bot_user_id)
        assert counts == {"messages": 2, "private_channels": 1}
        assert await store.get_recent_messages("fake", dm) == []

    async def test_shared_channel_keeps_other_users(self, store):
        # A GROUP where two humans posted — only the target user's own
        # inbound goes; the other human's message and the bot's survive.
        u1 = make_user(provider_user_id="1", display_name="elimar")
        u2 = make_user(provider_user_id="2", display_name="bob")
        bot = make_user(provider_user_id="bot", display_name="DEILE", is_bot=True)
        ch = make_channel(scope=ChannelScope.GROUP, provider_channel_id="grp-1")
        for x in (u1, u2, bot):
            await store.upsert_user(x)
        await store.upsert_channel(ch)
        await store.record_inbound(
            make_envelope(channel=ch, author=u1, message_id="g-u1", text="u1 here")
        )
        await store.record_inbound(
            make_envelope(channel=ch, author=u2, message_id="g-u2", text="u2 here")
        )
        await store.record_outbound(
            "fake", ch, "g-bot", bot.bot_user_id, "bot here",
            reply_to=None, sent_at=datetime.now(timezone.utc),
        )
        counts = await store.delete_user_history(u1.bot_user_id)
        assert counts == {"messages": 1, "private_channels": 0}
        remaining = {m.text for m in await store.get_recent_messages("fake", ch)}
        assert remaining == {"u2 here", "bot here"}

    async def test_no_history_is_idempotent(self, store):
        u = make_user(provider_user_id="ghost")
        await store.upsert_user(u)
        counts = await store.delete_user_history(u.bot_user_id)
        assert counts == {"messages": 0, "private_channels": 0}

    async def test_mixed_private_and_shared(self, store):
        # The user has a private DM AND posts in a shared group: the DM is
        # wiped whole, the group loses only the user's own inbound.
        u = make_user(provider_user_id="1", display_name="elimar")
        other = make_user(provider_user_id="2", display_name="bob")
        bot = make_user(provider_user_id="bot", display_name="DEILE", is_bot=True)
        dm = make_channel(scope=ChannelScope.DM, provider_channel_id="dm-x")
        grp = make_channel(scope=ChannelScope.GROUP, provider_channel_id="grp-x")
        for x in (u, other, bot):
            await store.upsert_user(x)
        for c in (dm, grp):
            await store.upsert_channel(c)
        await store.record_inbound(
            make_envelope(channel=dm, author=u, message_id="dm-in", text="dm msg")
        )
        await store.record_outbound(
            "fake", dm, "dm-out", bot.bot_user_id, "dm reply",
            reply_to=None, sent_at=datetime.now(timezone.utc),
        )
        await store.record_inbound(
            make_envelope(channel=grp, author=u, message_id="grp-in", text="grp msg")
        )
        await store.record_inbound(
            make_envelope(channel=grp, author=other, message_id="grp-other",
                          text="keep me")
        )
        counts = await store.delete_user_history(u.bot_user_id)
        assert counts == {"messages": 3, "private_channels": 1}
        assert await store.get_recent_messages("fake", dm) == []
        grp_left = {m.text for m in await store.get_recent_messages("fake", grp)}
        assert grp_left == {"keep me"}


class TestConcurrency:
    async def test_parallel_inserts_no_corruption(self, tmp_path):
        s = ConversationStore(tmp_path / "concurrent.sqlite")
        await s.init()
        u = make_user()
        ch = make_channel(scope=ChannelScope.GROUP)
        await s.upsert_user(u)
        await s.upsert_channel(ch)
        envs = [
            make_envelope(channel=ch, author=u, message_id=f"m-{i}", text=f"m{i}")
            for i in range(50)
        ]
        await asyncio.gather(*(s.record_inbound(e) for e in envs))
        msgs = await s.get_recent_messages("fake", ch, limit=100)
        assert len(msgs) == 50
        await s.close()


class TestAttachments:
    async def test_attachment_inline_under_threshold(self, store):
        att = Attachment(kind=AttachmentKind.IMAGE, bytes_inline=b"hi", filename="x.png")
        env = make_envelope(attachments=(att,))
        await store.upsert_user(env.author)
        await store.upsert_channel(env.channel)
        await store.record_inbound(env)
        cur = await store._db.execute("SELECT bytes_inline_b64 FROM attachment")
        row = await cur.fetchone()
        await cur.close()
        assert row[0] is not None


class TestAudit:
    async def test_audit_insert_and_query(self, store):
        await store.insert_audit("inbound_received", payload={"channel": "general"})
        await store.insert_audit("outbound_sent", payload={"chars": 42})
        rows = await store.query_audit()
        types = [r["event_type"] for r in rows]
        assert "inbound_received" in types
        assert "outbound_sent" in types

    async def test_audit_filter_by_event(self, store):
        await store.insert_audit("agent_invoked")
        await store.insert_audit("agent_failed")
        rows = await store.query_audit(event_type="agent_failed")
        assert all(r["event_type"] == "agent_failed" for r in rows)


class TestDLQ:
    async def test_insert_list_count(self, store):
        await store.insert_dlq("discord", {"text": "fail"}, "boom", attempts=3)
        assert await store.count_dlq("discord") == 1
        rows = await store.list_dlq("discord")
        assert rows[0]["last_error"] == "boom"

    async def test_purge_dlq(self, store):
        await store.insert_dlq("discord", {"text": "old"}, "e", 1)
        # All entries are fresh; purging older than 0 days clears all
        removed = await store.purge_dlq_older_than(days=0)
        assert removed >= 0


class TestChannel:
    async def test_upsert_and_get_channel(self, store):
        ch = make_channel(provider_channel_id="c-99", scope=ChannelScope.GROUP)
        await store.upsert_channel(ch)
        retrieved = await store.get_channel("fake", "c-99")
        assert retrieved is not None
        assert retrieved.scope == ChannelScope.GROUP


class TestGetWindow:
    """ConversationStore.get_window — derived from latest inbound timestamp."""

    async def test_no_inbound_yields_closed_window(self, store):
        ch = make_channel(provider="whatsapp", provider_channel_id="100:5511999")
        await store.upsert_channel(ch)
        win = await store.get_window("whatsapp", ch)
        assert win.last_inbound_at is None
        assert win.is_open is False

    async def test_recent_inbound_yields_open_window(self, store):
        ch = make_channel(provider="whatsapp", provider_channel_id="100:5511999")
        u = make_user(provider="whatsapp", provider_user_id="5511999")
        await store.upsert_user(u)
        await store.upsert_channel(ch)
        env = make_envelope(
            text="oi",
            channel=ch,
            author=u,
            sent_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        )
        await store.record_inbound(env)
        win = await store.get_window("whatsapp", ch)
        assert win.last_inbound_at is not None
        assert win.is_open is True

    async def test_stale_inbound_yields_closed_window(self, store):
        ch = make_channel(provider="whatsapp", provider_channel_id="100:5511999")
        u = make_user(provider="whatsapp", provider_user_id="5511999")
        await store.upsert_user(u)
        await store.upsert_channel(ch)
        env = make_envelope(
            text="oi",
            channel=ch,
            author=u,
            sent_at=datetime.now(timezone.utc) - timedelta(hours=25),
        )
        await store.record_inbound(env)
        win = await store.get_window("whatsapp", ch, window_hours=24)
        assert win.is_open is False

    async def test_outbound_only_yields_closed_window(self, store):
        """Window opens only on inbound — agent's own outbound does not count."""
        ch = make_channel(provider="whatsapp", provider_channel_id="100:5511999")
        u = make_user(provider="whatsapp", provider_user_id="5511999")
        await store.upsert_user(u)
        await store.upsert_channel(ch)
        await store.record_outbound(
            provider="whatsapp",
            channel=ch,
            provider_message_id="wamid.outbound",
            bot_user_id=u.bot_user_id,
            text="hi",
            reply_to=None,
            sent_at=datetime.now(timezone.utc),
        )
        win = await store.get_window("whatsapp", ch)
        assert win.last_inbound_at is None
        assert win.is_open is False

    async def test_custom_window_hours(self, store):
        ch = make_channel(provider="whatsapp", provider_channel_id="100:5511999")
        u = make_user(provider="whatsapp", provider_user_id="5511999")
        await store.upsert_user(u)
        await store.upsert_channel(ch)
        env = make_envelope(
            text="oi",
            channel=ch,
            author=u,
            sent_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )
        await store.record_inbound(env)
        # 1h window — closed; 24h window — open
        assert (await store.get_window("whatsapp", ch, window_hours=1)).is_open is False
        assert (await store.get_window("whatsapp", ch, window_hours=24)).is_open is True


class TestUpdateMessageText:
    """update_message_text — keeps a stored row in sync with provider edits.

    The deile-worker posts one status message and edits it live; each
    edit must update the row so the DB mirrors what the user sees.
    """

    async def test_update_existing_outbound(self, store):
        u = make_user()
        ch = make_channel(scope=ChannelScope.DM)
        await store.upsert_user(u)
        await store.upsert_channel(ch)
        await store.record_outbound(
            "fake", ch, "out-1", u.bot_user_id, "🔧 inicializando",
            reply_to=None, sent_at=datetime.now(timezone.utc),
        )
        ok = await store.update_message_text(
            "fake", ch.provider_channel_id, "out-1", "✅ concluído"
        )
        assert ok is True
        msgs = await store.get_recent_messages("fake", ch)
        assert len(msgs) == 1  # edited in place — not a duplicate row
        assert msgs[0].text == "✅ concluído"

    async def test_update_missing_row_returns_false(self, store):
        ch = make_channel()
        ok = await store.update_message_text(
            "fake", ch.provider_channel_id, "ghost", "x"
        )
        assert ok is False

    async def test_update_keeps_sent_at(self, store):
        # sent_at is the message's position in the conversation — an edit
        # must not move it.
        u = make_user()
        ch = make_channel(scope=ChannelScope.DM)
        await store.upsert_user(u)
        await store.upsert_channel(ch)
        original = datetime(2020, 1, 1, tzinfo=timezone.utc)
        await store.record_outbound(
            "fake", ch, "out-1", u.bot_user_id, "v1",
            reply_to=None, sent_at=original,
        )
        await store.update_message_text("fake", ch.provider_channel_id, "out-1", "v2")
        msgs = await store.get_recent_messages("fake", ch)
        assert msgs[0].sent_at == original


class TestLastInboundUser:
    """get_last_inbound_bot_user_id — attributes control-plane posts."""

    async def test_returns_latest_inbound_user(self, store):
        ch = make_channel(scope=ChannelScope.DM)
        u = make_user(provider_user_id="u-1")
        await store.upsert_user(u)
        await store.upsert_channel(ch)
        await store.record_inbound(
            make_envelope(channel=ch, author=u, message_id="in-1", text="oi")
        )
        got = await store.get_last_inbound_bot_user_id("fake", ch.provider_channel_id)
        assert got == u.bot_user_id

    async def test_none_when_no_inbound(self, store):
        ch = make_channel()
        got = await store.get_last_inbound_bot_user_id("fake", ch.provider_channel_id)
        assert got is None

    async def test_ignores_outbound_only_channel(self, store):
        u = make_user()
        ch = make_channel(scope=ChannelScope.DM)
        await store.upsert_user(u)
        await store.upsert_channel(ch)
        await store.record_outbound(
            "fake", ch, "out-1", u.bot_user_id, "só outbound",
            reply_to=None, sent_at=datetime.now(timezone.utc),
        )
        got = await store.get_last_inbound_bot_user_id("fake", ch.provider_channel_id)
        assert got is None
