"""Crash recovery closes abandoned tasks, never replays tools or steals live work."""
from pathlib import Path
import os
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import main
import store
from sandbox import KernelError
from task_lease import task_lease
from tool_router import Tool


class RuntimeRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(store, "DB_PATH", Path(self.tmp.name) / "runtime.db")
        patch.start()
        self.addCleanup(patch.stop)
        self.context = main.dispatch("runtime.context")["data"]

    def call(self, name, payload=None):
        return main.dispatch(name, payload, self.context["context_token"])["data"]

    def abandoned(self, effect="none"):
        task = self.call("runtime.task.submit", {"tool": "draft.reply", "input": {"message": "工作地点？"},
                                                "idempotency_key": "one"})
        with store.runtime_db() as c:
            c.execute("UPDATE runtime_tasks SET status='executing',effect=? WHERE task_id=?", (effect, task["task_id"]))
        return task["task_id"]

    def test_abandoned_draft_becomes_failed_and_never_reexecutes(self):
        task_id = self.abandoned()
        with mock.patch("recruiter_reply.draft", side_effect=AssertionError("no replay")):
            done = self.call("runtime.task.recover", {"task_id": task_id})
            self.assertEqual((done["status"], done["error_code"]), ("failed", "INTERRUPTED"))
            self.assertIsNone(done["result"])
            self.assertEqual(self.call("runtime.task.execute", {"task_id": task_id}), done)
            self.assertEqual(self.call("runtime.task.recover", {"task_id": task_id}), done)
        events = [e for e in self.call("runtime.events") if e["task_id"] == task_id and e["code"] == "INTERRUPTED"]
        self.assertEqual(len(events), 1)

    def test_external_result_stays_unknown_and_prevents_scope_switch(self):
        task_id = self.abandoned("external_write")
        done = self.call("runtime.task.recover", {"task_id": task_id})
        self.assertEqual((done["status"], done["error_code"]), ("outcome_unknown", "OUTCOME_UNKNOWN"))
        self.assertEqual(self.call("runtime.task.execute", {"task_id": task_id}), done)
        with self.assertRaises(KernelError) as error:
            self.call("runtime.switch", {"role": "applicant", "organization_id": "local", "account_id": "other"})
        self.assertEqual(error.exception.code, "UNRESOLVED_EXTERNAL_ACTION")

    def test_live_lease_refuses_recovery_even_in_same_process(self):
        task_id = self.abandoned()
        with task_lease(task_id):
            with self.assertRaises(KernelError) as error:
                self.call("runtime.task.recover", {"task_id": task_id})
            self.assertEqual(error.exception.code, "TASK_BUSY")
        self.assertEqual(self.call("runtime.task.get", {"task_id": task_id})["status"], "executing")

    def test_lease_is_released_after_exception(self):
        task_id = self.abandoned()
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            with task_lease(task_id):
                raise RuntimeError("interrupted")
        self.assertEqual(self.call("runtime.task.recover", {"task_id": task_id})["error_code"], "INTERRUPTED")

    @unittest.skipUnless(os.name == "nt", "Windows path aliases")
    def test_windows_case_alias_cannot_acquire_a_second_lease(self):
        task_id = self.abandoned()
        with task_lease(task_id):
            with mock.patch.object(store, "DB_PATH", Path(str(store.DB_PATH).upper())):
                with self.assertRaises(KernelError) as error:
                    with task_lease(task_id):
                        self.fail("alias obtained second lease")
                self.assertEqual(error.exception.code, "TASK_BUSY")

    def test_queued_and_completed_tasks_are_not_changed(self):
        task = self.call("runtime.task.submit", {"tool": "draft.reply", "input": {"message": "工作地点？"},
                                                "idempotency_key": "one"})
        self.assertEqual(self.call("runtime.task.recover", {"task_id": task["task_id"]}), task)
        done = self.call("runtime.task.execute", {"task_id": task["task_id"]})
        self.assertEqual(self.call("runtime.task.recover", {"task_id": task["task_id"]}), done)

    def test_wrong_scope_is_rejected_and_completed_control_is_unchanged(self):
        task_id = self.abandoned()
        response = main.dispatch("runtime.document.get", {}, self.context["context_token"])
        recovered = self.call("runtime.task.recover", {"task_id": response["trace_id"]})
        self.assertEqual(recovered["status"], "completed")
        self.context = self.call("runtime.switch", {"role": "recruiter", "organization_id": "other", "account_id": "other"})
        with self.assertRaises(KernelError) as error:
            self.call("runtime.task.recover", {"task_id": task_id})
        self.assertEqual(error.exception.code, "TASK_NOT_FOUND")

    def test_real_process_exit_releases_execution_lease(self):
        tool_name = "test.crash"
        main._router.register(Tool(tool_name, lambda *args: self.fail("no replay"), kind="work", effect="none"))
        self.addCleanup(lambda: main._router._tools.pop(tool_name))
        task = main._engine.submit(tool_name, {}, "crash", self.context)
        script = '''
import os, sys
from pathlib import Path
import store
store.DB_PATH = Path(sys.argv[1])
import main
from tool_router import Tool
def crash(payload, context):
    print("executing", flush=True)
    sys.stdin.readline()
    os._exit(0)
main._router.register(Tool("test.crash", crash, kind="work", effect="none"))
main._engine.execute(sys.argv[2], sys.argv[3])
'''
        child = subprocess.Popen([sys.executable, "-c", script, str(store.DB_PATH), task["task_id"], self.context["context_token"]],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 cwd=Path(__file__).parent)
        def cleanup():
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
        self.addCleanup(cleanup)
        lines = queue.Queue()
        reader = threading.Thread(target=lambda: lines.put(child.stdout.readline()), daemon=True)
        reader.start()
        self.assertEqual(lines.get(timeout=10).strip(), "executing")
        with self.assertRaises(KernelError) as error:
            self.call("runtime.task.recover", {"task_id": task["task_id"]})
        self.assertEqual(error.exception.code, "TASK_BUSY")
        child.stdin.write("exit\n")
        child.stdin.flush()
        child.wait(timeout=5)
        self.assertEqual(child.returncode, 0)
        done = self.call("runtime.task.recover", {"task_id": task["task_id"]})
        self.assertEqual(done["error_code"], "INTERRUPTED")


if __name__ == "__main__":
    unittest.main()
