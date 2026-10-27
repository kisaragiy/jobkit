"""一次性验证脚本：走完 B 方案四步协议后真发一条消息，并记录前后证据。

⚠️ 这不是生产路径。内核算子（authorize/execute 工具）尚未接线，
本脚本手动执行协议逻辑，用来验证「协议 + 真实写入」是否真的成立。
生产前必须把这些逻辑搬进内核工具。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import applicant_browser
import applicant_conversation as C
import main
import sandbox
import store

TEXT = ('您好，上周面试后一直没收到后续结果，想了解一下进展。'
        '如果暂时没有名额也没关系，麻烦您告知一声，谢谢。')
BY = 'user'


def log(step, data):
    print(json.dumps({'step': step, 'data': data}, ensure_ascii=False))
    sys.stdout.flush()


def main_run():
    with store.runtime_db() as c:
        context = store.runtime_context(c)
        profile_rev = store.runtime_document(c, context['scope_id'], 'profile')['revision']
        plan_rev = store.runtime_document(c, context['scope_id'], 'applicant_plan')['revision']
    token = context['context_token']

    from sandbox import maintenance_access

    def dispatch(tool, payload=None):
        with maintenance_access():
            return main.dispatch(tool, payload or {}, token)['data']

    # 1) 读真实身份
    target = dispatch('runtime.applicant.chat.target')
    log('1_target', target)
    if target.get('status') != 'target_read':
        log('abort', 'target 读不到，不发')
        return 2
    snapshot = C.target_snapshot(name=target['name'], company=target['company'],
                                 position=target.get('position'))

    # 2) 人工确认（90s）
    confirmation = C.confirmation_required(snapshot, by=BY)
    log('2_confirmation', {'by': confirmation['by'],
                           'ttl': C.CONFIRMATION_TTL_SECONDS,
                           'target_sha256': confirmation['target_sha256'][:16]})

    # 3) 一次性授权（120s）
    document = C.authorization(scope_id=context['scope_id'], target=snapshot,
                               confirmation=confirmation, text=TEXT,
                               profile_revision=profile_rev, plan_revision=plan_rev)
    log('3_authorization', {'status': document['status'], 'max_uses': document['max_uses'],
                            'ttl': C.AUTHORIZATION_TTL_SECONDS,
                            'body_sha256': document['body_sha256'][:16]})

    # 3b) 授权落库：内核 submit 的 external_write 门会读它
    with store.runtime_db() as c:
        c.execute('BEGIN IMMEDIATE')
        existing = store.runtime_document(c, context['scope_id'], 'conversation_authorization')
        c.execute("INSERT INTO runtime_documents VALUES (?,?,?,?) "
                  "ON CONFLICT(scope_id,kind) DO UPDATE SET revision=excluded.revision, "
                  "content=excluded.content",
                  (context['scope_id'], 'conversation_authorization',
                   existing['revision'] + 1, json.dumps(document, ensure_ascii=False)))
    log('3b_persisted', {'revision': existing['revision'] + 1})

    # 4) 发送前重读真实身份并核对
    current = dispatch('runtime.applicant.chat.target')
    if current.get('status') != 'target_read':
        log('abort', '发送前读不到身份')
        return 2
    current_snapshot = C.target_snapshot(name=current['name'], company=current['company'],
                                        position=current.get('position'))
    code = C.check(document, target=current_snapshot, text=TEXT,
                   profile_revision=profile_rev, plan_revision=plan_rev)
    log('4_check', {'code': code, 'diff': C.observe_target(current_snapshot, document=document)})
    if code:
        log('abort', code)
        return 2

    # 填入
    filled = dispatch('runtime.applicant.chat.fill', {'text': TEXT})
    log('5_fill', filled)
    if not filled.get('matched'):
        log('abort', '填入内容不一致')
        return 2
    point = filled['point']
    before = dispatch('runtime.applicant.chat.observe')
    log('6_observe_before', {'delivered': before['delivered_marks'],
                             'read': before['read_marks'],
                             'panel_len': len(before.get('panel_text') or '')})

    # 发送（真发）
    sent = dispatch('runtime.applicant.chat.submit', {'point': point})
    log('7_submit', sent)

    time.sleep(3.0)
    after = dispatch('runtime.applicant.chat.observe')
    log('8_observe_after', {'delivered': after['delivered_marks'],
                            'read': after['read_marks'],
                            'can_send': after['can_send'],
                            'panel_len': len(after.get('panel_text') or ''),
                            'panel_tail': (after.get('panel_text') or '')[-120:]})

    evidence = {
        'target': snapshot, 'text': TEXT, 'check_code': code,
        'before': {'delivered': before['delivered_marks'], 'read': before['read_marks']},
        'after': {'delivered': after['delivered_marks'], 'read': after['read_marks']},
        'delivered_delta': after['delivered_marks'] - before['delivered_marks'],
        'composer_cleared': after['can_send'] is False,
    }
    with open('tmp/dom-snapshots/real-send-evidence.json', 'w', encoding='utf-8') as fh:
        json.dump(evidence, fh, ensure_ascii=False, indent=2)
    log('9_evidence', evidence)
    return 0


if __name__ == '__main__':
    sys.exit(main_run())
