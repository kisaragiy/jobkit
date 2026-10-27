#!/usr/bin/env python3
"""jobkit 单一入口 —— 用户只需要记一个命令。

设计原则（为什么这样分）：
  1. **不带参数就是最有用的那一屏**。多数人打开只想问「今天有什么值得投的」，
     不该先逼他学参数。所以 `jobkit` = 看板。
  2. **意图即命令**：找岗 / 看全部 / 投递 / 进度，四个动词覆盖日常。
  3. **参数能用中文就用中文**：城市名、方向词直接写，不查代码、不看文档。
  4. 出错不甩 traceback —— 见 main() 的兜底。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import store  # noqa: E402

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    _con = Console()
    HAVE_RICH = True
except ImportError:                              # 没装 rich 也要能用
    _con = None
    HAVE_RICH = False


# ── 输出（rich 与纯文本双实现，保证任何环境都能跑）─────────────
def _say(text: str = "") -> None:
    if HAVE_RICH:
        _con.print(text)
    else:
        print(text.replace("[bold]", "").replace("[/bold]", "")
                   .replace("[dim]", "").replace("[/dim]", ""))


def _table(title: str, headers: list[str], rows: list[list[str]]) -> None:
    if HAVE_RICH:
        t = Table(title=title, title_justify="left", show_lines=False, pad_edge=False)
        for i, h in enumerate(headers):
            t.add_column(h, justify="right" if i == 0 else "left", no_wrap=(i == 0))
        for r in rows:
            t.add_row(*r)
        _con.print(t)
        return
    _say(title)
    for r in rows:
        _say("  " + "  ".join(str(x) for x in r))


_CONN = None


def _db():
    """共享一个连接，main() 结束统一关闭 —— 否则 Windows 上会锁住 db 文件。"""
    global _CONN
    if _CONN is None:
        _CONN = store.connect()
    return _CONN


def _fmt_score(v) -> str:
    try:
        f = float(v or 0)
    except (TypeError, ValueError):
        return "—"
    return f"{f:.0f}" if f > 0 else "—"


def _cut(s, n: int) -> str:
    s = str(s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


# ── 首页看板 ────────────────────────────────────────────────
def cmd_home(_args) -> int:
    c = _db()
    now = time.time()
    total = c.execute("select count(*) from jobs").fetchone()[0]
    if total == 0:
        _say("[bold]jobkit · 还没有岗位数据[/bold]")
        _say("")
        _say("第一次用，跑这个：")
        _say("  [bold]python jobkit.py find 广州 Python[/bold]")
        return 0

    import rules

    new24 = c.execute("select count(*) from jobs where first_seen > ?", (now - 86400,)).fetchone()[0]
    greeted = c.execute("select count(*) from greetings").fetchone()[0]

    # 待投 = **通过筛选** 且没投过的。库里 633 条只有一百多条真值得投，
    # 把筛选掉的全算进来，「待投 630」就是个假数字，用户会失去对推荐的信任。
    ungreeted = [dict(r) for r in c.execute(
        "select j.* from jobs j where not exists "
        "(select 1 from greetings g where g.job_id = j.job_id)")]
    passed = [j for j in ungreeted if rules.job_ok(j)[0]]
    filtered = len(ungreeted) - len(passed)
    passed.sort(key=lambda j: -(j.get("match_score") or 0))

    _say("")
    _say(f"[bold]🐈‍⬛ jobkit[/bold]  {time.strftime('%m-%d %H:%M')}")
    _say(f"  值得投 [bold]{len(passed)}[/bold] · 今日新增 [bold]{new24}[/bold] · "
         f"已投 {greeted} · 库里 {total}（筛掉 {filtered} 条不合适的）")
    _say("")

    if not passed:
        _say("没有可投的了 —— 两条路：")
        _say("  采新岗： [bold]python jobkit.py find 广州 Python[/bold]")
        _say("  看被筛掉的原因： [bold]python jobkit.py list --rejected[/bold]")
        return 0

    rows = passed[:10]
    body = []
    for i, r in enumerate(rows, 1):
        where = f"{r.get('city') or ''}{r.get('district') or ''}"
        body.append([
            str(i),
            _cut(r.get("title"), 26),
            _cut(r.get("salary"), 14),
            _cut(_co_label(r.get("company")), 20),
            _cut(where, 10),
            _fmt_score(r.get("match_score")),
        ])
    _table("最该投的", ["#", "岗位", "薪资", "公司", "地点", "匹配"], body)
    _say("")
    _say("[dim]下一步：[/dim][bold]jobkit list[/bold] 看全部 · "
         "[bold]jobkit find 广州 Python[/bold] 采新岗 · "
         "[bold]jobkit apply[/bold] 投递")
    _say("[dim]想看完整界面（薪资计算器 / Offer 对比 / 简历体检 / 模拟面试）：[/dim]"
         "[bold]jobkit web[/bold]")
    return 0


# ── 列岗位 ──────────────────────────────────────────────────
def _co_label(company: str) -> str:
    """公司名后加外包标记 —— 投递前一眼看得见，不用点进去查。"""
    import company_guard as cg

    tag = cg.label(company)
    return f"{company or ''} {tag}".strip() if tag else (company or "")


def cmd_list(args) -> int:
    import rules

    c = _db()
    sql = ["select j.* from jobs j where 1=1"]
    params: list = []
    if args.city:
        sql.append("and j.city = ?")
        params.append(args.city)
    if args.min_score is not None:
        sql.append("and coalesce(j.match_score, 0) >= ?")
        params.append(args.min_score)
    if args.pending:
        sql.append("and not exists (select 1 from greetings g where g.job_id = j.job_id)")

    rows = [dict(r) for r in c.execute(" ".join(sql), params)]

    # 筛选：默认给「能投的」；--rejected 反过来，专门用来看为什么被筛掉
    kept, dropped = [], []
    for j in rows:
        good, why = rules.job_ok(j)
        (kept if good else dropped).append((j, why))

    if args.rejected:
        dropped.sort(key=lambda x: -(x[0].get("match_score") or 0))
        if not dropped:
            _say("没有岗位被筛掉 —— 库里的都符合条件。")
            return 0
        body = [[str(i), _cut(j.get("title"), 26), _cut(j.get("salary"), 12),
                 _cut(j.get("company"), 16), _cut(why, 22)]
                for i, (j, why) in enumerate(dropped[: args.limit], 1)]
        _table(f"被筛掉的 {len(dropped)} 条（为什么没推荐给你）",
               ["#", "岗位", "薪资", "公司", "原因"], body)
        _say("")
        _say("[dim]这些规则可以在设置里调（黑名单/经验/薪资上限）。[/dim]")
        return 0

    if args.all:
        kept = [(j, "") for j in rows]

    kept.sort(key=lambda x: -(x[0].get("match_score") or 0))
    if not kept:
        _say("没有符合条件的岗位。放宽条件，或先采集：")
        _say("  [bold]python jobkit.py find 广州 Python[/bold]")
        return 0

    body = []
    for i, (r, _why) in enumerate(kept[: args.limit], 1):
        body.append([
            str(i),
            _cut(r.get("title"), 28),
            _cut(r.get("salary"), 14),
            _cut(_co_label(r.get("company")), 20),
            _cut(f"{r.get('city') or ''}{r.get('district') or ''}", 10),
            _fmt_score(r.get("match_score")),
            _cut(r.get("platform") or "boss", 7),
            _cut(r.get("experience"), 7),
        ])
    _table(f"岗位 {len(kept)} 条（筛掉 {len(dropped)}）",
           ["#", "岗位", "薪资", "公司", "地点", "匹配", "平台", "经验"], body)
    return 0


# ── 看单个岗位 ──────────────────────────────────────────────
def cmd_show(args) -> int:
    """`jobkit show 3` = 看榜单第 3 名的完整信息。

    序号按「当前榜单同样的筛选+排序」算，所以看到第 3 个，show 3 就是它。
    """
    import rules

    c = _db()
    ungreeted = [dict(r) for r in c.execute(
        "select j.* from jobs j where not exists "
        "(select 1 from greetings g where g.job_id = j.job_id)")]
    passed = sorted([j for j in ungreeted if rules.job_ok(j)[0]],
                    key=lambda j: -(j.get("match_score") or 0))

    if not passed:
        _say("当前没有可投岗位。")
        return 0

    idx = args.index
    if idx < 1 or idx > len(passed):
        _say(f"序号超范围：榜单现在有 {len(passed)} 条（1~{len(passed)}）。")
        return 2
    j = passed[idx - 1]

    _say("")
    _say(f"[bold]{j.get('title') or '（无标题）'}[/bold]")
    _say(f"  {j.get('salary') or '薪资面议'}   ·   {j.get('company') or ''}")
    _say(f"  {j.get('city') or ''}{j.get('district') or ''}   ·   "
         f"{j.get('experience') or ''} {j.get('education') or ''}   ·   "
         f"匹配 {_fmt_score(j.get('match_score'))}   ·   {j.get('platform') or 'boss'}")
    _say("")

    hits = rules.match_hits(j)
    if hits:
        _say(f"  命中你的技能：{'、'.join(hits)}")
    else:
        _say("  没命中技术词 —— 排在前面是因为别的原因（薪资/学历门槛低）")
    _say("")

    jd = str(j.get("job_description") or j.get("jd") or "").strip()
    if jd:
        _say(jd[:1200] + ("…（已截断）" if len(jd) > 1200 else ""))
    else:
        _say("[dim]这条没有 JD 全文（BOSS 通道不返回详情）。[/dim]")

    url = str(j.get("url") or "").strip()
    if url:
        _say("")
        _say(f"  打开：{url}")
    return 0


# ── 补 JD ───────────────────────────────────────────────────
def cmd_enrich(args) -> int:
    """给缺 JD 的 BOSS 岗位补详情页。

    为什么值得单独一个命令：BOSS 列表页只给技能标签，没有 JD 正文。
    而 JD 是判断「这岗到底要什么」的唯一依据，也是匹配度打分的主要输入。
    智联那边一次就带 JD，BOSS 要额外开一次详情页——这是补上这个差距。
    """
    import random
    import dom_channel as DC
    import dom_probe

    c = _db()
    sql = ("select job_id, title, match_score, company, salary, city, district, "
           "experience, education, skills from jobs "
           "where coalesce(job_description, '') = '' and job_id not like 'CC%'")
    sql += " order by coalesce(match_score, 0) desc"
    all_rows = [dict(r) for r in c.execute(sql)]
    if not all_rows:
        _say("所有 BOSS 岗位都有 JD 了，不用补。")
        return 0

    # 默认只补「值得投」的：不值得投的岗补 JD 没有任何意义，
    # 610 条全补要两个多小时，等于把时间花在永远不看的数据上。
    import rules
    if args.all_jobs:
        rows = all_rows
        scope = "全部"
    else:
        rows = [j for j in all_rows if rules.job_ok(j)[0]]
        scope = "值得投的"
        if not rows:
            _say(f"缺 JD 的 {len(all_rows)} 条全被筛掉了，没必要补。")
            _say("想强行全补： [bold]python jobkit.py enrich --all-jobs[/bold]")
            return 0

    limit = len(rows) if args.all else min(args.limit, len(rows))
    todo = rows[:limit]
    _say(f"缺 JD 的 BOSS 岗位 {len(all_rows)} 条，其中{scope} {len(rows)} 条，"
         f"本次补 {limit} 条（每条间隔 3-6 秒，防风控）")
    _say("")

    ok_b, why = dom_probe.ensure_browser()
    if not ok_b:
        _say(f"浏览器准备失败：{why}")
        return 2

    import rules
    filled = failed = 0
    for i, r in enumerate(todo, 1):
        jid = r["job_id"]
        url = f"https://www.zhipin.com/job_detail/{jid}.html"
        try:
            ok, job, reason = DC.fetch_detail(url)
        except Exception as e:                   # noqa: BLE001  单条失败不该炸整批
            ok, job, reason = False, None, f"{type(e).__name__}"

        if ok and job and (job.get("job_description") or "").strip():
            job["match_score"] = None            # JD 变了，匹配度要重算
            job["match_score"] = rules.pure_match(job)
            store.upsert_job(c, job, "enrich:jd", time.time())
            c.commit()
            filled += 1
            n = len(job["job_description"])
            _say(f"  [{i}/{limit}] ✅ {r['title'][:22]} —— JD {n} 字")
        else:
            failed += 1
            _say(f"  [{i}/{limit}] ❌ {r['title'][:22]} —— {reason}")
            if failed >= 3 and filled == 0:
                _say("")
                _say("连续 3 条都失败 —— 停手。多半是登录态失效：")
                _say("  [bold]python boss_login.py[/bold] 重新扫码，再回来跑本命令")
                return 2
        if i < limit:
            time.sleep(random.uniform(3.0, 6.0))

    _say("")
    _say(f"补上 {filled} 条，失败 {failed} 条。")
    if filled:
        _say("[dim]匹配度已按 JD 重算，看板会跟着变。[/dim]")
    return 0


# ── Web 界面 ────────────────────────────────────────────────
def cmd_web(args) -> int:
    """起图形界面（薪资计算器 / Offer 对比 / 简历体检 / 模拟面试都在里面）。

    用户不必知道 app.py、uvicorn、端口这些内部名词 —— 一个 jobkit web 就够。
    端口被系统保留或占用时 app.py 会自己换，不把这事丢给用户。
    """
    import subprocess
    import sys as _sys
    from pathlib import Path

    app_py = Path(__file__).with_name("app.py")
    if not app_py.exists():
        _say(f"找不到界面程序：{app_py}")
        return 1
    cmd = [_sys.executable, str(app_py)]
    if getattr(args, "port", None):
        cmd += ["--port", str(args.port)]
    _say("正在启动界面… 浏览器会打开；关掉这个窗口就退出。")
    try:
        return int(subprocess.call(cmd) or 0)
    except KeyboardInterrupt:
        return 130


# ── 采集 ────────────────────────────────────────────────────
def cmd_find(args) -> int:
    import collector

    cities = [args.city] if args.city else None
    sources = {"boss": "auto", "zhilian": "zhilian", "auto": "auto"}
    src = sources[args.source]

    _say(f"[bold]找岗[/bold]  关键词={args.keyword}  "
         f"城市={args.city or '（配置里的全部）'}  通道={args.source}")
    r = collector.collect(dry_run=args.dry_run, max_searches=args.max,
                          keywords=[args.keyword] if args.keyword else None,
                          cities=cities, source=src)
    if r.get("error"):
        _say(f"[bold]没跑成[/bold]：{r['error']}")
        return 2
    if r.get("blocked"):
        _say(f"[bold]稍后再来[/bold]：{r['blocked']}")
        return 0
    _say(f"  新增 {r.get('inserted', 0)} · 更新 {r.get('updated', 0)} · "
         f"失败 {r.get('failed', 0)}")
    if r.get("inserted"):
        _say("  [dim]看一眼：[/dim][bold]python jobkit.py[/bold]")
    return 0


# ── 投递 / 进度 ─────────────────────────────────────────────
def cmd_apply(args) -> int:
    import apply_pipeline
    argv = ["apply_pipeline.py"] + ([] if args.send else ["--dry-run"])
    saved, sys.argv = sys.argv, argv
    try:
        return int(apply_pipeline.main() or 0)
    finally:
        sys.argv = saved


def cmd_commute(args) -> int:
    """通勤估算（区镇中心直线距离）。家默认读 data/home.json。"""
    import commute

    if args.set_home:
        if not args.set_home_city:
            print("❌ 还要给城市：--set-home 威远岛 --set-home-city 东莞")
            return 2
        try:
            h = commute.save_home(args.set_home, args.set_home_city)
        except ValueError as e:
            print(f"❌ {e}")
            return 2
        print(f"✅ 家的位置已存：{h['city']}{h['name']} "
              f"({h['lat']:.4f}, {h['lon']:.4f})")
        return 0

    cfg = commute.load_home()
    if not (args.home and args.city) and not cfg:
        print("❌ 还没设置家的位置。先跑：\n"
              "   jobkit commute --set-home 威远岛 --set-home-city 东莞")
        return 2
    if cfg and not (args.home and args.city):
        print(f"家：{cfg['city']}{cfg['name']}（要改就加 --home/--city）")
    home_city = args.city or (cfg or {}).get("city", "")

    if args.district:
        r = commute.estimate(args.home, args.city, args.district,
                             args.district_city or home_city)
        print(commute.render(r))
        return 0 if r.get("ok") else 2

    c = _db()
    try:
        row = c.execute(
            "SELECT district, city, title, company, match_score FROM jobs "
            "WHERE coalesce(district,'')!='' "
            "ORDER BY match_score DESC LIMIT 1 OFFSET ?", (args.index,)).fetchone()
    finally:
        c.close()
    if not row:
        print("❌ 没有带区镇信息的岗位（先采集，或换 --index）")
        return 2
    district, city, title, company, _ = row
    print(f"岗位：{title}  {company}")
    r = commute.estimate(args.home, args.city, district, city)
    print(commute.render(r))
    return 0 if r.get("ok") else 2


def cmd_company(args) -> int:
    """公司画像（只看本地数据）。"""
    import company_profile as CP

    c = _db()
    try:
        print(CP.render(CP.profile(c, args.name)))
    finally:
        c.close()
    return 0


def cmd_redline(args) -> int:
    """岗位红线检测：投之前把坑岗筛掉。"""
    import redline

    c = _db()
    try:
        if args.scan:
            print(redline.render_scan(redline.scan(c)))
            return 0

        sql = ("SELECT title, company, salary, job_description FROM jobs ")
        params = ()
        if args.query:
            sql += "WHERE title LIKE ? OR company LIKE ? "
            params = (f"%{args.query}%", f"%{args.query}%")
        sql += "ORDER BY match_score DESC LIMIT 1 OFFSET ?"
        params += (args.index or 0,)

        row = c.execute(sql, params).fetchone()
        if not row:
            print("❌ 没找到这个岗位（换 --query 或先采集）")
            return 2
        print(redline.render(redline.check({
            "title": row[0], "company": row[1],
            "salary": row[2], "job_description": row[3]})))
    finally:
        c.close()
    return 0


def cmd_priority(args) -> int:
    """今日优先投哪几个 —— 综合 HR 活跃度/薪资落点/经验门槛排序。"""
    import priority

    c = _db()
    try:
        # 已投过的不能再出现在「该投」名单里；绝对硬伤也不必占位次
        sql = ("SELECT j.* FROM jobs j "
               "WHERE j.job_id NOT IN (SELECT job_id FROM greetings "
               "                       WHERE job_id IS NOT NULL)")
        params: list = []
        if args.city:
            sql += " AND j.city LIKE ?"
            params.append(f"%{args.city}%")

        jobs = [dict(r) for r in c.execute(sql, params)]
        if not jobs:
            print("没有待投岗位 —— 要么全投过了，要么还没采集。")
            return 0

        # 先剔除红线硬伤，剩下的参与排序
        import filters as F
        cfg = F.from_settings({})
        clean = []
        for j in jobs:
            ok, reason = F.check_job(j, cfg)
            if not (not ok and reason.startswith("红线")):
                clean.append(j)
        dropped = len(jobs) - len(clean)

        rows = priority.rank(clean, args.n)
        print(priority.render(rows, len(clean)))
        if dropped:
            print(f"（候选池已排除 {dropped} 个红线硬伤岗）")
    finally:
        c.close()
    return 0


def cmd_resume_batch(args) -> int:
    """简历 × 一批岗：跨岗看缺什么词（补一个词能覆盖几个岗）。"""
    import priority
    import resume_check as RC
    import resume_kit

    c = _db()
    try:
        if args.path:
            rpath = args.path
        else:
            act = resume_kit.active(c)
            if not act:
                print("❌ 没有启用的简历版本 —— 先跑 jobkit resume list")
                return 2
            rpath = act["path"]
        try:
            rtext = RC.load_text(rpath)
        except OSError as e:
            print(f"❌ 读不到简历：{e}")
            return 2

        # 默认只看「还没投、且排序靠前」的岗 —— 那才是马上要投的一批。
        # --all 才是全库，用来做长期能力盘点。
        sql = ("SELECT j.* FROM jobs j "
               "WHERE j.job_id NOT IN (SELECT job_id FROM greetings "
               "                       WHERE job_id IS NOT NULL)")
        jobs = [dict(r) for r in c.execute(sql)]
        picked = jobs if args.all else priority.rank(jobs, args.n)

        print(f"简历：{rpath}")
        print()
        print(RC.render_batch(RC.batch(rtext, picked, args.top)))
    finally:
        c.close()
    return 0


def cmd_review(args) -> int:
    """面试复盘：面完立刻记，下次不重新踩坑。"""
    import interview_log as IL

    c = _db()
    try:
        if args.add:
            if not args.company:
                print("❌ 要指定公司：--add --company \"某公司\"")
                return 2
            iid = IL.add_interview(c, args.company, args.position or "",
                                   args.stage or "", note=args.note or "")
            print(f"✅ 记下面试 #{iid}：{args.company}")
            print(f"   接着记问题：jobkit review --id {iid} "
                  f"--question \"问了什么\" --score 3 --better \"下次怎么答\"")
            return 0

        if args.id is not None:
            if args.question:
                IL.add_question(c, args.id, args.question, args.answer or "",
                                args.score, args.better or "")
                print(f"✅ 已记到面试 #{args.id}")
                return 0
            if args.result:
                IL.set_result(c, args.id, args.result)
                print(f"✅ 面试 #{args.id} 结果已更新")
                return 0
            iv = IL.get_interview(c, args.id)
            if not iv:
                print(f"❌ 没有 #{args.id} 这场面试")
                return 2
            print(IL.render_one(iv))
            return 0

        print(IL.render_review(IL.review(c)))
        print()
        print(IL.render_list(IL.list_interviews(c)))
    except ValueError as e:
        print(f"❌ {e}")
        return 2
    finally:
        c.close()
    return 0


def cmd_weekly(args) -> int:
    """求职周报：本周做了什么、卡在哪一层。"""
    import weekly

    c = _db()
    try:
        if args.set_target:
            d = {}
            for item in args.set_target:
                if "=" not in item:
                    print(f"❌ 格式错了：{item}，要写成 apply=20")
                    return 2
                k, v = item.split("=", 1)
                if k not in ("apply", "interview"):
                    print(f"❌ 只能设 apply 或 interview，你给的是 {k}")
                    return 2
                try:
                    d[k] = int(v)
                except ValueError:
                    print(f"❌ {k} 要整数，你给的是「{v}」")
                    return 2
            t = weekly.save_targets(**d)
            print(f"✅ 目标已更新：每周投递 {t['apply']} 个，面试 {t['interview']} 场")
            return 0
        print(weekly.render(weekly.build(c, args.week)))
    finally:
        c.close()
    return 0


def cmd_living(args) -> int:
    """异地入职收支测算：这份工资在那边每月能剩多少。

    支出可以手填，也可以按「城市+区/镇街+档位」取预置档位（data/living_cost.json）。
    """
    import company_profile as CP
    import living_cost as LC

    if getattr(args, "cities", False):
        meta = LC.data_meta()
        print(f"预置生活成本数据：{meta['source']}")
        print(f"采集 {meta['collected_at']} · 覆盖 {meta['coverage']}")
        for city in LC.covered_cities():
            print(f"  {city:<4} {len(LC.districts(city)):>2} 个区/镇街："
                  f"{'、'.join(LC.districts(city))}")
        print("\n没覆盖的城市（如惠州）不给估算，请用 --rent/--food 手填。")
        return 0

    city = args.city
    pretax = args.salary * 1000 if args.salary else None
    district = getattr(args, "district", None)

    # 不给 --city 就从榜单取岗位（城市/薪资/区都从岗位上读）
    if not city:
        c = _db()
        try:
            row = c.execute(
                "SELECT title, company, salary, city, district FROM jobs "
                "WHERE coalesce(city,'')!='' "
                "ORDER BY match_score DESC LIMIT 1 OFFSET ?",
                (args.index or 0,)).fetchone()
        finally:
            c.close()
        if not row:
            print("❌ 没有带城市的岗位（先采集，或用 --city 指定）")
            return 2
        title, company, salary, city, job_district = row
        print(f"岗位：{title}  {company}  {salary}  {city}")
        if district is None and job_district:
            district = job_district
            print(f"岗位区镇：{district}")
        elif district is None:
            print("（岗位没抓到区镇，按城市中位算）")
        if pretax is None:
            parsed = CP._parse_salary(salary)
            if not parsed:
                print(f"❌ 岗位薪资「{salary}」解析不了，请用 --salary 指定（单位 K）")
                return 2
            lo, hi = parsed
            pretax = round((lo + hi) / 2) * 1000
            print(f"按薪资区间中位数 {pretax/1000:.0f}K 算"
                  f"（区间 {lo:g}-{hi:g}K）")

    if pretax is None:
        print("❌ 还要给 --salary（税前月薪，单位 K，如 --salary 9）")
        return 2

    # 对比模式：同城各区 × 两档
    if getattr(args, "compare", False):
        cmp = LC.compare_tiers(city, pretax, district, args.scene)
        print()
        print(LC.render_compare(cmp, pretax))
        return 0 if cmp.get("ok") else 2

    tier = getattr(args, "tier", None)
    if not tier and not district:
        if not any((args.rent, args.food, args.transport, args.other)):
            print(f"提示：不填支出会按 0 算（结余虚高）。可以改用预置档位，例如："
                  f" --district {LC.districts(city)[0] if LC.districts(city) else '区名'}"
                  f" --tier normal（覆盖：{'、'.join(LC.covered_cities())}）")
            print()

    print()
    b = LC.balance(pretax, city, args.rent or 0, args.food or 0,
                   args.transport or 0, args.other or 0,
                   district=district, tier=tier, scene=args.scene)
    print(LC.render(b))
    if not b.get("ok"):
        return 2

    # 东莞住家场景：看租房档时顺手算一下自家的账（用户在虎门威远岛有房）
    if city == "东莞" and b.get("plan") and b["plan"].get("scene") == "rent":
        hd = b["plan"]["district"] or "虎门"
        h = LC.plan_cost(city, hd, "normal", "home")
        if h.get("ok"):
            hv = LC.balance(pretax, city, district=hd,
                            tier="normal", scene="home")
            hf = LC.plan_cost(city, hd, "frugal", "home")
            print()
            print(f"—— 住家对比（{hd}，无房租）——")
            line = f"正常档月支出 {h['total']:,} → 月结余 {hv['left']:+,.0f}"
            if hf.get("ok"):
                hfv = LC.balance(pretax, city, district=hd,
                                 tier="frugal", scene="home")
                line += f"；省着过 {hf['total']:,} → 结余 {hfv['left']:+,.0f}"
            print(line)
    return 0


def cmd_resume_check(args) -> int:
    """简历 × 岗位 JD 匹配度诊断。"""
    import resume_check as RC
    import resume_kit

    c = _db()
    try:
        if args.path:
            rpath = args.path
        else:
            act = resume_kit.active(c)
            if not act:
                print("❌ 没有启用的简历版本，先跑：python jobkit.py resume list")
                return 2
            rpath = act["path"]
        try:
            rtext = RC.load_text(rpath)
        except OSError as e:
            print(f"❌ 读不到简历：{e}")
            return 2

        if args.jd_file:
            try:
                jd = RC.load_text(args.jd_file)
            except OSError as e:
                print(f"❌ 读不到 JD 文件：{e}")
                return 2
            title, company = "", ""
        else:
            row = c.execute(
                "SELECT title, company, job_description, match_score FROM jobs "
                "WHERE length(coalesce(job_description,''))>100 "
                "ORDER BY match_score DESC LIMIT 1 OFFSET ?", (args.index,)).fetchone()
            if not row:
                print("❌ 没有可用的岗位（需要先采集，且 JD 长度 > 100）")
                return 2
            title, company, jd, _ = row

        print(RC.render(RC.check(rtext, jd), title, company))
        return 0
    finally:
        c.close()


def cmd_greet_check(args) -> int:
    """招呼语质量检查：单条 / 文件 / 库里全部。"""
    import greeting_kit as G

    if args.file:
        try:
            text = Path(args.file).read_text(encoding="utf-8")
        except OSError as e:
            print(f"❌ 读不到文件：{e}")
            return 2
        print(G.render(G.check(text)))
        return 0

    if args.text:
        print(G.render(G.check(args.text)))
        return 0

    # 不给文本 → 体检库里所有存下来的招呼语
    c = _db()
    try:
        rows = [r[0] for r in c.execute(
            "SELECT message FROM greetings WHERE coalesce(message,'')!=''")]
        total = c.execute("SELECT COUNT(*) FROM greetings").fetchone()[0]
    finally:
        c.close()

    if not rows:
        print("库里没有存下任何招呼语内容 —— 无法体检。")
        print("（早期版本的投递没有记录 message，这条从新版起才会存）")
        return 0

    print(f"招呼语体检：{len(rows)} 条有内容 / 共 {total} 条投递")
    if len(rows) < total:
        print(f"⚠ 有 {total - len(rows)} 条没存 message —— 早期版本没记录，无法回溯")
    print()
    scores = []
    for i, m in enumerate(rows, 1):
        r = G.check(m)
        scores.append(r["score"])
        print(f"[{i}] {r['score']}分({r['grade']}) {r['length']}字  "
              f"{(m or '')[:34].replace(chr(10), ' ')}…")
    avg = sum(scores) / len(scores)
    print()
    print(f"平均 {avg:.1f} 分 · 最高 {max(scores)} · 最低 {min(scores)}")
    if avg >= 85:
        print("整体已经不错，主要看回复率还有没有提升空间。")
    else:
        print("跑 python jobkit.py greet-check \"你的招呼语\" 看逐条问题。")
    return 0


def cmd_hr(args) -> int:
    """HR 高频问题应答库。"""
    import hr_answers

    try:
        if args.topic:
            print(hr_answers.render_one(args.topic, args.target, args.high))
        else:
            print(hr_answers.render(args.target, args.high))
    except KeyError:
        print(f"❌ 没有这个 topic：{args.topic}\n"
              f"可选：{' / '.join(hr_answers.all_topics())}")
        return 2
    return 0


def cmd_resume(args) -> int:
    """简历多版本：list / add / use + 效果统计。"""
    import resume_kit

    c = _db()
    try:
        action = (args.action or "list").lower()
        if action == "add":
            if not args.name or not args.path:
                print("add 需要 --name 和 --path，例如：\n"
                      "  python jobkit.py resume add --name \"Python后端-秋招\" "
                      "--direction python --path \"C:/面试/Resume.md\"")
                return 2
            try:
                rid = resume_kit.add(c, args.name, args.direction or "通用",
                                     args.path, note=args.note or "",
                                     make_active=True)
            except FileNotFoundError as e:
                print(f"❌ {e}")
                return 2
            print(f"✅ 已添加简历版本 [{rid}] {args.name}（{args.direction}），并设为当前使用")
            return 0

        if action == "use":
            if not args.id:
                print("use 需要 --id，先跑 python jobkit.py resume list 看有哪些版本")
                return 2
            if not resume_kit.activate(c, args.id):
                print(f"❌ 没有 id={args.id} 的简历版本")
                return 2
            print(f"✅ 已切换到版本 [{args.id}]")
            return 0

        print(resume_kit.render(c))
        return 0
    finally:
        c.close()


def cmd_funnel(_args) -> int:
    """求职漏斗 + 数据完整性体检。"""
    import funnel

    c = _db()
    try:
        print(funnel.render(funnel.snapshot(c)))
    finally:
        c.close()
    return 0


def cmd_interview(args) -> int:
    """按岗位生成面试题库（本地模型，0 成本）。"""
    import interview_kit as ik

    c = _db()
    if args.index:
        rows = [dict(r) for r in c.execute(
            "select * from jobs order by coalesce(match_score,0) desc limit ?", (args.index,))]
        if len(rows) < args.index:
            _say(f"[red]榜单里只有 {len(rows)} 个岗位[/red]")
            return 2
        job = rows[-1]
    else:
        job = dict(c.execute(
            "select * from jobs where length(coalesce(job_description,''))>100 "
            "order by coalesce(match_score,0) desc limit 1").fetchone() or {})
    if not job:
        _say("[red]没有带 JD 的岗位。先采集：python jobkit.py find 广州 大模型[/red]")
        return 2

    jd = job.get("job_description") or ""
    if len(jd) < 100:
        _say(f"[yellow]这个岗位的 JD 只有 {len(jd)} 字，题目可能不准。"
             f"先补 JD：python jobkit.py enrich[/yellow]")

    _say(f"岗位：[bold]{job.get('title')}[/bold] / {job.get('company')}")
    _say("正在用本地模型出题…")
    try:
        g = ik.generate(jd, args.resume, company=job.get("company") or "",
                        title=job.get("title") or "")
    except ik.QuestionParseError as e:
        _say(f"[red]出题失败：{e}[/red]")
        return 2
    _say("")
    print(ik.render(g))
    return 0


def cmd_gap(args) -> int:
    """空窗期怎么答 —— 简历怎么写、面试怎么说、哪些话不能说。"""
    import gap_kit

    if args.scenario:
        try:
            s = gap_kit.get(args.scenario)
        except KeyError as e:
            _say(f"[red]{e}[/red]")
            _say(f"可选：{', '.join(gap_kit.all_scenarios())}")
            return 2
        print(f"【{s['label']}】")
        print(f"\n一句话：{s['one_liner']}")
        print(f"\n展开版：{s['expanded']}")
        print(f"\n被追问：{s['followup']}")
        print(f"\n别踩：")
        for p in s["pitfalls"]:
            print(f"  ✗ {p}")
        return 0

    for key in gap_kit.all_scenarios():
        s = gap_kit.get(key)
        print(f"\n【{s['label']}】")
        print(f"  一句话：{s['one_liner']}")
        print(f"  被追问：{s['followup']}")

    print("\n" + "─" * 60)
    print("简历上怎么写：" + gap_kit.RESUME_ADVICE["why"])
    for i, h in enumerate(gap_kit.RESUME_ADVICE["how"], 1):
        print(f"  {i}. {h}")
    print("\n示例：")
    for line in gap_kit.RESUME_ADVICE["example"].splitlines():
        print("  " + line)
    print("\n通用原则：")
    for p in gap_kit.PRINCIPLES:
        print(f"  · {p}")
    print("\n看单个场景的完整答案：python jobkit.py gap --scenario project")
    return 0


def cmd_reply(args) -> int:
    """HR 问了话，起草一句可以直接回的。

    两条纪律：
    · 缺事实（住哪 / 在职吗 / 到岗时间）→ 停下来让你填，**不替你编**
    · 草稿只填不发，你自己看一眼点发送
    """
    import json

    import reply_draft as RD
    import store

    if args.init:
        RD.save_profile(RD.load_profile())
        _say(f"已生成 {RD.PROFILE_PATH}")
        for k, desc in RD.PROFILE_FIELDS.items():
            _say(f"  {k}：{desc}")
        _say("")
        _say("填好这几项就能自动出草稿了。空着也行，遇到要用的那条会提醒你填。")
        return 0

    prof = RD.load_profile()
    conn = store.connect()
    try:
        rows = store.list_conversations(conn, pending_only=True, limit=max(1, args.limit))
    finally:
        conn.close()

    items = []
    for r in rows:
        res = RD.draft(r.get("last_msg") or "", prof)
        items.append({"company": r.get("company") or "",
                      "hr_name": r.get("hr_name") or "",
                      "last_msg": r.get("last_msg") or "",
                      **res})

    if args.json:
        print(json.dumps(items, ensure_ascii=False))
        return 0

    if not items:
        _say("没有待回的消息。")
        return 0

    _say(f"待回 {len(items)} 条（画像：{'、'.join(f'{k}={v}' for k, v in prof.items() if v) or '空'}）")
    ready = [it for it in items if it["text"] and not it["skip"]]
    for it in items:
        head = f"{it['company'] or '（公司名缺失）'} · {it['hr_name'] or 'HR'}"
        _say("")
        _say(f"【{head}】")
        _say(f"  HR：{it['last_msg'][:64]}")
        if it["skip"]:
            _say(f"  → 不用回（{it['reason']}）")
        elif it["needs"]:
            _say("  → 缺事实，不能替你编：" +
                 "、".join(RD.PROFILE_FIELDS.get(n, n) for n in it["needs"]))
        elif it["text"]:
            _say(f"  → 建议回：{it['text']}")
        else:
            _say(f"  → {it['reason']}")

    if not args.fill:
        if ready:
            _say("")
            _say("要把哪条填进聊天框：python jobkit.py reply --fill <公司名>")
        return 0

    # 填进聊天框（不发）
    target = next((it for it in ready if args.fill in it["company"]), None)
    if target is None:
        _say(f"❌ 没有「{args.fill}」这条可直接回的草稿（可能缺事实或不需要回）")
        return 2
    import chat_send
    try:
        out = chat_send.send(target["company"], target["text"], dry_run=True, keep_text=True)
    except Exception as exc:                      # noqa: BLE001
        _say(f"❌ 填不进去：{type(exc).__name__}: {exc}")
        return 2
    row = ((out.get("conversation") or {}).get("row") or "").replace("\n", " ")[:40]
    _say(f"📝 已填入：{row}")
    _say("👉 到浏览器里看一眼，确认无误自己点发送。")
    return 0


def cmd_today(args) -> int:
    """每日指挥中心：一屏看完今天该干什么（只读库，不抓取）。"""
    import today as T

    # 委托给模块自己的 main —— 两处入口共用一套参数解析，不重复实现
    argv = []
    if getattr(args, "json", False):
        argv.append("--json")
    if getattr(args, "sync", False):
        argv.append("--sync")
    return T.main(argv)


def cmd_diagnose(args) -> int:
    """简历结构诊断：四块齐不齐 + 项目组合有没有硬伤。"""
    import resume_diagnose as RDG

    path = args.path
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        _say(f"❌ 读不到 {path}：{exc}")
        return 2
    print(RDG.render(RDG.diagnose(text)))
    return 0


def cmd_mock(args) -> int:
    """拿到真题后准备答题卡（考察点 / 骨架 / 必被追问 / 红线）。"""
    import mock_interview as MI
    import store

    text = args.text or ""
    if args.from_chat:
        conn = store.connect()
        try:
            rows = store.list_conversations(conn, pending_only=False, limit=200)
        finally:
            conn.close()
        hit = next((r for r in rows if args.from_chat in (r.get("company") or "")), None)
        if not hit:
            _say(f"❌ 找不到「{args.from_chat}」的会话")
            return 2
        text = hit.get("last_msg") or ""
        _say(f"取到 {hit.get('company')} · {hit.get('hr_name') or 'HR'} 的消息")

    if not text.strip():
        _say("给个题目：jobkit mock \"题目原文\"  或  jobkit mock --from-chat 公司名")
        return 1

    print(MI.render(MI.prepare(text)))
    return 0


def cmd_stack(args) -> int:
    """技术栈匹配 —— 投之前先看「这岗的栈你够不够」。

    存在理由（一手痛点）：JD 写 langchain，拿 Java 的 agent 去砸，
    筛那关就过不去。投得多但回得少时，先修「别投错的」。
    """
    import stack_match as sm

    if args.init:
        p = sm.save_profile()
        _say(f"画像模板已写到 {p}")
        _say("按你真实情况改 skills / never / core_lang，然后重跑 stack。")
        return 0

    import sqlite3
    import store

    prof = sm.load_profile()
    _say(f"[dim]画像来源：{prof.get('_来源')}[/dim]")

    conn = sqlite3.connect(store.DB_PATH)
    try:
        sql = ("select job_id, title, company, salary, job_description from jobs "
               "where job_description is not null and length(job_description) > 200")
        params: list = []
        if args.limit:
            sql += " limit ?"
            params.append(args.limit)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    if not rows:
        _say("库里没有带 JD 的岗位。先跑 `jobkit.py enrich` 补 JD。")
        return 2

    results = []
    for job_id, title, company, salary, jd in rows:
        r = sm.match({"title": title, "company": company, "job_description": jd}, prof)
        r.update({"job_id": job_id, "title": title, "company": company, "salary": salary})
        results.append(r)

    if args.only:
        results = [r for r in results if r["verdict"] == args.only]

    if args.json:
        import json as _json
        _say(_json.dumps(results, ensure_ascii=False, indent=1))
        return 0

    _say(sm.render(results))
    bad = sum(1 for r in results if r["verdict"] == "mismatch")
    if bad:
        _say(f"[dim]{bad} 个岗位技术栈硬不匹配 —— 投这些基本等于把机会扔了。[/dim]")
    return 0


def cmd_companies(args) -> int:
    """列出库里的外包/人力派遣公司 —— 投之前先看清是谁家在招。"""
    import company_guard as cg

    items = cg.scan_db()
    confirmed = [x for x in items if x["level"] == "confirmed"]
    suspected = [x for x in items if x["level"] == "suspected"]
    if not items:
        _say("库里没识别出外包公司。")
        return 0
    body = [[("⛔" if x["level"] == "confirmed" else "⚠"),
             _cut(x["company"], 24), str(x["jobs"]), _cut(x["reason"], 44)]
            for x in items[: args.limit]]
    _table(f"外包/派遣 {len(items)} 家（明确 {len(confirmed)} / 疑似 {len(suspected)}）"
           f"，涉及 {sum(x['jobs'] for x in items)} 个岗位",
           ["", "公司", "岗位数", "依据"], body)
    _say("")
    _say("[dim]默认不拦截（外包不一定差）。要「只投自研」，在设置里开启公司过滤。[/dim]")
    return 0


def cmd_chat(args) -> int:
    """抓一次 BOSS 聊天页，告诉你谁回了话。

    分类的意义：系统消息（简历已发送）和真实回复混在一起时，用户会白高兴，
    所以这里按 面试/拒信/附件/系统/普通 分开列。
    """
    import chat_sync

    try:
        r = chat_sync.sync()
    except chat_sync.ChatUnavailable as exc:
        print(f"抓不到聊天页：{exc}")
        print("（浏览器没开 / 没登录 / 页面改版，都不是「今天没人理你」）")
        return 2
    s = r["summary"]
    print(f"会话 {s['total']} 条，待处理 {s['pending']} 条"
          f"（新增 {r['added']}，有变化 {r['changed']}）")
    labels = (("面试/邀约", "interview"), ("拒信", "rejected"),
              ("附件/简历卡片", "resume_sent"), ("系统消息", "system"),
              ("普通消息", "normal"))
    for label, key in labels:
        items = s.get(key) or []
        if not items:
            continue
        print(f"\n【{label}】{len(items)} 条")
        for it in items[: args.limit]:
            print(f"  {it['hr']} / {it['company']}"
                  + (f" / {it['position']}" if it.get("position") else "")
                  + f"  [{it.get('time','')}]")
            print(f"    {it['last_msg'][:110]}")
    return 0


def cmd_status(_args) -> int:
    import followup  # 复用它的统计口径，别造第二套
    c = _db()
    rows = c.execute(
        """select status, count(*) n from greetings group by status order by n desc"""
    ).fetchall()
    if not rows:
        _say("还没有投递记录。跑 [bold]python jobkit.py apply[/bold] 开始。")
        return 0
    _table("投递进度", ["状态", "数量"],
           [[str(r["status"] or "未知"), str(r["n"])] for r in rows])
    return 0


# ── 入口 ────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="jobkit",
        description="求职工作台。不带参数 = 看「今天该投什么」。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
例子:
  python jobkit.py                     看板（最常用）
  python jobkit.py find 广州 Python    采一批岗位
  python jobkit.py find 佛山 AI --source zhilian
  python jobkit.py list --pending      还没投的
  python jobkit.py apply               投递（默认演练，加 --send 才真发）
  python jobkit.py status              投递进度
""")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("find", help="采集岗位")
    p.add_argument("positional", nargs="*", help="城市 关键词（顺序随意）")
    p.add_argument("--keyword", default="")
    p.add_argument("--city", default="")
    p.add_argument("--source", choices=["auto", "boss", "zhilian"], default="auto")
    p.add_argument("--max", type=int, default=3, help="本轮最多搜几次")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("list", help="列出岗位")
    p.add_argument("--city", default="")
    p.add_argument("--min-score", type=float, default=None, dest="min_score")
    p.add_argument("--pending", action="store_true", help="只看没投过的")
    p.add_argument("--rejected", action="store_true", help="反过来：看被筛掉的及其原因")
    p.add_argument("--all", action="store_true", help="不筛选，全部列出")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="看榜单里第 N 个岗位的详情")
    p.add_argument("index", type=int, metavar="N", help="榜单序号（从 1 开始）")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("enrich", help="给 BOSS 岗位补 JD 全文（详情页）")
    p.add_argument("--limit", type=int, default=20, help="本次最多补几条（默认 20）")
    p.add_argument("--all", action="store_true", help="补当前这批的全部，不设上限")
    p.add_argument("--all-jobs", action="store_true", dest="all_jobs",
                   help="连被筛掉的岗位也补（很慢，一般没必要）")
    p.set_defaults(func=cmd_enrich)

    p = sub.add_parser("apply", help="投递（默认演练）")
    p.add_argument("--send", action="store_true", help="真的发出去（不加速度只演练）")
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("status", help="投递进度")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("chat", help="抓 BOSS 聊天页，看谁回了话")
    p.add_argument("--limit", type=int, default=20, help="每类最多显示几条")
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("companies", help="列出库里的外包/人力派遣公司")
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(func=cmd_companies)

    p = sub.add_parser("reply", help="HR 问了话，起草一句可以直接回的")
    p.add_argument("--init", action="store_true", help="生成 reply_profile.json（城市/在职状态等）")
    p.add_argument("--fill", metavar="公司名",
                   help="把这条草稿填进 BOSS 聊天框（不发，你自己点发送）")
    p.add_argument("--limit", type=int, default=10, help="看前 N 条待回")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    p.set_defaults(func=cmd_reply)

    p = sub.add_parser("today", help="每日指挥中心：一屏看完今天该干什么")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    p.add_argument("--sync", action="store_true",
                   help="先抓一次聊天页再算（需要浏览器在 /web/geek/chat）")
    p.set_defaults(func=cmd_today)

    p = sub.add_parser("diagnose", help="简历结构诊断：项目四块 + 组合硬伤")
    p.add_argument("--path", default=r"C:\面试\Resume.md", help="简历 Markdown 路径")
    p.set_defaults(func=cmd_diagnose)

    p = sub.add_parser("mock", help="拿到真题后准备答题卡（骨架/追问/红线）")
    p.add_argument("text", nargs="?", default="", help="题目原文")
    p.add_argument("--from-chat", metavar="公司名", help="从库里的会话取最后一（几）条消息当题目")
    p.set_defaults(func=cmd_mock)

    p = sub.add_parser("stack", help="技术栈匹配：哪些岗的栈你够、哪些别投")
    p.add_argument("--init", action="store_true", help="生成画像模板（stack_profile.json）")
    p.add_argument("--limit", type=int, default=0, help="只看前 N 个岗位（默认全部）")
    p.add_argument("--only", default="",
                   choices=["", "mismatch", "weak", "ok", "strong", "unknown"],
                   help="只看某一档")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    p.set_defaults(func=cmd_stack)

    p = sub.add_parser("gap", help="空窗期怎么答（简历 + 面试话术）")
    p.add_argument("--scenario", default="", help="project/exam/family/freelance")
    p.set_defaults(func=cmd_gap)

    p = sub.add_parser("interview", help="按岗位生成面试题库（本地模型）")
    p.add_argument("--index", type=int, default=0, help="榜单第 N 个岗位，默认取最高分")
    p.add_argument("--resume", default="2025届，Python后端 + LLM Agent + AIGC 方向",
                   help="你的技术背景（一句话即可）")
    p.set_defaults(func=cmd_interview)

    p = sub.add_parser("funnel", help="求职漏斗 + 数据完整性体检")
    p.set_defaults(func=cmd_funnel)

    p = sub.add_parser("resume-check", help="简历 × 岗位 JD 匹配度诊断")
    p.add_argument("--index", type=int, default=0, help="榜单第 N 个岗位")
    p.add_argument("--path", help="简历文件路径（默认取当前启用版本）")
    p.add_argument("--jd-file", help="直接指定 JD 文本文件")
    p.set_defaults(func=cmd_resume_check)

    p = sub.add_parser("commute", help="通勤估算（区镇中心直线距离）")
    p.add_argument("--set-home", dest="set_home", help="设置家的区镇，如「威远岛」")
    p.add_argument("--set-home-city", dest="set_home_city", help="家所在市，如「东莞」")
    p.add_argument("--home", help="家的区/镇（覆盖已存配置）")
    p.add_argument("--city", help="家所在市（覆盖已存配置）")
    p.add_argument("--index", type=int, default=0, help="榜单第 N 个岗位")
    p.add_argument("--district", help="直接指定岗位区镇")
    p.add_argument("--district-city", dest="district_city", help="岗位所在市（默认同家）")
    p.set_defaults(func=cmd_commute)

    p = sub.add_parser("company", help="公司画像（仅本地数据，不做尽调）")
    p.add_argument("name", help="公司名")
    p.set_defaults(func=cmd_company)

    p = sub.add_parser("redline", help="岗位红线检测（投之前筛坑岗）")
    p.add_argument("--scan", action="store_true", help="扫全库统计")
    p.add_argument("--query", help="按标题/公司名模糊找")
    p.add_argument("--index", type=int, default=0, help="榜单第 N 个岗位")
    p.set_defaults(func=cmd_redline)

    p = sub.add_parser("resume-batch", help="简历 × 一批岗：跨岗看缺什么词")
    p.add_argument("--n", type=int, default=20, help="看 priority 前几个岗")
    p.add_argument("--top", type=int, default=5, help="补几个词能覆盖最多岗")
    p.add_argument("--all", action="store_true", help="全库盘点（不只优先岗）")
    p.add_argument("--path", help="简历文件路径（默认用启用的版本）")
    p.set_defaults(func=cmd_resume_batch)

    p = sub.add_parser("priority", help="今日优先投哪几个（综合排序）")
    p.add_argument("--n", type=int, default=10, help="看前几个，默认 10")
    p.add_argument("--city", help="只看这个城市")
    p.set_defaults(func=cmd_priority)

    p = sub.add_parser("review", help="面试复盘（面完记问题，下次不重踩）")
    p.add_argument("--add", action="store_true", help="记一场面试")
    p.add_argument("--company", help="公司名")
    p.add_argument("--position", help="岗位")
    p.add_argument("--stage", help="轮次：初面/二面/技术面/HR面/终面")
    p.add_argument("--note", help="备注")
    p.add_argument("--id", type=int, help="面试编号")
    p.add_argument("--question", help="记一个问题")
    p.add_argument("--answer", help="我当时怎么答的")
    p.add_argument("--score", type=int, default=0, help="自评 0-5（0=未评）")
    p.add_argument("--better", help="下次该怎么答")
    p.add_argument("--result", help="更新结果：pending/pass/reject")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("weekly", help="求职周报（本期做了什么、卡在哪）")
    p.add_argument("--week", type=int, default=0, help="0=本周，-1=上周")
    p.add_argument("--set-target", action="append",
                   help="设每周目标，如 apply=20，可多次")
    p.set_defaults(func=cmd_weekly)

    p = sub.add_parser("living", help="异地入职收支测算（到手 - 支出，可套预置档位）")
    p.add_argument("--city", help="城市（不给就从榜单岗位读）")
    p.add_argument("--salary", type=float, help="税前月薪，单位 K，如 9")
    p.add_argument("--district", help="区/镇街（如 南山区/虎门镇/南城街道），用于取预置档位")
    p.add_argument("--tier", choices=["frugal", "normal"],
                   help="生活档位：frugal 省着过 / normal 正常（给了就用预置数据）")
    p.add_argument("--scene", choices=["rent", "home", "co_rent"], default="rent",
                   help="住法：rent 租房（默认）/ home 东莞住家 / co_rent 佛山三水合租AA")
    p.add_argument("--compare", action="store_true",
                   help="对比：同城各区 × 两档（给 --district 则只比该区各档）")
    p.add_argument("--cities", action="store_true", help="看预置数据覆盖哪些城市/区")
    p.add_argument("--rent", type=float, help="月租，元（手填，优先于预置）")
    p.add_argument("--food", type=float, help="每月吃饭，元")
    p.add_argument("--transport", type=float, help="每月交通，元")
    p.add_argument("--other", type=float, help="每月其他（水电+通讯日杂），元")
    p.add_argument("--index", type=int, default=0, help="榜单第 N 个岗位")
    p.set_defaults(func=cmd_living)

    p = sub.add_parser("greet-check", help="招呼语质量检查（评分 + 逐条问题）")
    p.add_argument("text", nargs="?", help="要检查的招呼语文本")
    p.add_argument("--file", help="从文件读")
    p.set_defaults(func=cmd_greet_check)

    p = sub.add_parser("hr", help="HR 高频问题应答库（薪资/到岗/离职/加班/反问）")
    p.add_argument("--topic", help="salary / onboard / leave / overtime / why_us / questions")
    p.add_argument("--target", default="8-9K", help="你的目标月薪区间")
    p.add_argument("--high", default="10-11K", help="进攻版报的高位区间")
    p.set_defaults(func=cmd_hr)

    p = sub.add_parser("resume", help="简历多版本与效果统计")
    p.add_argument("action", nargs="?", default="list",
                   choices=["list", "add", "use"])
    p.add_argument("--id", type=int, default=0, help="use 时指定版本 id")
    p.add_argument("--name", help="版本名，如「Python后端-秋招」")
    p.add_argument("--direction", default="通用", help="方向，如 python/ai/aigc")
    p.add_argument("--path", help="简历文件路径")
    p.add_argument("--note", default="", help="备注")
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("web", help="打开图形界面（薪资/Offer/简历体检/模拟面试）")
    p.add_argument("--port", type=int, default=None, help="端口（默认自动避开系统保留段）")
    p.set_defaults(func=cmd_web)

    ap.set_defaults(func=cmd_home)
    return ap


