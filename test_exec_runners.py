"""执行入口单测（stdlib unittest，零外部依赖、零网络、零真实投递）。

覆盖：未到点不投 / 到点 dry-run 只打印 / 同一槽位不重复投 / 单实例锁互斥 / 退出码契约。
真实发送一律用 stub（apply_fn / sender / gate_fn），任何测试都不许碰 apply_pipeline.main()
或 BOSS 网络。

跑法：python -m unittest test_exec_runners
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import followup as F
import run_followup
import run_schedule
import schedule as S


def ms(y, mo, d, h=0, mi=0, s=0) -> int:
    """本地时间 → 毫秒时间戳（与 schedule/followup 内部一致）。"""
    return int(round(datetime(y, mo, d, h, mi, s).timestamp() * 1000))


def daily_slot(time_str="09:30", quantity=30, image_resume=0, enabled=1, sid="s-daily"):
    return {"id": sid, "time": time_str, "repeat": "daily", "quantity": quantity,
            "enabled": enabled, "image_resume": image_resume}


def once_slot(date="2026-10-19", time_str="09:30", quantity=10, sid="s-once"):
    return {"id": sid, "time": time_str, "repeat": "once", "date": date,
            "quantity": quantity, "enabled": 1, "image_resume": 0}


class _TmpCase(unittest.TestCase):
    """每个测试一个临时目录：槽位文件、状态、锁全隔离。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jobkit_exec_"))
        self.sched_path = self.tmp / "schedule.json"
        self.state_path = self.tmp / "exec_state.json"
        self.lock_path = self.tmp / "exec_runner.lock"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_slots(self, slots):
        self.sched_path.write_text(
            json.dumps({"slots": slots, "runs": []}, ensure_ascii=False), encoding="utf-8")

    def read_sched(self) -> dict:
        return json.loads(self.sched_path.read_text(encoding="utf-8"))

    def cycle(self, **kwargs):
        buf: list[str] = []
        kwargs.setdefault("schedule_path", self.sched_path)
        kwargs.setdefault("state_path", self.state_path)
        code = run_schedule.run_cycle(out=buf.append, **kwargs)
        return code, "\n".join(buf)


# ══════════════════════════════════════════════════════════════
# ① 未到点不投递
# ══════════════════════════════════════════════════════════════
class NotDueTest(_TmpCase):
    def test_before_time_does_not_send(self):
        self.write_slots([daily_slot()])
        calls = []
        code, out = self.cycle(send=True, apply_fn=lambda argv: calls.append(argv) or 0,
                               now_ms=ms(2026, 10, 19, 9, 0))
        self.assertEqual(code, run_schedule.EXIT_OK)
        self.assertEqual(calls, [])
        self.assertIn("没有到点", out)
        self.assertIn("下一次", out)

    def test_late_beyond_miss_window_does_not_send(self):
        """09:30 的槽位，09:36 才跑：超过 5 分钟窗口，宁可不投（不补跑）。"""
        self.write_slots([daily_slot()])
        calls = []
        code, out = self.cycle(send=True, apply_fn=lambda argv: calls.append(argv) or 0,
                               now_ms=ms(2026, 10, 19, 9, 36))
        self.assertEqual(code, run_schedule.EXIT_OK)
        self.assertEqual(calls, [])

    def test_disabled_slot_is_ignored(self):
        self.write_slots([daily_slot(enabled=0)])
        calls = []
        code, _ = self.cycle(send=True, apply_fn=lambda argv: calls.append(argv) or 0,
                             now_ms=ms(2026, 10, 19, 9, 31))
        self.assertEqual(code, run_schedule.EXIT_OK)
        self.assertEqual(calls, [])

    def test_expired_once_slot_is_ignored(self):
        self.write_slots([once_slot(date="2026-10-18")])
        calls = []
        code, _ = self.cycle(send=True, apply_fn=lambda argv: calls.append(argv) or 0,
                             now_ms=ms(2026, 10, 19, 9, 31))
        self.assertEqual(code, run_schedule.EXIT_OK)
        self.assertEqual(calls, [])

    def test_no_slots_at_all_is_ok_not_error(self):
        """空配置不是故障：退出码 0，绝不能用 1（cron 会当成脚本坏了）。"""
        code, out = self.cycle(send=True, apply_fn=lambda argv: 0,
                               now_ms=ms(2026, 10, 19, 9, 31))
        self.assertEqual(code, run_schedule.EXIT_OK)
        self.assertIn("没有配置任何槽位", out)


