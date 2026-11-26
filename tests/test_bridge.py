import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "python" / "hooks"))

import bridge
import agy_hook


class BridgeTextFormattingTests(unittest.TestCase):
    def test_clean_tui_output_strips_agy_chrome_and_echo(self):
        sample = """────────────────────────────────
> 1

收到，有什么我可以帮你的？
────────────────────────────────
>
────────────────────────────────
? for shortcuts
accept-edits · Gemini 4 Neon · high
|
Ctx ━────────── 1.5% · ↑115.4k ↓325
Quota Gemini 5h 63.6%(2h40m) wk 28.6%(5d16h) | 3P 5h 100% wk 35.3%(6d0h)
"""
        self.assertEqual(
            bridge.clean_tui_output(sample, last_sent="1"),
            "收到，有什么我可以帮你的？",
        )

    def test_md_to_tg_html_renders_markdown_and_escapes_html(self):
        md = (
            "### 结论\n"
            "- **正常**运行 `rex.service` 在 [/root](file:///root)\n"
            "- 查看 [文档](https://example.com/a?x=1&y=2)\n"
            "```python\n"
            "print(1 < 2 and 3 > 1)\n"
            "```"
        )
        rendered = bridge.md_to_tg_html(md)
        self.assertIn("<b>结论</b>", rendered)
        self.assertIn("• <b>正常</b>运行 <code>rex.service</code> 在 <code>/root</code>", rendered)
        self.assertIn('<a href="https://example.com/a?x=1&amp;y=2">文档</a>', rendered)
        self.assertIn('<pre><code class="language-python">print(1 &lt; 2 and 3 &gt; 1)</code></pre>', rendered)

    def test_split_chunks_respects_limit(self):
        text = "\n".join(f"line-{i}" for i in range(100))
        chunks = bridge.split_chunks(text, limit=80)
        self.assertTrue(len(chunks) > 1)
        for c in chunks:
            self.assertLessEqual(len(c), 80)

    def test_compute_capture_diff(self):
        prev = "a\nb\nc\nd\ne\nf\ng"
        curr = "a\nb\nc\nd\ne\nf\ng\nnew-1\nnew-2"
        self.assertEqual(bridge.compute_capture_diff(prev, curr), "new-1\nnew-2")


