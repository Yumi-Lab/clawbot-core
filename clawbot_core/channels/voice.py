"""
VoiceChannel — Audio channel for voice pipeline.
STT/TTS is handled cloud-side; this channel just tags requests as voice.
"""

import logging

from channels.base import ChannelBase, ChannelCapabilities, MessageOut

log = logging.getLogger(__name__)


class VoiceChannel(ChannelBase):
    """Channel for voice interactions — cloud handles STT/TTS."""
    channel_id = "voice"

    def start(self):
        pass  # Cloud manages the voice pipeline

    def stop(self):
        pass

    def send(self, session_id, message):
        pass  # Cloud handles TTS output

    def get_capabilities(self):
        return ChannelCapabilities(
            streaming=False,
            images=False,
            audio=True,
            files=False,
            groups=False,
            max_message_length=4096,
        )
