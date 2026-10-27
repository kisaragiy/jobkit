"""Manual HR assistance uses the existing kernel and tasks, never a platform account."""
from contextlib import closing
import sqlite3
import tempfile
from pathlib import Path
import unittest
from unittest import mock

from fastapi.testclient import TestClient
import app
import main
import store
from sandbox import KernelError


class ManualQueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(store, "DB_PATH", Path(self.tmp.name) / "runtime.db")
        patch.start()
        self.addCleanup(patch.stop)
        self.context = main.dispatch("runtime.context")["data"]
        self.call("runtime.document.save", {"content": {"location": "fictional office", "salary": "approved salary", "source": "fixture"},
                                            "confirmed": True, "expected_revision": 0})

    def call(self, tool, payload=None):
        return main.dispatch(tool, payload, self.context["context_token"])

    def items(self):
        return [{"conversation_note": "candidate A", "message": "工作地点在哪里？"},
                {"conversation_note": "candidate B", "message": "工作地点在哪里？"}]

    def batch(self, items=None, key="one"):
        return self.call("runtime.task.batch.submit", {"items": self.items() if items is None else items, "idempotency_key": key})

    def test_identical_text_is_two_manual_records_and_retry_reuses_ids(self):
        first = self.batch()["data"]
        self.assertEqual(first["created"], 2)
        self.assertNotEqual(first["tasks"][0]["task_id"], first["tasks"][1]["task_id"])
        self.assertEqual(first["tasks"][0]["source_type"], "manual")
        self.assertFalse(self.context["account_verified"])
        repeated = self.batch()["data"]
        self.assertEqual((repeated["created"], repeated["reused"]), (0, 2))
        self.assertEqual([t["task_id"] for t in repeated["tasks"]], [t["task_id"] for t in first["tasks"]])
        self.assertEqual(self.batch(key="new-batch")["data"]["created"], 2)

    def test_changed_batch_count_or_contents_conflicts_without_partial_insert(self):
        self.batch()
        for items in (self.items()[:1], self.items() + [{"conversation_note": "C", "message": "薪资？"}],
                      [{"conversation_note": "different", "message": "工作地点在哪里？"}] + self.items()[1:]):
            with self.assertRaises(KernelError) as error:
                self.batch(items)
            self.assertEqual(error.exception.code, "IDEMPOTENCY_CONFLICT")
        self.assertEqual(len(self.call("runtime.task.list")["data"]), 2)

    def test_invalid_row_or_receipt_failure_rolls_back_entire_batch(self):
        with self.assertRaises(KernelError):
            self.batch([self.items()[0], {"conversation_note": "B", "message": ""}])
        with mock.patch("execution_engine.record_commit", side_effect=KernelError("FIXTURE_FAILURE", "fixture")):
            with self.assertRaises(KernelError):
                self.batch()
        with store.runtime_db() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM runtime_tasks WHERE kind='work'").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT count(*) FROM runtime_manual_batches").fetchone()[0], 0)

    def test_draft_review_and_reopen_do_not_claim_platform_send(self):
        task = self.batch()["data"]["tasks"][0]
        done = self.call("runtime.task.execute", {"task_id": task["task_id"]})["data"]
        self.assertEqual(done["conversation_note"], "candidate A")
        self.assertEqual(done["result"]["text"], "fictional office")
        reviewed = self.call("runtime.task.review", {"task_id": task["task_id"], "handled": True, "expected_review_revision": 0})
        self.assertEqual(reviewed["data"]["review_status"], "handled")
        self.assertFalse(reviewed["data"]["result"]["sent"])
        self.assertEqual(len(self.call("runtime.task.list", {"review": "pending"})["data"]), 1)
        self.assertEqual(len(self.call("runtime.task.list", {"review": "handled"})["data"]), 1)
        with self.assertRaises(KernelError) as error:
            self.call("runtime.task.review", {"task_id": task["task_id"], "handled": False, "expected_review_revision": 0})
        self.assertEqual(error.exception.code, "STALE_REVIEW")
        reopened = self.call("runtime.task.review", {"task_id": task["task_id"], "handled": False, "expected_review_revision": 1})["data"]
        self.assertEqual((reopened["review_status"], reopened["status"]), ("pending", "completed"))

    def test_queued_task_cannot_be_mislabelled_handled(self):
        task = self.batch()["data"]["tasks"][0]
        with self.assertRaises(KernelError) as error:
            self.call("runtime.task.review", {"task_id": task["task_id"], "handled": True, "expected_review_revision": 0})
        self.assertEqual(error.exception.code, "REVIEW_NOT_READY")

    def test_batch_and_review_receipts_reconcile_without_new_work_or_reverting_review(self):
        batch = self.batch()
        task = batch["data"]["tasks"][0]
        self.call("runtime.task.execute", {"task_id": task["task_id"]})
        review = self.call("runtime.task.review", {"task_id": task["task_id"], "handled": True, "expected_review_revision": 0})
        for response in (batch, review):
            with store.runtime_db() as c:
                c.execute("UPDATE runtime_tasks SET status='executing',result=NULL WHERE task_id=?", (response["trace_id"],))
            done = self.call("runtime.task.recover", {"task_id": response["trace_id"]})["data"]
            self.assertEqual(done["error_code"], "RECOVERED_COMMIT")
        self.assertEqual(self.call("runtime.task.get", {"task_id": task["task_id"]})["data"]["review_status"], "handled")
        self.assertEqual(len(self.call("runtime.task.list")["data"]), 2)

    def test_other_scope_cannot_read_or_review_manual_message(self):
        task = self.batch()["data"]["tasks"][0]
        self.context = self.call("runtime.switch", {"role": "applicant", "organization_id": "local", "account_id": "other"})["data"]
        self.assertEqual(self.call("runtime.task.list")["data"], [])
        with self.assertRaises(KernelError) as error:
            self.call("runtime.task.review", {"task_id": task["task_id"], "handled": True, "expected_review_revision": 0})
        self.assertEqual(error.exception.code, "TASK_NOT_FOUND")

    def test_existing_schema_gains_review_columns_without_losing_draft(self):
        task = self.batch()["data"]["tasks"][0]
        with closing(sqlite3.connect(store.DB_PATH)) as c:
            for column in ("conversation_note", "review_status", "review_revision"):
                c.execute("ALTER TABLE runtime_tasks DROP COLUMN " + column)
            c.commit()
        restored = self.call("runtime.task.get", {"task_id": task["task_id"]})["data"]
        self.assertEqual(restored["message"], task["message"])
        self.assertEqual((restored["review_status"], restored["review_revision"]), ("pending", 0))

    def test_real_http_manual_assistance_workflow(self):
        with TestClient(app.app, base_url="http://127.0.0.1:18770") as client:
            client.get("/workspace")
            headers = {"X-Jobkit-Context": self.context["context_token"]}
            def request(method, path, payload=None):
                return client.request(method, "/api/runtime/" + path, headers=headers, json=payload)
            response = request("POST", "tasks/batch", {"items": self.items(), "idempotency_key": "http"})
            self.assertEqual(response.status_code, 200, response.text)
            task = response.json()["data"]["tasks"][0]
            self.assertEqual(request("POST", "tasks/" + task["task_id"] + "/execute", {}).json()["data"]["result"]["text"], "fictional office")
            reviewed = request("POST", "tasks/" + task["task_id"] + "/review", {"handled": True, "expected_review_revision": 0})
            self.assertEqual(reviewed.status_code, 200)
            self.assertEqual(len(request("GET", "tasks?review=handled").json()["data"]), 1)
            self.assertEqual(len(request("GET", "tasks?review=pending").json()["data"]), 1)
            self.assertEqual(request("GET", "tasks?review=invalid").json()["code"], "INVALID_REVIEW")
        with TestClient(app.app, base_url="http://127.0.0.1:18770") as anonymous:
            self.assertEqual(anonymous.post("/api/runtime/tasks/batch", json={}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
