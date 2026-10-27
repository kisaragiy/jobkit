/**
 * blocker-detect.js 单测 —— 全部用 DOM 桩，不碰真页面、不发任何请求。
 *
 * 为什么必须测「拒绝同意」那一条：隐私弹窗上「同意」和「拒绝同意」是两个按钮，
 * 用 includes('同意') 匹配会点到「拒绝同意」= 直接拒绝授权。这是灾难级 bug，
 * 而它在真机上很难复现（要先弹出来），所以必须靠桩把两个按钮都覆盖。
 */
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const SRC = path.join(__dirname, 'renderer', 'blocker-detect.js');
const JK = require(SRC);          // 模块本身可直接 require（纯逻辑）

let fail = 0;
function ck(name, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) fail++;
  console.log(`  ${ok ? '✅' : '❌'} ${name}`);
  if (!ok) console.log(`      得到 ${JSON.stringify(got)} · 期望 ${JSON.stringify(want)}`);
}
function ckTrue(name, cond, extra) {
  if (!cond) fail++;
  console.log(`  ${cond ? '✅' : '❌'} ${name}${cond ? '' : '  ← ' + (extra || '')}`);
}

/* ── DOM 桩 ──────────────────────────────────────────────── */
function fakeEl(o) {
  o = o || {};
  const mk = () => ({
    className: '', id: '', innerText: '', textContent: '', parentElement: null,
    getBoundingClientRect: () => ({ width: 10, height: 10, top: 0, left: 0 }),
    querySelector: () => null, querySelectorAll: () => [],
  });
  return {
    className: o.className || '',
    id: o.id || '',
    innerText: o.text || '',
    textContent: o.text || '',
    parentElement: null,
    getBoundingClientRect: () => ({
      width: o.w == null ? 400 : o.w,
      height: o.h == null ? 300 : o.h, top: 0, left: 0,
    }),
    querySelector: (sel) => {
      if (/job-card-wrapper|job-list-box|ul\.job-list|chat-input|chat-conversation/.test(sel)) {
        return o.skipContains ? mk() : null;
      }
      if (/\[class\*=|\[id\*=/.test(sel)) return o.descendantHint ? mk() : null;
      return null;
    },
    querySelectorAll: (sel) => {
      if (sel === '[class],[id]') return o.decorations || [];
      if (/button|\.ant-btn|\.el-button|span/.test(sel)) {
        return (o.buttons || []).map(b => ({
          className: '', id: '', innerText: b, textContent: b, parentElement: null,
          getBoundingClientRect: () => ({ width: 60, height: 30, top: 0, left: 0 }),
          querySelector: () => null, querySelectorAll: () => [],
          click: () => { o.__clicked = b; },
        }));
      }
      return [];
    },
  };
}

function runScript(src, opts) {
  const doc = {
    querySelector: (sel) => {
      if (/geetest|nc-container|captcha/.test(sel)) return (opts && opts.captcha) ? fakeEl() : null;
      return null;
    },
    querySelectorAll: (sel) => (/dialog|modal|popup|wrap|container/.test(sel) ? (opts && opts.dialogs) || [] : []),
  };
  const sandbox = {
    document: doc,
    location: { href: (opts && opts.url) || 'https://www.zhipin.com/web/geek/job' },
    window: { getComputedStyle: () => ({ display: 'block', visibility: 'visible', opacity: '1' }) },
    setTimeout: setTimeout, Date: Date, Promise: Promise, console: console,
  };
  sandbox.window.document = doc;
  return vm.runInNewContext(src, sandbox);
}

console.log('=== 1. 尺寸门槛：小气泡不算拦截 ===');
ck('太小的 dialog', JK.classify({ width: 100, height: 40, text: '一些提示文字够长了吧', classBlob: '', hasButton: true }), null);

console.log('\n=== 2. 白名单：正常流程不能当拦截 ===');
ck('class 含 greet-boss（招呼框）',
  JK.classify({ width: 400, height: 300, text: '随便什么', classBlob: 'greet-boss-dialog', hasButton: true }), null);
ck('文本命中「已向BOSS发送消息」',
  JK.classify({ width: 400, height: 300, text: '已向BOSS发送消息，请留在此页', classBlob: '', hasButton: true }), null);

console.log('\n=== 3. 隐私协议 → 允许自动点 ===');
ck('识别 privacy',
  JK.classify({ width: 500, height: 400, text: '个人信息保护须知 请您阅读并同意', classBlob: '', hasButton: true }),
  { kind: 'privacy', preview: '个人信息保护须知 请您阅读并同意' });
ck('privacy 的处置 = dismiss', JK.decide({ kind: 'privacy' }).action, 'dismiss');

console.log('\n=== 4. ★ 同意按钮必须精确匹配（不能 includes）===');
ckTrue('「同意」可点', JK.isPrivacyAgreeLabel('同意') === true);
ckTrue('「同意并继续」可点', JK.isPrivacyAgreeLabel('同意并继续') === true);
ckTrue('「同意」带空格也能认', JK.isPrivacyAgreeLabel(' 同 意 ') === true);
ckTrue('★「拒绝同意」绝不可点', JK.isPrivacyAgreeLabel('拒绝同意') === false, 'includes 匹配会误点！');
ckTrue('「不同意」不可点', JK.isPrivacyAgreeLabel('不同意') === false);
ckTrue('「暂不」不可点', JK.isPrivacyAgreeLabel('暂不') === false);

console.log('\n=== 5. 风控 / 登录 → 硬停，不重试 ===');
ck('安全验证 → block',
  JK.classify({ width: 400, height: 300, text: '请完成安全验证', classBlob: '', hasButton: true }),
  { kind: 'block', preview: '请完成安全验证' });
ck('block 的处置 = stop', JK.decide({ kind: 'block' }).action, 'stop');
ck('请先登录 → login',
  JK.classify({ width: 400, height: 300, text: '请先登录后查看', classBlob: '', hasButton: true }),
  { kind: 'login', preview: '请先登录后查看' });
ck('login 的处置 = stop', JK.decide({ kind: 'login' }).action, 'stop');

console.log('\n=== 6. 沟通次数用尽（预期结束，重试无意义）===');
ck('次数用尽 → quota',
  JK.classify({ width: 400, height: 300, text: '今日沟通次数用尽，明天再来', classBlob: '', hasButton: true }),
  { kind: 'quota', preview: '今日沟通次数用尽，明天再来' });
ckTrue('「还剩 30 次」不算用尽（不能误判）',
  JK.classify({ width: 400, height: 300, text: '今日还剩 30 次沟通', classBlob: '', hasButton: true }).kind !== 'quota');
ckTrue('「还剩 0 次」算用尽',
  JK.classify({ width: 400, height: 300, text: '今日还剩 0 次沟通', classBlob: '', hasButton: true }).kind === 'quota');

console.log('\n=== 7. ★ 未知弹窗：绝不乱点，一律停 ===');
ck('有按钮但认不出来 → unknown',
  JK.classify({ width: 400, height: 300, text: '这是一段无法归类的中文弹窗文案', classBlob: '', hasButton: true }).kind, 'unknown');
ck('unknown 的处置 = stop（不点任何按钮）', JK.decide({ kind: 'unknown' }).action, 'stop');

console.log('\n=== 8. 停投原因分级（expected 不告警 / abnormal 要显眼）===');
ck('quota → 预期结束', JK.stopReasonCode('quota'), { code: 'quota-exhausted', abnormal: false });
ck('login → 预期结束', JK.stopReasonCode('login'), { code: 'not-logged-in', abnormal: false });
ck('captcha → 异常', JK.stopReasonCode('captcha'), { code: 'captcha', abnormal: true });
ck('unknown → 异常', JK.stopReasonCode('unknown'), { code: 'popup', abnormal: true });
ck('未登记 → 归 unknown 异常', JK.stopReasonCode('什么鬼'), { code: 'unknown', abnormal: true });

console.log('\n=== 9. 列表 URL 判定 + 卡片选择器 ===');
ckTrue('/web/geek/job 是列表', JK.isJobListUrl('https://www.zhipin.com/web/geek/job?query=AI') === true);
ckTrue('/web/user/ 不是列表', JK.isJobListUrl('https://www.zhipin.com/web/user/') === false);
ckTrue('卡片选择器含现行 class .job-card-box（实测校准）',
  JK.JOB_CARD_SEL.indexOf('.job-card-box') >= 0, JK.JOB_CARD_SEL);
ckTrue('卡片选择器保留旧 class 兜底',
  JK.JOB_CARD_SEL.indexOf('.job-card-wrapper') >= 0, JK.JOB_CARD_SEL);
ckTrue('数卡脚本 ok=false 时 count=0', (() => {
  const r = vm.runInNewContext(JK.buildCountCardsScript(), {
    document: { querySelectorAll: () => [] }, location: { href: 'x' },
  });
  return r.count === 0 && r.ok === false;
})(), 'count=0 必须 ok=false，避免上层把空页当就绪');

console.log('\n=== 10. 注入脚本：语法正确 + 真能跑（转义回归）===');
for (const [name, src] of [
  ['detect', JK.buildDetectScript()],
  ['privacyAgree', JK.buildPrivacyAgreeScript()],
  ['waitListReady', JK.buildWaitListReadyScript(300)],
  ['countCards', JK.buildCountCardsScript()],
]) {
  try {
    new vm.Script(src);
    ckTrue(`${name} 脚本语法 OK`, true);
  } catch (e) {
    ckTrue(`${name} 脚本语法 OK`, false, e.message);
  }
}

console.log('\n=== 11. detect 脚本端到端（真跑在桩 DOM 上）===');
let r = runScript(JK.buildDetectScript(), { dialogs: [] });
ck('空页面 → 无拦截', r, null);

r = runScript(JK.buildDetectScript(), { captcha: true, dialogs: [] });
ck('出现验证码组件 → captcha', r && r.kind, 'captcha');

r = runScript(JK.buildDetectScript(), {
  dialogs: [fakeEl({ w: 500, h: 400, text: '个人信息保护须知 请阅读', buttons: ['同意'] })],
});
ck('真跑：识别隐私弹窗', r && r.kind, 'privacy');

r = runScript(JK.buildDetectScript(), {
  dialogs: [fakeEl({ w: 500, h: 400, text: '个人信息保护须知', buttons: ['同意'], skipContains: true })],
});
ck('弹窗里含列表元素 → 不当拦截', r, null);

r = runScript(JK.buildDetectScript(), {
  dialogs: [fakeEl({ w: 500, h: 400, text: '个人信息保护须知', className: 'greet-boss-dialog', buttons: ['同意'] })],
});
ck('class 白名单命中 → 不当拦截', r, null);

console.log('\n=== 12. privacyAgree 脚本端到端 ===');
const dlgOpts = { w: 500, h: 400, text: '个人信息保护须知 请您阅读', buttons: ['拒绝同意', '同意'] };
runScript(JK.buildPrivacyAgreeScript(), { dialogs: [fakeEl(dlgOpts)] });
ck('点了哪一个（脚本返回值）', runScript(JK.buildPrivacyAgreeScript(), { dialogs: [fakeEl(dlgOpts)] }).clicked, '同意');
ckTrue('★ 实际点击的是「同意」而不是「拒绝同意」', dlgOpts.__clicked === '同意', `实际点了 ${dlgOpts.__clicked}`);

const dlg2Opts = { w: 500, h: 400, text: '个人信息保护须知', buttons: ['拒绝同意', '不同意'] };
r = runScript(JK.buildPrivacyAgreeScript(), { dialogs: [fakeEl(dlg2Opts)] });
ck('只有拒绝类按钮 → 不点，报出来', r, { ok: false, reason: 'agree-button-not-found', text: '个人信息保护须知' });
ckTrue('★ 一个按钮都没点（绝不误点拒绝类）', dlg2Opts.__clicked === undefined, `实际点了 ${dlg2Opts.__clicked}`);

console.log('\n' + '='.repeat(58));
console.log(fail ? `❌ ${fail} 项失败` : '✅ 全部通过（含「拒绝同意」防误点、未知弹窗不乱点两条红线）');
process.exit(fail ? 1 : 0);
