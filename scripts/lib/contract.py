"""脚本共用的返回值契约（G6/静默失败检测）。

约定：脚本把结果以 JSON 行写到 stdout，退出码只表达「这次检查的结论」：

    0 = OK     检查跑完，结论是正常
    2 = ALERT  检查跑完，结论是需要人看（缺依赖、版本不符、门禁不过）
    1 = ERROR  检查本身没法完成（文件缺失、参数错、脚本异常）

不要把「检查失败」也返回 1：1 表示我们不知道结论，2 表示我们知道有问题。
"""
from __future__ import annotations

import json
import sys

OK = 0
ERROR = 1
ALERT = 2

__all__ = ["OK", "ALERT", "ERROR", "emit", "verdict", "main_guard"]


def emit(step: str, **fields) -> dict:
    """输出一行 JSON 结果并立即 flush（脚本可能被父进程按行读取）。"""
    payload = {"step": step, **fields}
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    return payload


def verdict(*, error: bool = False, alerts: int = 0) -> int:
    """按契约把检查结论折算成退出码。"""
    if error:
        return ERROR
    return ALERT if alerts else OK


def main_guard(fn) -> int:  # pragma: no cover - 供脚本 __main__ 复用
    """跑检查函数；未预期异常一律落到 ERROR(1)，不静默当成 OK。"""
    try:
        return int(fn())
    except Exception as exc:  # noqa: BLE001 - 脚本边界，必须给出明确错误码
        emit("error", type=type(exc).__name__, detail=str(exc)[:300])
        return ERROR


if __name__ == "__main__":  # 自检：契约本身要能跑
    emit("contract", ok=True, codes={"OK": OK, "ALERT": ALERT, "ERROR": ERROR})
    sys.exit(OK)
