"""chat_send 单测 —— 全部 stub CDP，不碰真浏览器、不发任何消息。"""
from __future__ import annotations

import unittest
from unittest import mock

import chat_send as CS


class PickRowTest(unittest.TestCase):
    ROWS = [
        {"i": 0, "text": "11:59 张女士 领亚电子 招聘主管 [已读] 对不起，我觉得该职位不太适合我"},
        {"i": 1, "text": "2小时前 庞女士 中软国际 招聘专员 你目前在深圳吗 可以现场面试吗 离职了吗"},
        {"i": 2, "text": "16小时前 谢女士 四箐园 面试邀请"},
    ]

    def test_unique_hit(self):
        self.assertEqual(CS.pick_row(self.ROWS, "中软国际"), 1)

    def test_missing_raises_skip(self):
        with self.assertRaises(CS.SendSkip):
            CS.pick_row(self.ROWS, "不存在的公司")

    def test_ambiguous_raises_skip(self):
        rows = self.ROWS + [{"i": 3, "text": "昨天 中软国际 田女士 拒信"}]
        with self.assertRaises(CS.SendSkip) as ctx:
            CS.pick_row(rows, "中软国际")
        self.assertIn("2 个会话", str(ctx.exception))

    def test_empty_needle(self):
        with self.assertRaises(CS.SendSkip):
            CS.pick_row(self.ROWS, "  ")

    def test_matches_hr_name(self):
        self.assertEqual(CS.pick_row(self.ROWS, "谢女士"), 2)


class FillInputTest(unittest.TestCase):
    def test_empty_text_rejected(self):
        with self.assertRaises(CS.SendError):
            CS.fill_input(None, [0], "   ")

    def test_landed_text_returned(self):
        calls = []

        def fake_eval(ws, mid, expr):
            calls.append(expr)
            if "innerText || '').trim()" in expr:
                return "你好，我目前在深圳"
            return True

        with mock.patch.object(CS, "_eval", side_effect=fake_eval):
            got = CS.fill_input(object(), [0], "你好，我目前在深圳")
        self.assertEqual(got, "你好，我目前在深圳")


class SendFlowTest(unittest.TestCase):
    def _run(self, *, dry_run, eval_script, keep_text=False):
        """eval_script: 对表达式返回值的函数。"""
        created = []

        class FakeWs:
            def close(self):
                created.append("closed")

        def fake_conn(url, **kw):
            return FakeWs()

        evals = []

        def fake_eval(ws, mid, expr):
            evals.append(expr)
            return eval_script(expr)

        with mock.patch.object(CS, "_cdp_target",
                               return_value={"webSocketDebuggerUrl": "ws://fake"}), \
             mock.patch.object(CS.websocket, "create_connection", side_effect=fake_conn), \
             mock.patch.object(CS, "_eval", side_effect=fake_eval), \
             mock.patch.object(CS, "_call", return_value={}), \
             mock.patch("sandbox.require_registered"):
            out = CS.send("中软国际", "好的，我目前在深圳，随时可以现场面试",
                          dry_run=dry_run, keep_text=keep_text)
        return out, evals, created

    def test_dry_run_never_clicks_send(self):
        def script(expr):
            if "user-list-content li" in expr and "map" in expr:
                return [{"i": 0, "text": "中软国际 庞女士 你好"}]
            if "getBoundingClientRect" in expr and "%d" in expr:
                return {"x": 10, "y": 20}
            if "getBoundingClientRect" in expr:
                return {"x": 100, "y": 200}
            if "r.width > 0 && r.height > 0" in expr:
                return True
            if "innerText || '').trim()" in expr:
                return "好的，我目前在深圳"
            if "btn-send" in expr and "classList" in expr:
                return False  # 已解除 disabled
            if "insertText" in expr:
                return True
            return True

        out, evals, created = self._run(dry_run=True, eval_script=script)
        self.assertTrue(out["ok"])
        self.assertTrue(out["dry_run"])
        self.assertTrue(out["send_enabled"])
        self.assertEqual(out["conversation"]["index"], 0)
        # 干跑绝不能走到点发送
        self.assertFalse(any("count" in e and "item-myself" in e for e in evals))
        self.assertEqual(created, ["closed"])

    def test_fill_mode_keeps_text(self):
        """--fill：填好留着让人自己点发送。不清空，也不点按钮。"""
        def script(expr):
            if "user-list-content li" in expr and "map" in expr:
                return [{"i": 0, "text": "中软国际 庞女士 你好"}]
            if "getBoundingClientRect" in expr and "%d" in expr:
                return {"x": 10, "y": 20}
            if "getBoundingClientRect" in expr:
                return {"x": 100, "y": 200}
            if "r.width > 0 && r.height > 0" in expr:
                return True
            if "innerText || '').trim()" in expr:
                return "好的，我目前在深圳"
            if "btn-send" in expr and "classList" in expr:
                return False
            if "insertText" in expr:
                return True
            return True

        out, evals, _ = self._run(dry_run=True, eval_script=script, keep_text=True)
        self.assertTrue(out.get("kept"), "keep_text 应标记 kept")
        self.assertNotIn("cleared", out, "keep_text 不能清空输入框")
        self.assertFalse(any("item-myself" in e and "count" in e for e in evals),
                         "干跑绝不能点发送")

    def test_send_disabled_aborts(self):
        def script(expr):
            if "user-list-content li" in expr and "map" in expr:
                return [{"i": 0, "text": "中软国际 庞女士 你好"}]
            if "getBoundingClientRect" in expr and "%d" in expr:
                return {"x": 10, "y": 20}
            if "getBoundingClientRect" in expr:
                return {"x": 100, "y": 200}
            if "r.width > 0 && r.height > 0" in expr:
                return True
            if "innerText || '').trim()" in expr:
                return "内容"
            if "btn-send" in expr and "classList" in expr:
                return True  # 仍 disabled
            return True

        with self.assertRaises(CS.SendError) as ctx:
            self._run(dry_run=False, eval_script=script)
        self.assertIn("disabled", str(ctx.exception))


