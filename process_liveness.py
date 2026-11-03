"""Read-only PID liveness; unknown ownership stays protected. Never signal on Windows."""
import os


def pid_alive(pid) -> bool:
    """pid 还活着吗。查不出来按「活的」处理（保守，宁可少跑不并发）。

    ⚠️ Windows 上**不能**用 `os.kill(pid, 0)` 探活：`signal.CTRL_C_EVENT == 0`，
    CPython 会把 sig=0 当成 `GenerateConsoleCtrlEvent(CTRL_C_EVENT, pid)`，
    也就是**向控制台进程组发 Ctrl-C**，不是查进程是否存在。实测后果：
    在 CI 的真实控制台上，一次「死 pid」探活会让**自己**收到 KeyboardInterrupt，
    unittest 捕不住（BaseException）→ 进程直接退出、不打印汇总（公开仓 CI 连续
    三轮在同一用例崩溃就是这么来的，2026-11-04 定位）。

    这里改用只读进程句柄：OpenProcess(QUERY_LIMITED_INFORMATION) + GetExitCodeProcess。
    不存在返回 False；权限不足或查询失败保守返回 True。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() != 87  # absent PID; other errors stay conservative
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        if getattr(exc, "winerror", None) == 87:    # ERROR_INVALID_PARAMETER：pid 不存在
            return False
        return True
