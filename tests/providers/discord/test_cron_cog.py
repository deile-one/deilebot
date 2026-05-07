"""Tests for CronCog — /agendar, /agendamentos, /cancelar slash commands."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(
    *,
    author_id: int = 1,
    author_name: str = "testuser",
    channel_id: int = 200,
    has_interaction: bool = False,
) -> MagicMock:
    ctx = MagicMock()
    ctx.defer = AsyncMock()
    ctx.send = AsyncMock()
    ctx.channel.id = channel_id
    ctx.author.id = author_id
    ctx.author.display_name = author_name
    ctx.author.name = author_name
    ctx.interaction = MagicMock() if has_interaction else None
    return ctx


def _make_store(
    *,
    create_return: str = "abc-123",
    list_return: list | None = None,
    delete_return: bool = True,
) -> MagicMock:
    """Return a mock CronStore."""
    store = MagicMock()
    store.create = AsyncMock(return_value=create_return)
    store.list_all = AsyncMock(return_value=list_return if list_return is not None else [])
    store.delete = AsyncMock(return_value=delete_return)
    return store


def _make_cog(store: MagicMock) -> "CronCog":
    from deilebot.providers.discord.cogs.cron_cog import CronCog

    cog = CronCog.__new__(CronCog)
    cog.bot = None
    cog.store = store
    return cog


async def _call_agendar(cog, ctx, prompt: str, quando: str) -> None:
    await cog.agendar.callback(cog, ctx, prompt=prompt, quando=quando)


async def _call_agendamentos(cog, ctx) -> None:
    await cog.agendamentos.callback(cog, ctx)


async def _call_cancelar(cog, ctx, id: str) -> None:
    await cog.cancelar.callback(cog, ctx, id=id)


# ---------------------------------------------------------------------------
# _parse_quando unit tests
# ---------------------------------------------------------------------------

class TestParseQuando:
    def test_valid_cron_expression(self):
        from deilebot.providers.discord.cogs.cron_cog import _parse_quando

        cron, run_at = _parse_quando("*/5 * * * *")
        assert cron == "*/5 * * * *"
        assert run_at is None

    def test_valid_iso_datetime_naive(self):
        from deilebot.providers.discord.cogs.cron_cog import _parse_quando

        cron, run_at = _parse_quando("2025-06-15T10:30:00")
        assert cron is None
        assert run_at is not None
        assert run_at.year == 2025
        assert run_at.tzinfo == timezone.utc

    def test_valid_iso_datetime_with_tz(self):
        from deilebot.providers.discord.cogs.cron_cog import _parse_quando

        cron, run_at = _parse_quando("2025-06-15T10:30:00+00:00")
        assert cron is None
        assert run_at is not None

    def test_invalid_input_raises_value_error(self):
        from deilebot.providers.discord.cogs.cron_cog import _parse_quando

        with pytest.raises(ValueError):
            _parse_quando("not a cron or date")

    def test_invalid_cron_four_fields_raises(self):
        from deilebot.providers.discord.cogs.cron_cog import _parse_quando

        with pytest.raises(ValueError):
            _parse_quando("* * * *")  # only 4 fields


# ---------------------------------------------------------------------------
# /agendar — happy paths
# ---------------------------------------------------------------------------

class TestAgendar:
    async def test_agendar_with_iso_datetime_creates_task(self):
        store = _make_store(create_return="task-001")
        cog = _make_cog(store)
        ctx = _make_ctx()

        await _call_agendar(cog, ctx, "enviar relatório", "2025-08-01T09:00:00")

        store.create.assert_awaited_once()
        call_kwargs = store.create.call_args.kwargs
        assert call_kwargs["prompt"] == "enviar relatório"
        assert call_kwargs["cron_expr"] is None
        assert isinstance(call_kwargs["run_at"], datetime)

        sent_text = ctx.send.call_args[0][0]
        assert "task-001" in sent_text
        assert "✅" in sent_text

    async def test_agendar_with_cron_creates_recurring_task(self):
        store = _make_store(create_return="task-cron-99")
        cog = _make_cog(store)
        ctx = _make_ctx()

        await _call_agendar(cog, ctx, "verificar pipeline", "0 8 * * 1")

        call_kwargs = store.create.call_args.kwargs
        assert call_kwargs["cron_expr"] == "0 8 * * 1"
        assert call_kwargs["run_at"] is None

        sent_text = ctx.send.call_args[0][0]
        assert "task-cron-99" in sent_text
        assert "recorrente" in sent_text.lower() or "0 8 * * 1" in sent_text

    async def test_agendar_invalid_quando_sends_error(self):
        store = _make_store()
        cog = _make_cog(store)
        ctx = _make_ctx()

        await _call_agendar(cog, ctx, "tarefa", "invalid-date-or-cron")

        ctx.send.assert_awaited_once()
        sent_text = ctx.send.call_args[0][0]
        assert "❌" in sent_text
        store.create.assert_not_awaited()

    async def test_agendar_store_error_sends_warning(self):
        store = _make_store()
        store.create = AsyncMock(side_effect=RuntimeError("DB unavailable"))
        cog = _make_cog(store)
        ctx = _make_ctx()

        await _call_agendar(cog, ctx, "tarefa", "*/10 * * * *")

        sent_text = ctx.send.call_args[0][0]
        assert "⚠️" in sent_text


# ---------------------------------------------------------------------------
# /agendamentos — happy paths
# ---------------------------------------------------------------------------

class TestAgendamentos:
    async def test_agendamentos_empty_list(self):
        store = _make_store(list_return=[])
        cog = _make_cog(store)
        ctx = _make_ctx()

        await _call_agendamentos(cog, ctx)

        sent_text = ctx.send.call_args[0][0]
        assert "📭" in sent_text

    async def test_agendamentos_lists_tasks(self):
        tasks = [
            {"id": "t1", "prompt": "checar logs", "cron_expr": "0 * * * *", "run_at": None, "status": "pending"},
            {"id": "t2", "prompt": "backup db", "cron_expr": None, "run_at": "2025-09-01T00:00:00", "status": "pending"},
        ]
        store = _make_store(list_return=tasks)
        cog = _make_cog(store)
        ctx = _make_ctx()

        await _call_agendamentos(cog, ctx)

        sent_text = ctx.send.call_args[0][0]
        assert "t1" in sent_text
        assert "t2" in sent_text
        assert "checar logs" in sent_text

    async def test_agendamentos_store_error_sends_warning(self):
        store = _make_store()
        store.list_all = AsyncMock(side_effect=RuntimeError("DB error"))
        cog = _make_cog(store)
        ctx = _make_ctx()

        await _call_agendamentos(cog, ctx)

        sent_text = ctx.send.call_args[0][0]
        assert "⚠️" in sent_text


# ---------------------------------------------------------------------------
# /cancelar — happy paths and error handling
# ---------------------------------------------------------------------------

class TestCancelar:
    async def test_cancelar_existing_task_succeeds(self):
        store = _make_store(delete_return=True)
        cog = _make_cog(store)
        ctx = _make_ctx()

        await _call_cancelar(cog, ctx, "task-001")

        store.delete.assert_awaited_once_with("task-001")
        sent_text = ctx.send.call_args[0][0]
        assert "✅" in sent_text
        assert "task-001" in sent_text

    async def test_cancelar_not_found_sends_not_found(self):
        store = _make_store(delete_return=False)
        cog = _make_cog(store)
        ctx = _make_ctx()

        await _call_cancelar(cog, ctx, "nonexistent-id")

        sent_text = ctx.send.call_args[0][0]
        assert "❌" in sent_text
        assert "nonexistent-id" in sent_text

    async def test_cancelar_empty_id_sends_error(self):
        store = _make_store()
        cog = _make_cog(store)
        ctx = _make_ctx()

        await _call_cancelar(cog, ctx, "   ")

        sent_text = ctx.send.call_args[0][0]
        assert "❌" in sent_text
        store.delete.assert_not_awaited()

    async def test_cancelar_store_error_sends_warning(self):
        store = _make_store()
        store.delete = AsyncMock(side_effect=RuntimeError("DB crash"))
        cog = _make_cog(store)
        ctx = _make_ctx()

        await _call_cancelar(cog, ctx, "task-xyz")

        sent_text = ctx.send.call_args[0][0]
        assert "⚠️" in sent_text