class VerifySentTest(unittest.TestCase):
    def test_local_echo_is_not_platform_delivery_confirmation(self):
        state = {"n": 3}

        def fake_eval(ws, mid, expr):
            if "message-tip-bar" in expr:
                return ""
            if "item-myself" in expr and "innerText" in expr:
                return "好的，我目前在深圳，随时可以现场面试"
            if "length" in expr:
                return state["n"]
            return None

        with mock.patch.object(CS, "_eval", side_effect=fake_eval):
            out = CS.verify_sent(object(), [0], "好的，我目前在深圳，随时可以现场面试", 2, timeout=0.5)
        self.assertFalse(out["ok"])
        self.assertEqual(out["evidence"], "local_echo")
        self.assertEqual(out["outcome"], "outcome_unknown")

    def test_same_prefix_different_message_is_not_success(self):
        def fake_eval(ws, mid, expr):
            if "message-tip-bar" in expr:
                return ""
            if "item-myself" in expr and "innerText" in expr:
                return "SAME-PREFIX-123456-different"
            return 3
        with mock.patch.object(CS, "_eval", side_effect=fake_eval):
            with self.assertRaisesRegex(CS.SendError, "OUTCOME_UNKNOWN"):
                CS.verify_sent(object(), [0], "SAME-PREFIX-123456-original", 2, timeout=0.01)

    def test_page_error_wins_over_matching_local_echo(self):
        def fake_eval(ws, mid, expr):
            return "发送失败" if "message-tip-bar" in expr else "正文"
        with mock.patch.object(CS, "_eval", side_effect=fake_eval):
            with self.assertRaisesRegex(CS.SendError, "发送失败"):
                CS.verify_sent(object(), [0], "正文", 0, timeout=0.01)

    def test_failure_surfaces_page_tip(self):
        def fake_eval(ws, mid, expr):
            if "message-tip-bar" in expr:
                return "发送过于频繁，请稍后再试"
            if "item-myself" in expr and "innerText" in expr:
                return ""
            if "length" in expr:
                return 3
            return None

        with mock.patch.object(CS, "_eval", side_effect=fake_eval):
            with self.assertRaises(CS.SendError) as ctx:
                CS.verify_sent(object(), [0], "内容", 3, timeout=0.4)
        self.assertIn("发送过于频繁", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
