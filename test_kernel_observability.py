"""Rejected and failed operations remain attributable without leaking inputs."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import main
import store
from sandbox import KernelError
from tool_router import Tool, ToolRouter


class KernelObservabilityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(store, "DB_PATH", Path(self.tmp.name) / "runtime.db")
        patch.start()
        self.addCleanup(patch.stop)
        self.context = main.dispatch("runtime.context")["data"]

    def call(self, name, payload=None, token=None):
        return main.dispatch(name, payload, token or self.context["context_token"])["data"]

    def trace_rows(self, trace_id):
        with store.runtime_db() as c:
            return [dict(row) for row in c.execute("SELECT * FROM runtime_events WHERE task_id=?", (trace_id,))]

    def register(self, name, handler, effect="none"):
        main._router.register(Tool(name, handler, kind="work", effect=effect))
        self.addCleanup(lambda: main._router._tools.pop(name))
        return main._engine.submit(name, {}, name, self.context)["task_id"]

    def test_preexecution_refusals_have_private_event_only_traces(self):
        cycle = {}
        cycle["private-cycle"] = cycle
        cases = [("private-credential-do-not-log", {"private": "body"}, "UNREGISTERED_TOOL"),
                 ("runtime.document.get", ["private-body"], "INVALID_INPUT"),
                 ("runtime.document.get", {"private": object()}, "INVALID_INPUT"),
                 ("runtime.document.get", {"private": float("nan")}, "INVALID_INPUT"),
                 ("runtime.document.get", cycle, "INVALID_INPUT")]
        for name, payload, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(KernelError) as error:
                    self.call(name, payload)
                trace_id = error.exception.trace_id
                rows = self.trace_rows(trace_id)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["code"], code)
                self.assertNotIn("private", str(rows))
                with store.runtime_db() as c:
                    self.assertIsNone(c.execute("SELECT * FROM runtime_tasks WHERE task_id=?", (trace_id,)).fetchone())

    def test_stale_context_and_submit_race_refusals_have_traces(self):
        with self.assertRaises(KernelError) as error:
            self.call("runtime.document.get", token="old-token")
        self.assertEqual(self.trace_rows(error.exception.trace_id)[0]["code"], "STALE_CONTEXT")
        with mock.patch.object(main._engine, "submit", side_effect=KernelError("STALE_CONTEXT", "身份已切换")):
            with self.assertRaises(KernelError) as error:
                self.call("runtime.document.get")
        self.assertEqual(self.trace_rows(error.exception.trace_id)[0]["code"], "STALE_CONTEXT")

    def test_invalid_results_are_terminal_and_do_not_repeat(self):
        cycle = []
        cycle.append(cycle)
        for index, result in enumerate(({"value": object()}, {"value": float("nan")}, cycle)):
            with self.subTest(index=index):
                handler = mock.Mock(return_value=result)
                task_id = self.register("test.result" + str(index), handler)
                with self.assertRaises(KernelError) as error:
                    self.call("runtime.task.execute", {"task_id": task_id})
                self.assertEqual(error.exception.code, "INVALID_TOOL_RESULT")
                done = self.call("runtime.task.get", {"task_id": task_id})
                self.assertEqual((done["status"], done["error_code"], done["result"]), ("failed", "INVALID_TOOL_RESULT", None))
                self.assertEqual(self.call("runtime.task.execute", {"task_id": task_id}), done)
                handler.assert_called_once()
                self.assertTrue(any(e["status"] == "failed" for e in self.trace_rows(task_id)))

    def test_unexpected_exception_logs_only_type_and_trace(self):
        task_id = self.register("test.privatefailure", mock.Mock(side_effect=RuntimeError("private-candidate-message")))
        with self.assertLogs("execution_engine", level="ERROR") as logs:
            with self.assertRaises(KernelError) as error:
                self.call("runtime.task.execute", {"task_id": task_id})
        self.assertEqual(error.exception.code, "TOOL_FAILED")
        self.assertIn(task_id, str(logs.output))
        self.assertIn("RuntimeError", str(logs.output))
        self.assertNotIn("private-candidate-message", str(logs.output))
        self.assertNotIn("Traceback", str(logs.output))
        self.assertEqual(self.call("runtime.task.get", {"task_id": task_id})["status"], "failed")

    def test_mutating_tool_failure_keeps_result_unknown(self):
        def committed(payload, context):
            with store.runtime_db() as c:
                c.execute("INSERT INTO runtime_documents VALUES (?,?,?,?)", (context["scope_id"], "fixture", 1, '{}'))
            return {"invalid": object()}
        task_id = self.register("test.committedresult", committed, effect="state")
        with self.assertRaises(KernelError):
            self.call("runtime.task.execute", {"task_id": task_id})
        done = self.call("runtime.task.get", {"task_id": task_id})
        self.assertEqual(done["status"], "outcome_unknown")
        self.assertEqual(self.call("runtime.task.execute", {"task_id": task_id}), done)
        with store.runtime_db() as c:
            self.assertEqual(c.execute("SELECT revision FROM runtime_documents WHERE kind='fixture'").fetchone()[0], 1)

    def test_control_uncertainty_survives_context_switch(self):
        old = self.context.copy()
        def committed(payload, context):
            with store.runtime_db() as c:
                c.execute("INSERT INTO runtime_documents VALUES (?,?,?,?)", (context["scope_id"], "fixture", 1, '{}'))
            self.context = self.call("runtime.switch", {"role": "recruiter", "organization_id": "other", "account_id": "other"})
            return {"invalid": object()}
        name = "test.controluncertainty"
        main._router.register(Tool(name, committed))
        self.addCleanup(lambda: main._router._tools.pop(name))
        with self.assertRaises(KernelError) as error:
            self.call(name)
        self.assertEqual(error.exception.code, "STALE_CONTEXT")
        self.context = self.call("runtime.switch", {"role": old["role"], "organization_id": old["organization_id"], "account_id": old["account_id"]})
        done = self.call("runtime.task.get", {"task_id": error.exception.trace_id})
        self.assertEqual((done["status"], done["error_code"]), ("outcome_unknown", "INVALID_TOOL_RESULT"))

    def test_corrupt_payload_is_blocked_before_handler(self):
        for index, payload in enumerate(('broken-json', '[]', '{"message":"missing-snapshot"}')):
            task = self.call("runtime.task.submit", {"tool": "draft.reply", "input": {"message": "工作地点？"}, "idempotency_key": str(index)})
            with store.runtime_db() as c:
                c.execute("UPDATE runtime_tasks SET payload=? WHERE task_id=?", (payload, task["task_id"]))
            with mock.patch("recruiter_reply.draft", side_effect=AssertionError("must not execute")):
                with self.assertRaises(KernelError) as error:
                    self.call("runtime.task.execute", {"task_id": task["task_id"]})
            self.assertEqual(error.exception.code, "INVALID_TASK_PAYLOAD")
            self.assertEqual(self.call("runtime.task.get", {"task_id": task["task_id"]})["status"], "blocked")

    def test_corrupt_document_before_and_during_work_never_leaves_executing(self):
        self.call("runtime.document.save", {"content": {"location": "example", "source": "fixture"}, "confirmed": True, "expected_revision": 0})
        def corrupt(*args):
            with store.runtime_db() as c:
                c.execute("UPDATE runtime_documents SET content='broken-json'")
            return {"text": "must-not-display"}
        for index in range(2):
            with store.runtime_db() as c:
                c.execute("UPDATE runtime_documents SET content=?", (json.dumps({"location": "example", "source": "fixture"}),))
            task = self.call("runtime.task.submit", {"tool": "draft.reply", "input": {"message": "工作地点？"}, "idempotency_key": str(index)})
            if index == 0:
                corrupt()
            with mock.patch("recruiter_reply.draft", side_effect=corrupt) as handler:
                with self.assertRaises(KernelError) as error:
                    self.call("runtime.task.execute", {"task_id": task["task_id"]})
                if index == 0:
                    handler.assert_not_called()
            self.assertEqual(error.exception.code, "INVALID_DOCUMENT_STATE")
            done = self.call("runtime.task.get", {"task_id": task["task_id"]})
            self.assertEqual((done["status"], done["result"]), ("blocked", None))

    def test_direct_draft_dispatch_with_corrupt_document_has_refusal_trace(self):
        with store.runtime_db() as c:
            c.execute("INSERT INTO runtime_documents VALUES (?,?,?,?)", (self.context["scope_id"], "facts", 1, 'broken-json'))
        with self.assertRaises(KernelError) as error:
            self.call("draft.reply", {"message": "工作地点？"})
        self.assertEqual(error.exception.code, "INVALID_DOCUMENT_STATE")
        self.assertEqual(self.trace_rows(error.exception.trace_id)[0]["code"], "INVALID_DOCUMENT_STATE")

    def test_invalid_tool_metadata_never_enters_registry(self):
        router = ToolRouter()
        bad = [Tool("test.one", lambda *a: None, effect="external_wirte"),
               Tool("test.two", lambda *a: None, kind="worker"),
               Tool("test.three", None), Tool("bad name", lambda *a: None),
               Tool("test.four", lambda *a: None, context_required="false"),
               Tool("test.five", lambda *a: None, kind="work", changes_context=True)]
        for tool in bad:
            with self.assertRaises(ValueError):
                router.register(tool)
        self.assertEqual(router._tools, {})


if __name__ == "__main__":
    unittest.main()
