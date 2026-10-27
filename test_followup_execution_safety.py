"""Transport ambiguity, crashes and draft fill must not replay sends."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from datetime import datetime
import followup as F
import run_followup as runner


class FollowupSafetyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state.json"
        self.now = int(datetime(2026, 10, 19, 12).timestamp() * 1000)
        self.calls = []
        self.logs = []

    def tearDown(self):
        self.tmp.cleanup()

    def cycle(self, sender, **kwargs):
        row = {"job_id": "job", "company": "synthetic", "title": "synthetic",
               "status": "sent", "greeted_at": (self.now - 4 * F.DAY_MS) / 1000}
        return runner.run_cycle(send=not kwargs.get("fill", False), state_path=self.state, rows_fn=lambda: [row],
                                replied_fn=lambda: [], matcher_fn=lambda a, b: a == b,
                                gate_fn=lambda: (True, "stub", "stub"), sender=sender,
                                now_ms=self.now, out=self.logs.append, **kwargs)

    def test_remote_timeout_is_blocked_on_next_run(self):
        def sender(*args):
            self.calls.append(1)
            return False, "[TIMEOUT] synthetic"
        self.assertEqual(self.cycle(sender), runner.EXIT_ALERT)
        self.assertEqual(self.cycle(sender), runner.EXIT_ALERT)
        self.assertEqual(self.calls, [1])
        self.assertEqual(F.load_state(self.state)["sent"], [])
        self.assertEqual(F.load_state(self.state)["pending"]["job"]["state"], "outcome_unknown")

    def test_inflight_crash_marker_survives_restart(self):
        self.state.write_text(json.dumps({"sent": [], "pending": {"job": {"state": "executing"}}}))
        self.assertEqual(self.cycle(lambda *args: self.calls.append(1)), runner.EXIT_ALERT)
        self.assertEqual(self.calls, [])

    def test_unwritable_intent_prevents_send(self):
        with mock.patch.object(F, "save_state", return_value=False):
            self.assertEqual(self.cycle(lambda *args: self.calls.append(1)), runner.EXIT_ERROR)
        self.assertEqual(self.calls, [])

    def test_corrupt_state_prevents_send(self):
        self.state.write_text("{broken")
        self.assertEqual(self.cycle(lambda *args: self.calls.append(1)), runner.EXIT_ERROR)
        self.assertEqual(self.calls, [])

    def test_fill_is_not_recorded_as_a_send(self):
        self.assertEqual(self.cycle(lambda *args: (True, ""), fill=True), runner.EXIT_OK)
        self.assertFalse(self.state.exists())

    def test_local_echo_cannot_mark_followup_as_confirmed(self):
        import chat_send
        with mock.patch.object(chat_send, "send", return_value={"ok": True, "evidence": "local_echo"}):
            ok, error = runner.make_cdp_sender()({"company": "synthetic", "message": "synthetic"}, self.now)
        self.assertFalse(ok)
        self.assertIn("OUTCOME_UNKNOWN", error)

    def test_invalid_history_cannot_be_silently_discarded(self):
        self.state.write_text(json.dumps({"sent": "broken"}))
        self.assertEqual(self.cycle(lambda *args: self.calls.append(1)), runner.EXIT_ERROR)
        self.assertEqual(self.calls, [])
