"""Canonical entry: main → tool_router → execution_engine → sandbox → tool."""
import json
import sys
if __name__ == "__main__":
    # Serving from this entry must not instantiate a second kernel on `import main`.
    sys.modules["main"] = sys.modules[__name__]
from execution_engine import ExecutionEngine
from tool_router import ToolRouter
import runtime_tools

_router = ToolRouter()
_engine = ExecutionEngine(_router)
runtime_tools.register(_router, _engine)


def dispatch(tool, payload=None, context_token=None):
    task = _engine.dispatch(tool, payload, context_token)
    return {"ok": True, "data": task["result"], "trace_id": task["task_id"]}


def cli():
    if len(sys.argv) > 1 and sys.argv[1] in {'backup', 'backup-check', 'applicant-browser', 'applicant-chat'}:
        import argparse
        from sandbox import KernelError, maintenance_access
        operation = sys.argv[1]
        parser = argparse.ArgumentParser(prog='python main.py ' + operation)
        if operation == 'backup':
            parser.add_argument('--confirm-all-workspaces', action='store_true')
        elif operation == 'backup-check':
            parser.add_argument('--backup-id', required=True)
        else:
            parser.add_argument('--open', action='store_true')
        args = parser.parse_args(sys.argv[2:])
        try:
            with maintenance_access():
                context = dispatch('runtime.context')['data']
                if operation == 'applicant-browser':
                    tool, payload = 'runtime.applicant.browser.open', {'confirmed': args.open}
                elif operation == 'applicant-chat':
                    tool, payload = 'runtime.applicant.chat.open', {'confirmed': args.open}
                elif operation == 'backup':
                    tool, payload = 'runtime.database.backup', {'confirmed_global': args.confirm_all_workspaces}
                else:
                    tool, payload = 'runtime.database.backup.check', {'backup_id': args.backup_id}
                result = dispatch(tool, payload, context['context_token'])
            print(json.dumps(result, ensure_ascii=False))
            return 0
        except KernelError as error:
            print(json.dumps({'ok': False, 'code': error.code, 'message': str(error),
                              'trace_id': getattr(error, 'trace_id', None)}, ensure_ascii=False), file=sys.stderr)
            return 1
    if len(sys.argv) > 1 and sys.argv[1] == "job-search":
        import argparse
        from sandbox import KernelError, maintenance_access
        parser = argparse.ArgumentParser(prog='python main.py job-search',
                                         description='Bounded read-only BOSS job search (maintenance).')
        parser.add_argument('--keyword', required=True)
        parser.add_argument('--city', required=True)
        parser.add_argument('--pages', type=int, default=3)
        parser.add_argument('--details', type=int, default=0,
                            help='读完列表后按上限补读详情 JD（0 不读，1..5）')
        parser.add_argument('--reset', action='store_true')
        args = parser.parse_args(sys.argv[2:])
        if not 1 <= args.pages <= 6:
            print(json.dumps({'ok': False, 'code': 'INVALID_JOB_SEARCH',
                              'message': '一次最多读取 6 页。'}, ensure_ascii=False), file=sys.stderr)
            return 1
        if not 0 <= args.details <= 5:
            print(json.dumps({'ok': False, 'code': 'INVALID_JOB_SEARCH',
                              'message': '一次最多补 5 条详情。'}, ensure_ascii=False), file=sys.stderr)
            return 1
        try:
            with maintenance_access():
                context = dispatch('runtime.context')['data']
                if args.reset:
                    try:
                        dispatch('runtime.applicant.jobs.search.reset',
                                 {'confirmed': True}, context['context_token'])
                    except KernelError as error:
                        if error.code != 'JOB_SEARCH_REQUIRED':
                            raise
                opened = dispatch('runtime.applicant.jobs.search.open',
                                  {'keyword': args.keyword, 'city': args.city}, context['context_token'])
                if not opened['ok']:
                    print(json.dumps(opened, ensure_ascii=False))
                    return 1
                for _ in range(args.pages):
                    step = dispatch('runtime.applicant.jobs.search.step', {}, context['context_token'])
                    event = step['data'].get('event') or {}
                    print(json.dumps({'step': event.get('status'), 'added': event.get('added'),
                                      'pages': step['data']['state']['counts']['pages_read'],
                                      'jobs': step['data']['state']['counts']['jobs'],
                                      'stop_reason': step['data']['state']['stop_reason']},
                                     ensure_ascii=False))
                    if step['data']['status'] in {'navigation_required', 'finished'} or \
                            step['data']['state']['status'] != 'running':
                        break
                summary = dispatch('runtime.applicant.jobs.search.get', {}, context['context_token'])
            print(json.dumps(summary, ensure_ascii=False))
            if args.details and summary['data']['state'] is not None:
                # 只补缺口：每次最多 args.details 条，读完就停；单条失败已在 results 里。
                for _ in range(summary['data']['state']['counts']['jobs'] or 1):
                    with maintenance_access():
                        batch = dispatch('runtime.applicant.jobs.search.details',
                                         {'limit': args.details}, context['context_token'])['data']
                    print(json.dumps({key: batch[key] for key in
                                      ('status', 'filled', 'failed', 'remaining', 'stopped', 'results')},
                                     ensure_ascii=False))
                    if batch['remaining'] <= 0 or batch['filled'] <= 0 or batch['stopped']:
                        break
            return 0
        except KernelError as error:
            print(json.dumps({'ok': False, 'code': error.code, 'message': str(error),
                              'trace_id': getattr(error, 'trace_id', None)}, ensure_ascii=False), file=sys.stderr)
            return 1
    if len(sys.argv) > 1 and sys.argv[1] == "outcome":
        import argparse
        from sandbox import KernelError
        parser = argparse.ArgumentParser(
            prog='python main.py outcome',
            description='核对真实发送结果（只读核对或人工结论，不发任何东西）。')
        sub = parser.add_subparsers(dest='action', required=True)
        sub.add_parser('list', help='看还有几条发送结果没核对')
        sub.add_parser('verify', help='读页面回显与点击前基线比对')
        resolve = sub.add_parser('resolve', help='人工给出核对结论（唯一解除路径）')
        resolve.add_argument('ledger_id')
        resolve.add_argument('resolution', choices=['delivered', 'not_delivered',
                                                    'duplicate_confirmed'])
        resolve.add_argument('--by', required=True, help='核对人')
        resolve.add_argument('--note', default=None)
        archive = sub.add_parser(
            'archive',
            help='把已联系证据归档到同库归档文档（不删除；归档后仍拦得住同一对象）')
        archive.add_argument('--by', required=True, help='归档操作人')
        archive.add_argument('--ledger-id', action='append', default=None,
                             help='只归档这条台账记录（可重复；未闭环会被拒绝）')
        archive.add_argument('--target', default=None,
                             help='只归档这个目标指纹（64 位 sha256）的已联系证据')
        args = parser.parse_args(sys.argv[2:])
        try:
            context = dispatch('runtime.context')['data']
            if args.action == 'list':
                result = dispatch('runtime.applicant.chat.outcome.get', {},
                                  context['context_token'])
            elif args.action == 'verify':
                result = dispatch('runtime.applicant.chat.outcome.verify', {},
                                  context['context_token'])
            elif args.action == 'archive':
                payload = {'confirmed_by': args.by}
                if args.ledger_id:
                    payload['ledger_ids'] = list(args.ledger_id)
                if args.target:
                    payload['target_sha256'] = args.target
                result = dispatch('runtime.applicant.chat.outcome.archive', payload,
                                  context['context_token'])
            else:
                payload = {'ledger_id': args.ledger_id, 'resolution': args.resolution,
                           'confirmed_by': args.by}
                if args.note:
                    payload['note'] = args.note
                result = dispatch('runtime.applicant.chat.outcome.resolve', payload,
                                  context['context_token'])
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        except KernelError as error:
            print(json.dumps({'ok': False, 'code': error.code, 'message': str(error),
                              'trace_id': getattr(error, 'trace_id', None)},
                             ensure_ascii=False), file=sys.stderr)
            return 1
    if len(sys.argv) > 1 and sys.argv[1] == "context":
        print(json.dumps(dispatch("runtime.context"), ensure_ascii=False, indent=2))
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        sys.argv = [sys.argv[0], *sys.argv[2:]]
    import app
    return app.main()


if __name__ == "__main__":
    raise SystemExit(cli())
