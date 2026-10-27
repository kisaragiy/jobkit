#!/usr/bin/env python3
"""依赖自检（G6）：requirements*.txt 的每一项能否真的导入，OCR 的 DLL/模型是否就绪。

为什么不能只 `pip install -r` 就算过：装上了不等于导得进来。Windows 上 onnxruntime /
cryptography 这类带原生扩展的包，缺 VC++ 运行库时 pip 显示成功、import 才报
「DLL load failed」。这里逐项在**子进程**里真导入一次，把结论落成可核对的 JSON 行。

用法：
    python scripts/check-deps.py                # 核心 + OCR 全都要可用，否则退出 2
    python scripts/check-deps.py --allow-missing-ocr   # OCR 视为可选（缺了只报告）
    python scripts/check-deps.py --requirements requirements-dev.txt

退出码（scripts/lib/contract.py）：
    0 = OK     所有检查项可用
    2 = ALERT  有依赖缺失 / 版本与 pin 不符 / OCR 不可用
    1 = ERROR  检查本身没跑起来（requirements 文件缺失、脚本异常）

只读：不改环境、不联网、不装包、不下载模型。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import contract  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PIN_PATTERN = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?P<op>==|>=|<=|~=|!=|>|<)?\s*(?P<want>[A-Za-z0-9.*+!_-]*)\s*$")
# 发行名 → 导入名（其余按 PEP 503 把 - 换 _）
MODULE_OVERRIDES = {
    "pillow": "PIL",
    "websocket-client": "websocket",
    "rapidocr-onnxruntime": "rapidocr_onnxruntime",
    "opencv-python": "cv2",
    "pyyaml": "yaml",
    "python-dotenv": "dotenv",
}
OCR_MODULES = ("rapidocr_onnxruntime", "onnxruntime", "PIL")
OCR_MODELS = (
    "ch_PP-OCRv4_det_infer.onnx",
    "ch_PP-OCRv4_rec_infer.onnx",
    "ch_ppocr_mobile_v2.0_cls_infer.onnx",
)

# 子进程探针：只做「能不能导入 + 装了哪个版本」，任何失败都折算成 JSON 而不是崩溃。
PROBE = r"""
import importlib, importlib.metadata as md, json, sys
name, module = sys.argv[1], sys.argv[2]
try:
    importlib.import_module(module)
except BaseException as exc:
    print(json.dumps({"ok": False, "error": type(exc).__name__, "detail": str(exc)[:300]}))
    sys.exit(0)
try:
    version = md.version(name)
except Exception:
    version = None
print(json.dumps({"ok": True, "version": version}))
"""

# OCR 探针：模型文件齐全且能实例化引擎才算就绪（与 applicant_salary_ocr.recognize 同口径）。
OCR_PROBE = r"""
import importlib.util as u, json, sys
from pathlib import Path
spec = u.find_spec("rapidocr_onnxruntime")
if spec is None:
    print(json.dumps({"ok": False, "reason": "package_missing"})); sys.exit(0)
root = Path(spec.origin).parent
missing = [n for n in sys.argv[1:] if not (root / "models" / n).is_file()]
if missing:
    print(json.dumps({"ok": False, "reason": "models_missing", "missing": missing})); sys.exit(0)
try:
    from rapidocr_onnxruntime import RapidOCR
    RapidOCR(intra_op_num_threads=1, inter_op_num_threads=1)
except BaseException as exc:
    print(json.dumps({"ok": False, "reason": "engine_unavailable",
                      "error": type(exc).__name__, "detail": str(exc)[:300]})); sys.exit(0)
