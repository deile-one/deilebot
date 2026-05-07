"""
dm_tool — Standalone Discord DM tool for DEILE.

Reusable bridge between the DEILE agent (CLI context) and the Discord API.
Uses DEILE_BOT_DISCORD_TOKEN from environment/.env to authenticate.

Usage (CLI):
    python -m deile_bot.bridge.dm_tool <user_id> "message text"

Usage (import):
    from deile_bot.bridge.dm_tool import send_discord_dm
    msg_id = await send_discord_dm("user_id", "Hello!")
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Optional


async def send_discord_dm(
    user_id: str,
    text: str,
    token: Optional[str] = None,
) -> dict:
    """
    Send a direct message to a Discord user.

    Args:
        user_id: Discord user ID (snowflake).
        text: Message content.
        token: Bot token. Falls back to DEILE_BOT_DISCORD_TOKEN env var.

    Returns:
        dict with keys: ok (bool), message_id (str|None), error (str|None)
    """
    import discord

    resolved_token = token or os.environ.get("DEILE_BOT_DISCORD_TOKEN") or ""
    if not resolved_token:
        return {"ok": False, "message_id": None, "error": "No Discord token found"}

    intents = discord.Intents.default()
    intents.message_content = True

    client = discord.Client(intents=intents)

    result: dict = {"ok": False, "message_id": None, "error": None}

    @client.event
    async def on_ready() -> None:
        try:
            user = await client.fetch_user(int(user_id))
            msg = await user.send(content=text)
            result["ok"] = True
            result["message_id"] = str(msg.id)
        except discord.Forbidden:
            result["error"] = "Forbidden — bot cannot DM this user"
        except discord.HTTPException as e:
            result["error"] = f"HTTP error: {e}"
        except ValueError:
            result["error"] = f"Invalid user_id: {user_id}"
        except Exception as e:
            result["error"] = f"Unexpected error: {e}"
        finally:
            await client.close()

    try:
        await client.start(resolved_token)
    except discord.LoginFailure:
        return {"ok": False, "message_id": None, "error": "LoginFailure — invalid token"}
    except Exception as e:
        return {"ok": False, "message_id": None, "error": f"Client error: {e}"}

    return result


async def amain() -> None:
    if len(sys.argv) < 3:
        print("Usage: python -m deile_bot.bridge.dm_tool <user_id> <message>", file=sys.stderr)
        sys.exit(1)

    user_id = sys.argv[1]
    text = sys.argv[2]
    result = await send_discord_dm(user_id, text)

    if result["ok"]:
        print(f"DM sent! message_id={result['message_id']}")
        sys.exit(0)
    else:
        print(f"Failed: {result['error']}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
