"""IdeaCog — `/ideia <texto>` submits an idea to DEILE to create a GitHub INTENT issue.

The command:
1. Notifies the owner (DM) that an idea was received.
2. Invokes the DEILE bridge directly with an extra_system_prompt instructing
   issue creation, bypassing the full ingress pipeline (no rate-limit or
   intent-classifier interference for this structured action).
3. Extracts the GitHub issue URL from the response and replies to the user.
4. Notifies the owner (DM) when the issue is created.

Configuration (env / .env):
    DEILE_PIPELINE_NOTIFY_USER_ID  Discord snowflake to notify at each step.
                                   Defaults to empty string (no notification).
"""

from __future__ import annotations

import os
import re
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

from deilebot.foundation.agent_bridge import AgentInvocation
from deilebot.foundation.exceptions import (AgentInvocationError,
                                            AgentInvocationTimeout)
from deilebot.foundation.logging import get_logger

_logger = get_logger("idea_cog")

_GITHUB_URL_RE = re.compile(r"https://github\.com/[^\s\"'<>]+/issues/\d+", re.IGNORECASE)

_EXTRA_SYSTEM_PROMPT = (
    "CONTEXT: The user is submitting a raw idea for the DEILE project. "
    "Your ONLY job in this response is to open a new GitHub INTENT issue "
    "in the repository elimarcavalli/deile following the template at "
    ".github/ISSUE_TEMPLATE/intent.md. Fill in all sections based on the idea. "
    "When done, respond with EXACTLY one line containing the full GitHub issue URL "
    "(e.g. https://github.com/elimarcavalli/deile/issues/N). "
    "Do not ask for clarification — infer and act now."
)

_NOTIFY_USER_ID: str = os.environ.get("DEILE_PIPELINE_NOTIFY_USER_ID", "")


async def _send_owner_dm(text: str) -> None:
    """Send a DM to the pipeline owner. Best-effort — never raises."""
    if not _NOTIFY_USER_ID:
        return
    try:
        from deilebot.bridge.dm_tool import send_discord_dm
        await send_discord_dm(_NOTIFY_USER_ID, text)
    except Exception as exc:
        _logger.warning("owner DM failed: %s", exc)


async def _safe_dm(text: str) -> None:
    """Defense-in-depth: never let a DM failure abort the command flow."""
    try:
        await _send_owner_dm(text)
    except Exception as exc:  # noqa: BLE001 — DM is best-effort
        _logger.warning("safe DM swallowed: %s", exc)


def _extract_issue_url(text: str) -> Optional[str]:
    """Return the first GitHub issue URL found in text, or None."""
    m = _GITHUB_URL_RE.search(text)
    return m.group(0) if m else None


class IdeaCog(commands.Cog):
    """Cog that exposes /ideia to submit ideas as GitHub INTENT issues."""

    def __init__(self, bot: Any, runtime: Any, adapter: Any) -> None:
        self.bot = bot
        self.runtime = runtime
        self.adapter = adapter

    @commands.hybrid_command(name="ideia", description="Submete uma ideia para criar uma issue INTENT no GitHub")
    @app_commands.describe(texto="Descreva a ideia em linguagem natural")
    async def ideia(self, ctx: commands.Context, *, texto: str) -> None:
        await ctx.defer(ephemeral=False)

        author_name = getattr(ctx.author, "display_name", None) or ctx.author.name
        author_id = str(ctx.author.id)

        # 1. Notify owner that an idea arrived.
        await _safe_dm(
            f"💡 **Nova ideia recebida** de `{author_name}` (id={author_id}):\n> {texto}"
        )

        # 2. Build AgentInvocation — bypass ingress pipeline for this structured action.
        inv = AgentInvocation(
            bot_user_id=f"discord-{author_id}",
            persona="developer",
            forced_model=None,
            inbound_text=texto,
            extra_system_prompt=_EXTRA_SYSTEM_PROMPT,
            bot_context={
                "provider": "discord",
                "channel_id": str(ctx.channel.id),
                "author_id": author_id,
                "author_name": author_name,
                "source": "slash:/ideia",
            },
            timeout_seconds=180,
        )

        try:
            bridge = self.runtime.pipeline.bridge
            response = await bridge.invoke(inv)
            response_text = response.text or ""
        except AgentInvocationTimeout:
            _logger.warning("idea bridge timed out for user %s", author_id)
            await _safe_dm(f"⏱️ Timeout ao processar ideia de `{author_name}`.")
            await ctx.send("⏱️ O agente demorou demais. Tente novamente em instantes.")
            return
        except AgentInvocationError as exc:
            _logger.error("idea bridge error for user %s: %s", author_id, exc)
            await _safe_dm(f"⚠️ Erro ao processar ideia de `{author_name}`: {exc}")
            await ctx.send("⚠️ Não foi possível processar a ideia agora. Tente novamente.")
            return

        # 3. Extract issue URL and reply.
        issue_url = _extract_issue_url(response_text)
        if issue_url:
            await ctx.send(f"✅ Issue criada com sucesso!\n🔗 {issue_url}")
            await _safe_dm(
                f"✅ **Issue criada** a partir de ideia de `{author_name}`:\n🔗 {issue_url}"
            )
        else:
            short_response = response_text[:1800] if len(response_text) > 1800 else response_text
            await ctx.send(f"📝 DEILE processou a ideia:\n{short_response}")
            await _safe_dm(
                f"⚠️ **Ideia processada** de `{author_name}` — URL não encontrada na resposta."
            )

        # Clean up the deferred interaction message.
        if ctx.interaction is not None:
            try:
                await ctx.interaction.delete_original_response()
            except Exception:
                pass
