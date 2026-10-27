"""Crash recovery and cross-process claims use temporary SQLite databases."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import store


class DeliveryIntentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(store, "DB_PATH", Path(self.tmp.name) / "intent.db")
        self.patch.start()
        self.c = store.connect()

    def tearDown(self):
        self.c.close()
        self.patch.stop()
        self.tmp.cleanup()

    def test_second_connection_cannot_claim_inflight_write(self):
        self.assertTrue(store.claim_greet(self.c, "job", "sid", "body"))
        second = store.connect()
        try:
            self.assertFalse(store.claim_greet(second, "job", "sid", "other"))
            self.assertIn("job", store.delivery_blocked_jobs(second))
        finally:
            second.close()
        self.assertNotIn("job", store.already_greeted(self.c))

    def test_unknown_and_confirmed_writes_cannot_be_automatically_replayed(self):
        for status in ("sent", "outcome_unknown", "executing"):
            store.record_greet(self.c, status, "sid", "body", status)
            self.assertFalse(store.claim_greet(self.c, status, "sid", "again"))
        self.assertEqual(store.delivery_blocked_jobs(self.c), {"sent", "outcome_unknown", "executing"})

    def test_explicit_pre_submission_failure_can_be_claimed(self):
        store.record_greet(self.c, "job", "sid", "body", "failed")
        self.assertTrue(store.claim_greet(self.c, "job", "sid", "retry"))

    def test_crash_after_claim_is_still_blocked_after_reopen(self):
        store.claim_greet(self.c, "job", "sid", "body")
        self.c.close()
        self.c = store.connect()
        self.assertFalse(store.claim_greet(self.c, "job", "sid", "again"))
