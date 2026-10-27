"""Scoped literal search and deterministic keyset pages over the existing tasks."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from fastapi.testclient import TestClient
import app
import main
import store
from sandbox import KernelError


class TaskSearchTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        patch = mock.patch.object(store, "DB_PATH", Path(temp.name) / "runtime.db")
        patch.start()
        self.addCleanup(patch.stop)
        self.context = main.dispatch("runtime.context")["data"]
        self.call("runtime.document.save", {"content": {"location": "fictional office", "source": "fixture"}, "confirmed": True, "expected_revision": 0})
        self.tasks = []
        for start, count in ((0, 50), (50, 15)):
            items = [{"conversation_note": "Candidate " + str(index), "message": "工作地点在哪里？"} for index in range(start, start + count)]
            self.tasks += self.call("runtime.task.batch.submit", {"items": items, "idempotency_key": str(start)})["data"]["tasks"]
        with store.runtime_db() as c:
            c.execute("UPDATE runtime_tasks SET created_at=100 WHERE kind='work'")

    def call(self, name, payload=None):
        return main.dispatch(name, payload, self.context["context_token"])

    def search(self, query="", review="pending", cursor=None):
        return self.call("runtime.task.search", {"query": query, "review": review, "cursor": cursor})["data"]

    def test_tied_timestamps_page_all_rows_once(self):
        first = self.search()
        second = self.search(cursor=first["next_cursor"])
        self.assertEqual((len(first["tasks"]), len(second["tasks"]), first["total"], second["total"]), (50, 15, 65, 65))
        self.assertIsNone(second["next_cursor"])
        ids = [t["task_id"] for t in first["tasks"] + second["tasks"]]
        self.assertEqual(ids, sorted([t["task_id"] for t in self.tasks], reverse=True))
        self.assertEqual(len(set(ids)), 65)

    def test_notes_original_text_and_literal_search(self):
        self.assertEqual(self.search("candidate 12")["total"], 1)
        self.assertEqual(self.search("工作地点")["total"], 65)
        self.assertEqual(self.search("fictional office")["total"], 0, "facts and draft bodies are outside this search")
        text = "100%_ ' OR 1=1 --"
        task = self.call("runtime.task.submit", {"tool": "draft.reply", "input": {"message": text}, "idempotency_key": "literal"})["data"]
        for query in ("%", "_", "' OR 1=1 --"):
            found = self.search(query)
            self.assertEqual(found["total"], 1)
            self.assertEqual(found["tasks"][0]["task_id"], task["task_id"])

    def test_newer_insert_does_not_shift_next_page(self):
        first = self.search()
        new = self.call("runtime.task.submit", {"tool": "draft.reply", "input": {"message": "新消息"}, "idempotency_key": "new"})["data"]
        second = self.search(cursor=first["next_cursor"])
        self.assertEqual(second["total"], 66)
        combined = first["tasks"] + second["tasks"]
        self.assertEqual(len({t["task_id"] for t in combined}), 65)
        self.assertNotIn(new["task_id"], [t["task_id"] for t in combined])
        self.assertEqual(self.search()["tasks"][0]["task_id"], new["task_id"])

    def test_cursor_record_can_leave_filter_without_breaking_next_page(self):
        first = self.search()
        boundary = first["next_cursor"]
        self.call("runtime.task.execute", {"task_id": boundary})
        self.call("runtime.task.review", {"task_id": boundary, "handled": True, "expected_review_revision": 0})
        second = self.search(cursor=boundary)
        self.assertEqual((len(second["tasks"]), second["total"]), (15, 64))
        self.assertEqual(self.search(review="handled")["total"], 1)
        self.assertEqual(self.search(review="all")["total"], 65)

    def test_invalid_input_and_nonexistent_cursor(self):
        for payload in ({"query": "x" * 121}, {"query": []}, {"review": "other"}, {"cursor": True}, {"cursor": "invalid"}, {"extra": "value"}):
            with self.assertRaises(KernelError) as error:
                self.call("runtime.task.search", payload)
            self.assertEqual(error.exception.code, "INVALID_SEARCH")
        with self.assertRaises(KernelError) as error:
            self.search(cursor="f" * 32)
        self.assertEqual(error.exception.code, "INVALID_SEARCH_CURSOR")

    def test_scope_and_stale_context_are_fenced(self):
        cursor = self.search()["next_cursor"]
        old = self.context["context_token"]
        self.context = self.call("runtime.switch", {"role": "applicant", "organization_id": "local", "account_id": "another"})["data"]
        self.assertEqual(self.search(review="all")["total"], 0)
        with self.assertRaises(KernelError) as error:
            self.search(cursor=cursor, review="all")
        self.assertEqual(error.exception.code, "INVALID_SEARCH_CURSOR")
        with self.assertRaises(KernelError) as error:
            main.dispatch("runtime.task.search", {}, old)
        self.assertEqual(error.exception.code, "STALE_CONTEXT")

    def test_malformed_saved_input_is_visible_but_does_not_crash_search(self):
        target = self.tasks[0]["task_id"]
        with store.runtime_db() as c:
            c.execute("UPDATE runtime_tasks SET payload='invalid json',conversation_note='broken fixture' WHERE task_id=?", (target,))
        result = self.search("broken fixture")
        self.assertEqual((result["total"], result["tasks"][0]["input_state"]), (1, "unreadable"))
        self.assertEqual(self.search("工作地点")["total"], 64)

    def test_document_projection_remains_server_owned(self):
        self.call("runtime.document.save", {"content": {"location": "new office", "source": "fixture"}, "confirmed": True, "expected_revision": 1})
        result = self.search()
        self.assertTrue(all(t["draft_stale"] and t["current_document_revision"] == 2 for t in result["tasks"]))

    def test_real_http_search_and_access_control(self):
        with TestClient(app.app, base_url="http://127.0.0.1:18770") as client:
            client.get("/workspace")
            headers = {"X-Jobkit-Context": self.context["context_token"]}
            response = client.post("/api/runtime/tasks/search", headers=headers, json={"query": "Candidate 12", "review": "pending"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["data"]["total"], 1)
            invalid = client.post("/api/runtime/tasks/search", headers=headers, json={"query": "x" * 121})
            self.assertEqual(invalid.status_code, 400)
            self.assertNotIn("x" * 121, invalid.text)
        with TestClient(app.app, base_url="http://127.0.0.1:18770") as client:
            self.assertEqual(client.post("/api/runtime/tasks/search", json={}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
