"""Tests for IdeaCog — /ideia slash command."""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deilebot.foundation.agent_bridge import AgentInvocation, AgentResponse
from deilebot.foundation.exceptions import AgentInvocationError, AgentInvocationTimeout


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(
    *,
    author_id: int = 42,
    author_name: str = "alice",
    channel_id: int = 100,
    has_interaction: bool = False,
) -> MagicMock:
    """Return a minimal fake commands.Context for IdeaCog tests."""
    ctx = MagicMock()
    ctx.defer = AsyncMock()
    ctx.send = AsyncMock()
    ctx.channel.id = channel_id
    ctx.author.id = author_id
    ctx.author.display_name = author_name
    ctx.author.name = author_name
    ctx.interaction = MagicMock() if has_interaction else None
    if has_interaction:
        ctx.interaction.delete_original_response = AsyncMock()
    return ctx


def _make_runtime(bridge_response_text: str = "") -> MagicMock:
    """Return a fake runtime whose pipeline.bridge.invoke returns AgentResponse."""
    from deile.common.markup_ast import MarkupAST

    response = AgentResponse(
        text=bridge_response_text,
        markup=MarkupAST.from_plain(bridge_response_text),
        elapsed_ms=10,
        model_used="fake",
    )
    bridge = MagicMock()
    bridge.invoke = AsyncMock(return_value=response)
    pipeline = MagicMock()
    pipeline.bridge = bridge
    runtime = MagicMock()
    runtime.pipeline = pipeline
    return runtime


def _make_cog(runtime: MagicMock) -> "IdeaCog":
    from deilebot.providers.discord.cogs.idea_cog import IdeaCog

    cog = IdeaCog.__new__(IdeaCog)
    cog.bot = None
    cog.runtime = runtime
    cog.adapter = None
    return cog


async def _call_ideia(cog: "IdeaCog", ctx: MagicMock, texto: str) -> None:
    """Invoke the underlying command callback directly, bypassing discord.py wrapping."""
    # `commands.hybrid_command` wraps the function — `.callback` is the original coroutine.
    await cog.ideia.callback(cog, ctx, texto=texto)


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


class TestExtractIssueUrl:
    def test_extracts_github_issue_url(self):
        from deilebot.providers.discord.cogs.idea_cog import _extract_issue_url

        text = "Issue criada em https://github.com/elimarcavalli/deile/issues/99 !"
        assert _extract_issue_url(text) == "https://github.com/elimarcavalli/deile/issues/99"

    def test_returns_none_when_no_url(self):
        from deilebot.providers.discord.cogs.idea_cog import _extract_issue_url

        assert _extract_issue_url("nenhuma url aqui") is None

    def test_ignores_non_issue_github_urls(self):
        from deilebot.providers.discord.cogs.idea_cog import _extract_issue_url

        text = "see https://github.com/elimarcavalli/deile"
        assert _extract_issue_url(text) is None

    def test_accepts_url_at_end_of_line(self):
        from deilebot.providers.discord.cogs.idea_cog import _extract_issue_url

        text = "Criada:\nhttps://github.com/elimarcavalli/deile/issues/1\n"
        assert _extract_issue_url(text) == "https://github.com/elimarcavalli/deile/issues/1"


