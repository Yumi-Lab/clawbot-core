"""
Unit tests for the Channel Abstraction Layer.
stdlib-only — uses unittest, no pytest dependency.
"""

import io
import json
import sys
import os
import unittest
import time

# Add clawbot_core to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "clawbot_core"))

from channels.base import ChannelBase, ChannelCapabilities, MessageIn, MessageOut
from channels.router import ChannelRouter, get_router
from channels.web import WebChannel
from channels.api import APIChannel
from channels.telegram import TelegramChannel
from channels.voice import VoiceChannel


# ── base.py tests ─────────────────────────────────────────────────


class TestMessageIn(unittest.TestCase):
    def test_defaults(self):
        msg = MessageIn(channel="web", session_id="s1", user_id="u1", content="hello")
        self.assertEqual(msg.channel, "web")
        self.assertEqual(msg.session_id, "s1")
        self.assertEqual(msg.content, "hello")
        self.assertEqual(msg.attachments, [])
        self.assertEqual(msg.metadata, {})
        self.assertIsNone(msg.reply_to)
        self.assertIsInstance(msg.timestamp, float)

    def test_with_metadata(self):
        msg = MessageIn(
            channel="telegram", session_id="s2", user_id="u2",
            content="hi", metadata={"model": "haiku"},
        )
        self.assertEqual(msg.metadata["model"], "haiku")


class TestMessageOut(unittest.TestCase):
    def test_defaults(self):
        out = MessageOut()
        self.assertEqual(out.content, "")
        self.assertEqual(out.event_type, "done")
        self.assertIsNone(out.thinking)
        self.assertEqual(out.raw, {})

    def test_with_values(self):
        out = MessageOut(content="reply", event_type="content_delta", thinking="hmm")
        self.assertEqual(out.content, "reply")
        self.assertEqual(out.event_type, "content_delta")
        self.assertEqual(out.thinking, "hmm")


class TestChannelCapabilities(unittest.TestCase):
    def test_defaults(self):
        cap = ChannelCapabilities()
        self.assertFalse(cap.streaming)
        self.assertFalse(cap.images)
        self.assertEqual(cap.max_message_length, 4096)


class TestChannelBaseChunkText(unittest.TestCase):
    def setUp(self):
        # Create a concrete implementation for testing chunk_text
        class TestChannel(ChannelBase):
            channel_id = "test"
            def start(self): pass
            def stop(self): pass
            def send(self, session_id, message): pass
            def get_capabilities(self):
                return ChannelCapabilities(max_message_length=10)
        self.ch = TestChannel()

    def test_short_text(self):
        self.assertEqual(self.ch.chunk_text("hello"), ["hello"])

    def test_exact_limit(self):
        self.assertEqual(self.ch.chunk_text("1234567890"), ["1234567890"])

    def test_split(self):
        text = "12345678901234567890"  # 20 chars, limit 10
        chunks = self.ch.chunk_text(text)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0], "1234567890")
        self.assertEqual(chunks[1], "1234567890")
        self.assertEqual("".join(chunks), text)

    def test_empty(self):
        self.assertEqual(self.ch.chunk_text(""), [""])


# ── router.py tests ──────────────────────────────────────────────


class TestChannelRouter(unittest.TestCase):
    def setUp(self):
        self.router = ChannelRouter()

    def test_register_unregister(self):
        wc = WebChannel()
        self.router.register(wc)
        self.assertIs(self.router.get_channel("web"), wc)
        self.assertIn("web", self.router.list_channels())

        self.router.unregister("web")
        self.assertIsNone(self.router.get_channel("web"))
        self.assertNotIn("web", self.router.list_channels())

    def test_unregister_nonexistent(self):
        # Should not raise
        self.router.unregister("nonexistent")

    def test_get_channel_nonexistent(self):
        self.assertIsNone(self.router.get_channel("foo"))

    def test_multiple_channels(self):
        self.router.register(WebChannel())
        self.router.register(APIChannel())
        channels = self.router.list_channels()
        self.assertIn("web", channels)
        self.assertIn("api", channels)

    def test_handle_message_no_channel(self):
        # Should yield nothing if channel not found
        msg = MessageIn(channel="nonexistent", session_id="s1", user_id="u1", content="test")
        events = list(self.router.handle_message(msg, iter([])))
        self.assertEqual(events, [])

    def test_handle_message_with_events(self):
        self.router.register(WebChannel())
        msg = MessageIn(channel="web", session_id="s1", user_id="u1", content="test")
        fake_events = [
            {"type": "thinking", "message": "hmm"},
            {"type": "tool_call", "calls": [{"name": "bash"}]},
            {"type": "done", "content": "result"},
        ]
        results = list(self.router.handle_message(msg, iter(fake_events)))
        self.assertEqual(len(results), 3)

        # Check first event
        out, raw = results[0]
        self.assertIsInstance(out, MessageOut)
        self.assertEqual(out.event_type, "thinking")
        self.assertEqual(out.thinking, "hmm")

        # Check last event
        out, raw = results[2]
        self.assertEqual(out.event_type, "done")
        self.assertEqual(out.content, "result")


