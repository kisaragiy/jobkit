"""Local business-record export is scoped, complete, bounded, and never replayed."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from fastapi.testclient import TestClient
import app
import main
import runtime_tools
import store
from sandbox import KernelError


class WorkspaceExportTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        patch = mock.patch.object(store, "DB_PATH", Path(temp.name) / "runtime.db")
        patch.start()
        self.addCleanup(patch.stop)
        self.context = main.dispatch("runtime.context")["data"]
        self.call("runtime.document.save", {"content": {"location": "fictional office", "source": "fixture"}, "confirmed": True, "expected_revision": 0})
        self.tasks = self.call("runtime.task.batch.submit", {"items": [
            {"conversation_note": "A", "message": "地点在哪里？"},
            {"conversation_note": "B", "message": "能保证录用吗？"},
            {"conversation_note": "C", "message": "地点在哪里？"}], "idempotency_key": "fixture"})["data"]["tasks"]
        for index in (0, 1):
            self.call("runtime.task.execute", {"task_id": self.tasks[index]["task_id"]})
        for revision in range(2):
            self.edit(revision)

    def call(self, name, payload=None):
        return main.dispatch(name, payload, self.context["context_token"])

    def edit(self, revision):
        return self.call("runtime.task.edit", {"task_id": self.tasks[0]["task_id"], "text": "人工版本 " + str(revision + 1), "confirmed": True,
                                              "expected_manual_revision": revision, "expected_document_revision": 1})

    def export(self):
        return self.call("runtime.workspace.export", {})["data"]

    def test_preserves_original_human_versions_and_explicit_limits(self):
        with mock.patch("recruiter_reply.draft", side_effect=AssertionError("export must not execute")):
            snapshot = self.export()
        self.assertEqual(snapshot["counts"], {"tasks": 3, "draft_edits": 2})
        task = next(t for t in snapshot["tasks"] if t["task_id"] == self.tasks[0]["task_id"])
        self.assertEqual((task["result"]["text"], task["manual_text"], task["manual_revision"]), ("fictional office", "人工版本 2", 2))
        self.assertFalse(task["result"]["sent"])
        self.assertFalse(snapshot["restore_supported"])
        self.assertFalse(snapshot["platform_send_evidence"])
        self.assertEqual(json.loads(json.dumps(snapshot, ensure_ascii=False)), snapshot)
        text = json.dumps(snapshot)
        for excluded in ("context_token", "commit_receipt", "idempotency_key", "control_task_id", "parent_task_id", self.context["context_token"]):
            self.assertNotIn(excluded, text)

    def test_all_pages_and_more_than_twenty_edits_are_included(self):
        for start, count in ((0, 50), (50, 7)):
            self.call("runtime.task.batch.submit", {"items": [{"conversation_note": "many", "message": "地点？"}] * count, "idempotency_key": str(start)})
        for revision in range(2, 21):
            self.edit(revision)
        snapshot = self.export()
        self.assertEqual(snapshot["counts"], {"tasks": 60, "draft_edits": 21})
        self.assertEqual(len({task["task_id"] for task in snapshot["tasks"]}), 60)
        self.assertEqual([edit["revision"] for edit in snapshot["draft_edits"]], list(range(1, 22)))

    def test_other_scope_and_control_requests_are_excluded(self):
        self.context = self.call("runtime.switch", {"role": "applicant", "organization_id": "local", "account_id": "another"})["data"]
        self.call("runtime.document.save", {"content": {"city": "fictional city"}, "confirmed": True, "expected_revision": 0})
        snapshot = self.export()
        self.assertEqual(snapshot["workspace"]["role"], "applicant")
        self.assertEqual(snapshot["document"]["kind"], "profile")
        self.assertEqual(snapshot["counts"], {"tasks": 0, "draft_edits": 0})
        self.assertNotIn("fictional office", json.dumps(snapshot))

    def test_extra_scope_path_and_filter_parameters_are_rejected(self):
        for payload in ({"scope_id": "another"}, {"path": "C:/elsewhere"}, {"review": "pending"}, {"query": "A"}):
            with self.assertRaises(KernelError) as error:
                self.call("runtime.workspace.export", payload)
            self.assertEqual(error.exception.code, "INVALID_EXPORT")

    def test_unreadable_input_or_result_refuses_entire_export(self):
        target = self.tasks[0]["task_id"]
        for column in ("payload", "result"):
            with store.runtime_db() as c:
                original = c.execute("SELECT " + column + " FROM runtime_tasks WHERE task_id=?", (target,)).fetchone()[0]
                c.execute("UPDATE runtime_tasks SET " + column + "='invalid json' WHERE task_id=?", (target,))
            with self.assertRaises(KernelError) as error:
                self.export()
            self.assertEqual(error.exception.code, "INVALID_EXPORT_STATE")
            with store.runtime_db() as c:
                c.execute("UPDATE runtime_tasks SET " + column + "=? WHERE task_id=?", (original, target))

    def test_inconsistent_manual_history_refuses_export(self):
        with store.runtime_db() as c:
            c.execute("UPDATE runtime_tasks SET manual_text='unexpected' WHERE task_id=?", (self.tasks[0]["task_id"],))
        with self.assertRaises(KernelError) as error:
            self.export()
        self.assertEqual(error.exception.code, "INVALID_MANUAL_DRAFT_STATE")

    def test_count_and_serialized_byte_limits_never_truncate(self):
        snapshot = self.export()
        serialized = len(json.dumps(snapshot, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8"))
        for name, limit in (("EXPORT_MAX_TASKS", 2), ("EXPORT_MAX_EDITS", 1), ("EXPORT_MAX_BYTES", serialized - 100)):
            with mock.patch.object(runtime_tools, name, limit):
                with self.assertRaises(KernelError) as error:
                    self.export()
                self.assertEqual((error.exception.code, error.exception.status), ("EXPORT_TOO_LARGE", 413))

    def test_inflight_and_unknown_states_are_preserved_without_recovery(self):
        target = self.tasks[2]["task_id"]
        for status in ("executing", "outcome_unknown"):
            with store.runtime_db() as c:
                c.execute("UPDATE runtime_tasks SET status=? WHERE task_id=?", (status, target))
            snapshot = self.export()
            self.assertEqual(next(t for t in snapshot["tasks"] if t["task_id"] == target)["status"], status)
            self.assertEqual(self.call("runtime.task.get", {"task_id": target})["data"]["status"], status)

    def test_role_switch_during_export_fences_response(self):
        handler = runtime_tools.workspace_export
        def switch(payload, context):
            result = handler(payload, context)
            self.call("runtime.switch", {"role": "applicant", "organization_id": "local", "account_id": "new"})
            return result
        from dataclasses import replace
        original = main._router.resolve("runtime.workspace.export")
        with mock.patch.dict(main._router._tools, {"runtime.workspace.export": replace(original, handler=switch)}):
            with self.assertRaises(KernelError) as error:
                self.export()
        self.assertEqual(error.exception.code, "STALE_CONTEXT")

    def test_http_export_access_control_and_scoped_content(self):
        with TestClient(app.app, base_url="http://127.0.0.1:18770") as client:
            client.get("/workspace")
            headers = {"X-Jobkit-Context": self.context["context_token"]}
            response = client.post("/api/runtime/workspace/export", headers=headers, json={})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["data"]["counts"]["tasks"], 3)
            self.assertNotIn(self.context["context_token"], response.text)
        with TestClient(app.app, base_url="http://127.0.0.1:18770") as client:
            self.assertEqual(client.post("/api/runtime/workspace/export", json={}).status_code, 401)

    def test_missing_old_manual_version_refuses_export_even_when_latest_matches(self):
        with store.runtime_db() as c:
            c.execute("DELETE FROM runtime_draft_edits WHERE task_id=? AND revision=1", (self.tasks[0]["task_id"],))
        with self.assertRaises(KernelError) as error:
            self.export()
        self.assertEqual(error.exception.code, "INVALID_EXPORT_STATE")

    def test_old_manual_version_cannot_reference_a_different_document(self):
        with store.runtime_db() as c:
            c.execute("UPDATE runtime_draft_edits SET document_revision=9 WHERE task_id=? AND revision=1", (self.tasks[0]["task_id"],))
        with self.assertRaises(KernelError) as error:
            self.export()
        self.assertEqual(error.exception.code, "INVALID_EXPORT_STATE")

    def test_broken_replacement_link_refuses_whole_export(self):
        with store.runtime_db() as c:
            c.execute("UPDATE runtime_tasks SET review_status='superseded',superseded_by_task_id=? WHERE task_id=?", ("f" * 32, self.tasks[0]["task_id"]))
        with self.assertRaises(KernelError) as error:
            self.export()
        self.assertEqual(error.exception.code, "INVALID_EXPORT_STATE")

    def test_valid_replacement_chain_preserves_manual_history(self):
        self.call("runtime.document.save", {"content": {"location": "new office", "source": "fixture"}, "confirmed": True, "expected_revision": 1})
        successor = self.call("runtime.task.replace", {"task_id": self.tasks[0]["task_id"], "expected_review_revision": 0, "expected_document_revision": 2})["data"]
        snapshot = self.export()
        self.assertEqual(snapshot["counts"], {"tasks": 4, "draft_edits": 2})
        self.assertIn(successor["task_id"], [task["task_id"] for task in snapshot["tasks"]])
        with store.runtime_db() as c:
            c.execute("UPDATE runtime_tasks SET supersedes_task_id=NULL WHERE task_id=?", (successor["task_id"],))
        with self.assertRaises(KernelError):
            self.export()

    def test_self_link_and_nonincreasing_document_cycle_are_rejected(self):
        from workspace_records import validate_record_graph
        from copy import deepcopy
        snapshot = self.export()
        tasks = deepcopy(snapshot["tasks"])
        target = tasks[0]
        target.update(review_status="superseded", superseded_by_task_id=target["task_id"], supersedes_task_id=target["task_id"])
        with self.assertRaises(KernelError):
            validate_record_graph(tasks, snapshot["draft_edits"])
        first, second = tasks[:2]
        first.update(superseded_by_task_id=second["task_id"], supersedes_task_id=second["task_id"])
        second.update(review_status="superseded", superseded_by_task_id=first["task_id"], supersedes_task_id=first["task_id"])
        with self.assertRaises(KernelError):
            validate_record_graph(tasks, snapshot["draft_edits"])

    def test_duplicate_versions_and_invalid_earlier_text_are_rejected(self):
        from workspace_records import validate_record_graph
        from copy import deepcopy
        snapshot = self.export()
        with self.assertRaises(KernelError):
            validate_record_graph(snapshot["tasks"], snapshot["draft_edits"] + snapshot["draft_edits"][:1])
        edits = deepcopy(snapshot["draft_edits"])
        edits[0]["text"] = " "
        with self.assertRaises(KernelError):
            validate_record_graph(snapshot["tasks"], edits)

    def test_invalid_graph_is_not_repaired_and_does_not_return_partial_http_data(self):
        with store.runtime_db() as c:
            c.execute("UPDATE runtime_draft_edits SET text='' WHERE task_id=? AND revision=1", (self.tasks[0]["task_id"],))
        with TestClient(app.app, base_url="http://127.0.0.1:18770") as client:
            client.get("/workspace")
            response = client.post("/api/runtime/workspace/export", headers={"X-Jobkit-Context": self.context["context_token"]}, json={})
            self.assertEqual(response.json()["code"], "INVALID_EXPORT_STATE")
            self.assertNotIn("fictional office", response.text)
        with store.runtime_db() as c:
            self.assertEqual(c.execute("SELECT text FROM runtime_draft_edits WHERE task_id=? AND revision=1", (self.tasks[0]["task_id"],)).fetchone()[0], "")


if __name__ == "__main__":
    unittest.main()
