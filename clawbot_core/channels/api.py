"""
APIChannel — REST non-streaming channel.
Returns a single JSON response (OpenAI-compatible format).
Useful for external integrations that don't support SSE.
"""

import json
import logging
import time

from channels.base import ChannelBase, ChannelCapabilities

log = logging.getLogger(__name__)


class APIChannel(ChannelBase):
    """Channel for REST API — non-streaming JSON responses."""
    channel_id = "api"

    def start(self):
        pass

    def stop(self):
        pass

    def send(self, session_id, message):
        # API channel does not push — response is synchronous in handle_sync_request()
        pass

    def get_capabilities(self):
        return ChannelCapabilities(
            streaming=False,
            images=False,
            audio=False,
            files=False,
            groups=False,
            max_message_length=100000,
        )

    def handle_sync_request(self, handler, data):
        """Handle a non-streaming JSON request, returning OpenAI-compatible response."""
        from orchestrator import chat_with_tools
        result = chat_with_tools(data)
        handler.send_json(200, result)

    def handle_stream_request(self, handler, data, session_id, save_fn=None):
        """API channel doesn't stream — collect all events and return final JSON."""
        from orchestrator import chat_with_tools_stream

        gen = chat_with_tools_stream(data, session_id=session_id)
        final_content = ""

        try:
            for event_dict in gen:
                if event_dict.get("type") == "done":
                    final_content = event_dict.get("content", "")
        except Exception as e:
            log.error("API channel stream error: %s", e)
            handler.send_json(500, {"error": str(e)})
            return

        response = {
            "id": "chatcmpl-clawbot",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": data.get("model", "clawbot"),
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": final_content},
                "finish_reason": "stop",
            }],
        }
        handler.send_json(200, response)
