"""The documented applicant contract must match the code that actually exists."""
import re
import unittest
from pathlib import Path

import app
import main

DOCUMENT = Path(__file__).with_name('docs') / 'api' / 'applicant-send-loop.md'


class ApiContractTest(unittest.TestCase):
    def setUp(self):
        self.text = DOCUMENT.read_text(encoding='utf-8')
        self.registered = set(main._router._tools)

    def documented_tools(self):
        return set(re.findall(r'^### (runtime\.[a-z0-9_.]+)$', self.text, re.M))

    def documented_routes(self):
        routes = set()
        for method, path in re.findall(r'`(GET|POST) (/api/[^`]+)`', self.text):
            routes.add((method, re.sub(r'\{[^}]+\}', '{}', path)))
        return routes

    def test_every_documented_tool_is_registered(self):
        names = self.documented_tools()
        self.assertGreaterEqual(len(names), 15)
        self.assertEqual(sorted(names - self.registered), [])

    def test_every_registered_send_and_forget_tool_is_documented(self):
        names = self.documented_tools()
        expected = {name for name in self.registered
                    if name.startswith('runtime.applicant.send.') or name.startswith('runtime.applicant.reply.forget')}
        self.assertEqual(sorted(expected - names), [])

    def test_every_documented_route_exists(self):
        live = set()
        for route in app.app.routes:
            methods = getattr(route, 'methods', None) or set()
            path = getattr(route, 'path', '')
            for method in methods:
                live.add((method, re.sub(r'\{[^}]+\}', '{}', path)))
        documented = self.documented_routes()
        self.assertTrue(documented)
        self.assertEqual(sorted(documented - live), [])

    def test_maintenance_only_tools_have_no_http_route(self):
        for name in ('runtime.applicant.reply.forget', 'runtime.applicant.reply.forget.plan'):
            self.assertTrue(main._router._tools[name].maintenance_only)
        routes = self.documented_routes()
        self.assertEqual([route for route in routes if 'forget' in route[1]], [])


if __name__ == '__main__':
    unittest.main()
