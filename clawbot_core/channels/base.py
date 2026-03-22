"""
Channel Abstraction Layer — Base classes and data types.
Defines the interface all channels must implement.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
import time


@dataclass
class MessageIn:
    """Incoming message from any channel."""
    channel: str            # "web", "telegram", "api"
    session_id: str         # unique conversation ID
    user_id: str            # unique user identifier
    content: str            # text message
    attachments: list = field(default_factory=list)   # [{type, url|path, mime}]
    metadata: dict = field(default_factory=dict)       # channel-specific extras
    timestamp: float = field(default_factory=time.time)
    reply_to: Optional[str] = None                     # reply context


@dataclass
class MessageOut:
    """Outgoing message/event to a channel."""
    content: str = ""
    attachments: list = field(default_factory=list)
    thinking: Optional[str] = None
    tool_calls: list = field(default_factory=list)
    tool_results: list = field(default_factory=list)
    event_type: str = "done"  # "thinking", "content_delta", "tool_call", "tool_result", "done", "error"
    raw: dict = field(default_factory=dict)  # full event dict from orchestrator


@dataclass
class ChannelCapabilities:
    """Describes what a channel supports."""
    streaming: bool = False
    images: bool = False
    audio: bool = False
    files: bool = False
    groups: bool = False
    max_message_length: int = 4096


class ChannelBase(ABC):
    """Abstract base for all channels."""
    channel_id: str = ""

    @abstractmethod
    def start(self) -> None:
        """Start the channel (connection, polling, etc.)."""
        ...

    @abstractmethod
    def stop(self) -> None:
        """Stop the channel cleanly."""
        ...

    @abstractmethod
    def send(self, session_id: str, message: MessageOut) -> None:
        """Send a message/event to the client."""
        ...

    @abstractmethod
    def get_capabilities(self) -> ChannelCapabilities:
        """Return channel capabilities."""
        ...

    def chunk_text(self, text: str) -> list:
        """Split text according to max_message_length."""
        cap = self.get_capabilities()
        limit = cap.max_message_length
        if len(text) <= limit:
            return [text]
        chunks = []
        while text:
            chunks.append(text[:limit])
            text = text[limit:]
        return chunks
