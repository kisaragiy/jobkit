"""图标门禁：保证映射表里的每个图标名都真实存在。

踩过的坑：BTN_ICONS 里写了 rGo: 'search'，但 ICON_PATHS 只有 'jd' 没有
'search'。paintButtonIcons 里 `if (p)` 静默跳过 —— 界面上那个按钮就是
没图标，没报错、没日志，只有盯着看才发现。

所以这里把「映射的名字必须在图标表里」变成可执行断言。
"""
import re
import unittest
from pathlib import Path

HTML = Path(__file__).with_name("web") / "index.html"


def _block(text, name):
    """取出 `const NAME = { ... };` 的块内容。"""
    m = re.search(
        r"const\s+%s\s*=\s*\{(.*?)\n\};" % re.escape(name), text, re.S)
    if not m:
        raise AssertionError(f"找不到 {name} 定义")
    return m.group(1)


def _keys(block):
    """取块里 `键:` 形式的键（图标名）。"""
    return set(re.findall(r"^\s*([A-Za-z][A-Za-z0-9_]*)\s*:", block, re.M))


def _pairs(block):
    """取块里 `键: '值'` 形式的映射。"""
    return dict(re.findall(
        r"([A-Za-z][A-Za-z0-9_]*)\s*:\s*'([a-z][a-z0-9_]*)'", block))


class TestIconTables(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = HTML.read_text(encoding="utf-8")
        cls.paths = _keys(_block(cls.text, "ICON_PATHS"))
        cls.btns = _pairs(_block(cls.text, "BTN_ICONS"))

    def test_html_exists(self):
        self.assertTrue(HTML.exists(), f"{HTML} 不存在")

    def test_icon_paths_not_empty(self):
        self.assertGreaterEqual(len(self.paths), 20)

    def test_every_button_icon_exists(self):
        """BTN_ICONS 引用的每个图标名，必须在 ICON_PATHS 里有定义。"""
        missing = {k: v for k, v in self.btns.items() if v not in self.paths}
        self.assertEqual(
            missing, {},
            f"这些按钮指向了不存在的图标名（界面上会静默没图标）：{missing}")

    def test_every_tab_icon_exists(self):
        """工具入口 data-t 必须有对应图标。"""
        tabs = set(re.findall(r'data-t="([a-z]+)"', self.text))
        missing = {t for t in tabs if t not in self.paths}
        self.assertEqual(missing, set(), f"这些工具入口没有图标：{missing}")

    def test_icon_svg_wellformed(self):
        """图标路径必须是合法的 SVG 片段（成对标签、无裸引号）。"""
        block = _block(self.text, "ICON_PATHS")
        paths = re.findall(r"^\s*[a-z]+:\s*'([^']*)'", block, re.M)
        self.assertGreater(len(paths), 20)
        for p in paths:
            self.assertTrue(p.strip(), "有空图标路径")
            # 每个 < 都要有对应的 >
            self.assertEqual(p.count("<"), p.count(">"),
                             f"标签不成对: {p[:40]}")

    def test_observer_present(self):
        """重渲染后补图标的 observer 不能丢 —— 丢了按钮切面板后就没图标。"""
        self.assertIn("MutationObserver", self.text)
        self.assertIn("paintButtonIcons", self.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
