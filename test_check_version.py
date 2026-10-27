"""G6 第二半：scripts/check-version.py 的三档退出码（0 一致 / 2 落后 / 1 没法判断）。

验收点（docs/reviews/hermes-p8-2026-10-27.md 第 4 节）：
「运行版本 vs 磁盘」过去没有一条命令可验 —— /version 报了指纹，但没人拿它和磁盘比。

本文件：
- 不硬编码任何真实指纹（全部用临时目录现算），不连接任何真实服务；
- 「服务不可达」用真连一个刚释放的回环端口，不用假替身糊过去；
- 不启服务、不写库、不碰浏览器。
"""
import importlib.util
import io
import json
import shutil
import socket
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import app

ROOT = Path(app.__file__).parent
SCRIPT = ROOT / "scripts" / "check-version.py"
COMMIT = "a" * 40          # 假的 commit：只用于比「相同/不同」，不声称是真实哈希


def load_script():
    spec = importlib.util.spec_from_file_location("check_version_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def copy_core_files(destination: Path) -> Path:
    for name in app.FINGERPRINT_FILES:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    return destination


def payload_from(identity: dict, **overrides) -> dict:
    """按 /version 的真实形状造一份运行中进程的响应（字段与 app._compose_identity 对齐）。"""
    payload = {
        "service": "jobkit", "ok": True, "app_version": app.APP_VERSION,
        "started_at": "2026-10-27T00:00:00+08:00", "python_version": "3.11.0",
        "build_fingerprint": identity["build_fingerprint"],
        "files": dict(identity["files"]),
        "git_commit": identity["git_commit"],
    }
    payload.update(overrides)
    return payload


def run_check(script, url="http://127.0.0.1:18770", fetch=None, local=None):
    """跑一次检查，返回 (退出码, 解析出的 JSON 行列表)。"""
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = script.check(url, fetch=fetch, local=local)
    lines = [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]
    return code, lines


def steps(lines):
    return [line["step"] for line in lines]


class FingerprintSourceTest(unittest.TestCase):
    """磁盘侧必须用 app 的算法重算，不能自带第二套。"""

    @classmethod
    def setUpClass(cls):
        cls.script = load_script()

    def test_disk_identity_reuses_app_compute_build_fingerprint(self):
        expected, expected_files = app.compute_build_fingerprint(ROOT)
        identity = self.script.local_identity(ROOT, commit=COMMIT)
        self.assertEqual(identity["build_fingerprint"], expected)
        self.assertEqual(identity["files"], expected_files)
        self.assertEqual(set(identity["files"]), set(app.FINGERPRINT_FILES))

    def test_disk_identity_asks_git_unless_commit_is_injected(self):
        with mock.patch.object(app, "_read_git_commit", return_value="b" * 40):
            self.assertEqual(self.script.local_identity(ROOT)["git_commit"], "b" * 40)
        self.assertEqual(self.script.local_identity(ROOT, commit=None)["git_commit"], None)

    def test_script_has_no_server_or_database_dependency(self):
        source = SCRIPT.read_text(encoding="utf-8")
        for forbidden in ("uvicorn", "TestClient", "HTTPServer", "serve_forever",
                          "store.runtime_db", "jobkit.db"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


class ExitCodeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = load_script()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.clean = copy_core_files(self.root / "clean")

    def fake_fetch(self, payload):
        def fetch(url, timeout=5.0):
            if payload is not None:
                self.script.validate_payload(payload, url)
            return payload
        return fetch

    def test_consistent_runtime_exits_ok(self):
        disk = self.script.local_identity(self.clean, commit=COMMIT)
        code, lines = run_check(self.script, fetch=self.fake_fetch(payload_from(disk)),
                               local=lambda: disk)
        self.assertEqual(code, 0)
        self.assertEqual(steps(lines), ["runtime", "disk", "version", "summary"])
        verdict, summary = lines[-2], lines[-1]
        self.assertIs(verdict["ok"], True)
        self.assertEqual(verdict["file_diffs"], [])
        self.assertIs(verdict["git_commit_matches"], True)
        self.assertIs(summary["exit"], 0)
        self.assertIs(summary["drift"], False)
        self.assertEqual(summary["files_differing"], [])
        self.assertIn("一致", verdict["reason"])

    def test_one_changed_file_exits_alert_and_names_the_file(self):
        runtime = self.script.local_identity(self.clean, commit=COMMIT)      # 运行中的旧代码
        dirty = copy_core_files(self.root / "dirty")
        with open(dirty / "sandbox.py", "ab") as handle:
            handle.write(b"\n# drift: running process is one commit behind\n")
        disk = self.script.local_identity(dirty, commit=COMMIT)              # 磁盘上的新代码
        code, lines = run_check(self.script, fetch=self.fake_fetch(payload_from(runtime)),
                                local=lambda: disk)
        self.assertEqual(code, 2)
        verdict, summary = lines[-2], lines[-1]
        self.assertIs(verdict["ok"], False)
        self.assertEqual([item["file"] for item in verdict["file_diffs"]], ["sandbox.py"])
        self.assertNotEqual(verdict["file_diffs"][0]["runtime"],
                            verdict["file_diffs"][0]["disk"])
        self.assertIs(verdict["fingerprint_matches"], False)
        self.assertIn("落后", verdict["reason"])
        self.assertIn("sandbox.py", verdict["reason"])
        self.assertEqual(summary["files_differing"], ["sandbox.py"])
        self.assertIs(summary["exit"], 2)

    def test_same_files_but_different_commit_exits_alert(self):
        disk = self.script.local_identity(self.clean, commit=COMMIT)
        runtime = payload_from(disk, git_commit="c" * 40)
        code, lines = run_check(self.script, fetch=self.fake_fetch(runtime), local=lambda: disk)
        self.assertEqual(code, 2)
        verdict = lines[-2]
        self.assertEqual(verdict["file_diffs"], [])
        self.assertIs(verdict["git_commit_matches"], False)
        self.assertIn("commit", verdict["reason"])

    def test_missing_git_on_disk_does_not_fake_an_alert(self):
        disk = self.script.local_identity(self.clean, commit=None)   # 磁盘读不到 git
        runtime = payload_from(dict(disk, git_commit=COMMIT))
        code, lines = run_check(self.script, fetch=self.fake_fetch(runtime), local=lambda: disk)
        self.assertEqual(code, 0)
        self.assertIsNone(lines[-2]["git_commit_matches"])

    def test_unreachable_service_exits_error_not_alert(self):
        with socket.socket() as probe:          # 拿一个刚被释放的回环端口，真连不上
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        code, lines = run_check(self.script, url="http://127.0.0.1:%d" % port,
                                local=lambda: self.script.local_identity(self.clean))
        self.assertEqual(code, 1)
        self.assertEqual(steps(lines), ["version"])
        self.assertIs(lines[-1]["ok"], False)
        self.assertIn("连不上", lines[-1]["reason"])

    def test_invalid_payload_exits_error(self):
        broken = [
            {"service": "other", "ok": True, "build_fingerprint": "a" * 64, "files": {"app.py": "b" * 12}},
            {"service": "jobkit", "ok": True, "build_fingerprint": "not-a-hash", "files": {"app.py": "b" * 12}},
            {"service": "jobkit", "ok": True, "build_fingerprint": "a" * 64, "files": {}},
            {"service": "jobkit", "ok": True, "build_fingerprint": "a" * 64,
             "files": {"app.py": "b" * 12}, "git_commit": "not-a-commit"},
            "not-an-object",
        ]
        for payload in broken:
            with self.subTest(payload=payload):
                code, lines = run_check(self.script, fetch=self.fake_fetch(payload),
                                        local=lambda: self.script.local_identity(self.clean))
                self.assertEqual(code, 1)
                self.assertEqual(steps(lines), ["version"])
                self.assertIs(lines[-1]["ok"], False)

    def test_disk_side_failure_exits_error(self):
        disk = self.script.local_identity(self.clean, commit=COMMIT)

        def boom():
            raise OSError("disk unreadable")

        code, lines = run_check(self.script, fetch=self.fake_fetch(payload_from(disk)), local=boom)
        self.assertEqual(code, 1)
        self.assertIn("磁盘指纹重算失败", lines[-1]["reason"])

    def test_every_emitted_line_is_json_with_a_chinese_reason(self):
        disk = self.script.local_identity(self.clean, commit=COMMIT)
        _, lines = run_check(self.script, fetch=self.fake_fetch(payload_from(disk)),
                             local=lambda: disk)
        self.assertTrue(all(isinstance(line, dict) and "step" in line for line in lines))
        reason = lines[-2]["reason"]
        self.assertTrue(any("\u4e00" <= char <= "\u9fff" for char in reason), reason)


class DefaultsTest(unittest.TestCase):
    def test_default_url_matches_the_service_port(self):
        script = load_script()
        self.assertEqual(script.DEFAULT_URL, "http://127.0.0.1:18770")
        self.assertEqual(script.version_url("http://127.0.0.1:18800/"), "http://127.0.0.1:18800/version")

    def test_arguments_parse_without_touching_the_network(self):
        script = load_script()
        with mock.patch.object(script, "check", return_value=0) as called:
            self.assertEqual(script.main(["--url", "http://127.0.0.1:18800", "--timeout", "9"]), 0)
        called.assert_called_once_with("http://127.0.0.1:18800", timeout=9.0)


if __name__ == "__main__":
    unittest.main()
