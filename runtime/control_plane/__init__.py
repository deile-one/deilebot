"""Control-plane HTTP API — the deile (CLI) → deile-bot (daemon) flecha reversa.

The daemon binds an aiohttp server on 127.0.0.1, gated by a Bearer
token, exposing a small set of outbound operations (send to channel,
send DM, react, start thread, pin, mention role, fetch user profile).
The DEILE-side `deile-bot-client` consumes this API via httpx.

Layout:
- `settings.py` — port + token configuration (env, YAML).
- `auth.py` — constant-time token comparison.
- `errors.py` — canonical {error: {code, message, details}} envelope.
- `server.py` — ControlPlaneServer lifecycle (start/stop in same loop as adapter).
- `routes.py` — endpoint handlers, mapped onto adapter methods.
"""

from .server import ControlPlaneServer
from .settings import ControlPlaneSettings

__all__ = ["ControlPlaneServer", "ControlPlaneSettings"]
