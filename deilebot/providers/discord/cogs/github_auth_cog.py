"""GitHubAuthCog — comandos de login GitHub via Discord (owner-only).

Três comandos slash, todos restritos ao owner:

  /github_login metodo:pat    → abre um modal do Discord; o operador
                                cola o PAT na janela (nunca aparece no
                                histórico do canal).
  /github_login metodo:oauth  → device flow OAuth (RFC 8628); o operador
                                autoriza no navegador e o bot faz polling
                                e avisa o resultado por DM.
  /github_status              → mostra o login GitHub atual (revalidado
                                ao vivo) ou "não autenticado".
  /github_logout              → remove a credencial GitHub do bot.

A lógica de validação, device flow e instalação da credencial vive em
``deilebot.foundation.github_auth.GitHubAuthService`` — o token é
gravado em ``~/.git-credentials`` (modo 0600) e nunca trafega pelo
canal nem fica em variável de ambiente.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

import discord
from discord import app_commands
from discord.ext import commands

from deilebot.foundation.github_auth import GitHubAuthError, GitHubAuthService
from deilebot.foundation.settings import get_bot_settings
from deilebot.providers.discord.cogs.admin_cog import _is_owner_check

logger = logging.getLogger("deilebot.cogs.github_auth")

_COLOR_OK = 0x57F287
_COLOR_WARN = 0xFEE75C
_COLOR_ERR = 0xED4245
_COLOR_INFO = 0x5865F2


def _success_embed(login: str, token_type: str, method: str) -> discord.Embed:
    return discord.Embed(
        title="✅ GitHub conectado",
        color=_COLOR_OK,
        description=(
            f"Autenticado como **`{login}`** ({token_type}, via {method}).\n"
            "A credencial git foi instalada com segurança "
            "(`~/.git-credentials`, modo 0600)."
        ),
    )


def _error_embed(message: str) -> discord.Embed:
    return discord.Embed(
        title="❌ Falha no login GitHub", color=_COLOR_ERR, description=message
    )


class _PatTokenModal(discord.ui.Modal, title="Login GitHub — colar token"):
    """Modal onde o operador cola o PAT — não vai para o histórico do canal."""

    token = discord.ui.TextInput(
        label="Personal Access Token",
        placeholder="ghp_… ou github_pat_…",
        style=discord.TextStyle.short,
        required=True,
        min_length=20,
        max_length=255,
    )

    def __init__(self, service: GitHubAuthService) -> None:
        super().__init__()
        self._service = service

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        raw = str(self.token.value or "").strip()
        try:
            identity = await self._service.install_credentials(raw, method="pat")
        except GitHubAuthError as exc:
            await interaction.followup.send(embed=_error_embed(str(exc)), ephemeral=True)
            return
        except Exception as exc:  # noqa: BLE001 — modal não pode vazar exceção
            logger.exception("github pat install falhou")
            await interaction.followup.send(
                embed=_error_embed(f"erro inesperado: {type(exc).__name__}"),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=_success_embed(identity.login, identity.token_type, "PAT"),
            ephemeral=True,
        )


class GitHubAuthCog(commands.Cog):
    """Cog que expõe /github_login, /github_status e /github_logout."""

    def __init__(self, bot: Any, runtime: Any, adapter: Any) -> None:
        self.bot = bot
        self.runtime = runtime
        self.adapter = adapter
        self._service = GitHubAuthService()
        # Mantém referência forte às tasks de polling para não serem
        # coletadas pelo GC antes de terminar (asyncio não as retém).
        self._bg_tasks: set = set()

    async def _is_owner(self, ctx: commands.Context) -> bool:
        return await _is_owner_check(self.adapter)(ctx)

    @commands.hybrid_command(
        name="github_login",
        description="Autentica o git do bot no GitHub (owner only)",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        metodo="pat = colar token | oauth = login pelo navegador (device flow)",
    )
    async def github_login(
        self, ctx: commands.Context, metodo: Literal["pat", "oauth"] = "pat"
    ) -> None:
        if not await self._is_owner(ctx):
            await ctx.send("⛔ owner only")
            return
        if ctx.interaction is None:
            await ctx.send(
                "Use a slash command `/github_login` — este comando precisa "
                "de uma interação do Discord."
            )
            return
        if metodo == "pat":
            await ctx.interaction.response.send_modal(_PatTokenModal(self._service))
        else:
            await self._run_oauth_flow(ctx)

    async def _run_oauth_flow(self, ctx: commands.Context) -> None:
        """Inicia o device flow e dispara o polling em background."""
        await ctx.interaction.response.defer(ephemeral=True, thinking=True)
        gh = get_bot_settings().github
        try:
            grant = await self._service.start_device_flow(
                gh.oauth_client_id, gh.oauth_scope
            )
        except GitHubAuthError as exc:
            await ctx.interaction.followup.send(
                embed=_error_embed(str(exc)), ephemeral=True
            )
            return

        embed = discord.Embed(
            title="🔐 Login GitHub — device flow",
            color=_COLOR_INFO,
            description=(
                f"1. Abra **{grant.verification_uri}**\n"
                f"2. Digite o código: **`{grant.user_code}`**\n"
                "3. Autorize o aplicativo.\n\n"
                f"Vou verificar automaticamente e te aviso por DM "
                f"(o código expira em ~{max(grant.expires_in // 60, 1)} min)."
            ),
        )
        await ctx.interaction.followup.send(embed=embed, ephemeral=True)

        task = asyncio.create_task(
            self._await_device_authorization(ctx.author, gh.oauth_client_id, grant)
        )
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _await_device_authorization(
        self, author: Any, client_id: str, grant: Any
    ) -> None:
        """Faz polling do device flow e avisa o resultado por DM."""
        try:
            token = await self._service.poll_device_flow(client_id, grant)
            identity = await self._service.install_credentials(token, method="oauth")
        except GitHubAuthError as exc:
            await self._safe_dm(author, _error_embed(str(exc)))
            return
        except Exception as exc:  # noqa: BLE001 — task de background não pode crashar
            logger.exception("github oauth device flow falhou")
            await self._safe_dm(
                author, _error_embed(f"erro inesperado: {type(exc).__name__}")
            )
            return
        await self._safe_dm(
            author, _success_embed(identity.login, identity.token_type, "OAuth")
        )

    async def _safe_dm(self, user: Any, embed: discord.Embed) -> None:
        try:
            await user.send(embed=embed)
        except Exception:  # noqa: BLE001 — DM fechada / sem permissão
            logger.warning(
                "github_auth: não consegui enviar DM ao usuário %s",
                getattr(user, "id", "?"),
            )

    @commands.hybrid_command(
        name="github_status",
        description="Mostra o login GitHub atual do bot (owner only)",
    )
    @app_commands.default_permissions(administrator=True)
    async def github_status(self, ctx: commands.Context) -> None:
        if not await self._is_owner(ctx):
            await ctx.send("⛔ owner only")
            return
        await ctx.defer(ephemeral=True)
        identity = await self._service.current_identity()
        if identity is None:
            meta = self._service.stored_metadata()
            if meta:
                embed = discord.Embed(
                    title="⚠️ GitHub — token inválido",
                    color=_COLOR_WARN,
                    description=(
                        f"Havia um login (`{meta.get('login', '?')}`), mas o token "
                        "não é mais válido. Refaça o `/github_login`."
                    ),
                )
            else:
                embed = discord.Embed(
                    title="🔓 GitHub — não autenticado",
                    color=_COLOR_WARN,
                    description="Nenhum login ativo. Use `/github_login` para autenticar.",
                )
            await ctx.send(embed=embed, ephemeral=True)
            return

        meta = self._service.stored_metadata() or {}
        embed = discord.Embed(title="🔐 GitHub — autenticado", color=_COLOR_OK)
        embed.add_field(name="Login", value=f"`{identity.login}`", inline=True)
        if identity.name:
            embed.add_field(name="Nome", value=identity.name, inline=True)
        embed.add_field(name="Tipo de token", value=identity.token_type, inline=True)
        embed.add_field(
            name="Escopos", value=f"`{identity.scopes or '—'}`", inline=False
        )
        embed.add_field(name="Método", value=meta.get("method", "?"), inline=True)
        obtained = str(meta.get("obtained_at") or "")
        if obtained:
            embed.add_field(
                name="Desde", value=obtained[:19].replace("T", " "), inline=True
            )
        await ctx.send(embed=embed, ephemeral=True)

    @commands.hybrid_command(
        name="github_logout",
        description="Remove o login GitHub do bot (owner only)",
    )
    @app_commands.default_permissions(administrator=True)
    async def github_logout(self, ctx: commands.Context) -> None:
        if not await self._is_owner(ctx):
            await ctx.send("⛔ owner only")
            return
        await ctx.defer(ephemeral=True)
        removed = await self._service.logout()
        if removed:
            embed = discord.Embed(
                title="🔓 GitHub — logout",
                color=_COLOR_OK,
                description="Credencial do GitHub removida do bot.",
            )
        else:
            embed = discord.Embed(
                title="🔓 GitHub — nada a remover",
                color=_COLOR_WARN,
                description="Não havia nenhum login GitHub ativo.",
            )
        await ctx.send(embed=embed, ephemeral=True)
