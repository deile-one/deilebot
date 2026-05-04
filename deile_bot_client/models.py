"""Pydantic v2 models — single source of truth for the control-plane wire format.

Both client and server import these. Bumping the dist version is the
contract version: callers who pin a major version are guaranteed
backward compatibility within that major.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    """Forbid extra fields so contract drift is caught at parse time."""

    model_config = ConfigDict(extra="forbid")


# -- Health / metadata --------------------------------------------------------


class HealthResponse(_Strict):
    ok: bool
    version: str
    providers: List[str] = Field(default_factory=list)
    is_ready: bool = True


# -- channel.post -------------------------------------------------------------


class ChannelPostRequest(_Strict):
    channel_id: str
    text: str
    reply_to: Optional[str] = None


class ChannelPostResponse(_Strict):
    message_id: str
    channel_id: str
    sent_at: datetime


# -- dm.send ------------------------------------------------------------------


class DMSendRequest(_Strict):
    """user_id is the *provider* user id (e.g. discord snowflake).

    bot_user_id (the deile-bot stable ULID) may be passed instead. The
    server resolves either; provide exactly one.
    """

    user_id: Optional[str] = None
    bot_user_id: Optional[str] = None
    text: str

    def model_post_init(self, __context: Any) -> None:  # noqa: D401 - hook
        if (self.user_id is None) == (self.bot_user_id is None):
            raise ValueError("exactly one of user_id / bot_user_id must be set")


class DMSendResponse(_Strict):
    message_id: str
    user_id: str
    sent_at: datetime


# -- reaction.add -------------------------------------------------------------


class ReactionAddRequest(_Strict):
    channel_id: str
    message_id: str
    emoji: str


class ReactionAddResponse(_Strict):
    ok: bool = True


# -- thread.start -------------------------------------------------------------


class ThreadStartRequest(_Strict):
    """If `parent_message_id` is set, creates a thread anchored on that
    message; otherwise creates a thread directly in the channel.
    """

    channel_id: str
    name: str
    parent_message_id: Optional[str] = None


class ThreadStartResponse(_Strict):
    thread_id: str
    name: str


# -- message.pin --------------------------------------------------------------


class MessagePinRequest(_Strict):
    channel_id: str
    message_id: str


class MessagePinResponse(_Strict):
    ok: bool = True


# -- role.mention -------------------------------------------------------------


class RoleMentionRequest(_Strict):
    channel_id: str
    role_id: str
    text: str = ""


class RoleMentionResponse(_Strict):
    message_id: str


# -- users --------------------------------------------------------------------


class UserProfileResponse(_Strict):
    user_id: str
    username: str
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None
    is_bot: bool = False


# -- error envelope -----------------------------------------------------------


class ErrorBody(_Strict):
    code: str
    message: str
    details: Dict[str, Any] = Field(default_factory=dict)


class ErrorEnvelope(_Strict):
    error: ErrorBody
