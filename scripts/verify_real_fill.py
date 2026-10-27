"""真实填入验证：把正文填进 BOSS 输入框，观察按钮是否解禁。**不发送。**

第一步只填不发——因为一旦填进乱码又点发送，HR 就真收到了。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main
import store
import applicant_browser

TEXT = ('您好，上周面试后一直没收到后续结果，想了解一下进展。'
        '如果暂时没有名额也没关系，麻烦您告知一声，谢谢。')


def log(name, payload):
    print(json.dumps({'step': name, 'data': payload}, ensure_ascii=False))
    sys.stdout.flush()


def main_run():
    with store.runtime_db() as c:
        context = store.runtime_context(c)
    token = context['context_token']

    def dispatch(tool, payload=None):
        return main.dispatch(tool, payload or {}, token)['data']

    log('target', dispatch('runtime.applicant.chat.target'))
    before = dispatch('runtime.applicant.chat.observe')
    log('observe_before', {'composer_ready': before.get('composer_ready'),
                           'can_send': before.get('can_send'),
                           'send_disabled': before.get('send_disabled')})

    with store.runtime_db() as c:
        _ = c
    # real_fill 需要 current_execution 注册，走内核工具时由 dispatch 提供；
    # 这里直接调 browser 层，需在维护上下文里
    from sandbox import maintenance_access
    with maintenance_access():
        filled = applicant_browser.real_fill(TEXT)
    log('fill', filled)

    after = dispatch('runtime.applicant.chat.observe')
    log('observe_after', {'can_send': after.get('can_send'),
                          'send_disabled': after.get('send_disabled'),
                          'panel_text_tail': (after.get('panel_text') or '')[-80:]})
    composer = applicant_browser.real_read_composer() if True else {}
    log('composer', {'text_len': len(composer.get('text') or ''),
                     'matched': (composer.get('text') or '').strip() == TEXT,
                     'send_disabled': composer.get('send_disabled'),
                     'point': composer.get('point')})
    return 0


if __name__ == '__main__':
    sys.exit(main_run())
