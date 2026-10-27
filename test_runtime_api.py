"""Real ASGI routes: role switching, persistent tasks and retired write entrances."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from fastapi.testclient import TestClient
import app
import store


class RuntimeApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(store, "DB_PATH", Path(self.tmp.name) / "runtime.db")
        self.patch.start()
        self.client = TestClient(app.app, base_url="http://127.0.0.1:18770")
        self.client.get("/workspace")
        self.context = self.client.get("/api/runtime/context").json()["data"]

    def tearDown(self):
        self.client.close()
        self.patch.stop()
        self.tmp.cleanup()

    def request(self, method, path, body=None, token=None):
        return self.client.request(method, "/api/runtime/" + path,
                                   headers={"X-Jobkit-Context": token or self.context["context_token"]}, json=body)

    def switch(self, role="recruiter", account="hr-one"):
        response = self.request("POST", "context", {"role": role, "organization_id": "example", "account_id": account})
        self.assertEqual(response.status_code, 200, response.text)
        self.context = response.json()["data"]

    def test_real_end_to_end_recruiter_workflow_survives_client_reopen(self):
        self.switch()
        saved = self.request("POST", "document", {"content": {"salary": "12–15K，按批准岗位范围", "source": "HR已确认"},
                                                    "expected_revision": 0, "confirmed": True})
        self.assertEqual(saved.status_code, 200)
        created = self.request("POST", "tasks", {"tool": "draft.reply", "input": {"message": "薪资范围是多少？"},
                                                   "idempotency_key": "one-request"}).json()["data"]
        result = self.request("POST", "tasks/" + created["task_id"] + "/execute", {}).json()["data"]
        self.assertEqual(result["status"], "completed")
        self.assertIn("12–15K", result["result"]["text"])
        self.assertFalse(result["result"]["sent"])
        with TestClient(app.app, base_url="http://127.0.0.1:18770") as another:
            another.get("/workspace")
            read = another.get("/api/runtime/tasks/" + created["task_id"], headers={"X-Jobkit-Context": self.context["context_token"]})
            self.assertEqual(read.json()["data"]["result"], result["result"])

    def test_old_tab_cannot_read_new_role_documents(self):
        old = self.context["context_token"]
        self.switch()
        response = self.request("GET", "document", token=old)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "STALE_CONTEXT")
        for path in ("/api/resume", "/api/chat/list", "/legacy"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 409)
            self.assertNotIn("resume.txt", response.text)

    def test_legacy_write_endpoints_do_not_start_subprocesses_or_import_cookies(self):
        with mock.patch.object(app.subprocess, "Popen", side_effect=AssertionError("must not spawn")), \
             mock.patch.object(app.boss_session, "save_from_cookie_list", side_effect=AssertionError("must not import")):
            for path, body in (("/api/apply/start", {"dry_run": False}), ("/api/delivery/request", {}),
                               ("/api/login/import", {"cookies": [{"value": "synthetic"}]}),
                               ("/api/delivery/record", {"jobId": "arbitrary", "status": "sent"})):
                response = self.client.post(path, json=body)
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()["code"], "KERNEL_REQUIRED")
        with store.runtime_db() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM greetings").fetchone()[0], 0)

    def test_context_is_mandatory_even_with_valid_browser_session(self):
        response = self.client.post("/api/runtime/context", json={"role": "recruiter", "organization_id": "example", "account_id": "one"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "STALE_CONTEXT")

    def test_abandoned_task_recovery_is_scoped_and_session_protected(self):
        task = self.request("POST", "tasks", {"tool": "draft.reply", "input": {"message": "工作地点？"},
                                              "idempotency_key": "recovery"}).json()["data"]
        with store.runtime_db() as c:
            c.execute("UPDATE runtime_tasks SET status='executing' WHERE task_id=?", (task["task_id"],))
        path = "tasks/" + task["task_id"] + "/recover"
        with TestClient(app.app, base_url="http://127.0.0.1:18770") as anonymous:
            self.assertEqual(anonymous.post('/api/runtime/' + path, json={}).status_code, 401)
        response = self.request("POST", path, {}, token="stale-token")
        self.assertEqual(response.json()["code"], "STALE_CONTEXT")
        result = self.request("POST", path, {}).json()["data"]
        self.assertEqual((result["status"], result["error_code"]), ("failed", "INTERRUPTED"))

    def test_first_workspace_is_recruiter_and_legacy_files_are_never_read(self):
        self.assertEqual(self.context["role"], "recruiter")
        with mock.patch.object(app, "load_resume", side_effect=AssertionError("must not read personal resume")):
            self.assertEqual(self.client.get("/api/resume").status_code, 409)

    def test_unregistered_work_cannot_sneak_in_a_send(self):
        response = self.request("POST", "tasks", {"tool": "boss.chat.send", "input": {"message": "synthetic"},
                                                   "idempotency_key": "must-not-execute"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "UNREGISTERED_TOOL")

    def test_invalid_http_body_has_trace_without_echoing_private_input(self):
        for body in (["private-candidate-body"], "private-candidate-body"):
            response = self.request("POST", "tasks", body)
            self.assertEqual(response.status_code, 400)
            result = response.json()
            self.assertEqual(result["code"], "INVALID_INPUT")
            self.assertTrue(result["trace_id"])
            self.assertNotIn("private-candidate-body", response.text)
            with store.runtime_db() as c:
                row = c.execute("SELECT * FROM runtime_events WHERE task_id=?", (result["trace_id"],)).fetchone()
                self.assertEqual(row["code"], "INVALID_INPUT")

    def test_stale_context_and_legacy_refusals_return_trace_ids(self):
        stale = self.request("GET", "document", token="old-token").json()
        self.assertTrue(stale["trace_id"])
        legacy = self.client.get("/api/resume").json()
        self.assertTrue(legacy["trace_id"])
        with store.runtime_db() as c:
            for result in (stale, legacy):
                self.assertEqual(c.execute("SELECT code FROM runtime_events WHERE task_id=?", (result["trace_id"],)).fetchone()[0], result["code"])

    def test_trace_filter_reads_refusals_only_in_current_scope(self):
        refusal = self.client.get("/api/resume").json()
        path = "events?trace_id=" + refusal["trace_id"]
        events = self.request("GET", path).json()["data"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["code"], "KERNEL_REQUIRED")
        self.switch(account="another")
        self.assertEqual(self.request("GET", path).json()["data"], [])
        invalid = self.request("GET", "events?trace_id=private-invalid-value")
        self.assertEqual(invalid.json()["code"], "INVALID_TRACE_ID")
        self.assertNotIn("private-invalid-value", invalid.text)

    def test_control_recovery_list_is_private_and_scope_fenced(self):
        self.switch()
        saved = self.request("POST", "document", {"content": {"location": "private-control-location", "source": "fixture"},
                                                   "confirmed": True, "expected_revision": 0}).json()
        with store.runtime_db() as c:
            c.execute("UPDATE runtime_tasks SET status='executing',result=NULL WHERE task_id=?", (saved["trace_id"],))
        response = self.request("GET", "recovery")
        self.assertEqual(response.json()["data"][0]["task_id"], saved["trace_id"])
        self.assertNotIn("private-control-location", response.text)
        self.assertNotIn("commit_receipt", response.text)
        old_token = self.context["context_token"]
        self.switch(account="other")
        self.assertEqual(self.request("GET", "recovery").json()["data"], [])
        self.assertEqual(self.request("GET", "recovery", token=old_token).json()["code"], "STALE_CONTEXT")
        self.assertEqual(self.request("POST", "tasks/" + saved["trace_id"] + "/recover", {}).status_code, 404)

    def test_control_recovery_http_restores_status_without_reapplying_save(self):
        saved = self.request("POST", "document", {"content": {"location": "fictional-office", "source": "fixture"},
                                                   "confirmed": True, "expected_revision": 0}).json()
        with store.runtime_db() as c:
            c.execute("UPDATE runtime_tasks SET status='executing',result=NULL WHERE task_id=?", (saved["trace_id"],))
        path = "tasks/" + saved["trace_id"] + "/recover"
        with TestClient(app.app, base_url="http://127.0.0.1:18770") as anonymous:
            self.assertEqual(anonymous.get('/api/runtime/recovery').status_code, 401)
        done = self.request("POST", path, {}).json()["data"]
        self.assertEqual((done["status"], done["error_code"]), ("completed", "RECOVERED_COMMIT"))
        self.assertEqual(self.request("GET", "document").json()["data"]["revision"], 1)
        self.assertEqual(self.request("GET", "recovery").json()["data"], [])

    def test_execute_acknowledgement_is_recoverable_without_reexecution(self):
        self.request("POST", "document", {"content": {"location": "fixture office", "source": "fixture"},
                                           "confirmed": True, "expected_revision": 0})
        task = self.request("POST", "tasks", {"tool": "draft.reply", "input": {"message": "工作地点？"},
                                               "idempotency_key": "execution-receipt"}).json()["data"]
        executed = self.request("POST", "tasks/" + task["task_id"] + "/execute", {}).json()
        with store.runtime_db() as c:
            c.execute("UPDATE runtime_tasks SET status='executing',result=NULL WHERE task_id=?", (executed["trace_id"],))
        recovered = self.request("POST", "tasks/" + executed["trace_id"] + "/recover", {}).json()["data"]
        self.assertEqual(recovered["error_code"], "RECOVERED_COMMIT")
        self.assertEqual(recovered["result"], executed["data"])

    def test_queued_control_can_be_closed_without_saving_document(self):
        import main
        queued = main._engine.submit("runtime.document.save", {"content": {"source": "fixture"}, "expected_revision": 0,
                                                               "confirmed": True}, "queued-api", self.context, internal=True)
        self.assertEqual(self.request("GET", "recovery").json()["data"][0]["status"], "queued")
        result = self.request("POST", "tasks/" + queued["task_id"] + "/recover", {}).json()["data"]
        self.assertEqual((result["status"], result["error_code"]), ("cancelled", "NOT_STARTED"))
        self.assertEqual(self.request("GET", "document").json()["data"]["revision"], 0)

    def test_new_workspace_and_desktop_do_not_load_legacy_browser_engine(self):
        self.assertIn("/api/runtime/", self.client.get("/workspace").text)
        desktop = self.client.get("/desktop").text
        self.assertIn('src="/workspace"', desktop)
        self.assertNotIn("executeJavaScript", desktop)
        self.assertNotIn("delivery-engine.js", desktop)
        self.assertEqual(self.client.get("/desktop-assets/delivery-engine.js").status_code, 404)
