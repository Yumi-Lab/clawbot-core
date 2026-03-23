"""
WebChannel — SSE streaming channel for the ClawbotCore dashboard.
Extracts the SSE handling logic from main.py into a reusable channel.
"""

import json
import logging
import threading
import time

from channels.base import ChannelBase, ChannelCapabilities, MessageOut

log = logging.getLogger(__name__)


class WebChannel(ChannelBase):
    """Channel for the web dashboard — SSE streaming."""
    channel_id = "web"

    def start(self):
        pass  # Web is event-driven via HTTP, no persistent connection

    def stop(self):
        pass

    def send(self, session_id, message):
        # WebChannel does not push — SSE is handled in handle_stream_request()
        pass

    def get_capabilities(self):
        return ChannelCapabilities(
            streaming=True,
            images=True,
            audio=False,
            files=True,
            groups=False,
            max_message_length=100000,
        )

    def handle_stream_request(self, handler, data, session_id, save_fn=None):
        """
        Handle a streaming SSE request from main.py do_POST.

        Args:
            handler: The HTTP request handler (BaseHTTPRequestHandler)
            data: The parsed JSON request body
            session_id: The session ID from the request
            save_fn: Optional callback(session_id, content) to save assistant response
        """
        from orchestrator import chat_with_tools_stream

        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.send_header("X-Accel-Buffering", "no")
        handler.end_headers()

        gen = chat_with_tools_stream(data, session_id=session_id)
        final_content = None

        try:
            for event_dict in gen:
                event_type = event_dict.get("type", "data")
                if event_type == "done":
                    final_content = event_dict.get("content", "")

                # Native ClawBot event (named SSE event for dashboard)
                chunk = f"event: {event_type}\ndata: {json.dumps(event_dict)}\n\n".encode()
                handler.wfile.write(chunk)
                handler.wfile.flush()

                # On "done": also emit a standard OpenAI delta chunk
                # so Open WebUI / LibreChat / AnythingLLM receive the content
                if event_type == "done":
                    openai_delta = json.dumps({
                        "id": "chatcmpl-clawbot",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": data.get("model", "clawbot"),
                        "choices": [{
                            "index": 0,
                            "delta": {"content": final_content or ""},
                            "finish_reason": "stop",
                        }],
                    })
                    handler.wfile.write(f"data: {openai_delta}\n\n".encode())
                    handler.wfile.flush()

        except BrokenPipeError:
            log.info("Client disconnected, continuing in background (session=%s)", session_id)
            from orchestrator import mark_draining
            mark_draining(session_id)  # prevent new injects during background drain
            self._drain_in_background(gen, session_id, final_content, save_fn)
            return

        except Exception as e:
            log.error("chat stream error: %s", e)
            try:
                err = json.dumps({"type": "error", "message": str(e)})
                handler.wfile.write(f"event: error\ndata: {err}\n\n".encode())
                handler.wfile.flush()
            except Exception:
                pass

        try:
            handler.wfile.write(b"data: [DONE]\n\n")
            handler.wfile.flush()
        except Exception:
            pass

    def _drain_in_background(self, gen, session_id, final_content, save_fn):
        """Continue draining the generator in a background thread after client disconnect."""
        def _drain():
            from orchestrator import is_cancelled, clear_cancelled
            if is_cancelled(session_id):
                clear_cancelled(session_id)
                log.info("Session %s cancelled — skipping background drain", session_id)
                if save_fn:
                    save_fn(session_id, "⛔ Tâche arrêtée par l'utilisateur.")
                return
            content = final_content or ""
            try:
                for ev in gen:
                    if is_cancelled(session_id):
                        clear_cancelled(session_id)
                        log.info("Session %s cancelled mid-drain", session_id)
                        if save_fn:
                            save_fn(session_id, "⛔ Tâche arrêtée par l'utilisateur.")
                        return
                    if ev.get("type") == "done":
                        content = ev.get("content", "")
            except Exception as ex:
                log.warning("Background drain error: %s", ex)
            if save_fn:
                save_fn(session_id, content)

        threading.Thread(target=_drain, daemon=True).start()

    def handle_sync_request(self, handler, data):
        """Handle a non-streaming JSON request."""
        from orchestrator import chat_with_tools
        result = chat_with_tools(data)
        handler.send_json(200, result)