class BridgeConcurrencyAndLifecycleTests(unittest.TestCase):
    def _make_mock_bridge(self, tmpdir: str):
        calls = []

        class MockTG:
            def call(self, method, payload=None, timeout=40):
                if method == "createForumTopic":
                    time.sleep(0.15)
                    calls.append((method, payload))
                    return {"message_thread_id": 42}
                calls.append((method, payload))
                return {}

            def send_html(self, *args, **kwargs):
                return {"message_id": 100}

            def react(self, *args, **kwargs):
                pass

        class MockRex:
            def __init__(self):
                self.killed = []
                self.renamed = []
                self.app_clients = []
                self.cache_invalidations = 0

            def resolve_default_block(self, sid, preferred_block_id=None):
                return "block:abc123"

            def list_session_blocks(self, sid):
                return ({}, [])

            def kill_session(self, sid):
                self.killed.append(sid)

            def rename_session(self, sid, label):
                self.renamed.append((sid, label))

            def list_connected_app_clients(self):
                return list(self.app_clients)

            def invalidate_client_cache(self):
                self.cache_invalidations += 1

            def capture(self, sid, bid, tail_lines=None):
                return ""

        st = bridge.StateStore(Path(tmpdir) / "state.json")
        st.data["chat_id"] = -100123456
        st.data["allowed_user_id"] = 999
        st.save()
        rex = MockRex()
        b = bridge.Bridge(MockTG(), rex, st)
        return b, rex, st, calls

    def test_ensure_topic_concurrent_creates_only_once(self):
        with tempfile.TemporaryDirectory() as td:
            b, _rex, _st, calls = self._make_mock_bridge(td)
            results = []
            threads = [
                threading.Thread(target=lambda: results.append(b.ensure_topic_for_session("session:race", "demo")))
                for _ in range(5)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            create_calls = [c for c in calls if c[0] == "createForumTopic"]
            self.assertEqual(len(create_calls), 1)
            self.assertEqual(results, [42] * 5)

    def test_topic_close_kills_session_and_deletes_topic(self):
        with tempfile.TemporaryDirectory() as td:
            b, rex, st, calls = self._make_mock_bridge(td)
            b.ensure_topic_for_session("session:s1", "demo")
            b.handle_telegram_message(
                {
                    "chat": {"id": -100123456, "type": "supergroup", "is_forum": True},
                    "from": {"id": 999, "is_bot": False},
                    "message_thread_id": 42,
                    "forum_topic_closed": {},
                }
            )
            self.assertEqual(rex.killed, ["session:s1"])
            self.assertNotIn("session:s1", st.data["sessions"])
            self.assertNotIn("42", st.data["topics"])
            self.assertTrue(any(c[0] == "deleteForumTopic" and c[1]["message_thread_id"] == 42 for c in calls))

    def test_topic_edit_renames_rex_session(self):
        with tempfile.TemporaryDirectory() as td:
            b, rex, st, _calls = self._make_mock_bridge(td)
            b.ensure_topic_for_session("session:s1", "old-name")
            b.handle_telegram_message(
                {
                    "chat": {"id": -100123456, "type": "supergroup", "is_forum": True},
                    "from": {"id": 999, "is_bot": False},
                    "message_thread_id": 42,
                    "forum_topic_edited": {"name": "new-name"},
                }
            )
            self.assertEqual(rex.renamed, [("session:s1", "new-name")])
            self.assertEqual(st.data["sessions"]["session:s1"]["label"], "new-name")

    def test_smart_mode_suppresses_when_app_connected_unless_tg_active_or_urgent(self):
        with tempfile.TemporaryDirectory() as td:
            b, rex, st, _calls = self._make_mock_bridge(td)
            b.ensure_topic_for_session("session:s1", "demo")

            # App away -> push allowed
            rex.app_clients = []
            self.assertEqual(b.should_push_outbound("session:s1", "done")[0], True)

            # App connected, no TG interaction -> normal reply suppressed, urgent allowed
            rex.app_clients = [{"client_id": "c1", "info": {"kind": "app", "label": "MacBook"}}]
            ok_done, reason_done = b.should_push_outbound("session:s1", "done")
            self.assertFalse(ok_done)
            self.assertEqual(reason_done, "app_connected_idle_in_tg")
            self.assertEqual(b.should_push_outbound("session:s1", "blocked"), (True, "urgent"))
            self.assertEqual(b.should_push_outbound("session:s1", "question"), (True, "urgent"))
            self.assertEqual(b.should_push_outbound("session:s1", "error"), (True, "urgent"))

            # User interacts from TG -> normal reply allowed in smart mode
            b.mark_tg_activity("session:s1")
            self.assertEqual(b.should_push_outbound("session:s1", "done"), (True, "tg_active"))

            # Switching to away mode suppresses even TG-active normal replies while app connected
            st.data["notify_mode"] = "away"
            self.assertEqual(b.should_push_outbound("session:s1", "done"), (False, "app_connected_away"))

            # Per-topic /watch forces push; /mute suppresses even urgent
            st.data["sessions"]["session:s1"]["notify_override"] = "watch"
            self.assertEqual(b.should_push_outbound("session:s1", "done"), (True, "topic_watched"))
            st.data["sessions"]["session:s1"]["notify_override"] = "mute"
            self.assertEqual(b.should_push_outbound("session:s1", "blocked"), (False, "topic_muted"))

            # Client join/leave event invalidates app client cache
            b.handle_rex_event({"type": "client_left", "payload": {"client_id": "c1"}})
            self.assertEqual(rex.cache_invalidations, 1)


class AgyHookTests(unittest.TestCase):
    def test_extract_last_turn_reply(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "transcript.jsonl"
            entries = [
                {"type": "USER_INPUT", "content": "first"},
                {"type": "PLANNER_RESPONSE", "content": "old reply"},
                {"type": "USER_INPUT", "content": "second"},
                {"type": "PLANNER_RESPONSE", "thinking": "hmm", "tool_calls": [{"name": "x"}]},
                {"type": "PLANNER_RESPONSE", "content": "final clean reply"},
            ]
            p.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")
            self.assertEqual(agy_hook.extract_last_turn_reply(str(p)), "final clean reply")


if __name__ == "__main__":
    unittest.main()
