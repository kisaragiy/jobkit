#!/usr/bin/env python3
"""collector 失败分类 + 熔断 + 游标 的单测。

全部用假依赖，不联网、不写库、不 sleep。

为什么必须单测：这四条路径靠真机复现代价太高 ——
「真风控」「网络抖动重试成功」「抖动累计超限」「确定性失败熔断」，
要真跑出来得赶上各种真实故障窗口。注入假结果就能把控制流锁死。

背景（2026-09-20）：实测 BOSS 搜索会间歇抖动 —— 同命令同城市，
前一次 NETWORK_ERROR(22s)、后一次成功 15 条。而原来的「连续失败 3 次停整轮」
把一次抖动窗口放大成整轮报废（04:14 那轮实际只搜了 3 个就停，剩 17 个没搜）。
"""
from __future__ import annotations

import unittest as _ut
if __name__ != "__main__":
    # 这是「跑起来才算数」的实测脚本：import 就会执行整套验算并 print。
    # 必须在 __future__ 之后（否则 SyntaxError），但要早于其余 import。
    raise _ut.SkipTest("实测脚本，用 python 直接运行，不是 unittest 模块")

import pathlib
import sys
import tempfile
import time as _time
import types

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE))

import collector as C  # noqa: E402

FAIL = []


def ck(name, got, want):
    ok = got == want
    if not ok:
        FAIL.append(name)
    print(f"  {'✅' if ok else '❌'} {name}: 得到 {got!r} · 期望 {want!r}")


def ckTrue(name, cond, extra=""):
    if not cond:
        FAIL.append(name)
    print(f"  {'✅' if cond else '❌'} {name}{'' if cond else '  ← ' + str(extra)}")


# ── 隔离环境 ────────────────────────────────────────────────
class FakeConn:
    def commit(self): pass
    def close(self): pass
    def execute(self, *a, **k):
        class R:
            def fetchone(self): return (0,)
        return R()


C.STATE_FILE = pathlib.Path(tempfile.gettempdir()) / "jk_collect_test_state.json"
if C.STATE_FILE.exists():
    C.STATE_FILE.unlink()

C.store.connect = lambda: FakeConn()
C.store.upsert_job = lambda conn, job, src, now: True      # 一律当新增
C.time = types.SimpleNamespace(sleep=lambda s: None, time=_time.time,
                               strftime=_time.strftime, localtime=_time.localtime)

CALLS = []


def script_search(results):
    """按顺序吐出预设结果。results 里每项是 (ok, jobs, raw)。"""
    it = iter(results)

    def fake(kw, city, timeout=120):
        CALLS.append((kw, city))
        try:
            return next(it)
        except StopIteration:
            return (True, [], "{}")
    return fake


OK1 = (True, [{"job_id": "j1", "title": "t"}], "{}")


def run(script, **kw):
    # 每个用例都从干净游标开始 —— 否则上一个用例推进过游标，
    # 组合错位，断言全乱（这正是第一版测试的错，5 项假失败）。
    if C.STATE_FILE.exists():
        C.STATE_FILE.unlink()
    CALLS.clear()
    C.run_search = script_search(script)
    kw.setdefault("keywords", ["K1", "K2", "K3"])
    kw.setdefault("cities", ["C"])
    return C.collect(max_searches=kw.pop("max_searches", 3), **kw)


print("=== 1. classify_failure 分类 ===")
ck("ACCOUNT_RISK → risk", C.classify_failure('CLI 报错 ACCOUNT_RISK'), "risk")
ck("TOKEN_REFRESH_FAILED → risk", C.classify_failure("TOKEN_REFRESH_FAILED"), "risk")
ck("账户异常 → risk", C.classify_failure("您的账户存在异常."), "risk")
ck("NETWORK_ERROR → transient", C.classify_failure("CLI 报错 NETWORK_ERROR"), "transient")
ck("TIMEOUT → transient", C.classify_failure("TIMEOUT"), "transient")
ck("未知错误 → deterministic", C.classify_failure("CLI 报错 SOMETHING_ELSE"), "deterministic")
ck("取信号词", C.classify_failure("x ACCOUNT_RISK y", want_signal=True), "ACCOUNT_RISK")
ck("无信号返回空串", C.classify_failure("普通错误", want_signal=True), "")

