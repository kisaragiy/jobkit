"""脚本输出契约的编码健壮性。

背景：CI（windows-latest）上 stdout 是 charmap，`scripts/check-deps.py` 打中文时抛
`UnicodeEncodeError`，让一次正常检查变成「脚本崩了」。契约库必须自己扛住编码，
不能要求调用方记得设 PYTHONIOENCODING。
"""
import os
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent


class ContractEncodingTest(unittest.TestCase):
    def run_emit(self, io_encoding: str):
        code = ("from scripts.lib.contract import emit\n"
                "emit('probe', message='中文输出与 emoji 🐈‍⬛', ok=True)\n")
        env = {**os.environ, "PYTHONIOENCODING": io_encoding}
        return subprocess.run([sys.executable, "-c", code], capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              env=env, cwd=str(ROOT), timeout=60)

    def test_emit_survives_ascii_stdio(self):
        """ASCII 管道也必须退出 0：编码不支持时转义输出，而不是抛异常。"""
        done = self.run_emit("ascii")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("probe", done.stdout)

    def test_emit_keeps_readable_chinese_on_utf8_stdio(self):
        done = self.run_emit("utf-8")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("中文输出", done.stdout)

    def test_contract_selfcheck_exits_ok(self):
        done = subprocess.run([sys.executable, "scripts/lib/contract.py"],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", cwd=str(ROOT), timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('"step": "contract"', done.stdout)


if __name__ == "__main__":
    unittest.main()
