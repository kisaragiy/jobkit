"""G6 第一半：scripts/check-install.py 的一键体检报告。

验收点（方案 G6 第 181–189 行）：「本机 MVP 先交付…服务提供可读版本/构建指纹…
依赖检查需覆盖 OCR 的 DLL/可选依赖」—— 单个检查脚本都有了，缺的是一条命令给出
普通人能读懂的整份结论。

本文件：
- 四种情形（一切就绪 / 端口被别的程序占 / 库不存在 / 版本落后）全部用注入的探针造出来，
  不连真实端口、不碰真实 jobkit.db、不启停服务、不碰浏览器；
- 唯一真实的 IO 是临时目录里的 SQLite（只读语义还要验证它真没被写）。
"""
import hashlib
import importlib.util
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "scripts" / "check-install.py"

FINGERPRINT_OLD = "1" * 64
FINGERPRINT_NEW = "2" * 64
COMMIT = "a" * 40


def load_script():
    spec = importlib.util.spec_from_file_location("check_install_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def version_payload(fingerprint=FINGERPRINT_NEW, files=None, commit=COMMIT):
    """按 app._compose_identity 的真实字段造一份 /version 响应（不声称是真实指纹）。"""
    return {
        "service": "jobkit", "ok": True, "app_version": "0.0.0-test",
        "started_at": "2026-10-28T00:00:00+08:00", "python_version": "3.11.0",
        "build_fingerprint": fingerprint,
        "files": files or {"app.py": "a" * 64, "store.py": "b" * 64},
        "git_commit": commit,
    }


def make_http(*, health_service="jobkit", version=None, browser="Edg/1.0"):
    """假的 GET：按路径返回 /health、/version、9222/json/version 三类响应。"""
    def http(url, timeout=5.0):
        if url.endswith("/health"):
            return 200, json.dumps({"service": health_service, "ok": True}).encode()
        if url.endswith("/version"):
            if version is None:
                return 401, b'{"ok":false,"code":"LOCAL_SESSION_REQUIRED"}'
            return 200, json.dumps(version).encode()
        if "/json/version" in url:
            if browser is None:
                raise OSError("connection refused")
            return 200, json.dumps({"Browser": browser,
                                    "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools"}).encode()
        raise AssertionError("unexpected url: %s" % url)
    return http


class FakeDeps:
    """check-deps 的最小替身：接口与真模块一致，避免测试跑九次子进程导入。"""

    def __init__(self, *, missing=(), ocr_ready=True):
        self.missing = set(missing)
        self.ocr_ready = ocr_ready

    def parse_requirements(self, path):
        names = ["fastapi"] if path.name == "requirements.txt" else ["Pillow"]
        return [{"pins": [{"name": name, "op": "", "want": ""} for name in names],
                 "includes": []}]

    def module_for(self, distribution):
        return distribution.lower().replace("-", "_")

    def probe(self, name, module):
        if name in self.missing:
            return {"ok": False, "status": "missing", "detail": "No module named %r" % module}
        return {"ok": True, "status": "ok", "version": "1.0.0"}

    def satisfies(self, version, op, want):
        return True

    def ocr_status(self):
        return {"ready": self.ocr_ready, "reason": "ready" if self.ocr_ready else "package_missing"}


def make_root(tmp: Path, *, with_requirements=True) -> Path:
    root = tmp / "project"
    root.mkdir(parents=True, exist_ok=True)
    if with_requirements:
        (root / "requirements.txt").write_text("fastapi==1.0.0\n", encoding="utf-8")
        (root / "requirements-ocr.txt").write_text("Pillow==1.0.0\n", encoding="utf-8")
    return root


def make_db(path: Path, rows=()) -> Path:
    """造一个只有必需列的最小 jobkit 库（真 schema 由 store 负责，这里只要够查）。"""
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("CREATE TABLE runtime_tasks (task_id TEXT PRIMARY KEY, tool TEXT,"
                           " effect TEXT NOT NULL, status TEXT NOT NULL)")
        connection.execute("CREATE TABLE runtime_send_attempts (attempt_id TEXT PRIMARY KEY,"
                           " state TEXT NOT NULL)")
        for index, (effect, status) in enumerate(rows):
            connection.execute("INSERT INTO runtime_tasks VALUES (?,?,?,?)",
                               ("t%d" % index, "runtime.applicant.chat.submit", effect, status))
        connection.commit()
    finally:
        connection.close()
    return path


def run_check(script, **kwargs):
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = script.check(echo_text=False, **kwargs)
    lines = [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]
    return code, lines


def by_step(lines, step):
    matches = [line for line in lines if line.get("step") == step]
    assert len(matches) == 1, "expected exactly one %r step, got %d" % (step, len(matches))
    return matches[0]


class ScriptShapeTest(unittest.TestCase):
    """脚本本身：复用别人的逻辑，且不出现启服务/写库的依赖。"""

    @classmethod
    def setUpClass(cls):
        cls.script = load_script()

    def test_reuses_check_deps_and_check_version_instead_of_reimplementing(self):
        module = self.script.load_sibling("check-deps.py")
        self.assertTrue(callable(module.parse_requirements))
        self.assertTrue(callable(module.probe))
        self.assertTrue(callable(module.ocr_status))
        version = self.script.load_sibling("check-version.py")
        self.assertTrue(callable(version.compare))
        self.assertTrue(callable(version.local_identity))

    def test_pending_external_query_matches_kernel_busy_predicate(self):
        """「没闭环的外部动作」必须与 runtime_tools 的繁忙判定同口径。"""
        source = self.script.OPEN_EXTERNAL_ACTION
        self.assertIn("effect='external_write'", source)
        self.assertIn("'executing'", source)
        self.assertIn("'outcome_unknown'", source)

    def test_script_does_not_start_service_or_write_database(self):
        source = SCRIPT.read_text(encoding="utf-8")
        # 只拦「真会做事」的写法：注释与文档里提到这些名字是说明，不算
        for forbidden in ("uvicorn", "serve_forever", "HTTPServer", "TestClient",
                          "store.runtime_db(", "store.connect(", "import store",
                          "import subprocess"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_script_importable_on_python_311(self):
        self.assertGreaterEqual(sys.version_info[:2], (3, 11))


class ReadyInstallTest(unittest.TestCase):
    """情形一：一切就绪 —— 服务在跑、版本一致、库可读、依赖齐全。"""

    @classmethod
    def setUpClass(cls):
        cls.script = load_script()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)
        self.root = make_root(self.tmp_path)
        self.db = make_db(self.tmp_path / "jobkit.db")
        self.disk = {"build_fingerprint": FINGERPRINT_NEW,
                     "files": {"app.py": "a" * 64, "store.py": "b" * 64},
                     "git_commit": COMMIT}

    def test_everything_ready_exits_ok(self):
        code, lines = run_check(
            self.script, root=self.root, db_path=self.db,
            http=make_http(version=version_payload()), probe=lambda *a, **k: True,
            local=lambda: self.disk, checker=FakeDeps(),
            browser_probe=lambda http=None: {"step": "browser", "port": 9222,
                                             "reachable": True, "browser": "Edg/1.0",
                                             "optional": True, "hint": None})
        self.assertEqual(code, 0)
        self.assertEqual([line["step"] for line in lines],
                         ["python", "deps", "port", "database", "browser", "version", "summary"])
        summary = lines[-1]
        self.assertEqual(summary["exit"], 0)
        self.assertIs(summary["ok"], True)
        self.assertEqual(summary["alerts"], [])
        self.assertTrue(summary["service_running"])
        self.assertEqual(summary["version_status"], "match")
        self.assertEqual(summary["pending_external"], 0)
        self.assertIn("一切就绪", summary["text"])
        self.assertIn("[OK] 运行版本", summary["text"])
        self.assertEqual(by_step(lines, "port")["jobkit"], True)
        self.assertEqual(by_step(lines, "version")["status"], "match")

    def test_missing_database_is_not_a_failure(self):
        """库还没建出来是正常状态（所在目录可写）：报告里说清楚，不当成问题。"""
        code, lines = run_check(
            self.script, root=self.root, db_path=self.tmp_path / "fresh.db",
            http=make_http(version=version_payload()), probe=lambda *a, **k: True,
            local=lambda: self.disk, checker=FakeDeps(),
            browser_probe=lambda http=None: {"step": "browser", "port": 9222,
                                             "reachable": False, "browser": None,
                                             "optional": True,
                                             "hint": "未开浏览器调试端口：只能用命令行与只读部分。"})
        database = by_step(lines, "database")
        self.assertIs(database["exists"], False)
        self.assertIsNone(database["pending_external"])
        self.assertIn("还没创建", lines[-1]["text"])
        self.assertIn("只能用 CLI 与只读功能", lines[-1]["text"])
        self.assertEqual(code, 0)

    def test_service_not_started_is_not_a_failure(self):
        """刚装好还没启动：0，并把「先启动服务」写在结论里。"""
        code, lines = run_check(
            self.script, root=self.root, db_path=self.tmp_path / "fresh.db",
            http=make_http(), probe=lambda *a, **k: False, checker=FakeDeps(),
            browser_probe=lambda http=None: {"step": "browser", "port": 9222,
                                             "reachable": False, "browser": None,
                                             "optional": True, "hint": None})
        self.assertEqual(code, 0)
        summary = lines[-1]
        self.assertEqual(summary["alerts"], [])
        self.assertIs(summary["service_running"], False)
        self.assertEqual(summary["version_status"], "skipped")
        self.assertIn("服务还没启动", summary["text"])
        self.assertIn("python main.py serve", summary["text"])

    def test_database_in_missing_directory_is_an_alert(self):
        """库所在目录都不存在 = 服务建不出库，这是要人处理的事。"""
        code, lines = run_check(
            self.script, root=self.root, db_path=self.tmp_path / "nope" / "jobkit.db",
            http=make_http(version=version_payload()), probe=lambda *a, **k: True,
            local=lambda: {"build_fingerprint": FINGERPRINT_NEW, "files": {},
                           "git_commit": COMMIT},
            checker=FakeDeps(),
            browser_probe=lambda http=None: {"step": "browser", "port": 9222,
                                             "reachable": False, "browser": None,
                                             "optional": True, "hint": None})
        self.assertEqual(code, 2)
        self.assertEqual(by_step(lines, "database")["writable_target"], "directory")
        self.assertIn("database_not_writable", lines[-1]["alerts"])


class PortConflictTest(unittest.TestCase):
    """情形二：端口被别的程序占用（/health 不是 jobkit）。"""

    @classmethod
    def setUpClass(cls):
        cls.script = load_script()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)
        self.root = make_root(self.tmp_path)

    def test_foreign_service_on_port_exits_alert_with_hint(self):
        code, lines = run_check(
            self.script, root=self.root, db_path=self.tmp_path / "jobkit.db",
            http=make_http(health_service="other-tool"), probe=lambda *a, **k: True,
            checker=FakeDeps(),
            browser_probe=lambda http=None: {"step": "browser", "port": 9222,
                                             "reachable": False, "browser": None,
                                             "optional": True, "hint": None})
        self.assertEqual(code, 2)
        port = by_step(lines, "port")
        self.assertIs(port["in_use"], True)
        self.assertIs(port["jobkit"], False)
        self.assertIn("被别的程序占用", port["reason"])
        summary = lines[-1]
        self.assertEqual(summary["alerts"], ["port_taken_by_other"])
        self.assertEqual(summary["version_status"], "skipped")   # 不是 jobkit → 无从比对
        self.assertIn("被别的程序占用", summary["text"])
        self.assertIn("--port", summary["text"])

    def test_jobkit_running_but_version_unreadable_is_an_alert(self):
        """jobkit 在跑却读不到版本（旧进程/未公开 /version）：要人处理，不是跳过。"""
        code, lines = run_check(
            self.script, root=self.root, db_path=self.tmp_path / "jobkit.db",
            http=make_http(version=None), probe=lambda *a, **k: True,
            checker=FakeDeps(),
            browser_probe=lambda http=None: {"step": "browser", "port": 9222,
                                             "reachable": False, "browser": None,
                                             "optional": True, "hint": None})
        self.assertEqual(code, 2)
        self.assertEqual(by_step(lines, "version")["status"], "unavailable")
        self.assertEqual(lines[-1]["alerts"], ["version_unavailable"])


