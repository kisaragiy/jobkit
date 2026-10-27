"""Unregistered legacy primitives fail before touching any real browser or CLI."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import apply_pipeline
import chat_send
import store


class KernelBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(store, "DB_PATH", Path(self.tmp.name) / "runtime.db")
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_chat_fill_and_send_reject_before_cdp(self):
        with mock.patch.object(chat_send, "_cdp_target", side_effect=AssertionError("must not touch browser")):
            for dry_run in (False, True):
                with self.assertRaisesRegex(chat_send.SendError, "KERNEL_REQUIRED"):
                    chat_send.send("synthetic", "synthetic", dry_run=dry_run)

    def test_greet_rejects_before_cli_subprocess(self):
        from sandbox import KernelError
        with mock.patch.object(apply_pipeline, "run_boss", side_effect=AssertionError("must not call CLI")):
            with self.assertRaisesRegex(KernelError, "旧执行入口"):
                apply_pipeline.send_greet("synthetic", "synthetic", "synthetic")
        with store.runtime_db() as c:
            row = c.execute("SELECT code FROM runtime_events WHERE tool='boss.greet'").fetchone()
            self.assertEqual(row[0], "KERNEL_REQUIRED")

    def test_refused_legacy_write_is_not_misreported_as_an_unknown_send(self):
        with mock.patch.object(apply_pipeline, "run_boss", side_effect=AssertionError("must not call CLI")):
            ok, error, attempts = apply_pipeline.send_with_retry("synthetic", "synthetic", "synthetic")
        self.assertFalse(ok)
        self.assertIn("KERNEL_REQUIRED", error)
        self.assertNotIn("OUTCOME_UNKNOWN", error)
        self.assertEqual(attempts, 0)
