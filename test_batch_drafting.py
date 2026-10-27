"""Bounded manual batches reuse the one executor and never send or replay."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from fastapi.testclient import TestClient
import app
import main
import store
import recruiter_reply
from sandbox import KernelError


class BatchDraftingTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        patch = mock.patch.object(store, "DB_PATH", Path(temp.name) / "runtime.db")
        patch.start()
        self.addCleanup(patch.stop)
        self.context = main.dispatch("runtime.context")["data"]
        self.save(0)
        self.tasks = self.call("runtime.task.batch.submit", {"items": [
            {"conversation_note": "A", "message": "工作地点在哪里？"},
            {"conversation_note": "B", "message": "薪资范围是多少？"},
            {"conversation_note": "C", "message": "有年龄限制吗？"}], "idempotency_key": "fixture"})["data"]["tasks"]
        self.ids = [t["task_id"] for t in self.tasks]

    def call(self, name, payload=None):
        return main.dispatch(name, payload, self.context["context_token"])

    def save(self, revision):
        return self.call("runtime.document.save", {"content": {"location": "fictional office", "salary": "approved salary", "source": "fixture"}, "confirmed": True, "expected_revision": revision})

    def run_batch(self, ids=None, revision=1):
        return self.call("runtime.task.batch.execute", {"task_ids": self.ids if ids is None else ids, "expected_document_revision": revision})

    def statuses(self):
        return [self.call("runtime.task.get", {"task_id": t})["data"]["status"] for t in self.ids]

    def test_drafts_and_manual_review_share_existing_executor_and_traces(self):
        response = self.run_batch()
        result = response["data"]
        self.assertEqual((result["drafted"], result["needs_review"], result["failed"]), (2, 1, 0))
        self.assertEqual(result["remaining_task_ids"], [])
        self.assertIsNone(result["stopped_code"])
        self.assertEqual([t["task_id"] for t in result["tasks"]], self.ids)
        self.assertTrue(all(t["result"]["sent"] is False for t in result["tasks"]))
        with store.runtime_db() as c:
            for task_id in self.ids:
                self.assertEqual(c.execute("SELECT count(*) FROM runtime_events WHERE task_id=? AND status='executing'", (task_id,)).fetchone()[0], 1)

    def test_entire_selection_preflight_rejects_without_generating(self):
        cases = [[], self.ids * 4, [self.ids[0]] * 2, [self.ids[0], "f" * 32], [self.ids[0], True]]
        with mock.patch("recruiter_reply.draft", side_effect=AssertionError("must not generate")):
            for ids in cases:
                with self.assertRaises(KernelError):
                    self.run_batch(ids)
        self.assertEqual(self.statuses(), ["queued"] * 3)

    def test_stale_version_and_nonqueued_member_reject_whole_selection(self):
        self.call("runtime.task.execute", {"task_id": self.ids[1]})
        with mock.patch("recruiter_reply.draft", side_effect=AssertionError("must not generate")):
            with self.assertRaises(KernelError):
                self.run_batch()
        self.assertEqual(self.statuses(), ["queued", "completed", "queued"])
        self.save(1)
        with self.assertRaises(KernelError) as error:
            self.run_batch([self.ids[0]], revision=1)
        self.assertEqual(error.exception.code, "STALE_FACTS")
        with self.assertRaises(KernelError):
            self.run_batch([self.ids[0]], revision=2)

    def test_changed_document_stops_after_first_terminal_result(self):
        draft = recruiter_reply.draft
        def change(*args):
            self.save(1)
            return draft(*args)
        with mock.patch("recruiter_reply.draft", side_effect=change) as handler:
            result = self.run_batch()["data"]
        self.assertEqual(handler.call_count, 1)
        self.assertEqual(result["stopped_code"], "STALE_FACTS")
        self.assertEqual(result["remaining_task_ids"], self.ids[1:])
        self.assertEqual(self.statuses(), ["blocked", "queued", "queued"])
        self.assertEqual(result["drafted"], 0)

    def test_tool_failure_preserves_prior_draft_and_stops_remaining(self):
        draft = recruiter_reply.draft
        calls = []
        def fail(*args):
            calls.append(1)
            if len(calls) == 2:
                raise ValueError("fixture private error")
            return draft(*args)
        with mock.patch("recruiter_reply.draft", side_effect=fail):
            result = self.run_batch()["data"]
        self.assertEqual((result["drafted"], result["failed"], result["stopped_code"]), (1, 1, "TOOL_FAILED"))
        self.assertEqual(result["remaining_task_ids"], self.ids[2:])
        self.assertEqual(self.statuses(), ["completed", "failed", "queued"])

    def test_document_update_between_children_preserves_old_result_but_blocks_copy(self):
        execute = main._engine.execute
        def between(task_id, *args, **kwargs):
            result = execute(task_id, *args, **kwargs)
            if task_id == self.ids[0]:
                self.save(1)
            return result
        with mock.patch.object(main._engine, "execute", side_effect=between):
            result = self.run_batch()["data"]
        self.assertEqual(result["stopped_code"], "STALE_FACTS")
        self.assertEqual((result["drafted"], result["remaining_task_ids"]), (1, self.ids[1:]))
        self.assertEqual(self.statuses(), ["completed", "queued", "queued"])
        with self.assertRaises(KernelError) as error:
            self.call("runtime.task.copy.get", {"task_id": self.ids[0]})
        self.assertEqual(error.exception.code, "STALE_FACTS")

    def test_context_change_fences_response_and_stops_other_children(self):
        draft = recruiter_reply.draft
        def switch(*args):
            self.call("runtime.switch", {"role": "applicant", "organization_id": "local", "account_id": "different"})
            return draft(*args)
        with mock.patch("recruiter_reply.draft", side_effect=switch) as handler:
            with self.assertRaises(KernelError) as error:
                self.run_batch()
        self.assertEqual(error.exception.code, "STALE_CONTEXT")
        self.assertEqual(handler.call_count, 1)
        with store.runtime_db() as c:
            parent = c.execute("SELECT * FROM runtime_tasks WHERE tool='runtime.task.batch.execute'").fetchone()
            self.assertEqual(parent["status"], "completed")
            self.assertIsNotNone(parent["commit_receipt"])

    def test_aggregate_receipt_recovers_without_replaying_children(self):
        response = self.run_batch()
        with store.runtime_db() as c:
            c.execute("UPDATE runtime_tasks SET status='executing',result=NULL WHERE task_id=?", (response["trace_id"],))
        with mock.patch("recruiter_reply.draft", side_effect=AssertionError("no replay")):
            recovered = self.call("runtime.task.recover", {"task_id": response["trace_id"]})["data"]
            self.assertEqual(recovered["result"], response["data"])
            self.assertEqual(recovered["error_code"], "RECOVERED_COMMIT")
            with self.assertRaises(KernelError):
                self.run_batch()

    def test_http_static_route_auth_and_review_route(self):
        with TestClient(app.app, base_url="http://127.0.0.1:18770") as client:
            client.get("/workspace")
            headers = {"X-Jobkit-Context": self.context["context_token"]}
            response = client.post("/api/runtime/tasks/batch/execute", headers=headers, json={"task_ids": self.ids, "expected_document_revision": 1})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["data"]["drafted"], 2)
            review = client.post("/api/runtime/tasks/" + self.ids[0] + "/review", headers=headers, json={"handled": True, "expected_review_revision": 0})
            self.assertEqual(review.status_code, 200, review.text)
        with TestClient(app.app, base_url="http://127.0.0.1:18770") as client:
            self.assertEqual(client.post("/api/runtime/tasks/batch/execute", json={}).status_code, 401)

    def test_real_process_exit_preserves_completed_child_without_automatic_resume(self):
        control = main._engine.submit("runtime.task.batch.execute", {"task_ids": self.ids, "expected_document_revision": 1}, "crash", self.context, internal=True)
        script = '''
import os, sys
from pathlib import Path
import store
store.DB_PATH = Path(sys.argv[1])
import main
original = main._engine.execute
def checkpoint(task_id, *args, **kwargs):
    result = original(task_id, *args, **kwargs)
    if task_id == sys.argv[4]:
        os._exit(21)
    return result
main._engine.execute = checkpoint
main._engine.execute(sys.argv[2], sys.argv[3])
'''
        child = subprocess.run([sys.executable, "-c", script, str(store.DB_PATH), control["task_id"], self.context["context_token"], self.ids[0]], cwd=Path(__file__).parent, capture_output=True, timeout=15)
        self.assertEqual(child.returncode, 21, child.stderr)
        self.assertEqual(self.statuses(), ["completed", "queued", "queued"])
        with mock.patch("recruiter_reply.draft", side_effect=AssertionError("must not resume")):
            recovered = self.call("runtime.task.recover", {"task_id": control["task_id"]})["data"]
        self.assertEqual((recovered["status"], recovered["error_code"]), ("outcome_unknown", "OUTCOME_UNKNOWN"))
        self.assertEqual(self.statuses(), ["completed", "queued", "queued"])
        result = self.run_batch(self.ids[1:])["data"]
        self.assertEqual(result["remaining_task_ids"], [])

    def test_foreign_scope_member_blocks_all_children(self):
        original_context = self.context
        self.context = self.call("runtime.switch", {"role": "applicant", "organization_id": "local", "account_id": "other"})["data"]
        foreign = self.call("runtime.task.submit", {"tool": "draft.reply", "input": {"message": "地点？"}, "idempotency_key": "foreign"})["data"]
        self.context = self.call("runtime.switch", {"role": original_context["role"], "organization_id": original_context["organization_id"], "account_id": original_context["account_id"]})["data"]
        # Switching cancels old queued work; use fresh source-scope tasks.
        own = self.call("runtime.task.submit", {"tool": "draft.reply", "input": {"message": "地点？"}, "idempotency_key": "own"})["data"]
        with mock.patch("recruiter_reply.draft", side_effect=AssertionError("must not generate")):
            with self.assertRaises(KernelError) as error:
                self.run_batch([own["task_id"], foreign["task_id"]])
        self.assertEqual(error.exception.code, "TASK_NOT_FOUND")
        self.assertEqual(self.call("runtime.task.get", {"task_id": own["task_id"]})["data"]["status"], "queued")


if __name__ == "__main__":
    unittest.main()
