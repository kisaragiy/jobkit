#!/usr/bin/env python3
"""独立性守卫：jobkit 不得在运行时依赖 GetJob（2026-10-19 user 定的第一原则）。

判据是机械的、可证伪的：
  1. 任何 .py / .js 源码，**剔除注释与文档字符串后**，不得出现 `getjob`；
  2. 例外只有 docs/ 与 tmp/（参考资料，不参与运行）；
  3. 这条测试自己也会被扫描，所以它用拼接构造关键字，避免自指。

为什么用 tokenize 而不是 grep：
  `dom/boss_selectors.py` 的模块 docstring 里写了出处路径
  （`tmp/getjob231/platforms/boss/selectors.js`）——那是**参考资料索引**，
  不是依赖。grep 会把它误判成依赖，tokenize 分得清「字符串/注释」和「代码」。
"""
from __future__ import annotations

import io
import re
import tokenize
import unittest
from pathlib import Path

ROOT = Path(__file__).parent
# 拼接构造：避免本文件自身被规则命中
NEEDLE = "get" + "job"
NEEDLE_RE = re.compile(NEEDLE, re.I)

# 参考资料目录：允许出现
SKIP_DIRS = {
    "tmp", "docs", "knowledge", "logs", "data", ".git", "__pycache__",
    ".browser_profile", "node_modules", ".venv", "venv",
}


def _iter_sources():
    me = Path(__file__).name
    for p in ROOT.rglob("*"):
        if p.is_dir() or p.suffix not in {".py", ".js"}:
            continue
        if p.name == me:
            continue            # 守卫不审自己：本文件用拼接构造关键字，本身不含 needle
        if SKIP_DIRS & set(p.relative_to(ROOT).parts):
            continue
        yield p


def _py_code_only(path: Path) -> str:
    """去掉注释、字符串、docstring，只留真正的代码 token。"""
    out = []
    with open(path, "rb") as f:
        try:
            for tok in tokenize.tokenize(f.readline):
                if tok.type in (tokenize.COMMENT, tokenize.STRING,
                                tokenize.FSTRING_MIDDLE if hasattr(tokenize, "FSTRING_MIDDLE") else -1):
                    continue
                if tok.type in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT,
                                tokenize.DEDENT, tokenize.ENCODING, tokenize.ENDMARKER):
                    continue
                out.append(tok.string)
        except (tokenize.TokenError, SyntaxError, IndentationError, UnicodeDecodeError):
            # 解析不了就退回原文（宁可误报，不可漏报）
            return path.read_text(encoding="utf-8", errors="replace")
    return " ".join(out)


def _js_code_only(path: Path) -> str:
    """粗粒度剥 JS 注释（本仓库的 JS 没有花哨语法，够用）。"""
    src = path.read_text(encoding="utf-8", errors="replace")
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    src = re.sub(r"(?m)^\s*//.*$", " ", src)
    src = re.sub(r"(?<![:\"'])//[^\n]*$", " ", src, flags=re.M)
    return src


class GetJobIndependenceTest(unittest.TestCase):
    def test_no_runtime_reference_to_getjob(self):
        offenders = []
        for path in _iter_sources():
            code = _py_code_only(path) if path.suffix == ".py" else _js_code_only(path)
            if NEEDLE_RE.search(code):
                # 找出具体命中行，方便修
                for i, line in enumerate(code.splitlines(), 1):
                    if NEEDLE_RE.search(line):
                        offenders.append(f"{path.relative_to(ROOT)} → {line.strip()[:100]}")
                        break
        self.assertEqual(
            offenders, [],
            "发现对 GetJob 的运行时引用（注释与 docs/ 之外）：\n  " + "\n  ".join(offenders),
        )

    def test_scanner_actually_detects_a_fake_dependency(self):
        """反向自检：扫描器必须能抓到真依赖，否则这条守卫是摆设。"""
        tmp = ROOT / "_independence_probe.py"
        try:
            # 用 import 语句造一个「真依赖」：tokenize 后 NAME token 里会留下 needle
            tmp.write_text(f"import {NEEDLE}_helper\n", encoding="utf-8")
            self.assertTrue(NEEDLE_RE.search(_py_code_only(tmp)), "扫描器漏报：它识别不出真依赖")
        finally:
            tmp.unlink(missing_ok=True)

    def test_comments_and_docstrings_are_ignored(self):
        """正反两面：注释里写出处是允许的，代码里出现才算依赖。"""
        tmp = ROOT / "_independence_probe2.py"
        try:
            tmp.write_text(
                f'"""出处：tmp/{NEEDLE}231/platforms/boss/x.js"""\n'
                f"# 上面这行只是注释\nVALUE = 1\n",
                encoding="utf-8",
            )
            self.assertIsNone(NEEDLE_RE.search(_py_code_only(tmp)), "注释/文档字符串被误判成依赖")
        finally:
            tmp.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
