"""IngressPipeline._materialize_attachments — eager image download + base64.

Why this exists: Discord CDN URLs since 2024 carry expiring signatures
(`?ex=…&is=…&hm=…`). If we pass the URL straight to DEILE, by the time
DEILE goes to download the image it may 403. The bot already has access
to Discord (it's the connected client), so we do the IO once.

Tests:
- happy path: small image → base64 inline + URL preserved
- oversize: > 4 MiB → URL only + download_error
- 4xx: tagged with download_error, URL preserved, no data_base64
- non-image kind: passes through unchanged (no fetch attempt)
- multiple attachments: gathered in parallel
"""

from __future__ import annotations

import base64
from typing import AsyncIterator, Tuple

import pytest
from aiohttp import web

from deile_bot.foundation.envelope import Attachment, AttachmentKind
from deile_bot.foundation.pipeline import IngressPipeline

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="
)


@pytest.fixture
async def img_server() -> AsyncIterator[Tuple[str, dict]]:
    state = {"hits": 0}
    routes = web.RouteTableDef()

    @routes.get("/img.png")
    async def _img(_):
        state["hits"] += 1
        return web.Response(body=PNG_BYTES, content_type="image/png")

    @routes.get("/expired.png")
    async def _403(_):
        return web.Response(status=403)

    @routes.get("/huge.png")
    async def _big(_):
        return web.Response(body=b"\x00" * (5 * 1024 * 1024), content_type="image/png")

    app = web.Application()
    app.add_routes(routes)
    runner = web.AppRunner(app, handle_signals=False, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", state
    await runner.cleanup()


def _bare_pipeline() -> IngressPipeline:
    """Build a pipeline instance without wiring real deps — we only call
    `_materialize_attachments` which is self-contained."""
    return IngressPipeline.__new__(IngressPipeline)


async def test_image_inlined_with_base64(img_server):
    base, state = img_server
    pipe = _bare_pipeline()
    atts = (
        Attachment(
            kind=AttachmentKind.IMAGE,
            url=f"{base}/img.png",
            mime="image/png",
            filename="ok.png",
            size_bytes=len(PNG_BYTES),
        ),
    )
    out = await pipe._materialize_attachments(atts)
    assert len(out) == 1
    entry = out[0]
    assert entry["kind"] == "IMAGE"
    assert entry["url"] == f"{base}/img.png"
    assert entry["filename"] == "ok.png"
    assert "data_base64" in entry
    decoded = base64.b64decode(entry["data_base64"])
    assert decoded == PNG_BYTES
    assert "download_error" not in entry
    assert state["hits"] == 1


async def test_oversize_falls_back_to_url(img_server):
    base, _ = img_server
    pipe = _bare_pipeline()
    atts = (
        Attachment(
            kind=AttachmentKind.IMAGE,
            url=f"{base}/huge.png",
            mime="image/png",
            filename="big.png",
            size_bytes=5 * 1024 * 1024,  # advertised size triggers early skip
        ),
    )
    out = await pipe._materialize_attachments(atts)
    entry = out[0]
    assert "data_base64" not in entry
    assert entry["url"] == f"{base}/huge.png"
    assert "download_error" in entry
    assert "too large" in entry["download_error"]


async def test_403_url_recorded_as_download_error(img_server):
    base, _ = img_server
    pipe = _bare_pipeline()
    atts = (
        Attachment(
            kind=AttachmentKind.IMAGE,
            url=f"{base}/expired.png",
            mime="image/png",
            filename="expired.png",
            size_bytes=10,
        ),
    )
    out = await pipe._materialize_attachments(atts)
    entry = out[0]
    assert "data_base64" not in entry
    assert "download_error" in entry
    assert "403" in entry["download_error"]


async def test_non_image_passes_through_without_fetch(img_server):
    base, state = img_server
    pipe = _bare_pipeline()
    atts = (
        Attachment(
            kind=AttachmentKind.FILE,
            url=f"{base}/img.png",
            mime="application/zip",
            filename="archive.zip",
            size_bytes=42,
        ),
    )
    out = await pipe._materialize_attachments(atts)
    entry = out[0]
    assert entry["kind"] == "FILE"
    assert "data_base64" not in entry
    assert "download_error" not in entry
    assert state["hits"] == 0


async def test_multiple_attachments_parallel(img_server):
    base, state = img_server
    pipe = _bare_pipeline()
    atts = tuple(
        Attachment(
            kind=AttachmentKind.IMAGE,
            url=f"{base}/img.png",
            mime="image/png",
            filename=f"{i}.png",
            size_bytes=len(PNG_BYTES),
        )
        for i in range(3)
    )
    out = await pipe._materialize_attachments(atts)
    assert len(out) == 3
    for entry in out:
        assert "data_base64" in entry
    assert state["hits"] == 3


async def test_image_without_url_skipped_gracefully():
    pipe = _bare_pipeline()
    atts = (
        Attachment(
            kind=AttachmentKind.IMAGE,
            url=None,  # inline-only, no URL — bot can't fetch
            mime="image/png",
            filename="local.png",
            size_bytes=100,
        ),
    )
    out = await pipe._materialize_attachments(atts)
    entry = out[0]
    assert "data_base64" not in entry
    assert "download_error" not in entry  # nothing to fail at