class TestGetRouter(unittest.TestCase):
    def test_singleton(self):
        r1 = get_router()
        r2 = get_router()
        self.assertIs(r1, r2)


# ── web.py tests ─────────────────────────────────────────────────


class TestWebChannel(unittest.TestCase):
    def test_channel_id(self):
        wc = WebChannel()
        self.assertEqual(wc.channel_id, "web")

    def test_capabilities(self):
        wc = WebChannel()
        cap = wc.get_capabilities()
        self.assertTrue(cap.streaming)
        self.assertTrue(cap.images)
        self.assertFalse(cap.audio)
        self.assertEqual(cap.max_message_length, 100000)

    def test_start_stop_noop(self):
        wc = WebChannel()
        wc.start()
        wc.stop()

    def test_send_noop(self):
        wc = WebChannel()
        wc.send("s1", MessageOut(content="test"))


# ── api.py tests ─────────────────────────────────────────────────


class TestAPIChannel(unittest.TestCase):
    def test_channel_id(self):
        ac = APIChannel()
        self.assertEqual(ac.channel_id, "api")

    def test_capabilities(self):
        ac = APIChannel()
        cap = ac.get_capabilities()
        self.assertFalse(cap.streaming)
        self.assertFalse(cap.images)

    def test_has_handlers(self):
        ac = APIChannel()
        self.assertTrue(hasattr(ac, "handle_stream_request"))
        self.assertTrue(hasattr(ac, "handle_sync_request"))


# ── telegram.py tests ────────────────────────────────────────────


class TestTelegramChannel(unittest.TestCase):
    def test_channel_id(self):
        tc = TelegramChannel()
        self.assertEqual(tc.channel_id, "telegram")

    def test_capabilities(self):
        tc = TelegramChannel()
        cap = tc.get_capabilities()
        self.assertFalse(cap.streaming)
        self.assertTrue(cap.images)
        self.assertTrue(cap.audio)
        self.assertTrue(cap.groups)
        self.assertEqual(cap.max_message_length, 4000)

    def test_start_without_token(self):
        # Should not crash, just warn
        tc = TelegramChannel()
        tc.start()
        self.assertFalse(tc._running)

    def test_send_only_done(self):
        tc = TelegramChannel()
        tc.token = "fake"
        # send with non-done event should be a no-op (no crash)
        tc.send("123", MessageOut(event_type="thinking", content="hmm"))

    def test_chunk_text_telegram(self):
        tc = TelegramChannel()
        # 4000 char limit
        text = "x" * 8001
        chunks = tc.chunk_text(text)
        self.assertEqual(len(chunks), 3)
        self.assertEqual(len(chunks[0]), 4000)
        self.assertEqual(len(chunks[1]), 4000)
        self.assertEqual(len(chunks[2]), 1)


# ── voice.py tests ───────────────────────────────────────────────


