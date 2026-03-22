"""
ChannelRouter — Central dispatch for incoming messages.
Routes messages to the orchestrator and responses back to the originating channel.
"""

import logging
from typing import Optional

from channels.base import ChannelBase, MessageIn, MessageOut

log = logging.getLogger(__name__)


class ChannelRouter:
    """Singleton-style router: dispatches messages to orchestrator, routes responses to channels."""

    def __init__(self):
        self._channels: dict = {}  # channel_id -> ChannelBase

    def register(self, channel: ChannelBase) -> None:
        """Register a channel."""
        self._channels[channel.channel_id] = channel
        log.info("Channel registered: %s", channel.channel_id)

    def unregister(self, channel_id: str) -> None:
        """Unregister a channel."""
        if self._channels.pop(channel_id, None):
            log.info("Channel unregistered: %s", channel_id)

    def get_channel(self, channel_id: str) -> Optional[ChannelBase]:
        """Get a registered channel by ID."""
        return self._channels.get(channel_id)

    def list_channels(self) -> list:
        """Return list of registered channel IDs."""
        return list(self._channels.keys())

    def handle_message(self, msg: MessageIn, gen):
        """
        Route orchestrator events to the originating channel.

        Args:
            msg: The incoming message
            gen: Generator from chat_with_tools_stream yielding event dicts
        Returns:
            Generator of (MessageOut, event_dict) tuples for the caller to handle
        """
        channel = self.get_channel(msg.channel)
        if not channel:
            log.warning("No channel registered for: %s", msg.channel)
            return

        for event_dict in gen:
            event_type = event_dict.get("type", "unknown")
            out = MessageOut(
                event_type=event_type,
                content=event_dict.get("content", event_dict.get("text", "")),
                thinking=event_dict.get("message") if event_type == "thinking" else None,
                tool_calls=event_dict.get("calls", []),
                tool_results=event_dict.get("results", []),
                raw=event_dict,
            )
            yield out, event_dict

    def start_all(self) -> None:
        """Start all registered channels."""
        for ch in self._channels.values():
            try:
                ch.start()
                log.info("Channel started: %s", ch.channel_id)
            except Exception as e:
                log.error("Failed to start channel %s: %s", ch.channel_id, e)

    def stop_all(self) -> None:
        """Stop all registered channels."""
        for ch in self._channels.values():
            try:
                ch.stop()
                log.info("Channel stopped: %s", ch.channel_id)
            except Exception as e:
                log.error("Failed to stop channel %s: %s", ch.channel_id, e)


# Module-level singleton
_router: Optional[ChannelRouter] = None


def get_router() -> ChannelRouter:
    """Get or create the global ChannelRouter instance."""
    global _router
    if _router is None:
        _router = ChannelRouter()
    return _router
