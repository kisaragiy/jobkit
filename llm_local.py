#!/usr/bin/env python3
"""本地 ollama 调用适配器 —— jobkit 的小模型入口。

⛔ 为什么不用 OpenAI 兼容端点（实测 2026-09-20，这是血的教训）：

    qwen3.5:9b 走 /v1/chat/completions  → content = **0 字**（13s）  一片空白
    qwen3.5:9b 走 /api/chat + think:false → 正常返回 JSON（**2s**）  能用

    qwen3 系是**思考模型**：思考内容不写进 content 字段。
    走 OpenAI 兼容端点时，思考把 token 预算吃光，正文就空了 ——
    表现为"模型跑了一百多秒然后说找不到 JSON"。
    走原生端点 + think:false 关掉思考，又快又稳。

用法：
    python llm_local.py --status                 # ollama 活着吗、有哪些模型
    python llm_local.py --list                   # 只列模型
    python llm_local.py --chat "你好"             # 单轮测试
    python llm_local.py --chat "..." -m qwen3.5:9b
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent

# ollama 在 WSL 里，地址会漂移；用 wsl-ollama.py 同步（见 SOUL 环境事实）
WSL_OLLAMA = Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "scripts" / "wsl-ollama.py"

# 首选模型（按"能干结构化小任务"排序，实测过）
PREFERRED = ["qwen3.5:9b", "qwen3:1.7b", "user-coder:latest", "qwen3:14b-128k"]


def _native_base(b: str) -> str:
    """把 base 规范成 ollama 原生 API 的根。

    ⚠️ 坑：hermes config.yaml 里存的是 OpenAI 兼容地址（结尾带 /v1），
    直接拼 /api/chat 会变成 /v1/api/chat → 404。原生的 /api/* 挂在根上。
    """
    b = (b or "").rstrip("/")
    if b.endswith("/v1"):
        b = b[:-3]
    return b


# 本机实测：ollama 曾从 11434 被挤到 12600（HNS 动态保留段占端口，见
# SOUL 环境事实）。所以**不能再写死默认地址** —— 写死的后果是界面显示
# 「本地模型 ❌ 未运行」，而 0 成本的招呼语/简历优化其实全废了：上层只会
# 静默回退到欠费的远程 API，用户看到的是"功能能用但额度在烧"。
_CANDIDATE_BASES = [
    "http://127.0.0.1:12600",
    "http://127.0.0.1:11434",
]

_probe_cache: dict = {"t": 0.0, "base": ""}
_PROBE_TTL = 60.0


def _alive(base: str, timeout: float = 1.2) -> bool:
    """探测 ollama 是否在这个地址上。探测时关代理（Clash 会拦 localhost → 502）。"""
    saved = {k: os.environ.pop(k) for k in
             ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")
             if k in os.environ}
    try:
        with urllib.request.urlopen(_native_base(base) + "/api/tags",
                                    timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False
    finally:
        os.environ.update(saved)


def _candidates() -> list[str]:
    out = []
    for k in ("OLLAMA_BASE_URL", "DEEPSEEK_BASE_URL"):
        v = os.environ.get(k)
        if v:
            out.append(_native_base(v))
    for p in (Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "config.yaml",
              Path.home() / ".hermes" / "config.yaml"):
        try:
            import re
            txt = p.read_text(encoding="utf-8", errors="replace")
            m = re.search(r"https?://[\d.]+:\d+(?:/v1)?", txt)
            if m:
                out.append(_native_base(m.group(0)))
        except OSError:
            pass
    out += _CANDIDATE_BASES
    seen, uniq = set(), []
    for b in out:
        if b and b not in seen:
            seen.add(b)
            uniq.append(b)
    return uniq


def base_url() -> str:
    """拿 ollama 地址（返回原生根，不带 /v1）。

    先探测哪个候选真的活着，再用它 —— 不是直接读配置/soul 里的值。
    配置会过期（端口被系统保留段挤走），而 ollama 不在时上层只会
    「回退远程 API」，功能看着能用、账单在走，最难查。
    探测结果缓存 60s，避免每次调用都探。
    """
    now = time.time()
    if _probe_cache["base"] and now - _probe_cache["t"] < _PROBE_TTL:
        return _probe_cache["base"]
    cands = _candidates()
    for b in cands:
        if _alive(b):
            _probe_cache.update({"t": now, "base": b})
            return b
    # 都不通：返回第一个候选，让调用方拿到确定地址去报错（不要返回空）
    best = cands[0] if cands else _CANDIDATE_BASES[0]
    _probe_cache.update({"t": now, "base": best})
    return best


def _req(url: str, payload: dict | None = None, timeout: int = 300) -> dict:
    """请求 ollama。关掉代理 —— Clash 会拦 localhost/内网（实测返回 502）。"""
    env_proxy = {k: os.environ.pop(k) for k in
                 ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")
                 if k in os.environ}
    try:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"} if data else {},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    finally:
        os.environ.update(env_proxy)


def is_alive(base: str | None = None, timeout: int = 6) -> bool:
    try:
        _req((base or base_url()) + "/api/tags", None, timeout)
        return True
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return False


def ensure_started() -> tuple[bool, str]:
    """确保 ollama 在跑。没跑就调 wsl-ollama.py 拉起来。

    返回 (是否可用, 说明)。
    """
    b = base_url()
    if is_alive(b):
        return True, f"已在运行 {b}"
    if not WSL_OLLAMA.exists():
        return False, f"ollama 没跑，且找不到拉起脚本 {WSL_OLLAMA}"
    try:
        p = subprocess.run([sys.executable, str(WSL_OLLAMA)],
                           capture_output=True, text=True, timeout=180,
                           encoding="utf-8", errors="replace")
        tail = ((p.stdout or "") + (p.stderr or "")).strip().splitlines()[-3:]
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"拉起脚本失败: {e}"
    # 脚本会改写 config.yaml（地址可能变了），重新读地址再探一次
    b2 = base_url()
    for _ in range(10):
        if is_alive(b2, timeout=4):
            return True, f"已拉起 {b2}"
        time.sleep(2)
    return False, "拉起后仍连不上 | " + " / ".join(tail)


def list_models(base: str | None = None) -> list[dict]:
    try:
        d = _req((base or base_url()) + "/api/tags", None, 15)
    except Exception:
        return []
    out = []
    for m in d.get("models", []):
        out.append({
            "name": m.get("name", ""),
            "size_gb": round((m.get("size") or 0) / 1e9, 1),
            "family": (m.get("details") or {}).get("family", ""),
        })
    out.sort(key=lambda x: -x["size_gb"])
    return out


def pick_model(models: list[dict] | None = None) -> str:
    """挑一个适合结构化小任务的模型。"""
    names = {m["name"] for m in (models if models is not None else list_models())}
    for cand in PREFERRED:
        if cand in names:
            return cand
    # 退而求其次：最小的那个（小任务够用，还快）
    ms = models if models is not None else list_models()
    if ms:
        return sorted(ms, key=lambda x: x["size_gb"])[0]["name"]
    return PREFERRED[0]


def chat(messages: list[dict], model: str = "", *, temperature: float = 0.7,
         num_predict: int = 1024, timeout: int = 300, think: bool = False) -> tuple[str, dict]:
    """单轮对话。返回 (正文, 元信息)。

    ⚠️ 默认 think=False —— 思考模型的思考会吃掉预算导致 content 为空，见文件头说明。
    """
    b = base_url()
    mdl = model or pick_model()
    payload = {
        "model": mdl,
        "messages": messages,
        "stream": False,
        "think": think,
        "options": {"temperature": temperature, "num_predict": num_predict},
    }
    t0 = time.time()
    d = _req(b + "/api/chat", payload, timeout)
    dt = time.time() - t0
    msg = d.get("message") or {}
    content = msg.get("content") or ""
    meta = {
        "model": mdl,
        "elapsed_sec": round(dt, 1),
        "content_len": len(content),
        "thinking_len": len(msg.get("thinking") or ""),
        "eval_count": d.get("eval_count"),
        "prompt_eval_count": d.get("prompt_eval_count"),
        "base_url": b,
    }
    return content, meta


def main() -> int:
    ap = argparse.ArgumentParser(description="本地 ollama 适配器")
    ap.add_argument("--status", action="store_true", help="看 ollama 状态和模型")
    ap.add_argument("--list", action="store_true", help="只列模型")
    ap.add_argument("--start", action="store_true", help="拉起 ollama")
    ap.add_argument("--chat", default="", help="单轮测试")
    ap.add_argument("-m", "--model", default="")
    args = ap.parse_args()

    b = base_url()
    print(f"ollama 地址: {b}")

    if args.start:
        ok, msg = ensure_started()
        print(("✅ " if ok else "❌ ") + msg)
        return 0 if ok else 1

    if args.status or args.list or not args.chat:
        alive = is_alive(b)
        print("状态:", "✅ 运行中" if alive else "❌ 未运行")
        if not alive:
            print("  提示: python llm_local.py --start")
            return 1
        ms = list_models(b)
        print(f"模型 {len(ms)} 个:")
        pick = pick_model(ms)
        for m in ms:
            mark = " ★推荐" if m["name"] == pick else ""
            print(f"  {m['name']:<32} {m['size_gb']:5.1f} GB{mark}")
        if not args.chat:
            return 0

    if args.chat:
        print("\n提问:", args.chat)
        content, meta = chat([{"role": "user", "content": args.chat}], args.model)
        print(f"模型: {meta['model']} | 耗时 {meta['elapsed_sec']}s | 正文 {meta['content_len']} 字"
              f" | 思考 {meta['thinking_len']} 字")
        print("─" * 56)
        print(content[:800] or "(空) —— 若为空，检查是否用了 think=False")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