print(json.dumps({"ok": True, "reason": "ready"}))
"""


def module_for(distribution: str) -> str:
    key = distribution.lower()
    if key in MODULE_OVERRIDES:
        return MODULE_OVERRIDES[key]
    return key.replace("-", "_")


def parse_requirements(path: Path) -> list[dict]:
    """只取直接依赖行；-r/-c/注释/空行跳过（-r 由调用方单独展开）。"""
    pins, includes = [], []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-r") or line.startswith("--requirement"):
            includes.append(line.split(None, 1)[1].strip() if " " in line else "")
            continue
        if line.startswith("-"):
            continue
        match = PIN_PATTERN.match(line)
        if match:
            pins.append({"name": match.group("name"), "op": match.group("op") or "",
                         "want": match.group("want") or ""})
        else:
            pins.append({"name": line, "op": "", "want": "", "unparsed": True})
    return [{"pins": pins, "includes": includes}]


def _version_tuple(text: str | None) -> tuple | None:
    if not text:
        return None
    parts = re.findall(r"\d+", text)
    return tuple(int(p) for p in parts[:4]) if parts else None


def satisfies(version: str | None, op: str, want: str) -> bool | None:
    """够用时返回 True/False；比较不了（版本里有非数字、通配）返回 None（不猜）。"""
    if not op or not want:
        return True
    if "*" in want or not version:
        return None
    have, need = _version_tuple(version), _version_tuple(want)
    if have is None or need is None:
        return None
    width = max(len(have), len(need))
    have, need = have + (0,) * (width - len(have)), need + (0,) * (width - len(need))
    return {"==": have == need, "!=": have != need, ">=": have >= need,
            "<=": have <= need, ">": have > need, "<": have < need,
            "~=": have[:len(need) - 1] == need[:len(need) - 1]}.get(op, None)


def probe(name: str, module: str) -> dict:
    """在子进程里真导入一次；超时/崩溃都是明确结论，不是静默通过。"""
    try:
        done = subprocess.run([sys.executable, "-c", PROBE, name, module], cwd=str(ROOT),
                              capture_output=True, text=True, errors="replace", timeout=120, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "status": "probe_failed", "detail": f"{type(exc).__name__}: {exc}"[:300]}
    out = (done.stdout or "").strip().splitlines()
    try:
        result = json.loads(out[-1]) if out else {}
    except json.JSONDecodeError:
        result = {}
    if not result:
        tail = ((done.stderr or "").strip().splitlines() or [""])[-1]
        return {"ok": False, "status": "probe_failed", "detail": (tail or "无输出")[:300]}
    if result.get("ok"):
        return {"ok": True, "status": "ok", "version": result.get("version")}
    detail = str(result.get("detail") or "")
    lowered = detail.lower()
    if "dll load failed" in lowered or "winerror 126" in lowered or "error 0x7e" in lowered:
        status = "dll_unavailable"      # 典型：缺 VC++ 运行库，与「没装包」不是一回事
    elif "no module named" in lowered:
        status = "missing"
    else:
        status = "import_failed"
    return {"ok": False, "status": status, "error": result.get("error"), "detail": detail[:300]}


def ocr_status() -> dict:
    try:
        done = subprocess.run([sys.executable, "-c", OCR_PROBE, *OCR_MODELS], cwd=str(ROOT),
                              capture_output=True, text=True, errors="replace", timeout=180, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ready": False, "reason": "probe_failed", "detail": f"{type(exc).__name__}: {exc}"[:300]}
    lines = (done.stdout or "").strip().splitlines()
    try:
        result = json.loads(lines[-1]) if lines else {}
    except json.JSONDecodeError:
        result = {}
    if not result:
        tail = ((done.stderr or "").strip().splitlines() or [""])[-1]
        return {"ready": False, "reason": "probe_failed", "detail": (tail or "无输出")[:300]}
    return {"ready": bool(result.get("ok")), **{k: v for k, v in result.items() if k != "ok"}}


def check_file(path: Path, cache: dict) -> dict:
    try:
        groups = parse_requirements(path)
    except OSError as exc:
        contract.emit("requirements", file=path.name, ok=False, status="file_unreadable",
                      detail=f"{type(exc).__name__}: {exc}"[:300])
        raise
    packages = []
    for group in groups:
        for pin in group["pins"]:
            name = pin["name"]
            module = module_for(name)
            if name.lower() not in cache:
                cache[name.lower()] = probe(name, module)
            result = dict(cache[name.lower()])
            if result.get("ok"):
                match = satisfies(result.get("version"), pin["op"], pin["want"])
                result["pin"] = f"{pin['op']}{pin['want']}" if pin["op"] else ""
                result["pin_matches"] = match
                if match is False:
                    result["status"] = "version_mismatch"
                    result["ok"] = False
                elif match is None:
                    result["status"] = "version_unverified"
            packages.append({"name": name, "module": module, **result})
    contract.emit("requirements", file=path.name, ok=all(p["ok"] for p in packages), packages=packages)
    return {"file": path.name, "packages": packages}


def main() -> int:
    parser = argparse.ArgumentParser(description="逐项检查 requirements 里的依赖能否真的导入。")
    parser.add_argument("--requirements", action="append", default=None,
                        help="要检查的 requirements 文件（可多次；默认 requirements.txt + requirements-ocr.txt）")
    parser.add_argument("--allow-missing-ocr", action="store_true",
                        help="OCR 视为可选：不可用只报告，不影响退出码（0/2 由核心依赖决定）")
    args = parser.parse_args()

    cache: dict = {}
    checked, missing, unverified, error = [], [], [], False
    for name in (args.requirements or ["requirements.txt", "requirements-ocr.txt"]):
        path = ROOT / name
        if not path.is_file():
            contract.emit("requirements", file=name, ok=False, status="file_missing")
            error = True
            continue
        try:
            checked.append(check_file(path, cache))
        except OSError:
            error = True
    for entry in checked:
        for package in entry["packages"]:
            if not package["ok"]:
                missing.append({"file": entry["file"], "name": package["name"],
                                "status": package.get("status"),
                                "detail": package.get("detail") or package.get("version")})
            elif package.get("status") == "version_unverified":
                unverified.append({"file": entry["file"], "name": package["name"],
                                   "version": package.get("version")})

    ocr = ocr_status()
    contract.emit("ocr", **ocr)

    alerts = len(missing) + (0 if (ocr["ready"] or args.allow_missing_ocr) else 1)
    code = contract.verdict(error=error, alerts=alerts)
    contract.emit("summary", ok=code == contract.OK, exit=code, missing=missing,
                  version_unverified=unverified,
                  ocr_ready=ocr["ready"], ocr_reason=ocr.get("reason"),
                  ocr_optional=bool(args.allow_missing_ocr),
                  python=sys.version.split()[0])
    if code == contract.OK:
        contract.emit("note", message="全部依赖可导入；OCR 就绪" if ocr["ready"] else "核心依赖可导入，OCR 未就绪")
    return code


if __name__ == "__main__":
    sys.exit(contract.main_guard(main))
