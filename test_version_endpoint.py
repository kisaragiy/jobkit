"""G6 运行版本可证明：GET /version 只读指纹接口。

验收点（docs/handoff/hermes-implementation-plan-2026-10-27.md 第 183 行）：
「不能以磁盘代码测试通过替代运行版本验证」——所以要能从运行中的进程读出
版本号 / 启动时刻 / 构建指纹 / git commit，并且指纹真的跟着文件内容动。

本文件不读真实 jobkit.db、不启浏览器、不连平台；不硬编码真实 git 哈希。
"""
import json
import re
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import app
import store

HERE = Path(app.__file__).parent
SHORT_HASH = re.compile(r"^[0-9a-f]{12}$")
FULL_HASH = re.compile(r"^[0-9a-f]{64}$")
DOCUMENTED_FIELDS = {
    "service", "ok", "app_version", "started_at", "build_fingerprint",
    "files", "git_commit", "python_version",
}


class VersionEndpointTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_patch = mock.patch.object(store, "DB_PATH", Path(self.tmp.name) / "runtime.db")
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.client = TestClient(app.app, base_url="http://127.0.0.1:18770")
        self.addCleanup(self.client.close)

    def fetch(self, **headers):
        return self.client.get("/version", headers=headers)

    def body(self):
        response = self.fetch()
        self.assertEqual(response.status_code, 200)
        return response.json()

    def copy_tree(self) -> Path:
        """把参与指纹的文件复制到临时目录：改内容只影响副本，不碰工作区。"""
        root = Path(self.tmp.name) / "copy"
        for name in app.FINGERPRINT_FILES:
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(HERE / name, target)
        return root

    # ── 接口形状 ────────────────────────────────────────────
    def test_readable_without_session_but_loopback_only(self):
        self.assertEqual(self.fetch().status_code, 200)          # 与 /health 同级：不需要会话 cookie
        self.assertEqual(self.fetch(**{"Host": "audit.invalid"}).status_code, 403)   # 仍只限回环来源
        self.assertEqual(self.fetch().headers["cache-control"], "no-store")

    def test_payload_has_every_documented_field(self):
        payload = self.body()
        self.assertEqual(set(payload), DOCUMENTED_FIELDS)
        self.assertEqual(payload["service"], "jobkit")
        self.assertIs(payload["ok"], True)
        self.assertEqual(payload["app_version"], app.APP_VERSION)
        self.assertEqual(payload["python_version"], _python_version())
        self.assertRegex(payload["build_fingerprint"], FULL_HASH)
        commit = payload["git_commit"]
        if commit is not None:      # 没有 git 时必须为 None，而不是编一个值
            self.assertRegex(commit, r"^[0-9a-fA-F]{40,64}$")

    def test_started_at_is_process_start_in_utc8(self):
        first, second = self.body(), self.body()
        self.assertEqual(first["started_at"], second["started_at"])   # 进程内固定，不随请求漂移
        started = datetime.fromisoformat(first["started_at"])
        self.assertIsNotNone(started.tzinfo)
        self.assertEqual(started.utcoffset(), timedelta(hours=8))
        now = datetime.now(timezone(timedelta(hours=8)))
        self.assertLessEqual(started, now + timedelta(seconds=60))
        self.assertLess(now - started, timedelta(days=30))

    def test_files_are_fixed_core_set_with_short_hashes(self):
        files = self.body()["files"]
        self.assertEqual(set(files), set(app.FINGERPRINT_FILES))
        for name, digest in files.items():
            self.assertRegex(digest, SHORT_HASH, name)
        self.assertIn("app.py", files)          # 自己也在集内：改 app.py 一定动指纹

    def test_payload_exposes_no_paths_or_personal_data(self):
        raw = json.dumps(self.body(), ensure_ascii=False)
        for leak in ("C:", "C\\", "\\\\", "Users", "user", "resume.txt", ".enc", "token"):
            self.assertNotIn(leak, raw)

    def test_version_does_not_touch_the_database(self):
        with mock.patch.object(app, "connect", side_effect=AssertionError("版本接口不该访问数据库")):
            self.assertEqual(self.fetch().status_code, 200)

    # ── 指纹语义 ────────────────────────────────────────────
    def test_fingerprint_is_deterministic_for_same_code(self):
        root = self.copy_tree()
        first, first_files = app.compute_build_fingerprint(root)
        second, second_files = app.compute_build_fingerprint(root)
        self.assertEqual(first, second)
        self.assertEqual(first_files, second_files)
        self.assertEqual(first, self.body()["build_fingerprint"])   # 副本与运行版本一致

    def test_fingerprint_moves_when_one_file_changes(self):
        root = self.copy_tree()
        before, before_files = app.compute_build_fingerprint(root)
        with open(root / "sandbox.py", "ab") as handle:
            handle.write(b"\n# build probe: one byte of drift must move the fingerprint\n")
        after, after_files = app.compute_build_fingerprint(root)
        self.assertNotEqual(before, after)
        self.assertNotEqual(before_files["sandbox.py"], after_files["sandbox.py"])
        for name in set(before_files) - {"sandbox.py"}:
            self.assertEqual(before_files[name], after_files[name], name)

    def test_missing_file_is_marked_not_fatal(self):
        root = self.copy_tree()
        (root / "web" / "workspace.html").unlink()
        fingerprint, files = app.compute_build_fingerprint(root)
        self.assertEqual(files["web/workspace.html"], "missing")
        self.assertRegex(fingerprint, FULL_HASH)

    # ── git 缺失不报错 ──────────────────────────────────────
    def test_git_commit_is_null_when_git_missing(self):
        with mock.patch.object(app.subprocess, "run", side_effect=FileNotFoundError("no git")):
            self.assertIsNone(app._read_git_commit())
            self.assertIsNone(app._compose_identity()["git_commit"])

    def test_git_commit_is_null_on_non_repo_output(self):
        fake = mock.Mock(returncode=128, stdout="", stderr="fatal: not a git repository")
        with mock.patch.object(app.subprocess, "run", return_value=fake):
            self.assertIsNone(app._read_git_commit())
        fake = mock.Mock(returncode=0, stdout="fatal: not a git repository\n", stderr="")
        with mock.patch.object(app.subprocess, "run", return_value=fake):
            self.assertIsNone(app._read_git_commit())

    # ── 缓存语义 ────────────────────────────────────────────
    def test_endpoint_serves_cached_identity_instead_of_rescanning(self):
        cached = app.build_identity_snapshot()
        with mock.patch.object(app, "_compose_identity", side_effect=AssertionError("每次请求重算全盘")):
            payload = self.body()
        self.assertEqual({k: payload[k] for k in cached}, cached)

    def test_snapshot_is_a_copy(self):
        payload = self.body()
        payload["files"]["app.py"] = "tampered00000"
        self.assertEqual(self.body()["files"]["app.py"], app.build_identity_snapshot()["files"]["app.py"])


def _python_version() -> str:
    import platform
    return platform.python_version()


if __name__ == "__main__":
    unittest.main()
