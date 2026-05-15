"""HistoryCog — slash commands para inspeção de histórico do bot.

Comandos:
    /historico [user_id] [busca] [limit]
        - sem args: lista as últimas N mensagens do próprio usuário
          (admin sem ``user_id`` ganha o atalho de listar TODOS os
          ``bot_user`` disponíveis para escolher).
        - com ``user_id`` (admin only — ou self): mensagens daquele user.
        - ``user_id`` aceita os 3 formatos (escolha o mais conveniente):
            ``01KRNFTQQF38Z4G51KNX6QRTC0`` (bot_user_id ULID interno)
            ``discord:1475913578648436909`` (provider:provider_user_id)
            ``1475913578648436909`` (snowflake puro Discord)

    /historico_export
        - exporta TODO o histórico do próprio usuário num JSON,
          enviado como anexo via DM (LGPD/backup self-service).

    /historico_canais  (admin only)
        - lista bot_users que conversaram com o bot, com contagem
          de mensagens e last_seen. Útil quando outras pessoas
          começarem a usar.

    /memoria [user_id]  (admin only)
        - mostra o ``context_data_json`` da ``persisted_session`` do
          DEILE para um user — a working memory cross-turn do agente.
          Sem ``user_id`` mostra o do próprio caller.

Notas de segurança
------------------
- ``user_id != self`` exige ``is_owner`` (PermissionGate). Sem isso,
  qualquer pessoa veria as DMs de qualquer outra — vazamento crítico.
- ``/historico_canais`` e ``/memoria`` são owner-only por padrão; um
  user comum pegará 403.
- Todas as respostas que listam *outras* pessoas são enviadas com
  ``ephemeral=True`` para não expor metadados em canal público.
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


def _normalize_user_id_input(raw: str) -> str:
    """Normalize the operator-supplied user identifier.

    Accepts (in priority order):
        - bot_user_id ULID (26 chars, starts with letter)  → returned as-is
        - 'discord:1234' / '<provider>:<id>'              → returned as-is
        - bare snowflake digits                           → wrapped in 'discord:'
    """
    s = (raw or "").strip()
    if not s:
        return s
    # ULID = 26 chars, base32 (Crockford), starts with 0-9 or letter
    if re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", s.upper()):
        return s.upper()
    if ":" in s:
        return s
    if s.isdigit():
        return f"discord:{s}"
    return s


class HistoryCog(commands.Cog):
    """Read-only history inspection — owner can pick any user, others see self."""

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
        """Map raw input to a stored BotUser. None when not found."""
        if not raw:
            # Default: caller themselves.
            return await self._store.get_user_by_provider_id(
                "discord", str(caller_id)
            )
        normalized = _normalize_user_id_input(raw)
        # Try as bot_user_id (ULID).
        if re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", normalized):
            user = await self._store.get_user_by_bot_user_id(normalized)
            if user is not None:
                return user
        # Try as 'provider:provider_user_id'.
        if ":" in normalized:
            provider, _, pid = normalized.partition(":")
            user = await self._store.get_user_by_provider_id(provider, pid)
            if user is not None:
                return user
        return None

    # ------------------------------------------------------------------
    # /historico
    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="historico",
        description="Mostra histórico de mensagens (próprio ou de outro user, se admin)",
    )
    @app_commands.describe(
        user_id="(admin only) bot_user_id ULID, 'discord:<id>' ou snowflake — em branco = você",
        busca="palavra-chave (LIKE case-insensitive)",
        limite="quantas mensagens trazer (default 20, máx 100)",
    )
    async def historico(
        self,
        ctx: commands.Context,
        user_id: Optional[str] = None,
        busca: Optional[str] = None,
        limite: int = 20,
    ) -> None:
        await ctx.defer(ephemeral=True)
        is_owner = self._is_owner(ctx.author.id)

        # Cap the limit to keep messages within Discord's 2000-char window.
        limit = max(1, min(int(limite), 100))

        # Admin sem user_id e sem busca: atalho — listar users disponíveis
        # antes de mostrar mensagens. Caller pode ler a lista e re-invocar
        # /historico user_id:<X>.
        if is_owner and not user_id:
            users = await self._store.list_users(limit=50)
            if not users:
                await ctx.send("📭 Nenhum bot_user registrado.", ephemeral=True)
                return
            lines = [f"📋 **{len(users)} user(s) com histórico** — passa um pra ver as mensagens:\n"]
            for u in users:
                pid = f"{u['provider']}:{u['provider_user_id']}"
                lines.append(
                    f"• `{pid}` — **{u['display_name'] or '?'}** "
                    f"({u['msg_count']} msgs, last={u['last_seen_at'][:16].replace('T',' ')})"
                )
            lines.append(f"\n💡 Use: `/historico user_id:{users[0]['provider']}:{users[0]['provider_user_id']}`")
            msg = "\n".join(lines)
            if len(msg) > 1900:
                msg = msg[:1897] + "…"
            await ctx.send(msg, ephemeral=True)
            return

        # Non-owner trying to peek another user.
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
        if not msgs:
            await ctx.send(
                f"📭 Sem mensagens para **{target.display_name}** "
                f"(`{target.provider}:{target.provider_user_id}`)"
                + (f" matching `{busca}`" if busca else ""),
                ephemeral=True,
            )
            return

        header = (
            f"📜 **Histórico de {target.display_name}** "
            f"(`{target.provider}:{target.provider_user_id}`) — "
            f"últimas {len(msgs)}"
            + (f" com `{busca}`" if busca else "")
        )
        # Render newest-first, ASCII arrow for direction. Truncate per line.
        body_lines = []
        for m in msgs:
            arrow = "→" if m.direction == "inbound" else "←"
            ts = m.sent_at.strftime("%m-%d %H:%M")
            text = (m.text or "").replace("\n", " ").strip()
            text = text[:120] + ("…" if len(text) > 120 else "")
            body_lines.append(f"`{ts}` {arrow} {text}")
        full = header + "\n```\n" + "\n".join(body_lines) + "\n```"
        if len(full) > 1900:
            # Chunk via attachment for long histories.
            buf = io.StringIO("\n".join(body_lines))
            file = discord.File(io.BytesIO(buf.getvalue().encode()), filename=f"historico_{target.provider}_{target.provider_user_id}.txt")
            await ctx.send(header + "\n(too large — anexo)", file=file, ephemeral=True)
        else:
            await ctx.send(full, ephemeral=True)

    # ------------------------------------------------------------------
    # /historico_export
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

        # No upper limit for self-export — owner of the data sees it all.
        msgs = await self._store.list_messages_by_user(
            target.bot_user_id, limit=10_000,
        )
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
    # /historico_canais  (admin)
    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="historico_canais",
        description="(admin) Lista todos os bot_users que conversaram com o bot",
    )
    async def historico_canais(self, ctx: commands.Context) -> None:
        await ctx.defer(ephemeral=True)
        if not self._is_owner(ctx.author.id):
            await ctx.send("❌ Apenas o owner pode ver essa lista.", ephemeral=True)
            return
        users = await self._store.list_users(limit=200)
        if not users:
            await ctx.send("📭 Nenhum bot_user registrado.", ephemeral=True)
            return
        lines = [f"📋 **{len(users)} bot_user(s) registrados:**\n"]
        for u in users:
            pid = f"{u['provider']}:{u['provider_user_id']}"
            lines.append(
                f"• `{pid}` — **{u['display_name'] or '?'}** "
                f"({u['msg_count']} msgs, last={u['last_seen_at'][:16].replace('T',' ')})\n"
                f"   `bot_user_id={u['bot_user_id']}`"
            )
        msg = "\n".join(lines)
        if len(msg) > 1900:
            buf = io.StringIO(msg)
            file = discord.File(io.BytesIO(buf.getvalue().encode()), filename="bot_users.txt")
            await ctx.send(f"📋 {len(users)} bot_users (anexo, pra caber)", file=file, ephemeral=True)
        else:
            await ctx.send(msg, ephemeral=True)

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

        # Sessions DB lives in the same data dir as the bot sqlite. Path
        # is configurable via DEILE_BOT_SESSIONS_SQLITE_PATH.
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
