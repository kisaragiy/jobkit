#!/usr/bin/env python3
"""安装体检（G6 第一半）：一条命令回答「这台机器现在能不能好好用 jobkit」。

单项检查各自都有脚本了（check-deps / check-version），但普通用户不会按顺序跑三条命令
再自己拼结论。本脚本把它们串成**一份报告**：

    1. Python 版本与必需包（复用 check-deps 的解析与真导入探针，不复制一套）
    2. 端口 18770 是否被占用、占用者是不是 jobkit（读 /health 与 /version）
    3. 数据库文件是否存在、可写、大小、有没有未完成的外部动作（只读查询）
    4. 浏览器调试端口 9222 是否可达（可选；不可达只提示，不判错）
    5. 运行版本 vs 磁盘版本（复用 check-version 的指纹比较）

用法：
    python scripts/check-install.py
    python scripts/check-install.py --port 18800
    python scripts/check-install.py --db D:/jobkit-data/jobkit.db
    python scripts/check-install.py --require-ocr      # OCR 缺失也算问题

退出码（scripts/lib/contract.py）：
    0 = OK     体检跑完，一切就绪（或只有「可选/未启动」类提示）
    2 = ALERT  体检跑完，有需要人处理的事（缺包、端口被别的程序占、库不可写、
               还有未核对的外部动作、运行版本落后）
    1 = ERROR  体检本身没法给出完整结论（requirements 文件缺失、脚本异常）

「服务没启动」不算 ALERT：刚装好还没启动是正常状态，报告里会说「先把服务启动起来」，
版本比对这一步标记为 skipped —— 拿不到比对对象不等于版本有问题。

只读：不启停服务、不写库、不碰浏览器（不打开、不点击、不导航）。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import socket
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import contract  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18770
BROWSER_PORT = 9222
MIN_PYTHON = (3, 11)
DEFAULT_REQUIREMENTS = ("requirements.txt", "requirements-ocr.txt")
OCR_REQUIREMENTS = "requirements-ocr.txt"

#: 「未完成的外部动作」与 runtime_tools.context_switch 里的繁忙判定同口径：
#: 真发过 / 可能发过、但人还没核对的那条，比什么都值得先看到。
OPEN_EXTERNAL_ACTION = (
    "SELECT COUNT(*) FROM runtime_tasks "
    "WHERE effect='external_write' AND status IN ('executing','outcome_unknown')"
)
SEND_ATTEMPTS_UNKNOWN = (
    "SELECT COUNT(*) FROM runtime_send_attempts WHERE state='outcome_unknown'"
)


def load_sibling(name: str):
    """按文件名加载 scripts/ 下的同级脚本（check-deps.py 名字带连字符，不能直接 import）。"""
    path = Path(__file__).resolve().parent / name
    spec = importlib.util.spec_from_file_location(name.replace("-", "_").replace(".py", ""), path)
    if spec is None or spec.loader is None:  # pragma: no cover - 文件被删才会到这里
        raise FileNotFoundError(str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def http_get(url: str, timeout: float = 5.0) -> tuple[int, bytes]:
    """GET 一个本机地址，返回 (状态码, 响应体)。连不上就抛原异常，由调用方决定含义。"""
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return getattr(response, "status", 200), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() if hasattr(exc, "read") else b""


def port_open(host: str, port: int, timeout: float = 0.6) -> bool:
    """端口上有没有进程在监听。连不上就是没有，不猜。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _as_json(body: bytes) -> dict | None:
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def inspect_port(host: str, port: int, *, http=None, probe=None, timeout: float = 5.0) -> dict:
    """端口占用 + 占用者身份 + 运行中的 /version。

    返回 in_use / service / jobkit / version / reason；不抛异常，全部折算成字段。
    """
    http = http or http_get
    probe = probe or port_open
    result = {"host": host, "port": port, "in_use": False, "jobkit": False,
              "service": None, "version": None, "reason": None}
    if not probe(host, port):
        result["reason"] = "%s:%d 上没有任何程序在监听（服务没启动）" % (host, port)
        return result
    result["in_use"] = True
    base = "http://%s:%d" % (host, port)
    try:
        status, body = http(base + "/health", timeout=timeout)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        result["reason"] = "端口被占用，但读 /health 失败：%s" % getattr(exc, "reason", exc)
        return result
    health = _as_json(body) if status == 200 else None
    if not health or health.get("service") != "jobkit":
        result["service"] = (health or {}).get("service") if health else None
        result["reason"] = "端口被别的程序占用（HTTP %s，/health 不是 jobkit）" % status
        return result
    result["jobkit"] = True
    result["service"] = "jobkit"
    try:
        status, body = http(base + "/version", timeout=timeout)
        payload = _as_json(body) if status == 200 else None
        if payload is None:
            if status in (401, 403):
                # 本分支的 /version 是公开的；拿不到会话说明这是更早的构建在跑
                raise ValueError("HTTP %d：这个进程的 /version 还没公开，多半是旧版本在跑，"
                                 "重启本目录的服务后再体检" % status)
            raise ValueError("HTTP %s 且响应不是 JSON 对象" % status)
        load_sibling("check-version.py").validate_payload(payload, base + "/version")
    except Exception as exc:  # noqa: BLE001 - 形状不对只是「读不到版本」，不该炸掉整份报告
        result["reason"] = str(exc)[:200]
        return result
    result["version"] = payload
    result["reason"] = "jobkit 正在运行"
    return result


