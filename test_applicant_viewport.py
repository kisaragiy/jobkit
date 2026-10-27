"""Local browser display repair has its own maintenance-only capability and no sends."""
import json
from dataclasses import replace
import unittest
from unittest import mock

import applicant_browser
import main
import store
import test_applicant_observation as fixture
from sandbox import KernelError, maintenance_access
from tool_router import Tool, ToolRouter


class Socket:
    def __init__(self, before=None, after=None, timeout=False):
        self.before = before or {'width':103,'height':48,'outer_width':2048,'outer_height':1112,'visibility':'visible'}
        self.after = after or {**self.before,'width':2048,'height':952}
        self.sent, self.closed, self.timeout = [], False, timeout
    def send(self, value): self.sent.append(json.loads(value))
    def settimeout(self, value): pass
    def recv(self):
        request = self.sent[-1]
        if self.timeout and request['id'] > 1:
            raise applicant_browser.websocket.WebSocketTimeoutException('fixture')
        result = {'result':{'value':self.before if request['id']==1 else self.after}} if request['method']=='Runtime.evaluate' else {}
        return json.dumps({'id':request['id'],'result':result})
    def close(self): self.closed=True


class ViewportRepairTest(unittest.TestCase):
    def setUp(self):
        self.f=fixture.ApplicantObservationTest();self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.h=self.f.h

    def reset(self, **options):
        connection=Socket(**options)
        with mock.patch.object(applicant_browser,'_read_json',return_value=[self.f.tab]), \
             mock.patch.object(applicant_browser.websocket,'create_connection',return_value=connection),maintenance_access():
            result=self.h.call('runtime.applicant.browser.viewport.reset',{'confirmed':True})
        self.assertTrue(connection.closed)
        return result,connection

    def test_explicit_local_ui_effect_requires_maintenance_registration(self):
        with self.assertRaises(ValueError):
            ToolRouter().register(Tool('test.ui',lambda *args:None,effect='local_ui'))
        router=ToolRouter()
        router.register(Tool('test.ui',lambda *args:None,effect='local_ui',maintenance_only=True))

    def test_reset_requires_maintenance_and_explicit_confirmation(self):
        with self.assertRaises(KernelError) as caught:
            self.h.call('runtime.applicant.browser.viewport.reset',{'confirmed':True})
        self.assertEqual(caught.exception.code,'MAINTENANCE_REQUIRED')
        with maintenance_access(),self.assertRaises(KernelError) as caught:
            self.h.call('runtime.applicant.browser.viewport.reset',{'confirmed':False})
        self.assertEqual(caught.exception.code,'BROWSER_CONFIRMATION_REQUIRED')

    def test_visible_collapsed_page_is_repaired_and_read_back(self):
        result,connection=self.reset()
        self.assertEqual(result['status'],'reset_completed')
        self.assertFalse(result['sending_enabled'])
        self.assertEqual([r['method'] for r in connection.sent],['Runtime.evaluate','Emulation.clearDeviceMetricsOverride','Emulation.setVisibleSize','Runtime.evaluate'])
        self.assertEqual(connection.sent[2]['params'],{'width':2048,'height':952})

    def test_normal_page_is_not_modified(self):
        result,connection=self.reset(before={'width':1280,'height':800,'outer_width':1280,'outer_height':920,'visibility':'visible'})
        self.assertEqual(result['status'],'reset_not_needed')
        self.assertEqual(len(connection.sent),1)

    def test_hidden_window_requires_manual_restore(self):
        result,connection=self.reset(before={'width':103,'height':48,'outer_width':2048,'outer_height':1112,'visibility':'hidden'})
        self.assertEqual(result['status'],'manual_restore_required')
        self.assertEqual(len(connection.sent),1)

    def test_timeout_cannot_claim_repair_completed(self):
        with self.assertRaises(KernelError) as caught:self.reset(timeout=True)
        self.assertEqual(caught.exception.code,'VIEWPORT_RESET_UNKNOWN')

    def test_invalid_readback_does_not_claim_success(self):
        with self.assertRaises(KernelError) as caught:self.reset(after={'width':True})
        self.assertEqual(caught.exception.code,'VIEWPORT_RESET_UNKNOWN')

    def test_direct_reset_cannot_bypass_kernel(self):
        with maintenance_access(),self.assertRaises(KernelError) as caught:
            applicant_browser.read_current_jobs(reset_viewport=True)
        self.assertEqual(caught.exception.code,'KERNEL_REQUIRED')

    def test_interrupted_local_ui_action_recovers_unknown_without_replay(self):
        self.reset()
        with store.runtime_db() as c:
            row=c.execute("SELECT task_id FROM runtime_tasks WHERE tool='runtime.applicant.browser.viewport.reset' ORDER BY created_at DESC LIMIT 1").fetchone()
            task_id=row['task_id']
            c.execute("UPDATE runtime_tasks SET status='executing',result=NULL WHERE task_id=?",(task_id,))
        tool=main._router._tools['runtime.applicant.browser.viewport.reset']
        with mock.patch.dict(main._router._tools,{tool.name:replace(tool,handler=mock.Mock(side_effect=AssertionError('must not replay')))}):
            recovered=self.h.call('runtime.task.recover',{'task_id':task_id})
        self.assertEqual(recovered['status'],'outcome_unknown')


if __name__=='__main__':unittest.main()
