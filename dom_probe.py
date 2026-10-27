#!/usr/bin/env python3
"""DOM 通道真机探测：起一个带调试端口的浏览器 → 你登录 → 自动跑一轮抓取诊断。

为什么需要它
    dom_channel.fetch_jobs() 只连「已经开着调试端口的浏览器」，自己不启动、不导航登录。
    没有这一步，DOM 通道永远只有桩测试，选择器命中率是未知数。

用法
    python dom_probe.py                # 起浏览器并等你登录，然后探测（默认）
    python dom_probe.py --no-wait      # 已经登录过，直接探测
    python dom_probe.py --keyword AI应用开发 --city 广州 --limit 5
    python dom_probe.py --detail       # 额外抓一条详情页（详情选择器是自拟的，最该验）

注意
    用的是**独立 profile**（`.browser_profile/`），不碰你日常浏览器的登录态；
    第一次要扫码，之后 profile 会记住。要清干净就删掉那个目录。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

PORT = 9222
PROFILE = HERE / ".browser_profile"
CDP = f"http://127.0.0.1:{PORT}"

CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def find_browser() -> str | None:
    for p in CANDIDATES:
        if Path(p).exists():
            return p
    return None


def _opener():
    # 本机代理会把 localhost 也代理掉（实测 502），直连
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def cdp_alive(timeout: float = 1.5) -> dict | None:
    try:
        with _opener().open(CDP + "/json/version", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def launch(browser: str) -> None:
    PROFILE.mkdir(parents=True, exist_ok=True)
    args = [
        browser,
        f"--remote-debugging-port={PORT}",
        f"--user-data-dir={PROFILE}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate",
        # 禁用系统代理：本机 7899 兜底代理已挂（curl 走它返回 000），浏览器
        # 继承它会让**所有**导航变成 ERR_CONNECTION_CLOSED —— 而 zhipin 直连
        # 是 200。这个坑很隐蔽：标签页 URL 看着是对的，页面内容却是错误页。
        "--no-proxy-server",
        # 双保险：系统代理（HKCU ProxyServer=127.0.0.1:7899）是开着的，而那个
        # 代理已挂。只用 --no-proxy-server 在部分 Edge 版本上仍会走 WinINET
        # 的设置，所以显式指定 direct。
        "--proxy-server=direct://",
        "--proxy-bypass-list=*",
        "https://www.zhipin.com/web/user/?ka=header-login",
    ]
    print(f"  启动: {Path(browser).name}  profile={PROFILE}")
    subprocess.Popen(args, close_fds=True)
    for i in range(30):
        time.sleep(1)
        if cdp_alive():
            print(f"  ✅ 调试端口已就绪（{i + 1}s）")
            return
    raise SystemExit("❌ 30s 内调试端口没起来：检查是否已有同 profile 的实例在跑")


def ensure_browser(entry_url: str = "", wait: float = 25.0) -> tuple[bool, str]:
    """确保调试端口可用 —— 没有就自动拉起浏览器。返回 (ok, 人能看懂的原因)。

    采集入口都该先调它：用户不该为了「浏览器没开」去翻文档或手动加命令行参数。
    用独立 profile（.browser_profile/），不碰日常浏览器的登录态。
    """
    ver = cdp_alive()
    if ver:
        return True, f"复用已开的浏览器（{ver.get('Browser', '?')}）"

    browser = find_browser()
    if not browser:
        return False, "没找到 Edge/Chrome。请装一个，或设置环境变量 JOBKIT_BROWSER 指向浏览器路径"

    PROFILE.mkdir(parents=True, exist_ok=True)
    args = [
        browser,
        f"--remote-debugging-port={PORT}",
        f"--user-data-dir={PROFILE}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate",
        # 禁用系统代理：本机 7899 兜底代理已挂（curl 走它返回 000），浏览器
        # 继承它会让**所有**导航变成 ERR_CONNECTION_CLOSED —— 而 zhipin 直连
        # 是 200。这个坑很隐蔽：标签页 URL 看着是对的，页面内容却是错误页。
        "--no-proxy-server",
        # 双保险：系统代理（HKCU ProxyServer=127.0.0.1:7899）是开着的，而那个
        # 代理已挂。只用 --no-proxy-server 在部分 Edge 版本上仍会走 WinINET
        # 的设置，所以显式指定 direct。
        "--proxy-server=direct://",
        "--proxy-bypass-list=*",
        entry_url or "about:blank",
    ]
    try:
        subprocess.Popen(args, close_fds=True)
    except OSError as e:
        return False, f"浏览器启动失败：{e}"

    limit = max(1, int(wait))
    for i in range(limit):
        time.sleep(1)
        if cdp_alive():
            return True, f"已自动启动浏览器（{i + 1}s）"
    return False, (f"{limit}s 内调试端口没起来。多半是已有一个用同一 profile 的浏览器在跑——"
                   f"把它关掉再试")


def main() -> int:
    ap = argparse.ArgumentParser(description="DOM 通道真机探测")
    ap.add_argument("--keyword", default="AI应用开发")
    ap.add_argument("--city", default="广州")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--no-wait", action="store_true", help="跳过登录等待")
    ap.add_argument("--detail", action="store_true", help="额外抓一条详情页")
    args = ap.parse_args()

    ver = cdp_alive()
    if ver:
        print(f"✅ 已有调试端口：{ver.get('Browser', '?')}")
    else:
        browser = find_browser()
        if not browser:
            print("❌ 没找到 Edge/Chrome，请手动指定路径后改 CANDIDATES")
            return 2
        launch(browser)

    if not args.no_wait:
        print("\n" + "═" * 60)
        print("  请在弹出的浏览器窗口里登录 BOSS 直聘（扫码）")
        print("  登录完成、能看到右上角头像后，回到这里按回车")
        print("═" * 60)
        try:
            input("  按回车继续… ")
        except EOFError:
            print("\n  （无交互终端，跳过等待；如未登录会报 not-logged-in）")

    import dom_channel

    print(f"\n── 列表页探测：{args.keyword} @ {args.city}")
    t0 = time.time()
    ok, jobs, reason = dom_channel.fetch_jobs(args.keyword, args.city, limit=args.limit)
    dt = time.time() - t0
    print(f"  ok={ok} reason={reason} 耗时={dt:.1f}s")

    if not ok:
        print("\n  ❌ 通道失败。对照原因处理：")
        hints = {
            "not-logged-in": "没登录 —— 重新扫码，或确认 profile 目录没被清",
            "selector-miss": "选择器失效 —— BOSS 可能改版了，需要更新 dom/boss_selectors.py",
            "restricted": "命中风控 —— 立刻停，别重试；等几小时再试",
            "page-error": "页面/连接问题 —— 看上面是否有 CDP 或导航错误",
        }
        print(f"     {hints.get(reason, '未知原因')}")
        return 1

    print(f"  拿到 {len(jobs)} 条岗位：\n")
    for i, j in enumerate(jobs[: args.limit], 1):
        print(f"  [{i}] {j.get('title') or '(无标题)'} | {j.get('salary') or '(无薪资)'} | "
              f"{j.get('company') or '(无公司)'} | {j.get('city')}/{j.get('district')}")
        print(f"      exp={j.get('experience')!r} edu={j.get('education')!r} "
              f"skills={j.get('skills')} active={j.get('boss_active')!r}")
        print(f"      security_id={j.get('security_id')!r} job_url={j.get('job_url')}")

    # 逐字段命中率 —— 这才是选择器的真实体检
    fields = ["title", "salary", "company", "city", "district", "experience",
              "education", "skills", "boss_active", "security_id", "job_url"]
    print("\n  字段命中率：")
    for f in fields:
        hit = sum(1 for j in jobs if j.get(f))
        bar = "█" * round(hit / max(len(jobs), 1) * 20)
        print(f"    {f:<13} {hit}/{len(jobs)} {bar}")

    if args.detail and jobs:
        url = jobs[0].get("job_url") or ""
        if not url:
            print("\n  ⚠️ 首条没有 job_url，跳过详情探测")
        else:
            print(f"\n── 详情页探测：{url}")
            ok2, job, reason2 = dom_channel.fetch_detail(url)
            print(f"  ok={ok2} reason={reason2}")
            if ok2 and job:
                for k in ("title", "salary", "company", "job_description", "introduce"):
                    v = str(job.get(k) or "")
                    print(f"    {k:<16} {len(v):>5} 字  {v[:60]!r}")
                if not job.get("job_description"):
                    print("    ⚠️ 职位描述为空 —— DETAIL_* 选择器要调")

    print("\n完成。想单独跑通道："
          f'python -c "import dom_channel; print(dom_channel.fetch_jobs(\'{args.keyword}\',\'{args.city}\'))"')
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
