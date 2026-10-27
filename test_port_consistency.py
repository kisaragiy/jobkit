"""端口一致性门禁。

血教训：后端默认端口改成 18770，但 start.vbs / Electron / apply_blacklist.py
各自独立写死端口。只改一处 = 后端起来了、前端连不上，或者脚本直接用旧端口
调 API 而静默失败。这类错误没有任何报错，只是「东西不工作」。

这组测试只做一件事：**所有提到这个端口的地方必须一致**，且不许落在
Windows 的保留段里（本机 8720-8819 被系统锁死，bind 抛 WinError 10013
—— 表现是「端口被占用」，但 netstat 里一个进程都没有）。
"""
from __future__ import annotations

import re
import socket
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _reserved_port():
    """从 netsh 读一个当前真被 Windows 保留的端口；读不到返回 None。

    保留段是**动态的**（重启、Hyper-V、HNS 都会改），所以测试不能写死端口号
    —— 曾经把 8770 写死，段一变就从「验证正确」退化成「必然误报」。
    """
    try:
        # netsh 输出是 GBK：用 text=True 会按 utf-8 解码失败，stdout 直接变 None
        r = subprocess.run(
            ["netsh", "int", "ipv4", "show", "excludedportrange", "protocol=tcp"],
            capture_output=True, timeout=15,
        )
        out = (r.stdout or b"").decode("gbk", errors="replace")
    except Exception:  # noqa: BLE001
        return None
    for line in out.splitlines():
        m = re.search(r"^\s*(\d{3,5})\s+(\d{3,5})\s*$", line)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if hi > lo:
                return lo + 1
    return None

# 每个文件里端口的写法不同，用正则各自提取，避免把一个数字写三遍。
SPECS = {
    "app.py": r'add_argument\("--port",\s*type=int,\s*default=(\d+)',
    "apply_blacklist.py": r'API\s*=\s*"http://127\.0\.0\.1:(\d+)"',
    "start.vbs": r"backend\s+127\.0\.0\.1:(\d+)",
    "backend.vbs": r"port\s+:\s*http://127\.0\.0\.1:(\d+)",
    "shell/main.js": r"JOBKIT_URL\s*=.*?127\.0\.0\.1:(\d+)",
}


def port_in(rel: str) -> int:
    text = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
    m = re.search(SPECS[rel], text)
    if not m:
        raise AssertionError(f"{rel} 里找不到端口（正则过期了？）")
    return int(m.group(1))


class TestPortConsistency(unittest.TestCase):
    def test_renderer_uses_its_serving_origin(self):
        text = (ROOT / "shell/renderer/index.html").read_text(encoding="utf-8")
        self.assertIn('src="/workspace"', text)
        self.assertNotIn('http://127.0.0.1:', text)

    def test_all_files_agree(self):
        ports = {}
        for rel in SPECS:
            if (ROOT / rel).exists():
                ports[rel] = port_in(rel)
        distinct = set(ports.values())
        self.assertEqual(len(distinct), 1,
                         f"端口不一致，前端会连不上后端：{ports}")
        self.assertNotEqual(distinct.pop(), 0)

    def test_no_stale_port_left(self):
        """除了已知的这几处，源码里不该再出现 8770（旧的、被保留段锁死的端口）。"""
        bad = []
        me = Path(__file__).name          # 本文件要拿 8770 当反例，跳过自己
        for p in ROOT.rglob("*"):
            if not p.is_file() or p.suffix not in {".py", ".vbs", ".js", ".html", ".md"}:
                continue
            if p.name == me:
                continue
            if any(part in {".git", "node_modules", "tmp", "__pycache__", ".pytest_cache"}
                   or part.startswith(".browser_profile")   # 浏览器用户数据目录（含第三方扩展 JS，不是我们的源码）
                   for part in p.parts):
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            # 关键：不能用 "8770" in text —— "18770" 里也含 "8770"，
            # 新端口会被自己判成旧的。必须按数字边界匹配。
            if re.search(r"(?<!\d)8770(?!\d)", text):
                bad.append(str(p.relative_to(ROOT)))
        self.assertEqual(bad, [], f"这些文件还写着 8770（已被 Windows 保留段锁死）：{bad}")

    @unittest.skipUnless(sys.platform == "win32", "Windows 保留段才有这个问题")
    def test_default_port_not_reserved(self):
        """默认端口不能落在系统保留段。

        注意不能要求它「现在可以 bind」—— 服务正在跑的时候自然绑不上，
        那是有意的（单实例守卫）。判据只是：不是 reserved。
        """
        import app as A
        port = port_in("app.py")
        self.assertNotEqual(A._port_state("127.0.0.1", port), "reserved",
                            f"默认端口 {port} 落在 Windows 保留段里，换一个")


class TestPortStateClassification(unittest.TestCase):
    """reserved 和 busy 必须分开：用户看到「被占用」却查不到任何程序 = 无解。"""

    def test_reserved_and_busy_are_distinct(self):
        import app as A
        # 保留段是动态的：现场从 netsh 取一个真被保留的端口，取不到就跳过
        reserved = _reserved_port()
        if reserved is None:
            self.skipTest("本机当前没有 Windows 保留端口段，无法验证 reserved 分支")
        self.assertEqual(A._port_state("127.0.0.1", reserved), "reserved")
        # 真被占用：自己先 listen 一个
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.listen(1)
        try:
            self.assertEqual(A._port_state("127.0.0.1", port), "busy")
        finally:
            s.close()
        # 空着的端口（listen 关掉后立刻可能是 TIME_WAIT，换个高位号更稳）
        free = A._pick_port("127.0.0.1", 18900)
        self.assertIsNotNone(free)

    def test_pick_port_skips_reserved(self):
        import app as A
        got = A._pick_port("127.0.0.1", 8770)      # 起点在保留段里
        self.assertIsNotNone(got)
        self.assertNotEqual(A._port_state("127.0.0.1", got), "reserved")


if __name__ == "__main__":
    unittest.main()
