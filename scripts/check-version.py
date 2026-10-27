#!/usr/bin/env python3
"""运行版本 vs 磁盘版本（G6 第二半）：一条命令回答「跑着的进程到底是哪份代码」。

为什么需要它：`GET /version` 已经能报出运行进程的 build_fingerprint / files / git_commit，
但没人拿它和磁盘比 —— 于是「测试通过」和「运行版本正确」之间仍然隔着一个盲区：
改完代码没重启、重启了但启动脚本指向旧目录、或者跑的是另一份 checkout，
接口本身一切正常，只是那份代码不是你以为的那份。

本脚本只读：
- 用 HTTP 读运行中进程的 /version（默认 http://127.0.0.1:18770，可用 --url 覆盖）
- 在本仓库目录**独立重算**磁盘指纹（直接 import app 的 compute_build_fingerprint，
  不在这里写第二套算法 —— 两套算法对不上时，问题会被记到被测服务头上）
- 逐文件对比，指出哪个文件不一致

用法：
    python scripts/check-version.py
    python scripts/check-version.py --url http://127.0.0.1:18800
    python scripts/check-version.py --timeout 10

退出码（scripts/lib/contract.py）：
    0 = OK     运行版本与磁盘一致（指纹一致，且能读到 git commit 时也一致）
    2 = ALERT  运行版本落后于磁盘（指纹或 git commit 不同，逐文件列出差异）
    1 = ERROR  检查本身没法完成：服务不可达、响应不是 /version、脚本异常
               —— 「不知道」不是「有问题」，所以是 1 不是 2

绝不启停服务、不写库、不碰浏览器。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import contract  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_URL = "http://127.0.0.1:18770"
FULL_HASH = re.compile(r"^[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-fA-F]{40,64}$")


class VersionCheckError(RuntimeError):
    """检查没能得出结论（服务不可达 / 响应非法）。一律折算成 ERROR(1)。"""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


def version_url(base: str) -> str:
    return base.rstrip("/") + "/version"


def fetch_runtime(url: str, timeout: float = 5.0) -> dict:
    """GET /version；连不上、超时、非 200、不是 JSON 都抛 VersionCheckError。"""
    target = version_url(url)
    request = urllib.request.Request(target, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise VersionCheckError(
            f"服务在跑，但 {target} 返回 HTTP {exc.code}（可能是旧版本进程，或这个端口上不是 jobkit）",
            "当前端口上的进程不认识 /version：重启本工作区的服务后再检查。") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise VersionCheckError(
            f"连不上运行中的服务（{target}）：{getattr(exc, 'reason', exc)}",
            "先确认服务已启动：python main.py serve") from None
    if status != 200:
        raise VersionCheckError(f"服务返回 HTTP {status}（{target}）")
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise VersionCheckError(f"/version 响应不是 JSON（{target}）",
                                "确认 --url 指向 jobkit 服务端口（默认 18770）。") from None
    validate_payload(payload, target)
    return payload


def validate_payload(payload: object, target: str = "/version") -> None:
    """形状不对就当检查失败：拿半个响应去比对只会得出假结论。"""
    if not isinstance(payload, dict):
        raise VersionCheckError(f"{target} 响应不是对象")
    if payload.get("service") != "jobkit" or payload.get("ok") is not True:
        raise VersionCheckError(f"{target} 不是 jobkit 的 /version 响应（可能有别的服务占用该端口）")
    fingerprint = payload.get("build_fingerprint")
    if not isinstance(fingerprint, str) or not FULL_HASH.match(fingerprint):
        raise VersionCheckError(f"{target} 缺少合法的 build_fingerprint")
    files = payload.get("files")
    if not isinstance(files, dict) or not files or not all(
            isinstance(name, str) and isinstance(digest, str) for name, digest in files.items()):
        raise VersionCheckError(f"{target} 的 files 不是「文件名 → 摘要」映射")
    commit = payload.get("git_commit")
    if commit is not None and not (isinstance(commit, str) and COMMIT.match(commit)):
        raise VersionCheckError(f"{target} 的 git_commit 格式不合法")


def _load_app():
    """延迟 import：只有真要算磁盘指纹时才付 app 的导入成本（也方便测试替换）。"""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import app  # noqa: PLC0415 - 延迟导入是刻意设计
    return app


def local_identity(root: Path | str | None = None, *, commit=...) -> dict:
    """用 app 自己的算法在磁盘上重算一份版本身份。commit=... 表示去问 git。"""
    app = _load_app()
    base = Path(root) if root is not None else ROOT
    fingerprint, files = app.compute_build_fingerprint(base)
    git_commit = app._read_git_commit() if commit is ... else commit
    return {"build_fingerprint": fingerprint, "files": dict(files), "git_commit": git_commit}


def compare(runtime: dict, disk: dict) -> dict:
    """逐文件对比。fingerprint 一致 + commit 一致（可比时）才算一致。"""
    runtime_files = runtime.get("files") or {}
    disk_files = disk.get("files") or {}
    names = list(disk_files) + [name for name in runtime_files if name not in disk_files]
    file_diffs = [
        {"file": name, "runtime": runtime_files.get(name), "disk": disk_files.get(name)}
        for name in names
        if runtime_files.get(name) != disk_files.get(name)
    ]
    runtime_commit, disk_commit = runtime.get("git_commit"), disk.get("git_commit")
    # 读不到 git（没装 / 不是工作区）时不给结论：None 不是「不同」
    commit_matches = None if (runtime_commit is None or disk_commit is None) else runtime_commit == disk_commit
    fingerprint_matches = runtime.get("build_fingerprint") == disk.get("build_fingerprint")
    drift = bool(file_diffs) or not fingerprint_matches or commit_matches is False
    reasons = []
    if file_diffs:
        reasons.append("有 %d 个文件与磁盘不同：%s" % (
            len(file_diffs), "、".join(item["file"] for item in file_diffs)))
    elif not fingerprint_matches:
        reasons.append("build_fingerprint 不同，但逐文件摘要一致（文件集可能变了）")
    if commit_matches is False:
        reasons.append("git commit 不同（运行 %s / 磁盘 %s），运行进程可能是旧提交启动的" % (
            str(runtime_commit)[:12], str(disk_commit)[:12]))
    return {
        "ok": not drift,
        "fingerprint_matches": fingerprint_matches,
        "git_commit_matches": commit_matches,
        "file_diffs": file_diffs,
        "reasons": reasons,
    }


def check(url: str = DEFAULT_URL, *, timeout: float = 5.0, fetch=None, local=None) -> int:
    """跑一次对比并输出 JSON 行，返回契约退出码。fetch/local 为测试注入点。"""
    fetch = fetch or fetch_runtime
    local = local or local_identity
    try:
        runtime = fetch(url, timeout=timeout)
    except VersionCheckError as exc:
        contract.emit("version", ok=False, url=version_url(url), reason=str(exc),
                      hint=exc.hint or "确认 --url 指向 jobkit 服务端口（默认 18770）。")
        return contract.ERROR
    try:
        disk = local()
    except Exception as exc:  # noqa: BLE001 - 磁盘侧重算失败同样是「没得出结论」
        contract.emit("version", ok=False, url=version_url(url),
                      reason="磁盘指纹重算失败：%s: %s" % (type(exc).__name__, str(exc)[:200]))
        return contract.ERROR

    verdict = compare(runtime, disk)
    contract.emit("runtime", url=version_url(url), build_fingerprint=runtime.get("build_fingerprint"),
                  git_commit=runtime.get("git_commit"), started_at=runtime.get("started_at"),
                  app_version=runtime.get("app_version"), files=runtime.get("files"))
    contract.emit("disk", root=str(ROOT), build_fingerprint=disk.get("build_fingerprint"),
                  git_commit=disk.get("git_commit"), files=disk.get("files"))
    fields = {"url": version_url(url), **verdict}
    if verdict["ok"]:
        fields["reason"] = "运行版本与磁盘一致：指纹 %s…" % str(disk.get("build_fingerprint") or "")[:12]
    else:
        note = ""
        if verdict["git_commit_matches"] is None:
            note = "（本机读不到 git commit，仅按文件指纹判断）"
        fields["reason"] = "运行版本落后于磁盘，需要重启服务或确认启动目录；" + \
            "；".join(verdict["reasons"]) + note
        fields["hint"] = "运行进程加载的是旧代码，此时「磁盘测试通过」不代表线上行为正确。"
    contract.emit("version", **fields)
    code = contract.verdict(alerts=0 if verdict["ok"] else 1)
    contract.emit("summary", ok=code == contract.OK, exit=code, drift=not verdict["ok"],
                  files_differing=[item["file"] for item in verdict["file_diffs"]])
    return code


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="对比运行中进程的 /version 与磁盘重算的构建指纹。")
    parser.add_argument("--url", default=DEFAULT_URL, help="服务地址（默认 %s）" % DEFAULT_URL)
    parser.add_argument("--timeout", type=float, default=5.0, help="请求超时秒数（默认 5）")
    args = parser.parse_args(argv)
    return check(args.url, timeout=args.timeout)


if __name__ == "__main__":
    sys.exit(contract.main_guard(main))