class DatabaseTest(unittest.TestCase):
    """情形三：库不存在 / 库里有没闭环的外部动作（只读查询，不能写）。"""

    @classmethod
    def setUpClass(cls):
        cls.script = load_script()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)
        self.root = make_root(self.tmp_path)

    def test_open_external_action_is_reported_and_database_untouched(self):
        db = make_db(self.tmp_path / "jobkit.db",
                     rows=[("external_write", "outcome_unknown"),
                           ("external_write", "completed"),
                           ("state", "executing")])
        before = hashlib.sha256(db.read_bytes()).hexdigest()
        code, lines = run_check(
            self.script, root=self.root, db_path=db,
            http=make_http(health_service="other"), probe=lambda *a, **k: False,
            checker=FakeDeps(),
            browser_probe=lambda http=None: {"step": "browser", "port": 9222,
                                             "reachable": False, "browser": None,
                                             "optional": True, "hint": None})
        after = hashlib.sha256(db.read_bytes()).hexdigest()
        self.assertEqual(before, after, "体检不能改库（字节级一致）")
        self.assertEqual(code, 2)
        database = by_step(lines, "database")
        self.assertEqual(database["pending_external"], 1)   # 只算没闭环的那条
        self.assertIs(database["schema"], True)
        self.assertIs(database["writable"], True)
        self.assertIn("pending_external_action", lines[-1]["alerts"])
        self.assertIn("outcome list", lines[-1]["text"])

    def test_read_only_connection_does_not_create_side_rows(self):
        """只读打开：读完之后表里还是原来那些行。"""
        db = make_db(self.tmp_path / "jobkit.db", rows=[("state", "completed")])
        self.script.inspect_database(db)
        connection = sqlite3.connect(str(db))
        try:
            count = connection.execute("SELECT COUNT(*) FROM runtime_tasks").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(count, 1)

    def test_database_path_follows_jobkit_db_environment(self):
        """JOBKIT_DB 指定了库就查那个（多目录并行时别看成「数据全丢了」）。"""
        target = self.tmp_path / "elsewhere.db"
        with mock.patch.dict(os.environ, {"JOBKIT_DB": str(target)}):
            code, lines = run_check(
                self.script, root=self.root, db_path=None,
                http=make_http(), probe=lambda *a, **k: False, checker=FakeDeps(),
                browser_probe=lambda http=None: {"step": "browser", "port": 9222,
                                                 "reachable": False, "browser": None,
                                                 "optional": True, "hint": None})
        self.assertEqual(code, 0)
        self.assertEqual(by_step(lines, "database")["path"], str(target))
        self.assertEqual(lines[-1]["database"], str(target))

    def test_non_jobkit_database_is_flagged(self):
        stranger = self.tmp_path / "stranger.db"
        connection = sqlite3.connect(str(stranger))
        try:
            connection.execute("CREATE TABLE unrelated (x INTEGER)")
            connection.commit()
        finally:
            connection.close()
        info = self.script.inspect_database(stranger)
        self.assertIs(info["schema"], False)
        self.assertIn("jobkit", info["read_error"])