CITY_HINT = ("广州", "深圳", "佛山", "东莞", "惠州", "珠海", "中山", "北京", "上海", "杭州")


def _absorb_positionals(args) -> None:
    """`find 广州 Python` —— 城市和关键词都不加 -- 前缀也能认出来。

    城市名在已知城市表里就当地市，剩下的拼成关键词。用户不用背参数名。
    """
    if getattr(args, "cmd", "") != "find":
        return
    parts = list(getattr(args, "positional", []) or [])
    rest: list[str] = []
    for p in parts:
        name = p.strip()
        if not args.city and (name in CITY_HINT or name.endswith("市")):
            args.city = name[:-1] if name.endswith("市") else name
        else:
            rest.append(name)
    if rest and not args.keyword:
        args.keyword = " ".join(rest)


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    _absorb_positionals(args)
    if getattr(args, "cmd", "") == "find" and not args.keyword:
        _say("要搜什么？例如：[bold]python jobkit.py find 广州 Python[/bold]")
        return 2
    try:
        return int(args.func(args) or 0)
    finally:
        global _CONN
        if _CONN is not None:
            try:
                _CONN.close()
            except Exception:                    # noqa: BLE001
                pass
            _CONN = None


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断。")
        sys.exit(130)
    except Exception as e:                       # noqa: BLE001
        print(f"\n❌ 出错了：{type(e).__name__}: {e}")
        print("   这是 jobkit 内部的问题，不需要你改代码。")
        import os
        if os.environ.get("JOBKIT_DEBUG"):
            raise
        sys.exit(1)
