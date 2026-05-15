"""HistoryCog — slash commands para inspeção de histórico do bot.

Comandos:
    /historico [user_id?] [channel_id?] [busca?] [limite?]
        - Sem args (admin): visão consolidada — lista bot_users +
          canais lado a lado para o operador escolher um filtro.
        - Sem args (não-admin): mensagens do próprio user (todos os canais).
        - Com ``user_id``: mensagens daquele bot_user (admin only quando
          for diferente do caller).
        - Com ``channel_id``: mensagens daquele canal (admin only).
        - ``user_id`` aceita: ULID interno (26 chars), ``provider:id``,
          ou snowflake puro Discord.
        - ``channel_id`` aceita o snowflake puro do canal Discord.

    /historico_users   (admin)
        - Lista TODOS os bot_users com count + last_seen + bot_user_id.

    /historico_canais  (admin)
        - Lista TODOS os canais que o bot tocou (DM + GROUP) com count
          + last_msg + scope. **Era /historico_canais antigo (que listava
          users) — corrigido para fazer jus ao nome**.

    /historico_export
        - Exporta JSON com TODO o próprio histórico (LGPD self-service).

    /memoria [user_id?]   (admin)
        - context_data_json da persisted_session do agente DEILE para
          um user. Sem ``user_id`` mostra o do próprio caller.

Notas de segurança
------------------
- ``user_id != self`` ou ``channel_id`` exigem ``is_owner``.
- Toda resposta ``ephemeral=True`` (não vaza metadados em canal público).
- ``/memoria`` é owner-only por padrão.
"""

from __future__ import annotations

import io
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from deilebot.foundation.envelope import BotUser
from deilebot.foundation.logging import get_logger
from deilebot.foundation.settings import get_bot_settings

_logger = get_logger("history_cog")

_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def _normalize_user_id_input(raw: str) -> str:
    """Normalize the operator-supplied user identifier.

    Accepts (in priority order):
        - bot_user_id ULID (26 chars Crockford base32) → as-is, uppercased
        - 'discord:1234' / '<provider>:<id>'           → as-is
        - bare snowflake digits                        → wrapped in 'discord:'
    """
    s = (raw or "").strip()
    if not s:
        return s
    if _ULID_RE.fullmatch(s.upper()):
        return s.upper()
    if ":" in s:
        return s
    if s.isdigit():
        return f"discord:{s}"
    return s


def _short(text: str, n: int = 120) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text[:n] + ("…" if len(text) > n else "")


def _ts(dt_or_str) -> str:
    """Format an ISO timestamp string or datetime as 'MM-DD HH:MM'."""
    if dt_or_str is None:
        return "?"
    if hasattr(dt_or_str, "strftime"):
        return dt_or_str.strftime("%m-%d %H:%M")
    s = str(dt_or_str)
    return s[:16].replace("T", " ")


def _fmt_render_lines_for_messages(msgs, names: Optional[dict] = None) -> str:
    """Render message rows ASCII-style, newest first.

    ``names`` maps ``bot_user_id`` → display name. When provided and the
    channel has more than one inbound participant, the line is prefixed
    with the sender name. For single-user DMs (and outbound msgs from the
    bot itself) the name would only add noise so it's omitted.
    """
    names = names or {}
    # Count unique inbound senders to decide if name labels add value.
    inbound_senders = {m.bot_user_id for m in msgs if m.direction == "inbound" and m.bot_user_id}
    multi_party = len(inbound_senders) > 1

    out = []
    for m in msgs:
        arrow = "→" if m.direction == "inbound" else "←"
        prefix = ""
        if multi_party and m.direction == "inbound" and m.bot_user_id in names:
            prefix = f"[{names[m.bot_user_id]}] "
        out.append(f"`{_ts(m.sent_at)}` {arrow} {prefix}{_short(m.text)}")
    return "\n".join(out)


def _format_channel_label(ch: dict) -> str:
    """Build a human-friendly label for a channel row from list_channels.

    - DM:    "DM com <display_name>"  (or "DM <channel_id>" if name absent)
    - GROUP: "#<channel.name>"        (or fallback to channel_id)
    - Unknown scope: channel_id verbatim.

    Participants are appended in parens when the count > 1 (GROUP).
    """
    scope = (ch.get("scope") or "").upper()
    ch_id = ch.get("provider_channel_id") or "?"
    name = ch.get("name")
    parts_raw = ch.get("participant_names") or ""
    parts = [p for p in parts_raw.split(",") if p]

    if scope == "DM":
        if parts:
            return f"DM com **{parts[0]}**"
        return f"DM `{ch_id}`"
    if scope == "GROUP":
        base = f"#**{name}**" if name else f"GROUP `{ch_id}`"
        if len(parts) > 1:
            return f"{base} (com {', '.join(parts)})"
        if len(parts) == 1:
            return f"{base} (com {parts[0]})"
        return base
    return f"`{ch_id}`"


