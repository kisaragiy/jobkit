"""Read-only local kernel self-check; never changes customer settings or sends."""
from __future__ import annotations
import argparse
import json
import os
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError
from local_client import local_opener


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default=os.environ.get('JOBKIT_URL', 'http://127.0.0.1:18770'))
    origin = parser.parse_args().url.rstrip('/')
    client = local_opener(origin)
    def get(path, token=None):
        request = Request(origin + path, headers={'X-Jobkit-Context': token} if token else {})
        with client.open(request, timeout=10) as response:
            return json.load(response)
    if get('/health') != {'service': 'jobkit', 'ok': True}:
        raise RuntimeError('Local service identity does not match')
    context = get('/api/runtime/context')['data']
    token = context['context_token']
    for path in ('/api/runtime/document', '/api/runtime/tasks', '/api/runtime/events', '/api/runtime/recovery'):
        if not get(path, token).get('ok'):
            raise RuntimeError('Kernel read check failed')
    try:
        build_opener(ProxyHandler({})).open(origin + '/api/runtime/context', timeout=10)
    except HTTPError as error:
        if error.code != 401:
            raise
    else:
        raise RuntimeError('Unauthenticated API access was accepted')
    print('PASS: kernel session, scoped documents/tasks/events/recovery, and unauthenticated request denial')
    print('No platform calls, configuration changes, or send operations were performed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
