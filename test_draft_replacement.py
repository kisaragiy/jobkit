"""A new facts version creates one linked successor, never replays an old draft."""
from contextlib import closing
import sqlite3
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
from fastapi.testclient import TestClient
import app
import main
import store
from sandbox import KernelError
from task_lease import task_lease


class DraftReplacementTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(store, "DB_PATH", Path(self.tmp.name) / "runtime.db")
        patch.start()
        self.addCleanup(patch.stop)
        self.context = main.dispatch("runtime.context")["data"]
        self.save("old office", 0)
        self.task = self.call("runtime.task.submit", {"tool": "draft.reply", "input": {"message": "工作地点在哪里？", "conversation_note": "candidate A"}, "idempotency_key": "source"})["data"]

    def call(self, name, payload=None):
        return main.dispatch(name, payload, self.context["context_token"])

    def save(self, location, revision):
        self.call("runtime.document.save", {"content": {"location": location, "source": "fixture"}, "confirmed": True, "expected_revision": revision})

    def replace(self, revision=2, review=0):
        return self.call("runtime.task.replace", {"task_id": self.task["task_id"], "expected_review_revision": review, "expected_document_revision": revision})

    def test_stale_projection_and_server_copy_guard(self):
        self.call("runtime.task.execute", {"task_id": self.task["task_id"]})
        self.assertEqual(self.call("runtime.task.copy.get", {"task_id": self.task["task_id"]})["data"]["text"], "old office")
        self.save("new office", 1)
        row = self.call("runtime.task.list")["data"][0]
        self.assertTrue(row["draft_stale"])
        self.assertEqual(row["current_document_revision"], 2)
        with self.assertRaises(KernelError) as error:
            self.call("runtime.task.copy.get", {"task_id": self.task["task_id"]})
        self.assertEqual(error.exception.code, "STALE_FACTS")

    def test_completed_source_is_preserved_and_retries_share_successor(self):
        original = self.call("runtime.task.execute", {"task_id": self.task["task_id"]})["data"]
        self.save("new office", 1)
        new = self.replace()["data"]
        self.assertEqual(self.replace()["data"], new)
        self.assertEqual((new["status"], new["conversation_note"], new["document_revision"]), ("queued", "candidate A", 2))
        old = self.call("runtime.task.get", {"task_id": self.task["task_id"]})["data"]
        self.assertEqual((old["status"], old["review_status"]), ("completed", "superseded"))
        self.assertEqual(old["result"], original["result"])
        self.assertEqual(old["superseded_by_task_id"], new["task_id"])
        self.assertEqual(new["supersedes_task_id"], old["task_id"])
        with self.assertRaises(KernelError) as error:
            self.call("runtime.task.copy.get", {"task_id": old["task_id"]})
        self.assertEqual(error.exception.code, "TASK_SUPERSEDED")
        self.call("runtime.task.execute", {"task_id": new["task_id"]})
        proof = self.call("runtime.task.copy.get", {"task_id": new["task_id"]})["data"]
        self.assertEqual((proof["text"], proof["document_revision"]), ("new office", 2))
        self.assertEqual(len(self.call("runtime.task.list", {"review": "pending"})["data"]), 1)
        self.assertEqual(len(self.call("runtime.task.list", {"review": "superseded"})["data"]), 1)

    def test_queued_source_is_cancelled_without_implicit_generation(self):
        self.save("new office", 1)
        with mock.patch("recruiter_reply.draft", side_effect=AssertionError("must not generate")):
            new = self.replace()["data"]
            old = self.call("runtime.task.execute", {"task_id": self.task["task_id"]})["data"]
        self.assertEqual((old["status"], old["error_code"]), ("cancelled", "SUPERSEDED"))
        self.assertEqual(new["status"], "queued")

    def test_receipt_failure_rolls_back_both_links_and_new_task(self):
        self.save("new office", 1)
        with mock.patch("execution_engine.record_commit", side_effect=KernelError("FIXTURE_FAILURE", "fixture")):
            with self.assertRaises(KernelError):
                self.replace()
        old = self.call("runtime.task.get", {"task_id": self.task["task_id"]})["data"]
        self.assertIsNone(old["superseded_by_task_id"])
        self.assertEqual(old["review_status"], "pending")
        self.assertEqual(len(self.call("runtime.task.list")["data"]), 1)

    def test_handled_active_and_unknown_records_require_explicit_resolution(self):
        self.call("runtime.task.execute", {"task_id": self.task["task_id"]})
        self.call("runtime.task.review", {"task_id": self.task["task_id"], "handled": True, "expected_review_revision": 0})
        self.save("new office", 1)
        with self.assertRaises(KernelError):
            self.replace(review=1)
        with store.runtime_db() as c:
            c.execute("UPDATE runtime_tasks SET review_status='pending',review_revision=0,status='outcome_unknown' WHERE task_id=?", (self.task["task_id"],))
        with self.assertRaises(KernelError) as error:
            self.replace()
        self.assertEqual(error.exception.code, "TASK_BUSY")
        with task_lease(self.task["task_id"]):
            with self.assertRaises(KernelError) as error:
                self.replace()
        self.assertEqual(error.exception.code, "TASK_BUSY")

    def test_version_and_scope_changes_reject_without_new_work(self):
        self.save("new office", 1)
        for revision, review in ((1, 0), (2, 9)):
            with self.assertRaises(KernelError):
                self.replace(revision=revision, review=review)
        self.assertEqual(len(self.call("runtime.task.list")["data"]), 1)
        self.context = self.call("runtime.switch", {"role": "applicant", "organization_id": "local", "account_id": "other"})["data"]
        with self.assertRaises(KernelError) as error:
            self.replace()
        self.assertEqual(error.exception.code, "TASK_NOT_FOUND")

    def test_concurrent_requests_create_only_one_successor(self):
        self.save("new office", 1)
        results, errors = [], []
        def run():
            try:
                results.append(self.replace()["data"])
            except KernelError as error:
                errors.append(error.code)
        workers = [threading.Thread(target=run) for _ in range(2)]
        for worker in workers: worker.start()
        for worker in workers:
            worker.join(8)
            self.assertFalse(worker.is_alive())
        self.assertTrue(results)
        self.assertTrue(all(code == "TASK_BUSY" for code in errors))
        self.assertEqual(len({r["task_id"] for r in results}), 1)
        self.assertEqual(len(self.call("runtime.task.list")["data"]), 2)

    def test_recovery_restores_replacement_receipt_without_more_work(self):
        self.save("new office", 1)
        replaced = self.replace()
        with store.runtime_db() as c:
            c.execute("UPDATE runtime_tasks SET status='executing',result=NULL WHERE task_id=?", (replaced["trace_id"],))
        done = self.call("runtime.task.recover", {"task_id": replaced["trace_id"]})["data"]
        self.assertEqual(done["error_code"], "RECOVERED_COMMIT")
        self.assertEqual(done["result"], replaced["data"])
        self.assertEqual(len(self.call("runtime.task.list")["data"]), 2)

    def test_old_schema_gains_links_without_losing_history(self):
        with closing(sqlite3.connect(store.DB_PATH)) as c:
            for column in ("supersedes_task_id", "superseded_by_task_id"):
                c.execute("ALTER TABLE runtime_tasks DROP COLUMN " + column)
            c.commit()
        row = self.call("runtime.task.get", {"task_id": self.task["task_id"]})["data"]
        self.assertEqual(row["message"], self.task["message"])
        self.assertIsNone(row["supersedes_task_id"])

    def test_http_replacement_and_copy_proof_are_context_protected(self):
        self.call("runtime.task.execute", {"task_id": self.task["task_id"]})
        self.save("new office", 1)
        with TestClient(app.app, base_url="http://127.0.0.1:18770") as client:
            client.get("/workspace")
            headers = {"X-Jobkit-Context": self.context["context_token"]}
            base = "/api/runtime/tasks/" + self.task["task_id"]
            self.assertEqual(client.get(base + '/copy', headers=headers).json()["code"], "STALE_FACTS")
            response = client.post(base + '/replace', headers=headers, json={"expected_review_revision": 0, "expected_document_revision": 2})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(client.get(base + '/copy', headers=headers).json()["code"], "TASK_SUPERSEDED")
            self.assertEqual(client.get(base + '/copy', headers={"X-Jobkit-Context": "old"}).json()["code"], "STALE_CONTEXT")
        with TestClient(app.app, base_url="http://127.0.0.1:18770") as anonymous:
            self.assertEqual(anonymous.get(base + '/copy').status_code, 401)


if __name__ == "__main__":
    unittest.main()
