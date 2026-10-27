"""Local trusted TLS and interruptible socket semantics; no remote provider."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
import http.client
import os
from pathlib import Path
import select
import socket
import ssl
import threading
import time
import unittest
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
import llm_transport
import test_llm_transport as fixture
from sandbox import KernelError


class ModelLocalTlsTest(unittest.TestCase):
    def setUp(self):
        self.f = fixture.LlmTransportTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        os.environ['JOBKIT_LLM_TIMEOUT_SECONDS'] = '5'
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
        now = datetime.now(timezone.utc)
        certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost')]), critical=False)
            .sign(key, hashes.SHA256()))
        directory = Path(self.f.h.tmp.name)
        cert_path, key_path = directory / 'local-test-cert.pem', directory / 'local-test-key.pem'
        cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(cert_path, key_path)
        self.client_context = ssl.create_default_context(cafile=str(cert_path))
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), fixture.Handler)
        self.server.daemon_threads = True
        self.server.socket = server_context.wrap_socket(self.server.socket, server_side=True)
        self.server.requests, self.server.scenario = [], {}
        thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        def cleanup():
            self.server.shutdown()
            self.server.server_close()
            thread.join(timeout=1)
        self.addCleanup(cleanup)
        self.f.factory.side_effect = lambda timeout: http.client.HTTPSConnection('localhost', self.server.server_address[1],
            timeout=timeout, context=self.client_context)

    def wait_for_request(self):
        deadline = time.monotonic() + 3
        while not self.server.requests and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(self.server.requests)

    def task(self):
        return self.f.h.submit('工作地点工资？', key='local-tls')

    def test_real_local_tls_keeps_hostname_verification_and_response_facts(self):
        self.assertTrue(self.client_context.check_hostname)
        self.assertEqual(self.client_context.verify_mode, ssl.CERT_REQUIRED)
        result = self.f.execute()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['result']['text'], '虚构园区\n8-10K')
        self.assertEqual(self.server.requests[0]['path'], '/chat/completions')

    def test_cancel_local_tls_while_waiting_for_headers(self):
        self.server.scenario = {'delay_headers': 3}
        task = self.task()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.f.h.call, 'runtime.task.execute', {'task_id': task['task_id']})
            self.wait_for_request()
            self.f.h.call('runtime.task.cancel', {'task_id': task['task_id']})
            result = future.result(timeout=2)
        self.assertEqual(result['status'], 'cancelled')
        self.assertIsNone(result['result'])
        row = self.f.h.call('runtime.model.usage.get')['items'][0]
        self.assertEqual((row['state'], row['error_code']), ('outcome_unknown', 'CANCELLED'))

    def test_cancel_local_tls_during_fragmented_body(self):
        self.server.scenario = {'trickle_body': True}
        reading = threading.Event()
        factory = self.f.factory.side_effect
        def tracked(timeout):
            connection = factory(timeout)
            getresponse = connection.getresponse
            def response():
                value = getresponse()
                read = value.read1
                def read1(*args):
                    reading.set()
                    return read(*args)
                value.read1 = read1
                return value
            connection.getresponse = response
            return connection
        self.f.factory.side_effect = tracked
        task = self.task()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.f.h.call, 'runtime.task.execute', {'task_id': task['task_id']})
            self.assertTrue(reading.wait(3))
            self.f.h.call('runtime.task.cancel', {'task_id': task['task_id']})
            self.assertEqual(future.result(timeout=2)['status'], 'cancelled')
        row = self.f.h.call('runtime.model.usage.get')['items'][0]
        self.assertEqual(row['http_status'], 200)
        self.assertEqual(row['state'], 'outcome_unknown')

    def test_tls_hostname_mismatch_fails_before_http_request(self):
        self.f.factory.side_effect = lambda timeout: http.client.HTTPSConnection('127.0.0.1', self.server.server_address[1],
            timeout=timeout, context=self.client_context)
        self.f.assert_code('LLM_PROVIDER_FAILED')
        self.assertEqual(self.server.requests, [])
        self.assertEqual(self.f.h.call('runtime.model.usage.get')['items'][0]['state'], 'not_sent')


class InterruptibleIoTest(unittest.TestCase):
    def test_ssl_send_retries_same_bytes_for_io_readiness_only(self):
        raw = mock.Mock()
        received = bytearray()
        calls = 0
        def send(value):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ssl.SSLWantReadError()
            if calls == 2:
                raise ssl.SSLWantWriteError()
            size = min(2, len(value))
            received.extend(value[:size])
            return size
        raw.send.side_effect = send
        guarded = llm_transport._InterruptibleSocket(raw, lambda: None)
        with mock.patch.object(select, 'select', return_value=([], [], [])):
            guarded.sendall(b'abcd')
        self.assertEqual(received, b'abcd')

    def test_ssl_read_observes_interrupt_after_readiness_wait(self):
        raw = mock.Mock()
        interrupted = False
        def check():
            if interrupted:
                raise KernelError('CANCELLED', 'cancelled')
        def ready(*args):
            nonlocal interrupted
            interrupted = True
            return [], [], []
        raw.recv_into.side_effect = ssl.SSLWantWriteError()
        guarded = llm_transport._InterruptibleSocket(raw, check)
        with mock.patch.object(select, 'select', side_effect=ready):
            with self.assertRaises(KernelError) as caught:
                guarded.recv_into(bytearray(8))
        self.assertEqual(caught.exception.code, 'CANCELLED')
        self.assertEqual(raw.recv_into.call_count, 1)

    def test_response_reader_keeps_socket_alive_until_it_closes(self):
        raw, peer = socket.socketpair()
        self.addCleanup(raw.close)
        self.addCleanup(peer.close)
        guarded = llm_transport._InterruptibleSocket(raw, lambda: None)
        reader = guarded.makefile('rb')
        self.addCleanup(reader.close)
        guarded.close()
        self.assertGreaterEqual(raw.fileno(), 0)
        peer.sendall(b'ok')
        self.assertEqual(reader.read(2), b'ok')
        reader.close()
        self.assertEqual(raw.fileno(), -1)


if __name__ == '__main__':
    unittest.main()
