"""Execution acknowledgements commit with child results; queued controls never replay."""
from dataclasses import replace
import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import execution_engine
import main
import recruiter_reply
import store
from sandbox import KernelError


class ExecutionReceiptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(store, "DB_PATH", Path(self.tmp.name) / "runtime.db")
        patch.start()
        self.addCleanup(patch.stop)
        self.context = main.dispatch("runtime.context")["data"]

    def call(self, name, payload=None):
        return main.dispatch(name, payload, self.context["context_token"])

    def save(self, revision=0):
        return self.call("runtime.document.save", {"content": {"location": "fictional-office", "source": "fixture"},
                                                  "expected_revision": revision, "confirmed": True})

    def submit(self, key="one"):
        return self.call("runtime.task.submit", {"tool": "draft.reply", "input": {"message": "工作地点？"}, "idempotency_key": key})["data"]

    def row(self, task_id):
        with store.runtime_db() as c:
            return dict(c.execute("SELECT * FROM runtime_tasks WHERE task_id=?", (task_id,)).fetchone())

    def interrupt(self, task_id):
        with store.runtime_db() as c:
            c.execute("UPDATE runtime_tasks SET status='executing',result=NULL WHERE task_id=?", (task_id,))

    def test_every_registered_state_control_has_receipt_policy(self):
        controls = {name for name, tool in main._router._tools.items() if tool.kind == "control" and tool.effect == "state"}
        self.assertEqual(controls, execution_engine.RECEIPTED_CONTROLS)

    def test_execute_ack_recovers_without_regenerating_draft(self):
        self.save()
        task = self.submit()
        executed = self.call("runtime.task.execute", {"task_id": task["task_id"]})
        receipt = json.loads(self.row(executed["trace_id"])["commit_receipt"])
        self.assertEqual(receipt["result"], executed["data"])
        self.interrupt(executed["trace_id"])
        with mock.patch.object(recruiter_reply, "draft", side_effect=AssertionError("must not regenerate")):
            done = self.call("runtime.task.recover", {"task_id": executed["trace_id"]})["data"]
            self.assertEqual((done["status"], done["error_code"]), ("completed", "RECOVERED_COMMIT"))
            self.assertEqual(done["result"], executed["data"])
            repeated = self.call("runtime.task.execute", {"task_id": task["task_id"]})
            self.assertEqual(repeated["data"], executed["data"])
            self.assertIsNotNone(self.row(repeated["trace_id"])["commit_receipt"])

    def test_stale_facts_ack_does_not_claim_business_success(self):
        self.save()
        task = self.submit()
        self.save(1)
        executed = self.call("runtime.task.execute", {"task_id": task["task_id"]})
        self.assertEqual(executed["data"]["status"], "blocked")
        self.interrupt(executed["trace_id"])
        done = self.call("runtime.task.recover", {"task_id": executed["trace_id"]})["data"]
        self.assertEqual(done["result"]["error_code"], "STALE_FACTS")
        self.assertEqual(self.row(task["task_id"])["status"], "blocked")

    def test_receipt_failure_rolls_back_child_completion(self):
        self.save()
        task = self.submit()
        original = execution_engine.record_commit
        def refuse(c, context, tool, result):
            if tool == "runtime.task.execute":
                raise KernelError("FIXTURE_FAILURE", "fixture")
            return original(c, context, tool, result)
        with mock.patch.object(execution_engine, "record_commit", side_effect=refuse), \
             mock.patch.object(recruiter_reply, "draft", wraps=recruiter_reply.draft) as draft:
            with self.assertRaises(KernelError):
                self.call("runtime.task.execute", {"task_id": task["task_id"]})
            draft.assert_called_once()
        self.assertEqual(self.row(task["task_id"])["status"], "executing")
        with store.runtime_db() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM runtime_events WHERE task_id=? AND status='completed'", (task["task_id"],)).fetchone()[0], 0)

    def test_mismatched_execution_owner_is_rejected_before_handler(self):
        self.save()
        first, other = self.submit("first"), self.submit("other")
        owner = main._engine.submit("runtime.task.execute", {"task_id": first["task_id"]}, "owner", self.context, internal=True)
        self.interrupt(owner["task_id"])
        with mock.patch.object(recruiter_reply, "draft", side_effect=AssertionError("must not execute")):
            with self.assertRaises(KernelError) as error:
                main._engine.execute(other["task_id"], self.context["context_token"], work_only=True,
                                     receipt_owner={**self.context, "task_id": owner["task_id"]})
        self.assertEqual(error.exception.code, "INVALID_COMMIT_OWNER")
        self.assertEqual(self.row(other["task_id"])["status"], "queued")

    def test_queued_control_closes_without_applying_mutation(self):
        queued = main._engine.submit("runtime.document.save", {"content": {"location": "must-not-save", "source": "fixture"},
                                                               "expected_revision": 0, "confirmed": True}, "queued", self.context, internal=True)
        self.assertEqual(self.call("runtime.recovery.list")["data"][0]["task_id"], queued["task_id"])
        done = self.call("runtime.task.recover", {"task_id": queued["task_id"]})["data"]
        self.assertEqual((done["status"], done["error_code"]), ("cancelled", "NOT_STARTED"))
        self.assertEqual(self.call("runtime.document.get")["data"]["revision"], 0)
        self.assertEqual(self.call("runtime.task.recover", {"task_id": queued["task_id"]})["data"], done)
        self.assertEqual(self.call("runtime.recovery.list")["data"], [])

    def test_live_dispatch_cancelled_before_claim_does_not_report_success(self):
        ready, release = threading.Event(), threading.Event()
        original = main._engine.execute
        task_ids, outcomes = [], []
        def pause(task_id, token, **kwargs):
            if threading.current_thread().name == "receipt-origin":
                task_ids.append(task_id)
                ready.set()
                if not release.wait(5):
                    raise RuntimeError("test did not release origin")
            return original(task_id, token, **kwargs)
        def run():
            try:
                outcomes.append(self.save())
            except KernelError as error:
                outcomes.append((error.code, error.trace_id))
        with mock.patch.object(main._engine, "execute", side_effect=pause):
            worker = threading.Thread(target=run, name="receipt-origin")
            worker.start()
            try:
                self.assertTrue(ready.wait(5))
                self.call("runtime.task.recover", {"task_id": task_ids[0]})
            finally:
                release.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcomes, [("NOT_STARTED", task_ids[0])])
        self.assertEqual(self.call("runtime.document.get")["data"]["revision"], 0)

    def test_real_crash_before_and_after_execution_receipt_commit(self):
        self.save()
        script = '''
from dataclasses import replace
import os, sys
from pathlib import Path
import store
store.DB_PATH = Path(sys.argv[1])
import main, execution_engine
def pause_exit():
    print("checkpoint", flush=True)
    sys.stdin.readline()
    os._exit(0)
if sys.argv[4] == "before":
    original = execution_engine.record_commit
    def checkpoint(c, context, name, result):
        answer = original(c, context, name, result)
        if name == "runtime.task.execute": pause_exit()
        return answer
    execution_engine.record_commit = checkpoint
else:
    original = main._router.resolve("runtime.task.execute")
    def checkpoint(payload, context):
        original.handler(payload, context)
        pause_exit()
    main._router._tools[original.name] = replace(original, handler=checkpoint)
main._engine.execute(sys.argv[2], sys.argv[3])
'''
        for mode in ("before", "after"):
            task = self.submit(mode)
            control = main._engine.submit("runtime.task.execute", {"task_id": task["task_id"]}, "control-" + mode, self.context, internal=True)
            child = subprocess.Popen([sys.executable, "-c", script, str(store.DB_PATH), control["task_id"], self.context["context_token"], mode],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=Path(__file__).parent)
            try:
                lines = queue.Queue()
                reader = threading.Thread(target=lambda: lines.put(child.stdout.readline()), daemon=True)
                reader.start()
                self.assertEqual(lines.get(timeout=10).strip(), "checkpoint")
                child.stdin.write("exit\n")
                child.stdin.flush()
                child.wait(timeout=5)
                self.assertEqual(child.returncode, 0)
            finally:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=5)
            done = self.call("runtime.task.recover", {"task_id": control["task_id"]})["data"]
            if mode == "before":
                self.assertEqual(done["status"], "outcome_unknown")
                self.assertIsNone(self.row(control["task_id"])["commit_receipt"])
                self.assertEqual(self.row(task["task_id"])["status"], "executing")
            else:
                self.assertEqual(done["error_code"], "RECOVERED_COMMIT")
                self.assertEqual(done["result"]["status"], "completed")
                self.assertEqual(done["result"]["result"]["text"], "fictional-office")


if __name__ == "__main__":
    unittest.main()
