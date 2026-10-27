"""Fixed-location maintenance artifacts; SQLite remains owned by store.py."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

import store
from sandbox import KernelError, current_execution, require_maintenance

MAX_BYTES = 512 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024
TIME_LIMIT = 30
DATABASE_FILE = 'database.sqlite3'
MANIFEST_FILE = 'manifest.json'


def _safe(path, *, directory):
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise KernelError('BACKUP_NOT_FOUND', '指定备份文件或目录不存在。', 404) from None
    if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
            or os.path.normcase(str(path.resolve())) != os.path.normcase(str(path))
            or (not stat.S_ISDIR(info.st_mode) if directory else not stat.S_ISREG(info.st_mode))):
        raise KernelError('UNSAFE_BACKUP_PATH', '备份路径必须是固定目录内的普通文件，不能使用链接或重解析点。')
    return info


def _root(*, create=False):
    root = Path(store.DB_PATH).resolve().parent / '.jobkit-backups'
    if create and not root.exists() and not root.is_symlink():
        root.mkdir(mode=0o700)
    _safe(root, directory=True)
    return root


def _digest(path, deadline):
    digest, size = hashlib.sha256(), 0
    with path.open('rb') as stream:
        while True:
            if time.monotonic() >= deadline:
                raise KernelError('BACKUP_TIMEOUT', '备份摘要检查超时，未确认完成。')
            chunk = stream.read(128 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_BYTES:
                raise KernelError('BACKUP_TOO_LARGE', '备份超过当前容量，未接受截断文件。', 413)
            digest.update(chunk)
    return digest.hexdigest(), size


def _report(manifest, directory):
    return {**manifest, 'directory': str(directory), 'integrity_checked': True,
            'schema_checked': True, 'authenticity_verified': False}


def _cleanup(staging):
    _safe(staging, directory=True)
    for name in (DATABASE_FILE, DATABASE_FILE + '-journal', DATABASE_FILE + '-wal', DATABASE_FILE + '-shm', MANIFEST_FILE):
        path = staging / name
        if path.exists() or path.is_symlink():
            _safe(path, directory=False)
            path.unlink()
    staging.rmdir()  # Unknown files are never recursively deleted.


def create():
    require_maintenance()
    context = current_execution('runtime.database.backup')
    backup_id, deadline = context['task_id'], time.monotonic() + TIME_LIMIT
    root = _root(create=True)
    staging, final = root / ('.pending-' + backup_id), root / backup_id
    if any(path.exists() or path.is_symlink() for path in (staging, final)):
        raise KernelError('BACKUP_EXISTS', '该备份标识已有文件，未覆盖。')
    staging.mkdir(mode=0o700)
    published = False
    try:
        database = staging / DATABASE_FILE
        store.runtime_database_snapshot(database, max_bytes=MAX_BYTES, deadline=deadline)
        _safe(database, directory=False)
        store.runtime_database_inspect(database, deadline=deadline, trace_id=backup_id)
        digest, size = _digest(database, deadline)
        manifest = {
            'format': 'jobkit.database-backup', 'format_version': 1, 'backup_id': backup_id,
            'created_at': datetime.now(timezone.utc).isoformat(), 'database_file': DATABASE_FILE,
            'database_bytes': size, 'sha256': digest, 'contains_all_workspaces': True,
            'encrypted': False, 'automatic_restore': False, 'source_control_inflight': True,
        }
        with (staging / MANIFEST_FILE).open('x', encoding='utf-8', newline='\n') as stream:
            json.dump(manifest, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        _safe(staging, directory=True)
        if final.exists() or final.is_symlink():
            raise KernelError('BACKUP_EXISTS', '目标标识已被占用，未覆盖。')
        staging.rename(final)
        published = True
        return _report(manifest, final)
    except Exception:
        if not published:
            _cleanup(staging)
        raise


def _manifest(raw, backup_id):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate')
            result[key] = value
        return result
    def nonfinite(value):
        raise ValueError('nonfinite')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=nonfinite)
        fields = {'format', 'format_version', 'backup_id', 'created_at', 'database_file', 'database_bytes',
                  'sha256', 'contains_all_workspaces', 'encrypted', 'automatic_restore', 'source_control_inflight'}
        if (not isinstance(value, dict) or set(value) != fields or value['format'] != 'jobkit.database-backup'
                or type(value['format_version']) is not int or value['format_version'] != 1
                or value['backup_id'] != backup_id or value['database_file'] != DATABASE_FILE
                or type(value['database_bytes']) is not int or not 0 < value['database_bytes'] <= MAX_BYTES
                or not isinstance(value['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', value['sha256'])
                or value['contains_all_workspaces'] is not True or value['encrypted'] is not False
                or value['automatic_restore'] is not False or value['source_control_inflight'] is not True
                or not isinstance(value['created_at'], str) or len(value['created_at']) > 64
                or datetime.fromisoformat(value['created_at']).utcoffset() is None):
            raise ValueError('manifest')
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise KernelError('INVALID_BACKUP_MANIFEST', '备份清单格式或关联无效，未读取为有效备份。') from None
    return value


def verify(backup_id):
    require_maintenance()
    current_execution('runtime.database.backup.check')
    deadline = time.monotonic() + TIME_LIMIT
    directory = _root() / backup_id
    _safe(directory, directory=True)
    if {entry.name for entry in directory.iterdir()} != {DATABASE_FILE, MANIFEST_FILE}:
        raise KernelError('INVALID_BACKUP_LAYOUT', '备份目录应只包含数据库和清单，不能依赖额外侧文件。')
    manifest_path, database = directory / MANIFEST_FILE, directory / DATABASE_FILE
    manifest_before = _safe(manifest_path, directory=False)
    before = _safe(database, directory=False)
    with manifest_path.open('rb') as stream:
        raw = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise KernelError('INVALID_BACKUP_MANIFEST', '备份清单超过容量。')
    manifest = _manifest(raw, backup_id)
    if before.st_size != manifest['database_bytes']:
        raise KernelError('BACKUP_DIGEST_MISMATCH', '备份文件长度与清单不一致。')
    digest, size = _digest(database, deadline)
    if digest != manifest['sha256'] or size != manifest['database_bytes']:
        raise KernelError('BACKUP_DIGEST_MISMATCH', '备份摘要不一致，未确认完成。')
    store.runtime_database_inspect(database, deadline=deadline, trace_id=backup_id)
    after = _safe(database, directory=False)
    manifest_after = _safe(manifest_path, directory=False)
    for first, last in ((before, after), (manifest_before, manifest_after)):
        if (first.st_dev, first.st_ino, first.st_size, first.st_mtime_ns) != (last.st_dev, last.st_ino, last.st_size, last.st_mtime_ns):
            raise KernelError('BACKUP_CHANGED', '备份在校验期间变化，未确认完成。')
    return _report(manifest, directory)
