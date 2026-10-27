"""Identity fencing, deterministic tasks and traces; temporary DB, no platform access."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
import main
import store
from sandbox import KernelError
from tool_router import Tool


class RuntimeKernelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = mock.patch.object(store, "DB_PATH", Path(self.tmp.name) / "runtime.db")
        self.db_patch.start()
        self.context = self.call("runtime.context")

    def tearDown(self):
        self.db_patch.stop()
        self.tmp.cleanup()

    def call(self, tool, payload=None, token=None):
        token = token or (self.context["context_token"] if hasattr(self, "context") else None)
        return main.dispatch(tool, payload, token)["data"]

    def switch(self, role="recruiter", organization="example", account="hr-one"):
        self.context = self.call("runtime.switch", {"role": role, "organization_id": organization, "account_id": account})
        return self.context

    def save(self, values, revision=0):
        return self.call("runtime.document.save", {"content": values, "expected_revision": revision, "confirmed": True})

    def submit(self, message="工作地点在哪里？", key="request-one"):
        return self.call("runtime.task.submit", {"tool": "draft.reply", "input": {"message": message}, "idempotency_key": key})

    def test_recruiter_draft_is_grounded_and_not_sent(self):
        self.switch()
        self.save({"location": "示例园区三楼", "source": "HR已核对的岗位表"})
        task = self.submit()
        self.assertTrue(task["parent_task_id"])
        result = self.call("runtime.task.execute", {"task_id": task["task_id"]})
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["text"], "示例园区三楼")
        self.assertFalse(result["result"]["sent"])
        events = self.call("runtime.events")
        steps = [row["status"] for row in reversed(events) if row["task_id"] == task["task_id"]]
        self.assertEqual(steps, ["queued", "executing", "completed"])
        self.assertNotIn("示例园区", str(events))

    def test_unknown_and_unapproved_inputs_never_execute(self):
        with self.assertRaisesRegex(KernelError, "尚未注册"):
            self.call("boss.chat.send", {"message": "must-not-send"})
        events = self.call("runtime.events")
        self.assertTrue(any(row["code"] == "UNREGISTERED_TOOL" for row in events))
        self.switch()
        task = self.submit()
        result = self.call("runtime.task.execute", {"task_id": task["task_id"]})
        self.assertEqual(result["result"]["state"], "needs_review")
        self.assertEqual(result["result"]["text"], "")

    def test_role_and_account_documents_are_isolated(self):
        self.switch(role="applicant", organization="local", account="candidate-one")
        self.save({"city": "private-applicant-city"})
        self.switch()
        self.assertEqual(self.call("runtime.document.get")["content"], {})
        self.save({"location": "hr-one-location", "source": "verified document"})
        self.switch(account="hr-two")
        self.assertEqual(self.call("runtime.document.get")["content"], {})
        self.switch(account="hr-one")
        self.assertEqual(self.call("runtime.document.get")["content"]["location"], "hr-one-location")
        self.assertNotIn("private-applicant-city", str(self.call("runtime.task.list")))

    def test_switch_cancels_queued_tasks_and_rejects_old_tab(self):
        self.switch()
        task = self.submit()
        old = self.context.copy()
        self.switch(account="other")
        with self.assertRaises(KernelError):
            self.call("runtime.task.get", {"task_id": task["task_id"]})
        with self.assertRaisesRegex(KernelError, "身份已切换"):
            self.call("runtime.document.save", {"content": {}, "expected_revision": 0, "confirmed": True}, old["context_token"])
        self.switch(account="hr-one")
        self.assertEqual(self.call("runtime.task.get", {"task_id": task["task_id"]})["status"], "cancelled")

    def test_facts_changed_after_submit_prevent_old_draft(self):
        self.switch()
        self.save({"location": "old", "source": "approved"})
        task = self.submit()
        self.save({"location": "new", "source": "approved"}, revision=1)
        result = self.call("runtime.task.execute", {"task_id": task["task_id"]})
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error_code"], "STALE_FACTS")

    def test_idempotent_submit_and_completed_execute_do_not_repeat(self):
        self.switch()
        self.save({"location": "location", "source": "approved"})
        first, second = self.submit(), self.submit()
        self.assertEqual(first["task_id"], second["task_id"])
        task_id = first["task_id"]
        self.call("runtime.task.execute", {"task_id": task_id})
        self.call("runtime.task.execute", {"task_id": task_id})
        events = self.call("runtime.events")
        self.assertEqual(sum(row["status"] == "executing" and row["task_id"] == task_id for row in events), 1)
        with self.assertRaisesRegex(KernelError, "不同内容"):
            self.submit("工资是多少？")

    def test_document_cannot_be_saved_without_confirmation_or_with_stale_revision(self):
        self.switch()
        with self.assertRaises(KernelError):
            self.call("runtime.document.save", {"content": {"location": "x"}, "expected_revision": 0})
        self.save({"location": "one", "source": "approved"})
        with self.assertRaisesRegex(KernelError, "已更新"):
            self.save({"location": "two", "source": "approved"})

    def test_controls_cannot_be_submitted_as_background_work(self):
        with self.assertRaisesRegex(KernelError, "业务任务"):
            self.call("runtime.task.submit", {"tool": "runtime.switch", "input": {}, "idempotency_key": "unsafe"})

    def test_controls_cannot_be_cancelled_as_work(self):
        response = main.dispatch("runtime.document.get", {}, self.context["context_token"])
        with self.assertRaisesRegex(KernelError, "控制操作"):
            self.call("runtime.task.cancel", {"task_id": response["trace_id"]})

    def test_registered_external_tool_still_needs_sandbox_authorization(self):
        calls = []
        name = "test.external"
        main._router.register(Tool(name, lambda *args: calls.append(1), kind="work", effect="external_write"))
        self.addCleanup(lambda: main._router._tools.pop(name))
        task = main._engine.submit(name, {}, "external", self.context)
        with self.assertRaisesRegex(KernelError, "尚未验证"):
            main._engine.execute(task["task_id"], self.context["context_token"])
        self.assertEqual(calls, [])
        self.assertEqual(self.call("runtime.task.get", {"task_id": task["task_id"]})["status"], "blocked")

    def test_committed_control_fact_survives_switch_but_response_is_fenced(self):
        ready, release = threading.Event(), threading.Event()
        old = self.context.copy()
        def committed(payload, context):
            with store.runtime_db() as c:
                c.execute("INSERT INTO runtime_documents VALUES (?,?,?,?)",
                          (context["scope_id"], "test", 1, '{"text":"private-committed-fact"}'))
            ready.set()
            if not release.wait(3):
                raise RuntimeError("test did not release worker")
            return {"text": "private-committed-fact"}
        name = "test.committed"
        main._router.register(Tool(name, committed))
        self.addCleanup(lambda: main._router._tools.pop(name))
        outcomes = []
        def execute():
            try:
                main._engine.dispatch(name, {}, old["context_token"])
            except KernelError as exc:
                outcomes.append((exc.code, exc.trace_id))
        worker = threading.Thread(target=execute)
        worker.start()
        try:
            self.assertTrue(ready.wait(3))
            self.switch()
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcomes[0][0], "STALE_CONTEXT")
        self.switch(old["role"], old["organization_id"], old["account_id"])
        task = self.call("runtime.task.get", {"task_id": outcomes[0][1]})
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["result"]["text"], "private-committed-fact")

    def test_inflight_pure_result_is_discarded_after_scope_switch(self):
        ready, release = threading.Event(), threading.Event()
        def slow(payload, context):
            ready.set()
            if not release.wait(3):
                raise RuntimeError("test did not release worker")
            return {"text": "old-private-result"}
        name = "test.slow"
        main._router.register(Tool(name, slow, kind="work", effect="none"))
        self.addCleanup(lambda: main._router._tools.pop(name))
        task = main._engine.submit(name, {}, "slow", self.context)
        old_token = self.context["context_token"]
        outcomes = []
        def execute():
            try:
                outcomes.append(main._engine.execute(task["task_id"], old_token))
            except KernelError as exc:
                outcomes.append(exc.code)
        worker = threading.Thread(target=execute)
        worker.start()
        try:
            self.assertTrue(ready.wait(3))
            self.switch()
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcomes, ["STALE_CONTEXT"])
