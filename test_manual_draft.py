"""Human edits preserve the generated draft, use CAS, and never imply a send."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock
from fastapi.testclient import TestClient
import app
import main
import store
from sandbox import KernelError


class ManualDraftTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        patch = mock.patch.object(store, "DB_PATH", Path(temp.name) / "runtime.db")
        patch.start()
        self.addCleanup(patch.stop)
        self.context = main.dispatch("runtime.context")["data"]
        self.save(0)
        self.task = self.submit("工作地点在哪里？", "fixture")
        self.original = self.call("runtime.task.execute", {"task_id": self.task["task_id"]})["data"]["result"]

    def call(self, name, payload=None):
        return main.dispatch(name, payload, self.context["context_token"])

    def save(self, revision):
        return self.call("runtime.document.save", {"content": {"location": "fictional office", "source": "fixture"}, "confirmed": True, "expected_revision": revision})

    def submit(self, message, key):
        return self.call("runtime.task.submit", {"tool": "draft.reply", "input": {"message": message}, "idempotency_key": key})["data"]

    def edit(self, text="您好，工作地点为 fictional office。", expected=0, **overrides):
        payload = {"task_id": self.task["task_id"], "text": text, "confirmed": True, "expected_manual_revision": expected, "expected_document_revision": 1}
        return self.call("runtime.task.edit", {**payload, **overrides})

    def history(self):
        return self.call("runtime.task.edits.get", {"task_id": self.task["task_id"]})["data"]

    def test_edit_preserves_original_and_copy_uses_confirmed_human_text(self):
        edited = self.edit()["data"]
        self.assertEqual(edited["result"], self.original)
        self.assertEqual((edited["manual_revision"], edited["review_status"]), (1, "pending"))
        proof = self.call("runtime.task.copy.get", {"task_id": self.task["task_id"]})["data"]
        self.assertEqual((proof["text"], proof["manual_revision"]), (edited["manual_text"], 1))
        self.assertFalse(edited["result"]["sent"])
        self.assertEqual(self.history()["edits"][0]["text"], edited["manual_text"])

    def test_confirmation_and_validation_reject_without_edits(self):
        for override in ({"confirmed": False}, {"confirmed": 1}, {"text": " "}, {"text": "x" * 8001}, {"expected_manual_revision": True}, {"unexpected": 1}):
            with self.assertRaises(KernelError):
                self.edit(**override)
        self.assertEqual(self.history()["total"], 0)

    def test_retry_reuses_revision_and_conflicting_or_old_writer_cannot_overwrite(self):
        first = self.edit("first")["data"]
        self.assertEqual(self.edit("first")["data"], first)
        with self.assertRaises(KernelError) as error:
            self.edit("conflicting")
        self.assertEqual(error.exception.code, "STALE_MANUAL_DRAFT")
        self.edit("second", expected=1)
        with self.assertRaises(KernelError):
            self.edit("first")
        self.assertEqual([e["text"] for e in self.history()["edits"]], ["second", "first"])

    def test_rollback_includes_latest_text_history_event_and_receipt(self):
        with mock.patch("execution_engine.record_commit", side_effect=KernelError("FIXTURE", "fixture")):
            with self.assertRaises(KernelError):
                self.edit()
        self.assertEqual(self.history()["total"], 0)
        task = self.call("runtime.task.get", {"task_id": self.task["task_id"]})["data"]
        self.assertEqual((task["manual_text"], task["manual_revision"]), (None, 0))
        with store.runtime_db() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM runtime_events WHERE status='edited'").fetchone()[0], 0)

    def test_receipt_recovery_does_not_restore_earlier_manual_version(self):
        response = self.edit("first")
        self.edit("second", expected=1)
        with store.runtime_db() as c:
            c.execute("UPDATE runtime_tasks SET status='executing',result=NULL WHERE task_id=?", (response["trace_id"],))
        with mock.patch.object(main._engine, "edit_draft", side_effect=AssertionError("no replay")):
            done = self.call("runtime.task.recover", {"task_id": response["trace_id"]})["data"]
        self.assertEqual(done["error_code"], "RECOVERED_COMMIT")
        self.assertEqual(done["result"]["manual_text"], "first")
        self.assertEqual(self.call("runtime.task.copy.get", {"task_id": self.task["task_id"]})["data"]["text"], "second")

    def test_updated_facts_prevent_edit_and_copy_but_preserve_history(self):
        self.edit()
        self.save(1)
        for name, payload in (("runtime.task.edit", {"task_id": self.task["task_id"], "text": "new", "confirmed": True, "expected_manual_revision": 1, "expected_document_revision": 2}),
                              ("runtime.task.copy.get", {"task_id": self.task["task_id"]})):
            with self.assertRaises(KernelError) as error:
                self.call(name, payload)
            self.assertEqual(error.exception.code, "STALE_FACTS")
        successor = self.call("runtime.task.replace", {"task_id": self.task["task_id"], "expected_review_revision": 0, "expected_document_revision": 2})["data"]
        self.assertEqual((successor["manual_text"], successor["manual_revision"]), (None, 0))
        self.assertEqual(self.history()["total"], 1)
        with self.assertRaises(KernelError):
            self.edit()

    def test_queued_needs_review_and_handled_records_cannot_be_edited(self):
        queued = self.submit("地点？", "queued")
        with self.assertRaises(KernelError):
            self.edit(task_id=queued["task_id"])
        unknown = self.submit("年龄限制？", "unknown")
        self.call("runtime.task.execute", {"task_id": unknown["task_id"]})
        with self.assertRaises(KernelError):
            self.edit(task_id=unknown["task_id"])
        self.call("runtime.task.review", {"task_id": self.task["task_id"], "handled": True, "expected_review_revision": 0})
        with self.assertRaises(KernelError):
            self.edit()

    def test_scope_denies_edit_and_history(self):
        self.edit()
        self.context = self.call("runtime.switch", {"role": "applicant", "organization_id": "local", "account_id": "other"})["data"]
        for action in (self.edit, self.history):
            with self.assertRaises(KernelError) as error:
                action()
            self.assertEqual(error.exception.code, "TASK_NOT_FOUND")

    def test_concurrent_editors_keep_one_revision(self):
        outcomes = []
        def worker(text):
            try:
                outcomes.append(self.edit(text)["data"]["manual_text"])
            except KernelError as error:
                outcomes.append(error.code)
        threads = [threading.Thread(target=worker, args=(text,)) for text in ("one", "two")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sum(value in {"one", "two"} for value in outcomes), 1)
        self.assertEqual(self.history()["total"], 1)

    def test_old_schema_migration_preserves_original(self):
        with closing(sqlite3.connect(store.DB_PATH)) as c:
            c.execute("ALTER TABLE runtime_tasks DROP COLUMN manual_text")
            c.execute("ALTER TABLE runtime_tasks DROP COLUMN manual_revision")
            c.commit()
        task = self.call("runtime.task.get", {"task_id": self.task["task_id"]})["data"]
        self.assertEqual(task["result"], self.original)
        self.assertEqual((task["manual_text"], task["manual_revision"]), (None, 0))
        self.edit()

    def test_corrupt_manual_text_does_not_fall_back_to_original(self):
        for text in (None, "text without confirmed edit record"):
            with store.runtime_db() as c:
                c.execute("UPDATE runtime_tasks SET manual_revision=1,manual_text=? WHERE task_id=?", (text, self.task["task_id"]))
            for operation in (lambda: self.call("runtime.task.copy.get", {"task_id": self.task["task_id"]}), lambda: self.edit(expected=1)):
                with self.assertRaises(KernelError) as error:
                    operation()
                self.assertEqual(error.exception.code, "INVALID_MANUAL_DRAFT_STATE")

    def test_actual_http_edit_history_and_auth(self):
        with TestClient(app.app, base_url="http://127.0.0.1:18770") as client:
            client.get("/workspace")
            headers = {"X-Jobkit-Context": self.context["context_token"]}
            url = "/api/runtime/tasks/" + self.task["task_id"]
            response = client.post(url + "/edit", headers=headers, json={"text": "人工核对内容", "confirmed": True, "expected_manual_revision": 0, "expected_document_revision": 1})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(client.get(url + "/edits", headers=headers).json()["data"]["total"], 1)
            self.assertEqual(client.get(url + "/copy", headers=headers).json()["data"]["text"], "人工核对内容")
        with TestClient(app.app, base_url="http://127.0.0.1:18770") as client:
            self.assertEqual(client.post(url + "/edit", json={}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
