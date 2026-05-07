"""deilebot CLI — `python3 -m deilebot.cli ...` entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("deilebot.cli")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="deilebot", description="DEILE bot runtime")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Run a provider adapter")
    p_run.add_argument("--provider", required=True, choices=["discord", "telegram", "whatsapp", "meta"])
    p_run.add_argument("--guild-id", action="append", type=int, dest="guild_ids", default=None)

    p_dlq = sub.add_parser("dlq", help="DLQ ops")
    sub_dlq = p_dlq.add_subparsers(dest="dlq_action", required=True)
    p_dlq_list = sub_dlq.add_parser("list")
    p_dlq_list.add_argument("--provider")
    p_dlq_purge = sub_dlq.add_parser("purge")
    p_dlq_purge.add_argument("--older-than-days", type=int, default=30)

    p_sessions = sub.add_parser("sessions", help="Session ops")
    sub_sessions = p_sessions.add_subparsers(dest="sessions_action", required=True)
    p_sessions_purge = sub_sessions.add_parser("purge")
    p_sessions_purge.add_argument("--older-than-days", type=int, default=30)
    sub_sessions.add_parser("list")

    sub.add_parser("metrics", help="Print metrics snapshot")

    p_migrate = sub.add_parser("migrate-memory-json", help="Migrate legacy memory.json to SQLite")
    p_migrate.add_argument("--source", required=True)
    p_migrate.add_argument("--target-db", default="data/deilebot.sqlite")
    p_migrate.add_argument("--dry-run", action="store_true")

    p_persona = sub.add_parser("persona", help="Persona ops")
    sub_persona = p_persona.add_subparsers(dest="persona_action", required=True)
    sub_persona.add_parser("list")

    return parser


async def _run_provider(provider: str, guild_ids: Optional[List[int]] = None) -> int:
    if provider != "discord":
        print(f"provider {provider} not yet wired in this branch", file=sys.stderr)
        return 2
    from deilebot.foundation.audit import BotAuditLogger
    from deilebot.foundation.capabilities import CapabilityCatalog
    from deilebot.foundation.conversation_store import ConversationStore
    from deilebot.foundation.dlq import DeadLetterQueue
    from deilebot.foundation.event_bus import BotEventBus
    from deilebot.foundation.identity import IdentityResolver
    from deilebot.foundation.intent import build_intent_classifier
    from deilebot.foundation.logging import setup_logging
    from deilebot.foundation.metrics import MetricsCollector
    from deilebot.foundation.permissions import PermissionGate
    from deilebot.foundation.persona_selector import PersonaSelector
    from deilebot.foundation.pipeline import EgressPipeline, IngressPipeline
    from deilebot.foundation.rate_limit import RateLimiter
    from deilebot.foundation.settings import get_bot_settings
    from deilebot.providers.discord.adapter import DiscordAdapter
    from deilebot.providers.discord.formatter import DiscordOutputFormatter
    from deilebot.providers.discord.settings import DiscordBotSettings
    from deilebot.runtime.single_runtime import SingleProviderRuntime

    bot_settings = get_bot_settings()
    setup_logging(bot_settings.foundation.log_dir)

    discord_settings = DiscordBotSettings()
    if guild_ids:
        discord_settings.slash_sync_guild_ids = guild_ids

    store = ConversationStore(bot_settings.foundation.sqlite_path)
    await store.init()
    identity = IdentityResolver(store)
    permissions = PermissionGate(bot_settings, identity)
    rate_limit = RateLimiter(bot_settings)
    metrics = MetricsCollector()
    audit = BotAuditLogger(store)
    bus = BotEventBus()
    dlq = DeadLetterQueue(store, bot_settings)
    intent = build_intent_classifier(bot_settings.foundation)

    # Bridge: lazy DEILE agent (in-process)
    from deile.core.agent import DeileAgent
    from deilebot.foundation.agent_bridge import InProcessAgentBridge
    from deilebot.foundation.agent_meta import DeileAgentMetaProvider

    _agent_inst: Optional[DeileAgent] = None

    async def agent_provider():
        nonlocal _agent_inst
        if _agent_inst is None:
            model_router = None
            try:
                try:
                    from dotenv import load_dotenv as _load_dotenv
                    _load_dotenv()
                except ImportError:
                    pass
                from deile.config.manager import ConfigManager
                from deile.core.models.bootstrap import bootstrap_providers
                from deile.core.models.router import get_model_router

                ConfigManager().load_config()
                model_router = get_model_router()
                registered = bootstrap_providers(router=model_router)
                if not registered:
                    raise RuntimeError("bootstrap_providers returned 0 providers — check API keys in .env")
            except Exception as _boot_err:
                import logging as _logging
                _logging.getLogger("deilebot.cli").error(
                    "agent bootstrap failed: %s", _boot_err, exc_info=True
                )
            _agent_inst = DeileAgent(model_router=model_router)
            await _agent_inst.initialize()
        return _agent_inst

    bridge = InProcessAgentBridge(agent_provider)

    class _LazyMeta(DeileAgentMetaProvider):
        async def list_tools(self):
            ag = await agent_provider()
            self._agent = ag
            return await super().list_tools()

        async def list_models(self):
            ag = await agent_provider()
            self._agent = ag
            return await super().list_models()

        async def list_personas(self):
            ag = await agent_provider()
            self._agent = ag
            return await super().list_personas()

    meta = _LazyMeta(None)

    catalog = CapabilityCatalog(bot_settings)
    persona_selector = PersonaSelector(bot_settings, identity)

    formatters = {"discord": DiscordOutputFormatter()}
    egress = EgressPipeline(formatters, rate_limit, store, audit, bus, metrics, dlq)
    ingress = IngressPipeline(
        identity=identity, permissions=permissions, rate_limit=rate_limit, store=store,
        intent=intent, bridge=bridge, capability_catalog=catalog,
        persona_selector=persona_selector, audit=audit, event_bus=bus,
        metrics=metrics, egress=egress, agent_meta=meta,
    )
    adapter = DiscordAdapter(discord_settings)

    # Optional control-plane HTTP server (the deile-cli → deilebot flecha reversa).
    control_plane = None
    try:
        from deilebot.runtime.control_plane import (
            ControlPlaneServer,
            ControlPlaneSettings,
        )

        cp_settings = ControlPlaneSettings()
        if cp_settings.enabled and cp_settings.auth_token:
            control_plane = ControlPlaneServer(cp_settings, version="0.1.0")
            control_plane.audit_logger = audit
            control_plane.identity = identity
        elif cp_settings.enabled and not cp_settings.auth_token:
            import logging as _logging

            _logging.getLogger("deilebot.cli").warning(
                "control_plane disabled: DEILE_BOT_CONTROL_PLANE_AUTH_TOKEN not set"
            )
    except Exception:
        import logging as _logging

        _logging.getLogger("deilebot.cli").exception("control_plane init failed; continuing without it")

    runtime = SingleProviderRuntime(
        adapter,
        ingress,
        capability_catalog=catalog,
        agent_meta=meta,
        formatters=formatters,
        control_plane=control_plane,
    )

    # Optional CronRunner (intent #86)
    cron_runner = None
    if os.environ.get("DEILE_CRON_AUTOSTART") == "1":
        try:
            from deile.cron.store import CronStore
            from deile.cron.runner import CronRunner
            from deile.cron.agent_bridge import make_fire_callback  # may not exist yet
            from deilebot.bridge.dm_tool import send_discord_dm  # legacy import

            cron_db = Path(os.environ.get("DEILE_CRON_DB_PATH", "data/cron.db"))
            cron_store = CronStore(cron_db)

            async def _agent_provider():
                return await agent_provider()

            cron_runner = CronRunner(
                cron_store,
                fire_callback=make_fire_callback(_agent_provider),
                notify_dm=send_discord_dm,
            )
            await cron_runner.start()
            logger.info("CronRunner started")
        except Exception as e:
            logger.warning("CronRunner not started: %s", e)

    # Optional PipelineMonitor (intent #87)
    pipeline_monitor = None
    if os.environ.get("DEILE_PIPELINE_AUTOSTART") == "1":
        try:
            from deile.orchestration.pipeline.monitor import PipelineMonitor, PipelineConfig

            cfg = PipelineConfig(
                repo=os.environ.get("DEILE_PIPELINE_REPO", "elimarcavalli/deile"),
                base_repo_path=Path(os.environ.get("DEILE_PIPELINE_BASE_PATH", ".")),
                notify_user_id=os.environ.get("DEILE_PIPELINE_NOTIFY_USER_ID"),
            )
            pipeline_monitor = PipelineMonitor(cfg)
            await pipeline_monitor.start()
            logger.info("PipelineMonitor started")
        except Exception as e:
            logger.warning("PipelineMonitor not started: %s", e)

    try:
        await runtime.start()
    except KeyboardInterrupt:
        pass
    finally:
        if cron_runner is not None:
            await cron_runner.stop()
        if pipeline_monitor is not None:
            await pipeline_monitor.stop()
        await runtime.stop()
        await store.close()
    return 0


async def _dlq_list(provider: Optional[str]) -> int:
    from deilebot.foundation.conversation_store import ConversationStore
    from deilebot.foundation.dlq import DeadLetterQueue
    from deilebot.foundation.settings import get_bot_settings

    bs = get_bot_settings()
    store = ConversationStore(bs.foundation.sqlite_path)
    await store.init()
    dlq = DeadLetterQueue(store, bs)
    rows = await dlq.list_pending(provider=provider)
    print(json.dumps(rows, indent=2, default=str))
    await store.close()
    return 0


async def _dlq_purge(days: int) -> int:
    from deilebot.foundation.conversation_store import ConversationStore
    from deilebot.foundation.dlq import DeadLetterQueue
    from deilebot.foundation.settings import get_bot_settings

    bs = get_bot_settings()
    store = ConversationStore(bs.foundation.sqlite_path)
    await store.init()
    dlq = DeadLetterQueue(store, bs)
    removed = await dlq.purge(days)
    await store.close()
    print(f"Removed {removed} DLQ rows older than {days} days")
    return 0


async def _sessions_purge(days: int) -> int:
    from deile.core.session_store import SessionStore
    from deilebot.foundation.settings import get_bot_settings

    bs = get_bot_settings()
    store = SessionStore(bs.foundation.sessions_sqlite_path)
    await store.init()
    removed = await store.purge_older_than(days)
    await store.close()
    print(f"Removed {removed} sessions older than {days} days")
    return 0


async def _sessions_list() -> int:
    from deile.core.session_store import SessionStore
    from deilebot.foundation.settings import get_bot_settings

    bs = get_bot_settings()
    store = SessionStore(bs.foundation.sessions_sqlite_path)
    await store.init()
    rows = await store.list_all()
    print(json.dumps(rows, indent=2, default=str))
    await store.close()
    return 0


async def _metrics_snapshot() -> int:
    """Print an empty snapshot — without a running runtime there is no live data."""
    from deilebot.foundation.metrics import MetricsCollector

    m = MetricsCollector()
    print(json.dumps(m.snapshot(), indent=2, default=str))
    return 0


async def _migrate_memory_json(source: str, target: str, dry_run: bool) -> int:
    from scripts.migrate_memory_json_to_sqlite import migrate

    return await migrate(Path(source), Path(target), dry_run=dry_run)


async def _persona_list() -> int:
    try:
        from deile.personas.manager import PersonaManager

        mgr = PersonaManager()
        await mgr.initialize()
        for name in mgr.list_personas() if hasattr(mgr, "list_personas") else []:
            print(name)
    except Exception as e:
        print(f"persona list failed: {e}", file=sys.stderr)
        return 2
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return asyncio.run(_run_provider(args.provider, args.guild_ids))
    if args.command == "dlq":
        if args.dlq_action == "list":
            return asyncio.run(_dlq_list(args.provider))
        if args.dlq_action == "purge":
            return asyncio.run(_dlq_purge(args.older_than_days))
    if args.command == "sessions":
        if args.sessions_action == "purge":
            return asyncio.run(_sessions_purge(args.older_than_days))
        if args.sessions_action == "list":
            return asyncio.run(_sessions_list())
    if args.command == "metrics":
        return asyncio.run(_metrics_snapshot())
    if args.command == "migrate-memory-json":
        return asyncio.run(
            _migrate_memory_json(args.source, args.target_db, args.dry_run)
        )
    if args.command == "persona":
        if args.persona_action == "list":
            return asyncio.run(_persona_list())
    return 1


if __name__ == "__main__":
    sys.exit(main())