# ══════════════════════════════════════════════════════════════
# ② 到点 dry-run：只打印，不发送、不写状态
# ══════════════════════════════════════════════════════════════
class DryRunTest(_TmpCase):
    def test_due_slot_prints_plan_without_sending(self):
        self.write_slots([daily_slot(quantity=12)])
        calls = []
        code, out = self.cycle(send=False, apply_fn=lambda argv: calls.append(argv) or 0,
                               now_ms=ms(2026, 10, 19, 9, 31))
        self.assertEqual(code, run_schedule.EXIT_OK)
        self.assertEqual(calls, [], "dry-run 不许调用投递")
        self.assertIn("apply_pipeline.py --limit 12", out)
        self.assertIn("dry-run", out)

        data = self.read_sched()
        self.assertEqual(data["runs"], [], "dry-run 不许写 runs")
        self.assertNotIn("last_run_ms", data["slots"][0], "dry-run 不许标记已执行")
        self.assertFalse(self.state_path.exists(), "dry-run 不许写尝试台账")

    def test_image_resume_slot_is_flagged(self):
        self.write_slots([daily_slot(image_resume=1)])
        code, out = self.cycle(send=False, now_ms=ms(2026, 10, 19, 9, 31))
        self.assertEqual(code, run_schedule.EXIT_OK)
        self.assertIn("图片简历", out)

    def test_zero_quantity_slot_is_skipped(self):
        self.write_slots([daily_slot(quantity=0)])
        calls = []
        code, out = self.cycle(send=True, apply_fn=lambda argv: calls.append(argv) or 0,
                               now_ms=ms(2026, 10, 19, 9, 31))
        self.assertEqual(code, run_schedule.EXIT_OK)
        self.assertEqual(calls, [])
        self.assertIn("quantity=0", out)


# ══════════════════════════════════════════════════════════════
# ③ 幂等：同一槽位同一时间点不重复投
# ══════════════════════════════════════════════════════════════
class IdempotencyTest(_TmpCase):
    def test_same_slot_is_not_sent_twice(self):
        self.write_slots([daily_slot(quantity=5)])
        calls = []
        now = ms(2026, 10, 19, 9, 31)

        code, _ = self.cycle(send=True, apply_fn=lambda argv: calls.append(argv) or 0, now_ms=now)
        self.assertEqual(code, run_schedule.EXIT_OK)
        self.assertEqual(len(calls), 1, "第一次应该投")
        self.assertEqual(calls[0], ["--limit", "5"])

        data = self.read_sched()
        self.assertEqual(data["slots"][0]["last_run_ms"], now, "成功后必须回写 last_run_ms")
        self.assertEqual(len(data["runs"]), 1)

        code2, out2 = self.cycle(send=True, apply_fn=lambda argv: calls.append(argv) or 0, now_ms=now)
        self.assertEqual(code2, run_schedule.EXIT_OK)
        self.assertEqual(len(calls), 1, "同一波不许投第二次")
        self.assertIn("没有到点", out2)

    def test_next_day_daily_slot_fires_again(self):
        """幂等只挡同一波，不挡明天（否则定时就没意义了）。"""
        self.write_slots([daily_slot(quantity=5)])
        calls = []
        self.cycle(send=True, apply_fn=lambda argv: calls.append(argv) or 0,
                   now_ms=ms(2026, 10, 19, 9, 31))
        self.cycle(send=True, apply_fn=lambda argv: calls.append(argv) or 0,
                   now_ms=ms(2026, 10, 20, 9, 31))
        self.assertEqual(len(calls), 2)

    def test_retry_cap_stops_hammering(self):
        """失败不标记槽位（窗口内可重试），但同一波最多试 2 次，之后退出 2 等人。"""
        self.write_slots([daily_slot()])
        calls = []
        now = ms(2026, 10, 19, 9, 31)
        for _ in range(2):
            code, _ = self.cycle(send=True, apply_fn=lambda argv: calls.append(argv) or 2, now_ms=now)
            self.assertEqual(code, run_schedule.EXIT_ALERT)

        code, out = self.cycle(send=True, apply_fn=lambda argv: calls.append(argv) or 2, now_ms=now)
        self.assertEqual(len(calls), 2, "超过重试上限后不许再调投递")
        self.assertEqual(code, run_schedule.EXIT_ALERT)
        self.assertIn("不再重试", out)