class TestVoiceChannel(unittest.TestCase):
    def test_channel_id(self):
        vc = VoiceChannel()
        self.assertEqual(vc.channel_id, "voice")

    def test_capabilities(self):
        vc = VoiceChannel()
        cap = vc.get_capabilities()
        self.assertFalse(cap.streaming)
        self.assertFalse(cap.images)
        self.assertTrue(cap.audio)
        self.assertFalse(cap.files)
        self.assertFalse(cap.groups)
        self.assertEqual(cap.max_message_length, 4096)

    def test_start_stop_noop(self):
        vc = VoiceChannel()
        vc.start()
        vc.stop()

    def test_send_noop(self):
        vc = VoiceChannel()
        vc.send("s1", MessageOut(content="test"))

    def test_chunk_text(self):
        vc = VoiceChannel()
        text = "x" * 8193
        chunks = vc.chunk_text(text)
        self.assertEqual(len(chunks), 3)
        self.assertEqual(len(chunks[0]), 4096)
        self.assertEqual(len(chunks[1]), 4096)
        self.assertEqual(len(chunks[2]), 1)

    def test_router_registration(self):
        router = ChannelRouter()
        vc = VoiceChannel()
        router.register(vc)
        self.assertIs(router.get_channel("voice"), vc)
        self.assertIn("voice", router.list_channels())

    def test_handle_message(self):
        router = ChannelRouter()
        router.register(VoiceChannel())
        msg = MessageIn(channel="voice", session_id="s1", user_id="u1", content="bonjour")
        events = [{"type": "done", "content": "salut"}]
        results = list(router.handle_message(msg, iter(events)))
        self.assertEqual(len(results), 1)
        out, raw = results[0]
        self.assertEqual(out.event_type, "done")
        self.assertEqual(out.content, "salut")


# ── whatsapp.py tests ────────────────────────────────────────────


from channels.whatsapp import WhatsAppChannel


class TestWhatsAppChannel(unittest.TestCase):
    def test_channel_id(self):
        wc = WhatsAppChannel()
        self.assertEqual(wc.channel_id, "whatsapp")

    def test_capabilities(self):
        wc = WhatsAppChannel()
        cap = wc.get_capabilities()
        self.assertFalse(cap.streaming)
        self.assertTrue(cap.images)
        self.assertTrue(cap.audio)
        self.assertFalse(cap.files)
        self.assertFalse(cap.groups)
        self.assertEqual(cap.max_message_length, 4000)

    def test_start_stop_noop(self):
        wc = WhatsAppChannel()
        wc.start()
        wc.stop()

    def test_send_ignores_non_done(self):
        wc = WhatsAppChannel()
        # Should not crash on non-done events (no bridge running)
        wc.send("+33612345678", MessageOut(event_type="thinking", content="hmm"))

    def test_chunk_text(self):
        wc = WhatsAppChannel()
        text = "x" * 8001
        chunks = wc.chunk_text(text)
        self.assertEqual(len(chunks), 3)
        self.assertEqual(len(chunks[0]), 4000)
        self.assertEqual(len(chunks[1]), 4000)
        self.assertEqual(len(chunks[2]), 1)

    def test_normalize_sender(self):
        wc = WhatsAppChannel()
        self.assertEqual(wc.normalize_sender("33612345678"), "+33612345678")
        self.assertEqual(wc.normalize_sender("+33612345678"), "+33612345678")
        self.assertEqual(wc.normalize_sender("  +1555123  "), "+1555123")

    def test_is_available_when_bridge_down(self):
        wc = WhatsAppChannel()
        # Bridge not running — should return False, not crash
        self.assertFalse(wc.is_available())

    def test_get_bridge_status_when_down(self):
        wc = WhatsAppChannel()
        status = wc.get_bridge_status()
        self.assertFalse(status["connected"])
        self.assertEqual(status["status"], "error")

    def test_router_registration(self):
        router = ChannelRouter()
        wc = WhatsAppChannel()
        router.register(wc)
        self.assertIs(router.get_channel("whatsapp"), wc)
        self.assertIn("whatsapp", router.list_channels())

    def test_on_inbound_empty_text(self):
        wc = WhatsAppChannel()
        # Empty text and no media should return None
        result = wc.on_inbound({"from": "+33612345678", "text": "", "type": "text"})
        self.assertIsNone(result)

    def test_history_isolation(self):
        wc = WhatsAppChannel()
        # Each phone should have its own history
        self.assertEqual(wc._histories, {})


if __name__ == "__main__":
    unittest.main()
