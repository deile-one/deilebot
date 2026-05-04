"""Control-plane settings — port, bind host, auth token.

Loaded from env (DEILE_BOT_CONTROL_PLANE_*) with safe defaults. The
host defaults to 127.0.0.1 — exposing the control-plane to a public
interface is intentional and requires explicit override.
"""

from __future__ import annotations

import secrets

try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
    _HAVE = True
except ImportError:  # pragma: no cover
    from pydantic import BaseSettings  # type: ignore[attr-defined]
    SettingsConfigDict = None  # type: ignore[assignment]
    _HAVE = False


class ControlPlaneSettings(BaseSettings):
    """Configuration for the deile-bot control-plane HTTP server.

    enabled — kill switch; daemon never opens the port when False.
    host — default 127.0.0.1; override only with explicit intent.
    port — default 8765; 0 lets the OS pick a free port (useful in tests).
    auth_token — Bearer token clients must present. Empty disables the
        server (refuses to start) to fail loudly instead of running
        unprotected.
    rate_limit_per_minute — soft throttle per client per endpoint group.
    request_max_bytes — body cap for inbound JSON.
    """

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8765
    auth_token: str = ""
    rate_limit_per_minute: int = 120
    request_max_bytes: int = 32 * 1024  # 32 KiB
    log_requests: bool = True

    if _HAVE:
        model_config = SettingsConfigDict(
            env_prefix="DEILE_BOT_CONTROL_PLANE_",
            env_file=".env",
            extra="ignore",
        )
    else:  # pragma: no cover
        class Config:
            env_prefix = "DEILE_BOT_CONTROL_PLANE_"
            env_file = ".env"

    @classmethod
    def for_test(cls, *, port: int = 0) -> "ControlPlaneSettings":
        """Helper for tests — random token, OS-picked port."""
        return cls(
            enabled=True,
            host="127.0.0.1",
            port=port,
            auth_token=secrets.token_urlsafe(24),
        )

    def __repr__(self) -> str:  # pragma: no cover
        token = "<set>" if self.auth_token else "<unset>"
        return (
            f"ControlPlaneSettings(enabled={self.enabled}, host={self.host!r}, "
            f"port={self.port}, auth_token={token})"
        )
