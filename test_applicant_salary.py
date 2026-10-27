"""Rendered salary protocol and offline OCR evidence, no real browsers or model calls."""
import base64
from io import BytesIO
import json
from pathlib import Path
import types
import unittest
from unittest import mock

from PIL import Image
import applicant_browser
import applicant_salary_ocr
import test_applicant_observation as fixture
from sandbox import KernelError


def png(width=100, height=40):
    out = BytesIO()
    Image.new('RGB', (width, height), 'white').save(out, format='PNG')
    return out.getvalue()


class Socket:
    def __init__(self, value, after=None, image=None, timeout=False):
        self.value, self.after = value, after if after is not None else value
        self.image, self.timeout = image or png(1280,800), timeout
        self.sent, self.closed = [], False
    def send(self, value):
        self.sent.append(json.loads(value))
    def settimeout(self, value):
        pass
    def recv(self):
        request = self.sent[-1]
        if request['method'] == 'Page.captureScreenshot':
            if self.timeout:
                raise applicant_browser.websocket.WebSocketTimeoutException('fixture')
            result = {'data': base64.b64encode(self.image).decode('ascii')}
        else:
            result = {'result': {'value': self.value if request['id'] == 1 else self.after}}
        return json.dumps({'id': request['id'], 'result': result})
    def close(self):
        self.closed = True


