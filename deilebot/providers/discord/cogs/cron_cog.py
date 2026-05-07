"""CronCog — slash commands for scheduling DEILE tasks.

Commands:
    /agendar prompt:<text> quando:<text>   Schedule a new task.
    /agendamentos                           List scheduled tasks.
    /cancelar id:<text>                     Cancel a task by id.

``quando`` accepts either:
    - ISO 8601 datetime (e.g. ``2025-01-15T14:30:00``) for a one-shot run.
    - A 5-field cron expression (e.g. ``*/5 * * * *``) for recurring tasks.

Dependencies:
    ``deile.cron.store.CronStore`` — accessed directly (no tool layer).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from discord import app_commands
from discord.ext import commands

from deilebot.foundation.logging import get_logger

_logger = get_logger("cron_cog")

# A 5-field cron string: each field is digits, *, or digit-range/step patterns.
_CRON_FIELD = r"(\*|[0-9*/,-]+)"
_CRON_RE = re.compile(
    rf"^{_CRON_FIELD}\s+{_CRON_FIELD}\s+{_CRON_FIELD}\s+{_CRON_FIELD}\s+{_CRON_FIELD}$"
)


def _parse_quando(quando: str) -> tuple[str | None, datetime | None]:
    """Return ``(cron_expr, run_at)`` — exactly one will be non-None.

    Raises ``ValueError`` for inputs that are neither a valid ISO datetime nor
    a syntactically valid 5-field cron expression.
    """
    stripped = quando.strip()

    # ISO datetime heuristic: contains 'T' or '-' and no multiple spaces
    if _CRON_RE.match(stripped):
        return stripped, None

    # Attempt ISO datetime parse
    try:
        dt = datetime.fromisoformat(stripped)
        # Attach UTC if naive
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return None, dt
    except ValueError:
        pass

    raise ValueError(
        f"Não foi possível interpretar '{quando}' como data ISO ou expressão cron (5 campos)."
    )


class CronCog(commands.Cog):
    """Cog that exposes scheduling commands backed by CronStore."""

    def __init__(self, bot: Any, store: Any) -> None:
        self.bot = bot
        self.store = store

    # ------------------------------------------------------------------
    # /agendar
    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="agendar",
        description="Agenda uma tarefa para o agente DEILE",
    )
    @app_commands.describe(
        prompt="O que o agente deve fazer",
        quando="Data ISO (2025-01-15T14:30) ou cron de 5 campos (*/5 * * * *)",
    )
    async def agendar(self, ctx: commands.Context, *, prompt: str, quando: str) -> None:
        await ctx.defer(ephemeral=False)
        try:
            cron_expr, run_at = _parse_quando(quando)
        except ValueError as exc:
            await ctx.send(f"❌ {exc}")
            return

        try:
            task_id = await self.store.create(
                prompt=prompt,
                cron_expr=cron_expr,
                run_at=run_at,
            )
            if cron_expr:
                await ctx.send(
                    f"✅ Tarefa agendada (recorrente)!\n"
                    f"🆔 `{task_id}`\n"
                    f"🕐 Cron: `{cron_expr}`\n"
                    f"📝 {prompt}"
                )
            else:
                run_at_str = run_at.isoformat() if run_at else "?"
                await ctx.send(
                    f"✅ Tarefa agendada!\n"
                    f"🆔 `{task_id}`\n"
                    f"🕐 Em: {run_at_str}\n"
                    f"📝 {prompt}"
                )
        except Exception as exc:
            _logger.error("agendar store.create failed: %s", exc, exc_info=True)
            await ctx.send(f"⚠️ Não foi possível agendar a tarefa: {exc}")

    # ------------------------------------------------------------------
    # /agendamentos
    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="agendamentos",
        description="Lista as tarefas agendadas no DEILE",
    )
    async def agendamentos(self, ctx: commands.Context) -> None:
        await ctx.defer(ephemeral=False)
        try:
            tasks = await self.store.list_all()
        except Exception as exc:
            _logger.error("agendamentos store.list_all failed: %s", exc, exc_info=True)
            await ctx.send(f"⚠️ Não foi possível listar as tarefas: {exc}")
            return

        if not tasks:
            await ctx.send("📭 Nenhuma tarefa agendada no momento.")
            return

        lines = ["📋 **Tarefas agendadas:**"]
        for task in tasks:
            task_id = task.get("id", "?")
            prompt = task.get("prompt", "")
            cron_expr = task.get("cron_expr")
            run_at = task.get("run_at")
            status = task.get("status", "pending")

            if cron_expr:
                schedule_str = f"cron: `{cron_expr}`"
            elif run_at:
                schedule_str = f"em: {run_at}"
            else:
                schedule_str = "sem agendamento"

            short_prompt = (prompt[:80] + "…") if len(prompt) > 80 else prompt
            lines.append(f"• `{task_id}` [{status}] {schedule_str} — {short_prompt}")

        message = "\n".join(lines)
        # Discord message limit is 2000 chars
        if len(message) > 1900:
            message = message[:1897] + "…"

        await ctx.send(message)

    # ------------------------------------------------------------------
    # /cancelar
    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="cancelar",
        description="Cancela uma tarefa agendada pelo seu ID",
    )
    @app_commands.describe(id="ID da tarefa a cancelar")
    async def cancelar(self, ctx: commands.Context, *, id: str) -> None:
        await ctx.defer(ephemeral=False)
        task_id = id.strip()
        if not task_id:
            await ctx.send("❌ Informe o ID da tarefa a cancelar.")
            return

        try:
            deleted = await self.store.delete(task_id)
        except Exception as exc:
            _logger.error("cancelar store.delete failed: %s", exc, exc_info=True)
            await ctx.send(f"⚠️ Não foi possível cancelar a tarefa: {exc}")
            return

        if deleted:
            await ctx.send(f"✅ Tarefa `{task_id}` cancelada com sucesso.")
        else:
            await ctx.send(f"❌ Tarefa `{task_id}` não encontrada.")
