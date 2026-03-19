"""
ClawbotCore Channel Abstraction Layer.
Provides a unified interface for all communication channels (web, API, Telegram, etc.).
"""

from channels.base import (
    ChannelBase,
    ChannelCapabilities,
    MessageIn,
    MessageOut,
)
from channels.router import ChannelRouter
from channels.whatsapp import WhatsAppChannel

__all__ = [
    "ChannelBase",
    "ChannelCapabilities",
    "MessageIn",
    "MessageOut",
    "ChannelRouter",
    "WhatsAppChannel",
]