def inspect_database(path: Path) -> dict:
    """只读看一眼数据库：存在吗、多大、可写吗、有没有没闭环的外部动作。

    绝不写：用 `mode=ro` 打开 SQLite，也不调 store（store.connect 会建表/迁移 = 写库）。
    """
    path = Path(path)
    info = {
        "step": "database",
        "path": str(path),
        "exists": path.is_file(),
        "size_bytes": None,
        "writable": None,
        "writable_target": None,
        "pending_external": None,
        "pending_send_unknown": None,
        "schema": None,
        "read_error": None,
    }
    if info["exists"]:
        try:
            info["size_bytes"] = path.stat().st_size
        except OSError as exc:
            info["read_error"] = "读文件大小失败：%s" % str(exc)[:200]
        info["writable_target"] = "file"
        info["writable"] = os.access(str(path), os.W_OK)
    else:
        # 库还没建出来：能不能建取决于所在目录
        info["writable_target"] = "directory"
        info["writable"] = os.access(str(path.parent), os.W_OK)
        return info

    try:
        connection = sqlite3.connect("file:" + path.resolve().as_posix() + "?mode=ro", uri=True)
    except sqlite3.Error as exc:
        info["read_error"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
        return info
    try:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        info["schema"] = "runtime_tasks" in tables
        if not info["schema"]:
            info["read_error"] = "库里没有 runtime_tasks 表：这不是 jobkit 的数据库？"
            return info
        info["pending_external"] = int(connection.execute(OPEN_EXTERNAL_ACTION).fetchone()[0])
        if "runtime_send_attempts" in tables:
            info["pending_send_unknown"] = int(
                connection.execute(SEND_ATTEMPTS_UNKNOWN).fetchone()[0])
    except sqlite3.Error as exc:
        # 被正在运行的服务短时锁住只是「这次没看到」，不是「库坏了」
        info["read_error"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
    finally:
        connection.close()
    return info


def inspect_browser(*, http=None, timeout: float = 1.5) -> dict:
    """9222 上有没有带调试端口的浏览器。可选检查：不可达只提示，不影响结论。"""
    http = http or http_get
    result = {"step": "browser", "port": BROWSER_PORT, "reachable": False, "browser": None,
              "optional": True,
              "hint": "未开浏览器调试端口：只能用命令行与只读部分（搜索/草稿/查看），"
                      "需要读写页面时先按说明书启动浏览器。"}
    try:
        status, body = http("http://127.0.0.1:%d/json/version" % BROWSER_PORT, timeout=timeout)
    except (urllib.error.URLError, OSError, ValueError):
        return result
    payload = _as_json(body) if status == 200 else None
    if not payload or "webSocketDebuggerUrl" not in payload and "Browser" not in payload:
        return result
    result["reachable"] = True
    result["browser"] = str(payload.get("Browser") or "unknown")[:80]
    result["hint"] = None
    return result


def check_python(python_info=None) -> dict:
    info = python_info or sys.version_info[:3]
    ok = tuple(info[:2]) >= MIN_PYTHON
    return {"step": "python", "ok": ok, "version": ".".join(str(part) for part in info[:3]),
            "required": ">=%d.%d" % MIN_PYTHON,
            "executable": sys.executable,
            "reason": None if ok else "Python 太旧：需要 %d.%d 或更新（当前 %s）" % (
                MIN_PYTHON[0], MIN_PYTHON[1], ".".join(str(p) for p in info[:3]))}


def check_dependencies(root: Path, requirements, *, allow_missing_ocr: bool = True,
                       checker=None) -> dict:
    """复用 check-deps 的解析（parse_requirements）、探针（probe/satisfies）与 OCR 判定。"""
    cd = checker or load_sibling("check-deps.py")
    cache: dict = {}
    files, missing, optional_missing = [], [], []
    error = False
    for name in requirements:
        path = Path(root) / name
        if not path.is_file():
            files.append({"file": name, "ok": False, "status": "file_missing"})
            error = True
            continue
        optional = name == OCR_REQUIREMENTS
        try:
            packages_pins = [pin for group in cd.parse_requirements(path) for pin in group["pins"]]
        except OSError as exc:
            files.append({"file": name, "ok": False, "status": "file_unreadable",
                          "detail": str(exc)[:200]})
            error = True
            continue
        packages = []
        for pin in packages_pins:
            module = cd.module_for(pin["name"])
            key = pin["name"].lower()
            if key not in cache:
                cache[key] = cd.probe(pin["name"], module)
            entry = {"name": pin["name"], "module": module, "optional": optional,
                     **cache[key]}
            if entry.get("ok") and pin["op"]:
                match = cd.satisfies(entry.get("version"), pin["op"], pin["want"])
                entry["pin"] = pin["op"] + pin["want"]
                if match is False:
                    entry.update(ok=False, status="version_mismatch")
            packages.append(entry)
            if not entry.get("ok"):
                (optional_missing if optional else missing).append(
                    {"file": name, "name": pin["name"], "status": entry.get("status"),
                     "detail": entry.get("detail") or entry.get("version")})
        files.append({"file": name, "ok": all(p["ok"] for p in packages),
                      "optional": optional, "packages": packages})
    ocr = cd.ocr_status()
    ok = not missing and not error and (ocr["ready"] or allow_missing_ocr)
    return {"step": "deps", "ok": ok, "error": error, "requirements": files,
            "missing": missing, "optional_missing": optional_missing,
            "ocr": ocr, "ocr_optional": allow_missing_ocr}


def compare_version(runtime: dict | None, *, local=None, service_running: bool = False,
                    service_reason: str | None = None) -> dict:
    """复用 check-version 的 compare/local_identity：不在本脚本里写第二套指纹算法。"""
    cv = load_sibling("check-version.py")
    local = local or cv.local_identity
    if runtime is None:
        if service_running:
            # 服务在跑却读不到版本 —— 这是要人处理的事，不是「跳过」
            return {"step": "version", "status": "unavailable", "ok": False,
                    "reason": service_reason or "服务在跑但读不到版本信息",
                    "file_diffs": [], "reasons": []}
        return {"step": "version", "status": "skipped", "ok": True,
                "reason": "服务没启动，无法比对运行版本（这不代表版本有问题）",
                "file_diffs": [], "reasons": []}
    try:
        disk = local()
    except Exception as exc:  # noqa: BLE001 - 磁盘侧算不出来 = 没结论
        return {"step": "version", "status": "unavailable", "ok": False,
                "reason": "磁盘指纹重算失败：%s: %s" % (type(exc).__name__, str(exc)[:200]),
                "file_diffs": [], "reasons": []}
    verdict = cv.compare(runtime, disk)
    return {"step": "version", "status": "match" if verdict["ok"] else "drift",
            "ok": verdict["ok"],
            "build_fingerprint": runtime.get("build_fingerprint"),
            "disk_fingerprint": disk.get("build_fingerprint"),
            "git_commit_matches": verdict["git_commit_matches"],
            "file_diffs": verdict["file_diffs"],
            "reasons": verdict["reasons"],
            "reason": "运行版本与磁盘一致" if verdict["ok"]
                      else "运行版本落后于磁盘：" + "；".join(verdict["reasons"])}


def _conclude(python, deps, port, db, browser, version) -> tuple[int, list, list]:
    """把各部分字段折成 (退出码, alerts, errors)。"""
    alerts, errors = [], []
    if not python["ok"]:
        alerts.append("python_too_old")
    if deps["error"]:
        errors.append("requirements_unreadable")
    if deps["missing"]:
        alerts.append("deps_missing")
    if not deps["ocr"]["ready"] and not deps["ocr_optional"]:
        alerts.append("ocr_missing")
    if deps["optional_missing"] and not deps["ocr_optional"]:
        alerts.append("ocr_deps_missing")
    if port["in_use"] and not port["jobkit"]:
        alerts.append("port_taken_by_other")
    if port["jobkit"] and port["version"] is None:
        alerts.append("version_unavailable")
    if not db["writable"]:
        alerts.append("database_not_writable")
    if db["exists"] and db["schema"] is False:
        alerts.append("database_not_jobkit")
    elif db["read_error"] and db["schema"] is not True:
        # schema=True 时的 read_error 多为「被服务短时锁住」，那是没看到，不是坏了
        alerts.append("database_unreadable")
    if db["pending_external"]:
        alerts.append("pending_external_action")
    if version["status"] == "drift":
        alerts.append("version_drift")
    if version["status"] == "unavailable":
        alerts.append("version_unavailable")
    alerts = list(dict.fromkeys(alerts))     # 同一件事只报一次
    errors = list(dict.fromkeys(errors))
    code = contract.verdict(error=bool(errors), alerts=len(alerts))
    return code, alerts, errors


def build_text(*, code, python, deps, port, db, browser, version) -> str:
    """一段普通人能读懂的中文总结（配合 --text 或直接终端里看）。"""
    if code == contract.OK and not port["in_use"]:
        verdict = "环境就绪，服务还没启动（下面有启动命令）。"
    else:
        verdict = {0: "一切就绪。", 2: "有需要你处理的事，见下面带「要处理」的行。",
                   1: "体检没能跑完，见下面带「失败」的行。"}.get(code, "结论未知。")
    lines = ["jobkit 安装体检：%s" % verdict]

    lines.append("[%s] 运行环境：Python %s（需要 %s）" % (
        "OK" if python["ok"] else "要处理", python["version"], python["required"]))
    if not python["ok"]:
        lines.append("        " + (python.get("reason") or "Python 版本太旧，请装 %s 或更新"
                                  % python["required"]))

    ocr_text = "OCR 就绪（能读图片里的工资/学历等）" if deps["ocr"]["ready"] else (
        "OCR 未就绪，不影响主流程%s" % ("" if deps["ocr_optional"] else "（本次要求必须有 OCR）"))
    if deps["missing"]:
        names = "、".join(item["name"] for item in deps["missing"])
        lines.append("[要处理] 依赖：缺 %s。按说明书 pip 装一遍 requirements" % names)
    elif deps["error"]:
        lines.append("[失败] 依赖：requirements 文件缺失或读不了，无法核对")
    else:
        lines.append("[OK] 依赖：必需包都能导入；%s" % ocr_text)
    if deps["optional_missing"]:
        lines.append("[提示] 可选依赖未装：%s" % "、".join(
            item["name"] for item in deps["optional_missing"]))

    if port["jobkit"] and port["version"] is None:
        lines.append("[要处理] 服务：%s:%d 上有 jobkit 在跑，但读不到它的版本信息（%s）"
                     % (port["host"], port["port"], str(port["reason"])[:120]))
    elif port["jobkit"]:
        lines.append("[OK] 服务：%s:%d 上有 jobkit 在运行" % (port["host"], port["port"]))
    elif port["in_use"]:
        lines.append("[要处理] 端口：%s:%d 被别的程序占用（不是 jobkit）。"
                     "关掉它，或用 python main.py serve --port 其它端口" % (port["host"], port["port"]))
    else:
        lines.append("[提示] 服务：%s:%d 上没有 jobkit 在跑。要开始用：python main.py serve"
                     % (port["host"], port["port"]))

    if not db["exists"]:
        lines.append("[提示] 数据：%s 还没创建（第一次启动服务时自动建）" % db["path"])
    else:
        size = db["size_bytes"] or 0
        lines.append("[%s] 数据：%s（%.1f KB，%s）" % (
            "OK" if db["writable"] and not db["read_error"] else "要处理", db["path"],
            size / 1024.0, "可写" if db["writable"] else "不可写，检查文件权限/占用"))
        if db["pending_external"]:
            lines.append("[要处理] 有 %d 个外部动作没核对完（可能是上次真发出去没登记结果）："
                         "先跑 python main.py outcome list" % db["pending_external"])
        elif db["read_error"]:
            lines.append("[提示] 这次没能读库核对（%s），不影响其它结论" % db["read_error"])

    if not browser["reachable"]:
        lines.append("[提示] 浏览器调试口 %d 未开：只能用 CLI 与只读功能" % BROWSER_PORT)
    else:
        lines.append("[OK] 浏览器调试口 %d 可达（%s）" % (BROWSER_PORT, browser["browser"]))

    if version["status"] == "match":
        lines.append("[OK] 运行版本：与磁盘代码一致")
    elif version["status"] == "drift":
        lines.append("[要处理] 运行版本：落后于磁盘代码，重启服务后复查（%s）"
                     % "；".join(version["reasons"])[:160])
    elif version["status"] == "skipped":
        lines.append("[提示] 运行版本：跳过（服务没启动，无从比对）")
    else:
        lines.append("[要处理] 运行版本：读不到可比的版本信息（%s）" % version["reason"][:160])

    if code == contract.OK:
        lines.append("下一步：浏览器已就绪就直接用；没有就先 python main.py serve，再打开界面。")
    return "\n".join(lines)


def check(*, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, root=None,
          db_path=None, requirements=None, allow_missing_ocr: bool = True,
          timeout: float = 5.0, python_info=None, http=None, probe=None,
          local=None, checker=None, db_reader=None, browser_probe=None,
          echo_text: bool | None = None) -> int:
    """跑完整一次体检并输出 JSON 行 + 中文总结，返回契约退出码（全部依赖可注入，便于测试）。"""
    root = Path(root) if root is not None else ROOT
    database = Path(db_path) if db_path is not None else Path(
        os.environ.get("JOBKIT_DB") or (root / "jobkit.db"))
    python = check_python(python_info)
    contract.emit(**python)
    deps = check_dependencies(root, requirements or DEFAULT_REQUIREMENTS,
                             allow_missing_ocr=allow_missing_ocr, checker=checker)
    contract.emit(**deps)
    port_info = inspect_port(host, port, http=http, probe=probe, timeout=timeout)
    contract.emit("port", in_use=port_info["in_use"], jobkit=port_info["jobkit"],
                  host=host, port=port, service=port_info["service"],
                  reason=port_info["reason"],
                  app_version=(port_info["version"] or {}).get("app_version"),
                  started_at=(port_info["version"] or {}).get("started_at"))
    db = (db_reader or inspect_database)(database)
    contract.emit(**db)
    browser = (browser_probe or inspect_browser)(http=http)
    contract.emit(**browser)
    version = compare_version(port_info["version"], local=local,
                              service_running=port_info["jobkit"],
                              service_reason=port_info["reason"])
    contract.emit(**version)

    code, alerts, errors = _conclude(python, deps, port_info, db, browser, version)
    text = build_text(code=code, python=python, deps=deps, port=port_info,
                      db=db, browser=browser, version=version)
    contract.emit("summary", ok=code == contract.OK, exit=code, alerts=alerts,
                  errors=errors, python=python["version"], service_running=port_info["jobkit"],
                  database=db["path"], pending_external=db["pending_external"],
                  version_status=version["status"], text=text)
    if echo_text is None:
        echo_text = sys.stdout.isatty()
    if echo_text:
        print("", flush=True)
        print(text, flush=True)
    return code


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="一键安装体检：环境、依赖、端口、数据库、版本。")
    parser.add_argument("--host", default=DEFAULT_HOST, help="服务地址（默认 %s）" % DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="服务端口（默认 %d）" % DEFAULT_PORT)
    parser.add_argument("--timeout", type=float, default=5.0, help="HTTP 超时秒数（默认 5）")
    parser.add_argument("--root", default=None, help="项目目录（默认本脚本的上一级）")
    parser.add_argument("--db", default=None, help="数据库路径（默认 $JOBKIT_DB 或 <项目>/jobkit.db）")
    parser.add_argument("--requirements", action="append", default=None,
                        help="要检查的 requirements 文件（可多次；默认两个）")
    parser.add_argument("--require-ocr", action="store_true",
                        help="OCR 缺失也算问题（默认 OCR 视为可选）")
    parser.add_argument("--text", dest="text", action="store_true", default=None,
                        help="在 JSON 之后打印中文总结（终端里默认开启）")
    parser.add_argument("--json-only", dest="text", action="store_false",
                        help="只输出 JSON 行，不打印中文总结")
    args = parser.parse_args(argv)
    return check(host=args.host, port=args.port, root=args.root, db_path=args.db,
                 requirements=args.requirements,
                 allow_missing_ocr=not args.require_ocr, timeout=args.timeout,
                 echo_text=args.text)


if __name__ == "__main__":
    sys.exit(contract.main_guard(main))