# ══════════════════════════════════════════════════════════════
# ④ 单实例锁互斥
# ══════════════════════════════════════════════════════════════
class LockTest(_TmpCase):
    def test_second_acquire_fails_until_release(self):
        ok, why = run_schedule.acquire_lock(self.lock_path)
        self.assertTrue(ok, why)

        ok2, why2 = run_schedule.acquire_lock(self.lock_path)
        self.assertFalse(ok2, "同一份锁第二次必须拿不到")
        self.assertIn("正在跑", why2)

        info = json.loads(self.lock_path.read_text(encoding="utf-8"))
        self.assertEqual(info["pid"], os.getpid())
        self.assertIn("at", info)

        run_schedule.release_lock(self.lock_path)
        self.assertFalse(self.lock_path.exists())
        ok3, _ = run_schedule.acquire_lock(self.lock_path)
        self.assertTrue(ok3)
        run_schedule.release_lock(self.lock_path)

    def test_schedule_and_followup_share_one_lock(self):
        """两个 runner 不能并发 —— 必须是同一个锁文件，不是各管各的。"""
        self.assertEqual(run_followup.LOCK_FILE, run_schedule.LOCK_FILE)
        ok, _ = run_schedule.acquire_lock(self.lock_path)
        self.assertTrue(ok)
        try:
            ok2, why2 = run_followup.acquire_lock(self.lock_path)
            self.assertFalse(ok2, "run_schedule 拿着锁时 run_followup 必须被挡")
            self.assertIn("run_schedule.py", why2)
        finally:
            run_schedule.release_lock(self.lock_path)

    def test_dead_pid_lock_is_reclaimed(self):
        self.lock_path.write_text(json.dumps({"pid": 99999999, "at": time.time()}),
                                  encoding="utf-8")
        ok, why = run_schedule.acquire_lock(self.lock_path)
        self.assertTrue(ok, f"死进程的锁应该能抢占：{why}")
        run_schedule.release_lock(self.lock_path)

    def test_stale_lock_is_reclaimed(self):
        self.lock_path.write_text(
            json.dumps({"pid": os.getpid(), "at": time.time() - 7200}), encoding="utf-8")
        ok, why = run_schedule.acquire_lock(self.lock_path)
        self.assertTrue(ok, f"超过 1 小时的锁视为陈尸：{why}")
        run_schedule.release_lock(self.lock_path)

    def test_cli_skips_silently_when_locked(self):
        self.write_slots([daily_slot()])
        ok, _ = run_schedule.acquire_lock(self.lock_path)
        self.assertTrue(ok)
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = run_schedule.main([
                    "--once", "--send",
                    "--schedule", str(self.sched_path),
                    "--state", str(self.state_path),
                    "--lock", str(self.lock_path),
                    "--now-ms", str(ms(2026, 10, 19, 9, 31)),
                ])
            self.assertEqual(code, 0, "锁被占是设计的一部分，不是故障")
            self.assertIn("跳过", buf.getvalue())
        finally:
            run_schedule.release_lock(self.lock_path)