class VersionDriftTest(unittest.TestCase):
    """情形四：运行版本落后于磁盘 —— 结论必须是 ALERT(2)，并指出是哪个文件。"""

    @classmethod
    def setUpClass(cls):
        cls.script = load_script()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)
        self.root = make_root(self.tmp_path)
        self.db = make_db(self.tmp_path / "jobkit.db")

    def test_runtime_behind_disk_exits_alert(self):
        old = version_payload(fingerprint=FINGERPRINT_OLD,
                              files={"app.py": "a" * 64, "store.py": "0" * 64})
        disk = {"build_fingerprint": FINGERPRINT_NEW,
                "files": {"app.py": "a" * 64, "store.py": "b" * 64}, "git_commit": COMMIT}
        code, lines = run_check(
            self.script, root=self.root, db_path=self.db,
            http=make_http(version=old), probe=lambda *a, **k: True,
            local=lambda: disk, checker=FakeDeps(),
            browser_probe=lambda http=None: {"step": "browser", "port": 9222,
                                             "reachable": True, "browser": "Edg/1.0",
                                             "optional": True, "hint": None})
        self.assertEqual(code, 2)
        version = by_step(lines, "version")
        self.assertEqual(version["status"], "drift")
        self.assertIs(version["ok"], False)
        self.assertEqual([item["file"] for item in version["file_diffs"]], ["store.py"])
        self.assertIn("version_drift", lines[-1]["alerts"])
        self.assertIn("落后于磁盘", lines[-1]["text"])

    def test_disk_fingerprint_failure_is_not_reported_as_ok(self):
        """磁盘侧重算炸了 = 没结论，不能默默算成一致。"""
        def boom():
            raise OSError("cannot read files")
        code, lines = run_check(
            self.script, root=self.root, db_path=self.db,
            http=make_http(version=version_payload()), probe=lambda *a, **k: True,
            local=boom, checker=FakeDeps(),
            browser_probe=lambda http=None: {"step": "browser", "port": 9222,
                                             "reachable": False, "browser": None,
                                             "optional": True, "hint": None})
        self.assertEqual(code, 2)
        self.assertEqual(by_step(lines, "version")["status"], "unavailable")
        self.assertIn("version_unavailable", lines[-1]["alerts"])