class HistoryCog(commands.Cog):
    """Read-only history inspection — owner picks any user/channel; others see self."""

    def __init__(self, bot: Any, runtime: Any, adapter: Any) -> None:
        self.bot = bot
        self.runtime = runtime
        self.adapter = adapter

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @property
    def _store(self):
        return self.runtime.pipeline.store

    def _is_owner(self, discord_user_id: int) -> bool:
        try:
            settings = get_bot_settings()
            owners = set(settings.permissions.owners or [])
            return f"discord:{discord_user_id}" in owners
        except Exception:  # noqa: BLE001
            return False

    async def _resolve_target_user(
        self,
        caller_id: int,
        raw: Optional[str],
    ) -> Optional[BotUser]:
        if not raw:
            return await self._store.get_user_by_provider_id("discord", str(caller_id))
        normalized = _normalize_user_id_input(raw)
        if _ULID_RE.fullmatch(normalized):
            user = await self._store.get_user_by_bot_user_id(normalized)
            if user is not None:
                return user
        if ":" in normalized:
            provider, _, pid = normalized.partition(":")
            user = await self._store.get_user_by_provider_id(provider, pid)
            if user is not None:
                return user
        return None

    async def _send_admin_overview(self, ctx: commands.Context) -> None:
        """Visão consolidada — users + canais lado a lado (somente admin sem args)."""
        users = await self._store.list_users(limit=50)
        channels = await self._store.list_channels(limit=50)

        sections: List[str] = []

        if users:
            lines = [f"👤 **bot_users ({len(users)})**:"]
            for u in users:
                pid = f"{u['provider']}:{u['provider_user_id']}"
                lines.append(
                    f"• **{u['display_name'] or '?'}** `{pid}` · "
                    f"{u['msg_count']} msgs · last={_ts(u['last_seen_at'])}"
                )
            sections.append("\n".join(lines))

        if channels:
            lines = [f"📺 **canais ({len(channels)})**:"]
            for ch in channels:
                label = _format_channel_label(ch)
                lines.append(
                    f"• {label} `{ch['provider_channel_id']}` · "
                    f"{ch['msg_count']} msgs · last={_ts(ch['last_msg_at'])}"
                )
            sections.append("\n".join(lines))

        sections.append(
            "💡 **Para detalhar:**\n"
            "• `/historico user_id:<id>` (mensagens daquele user)\n"
            "• `/historico channel_id:<id>` (mensagens daquele canal)\n"
            "• `/historico_users` ou `/historico_canais` (lista dedicada)\n"
            "• `/memoria user_id:<id>` (working memory do agente)"
        )

        msg = "\n\n".join(sections) if sections else "📭 DB vazio."
        if len(msg) > 1900:
            buf = io.StringIO(msg)
            file = discord.File(io.BytesIO(buf.getvalue().encode()), filename="overview.txt")
            await ctx.send("📊 visão geral (anexo, é grande)", file=file, ephemeral=True)
        else:
            await ctx.send(msg, ephemeral=True)

    async def _render_message_block(
        self,
        ctx: commands.Context,
        header: str,
        msgs: List[Any],
        attachment_name: str,
    ) -> None:
        if not msgs:
            await ctx.send(header + "\n📭 sem mensagens.", ephemeral=True)
            return
        # Resolve display_names for inbound senders so multi-party blocks
        # (e.g. GROUP channels) show WHO said what.
        names = await self._store.get_display_names_for_users(
            [m.bot_user_id for m in msgs]
        )
        body = _fmt_render_lines_for_messages(msgs, names=names)
        full = f"{header}\n```\n{body}\n```"
        if len(full) > 1900:
            file = discord.File(io.BytesIO(body.encode()), filename=attachment_name)
            await ctx.send(header + "\n(grande — anexo)", file=file, ephemeral=True)
        else:
            await ctx.send(full, ephemeral=True)

    # ------------------------------------------------------------------
    # /historico — switch by args (user_id XOR channel_id)
    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="historico",
        description="Histórico de mensagens — por user, por canal, ou visão geral (admin)",
    )
    @app_commands.describe(
        user_id="bot_user_id ULID, 'discord:<id>' ou snowflake — em branco = você",
        channel_id="snowflake do canal Discord (admin only) — XOR com user_id",
        busca="palavra-chave (LIKE case-insensitive)",
        limite="quantas mensagens trazer (default 20, máx 100)",
    )
    async def historico(
        self,
        ctx: commands.Context,
        user_id: Optional[str] = None,
        channel_id: Optional[str] = None,
        busca: Optional[str] = None,
        limite: int = 20,
    ) -> None:
        await ctx.defer(ephemeral=True)
        is_owner = self._is_owner(ctx.author.id)
        limit = max(1, min(int(limite), 100))

        if user_id and channel_id:
            await ctx.send(
                "❌ Use `user_id` OU `channel_id`, não os dois (escopos diferentes).",
                ephemeral=True,
            )
            return

        # Sem args + admin → overview consolidado
        if not user_id and not channel_id and is_owner:
            return await self._send_admin_overview(ctx)

        # channel_id pedido → admin only
        if channel_id:
            if not is_owner:
                await ctx.send(
                    "❌ Apenas o owner pode filtrar por canal arbitrário.",
                    ephemeral=True,
                )
                return
            ch_id = channel_id.strip()
            msgs = await self._store.list_messages_by_channel(
                "discord", ch_id, limit=limit, search=busca,
            )
            # Build a rich header — find the channel row to derive the
            # "DM com X" / "#name (com Y, Z)" label. We list and filter
            # because there's no single-channel store getter that returns
            # participant_names.
            all_channels = await self._store.list_channels(limit=200)
            ch_row = next(
                (c for c in all_channels if c["provider_channel_id"] == ch_id),
                None,
            )
            label = (
                _format_channel_label(ch_row) if ch_row else f"`{ch_id}`"
            )
            header = (
                f"📺 **{label}** — últimas {len(msgs)}"
                + (f" com `{busca}`" if busca else "")
                + f"\n`channel_id={ch_id}`"
            )
            return await self._render_message_block(
                ctx, header, msgs, f"historico_canal_{ch_id}.txt",
            )

        # user_id ≠ self → admin only
        if user_id and not is_owner:
            await ctx.send(
                "❌ Apenas o owner pode consultar histórico de outros usuários.",
                ephemeral=True,
            )
            return

        target = await self._resolve_target_user(ctx.author.id, user_id)
        if target is None:
            label = user_id or "você"
            await ctx.send(f"❌ Nenhum histórico encontrado para `{label}`.", ephemeral=True)
            return

        msgs = await self._store.list_messages_by_user(
            target.bot_user_id, limit=limit, search=busca,
        )
        header = (
            f"👤 **{target.display_name}** "
            f"(`{target.provider}:{target.provider_user_id}`) — "
            f"últimas {len(msgs)}"
            + (f" com `{busca}`" if busca else "")
        )
        return await self._render_message_block(
            ctx, header, msgs,
            f"historico_{target.provider}_{target.provider_user_id}.txt",
        )

    # ------------------------------------------------------------------
    # /historico_users  (admin)
    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="historico_users",
        description="(admin) Lista bot_users registrados — count + last_seen",
    )
    async def historico_users(self, ctx: commands.Context) -> None:
        await ctx.defer(ephemeral=True)
        if not self._is_owner(ctx.author.id):
            await ctx.send("❌ Apenas o owner pode ver essa lista.", ephemeral=True)
            return
        users = await self._store.list_users(limit=200)
        if not users:
            await ctx.send("📭 Nenhum bot_user registrado.", ephemeral=True)
            return
        lines = [f"👤 **{len(users)} bot_user(s):**\n"]
        for u in users:
            pid = f"{u['provider']}:{u['provider_user_id']}"
            lines.append(
                f"• `{pid}` — **{u['display_name'] or '?'}** · "
                f"{u['msg_count']} msgs · last={_ts(u['last_seen_at'])}\n"
                f"   `bot_user_id={u['bot_user_id']}`"
            )
        msg = "\n".join(lines)
        if len(msg) > 1900:
            file = discord.File(io.BytesIO(msg.encode()), filename="bot_users.txt")
            await ctx.send(f"📋 {len(users)} bot_users (anexo)", file=file, ephemeral=True)
        else:
            await ctx.send(msg, ephemeral=True)

    # ------------------------------------------------------------------
    # /historico_canais  (admin)  — agora ALI lista canais de fato
    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="historico_canais",
        description="(admin) Lista canais (DM + GROUP) que o bot tocou — count + last_msg",
    )
    async def historico_canais(self, ctx: commands.Context) -> None:
        await ctx.defer(ephemeral=True)
        if not self._is_owner(ctx.author.id):
            await ctx.send("❌ Apenas o owner pode ver essa lista.", ephemeral=True)
            return
        channels = await self._store.list_channels(limit=200)
        if not channels:
            await ctx.send("📭 Nenhum canal registrado.", ephemeral=True)
            return
        lines = [f"📺 **{len(channels)} canal(is):**\n"]
        for ch in channels:
            label = _format_channel_label(ch)
            scope = (ch["scope"] or "?")
            lines.append(
                f"• {label} ({scope}) · "
                f"{ch['msg_count']} msgs · last={_ts(ch['last_msg_at'])}\n"
                f"   `channel_id={ch['provider_channel_id']}`"
            )
        lines.append(
            f"\n💡 Para mensagens: `/historico channel_id:{channels[0]['provider_channel_id']}`"
        )
        msg = "\n".join(lines)
        if len(msg) > 1900:
            file = discord.File(io.BytesIO(msg.encode()), filename="channels.txt")
            await ctx.send(f"📋 {len(channels)} canais (anexo)", file=file, ephemeral=True)
        else:
            await ctx.send(msg, ephemeral=True)

    # ------------------------------------------------------------------
    # /historico_export — JSON do próprio user
    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="historico_export",
        description="Exporta TODO o seu histórico num JSON (LGPD self-service)",
    )
    async def historico_export(self, ctx: commands.Context) -> None:
        await ctx.defer(ephemeral=True)
        target = await self._store.get_user_by_provider_id("discord", str(ctx.author.id))
        if target is None:
            await ctx.send("📭 Nenhum histórico seu encontrado.", ephemeral=True)
            return

        msgs = await self._store.list_messages_by_user(target.bot_user_id, limit=10_000)
        export = {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "user": {
                "bot_user_id": target.bot_user_id,
                "provider": target.provider,
                "provider_user_id": target.provider_user_id,
                "display_name": target.display_name,
            },
            "message_count": len(msgs),
            "messages": [
                {
                    "direction": m.direction,
                    "sent_at": m.sent_at.isoformat(),
                    "channel_id": m.provider_channel_id,
                    "message_id": m.provider_message_id,
                    "text": m.text,
                    "reply_to": m.reply_to_message_id,
                }
                for m in msgs
            ],
        }
        payload = json.dumps(export, indent=2, ensure_ascii=False).encode("utf-8")
        file = discord.File(
            io.BytesIO(payload),
            filename=f"historico_{target.provider}_{target.provider_user_id}.json",
        )
        await ctx.send(
            f"📦 Seu histórico completo: **{len(msgs)} mensagens** "
            f"({len(payload):,} bytes).",
            file=file,
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /memoria  (admin)
    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="memoria",
        description="(admin) Working memory do agente DEILE para um user",
    )
    @app_commands.describe(
        user_id="bot_user_id ULID, 'discord:<id>' ou snowflake — em branco = você",
    )
    async def memoria(
        self,
        ctx: commands.Context,
        user_id: Optional[str] = None,
    ) -> None:
        await ctx.defer(ephemeral=True)
        if not self._is_owner(ctx.author.id):
            await ctx.send("❌ Apenas o owner pode inspecionar working memory.", ephemeral=True)
            return

        target = await self._resolve_target_user(ctx.author.id, user_id)
        if target is None:
            await ctx.send(f"❌ User `{user_id or 'self'}` não encontrado.", ephemeral=True)
            return

        sessions_path = Path(
            os.environ.get(
                "DEILE_BOT_SESSIONS_SQLITE_PATH",
                "/home/deile/data/deile_sessions.sqlite",
            )
        )
        if not sessions_path.exists():
            await ctx.send(
                f"❌ DB de sessões não existe em `{sessions_path}`.",
                ephemeral=True,
            )
            return

        session_id = f"bot_session_{target.bot_user_id}"
        try:
            conn = sqlite3.connect(str(sessions_path))
            cur = conn.execute(
                "SELECT working_directory, context_data_json, created_at, last_used_at "
                "FROM persisted_session WHERE session_id = ?",
                (session_id,),
            )
            row = cur.fetchone()
            conn.close()
        except sqlite3.Error as exc:
            await ctx.send(f"⚠️ Erro lendo DB de sessões: {exc}", ephemeral=True)
            return

        if row is None:
            await ctx.send(
                f"📭 DEILE ainda não tem working memory pra **{target.display_name}** "
                f"(session_id=`{session_id}`).",
                ephemeral=True,
            )
            return

        wd, ctx_json, created_at, last_used_at = row
        try:
            parsed = json.loads(ctx_json) if ctx_json else {}
        except json.JSONDecodeError:
            parsed = None

        header = (
            f"🧠 **Working memory de {target.display_name}** "
            f"(session=`{session_id}`)\n"
            f"- created_at: `{created_at}`\n"
            f"- last_used_at: `{last_used_at}`\n"
            f"- working_directory: `{wd}`\n"
        )

        if parsed is None:
            body = f"```\n{ctx_json[:1500]}\n```"
        else:
            pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
            if len(pretty) > 1500:
                file = discord.File(
                    io.BytesIO(pretty.encode("utf-8")),
                    filename=f"memoria_{target.provider}_{target.provider_user_id}.json",
                )
                await ctx.send(header + "\n(JSON em anexo, é grande)", file=file, ephemeral=True)
                return
            body = f"```json\n{pretty}\n```"

        full = header + body
        if len(full) > 1900:
            full = full[:1897] + "…"
        await ctx.send(full, ephemeral=True)