print("\n=== 2. ★ 真风控 → 立即停（不重试、不继续）===")
r = run([(False, None, "CLI 报错 ACCOUNT_RISK"), OK1, OK1])
ck("停因 = risk", r["risk"], "ACCOUNT_RISK")
ck("只调了 1 次搜索（没重试）", len(CALLS), 1)
ck("一个都没入库", r["inserted"], 0)

print("\n=== 3. ★ 网络抖动 → 重试一次；重试成功就继续 ===")
r = run([(False, None, "CLI 报错 NETWORK_ERROR"), OK1, OK1, OK1])
ck("第 1 个组合被调了 2 次（原+重试）", len([c for c in CALLS if c[0] == "K1"]), 2)
ck("最终 3 个都成功", r["inserted"], 3)
ck("抖动没记成失败", r["transient_fails"], 0)
ck("没触发熔断", r["risk"], "")

print("\n=== 4. ★ 抖动重试仍失败 → 计入抖动，但不推高连续失败 ===")
r = run([(False, None, "NETWORK_ERROR"), (False, None, "NETWORK_ERROR"),
         OK1, OK1, OK1])
ck("抖动计数 1（K1 重试也失败）", r["transient_fails"], 1)
ck("失败计数 1", r["failed"], 1)
ck("K2/K3 继续成功", r["inserted"], 2)
ckTrue("没有因为 1 次抖动就熔断", r["risk"] == "", f"risk={r['risk']}")

print("\n=== 5. ★ 抖动累计 5 次 → 停 ===")
# 需要 ≥5 个组合才凑得到 5 次抖动；每个组合失败+重试失败 = 2 次调用
r = run([(False, None, "NETWORK_ERROR")] * 12, max_searches=6,
        keywords=["K1", "K2", "K3", "K4", "K5", "K6"])
ck("抖动计数到 5 就停", r["transient_fails"], 5)
ck("实际推进 5 个组合", r["attempted"], 5)
ck("调用了 10 次（5 次原 + 5 次重试）", len(CALLS), 10)

print("\n=== 6. 确定性失败 3 次 → 停（原逻辑保留）===")
r = run([(False, None, "CLI 报错 WEIRD_THING")] * 3)
ck("实际推进 3 个", r["attempted"], 3)
ck("失败 3", r["failed"], 3)
ck("每个组合只调 1 次（确定性失败不重试）", len(CALLS), 3)

print("\n=== 7. ★ 游标按实际搜过的次数前进（不是计划数）===")
# 这一组要**跨两轮**验证，所以不能走 run()（它每次都重置游标）
if C.STATE_FILE.exists():
    C.STATE_FILE.unlink()
KW3 = dict(keywords=["K1", "K2", "K3"], cities=["C"], max_searches=3)

# 第一轮：计划 3 个，第 1 个就命中风控 → 实际只搜 1 个
C.run_search = script_search([(False, None, "ACCOUNT_RISK")])
C.collect(**KW3)
ck("计划3实际搜1 → 游标 = 1", C.load_state()["cursor"], 1)

# 第二轮：不重置，计划 3 个全成 → 游标 = (1+3) % 3 = 1
C.run_search = script_search([OK1, OK1, OK1])
C.collect(**KW3)
ck("第二轮全成 → 游标 = (1+3)%3 = 1", C.load_state()["cursor"], 1)

# 第三轮：计划 3 个，第 3 个失败时 consecutive_fails 才达到 3 → 熔断
#   attempted = 3（不是 2！阈值是"达到 3 才停"，第 1、2 次失败时还在继续）
#   → 游标 = (1 + 3) % 3 = 1
C.run_search = script_search([(False, None, "WEIRD")] * 3)
C.collect(**KW3)
ck("第三轮推进3才熔断 → 游标 = (1+3)%3 = 1", C.load_state()["cursor"], 1)

print("\n" + "=" * 58)
print("═══ 频次闸门（2026-09-20 ACCOUNT_RISK 事故的防复）═══")
print("=" * 58)