# ══════════════════════════════════════════════════════════════
# ⑤ 退出码契约：0=OK 2=ALERT 1=ERROR
# ══════════════════════════════════════════════════════════════
class ExitCodeTest(_TmpCase):
    def test_gate_refusal_is_alert_2(self):
        self.write_slots([daily_slot()])
        code, out = self.cycle(send=True, apply_fn=lambda argv: 3,
                               now_ms=ms(2026, 10, 19, 9, 31))
        self.assertEqual(code, 2)
        self.assertIn("通道门禁", out)
        data = self.read_sched()
        self.assertEqual(data["runs"], [], "被拒不算执行过")
        attempts = json.loads(self.state_path.read_text(encoding="utf-8"))["attempts"]
        self.assertEqual(attempts[-1]["result"], "blocked")

    def test_login_failure_is_alert_2(self):
        self.write_slots([daily_slot()])
        code, _ = self.cycle(send=True, apply_fn=lambda argv: 2,
                             now_ms=ms(2026, 10, 19, 9, 31))
        self.assertEqual(code, 2)

    def test_pipeline_exception_is_error_1(self):
        self.write_slots([daily_slot()])

        def boom(argv):
            raise RuntimeError("apply_pipeline 内部炸了")

        code, out = self.cycle(send=True, apply_fn=boom, now_ms=ms(2026, 10, 19, 9, 31))
        self.assertEqual(code, 1)
        self.assertIn("抛异常", out)

    def test_pipeline_exit_1_is_error_1(self):
        self.write_slots([daily_slot()])
        code, _ = self.cycle(send=True, apply_fn=lambda argv: 1,
                             now_ms=ms(2026, 10, 19, 9, 31))
        self.assertEqual(code, 1)

    def test_nothing_to_do_is_0_never_1(self):
        """契约核心：『今天没事做』必须是 0，否则 cron 天天报错。"""
        self.write_slots([daily_slot()])
        code, _ = self.cycle(send=True, apply_fn=lambda argv: 0,
                             now_ms=ms(2026, 10, 19, 9, 0))
        self.assertEqual(code, 0)

    def test_cli_once_dry_run_returns_0_and_releases_lock(self):
        self.write_slots([daily_slot()])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = run_schedule.main([
                "--once", "--dry-run",
                "--schedule", str(self.sched_path),
                "--state", str(self.state_path),
                "--lock", str(self.lock_path),
                "--now-ms", str(ms(2026, 10, 19, 9, 31)),
            ])
        self.assertEqual(code, 0)
        self.assertIn("dry-run", buf.getvalue())
        self.assertFalse(self.lock_path.exists(), "跑完必须释放锁")

    def test_cli_rejects_send_plus_dry_run(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            run_schedule.parse_args(["--send", "--dry-run"])
        self.assertEqual(ctx.exception.code, 2)


# ══════════════════════════════════════════════════════════════
# 跟进 runner
# ══════════════════════════════════════════════════════════════
NOW = ms(2026, 10, 19, 12, 0)     # 周一中午，确定在 09:00-21:00 窗口内
DAY = F.DAY_MS


def greet_row(job_id="j1", days_ago=4, status="sent", title="AI 应用开发", company="某公司"):
    return {
        "job_id": job_id,
        "security_id": "sec-" + job_id,
        "greeted_at": (NOW - days_ago * DAY) / 1000.0,   # store.py 写的是秒
        "status": status,
        "message": "您好",
        "error": None,
        "title": title,
        "company": company,
        "city": "广州",
    }


class _FollowupCase(_TmpCase):
    def setUp(self):
        super().setUp()
        self.fstate = self.tmp / "followup_state.json"

    def run_fu(self, *, send=False, rows=None, sender=None, gate=None, now=NOW):
        buf: list[str] = []
        calls: list = []
        sender = sender or (lambda item, ts: calls.append(item) or (True, ""))
        gate = gate or (lambda: (True, "bridge", "Bridge 扩展已连接"))
        code = run_followup.run_cycle(
            send=send,
            state_path=self.fstate,
            rows_fn=lambda: (rows if rows is not None else []),
            sender=sender,
            gate_fn=gate,
            now_ms=now,
            out=buf.append,
        )
        return code, "\n".join(buf), calls


class FollowupBehaviourTest(_FollowupCase):
    def test_dry_run_prints_plan_without_sending_or_state(self):
        code, out, calls = self.run_fu(rows=[greet_row()], send=False)
        self.assertEqual(code, 0)
        self.assertEqual(calls, [], "dry-run 不许发送")
        self.assertIn("AI 应用开发", out)
        self.assertIn("礼貌追问", out)
        self.assertIn("dry-run", out)
        self.assertFalse(self.fstate.exists(), "dry-run 不许写状态")

    def test_send_marks_sent_and_is_idempotent(self):
        calls = []

        def sender(item, ts):
            calls.append(item["job_id"])
            return True, ""

        code, _, _ = self.run_fu(rows=[greet_row()], send=True, sender=sender)
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["j1"])

        state = F.load_state(self.fstate)
        self.assertEqual([x["job_id"] for x in state["sent"]], ["j1"])

        code2, out2, _ = self.run_fu(rows=[greet_row()], send=True, sender=sender)
        self.assertEqual(code2, 0)
        self.assertEqual(calls, ["j1"], "同一 HR 不许重复跟进")
        self.assertIn("没有到期", out2)

    def test_outside_send_window_holds_until_morning(self):
        code, out, calls = self.run_fu(rows=[greet_row()], send=True,
                                       now=ms(2026, 10, 19, 7, 30))
        self.assertEqual(code, 0, "不在发送时段是正常等待，不是故障")
        self.assertEqual(calls, [], "夜里不许发")
        self.assertIn("发送时段", out)

    def test_gate_blocked_is_alert_and_sends_nothing(self):
        calls = []
        code, out, calls = self.run_fu(
            rows=[greet_row()], send=True,
            sender=lambda item, ts: calls.append(item) or (True, ""),
            gate=lambda: (False, "headless", "Bridge 与 CDP 均不可用"),
        )
        self.assertEqual(code, 2)
        self.assertEqual(calls, [], "门禁拒绝后不许调用 sender")
        self.assertIn("门禁", out)
        self.assertFalse(self.fstate.exists())

    def test_sender_failure_is_alert_and_not_recorded(self):
        code, out, _ = self.run_fu(
            rows=[greet_row()], send=True,
            sender=lambda item, ts: (False, "NO_SENDER_CHANNEL: 通道未接入"),
            gate=lambda: (True, "bridge", "ok"),
        )
        self.assertEqual(code, 2)
        self.assertIn("未发送", out)
        self.assertEqual(F.load_state(self.fstate)["sent"], [],
                         "没发出去就不许登记，否则永远丢这条")

    def test_no_due_rows_is_0(self):
        code, out, calls = self.run_fu(rows=[], send=True)
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])
        self.assertIn("没有到期", out)

    def test_too_new_and_too_old_are_not_due(self):
        rows = [greet_row("new", days_ago=1), greet_row("old", days_ago=15)]
        code, out, calls = self.run_fu(rows=rows, send=True)
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])

    def test_daily_limit_exhausted_is_0(self):
        today = time.strftime("%Y-%m-%d", time.localtime(NOW / 1000))
        self.fstate.write_text(json.dumps(
            {"sent": [], "day": today, "count": 20}), encoding="utf-8")
        code, _, calls = self.run_fu(rows=[greet_row()], send=True)
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])

    def test_rows_error_is_error_1(self):
        buf: list[str] = []

        def boom():
            raise RuntimeError("db is gone")

        code = run_followup.run_cycle(send=True, state_path=self.fstate,
                                      rows_fn=boom, sender=lambda i, t: (True, ""),
                                      gate_fn=lambda: (True, "bridge", "ok"),
                                      now_ms=NOW, out=buf.append)
        self.assertEqual(code, 1)
        self.assertIn("读投递台账失败", "\n".join(buf))

    def test_cli_once_dry_run_returns_0(self):
        with mock.patch.object(run_followup, "load_greeted_rows",
                               return_value=[greet_row()]):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = run_followup.main([
                    "--once", "--dry-run",
                    "--state", str(self.fstate),
                    "--lock", str(self.lock_path),
                    "--now-ms", str(NOW),
                    "--cfg", '{"daily_limit": 20}',
                ])
        self.assertEqual(code, 0)
        self.assertIn("dry-run", buf.getvalue())
        self.assertFalse(self.lock_path.exists())
        self.assertFalse(self.fstate.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