class TestIdeaCogIdeia:
    async def test_success_with_url_replies_to_user(self):
        """When bridge returns text with a GitHub issue URL, user gets the URL."""
        url = "https://github.com/elimarcavalli/deile/issues/99"
        runtime = _make_runtime(f"Issue criada: {url}")
        cog = _make_cog(runtime)
        ctx = _make_ctx()

        with patch("deilebot.providers.discord.cogs.idea_cog._safe_dm", new=AsyncMock()):
            await _call_ideia(cog, ctx, "adicionar suporte multi-idioma")

        sent = ctx.send.call_args_list
        assert len(sent) == 1
        assert url in sent[0].args[0]
        assert "✅" in sent[0].args[0]

    async def test_success_builds_correct_agent_invocation(self):
        """AgentInvocation must have extra_system_prompt set and correct persona."""
        url = "https://github.com/elimarcavalli/deile/issues/5"
        runtime = _make_runtime(url)
        cog = _make_cog(runtime)
        ctx = _make_ctx(author_id=77, author_name="bob")

        with patch("deilebot.providers.discord.cogs.idea_cog._safe_dm", new=AsyncMock()):
            await _call_ideia(cog, ctx, "minha ideia")

        inv: AgentInvocation = runtime.pipeline.bridge.invoke.call_args[0][0]
        assert inv.inbound_text == "minha ideia"
        assert inv.persona == "developer"
        assert "INTENT" in inv.extra_system_prompt
        assert inv.bot_user_id == "discord-77"
        assert inv.bot_context["source"] == "slash:/ideia"

    async def test_bridge_error_sends_error_message(self):
        """AgentInvocationError → user receives friendly error message."""
        runtime = _make_runtime()
        runtime.pipeline.bridge.invoke = AsyncMock(
            side_effect=AgentInvocationError("fail", context={})
        )
        cog = _make_cog(runtime)
        ctx = _make_ctx()

        with patch("deilebot.providers.discord.cogs.idea_cog._safe_dm", new=AsyncMock()):
            await _call_ideia(cog, ctx, "ideia qualquer")

        sent_text = ctx.send.call_args[0][0]
        assert "⚠️" in sent_text
        assert "possível" in sent_text.lower() or "processa" in sent_text.lower()

    async def test_timeout_sends_timeout_message(self):
        """AgentInvocationTimeout → user receives timeout message."""
        runtime = _make_runtime()
        runtime.pipeline.bridge.invoke = AsyncMock(
            side_effect=AgentInvocationTimeout("timeout", context={})
        )
        cog = _make_cog(runtime)
        ctx = _make_ctx()

        with patch("deilebot.providers.discord.cogs.idea_cog._safe_dm", new=AsyncMock()):
            await _call_ideia(cog, ctx, "ideia longa")

        sent_text = ctx.send.call_args[0][0]
        assert "⏱️" in sent_text

    async def test_no_url_in_response_shows_full_response(self):
        """When bridge responds without a GitHub URL, show the full text."""
        runtime = _make_runtime("Aqui está minha resposta sem URL de issue.")
        cog = _make_cog(runtime)
        ctx = _make_ctx()

        with patch("deilebot.providers.discord.cogs.idea_cog._safe_dm", new=AsyncMock()):
            await _call_ideia(cog, ctx, "uma ideia")

        sent_text = ctx.send.call_args[0][0]
        assert "📝" in sent_text
        assert "sem URL" in sent_text or "Aqui está" in sent_text

    async def test_interaction_response_deleted_on_success(self):
        """Deferred slash interaction response is deleted after reply."""
        url = "https://github.com/elimarcavalli/deile/issues/3"
        runtime = _make_runtime(url)
        cog = _make_cog(runtime)
        ctx = _make_ctx(has_interaction=True)

        with patch("deilebot.providers.discord.cogs.idea_cog._safe_dm", new=AsyncMock()):
            await _call_ideia(cog, ctx, "ideia com interaction")

        ctx.interaction.delete_original_response.assert_called_once()

    async def test_interaction_deletion_error_does_not_propagate(self):
        """If delete_original_response raises, the cog must not propagate it."""
        url = "https://github.com/elimarcavalli/deile/issues/7"
        runtime = _make_runtime(url)
        cog = _make_cog(runtime)
        ctx = _make_ctx(has_interaction=True)
        ctx.interaction.delete_original_response = AsyncMock(side_effect=Exception("discord error"))

        with patch("deilebot.providers.discord.cogs.idea_cog._safe_dm", new=AsyncMock()):
            await _call_ideia(cog, ctx, "ideia com falha de delete")

        # Must have sent the reply despite the delete error
        assert ctx.send.call_count == 1


class TestOwnerNotifications:
    async def test_notify_on_receive(self):
        """Owner DM must be sent when an idea arrives."""
        url = "https://github.com/elimarcavalli/deile/issues/10"
        runtime = _make_runtime(url)
        cog = _make_cog(runtime)
        ctx = _make_ctx(author_name="carlos")

        owner_dms: List[str] = []

        async def capture_dm(text: str) -> None:
            owner_dms.append(text)

        with patch("deilebot.providers.discord.cogs.idea_cog._safe_dm", side_effect=capture_dm):
            await _call_ideia(cog, ctx, "nova ideia importante")

        assert any("💡" in dm and "carlos" in dm for dm in owner_dms), \
            "Expected a receive-notification DM with 💡 and author name"

    async def test_notify_on_issue_created(self):
        """Owner DM must be sent when issue URL is found in response."""
        url = "https://github.com/elimarcavalli/deile/issues/11"
        runtime = _make_runtime(f"Issue: {url}")
        cog = _make_cog(runtime)
        ctx = _make_ctx(author_name="diana")

        owner_dms: List[str] = []

        async def capture_dm(text: str) -> None:
            owner_dms.append(text)

        with patch("deilebot.providers.discord.cogs.idea_cog._safe_dm", side_effect=capture_dm):
            await _call_ideia(cog, ctx, "ideia de diana")

        assert any("✅" in dm and url in dm for dm in owner_dms), \
            "Expected a created-notification DM with ✅ and issue URL"

    async def test_notify_on_error(self):
        """Owner DM must be sent when bridge errors."""
        runtime = _make_runtime()
        runtime.pipeline.bridge.invoke = AsyncMock(
            side_effect=AgentInvocationError("boom", context={})
        )
        cog = _make_cog(runtime)
        ctx = _make_ctx(author_name="eve")

        owner_dms: List[str] = []

        async def capture_dm(text: str) -> None:
            owner_dms.append(text)

        with patch("deilebot.providers.discord.cogs.idea_cog._safe_dm", side_effect=capture_dm):
            await _call_ideia(cog, ctx, "erro aqui")

        assert any("⚠️" in dm for dm in owner_dms), "Expected error DM to owner"

    async def test_owner_dm_failure_does_not_abort_command(self):
        """If the underlying DM raises, _safe_dm must swallow it and the command completes."""
        url = "https://github.com/elimarcavalli/deile/issues/20"
        runtime = _make_runtime(url)
        cog = _make_cog(runtime)
        ctx = _make_ctx()

        async def failing_dm(text: str) -> None:
            raise RuntimeError("DM network error")

        # Patch the LOW-level helper. _safe_dm wraps it with try/except — must swallow.
        with patch("deilebot.providers.discord.cogs.idea_cog._send_owner_dm", side_effect=failing_dm):
            await _call_ideia(cog, ctx, "ideia resiliente")

        # User still gets the reply despite the DM failure.
        assert ctx.send.call_count == 1
        assert url in ctx.send.call_args[0][0]
