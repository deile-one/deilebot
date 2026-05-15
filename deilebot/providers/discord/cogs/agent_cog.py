"""AgentCog — `/deile <prompt>` is a PASSTHROUGH directo ao worker.

Diferente do fluxo normal (DM → bot LLM persona → talvez chama
``dispatch_deile_task``), o ``/deile`` é um atalho explícito: o operador
quer que o pedido vá DIRETO ao ``deile-worker`` sem o LLM do bot
intermediar e re-narrar em cima do que o worker fez. Isso elimina a
"dupla narrativa" (bot fala + worker fala) que o usuário reclamou.

Pipeline atual do ``/deile prompt``::

    1. Safety gate (regex deny-fast pra padrões catastróficos).
    2. Persiste inbound no DB (a slash command não passa por
       ``on_message``, então o histórico precisa do record manual).
    3. Dispara ``DispatchDeileTaskTool`` direto — o worker posta sua
       status message no canal e edita live.
    4. Worker termina → status message tem o resultado final.
    5. Cog fecha o deferred ``"DEILE está pensando…"``.

Não há ``M3`` de "bot narrando" — o usuário vê só o que o worker
mostra, sem ruído.

Safety gate (``_safety_check``)
-------------------------------
Regex deny-fast para pedidos catastróficos. Não é antimalware; é só
um filtro para o operador NÃO disparar acidentalmente algo que o
worker (mesmo sandboxed) executaria. O worker continua isolado em
K8s; o gate é UX/defesa-em-profundidade.

Para pedidos no limite, pode rejeitar e o operador reformula. Em
contexto que precisa de explicação (operador tá puxando um log de
incidente, por exemplo), o caminho é a DM normal — o LLM do bot lê
contexto e decide.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from deile.common.markup_ast import MarkupAST
from deile.tools.base import ToolContext
from deile.tools.dispatch_deile_task import DispatchDeileTaskTool
from deilebot.foundation.envelope import (BotUser, Channel, ChannelScope,
                                           MessageEnvelope)

_logger = logging.getLogger("deilebot.agent_cog")


# Each tuple = (compiled regex, human label).  Order doesn't matter; first
# match wins. Patterns are conservative — they only catch CLEARLY
# catastrophic operations or explicit malicious intent. Edge cases that
# look risky but might be legit (e.g. "rm -rf node_modules") pass through.
_DANGEROUS_PATTERNS: list[Tuple[re.Pattern[str], str]] = [
    # System-host destruction
    (re.compile(r"\brm\s+-rf\s+(/|~|\$HOME)(\s|$|\W)", re.IGNORECASE),
     "rm -rf em raiz do sistema"),
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}", re.IGNORECASE),
     "fork bomb"),
    (re.compile(r"\bdd\s+if=/dev/(zero|random|urandom)\b.*\bof=/?dev", re.IGNORECASE),
     "wipe de disco"),
    (re.compile(r"\bmkfs\.\w+\s+/dev/", re.IGNORECASE),
     "formatar disco"),
    (re.compile(r">\s*/dev/(sd[a-z]|nvme|hd[a-z])", re.IGNORECASE),
     "sobrescrever device de bloco"),
    (re.compile(r"\bshutdown\s+(-h|now|\+\d)\b", re.IGNORECASE),
     "desligar máquina"),
    # Explicit malicious intent
    (re.compile(r"\binvad(ir|e|am|ido|indo)\b.*\b(servidor|sistema|m[aá]quina|host|conta|empresa|rede)", re.IGNORECASE),
     "invasão"),
    (re.compile(r"\b(hack(ear|eando|er)?|crackear|crack|brute[\s-]?force)\b.*\b(senha|password|conta|account|sistema|login|hash|email|gmail|facebook|instagram|twitter|wifi)", re.IGNORECASE),
     "hacking de credenciais"),
    (re.compile(r"\b(ddos|d\.?d\.?o\.?s)\b", re.IGNORECASE),
     "ataque DDoS"),
    (re.compile(r"\b(stealer|keylogger|backdoor|rootkit|ransomware)\b", re.IGNORECASE),
     "malware"),
    (re.compile(r"\bexploit\b.*\b(zero[\s-]?day|0[\s-]?day|cve)\b", re.IGNORECASE),
     "exploit"),
    (re.compile(r"\b(phishing|spoofing)\b.*\b(email|conta|usu[aá]rio)", re.IGNORECASE),
     "phishing"),
    # Exfiltration / data theft
    (re.compile(r"\b(exfiltra|roubar)\b.*\b(senha|password|token|key|secret|credentials)", re.IGNORECASE),
     "exfiltração de credenciais"),
]


def _safety_check(prompt: str) -> Tuple[bool, str]:
    """Return ``(allowed, reason)`` — reason is the human label that matched."""
    if not prompt or not prompt.strip():
        return False, "prompt vazio"
    for pattern, label in _DANGEROUS_PATTERNS:
        m = pattern.search(prompt)
        if m:
            return False, f"{label}  (trecho: `{m.group(0)[:60]}`)"
    return True, ""


class AgentCog(commands.Cog):
    """``/deile`` slash command — passthrough direto ao worker."""

    def __init__(self, bot: Any, runtime: Any, adapter: Any):
        self.bot = bot
        self.runtime = runtime
        self.adapter = adapter
        # Single instance; tool is stateless beyond its schema.
        self._dispatch_tool = DispatchDeileTaskTool()

    def _make_envelope(self, ctx: commands.Context, prompt: str) -> MessageEnvelope:
        ch_obj = ctx.channel
        scope = (
            ChannelScope.DM
            if isinstance(ch_obj, discord.DMChannel)
            else ChannelScope.THREAD if isinstance(ch_obj, discord.Thread)
            else ChannelScope.GROUP
        )
        parent_id = None
        if scope == ChannelScope.THREAD and getattr(ch_obj, "parent", None):
            parent_id = str(ch_obj.parent.id)
        channel = Channel(
            provider="discord",
            provider_channel_id=str(ch_obj.id),
            name=getattr(ch_obj, "name", None),
            scope=scope,
            parent_channel_id=parent_id,
        )
        author = BotUser(
            bot_user_id=f"discord-{ctx.author.id}",
            provider="discord",
            provider_user_id=str(ctx.author.id),
            display_name=getattr(ctx.author, "display_name", None) or ctx.author.name,
            is_bot=bool(getattr(ctx.author, "bot", False)),
        )
        msg_id = (
            str(ctx.message.id) if ctx.message
            else f"slash-{int(datetime.now(timezone.utc).timestamp() * 1000)}"
        )
        return MessageEnvelope(
            message_id=msg_id,
            channel=channel,
            author=author,
            sent_at=datetime.now(timezone.utc),
            text=prompt,
            markup=MarkupAST.from_plain(prompt),
            raw=MappingProxyType({"force_respond": True, "source": "slash:/deile"}),
        )

    @commands.hybrid_command(
        name="deile",
        description="Passthrough direto ao DEILE worker (sem o bot narrar em cima)",
    )
    @app_commands.describe(
        prompt="O que o DEILE deve fazer (vai DIRETO ao worker; sujeito a safety gate)",
    )
    async def deile(self, ctx: commands.Context, *, prompt: str) -> None:
        await ctx.defer(ephemeral=False)
        try:
            # 1. Safety gate
            allowed, reason = _safety_check(prompt)
            if not allowed:
                await ctx.send(
                    f"❌ **Pedido bloqueado por safety:** {reason}\n\n"
                    "Reformule sem termos sensíveis. Ou, se há contexto "
                    "legítimo (ex: arquivos temporários, repo específico), "
                    "abra uma DM normal — o LLM lê contexto antes de agir.",
                )
                return

            # 2. Persist inbound — slash não passa por on_message, então o
            #    /historico precisa do record manual pra a pergunta aparecer.
            env = self._make_envelope(ctx, prompt)
            store = self.runtime.pipeline.store
            try:
                await store.upsert_user(env.author)
                await store.record_inbound(env)
            except Exception:
                _logger.exception("persist inbound failed (continuing)")

            # 3. Direct dispatch — bypass bot LLM persona narrative.
            channel_id = str(ctx.channel.id)
            user_message_id = str(ctx.message.id) if ctx.message else None
            tool_ctx = ToolContext(
                parsed_args={
                    "brief": prompt,
                    "channel_id": channel_id,
                    "user_message_id": user_message_id,
                    "persona": "developer",
                    "wait_for_result": True,
                },
                session_data={
                    "bot_context": {
                        "channel_id": channel_id,
                        "user_message_id": user_message_id,
                    }
                },
            )
            result = await self._dispatch_tool.execute(tool_ctx)

            # Worker já postou + editou a status message com o resultado
            # final. Não enviamos M3 — exatamente o que o operador pediu:
            # sem dupla narrativa. Só sinalizamos erro se o dispatch
            # FALHOU antes do worker conseguir postar.
            if not result.is_success:
                code = getattr(result, "error_code", None) or "UNKNOWN"
                msg = (getattr(result, "message", None) or "")[:300]
                await ctx.send(f"❌ Dispatch falhou: `{code}` — {msg}")
        finally:
            # Always cleanup the deferred placeholder, even if anything
            # above raised. Without this the "DEILE está pensando…"
            # lingers and times out with an empty reply.
            if ctx.interaction is not None:
                try:
                    await ctx.interaction.delete_original_response()
                except Exception:
                    pass
