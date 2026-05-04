"""SingleProviderRuntime — runs one adapter wired to the foundation pipeline.

The runtime also owns the optional control-plane HTTP server (the
flecha reversa: deile-cli → deile-bot). When a `ControlPlaneServer` is
attached, it is started right after the adapter and stopped on exit.
"""

from __future__ import annotations

from typing import Optional

from deile_bot.foundation.envelope import MessageEnvelope
from deile_bot.foundation.logging import get_logger
from deile_bot.foundation.pipeline import IngressPipeline


class SingleProviderRuntime:
    def __init__(
        self,
        adapter,
        pipeline: IngressPipeline,
        *,
        capability_catalog=None,
        agent_meta=None,
        formatters=None,
        control_plane: Optional["ControlPlaneServer"] = None,  # noqa: F821 - forward ref
    ):
        self.adapter = adapter
        self.pipeline = pipeline
        self.capability_catalog = capability_catalog or pipeline.capability_catalog
        self.agent_meta = agent_meta or pipeline.agent_meta
        self.formatters = formatters or {}
        self.control_plane = control_plane
        self._logger = get_logger("runtime.single")
        if hasattr(adapter, "runtime"):
            adapter.runtime = self

    async def start(self) -> None:
        async def on_inbound(env: MessageEnvelope, src) -> None:
            try:
                await self.pipeline.handle(env, src)
            except Exception:
                self._logger.exception("ingress pipeline raised")

        self.adapter.on_inbound = on_inbound
        if self.control_plane is not None:
            self.control_plane.register_adapter(self.adapter.name, self.adapter)
            try:
                bound = await self.control_plane.start()
                self._logger.info(
                    "control_plane started", extra={"port": bound, "adapter": self.adapter.name}
                )
            except Exception:
                self._logger.exception("control_plane start failed")
        await self.adapter.start()

    async def stop(self) -> None:
        # Both adapter.stop() and control_plane.stop() must be attempted even
        # if one raises — otherwise a hung adapter would leak the control-plane
        # port (orphan socket on 127.0.0.1) until the process dies.
        adapter_exc: Optional[BaseException] = None
        try:
            await self.adapter.stop()
        except Exception as exc:
            adapter_exc = exc
            self._logger.exception("adapter.stop raised; continuing to stop control_plane")
        if self.control_plane is not None:
            try:
                await self.control_plane.stop()
            except Exception:
                self._logger.exception("control_plane stop failed")
        if adapter_exc is not None:
            raise adapter_exc


class MultiProviderRuntime:
    """Runs N adapters sharing the same foundation pipeline."""

    def __init__(self, adapters: list, pipeline: IngressPipeline):
        self.adapters = adapters
        self.pipeline = pipeline
        self._logger = get_logger("runtime.multi")

    async def start(self) -> None:
        for adapter in self.adapters:
            async def make_cb(a):
                async def on_inbound(env, src):
                    try:
                        await self.pipeline.handle(env, src)
                    except Exception:
                        self._logger.exception("ingress pipeline raised")
                return on_inbound

            adapter.on_inbound = await make_cb(adapter)
            await adapter.start()

    async def stop(self) -> None:
        for adapter in self.adapters:
            try:
                await adapter.stop()
            except Exception:
                self._logger.exception("adapter.stop raised")
