"""Atomic control receipts reconcile committed state without replaying handlers."""
from dataclasses import replace
from contextlib import closing
import json
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import main
import runtime_tools
import store
from sandbox import KernelError
from task_lease import task_lease


class ControlReceiptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(store, "DB_PATH", Path(self.tmp.name) / "runtime.db")
        patch.start()
        self.addCleanup(patch.stop)
        self.context = main.dispatch("runtime.context")["data"]

    def call(self, name, payload=None):
        return main.dispatch(name, payload, self.context["context_token"])

    def save(self):
        return self.call("runtime.document.save", {"content": {"location": "fictional office", "source": "private-fixture-source"},
                                                   "expected_revision": 0, "confirmed": True})

    def row(self, task_id):
        with store.runtime_db() as c:
            return dict(c.execute("SELECT * FROM runtime_tasks WHERE task_id=?", (task_id,)).fetchone())

    def interrupt(self, task_id):
        with store.runtime_db() as c:
            c.execute("UPDATE runtime_tasks SET status='executing',result=NULL,error_code=NULL WHERE task_id=?", (task_id,))

    def recover(self, task_id):
        return self.call("runtime.task.recover", {"task_id": task_id})["data"]

    def test_committed_document_recovery_preserves_newer_document_and_does_not_replay(self):
        saved = self.save()
        task_id = saved["trace_id"]
        self.interrupt(task_id)
        self.call("runtime.document.save", {"content": {"location": "newer office", "source": "newer fixture"},
                                             "expected_revision": 1, "confirmed": True})
        tool = main._router._tools["runtime.document.save"]
        with mock.patch.dict(main._router._tools, {tool.name: replace(tool, handler=mock.Mock(side_effect=AssertionError("no replay")))}):
            done = self.recover(task_id)
            self.assertEqual((done["status"], done["error_code"]), ("completed", "RECOVERED_COMMIT"))
            self.assertEqual(done["result"]["revision"], 1, "receipt is the historical commit result")
            self.assertEqual(self.recover(task_id), done)
            with self.assertRaises(KernelError) as error:
                self.call("runtime.task.execute", {"task_id": task_id})
            self.assertEqual(error.exception.code, "CONTROL_NOT_EXECUTABLE")
        current = self.call("runtime.document.get")["data"]
        self.assertEqual((current["revision"], current["content"]["location"]), (2, "newer office"))
        self.assertNotIn("commit_receipt", done)

    def test_mutation_and_receipt_rollback_together(self):
        with mock.patch.object(runtime_tools, "record_commit", side_effect=KernelError("FIXTURE_FAILURE", "fixture")):
            with self.assertRaises(KernelError):
                self.save()
        self.assertEqual(self.call("runtime.document.get")["data"], {"kind": "facts", "revision": 0, "content": {}})
        with store.runtime_db() as c:
            row = c.execute("SELECT * FROM runtime_tasks WHERE tool='runtime.document.save'").fetchone()
            self.assertIsNone(row["commit_receipt"])

    def test_switch_receipt_never_switches_identity_again(self):
        old = self.context.copy()
        switched = self.call("runtime.switch", {"role": "applicant", "organization_id": "local", "account_id": "candidate"})
        self.context = switched["data"]
        self.interrupt(switched["trace_id"])
        self.assertEqual(self.call("runtime.recovery.list")["data"], [])
        with self.assertRaises(KernelError):
            self.recover(switched["trace_id"])
        self.context = self.call("runtime.switch", {"role": old["role"], "organization_id": old["organization_id"], "account_id": old["account_id"]})["data"]
        current_token = self.context["context_token"]
        done = self.recover(switched["trace_id"])
        self.assertEqual(done["error_code"], "RECOVERED_COMMIT")
        current = main.dispatch("runtime.context")["data"]
        self.assertEqual((current["scope_id"], current["context_token"]), (old["scope_id"], current_token))

    def test_submit_cancel_and_recover_controls_have_atomic_receipts(self):
        submitted = self.call("runtime.task.submit", {"tool": "draft.reply", "input": {"message": "地点？"}, "idempotency_key": "one"})
        cancelled = self.call("runtime.task.cancel", {"task_id": submitted["data"]["task_id"]})
        recovered = self.call("runtime.task.recover", {"task_id": submitted["data"]["task_id"]})
        for response in (submitted, cancelled, recovered):
            self.assertIsNotNone(self.row(response["trace_id"])["commit_receipt"])
            self.interrupt(response["trace_id"])
            self.assertEqual(self.recover(response["trace_id"])["error_code"], "RECOVERED_COMMIT")
        with store.runtime_db() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM runtime_tasks WHERE kind='work'").fetchone()[0], 1)

    def test_cancel_target_rolls_back_when_receipt_cannot_be_written(self):
        submitted = self.call("runtime.task.submit", {"tool": "draft.reply", "input": {"message": "地点？"}, "idempotency_key": "one"})["data"]
        with mock.patch("execution_engine.record_commit", side_effect=KernelError("FIXTURE_FAILURE", "fixture")):
            with self.assertRaises(KernelError):
                self.call("runtime.task.cancel", {"task_id": submitted["task_id"]})
        self.assertEqual(self.row(submitted["task_id"])["status"], "queued")

    def test_missing_or_corrupt_receipt_keeps_mutation_unknown(self):
        saved = self.save()
        task_id = saved["trace_id"]
        valid = self.row(task_id)["commit_receipt"]
        wrong_owner = json.loads(valid)
        wrong_owner["task_id"] = "different-task"
        invalid_result = json.loads(valid)
        invalid_result["result"] = {}
        for receipt, code in ((None, "OUTCOME_UNKNOWN"), ('broken-json', "INVALID_COMMIT_RECEIPT"),
                              (json.dumps(wrong_owner), "INVALID_COMMIT_RECEIPT"), (json.dumps(invalid_result), "INVALID_COMMIT_RECEIPT")):
            self.interrupt(task_id)
            with store.runtime_db() as c:
                c.execute("UPDATE runtime_tasks SET commit_receipt=? WHERE task_id=?", (receipt, task_id))
            done = self.recover(task_id)
            self.assertEqual((done["status"], done["error_code"]), ("outcome_unknown", code))
            self.assertEqual(self.recover(task_id), done)

    def test_read_only_control_can_be_closed_without_replaying(self):
        response = self.call("runtime.document.get")
        self.interrupt(response["trace_id"])
        done = self.recover(response["trace_id"])
        self.assertEqual((done["status"], done["error_code"]), ("failed", "INTERRUPTED"))

    def test_queued_control_cannot_be_replayed_via_work_entry(self):
        task = main._engine.submit("runtime.document.save", {"content": {"location": "must-not-save", "source": "fixture"},
                                                            "expected_revision": 0, "confirmed": True}, "queued-control", self.context, internal=True)
        with self.assertRaises(KernelError) as error:
            self.call("runtime.task.execute", {"task_id": task["task_id"]})
        self.assertEqual(error.exception.code, "CONTROL_NOT_EXECUTABLE")
        self.assertEqual(self.call("runtime.document.get")["data"]["revision"], 0)
        self.assertEqual(self.row(task["task_id"])["status"], "queued")

    def test_list_omits_private_receipts_and_busy_control_cannot_be_recovered(self):
        saved = self.save()
        self.interrupt(saved["trace_id"])
        rows = self.call("runtime.recovery.list")["data"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["task_id"], saved["trace_id"])
        self.assertNotIn("private-fixture", str(rows))
        self.assertNotIn("result", rows[0])
        self.assertNotIn("commit_receipt", rows[0])
        with task_lease(saved["trace_id"]):
            with self.assertRaises(KernelError) as error:
                self.recover(saved["trace_id"])
            self.assertEqual(error.exception.code, "TASK_BUSY")

    def test_old_schema_migrates_once_with_concurrent_connections(self):
        with closing(sqlite3.connect(store.DB_PATH)) as c:
            c.execute("ALTER TABLE runtime_tasks DROP COLUMN commit_receipt")
            c.commit()
        errors = []
        def connect():
            try:
                with store.runtime_db() as c:
                    self.assertIn("commit_receipt", {row[1] for row in c.execute("PRAGMA table_info(runtime_tasks)")})
            except Exception as error:
                errors.append(error)
        workers = [threading.Thread(target=connect) for _ in range(3)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(10)
            self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(main.dispatch("runtime.context")["data"]["scope_id"], self.context["scope_id"])

    def test_real_crash_before_and_after_commit(self):
        script = '''
from dataclasses import replace
import os, sys
from pathlib import Path
import store
store.DB_PATH = Path(sys.argv[1])
import main, runtime_tools
def pause_and_exit():
    print("checkpoint", flush=True)
    sys.stdin.readline()
    os._exit(0)
if sys.argv[4] == "before":
    original = runtime_tools.record_commit
    def checkpoint(*args):
        original(*args)
        pause_and_exit()
    runtime_tools.record_commit = checkpoint
else:
    original = main._router.resolve("runtime.document.save")
    def checkpoint(payload, context):
        original.handler(payload, context)
        pause_and_exit()
    main._router._tools[original.name] = replace(original, handler=checkpoint)
main._engine.execute(sys.argv[2], sys.argv[3])
'''
        for mode in ("before", "after"):
            task = main._engine.submit("runtime.document.save", {"content": {"location": "fixture-office", "source": "fixture"},
                                                                 "expected_revision": 0, "confirmed": True}, mode, self.context, internal=True)
            child = subprocess.Popen([sys.executable, "-c", script, str(store.DB_PATH), task["task_id"], self.context["context_token"], mode],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=Path(__file__).parent)
            try:
                lines = queue.Queue()
                reader = threading.Thread(target=lambda: lines.put(child.stdout.readline()), daemon=True)
                reader.start()
                self.assertEqual(lines.get(timeout=10).strip(), "checkpoint")
                if mode == "after":
                    with self.assertRaises(KernelError) as error:
                        self.recover(task["task_id"])
                    self.assertEqual(error.exception.code, "TASK_BUSY")
                child.stdin.write("exit\n")
                child.stdin.flush()
                child.wait(timeout=5)
                self.assertEqual(child.returncode, 0)
            finally:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=5)
            done = self.recover(task["task_id"])
            document = self.call("runtime.document.get")["data"]
            if mode == "before":
                self.assertEqual((done["status"], document["revision"]), ("outcome_unknown", 0))
                self.assertIsNone(self.row(task["task_id"])["commit_receipt"])
            else:
                self.assertEqual((done["status"], done["error_code"], document["revision"]), ("completed", "RECOVERED_COMMIT", 1))
                self.assertEqual(self.recover(task["task_id"]), done)


if __name__ == "__main__":
    unittest.main()