class EnvironmentTest(unittest.TestCase):
    """环境与依赖缺口的折算：Python 太旧 → 2；requirements 读不到 → 1。"""

    @classmethod
    def setUpClass(cls):
        cls.script = load_script()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)

    def test_old_python_is_an_alert(self):
        root = make_root(self.tmp_path)
        code, lines = run_check(
            self.script, root=root, db_path=self.tmp_path / "jobkit.db",
            http=make_http(), probe=lambda *a, **k: False, checker=FakeDeps(),
            python_info=(3, 9, 7),
            browser_probe=lambda http=None: {"step": "browser", "port": 9222,
                                             "reachable": False, "browser": None,
                                             "optional": True, "hint": None})
        self.assertEqual(code, 2)
        self.assertEqual(by_step(lines, "python")["version"], "3.9.7")
        self.assertIn("python_too_old", lines[-1]["alerts"])
        self.assertIn("Python 太旧", lines[-1]["text"])

    def test_missing_requirements_file_is_an_error_not_an_alert(self):
        root = make_root(self.tmp_path, with_requirements=False)
        code, lines = run_check(
            self.script, root=root, db_path=self.tmp_path / "jobkit.db",
            http=make_http(), probe=lambda *a, **k: False, checker=FakeDeps(),
            browser_probe=lambda http=None: {"step": "browser", "port": 9222,
                                             "reachable": False, "browser": None,
                                             "optional": True, "hint": None})
        self.assertEqual(code, 1)
        self.assertIn("requirements_unreadable", lines[-1]["errors"])
        self.assertIn("[失败] 依赖", lines[-1]["text"])

    def test_missing_core_package_is_an_alert_but_ocr_is_optional(self):
        root = make_root(self.tmp_path)
        code, lines = run_check(
            self.script, root=root, db_path=self.tmp_path / "jobkit.db",
            http=make_http(), probe=lambda *a, **k: False,
            checker=FakeDeps(missing={"fastapi"}, ocr_ready=False),
            browser_probe=lambda http=None: {"step": "browser", "port": 9222,
                                             "reachable": False, "browser": None,
                                             "optional": True, "hint": None})
        self.assertEqual(code, 2)
        deps = by_step(lines, "deps")
        self.assertEqual([item["name"] for item in deps["missing"]], ["fastapi"])
        self.assertIs(deps["ocr_optional"], True)
        self.assertNotIn("ocr_missing", lines[-1]["alerts"])   # 默认 OCR 可选
        self.assertIn("缺 fastapi", lines[-1]["text"])

    def test_require_ocr_turns_missing_ocr_into_an_alert(self):
        root = make_root(self.tmp_path)
        code, lines = run_check(
            self.script, root=root, db_path=self.tmp_path / "jobkit.db",
            http=make_http(), probe=lambda *a, **k: False,
            checker=FakeDeps(ocr_ready=False), allow_missing_ocr=False,
            browser_probe=lambda http=None: {"step": "browser", "port": 9222,
                                             "reachable": False, "browser": None,
                                             "optional": True, "hint": None})
        self.assertEqual(code, 2)
        self.assertIn("ocr_missing", lines[-1]["alerts"])


class RealDependencyProbeTest(unittest.TestCase):
    """复用真 check-deps 的探针跑一遍本机 requirements（只读导入，不装包）。"""

    def test_real_requirements_produce_a_conclusive_report(self):
        script = load_script()
        report = script.check_dependencies(ROOT, ("requirements.txt",))
        self.assertEqual(report["step"], "deps")
        self.assertFalse(report["error"])
        names = [package["name"] for file in report["requirements"]
                 for package in file["packages"]]
        self.assertIn("fastapi", names)
        self.assertTrue(all("ok" in package for file in report["requirements"]
                            for package in file["packages"]))


if __name__ == "__main__":
    unittest.main()