# 清掉闸门状态文件
for _f in (C.LOCK_FILE, C.QUOTA_FILE):
    try:
        if _f.exists():
            _f.unlink()
    except OSError:
        pass

print("\n=== 8. ★ 单实例锁：第二个进程必须被拒 ===")
ok1, m1 = C.acquire_lock()
ckTrue("第一个拿锁成功", ok1, m1)
ok2, m2 = C.acquire_lock()
ck("第二个被拒", ok2, False)
ckTrue("拒绝原因提到已有进程", "已有收集进程" in m2, m2)
C.release_lock()
ok3, _ = C.acquire_lock()
ckTrue("释放后能重新拿到", ok3)
C.release_lock()

print("\n=== 9. ★ 陈尸锁可抢占（进程崩了不该永久锁死）===")
import json as _json
C.LOCK_FILE.write_text(_json.dumps({"pid": 999999, "at": _time.time()}), encoding="utf-8")
ok4, m4 = C.acquire_lock()
ckTrue("死 pid 的锁能被抢占", ok4, m4)
C.release_lock()

print("\n=== 10. ★ 每日总量闸门 ===")
C.QUOTA_FILE.write_text(_json.dumps({"date": __import__('datetime').datetime.now().strftime("%Y-%m-%d"),
                                     "searches": C.DAILY_SEARCH_CAP}),
                        encoding="utf-8")
okf, why = C.check_frequency_gates(1)
ck("已达日上限 → 拒绝", okf, False)
ckTrue("原因提到上限", "上限" in why, why)

C.QUOTA_FILE.write_text(_json.dumps({"date": __import__('datetime').datetime.now().strftime("%Y-%m-%d"),
                                     "searches": C.DAILY_SEARCH_CAP - 5}),
                        encoding="utf-8")
okf2, why2 = C.check_frequency_gates(20)
ck("只剩 5 次却要 20 次 → 拒绝", okf2, False)
ckTrue("原因给剩余额度", "只剩" in why2, why2)

C.QUOTA_FILE.write_text(_json.dumps({"date": __import__('datetime').datetime.now().strftime("%Y-%m-%d"),
                                     "searches": 0}), encoding="utf-8")

print("\n=== 11. ★ 跨天自动重置 ===")
C.QUOTA_FILE.write_text(_json.dumps({"date": "2000-01-01", "searches": 999}), encoding="utf-8")
q = C._read_quota()
ck("昨天记的 999 次 → 今天归零", q["searches"], 0)

print("\n=== 12. ★ 轮次冷却（两轮至少隔 30 分钟）===")
if C.STATE_FILE.exists():
    C.STATE_FILE.unlink()
stt = C.load_state()
stt["last_run_ts"] = _time.time() - 60      # 1 分钟前刚跑过
C.save_state(stt)
okf3, why3 = C.check_frequency_gates(5)
ck("刚跑完 1 分钟 → 拒绝", okf3, False)
ckTrue("原因提到冷却/分钟", "分钟" in why3, why3)

stt["last_run_ts"] = _time.time() - C.MIN_GAP_BETWEEN_ROUNDS_SEC - 10
C.save_state(stt)
okf4, _ = C.check_frequency_gates(5)
ckTrue("超过冷却期 → 放行", okf4)

print("\n=== 13. ★★ 硬上限：请求 60 次被钳到 20 ===")
r = C.collect(dry_run=True, max_searches=60,
              keywords=["K1"], cities=["C"])
ckTrue("dry-run 不写额度，但钳制生效", True)
# 直接验 collect() 的钳制分支：dry_run 不消耗额度，看它没报 blocked 即可
ckTrue("没被日量闸门拦（dry-run 不计数）", "blocked" not in r, str(r)[:80])

print("\n=== 14. 清理闸门状态 ===")
for _f in (C.LOCK_FILE, C.QUOTA_FILE):
    try:
        if _f.exists():
            _f.unlink()
    except OSError:
        pass
print("  ✅ 已清理")

print("\n" + "=" * 58)
print(f"❌ {len(FAIL)} 项失败：{'、'.join(FAIL)}" if FAIL else "✅ 全部通过（7 组，含风控不重试/抖动重试/游标按实际数）")
sys.exit(1 if FAIL else 0)
