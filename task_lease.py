"""Per-task OS lease: released by the OS on process exit, never inferred by age."""
import hashlib
import os
from contextlib import contextmanager
from pathlib import Path

import store
from sandbox import KernelError


@contextmanager
def task_lease(task_id):
    if not isinstance(task_id, str) or not task_id or len(task_id) > 128:
        raise KernelError("INVALID_TASK_ID", "任务标识格式不正确。", 400)
    database = Path(store.DB_PATH).resolve()
    directory = database.parent / ".jobkit-task-locks"
    directory.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256((os.path.normcase(str(database)) + "\0" + task_id).encode()).hexdigest()
    # Never unlink lock files: deleting an inode while another process waits can
    # produce two independent locks for the same task.
    stream = open(directory / key, "a+b")
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt
            if stream.seek(0, 2) == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise KernelError("TASK_BUSY", "任务仍在运行，请稍后核对。") from exc
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise KernelError("TASK_BUSY", "任务仍在运行，请稍后核对。") from exc
        acquired = True
        yield
    finally:
        try:
            if acquired:
                if os.name == "nt":
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()
