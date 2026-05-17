"""slash_dispatch — shared core of the /deile passthrough.

Both ``providers.discord.cogs.agent_cog.AgentCog.deile`` (real slash
command) and the ``/v1/test/simulate`` harness (operator-driven
integration tests) invoke this function. Extracting the logic ensures
the harness exercises the SAME safety check + dispatch path that real
``/deile`` users do — no mock, no drift.

The function does NOT depend on ``discord.py`` Context objects. It
takes plain values (prompt, channel_id, user info) plus references to
the store and identity resolver. Callers stay responsible for the
UX layer (defer, delete_original_response, reply text formatting).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, List, Optional, Tuple

from deile.common.markup_ast import MarkupAST
from deile.tools.base import ToolContext

from deilebot.foundation.envelope import (BotUser, Channel, ChannelScope,
                                           MessageEnvelope)

_logger = logging.getLogger("deilebot.slash_dispatch")


# Each tuple = (compiled regex, human label). First match wins.
_DANGEROUS_PATTERNS: List[Tuple[re.Pattern[str], str]] = [
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
    (re.compile(r"\b(exfiltra|roubar)\b.*\b(senha|password|token|key|secret|credentials)", re.IGNORECASE),
     "exfiltração de credenciais"),
]


def safety_check(prompt: str) -> Tuple[bool, str]:
    """Return ``(allowed, reason)`` — reason is the human label that matched."""
    if not prompt or not prompt.strip():
        return False, "prompt vazio"
    for pattern, label in _DANGEROUS_PATTERNS:
        m = pattern.search(prompt)
        if m:
            return False, f"{label}  (trecho: `{m.group(0)[:60]}`)"
    return True, ""


@dataclass
class SlashDispatchResult:
    """Outcome of a ``/deile`` invocation.

    Fields:
        kind:           one of ``"blocked"``, ``"dispatched"``, ``"error"``.
        reason:         human reason — populated when ``kind="blocked"`` or
                        ``"error"``. Empty when ``kind="dispatched"``.
        tool_result:    when ``kind="dispatched"`` or ``"error"`` from the
                        worker side — the underlying ToolResult.
        message_id:     synthetic id assigned to the inbound envelope (for
                        log correlation).
    """

    kind: str
    reason: str = ""
    tool_result: Any = None
    message_id: str = ""
    metadata: dict = field(default_factory=dict)


# ``DispatchDeileTaskTool`` é importada SOB DEMANDA. O módulo
# ``deile.tools.dispatch_deile_task`` é uma dependência OPCIONAL — só o
# caminho /deile a usa. Importá-la no topo deste módulo fazia um
# ModuleNotFoundError (quando o worker stack não está instalado) derrubar
# a carga de TODOS os cogs em ``adapter.on_ready`` — inclusive os que nada
# têm a ver com dispatch. Com o import tardio, a ausência só afeta /deile.
_DISPATCH_TOOL: Any = None


def _get_dispatch_tool() -> Any:
    """Devolve o ``DispatchDeileTaskTool`` (singleton), importando sob demanda.

    Levanta ``ImportError`` se ``deile.tools.dispatch_deile_task`` não estiver
    disponível — o chamador trata como erro de /deile, sem derrubar o bot.
    """
    global _DISPATCH_TOOL
    if _DISPATCH_TOOL is None:
        from deile.tools.dispatch_deile_task import DispatchDeileTaskTool

        _DISPATCH_TOOL = DispatchDeileTaskTool()
    return _DISPATCH_TOOL


def build_envelope(
    *,
    prompt: str,
    user_id: str,
    display_name: str,
    channel_id: str,
    channel_scope: ChannelScope = ChannelScope.DM,
    channel_name: Optional[str] = None,
    is_bot: bool = False,
    user_message_id: Optional[str] = None,
) -> MessageEnvelope:
    """Construct a synthetic MessageEnvelope mimicking the real slash flow."""
    channel = Channel(
        provider="discord",
        provider_channel_id=channel_id,
        name=channel_name,
        scope=channel_scope,
    )
    author = BotUser(
        bot_user_id=f"discord-{user_id}",
        provider="discord",
        provider_user_id=str(user_id),
        display_name=display_name,
        is_bot=is_bot,
    )
    msg_id = user_message_id or (
        f"slash-{int(datetime.now(timezone.utc).timestamp() * 1000)}"
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


async def run_slash_dispatch(
    *,
    prompt: str,
    user_id: str,
    display_name: str,
    channel_id: str,
    store: Any,
    identity: Any,
    channel_scope: ChannelScope = ChannelScope.DM,
    channel_name: Optional[str] = None,
) -> SlashDispatchResult:
    """Core /deile pipeline: safety → persist inbound → dispatch to worker.

    Returns a :class:`SlashDispatchResult` describing what happened. The
    caller is responsible for surfacing the outcome (Discord ctx.send,
    HTTP response, etc).
    """
    # 1. Safety gate
    allowed, reason = safety_check(prompt)
    if not allowed:
        _logger.info("slash_dispatch: BLOCKED — %s", reason)
        return SlashDispatchResult(kind="blocked", reason=reason)

    # 2. Persist inbound — slash não passa por on_message, então o
    #    /historico precisa do record manual com bot_user_id ULID
    #    resolvido pelo IdentityResolver.
    env = build_envelope(
        prompt=prompt,
        user_id=user_id,
        display_name=display_name,
        channel_id=channel_id,
        channel_scope=channel_scope,
        channel_name=channel_name,
    )
    try:
        real_user = await identity.resolve(
            provider="discord",
            provider_user_id=str(user_id),
            display_name=display_name,
        )
        await store.record_inbound(env, bot_user_id_override=real_user.bot_user_id)
    except Exception:
        _logger.exception("slash_dispatch: persist inbound failed (continuing)")

    # 3. Dispatch to worker — direct call, no LLM intermediary.
    tool_ctx = ToolContext(
        user_input=prompt,
        parsed_args={
            "brief": prompt,
            "channel_id": channel_id,
            "user_message_id": env.message_id,
            "persona": "developer",
            "wait_for_result": True,
        },
        session_data={
            "bot_context": {
                "channel_id": channel_id,
                "user_message_id": env.message_id,
            }
        },
    )
    try:
        dispatch_tool = _get_dispatch_tool()
    except ImportError as exc:
        _logger.warning("slash_dispatch: DispatchDeileTaskTool indisponível — %s", exc)
        return SlashDispatchResult(
            kind="error",
            reason="worker dispatch indisponível (deile.tools.dispatch_deile_task ausente)",
            message_id=env.message_id,
        )
    try:
        result = await dispatch_tool.execute(tool_ctx)
    except Exception as exc:  # noqa: BLE001
        _logger.exception("slash_dispatch: dispatch tool raised")
        return SlashDispatchResult(
            kind="error",
            reason=f"{type(exc).__name__}: {exc}",
            message_id=env.message_id,
        )

    if not result.is_success:
        # ToolResult.error_code is stored in metadata (see base.py:286)
        meta = getattr(result, "metadata", {}) or {}
        code = (
            meta.get("error_code")
            or getattr(result, "error_code", None)  # newer API path
            or "UNKNOWN"
        )
        msg = (getattr(result, "message", None) or "")[:300]
        _logger.info("slash_dispatch: dispatch error code=%s msg=%s", code, msg)
        return SlashDispatchResult(
            kind="error",
            reason=f"{code}: {msg}",
            tool_result=result,
            message_id=env.message_id,
        )

    return SlashDispatchResult(
        kind="dispatched",
        tool_result=result,
        message_id=env.message_id,
    )
