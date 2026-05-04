"""ControlPlaneServer — aiohttp lifecycle wrapper.

The server lives in the same asyncio loop as the provider adapter so
they share a process and a signal handler. `start()` is non-blocking
and returns the bound port (useful when port=0 for tests). `stop()`
waits for in-flight handlers to finish.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from time import monotonic
from typing import Any, Dict, Optional

from aiohttp import web

from .auth import make_auth_middleware
from .errors import json_error
from .routes import register_routes
from .settings import ControlPlaneSettings

logger = logging.getLogger("deile_bot.control_plane.server")


def _make_rate_limit_middleware(per_minute: int) -> web.middleware:
    """Token-bucket-ish per-IP throttle. Runs after auth_mw."""

    if per_minute <= 0:
        @web.middleware
        async def _passthrough(request, handler):
            return await handler(request)
        return _passthrough

    bucket: Dict[str, list[float]] = defaultdict(list)
    window_s = 60.0

    @web.middleware
    async def rl_mw(request: web.Request, handler):
        now = monotonic()
        ip = request.remote or "unknown"
        timestamps = bucket[ip]
        # drop entries older than the window
        cutoff = now - window_s
        while timestamps and timestamps[0] < cutoff:
            timestamps.pop(0)
        if len(timestamps) >= per_minute:
            retry_after = max(1, int(window_s - (now - timestamps[0])))
            return json_error(
                "RATE_LIMITED",
                f"too many requests; retry after {retry_after}s",
                status=429,
                headers={"Retry-After": str(retry_after)},
            )
        timestamps.append(now)
        return await handler(request)

    return rl_mw


class ControlPlaneServer:
    """HTTP server that exposes outbound provider operations.

    Wire-up:
        srv = ControlPlaneServer(settings, version="0.1.0")
        srv.register_adapter("discord", discord_adapter)
        srv.audit_logger = bot_audit
        srv.identity = identity_resolver
        port = await srv.start()    # returns bound port
        ...
        await srv.stop()
    """

    def __init__(self, settings: ControlPlaneSettings, *, version: str = "0.1.0") -> None:
        self.settings = settings
        self.version = version
        self.adapters: Dict[str, Any] = {}
        self.audit_logger: Optional[Any] = None
        self.identity: Optional[Any] = None
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.BaseSite] = None
        self._bound_port: Optional[int] = None

    def register_adapter(self, name: str, adapter: Any) -> None:
        self.adapters[name] = adapter

    def _build_app(self) -> web.Application:
        middlewares = [
            make_auth_middleware(self.settings.auth_token),
            _make_rate_limit_middleware(self.settings.rate_limit_per_minute),
        ]
        app = web.Application(middlewares=middlewares, client_max_size=self.settings.request_max_bytes + 4096)
        app["server"] = self
        register_routes(app)
        return app

    async def start(self) -> int:
        """Bind the server. Returns the actual port (useful with port=0)."""
        if not self.settings.enabled:
            logger.info("control-plane disabled by settings")
            return 0
        if self._site is not None:
            return self._bound_port or 0

        self._app = self._build_app()
        self._runner = web.AppRunner(self._app, handle_signals=False, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host=self.settings.host, port=self.settings.port)
        await self._site.start()
        # Read back the actual port (matters when settings.port == 0).
        bound_port = self.settings.port
        # aiohttp >= 3.9 exposes sockets via runner.server (Server object)
        # but the most reliable way is to walk site._server.sockets.
        candidate_sockets = []
        for owner in (self._site, self._runner, getattr(self._runner, "server", None)):
            if owner is None:
                continue
            for attr in ("_server", "sockets"):
                target = getattr(owner, attr, None)
                if target is None:
                    continue
                socks = getattr(target, "sockets", target)
                if socks and not callable(socks):
                    candidate_sockets.extend(socks)
        for sock in candidate_sockets:
            try:
                bound_port = sock.getsockname()[1]
                if bound_port:
                    break
            except Exception:
                continue
        self._bound_port = bound_port
        logger.info(
            "control-plane listening",
            extra={"host": self.settings.host, "port": bound_port},
        )
        return bound_port

    async def stop(self) -> None:
        if self._site is not None:
            try:
                await self._site.stop()
            except Exception:
                logger.exception("control-plane site stop failed")
            self._site = None
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:
                logger.exception("control-plane runner cleanup failed")
            self._runner = None
        self._app = None
        self._bound_port = None

    @property
    def bound_port(self) -> Optional[int]:
        return self._bound_port
