"""多进程并发探针：额度占位在真实多进程竞争下会不会超限。

方案验收矩阵「幂等」要求「多进程只有一个外部动作候选」，G4 要求「150 上限在并发
和跨 scope 下不能突破」。单进程测试用同一连接串行调用，证不了这件事——本探针起
真实子进程抢同一个 SQLite 文件。

跑法：
    python scripts/concurrency_probe.py                 # 默认 3 进程 × 5 次，上限 8
    python scripts/concurrency_probe.py --workers 4 --per-worker 10 --limit 12

退出码（沿用 scripts/lib/contract.py）：
    0 = 成功数恰好等于上限，且没有 database is locked
    2 = 有并发问题（超限 / 少占 / 出现 locked）
    1 = 探针自己没跑成

判据只有两条：
    1. 成功的占位数量 == limit（不多不少：多 = 突破上限，少 = 丢额度）
    2. 落库的 attempts 行数 == 成功数（占位与结果不能双计）
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SETTINGS_KIND = 'applicant_greeting_settings'
ATTEMPTS_KIND = 'applicant_greeting_attempts'


def _worker(args) -> int:
    """子进程：抢 N 次额度占位，每次一个独立事务。"""
    os.environ['JOBKIT_DB'] = args.db
    import store  # noqa: E402 - 必须在设置 JOBKIT_DB 之后导入
    import runtime_tools  # noqa: E402
    from sandbox import KernelError  # noqa: E402

    ok = limited = locked = other = 0
    codes = {}
    for index in range(args.per_worker):
        try:
            with store.runtime_db() as c:
                c.execute('BEGIN IMMEDIATE')
                context = store.runtime_context(c)
                context['context_token'] = args.token
                runtime_tools.reserve_quota(c, context, f'job-{args.name}-{index}', '示例公司')
                c.commit()
            ok += 1
        except KernelError as exc:
            if exc.code == 'DAILY_LIMIT_REACHED':
                limited += 1
            else:
                other += 1
                codes[exc.code] = codes.get(exc.code, 0) + 1
        except sqlite3.OperationalError as exc:
            if 'locked' in str(exc).lower() or 'busy' in str(exc).lower():
                locked += 1
            else:
                other += 1
                codes[type(exc).__name__] = codes.get(type(exc).__name__, 0) + 1
        except Exception as exc:  # noqa: BLE001 - 探针要如实分类所有意外
            other += 1
            codes[f'{type(exc).__name__}:{str(exc)[:60]}'] = codes.get(
                f'{type(exc).__name__}:{str(exc)[:60]}', 0) + 1
    print(json.dumps({'worker': args.name, 'ok': ok, 'limited': limited,
                      'locked': locked, 'other': other, 'codes': codes}), flush=True)
    return 0


def _init(db: str, limit: int) -> str:
    """建库、写额度上限、返回 context_token。"""
    os.environ['JOBKIT_DB'] = db
    import store  # noqa: E402
    with store.runtime_db() as c:
        c.execute('BEGIN IMMEDIATE')
        context = store.runtime_context(c)
        c.execute("INSERT INTO runtime_documents VALUES (?,?,?,?) "
                  "ON CONFLICT(scope_id,kind) DO UPDATE SET revision=excluded.revision,"
                  "content=excluded.content",
                  (context['scope_id'], SETTINGS_KIND, 1,
                   json.dumps({'configured': limit, 'platform_limit': None,
                               'platform_expires_at': None}, ensure_ascii=False)))
        c.commit()
        return context['context_token']


def _counts(db: str) -> dict:
    os.environ['JOBKIT_DB'] = db
    import store  # noqa: E402
    with store.runtime_db() as c:
        c.execute('BEGIN')
        rows = c.execute("SELECT scope_id, content FROM runtime_documents WHERE kind=?",
                         (ATTEMPTS_KIND,)).fetchall()
    total = reserved = 0
    for _scope, content in rows:
        data = json.loads(content) if isinstance(content, str) else content
        items = (data or {}).get('items') or []
        total += len(items)
        reserved += sum(1 for item in items
                        if isinstance(item, dict) and item.get('state') == 'reserved')
    return {'attempts': total, 'reserved': reserved}


def main(argv=None) -> int:
    from scripts.lib.contract import ALERT, ERROR, OK, emit  # 延迟导入，子进程不加载

    parser = argparse.ArgumentParser(description='额度占位多进程并发探针')
    parser.add_argument('--workers', type=int, default=3)
    parser.add_argument('--per-worker', type=int, default=5)
    parser.add_argument('--limit', type=int, default=8)
    parser.add_argument('--db', default=None, help='临时库路径（默认建在系统临时目录）')
    parser.add_argument('--worker', default=None, help=argparse.SUPPRESS)
    parser.add_argument('--name', default='', help=argparse.SUPPRESS)
    parser.add_argument('--token', default='', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.worker is not None:            # 子进程分支
        return _worker(args)

    if not 2 <= args.workers <= 8 or not 1 <= args.per_worker <= 50 or args.limit < 1:
        emit('error', reason='参数越界：workers 2..8，per_worker 1..50，limit >= 1')
        return ERROR

    db = args.db or str(pathlib.Path(tempfile.mkdtemp(prefix='jobkit-concurrency-')) / 'probe.db')
    started = time.time()
    try:
        token = _init(db, args.limit)
    except Exception as exc:  # noqa: BLE001
        emit('error', stage='init', detail=f'{type(exc).__name__}: {exc}'[:300])
        return ERROR

    procs = []
    for index in range(args.workers):
        procs.append(subprocess.Popen(
            [sys.executable, str(pathlib.Path(__file__).resolve()),
             '--worker', str(index), '--name', f'w{index}', '--token', token,
             '--db', db, '--per-worker', str(args.per_worker), '--limit', str(args.limit)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
            errors='replace', cwd=str(ROOT)))

    results, errors = [], []
    for proc in procs:
        out, err = proc.communicate(timeout=300)
        for line in (out or '').splitlines():
            line = line.strip()
            if line.startswith('{'):
                try:
                    results.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        if proc.returncode != 0:
            errors.append((err or '')[-300:])

    totals = {'ok': 0, 'limited': 0, 'locked': 0, 'other': 0}
    for item in results:
        for key in totals:
            totals[key] += int(item.get(key) or 0)

    stored = _counts(db)
    elapsed = round(time.time() - started, 2)
    problems = []
    if totals['ok'] != args.limit:
        problems.append(f'成功占位 {totals["ok"]} != 上限 {args.limit}'
                        '（多 = 突破上限，少 = 丢额度）')
    if stored['attempts'] != totals['ok']:
        problems.append(f'落库 attempts {stored["attempts"]} != 成功数 {totals["ok"]}'
                        '（占位与结果可能双计）')
    if stored['reserved'] != stored['attempts']:
        problems.append(f'落库里有非 reserved 条目：{stored}')
    if totals['locked']:
        problems.append(f'出现 {totals["locked"]} 次 database is locked（busy_timeout 不够）')
    if totals['other'] or errors:
        problems.append(f'意外错误：codes={totals} stderr={errors[:1]}')

    emit('concurrency', workers=args.workers, per_worker=args.per_worker,
         limit=args.limit, requested=args.workers * args.per_worker,
         ok=totals['ok'], limited=totals['limited'], locked=totals['locked'],
         stored=stored, elapsed_seconds=elapsed, db=db,
         verdict='ok' if not problems else 'alert', problems=problems)
    if problems:
        emit('alert', problems=problems)
        return ALERT
    emit('note', message=f'并发占位恰好 {args.limit} 次，无超限、无锁冲突')
    return OK


if __name__ == '__main__':
    sys.exit(main())
