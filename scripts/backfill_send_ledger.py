"""把修复前发生的真实发送事故补进台账（默认只打印，不写库）。

为什么需要：本闸门只拦「修复之后」的发送。2026-10-27 20:29 那两次
`chat.submit`（一次 outcome_unknown、一次 completed）当时没有台账记录，
系统不会知道那个对象的结果还没核对 —— 补账之后闸门才管得到它。

用法：
    python scripts/backfill_send_ledger.py                 # dry-run，只打印
    python scripts/backfill_send_ledger.py --apply         # 真正写入

安全边界：只读 jobkit.db 判断，写入仅在 --apply 时对 runtime_documents 做一次
upsert；不改任务表、不删除任何既有记录。
"""
import argparse
import json
import sqlite3
import sys

sys.path.insert(0, '.')

import applicant_send_ledger as ledger  # noqa: E402

DB = 'jobkit.db'
AUTH_KIND = 'conversation_authorization'
BACKFILL_NOTE = 'backfilled:2026-10-27 真实事故补账'


def load(db_path):
    c = sqlite3.connect('file:%s?mode=ro' % db_path, uri=True)
    c.row_factory = sqlite3.Row
    try:
        rows = c.execute(
            "SELECT task_id, scope_id, tool, status, error_code, created_at FROM runtime_tasks "
            "WHERE effect='external_write' AND tool='runtime.applicant.chat.submit' "
            "ORDER BY created_at").fetchall()
        auth = c.execute(
            "SELECT scope_id, content FROM runtime_documents WHERE kind=?", (AUTH_KIND,)).fetchall()
        existing = c.execute(
            "SELECT scope_id, content FROM runtime_documents WHERE kind=?",
            (ledger.LEDGER_KIND,)).fetchall()
    finally:
        c.close()
    return rows, auth, existing


def build_entries(rows, auth):
    """把每次提交映射成一条台账。只覆盖有明显结果证据的提交。"""
    by_scope = {}
    for row in auth:
        content = row['content']
        doc = json.loads(content) if isinstance(content, str) else content
        if isinstance(doc, dict) and doc.get('target_sha256'):
            by_scope[row['scope_id']] = doc
    entries = []
    for row in rows:
        doc = by_scope.get(row['scope_id'])
        if doc is None:
            continue
        if row['status'] == 'blocked':
            continue                      # 被内核拒绝的，没有发生任何外部动作
        state = {
            'completed': ledger.STATE_SUBMITTED,
            'outcome_unknown': ledger.STATE_UNKNOWN,
            'failed': ledger.STATE_UNKNOWN,
        }.get(row['status'])
        if state is None:
            continue
        entry = ledger.new_entry(
            scope_id=row['scope_id'], target_sha256=doc['target_sha256'],
            body_sha256=doc.get('body_sha256') or '',
            action=doc.get('action', 'reply'), ts=row['created_at'], note=BACKFILL_NOTE)
        entry['task_id'] = row['task_id']
        entry['backfilled'] = True
        entry['error_code'] = row['error_code']
        entry = ledger.close(entry, state, note=BACKFILL_NOTE, ts=row['created_at'])
        entry['task_id'] = row['task_id']
        entry['backfilled'] = True
        entry['error_code'] = row['error_code']
        entries.append(entry)
    return entries


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true', help='真正写入（默认只打印）')
    parser.add_argument('--db', default=DB)
    args = parser.parse_args()

    rows, auth, existing = load(args.db)
    entries = build_entries(rows, auth)
    print('扫描到 chat.submit 真实提交 %d 次，可补账 %d 条' % (len(rows), len(entries)))
    for entry in entries:
        print('  %s  %s  target=%s  %s'
              % (entry['day'], entry['state'], entry['target_sha256'][:12],
                 entry.get('error_code') or ''))
    if not entries:
        print('没有需要补账的记录。')
        return 0

    current = {}
    for row in existing:
        content = row['content']
        doc = json.loads(content) if isinstance(content, str) else content
        current[row['scope_id']] = doc if isinstance(doc, dict) else {}

    plan = {}
    for entry in entries:
        scope = entry['scope_id']
        items = plan.get(scope)
        if items is None:
            items = (current.get(scope) or {}).get('items') or []
        if any(i.get('ledger_id') == entry['ledger_id'] for i in items if isinstance(i, dict)):
            print('  跳过（已存在）: %s' % entry['ledger_id'])
            continue
        plan[scope] = ledger.merge(items, entry)

    if not args.apply:
        print('\n[dry-run] 将写入 %d 个 scope 的台账；确认后加 --apply 执行。' % len(plan))
        return 0

    c = sqlite3.connect(args.db)
    try:
        with c:
            for scope, items in plan.items():
                row = c.execute(
                    "SELECT revision FROM runtime_documents WHERE scope_id=? AND kind=?",
                    (scope, ledger.LEDGER_KIND)).fetchone()
                revision = (row[0] if row else 0) + 1
                c.execute(
                    "INSERT INTO runtime_documents VALUES (?,?,?,?) "
                    "ON CONFLICT(scope_id,kind) DO UPDATE SET revision=excluded.revision,"
                    "content=excluded.content",
                    (scope, ledger.LEDGER_KIND, revision,
                     json.dumps({'items': items}, ensure_ascii=False)))
    finally:
        c.close()
    print('已写入 %d 个 scope 的台账。' % len(plan))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