class SalaryReadTest(unittest.TestCase):
    def setUp(self):
        self.f = fixture.ApplicantObservationTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.h = self.f.h
        self.value = {'status': 'observed', 'viewport': {'width':1280,'height':800}, 'regions': [
            {'job_id': 'fictional~', 'raw_text': '\ue032-\ue033K', 'rect': {'x': 200, 'y': 200, 'width': 80, 'height': 24}}]}

    def read(self, value=None, **options):
        connection = Socket(value or self.value, **options)
        with mock.patch.object(applicant_browser, '_read_json', return_value=[self.f.tab]), \
             mock.patch.object(applicant_browser.websocket, 'create_connection', return_value=connection):
            result = self.h.call('runtime.applicant.salary.read')
        self.assertTrue(connection.closed)
        return result, connection

    def test_capture_has_no_viewport_mutation_and_crops_only_salary_for_ocr(self):
        evidence = {'status': 'needs_confirmation', 'recognized_text': '8-12K', 'confidence': 0.99,
                    'monthly_salary': [8000,12000], 'confirmed': False, 'image_sha256': 'fixture'}
        with mock.patch.object(applicant_salary_ocr, 'recognize', return_value=evidence) as recognize:
            result, connection = self.read()
        self.assertFalse(result['matching_updated'])
        self.assertFalse(result['sending_enabled'])
        self.assertEqual(result['salaries'][0]['job_id'], 'fictional~')
        self.assertEqual([r['method'] for r in connection.sent], ['Runtime.evaluate','Page.captureScreenshot','Runtime.evaluate'])
        self.assertNotIn('clip', connection.sent[1]['params'])
        with Image.open(BytesIO(recognize.call_args.args[0])) as crop:
            self.assertEqual(crop.size,(80,24))
        self.assertNotIn('data', result['salaries'][0])

    def test_page_drift_discards_images_before_ocr(self):
        with mock.patch.object(applicant_salary_ocr, 'recognize') as recognize:
            with self.assertRaises(KernelError) as caught:
                self.read(after={'status': 'browser_not_visible', 'regions': []})
        self.assertEqual(caught.exception.code, 'SALARY_PAGE_CHANGED')
        recognize.assert_not_called()

    def test_multiple_salaries_use_one_viewport_capture(self):
        second = {**self.value['regions'][0], 'job_id': 'second', 'rect': {'x': 200, 'y': 350, 'width': 60, 'height': 24}}
        value = {**self.value, 'regions': [self.value['regions'][0], second]}
        with mock.patch.object(applicant_salary_ocr, 'recognize', return_value={'confirmed': False}) as recognize:
            result, connection = self.read(value)
        self.assertEqual(len(result['salaries']), 2)
        self.assertEqual(recognize.call_count, 2)
        self.assertEqual(sum(r['method']=='Page.captureScreenshot' for r in connection.sent), 1)

    def test_device_scale_is_applied_only_to_local_crops(self):
        with mock.patch.object(applicant_salary_ocr, 'recognize', return_value={'confirmed':False}) as recognize:
            self.read(image=png(1600,1000))
        with Image.open(BytesIO(recognize.call_args.args[0])) as crop:
            self.assertEqual(crop.size,(100,30))

    def test_mismatched_viewport_image_ratio_is_rejected(self):
        with mock.patch.object(applicant_salary_ocr, 'recognize') as recognize:
            with self.assertRaises(KernelError) as caught:
                self.read(image=png(1280,400))
        self.assertEqual(caught.exception.code,'INVALID_SALARY_IMAGE')
        recognize.assert_not_called()

    def test_off_viewport_region_is_rejected(self):
        value={**self.value,'regions':[{**self.value['regions'][0], 'rect':{'x':1270,'y':200,'width':80,'height':24}}]}
        with self.assertRaises(KernelError) as caught:
            self.read(value)
        self.assertEqual(caught.exception.code,'BROWSER_SCHEMA_CHANGED')

    def test_unready_states_never_capture_or_load_ocr(self):
        for status in ('browser_not_visible','restricted','auth_required','fonts_loading','schema_changed','no_visible_salary'):
            with self.subTest(status=status), mock.patch.object(applicant_salary_ocr, 'recognize') as recognize:
                result, connection = self.read({'status':status,'regions':[]})
                self.assertEqual(result['status'],status)
                self.assertEqual(len(connection.sent),1)
                recognize.assert_not_called()

    def test_invalid_region_sizes_duplicates_and_types_stop(self):
        region = self.value['regions'][0]
        bad = [{**region, 'rect':{**region['rect'], 'width': 601}},
               {**region, 'rect':{**region['rect'], 'x': -1}},
               {**region, 'rect':{**region['rect'], 'x': True}},
               {**region, 'rect':{**region['rect'], 'height': float('nan')}},
               {**region, 'job_id': '../bad'}]
        for item in bad:
            with self.subTest(item=item), self.assertRaises(KernelError):
                self.read({**self.value,'regions':[item]})
        with self.assertRaises(KernelError):
            self.read({**self.value,'regions':[region,region]})

    def test_screenshot_timeout_is_explicit(self):
        with self.assertRaises(KernelError) as caught:
            self.read(timeout=True)
        self.assertEqual(caught.exception.code,'BROWSER_READ_TIMEOUT')

    def test_client_cannot_supply_images(self):
        with mock.patch.object(applicant_salary_ocr.importlib.util,'find_spec',return_value=None):
            with self.assertRaises(KernelError) as caught:
                self.h.call('runtime.applicant.salary.read', {'image': 'arbitrary'})
        self.assertEqual(caught.exception.code,'INVALID_BROWSER_REQUEST')

    def test_missing_dependency_does_not_call_a_remote_fallback(self):
        with mock.patch.object(applicant_salary_ocr.importlib.util,'find_spec',return_value=None):
            with self.assertRaises(KernelError) as caught:
                self.read()
        self.assertEqual(caught.exception.code,'OCR_UNAVAILABLE')

    def test_missing_models_are_reported_without_downloading(self):
        with mock.patch.object(applicant_salary_ocr.importlib.util,'find_spec',return_value=types.SimpleNamespace(origin=str(Path(__file__)))), \
             mock.patch.object(Path,'is_file',return_value=False):
            with self.assertRaises(KernelError) as caught:
                self.read()
        self.assertEqual(caught.exception.code,'OCR_UNAVAILABLE')

    def ocr(self, output, image=None):
        engine = mock.Mock(return_value=(output,[0.01]))
        module = types.SimpleNamespace(RapidOCR=mock.Mock(return_value=engine))
        # Enter through the actual registered tool capability, replacing only the browser response.
        connection = Socket(self.value, image=image)
        with mock.patch.object(applicant_browser,'_read_json',return_value=[self.f.tab]), \
             mock.patch.object(applicant_browser.websocket,'create_connection',return_value=connection), \
             mock.patch.dict('sys.modules', {'rapidocr_onnxruntime': module}), \
             mock.patch.object(applicant_salary_ocr.importlib.util,'find_spec',return_value=types.SimpleNamespace(origin=str(Path(__file__)))), \
             mock.patch.object(Path,'is_file',return_value=True):
            return self.h.call('runtime.applicant.salary.read')

    def test_high_confidence_parsed_ocr_still_requires_confirmation(self):
        result = self.ocr([['8-12K',0.999]])['salaries'][0]
        self.assertEqual(result['status'],'needs_confirmation')
        self.assertFalse(result['confirmed'])
        self.assertEqual(result['monthly_salary'],[8000,12000])

    def test_low_confidence_or_non_monthly_text_requires_review(self):
        for output in ([['8-12K',0.5]], [['300元/天',0.999]], [['面议',0.999]]):
            self.assertEqual(self.ocr(output)['salaries'][0]['status'],'needs_review')

    def test_invalid_ocr_outputs_do_not_become_facts(self):
        for output in (None, [], [['8K', True]], [['8K', float('nan')]], [['8K',0.99],['12K',0.99]]):
            self.assertEqual(self.ocr(output)['salaries'][0]['status'],'unreadable')

    def test_oversized_image_dimensions_are_rejected_before_ocr(self):
        with self.assertRaises(KernelError) as caught:
            self.ocr([['8K',0.99]],image=png(4097,40))
        self.assertEqual(caught.exception.code,'INVALID_SALARY_IMAGE')

    def test_direct_ocr_cannot_bypass_the_kernel(self):
        with self.assertRaises(KernelError) as caught:
            applicant_salary_ocr.recognize(png())
        self.assertEqual(caught.exception.code,'KERNEL_REQUIRED')


if __name__ == '__main__':
    unittest.main()
