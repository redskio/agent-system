from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Awaitable

from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from .config import AppConfig

log = logging.getLogger(__name__)

MessageHandler = Callable[[dict[str, Any]], Awaitable[None]]


class SlackListener:
    """
    Listens for Slack Socket Mode events via slack_bolt and dispatches
    messages to a handler.

    Only processes messages from AUTHORIZED_USER_ID.

    Phase 3 extension point: add a reaction_event handler by registering
    app.event("reaction_added") in start().
    """

    def __init__(self, config: AppConfig, on_message: MessageHandler) -> None:
        self.config = config
        self.on_message = on_message

        self.app = AsyncApp(token=config.slack_bot_token)

        # Register event handlers
        self.app.event("message")(self._on_message_event)
        self.app.event("app_mention")(self._on_message_event)

        # Phase 3 hook: reaction_added handler placeholder
        # self.app.event("reaction_added")(self._on_reaction_event)

        self.handler = AsyncSocketModeHandler(
            app=self.app,
            app_token=config.slack_app_token,
        )

    async def start(self) -> None:
        await self.handler.start_async()
        log.info("Slack Socket Mode connected (slack_bolt)")
        await asyncio.Event().wait()  # keep alive until cancelled

    async def _on_message_event(self, event: dict[str, Any], say: Any) -> None:
        # Ignore bot messages and message edits (but allow file_share)
        subtype = event.get("subtype")
        if event.get("bot_id") or (subtype and subtype != "file_share"):
            return

        user = event.get("user", "")
        if self.config.authorized_user_id and user != self.config.authorized_user_id:
            log.debug("Ignoring message from unauthorized user: %s", user)
            return

        await self.on_message(event)
