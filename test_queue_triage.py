"""Business queue projections must not equate kernel completion with a usable reply."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from fastapi.testclient import TestClient
import app
import main
import store
from sandbox import KernelError


class QueueTriageTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        patch = mock.patch.object(store, "DB_PATH", Path(temp.name) / "runtime.db")
        patch.start()
        self.addCleanup(patch.stop)
        self.context = main.dispatch("runtime.context")["data"]
        self.save(0)
        self.tasks = self.call("runtime.task.batch.submit", {"items": [
            {"conversation_note": "queued", "message": "工作地点在哪里？"},
            {"conversation_note": "drafted", "message": "工作地点在哪里？"},
            {"conversation_note": "needs human", "message": "能保证录用吗？"},
            {"conversation_note": "cancelled", "message": "工作地点在哪里？"},
            {"conversation_note": "running", "message": "工作地点在哪里？"}], "idempotency_key": "fixture"})["data"]["tasks"]
        for index in (1, 2):
            self.call("runtime.task.execute", {"task_id": self.tasks[index]["task_id"]})
        self.call("runtime.task.cancel", {"task_id": self.tasks[3]["task_id"]})
        with store.runtime_db() as c:
            c.execute("UPDATE runtime_tasks SET status='executing' WHERE task_id=?", (self.tasks[4]["task_id"],))

    def call(self, name, payload=None):
        return main.dispatch(name, payload, self.context["context_token"])

    def save(self, revision):
        self.call("runtime.document.save", {"content": {"location": "fictional office", "source": "fixture"}, "confirmed": True, "expected_revision": revision})

    def search(self, stage="all", **payload):
        return self.call("runtime.task.search", {"stage": stage, **payload})["data"]

    def test_counts_partition_existing_states_and_stage_filters(self):
        result = self.search()
        self.assertEqual(result["stage_counts"], {"queued": 1, "drafted": 1, "needs_review": 1, "attention": 1, "running": 1, "history": 0})
        self.assertEqual(sum(result["stage_counts"].values()), result["total"])
        for stage, count in result["stage_counts"].items():
            page = self.search(stage)
            self.assertEqual((page["total"], len(page["tasks"])), (count, count))
            self.assertTrue(all(t["queue_stage"] == stage for t in page["tasks"]))
            self.assertEqual(page["stage_counts"], result["stage_counts"], "summary is independent of selected stage")

    def test_sensitive_question_is_completed_work_but_not_a_copyable_draft(self):
        task = self.search("needs_review")["tasks"][0]
        self.assertEqual((task["status"], task["review_status"], task["result"]["text"]), ("completed", "pending", ""))
        self.assertFalse(task["result"]["sent"])
        with self.assertRaises(KernelError):
            self.call("runtime.task.copy.get", {"task_id": task["task_id"]})
        with self.assertRaises(KernelError):
            self.call("runtime.task.edit", {"task_id": task["task_id"], "text": "guess", "confirmed": True, "expected_manual_revision": 0, "expected_document_revision": 1})

    def test_manual_edit_and_handled_progress_are_separate_from_draft_stage(self):
        task_id = self.tasks[1]["task_id"]
        self.call("runtime.task.edit", {"task_id": task_id, "text": "human confirmed text", "confirmed": True, "expected_manual_revision": 0, "expected_document_revision": 1})
        self.assertEqual(self.search("drafted")["tasks"][0]["review_status"], "pending")
        self.call("runtime.task.review", {"task_id": task_id, "handled": True, "expected_review_revision": 0})
        self.assertEqual(self.search("drafted", review="pending")["total"], 0)
        self.assertEqual(self.search("drafted", review="handled")["total"], 1)

    def test_updated_document_moves_current_work_to_attention_and_history_stays_distinct(self):
        self.save(1)
        self.assertEqual(self.search("attention")["total"], 5)
        self.assertEqual(self.search("drafted")["total"], 0)
        self.call("runtime.task.replace", {"task_id": self.tasks[1]["task_id"], "expected_review_revision": 0, "expected_document_revision": 2})
        result = self.search(review="all")
        self.assertEqual((result["stage_counts"]["history"], result["stage_counts"]["queued"], result["stage_counts"]["attention"]), (1, 1, 4))

    def test_invalid_saved_results_are_visible_in_attention_without_fallback(self):
        target = self.tasks[1]["task_id"]
        for result in ("invalid json", "[]", "null", json.dumps({"text": 5, "document_revision": 1})):
            with store.runtime_db() as c:
                c.execute("UPDATE runtime_tasks SET result=? WHERE task_id=?", (result, target))
            page = self.search("attention")
            self.assertIn(target, [task["task_id"] for task in page["tasks"]])
            self.assertEqual(page["total"], 2)
            self.assertEqual(self.search("drafted")["total"], 0)
            with self.assertRaises(KernelError):
                self.call("runtime.task.copy.get", {"task_id": target})
            with self.assertRaises(KernelError):
                self.call("runtime.task.edit", {"task_id": target, "text": "must not replace corrupt output", "confirmed": True,
                                               "expected_manual_revision": 0, "expected_document_revision": 1})

    def test_counts_follow_scope_query_and_review(self):
        result = self.search(query="needs human")
        self.assertEqual((result["total"], result["stage_counts"]["needs_review"]), (1, 1))
        self.context = self.call("runtime.switch", {"role": "applicant", "organization_id": "local", "account_id": "another"})["data"]
        empty = self.search(review="all")
        self.assertEqual((empty["total"], sum(empty["stage_counts"].values())), (0, 0))

    def test_stage_pagination_retains_all_matching_records(self):
        tasks = []
        for count, key in ((50, "many1"), (7, "many2")):
            tasks += self.call("runtime.task.batch.submit", {"items": [{"conversation_note": "many", "message": "地点？"}] * count, "idempotency_key": key})["data"]["tasks"]
        first = self.search("queued", query="many")
        second = self.search("queued", query="many", cursor=first["next_cursor"])
        self.assertEqual((len(first["tasks"]), len(second["tasks"]), second["total"]), (50, 7, 57))
        self.assertEqual(len({t["task_id"] for t in first["tasks"] + second["tasks"]}), 57)

    def test_invalid_stage_rejected_and_no_document_is_attention(self):
        for stage in ([], True, "unknown"):
            with self.assertRaises(KernelError) as error:
                self.search(stage)
            self.assertEqual(error.exception.code, "INVALID_SEARCH")
        with store.runtime_db() as c:
            c.execute("DELETE FROM runtime_documents WHERE scope_id=?", (self.context["scope_id"],))
        self.assertEqual(self.search("queued")["total"], 0)
        self.assertEqual(self.search("attention")["total"], 5)

    def test_http_stage_filter_and_business_counts(self):
        with TestClient(app.app, base_url="http://127.0.0.1:18770") as client:
            client.get("/workspace")
            headers = {"X-Jobkit-Context": self.context["context_token"]}
            result = client.post("/api/runtime/tasks/search", headers=headers, json={"stage": "needs_review", "review": "pending"})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["data"]["tasks"][0]["queue_stage"], "needs_review")
            self.assertEqual(result.json()["data"]["stage_counts"]["drafted"], 1)


if __name__ == "__main__":
    unittest.main()
