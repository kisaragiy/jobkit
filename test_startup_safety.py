"""Startup refuses legacy process cleanup without touching real processes/ports."""
import contextlib
import io
import unittest
from unittest import mock

import app


class StartupSafetyTest(unittest.TestCase):
    def test_legacy_force_and_cleanup_stop_before_any_probe_or_subprocess(self):
        for flag in ("--force", "--cleanup"):
            with self.subTest(flag=flag), mock.patch("sys.argv", ["main.py", flag]), \
                 mock.patch.object(app, "_port_in_use", side_effect=AssertionError("must not probe")), \
                 mock.patch.object(app.subprocess, "run", side_effect=AssertionError("must not launch process")), \
                 contextlib.redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(SystemExit) as error:
                    app.main()
                self.assertEqual(error.exception.code, 2)
                self.assertIn("旧强制清理入口已停用", stderr.getvalue())
        self.assertFalse(hasattr(app, "_kill_stale_jobkit"))

    def test_invalid_ports_refuse_before_network_access(self):
        for port in ("0", "-1", "65536"):
            with mock.patch("sys.argv", ["main.py", "--port", port]), \
                 mock.patch.object(app, "_port_in_use", side_effect=AssertionError("must not probe")), \
                 contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    app.main()
                self.assertEqual(error.exception.code, 2)

    def test_existing_jobkit_returns_without_killing_or_restarting(self):
        with mock.patch("sys.argv", ["main.py"]), mock.patch.object(app, "_port_in_use", return_value=True), \
             mock.patch.object(app, "_is_our_jobkit", return_value=True), \
             mock.patch.object(app.subprocess, "run", side_effect=AssertionError("must not kill")), \
             mock.patch("uvicorn.run", side_effect=AssertionError("must not start")), \
             contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(app.main(), 3)
            self.assertNotIn("--force", stdout.getvalue())

    def test_another_service_is_preserved_and_alternate_port_is_used(self):
        with mock.patch("sys.argv", ["main.py"]), mock.patch.object(app, "_port_in_use", return_value=True), \
             mock.patch.object(app, "_is_our_jobkit", return_value=False), \
             mock.patch.object(app, "_port_state", return_value="busy"), \
             mock.patch.object(app, "_pick_port", return_value=18800), \
             mock.patch.object(app.subprocess, "run", side_effect=AssertionError("must not kill")), \
             mock.patch("uvicorn.run") as serve, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(app.main(), 0)
            serve.assert_called_once_with(app.app, host="127.0.0.1", port=18800, log_level="warning")

    def test_non_loopback_host_refuses_before_network_access(self):
        with mock.patch("sys.argv", ["main.py", "--host", "0.0.0.0"]), \
             mock.patch.object(app, "_port_in_use", side_effect=AssertionError("must not probe")), \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                app.main()
            self.assertEqual(error.exception.code, 2)

    def test_alternate_port_search_does_not_cross_tcp_range(self):
        probed = []
        def busy(host, port):
            self.assertTrue(1 <= port <= 65535)
            probed.append(port)
            return "busy"
        with mock.patch.object(app, "_port_state", side_effect=busy):
            self.assertIsNone(app._pick_port("127.0.0.1", 65534))
        self.assertEqual(probed, [65534, 65535])
