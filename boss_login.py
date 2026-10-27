#!/usr/bin/env python3
"""一键 BOSS 登录 —— 自己拉起浏览器，不用手工开 Edge。

流程（一条命令跑完，你只需在弹出的窗口里扫一次码）：
  1. 启动带 CDP 的 Edge（独立 profile，不碰你的日常浏览器）
  2. 打开 zhipin.com 让你扫码
  3. 轮询检测登录态，扫描到 wt2 + __zp_stoken__ 后自动抓取
  4. 写回 boss-agent-cli 的 session.enc 并校验

登录窗口**跑完后保持打开**：BOSS 的打招呼/投递动作需要这个 CDP 浏览器在线。

用法：
    python boss_login.py            # 默认等 300 秒
    python boss_login.py --wait 600 # 等 10 分钟
    python boss_login.py --kill     # 先杀掉 9222 上的陈旧进程再启动
    python boss_login.py --status   # 只看当前登录态，不启动浏览器
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
PROFILE_DIR = r"C:\Users\<user>\AppData\Local\Microsoft\Edge\User Data\BossLoginCDP"
CLI_DIR = Path(r"C:\Users\<user>\boss-agent-cli-dev")
CDP_PORT = 9222
LOGIN_URL = "https://www.zhipin.com/"


def find_edge() -> str | None:
    for p in EDGE_CANDIDATES:
        if Path(p).exists():
            return p
    return shutil.which("msedge")


def clean_env() -> dict[str, str]:
    """httpx 会走 Clash 代理导致 localhost:9222 返回 502，必须清掉代理变量。"""
    env = os.environ.copy()
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        env.pop(k, None)
    env["NO_PROXY"] = "localhost,127.0.0.1"
    return env


def cdp_pages() -> list[dict]:
    """列出 CDP 当前打开的页面。"""
    try:
        with urllib.request.urlopen(f"http://localhost:{CDP_PORT}/json", timeout=4) as r:
            return [t for t in json.loads(r.read().decode("utf-8", "replace"))
                    if t.get("type") == "page"]
    except Exception:
        return []


def cdp_login_hint() -> tuple[bool, str]:
    """粗判浏览器里是否已离开登录页。

    只为省掉「每次都触发 boss login --cdp 挂死 90 秒」；真正的判定仍以
    session 里出现 __zp_stoken__ 为准。
    """
    pages = [p for p in cdp_pages() if "zhipin" in (p.get("url") or "")]
    if not pages:
        return False, "浏览器里没有 BOSS 页面"
    for p in pages:
        url = p.get("url") or ""
        title = p.get("title") or ""
        if "/web/user" in url or "注册登录" in title or "登录" in title:
            continue
        if "zhipin.com" in url:
            return True, f"已离开登录页：{title[:40]}"
    return False, "仍在登录页"


def cdp_alive(timeout: float = 3.0) -> str | None:
    """9222 上有 CDP 就返回它的 Browser 版本串。"""
    try:
        with urllib.request.urlopen(
            f"http://localhost:{CDP_PORT}/json/version", timeout=timeout
        ) as r:
            return json.loads(r.read().decode("utf-8", "replace")).get("Browser", "unknown")
    except Exception:
        return None


def boss_cmd() -> list[str]:
    """优先用 dev 版（含 patch），否则回落到 PATH 里的 boss。"""
    if (CLI_DIR / "pyproject.toml").exists() and shutil.which("uv"):
        return ["uv", "run", "--directory", str(CLI_DIR), "boss"]
    exe = shutil.which("boss")
    if exe:
        return [exe]
    raise RuntimeError("找不到 boss 命令（也没装 uv）")


def run_boss(args: list[str], timeout: int = 60) -> tuple[bool, str]:
    try:
        p = subprocess.run(
            boss_cmd() + args,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=clean_env(), cwd=str(CLI_DIR),
        )
        return p.returncode == 0, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return False, f"命令超时（{timeout}s）: boss {' '.join(args)}"


def parse_json(text: str) -> dict | None:
    """boss 输出里找第一个完整 JSON 对象。"""
    start = text.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i, ch in enumerate(text[start:], start):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def probe_status() -> dict:
    """返回 {ok, state, summary, checks{}, missing[]}"""
    _, out = run_boss(["status"], timeout=45)
    d = parse_json(out) or {}
    data = d.get("data", {}) or {}
    health = data.get("auth_health", {}) or {}
    # checks 可能在 data.checks，也可能在 data.auth_health.checks（版本差异）
    raw_checks = data.get("checks") or health.get("checks") or []
    checks = {c.get("name"): c for c in raw_checks}
    missing = [k for k, v in checks.items() if v.get("status") == "warn"]
    return {
        "ok": bool(d.get("ok")),
        "state": data.get("auth_state") or health.get("auth_state") or "unknown",
        "summary": data.get("auth_summary") or health.get("summary") or "",
        "checks": checks,
        "missing": missing,
        "raw": d,
    }


def kill_port_holder() -> bool:
    """杀掉占用 9222 的进程（只杀监听该端口的 pid）。"""
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                             timeout=20).stdout
    except Exception:
        return False
    pids = set()
    for line in out.splitlines():
        if f":{CDP_PORT}" in line and "LISTENING" in line.upper():
            m = re.search(r"(\d+)\s*$", line.strip())
            if m:
                pids.add(m.group(1))
    for pid in pids:
        subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, timeout=20)
        print(f"  · 已结束占用 {CDP_PORT} 的进程 PID={pid}")
    return bool(pids)


def launch_edge(edge: str, profile: str) -> subprocess.Popen:
    args = [
        edge,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate",
        "--new-window", LOGIN_URL,
    ]
    # 用 DETACHED 起，避免随本进程退出被带走
    return subprocess.Popen(
        args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="一键 BOSS 登录（自建）")
    ap.add_argument("--wait", type=int, default=300,
                    help="等待扫码的秒数，默认 300；设为 0 表示一直等")
    ap.add_argument("--profile", default=PROFILE_DIR, help="CDP 专用浏览器 profile 目录")
    ap.add_argument("--kill", action="store_true", help="先杀掉 9222 上的陈旧进程")
    ap.add_argument("--status", action="store_true", help="只查登录态，不启动浏览器")
    args = ap.parse_args()

    print("═" * 58)
    print("BOSS 登录（自建 jobkit）")
    print("═" * 58)

    # ── 1. 现状 ──────────────────────────────────────────────
    st = probe_status()
    print(f"\n[1/4] 当前登录态：{st['state']}（{st['summary'] or '无说明'}）")
    if st["missing"]:
        print(f"      缺失项：{', '.join(st['missing'])}")
    if st["state"] in ("ok", "full", "complete", "healthy") and not st["missing"]:
        print("\n✅ 登录态完整，无需重新登录。")
        return 0
    if args.status:
        print("\n⚠️ 登录态不完整。去掉 --status 即可启动浏览器扫码。")
        return 1

    # ── 2. 浏览器 ────────────────────────────────────────────
    print("\n[2/4] 准备带调试端口的浏览器")
    ver = cdp_alive()
    if ver and args.kill:
        kill_port_holder()
        time.sleep(2)
        ver = cdp_alive()
    if ver:
        print(f"      ✅ 已在运行：{ver}")
        print(f"      如果窗口里不是登录页，请手动打开 {LOGIN_URL}")
    else:
        edge = find_edge()
        if not edge:
            print("      ❌ 找不到 Edge，无法自动启动", file=sys.stderr)
            return 2
        print(f"      · 启动 {Path(edge).name}（profile: {args.profile}）")
        launch_edge(edge, args.profile)
        for _ in range(20):
            time.sleep(1)
            ver = cdp_alive()
            if ver:
                break
        if not ver:
            print("      ❌ 浏览器起来了但调试端口没响应", file=sys.stderr)
            return 2
        print(f"      ✅ 调试端口就绪：{ver}")

    # ── 3. 等扫码 ────────────────────────────────────────────
    print(f"\n[3/4] 请在弹出的窗口里扫码登录 BOSS 直聘")
    if args.wait > 0:
        print(f"      最长等待 {args.wait} 秒，检测到登录会自动继续……")
    else:
        print(f"      一直等（Ctrl-C 结束），检测到登录会自动继续……")
    deadline = time.time() + args.wait if args.wait > 0 else float("inf")
    attempt = 0
    logged_in = False
    while time.time() < deadline:
        attempt += 1
        left = int(deadline - time.time()) if args.wait > 0 else -1

        # 先看浏览器是否已离开登录页 —— 避免每次都触发 boss login 挂死 90 秒
        hint_ok, hint = cdp_login_hint()
        if not hint_ok:
            print(f"      · 第 {attempt} 次观察：{hint}，等待扫码…")
            time.sleep(8)
            continue

        print(f"      · 第 {attempt} 次观察：{hint} → 抓取 session")
        t0 = time.time()
        # 注意：不能用 boss login --cdp —— 它依赖的 patchright driver 在本机 Edge 上会崩
        # （Connection closed while reading from the driver）。改用纯标准库 CDP 直读 cookie，
        # 见 boss_session.py 的完整排查记录。
        try:
            import boss_session
            data = boss_session.fetch_from_browser()
            ck = data["cookies"]
            if not ck.get("wt2"):
                print("      ⚠️ 浏览器里没有 wt2，继续等")
                time.sleep(8)
                continue
            token = {"cookies": ck, "stoken": data["stoken"], "user_agent": data["user_agent"]}
            if boss_session.SESSION.exists():
                (boss_session.SESSION.with_suffix(".enc.bak")
                 .write_bytes(boss_session.SESSION.read_bytes()))
            boss_session.save_token(token)
            st = probe_status()
            stoken_ok = st["checks"].get("stoken_presence", {}).get("status") == "ok"
            print(f"      · 第 {attempt} 次：抓到 {len(ck)} 个 cookie，"
                  f"stoken {'✅ 有（%d）' % len(data['stoken']) if data['stoken'] else '❌ 无'}"
                  f"，耗时 {time.time() - t0:.1f}s")
            if stoken_ok:
                logged_in = True
                break
            print("      ⚠️ 写入成功但 boss 仍报缺 stoken，稍后重试")
        except Exception as e:
            print(f"      ⚠️ 抓取失败（{type(e).__name__}: {str(e)[:100]}），重试")
            time.sleep(8)
            continue

    print()
    if not logged_in:
        print(f"\n⏰ {args.wait} 秒内没检测到完整登录态。")
        print("   浏览器窗口保留着，你扫完码后再跑一次 python boss_login.py 即可。")
        print(f"   若反复拿到 partial（wt2 在、stoken 缺）：清掉 {args.profile}\\Default\\Cookies 再重试。")
        return 1

    # ── 4. 校验 ──────────────────────────────────────────────
    print("\n[4/4] 校验登录态")
    st = probe_status()
    print(f"      状态：{st['state']}（{st['summary']}）")
    for name, c in st["checks"].items():
        mark = {"ok": "✅", "warn": "⚠️", "error": "❌"}.get(c.get("status"), "•")
        print(f"      {mark} {name}: {c.get('detail', '')[:76]}")
    print()
    if st["state"] in ("ok", "full", "complete", "healthy") and not st["missing"]:
        print("✅ 登录完成，session 已写入。")
        print("   正在关闭登录用的浏览器（重要）…")
        if kill_port_holder():
            time.sleep(2)
        print("   ✅ 已关闭。")
        print()
        print("   ⚠️ 关键：这个 CDP 浏览器**必须保持关闭**。实测它在运行时，")
        print("      patchright 的 connect_over_cdp 会让 Node driver 崩溃，")
        print("      导致 search / greet 全部卡死。关闭后走 headless 模式才正常。")
        print()
        print("   下一步：python apply_pipeline.py --dry-run")
        return 0
    print("⚠️ 登录态仍不完整，再扫一次或按上面的提示清 cookie 重试。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
