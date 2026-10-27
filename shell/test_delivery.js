/**
 * delivery-engine.js 单测 —— 纯逻辑 + 脚本生成，不碰真页面。
 *
 * 重点覆盖三条安全红线：
 *   ① buildClickChatScript(false) 必须拒绝执行（dry-run 永不误触开聊）
 *   ② 熔断分支必须齐（风控/连续失败/超上限）
 *   ③ 节奏计划必须落在区间内且插入长休息
 */
const vm = require('vm');
const path = require('path');
const JK = require(path.join(__dirname, 'renderer', 'delivery-engine.js'));

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

console.log('=== 1. job id 抽取（用实测拿到的真值）===');
ck('从沟通按钮 ka 抽 id',
  JK.extractJobId('cpc_job_list_chat_1489b9c1f8d362a60nB92Ni8GFpR'),
  '1489b9c1f8d362a60nB92Ni8GFpR');
ck('从详情链接抽 id',
  JK.extractJobIdFromHref('/job_detail/1489b9c1f8d362a60nB92Ni8GFpR.html'),
  '1489b9c1f8d362a60nB92Ni8GFpR');
ck('公司链接不应误抽成 job id',
  JK.extractJobIdFromHref('/gongsi/dd91164449acb6a61HJy2Ni-FlM~.html?from=top-card'),
  '');
ck('空值不崩', JK.extractJobId(''), '');

console.log('\n=== 2. 节奏计划 ===');
let plan = JK.planPacing(12, { minSec: 60, maxSec: 120, restEvery: 5, restSec: 600, random: () => 0.5 });
ck('条数', plan.length, 12);
ckTrue('每步都有等待', plan.every(p => p.waitSec > 0));
ckTrue('普通间隔落在 60-120',
  plan.filter(p => !p.isRest).every(p => p.waitSec >= 60 && p.waitSec <= 120),
  JSON.stringify(plan.map(p => p.waitSec)));
ck('第 5 个是长休息', plan[5].isRest, true);
ck('第 10 个是长休息', plan[10].isRest, true);
ck('长休息 = 600s', plan[5].waitSec, 600);
ck('第 0 个不是休息（不浪费）', plan[0].isRest, false);

plan = JK.planPacing(3, { minSec: 99999, maxSec: 1, restEvery: 5 });
ckTrue('非法/倒挂参数被夹紧，不产生负数', plan.every(p => p.waitSec > 0), JSON.stringify(plan));

console.log('\n=== 3. 熔断判定 ===');
ck('风控信号 → 停', JK.shouldStop({ riskSignal: 'captcha' }), { stop: true, reason: 'risk' });
ck('连续失败 3 次 → 停', JK.shouldStop({ consecutiveFails: 3 }), { stop: true, reason: 'consecutive-fails' });
ck('连续失败 2 次 → 继续', JK.shouldStop({ consecutiveFails: 2 }).stop, false);
ck('达到日上限 → 停', JK.shouldStop({ delivered: 130, maxDelivery: 130 }), { stop: true, reason: 'max-delivery' });
ck('列表空了 → 停', JK.shouldStop({ listEmpty: true }), { stop: true, reason: 'no-more-jobs' });
ck('用户手动停 → 停', JK.shouldStop({ userStop: true }), { stop: true, reason: 'user-stop' });
ck('风控优先级高于其他', JK.shouldStop({ riskSignal: 'block', delivered: 0, userStop: false }).reason, 'risk');
ck('正常状态 → 继续', JK.shouldStop({ delivered: 4, maxDelivery: 130 }).stop, false);
ck('空状态不崩', JK.shouldStop().stop, false);

console.log('\n=== 4. ★ 安全红线：dry-run 绝不能点开聊 ===');
let r = vm.runInNewContext(JK.buildClickChatScript(false), { document: { querySelector: () => ({ click() { throw new Error('不该被点到') } }) } });
ck('confirm=false → 拒绝执行', r, { ok: false, reason: 'not-confirmed' });

r = vm.runInNewContext(JK.buildClickChatScript(true), {
  document: { querySelector: () => ({ querySelector: () => null }) },
});
ck('confirm=true 但页面上没有按钮 → 停，不盲点', r, { ok: false, reason: 'chat-button-not-found' });

// 详情容器存在，按钮在其中
let clicked = false;
const mkBox = (btn) => ({ querySelector: (sel) => (btn && /op-btn-chat|btn-startchat/.test(sel) ? btn : null) });
const mkBtn = (w, h) => ({
  getBoundingClientRect: () => ({ width: w, height: h }),
  getAttribute: () => 'cpc_job_list_chat_1489b9c1f8d362a60nB92Ni8GFpR',
  innerText: '立即沟通',
  click: () => { clicked = true; },
});

r = vm.runInNewContext(JK.buildClickChatScript(true), {
  document: { querySelector: () => mkBox(mkBtn(0, 0)) },
  location: { href: 'https://www.zhipin.com/web/geek/jobs' },
});
ck('按钮不可见 → 停（不点隐藏元素）', r, { ok: false, reason: 'chat-button-invisible' });
ckTrue('★ 不可见时确实没点', clicked === false);

r = vm.runInNewContext(JK.buildClickChatScript(true), {
  document: { querySelector: () => mkBox(mkBtn(104, 34)) },
  location: { href: 'https://www.zhipin.com/web/geek/jobs' },
});
ck('confirm=true + 可见按钮 → 执行', r.ok, true);
ckTrue('★ 确实点了', clicked === true);
ck('返回里带 ka 便于对账', JK.extractJobId(r.ka), '1489b9c1f8d362a60nB92Ni8GFpR');

console.log('\n=== 5. 注入脚本语法 ===');
for (const [name, src] of [
  ['collectCards', JK.buildCollectCardsScript()],
  ['readDetail', JK.buildReadDetailScript()],
  ['focusCard', JK.buildFocusCardScript(3)],
  ['waitDetailReady', JK.buildWaitDetailReadyScript(500)],
  ['clickChat(false)', JK.buildClickChatScript(false)],
  ['clickChat(true)', JK.buildClickChatScript(true)],
]) {
  try { new vm.Script(src); ckTrue(`${name} 语法 OK`, true); }
  catch (e) { ckTrue(`${name} 语法 OK`, false, e.message); }
}

console.log('\n=== 6. collectCards 脚本端到端（桩 DOM）===');
const fakeCard = (href, title, company) => ({
  querySelector: (sel) => {
    if (/job-name|job_detail/.test(sel)) return { getAttribute: () => href, innerText: title };
    if (/boss-name|company/.test(sel)) return { innerText: company };
    if (/salary/.test(sel)) return { innerText: '15-25K' };
    return null;
  },
  innerText: `${title} 15-25K ${company}`,
});
r = vm.runInNewContext(JK.buildCollectCardsScript(), {
  document: {
    querySelectorAll: () => [
      fakeCard('/job_detail/aaaa1111bbbb2222cccc.html', 'AI工程师', '甲公司'),
      fakeCard('/job_detail/dddd3333eeee4444ffff.html', 'LLM应用', '乙公司'),
    ],
  },
  location: { href: 'https://www.zhipin.com/web/geek/jobs' },
});
ck('收集到 2 张卡', r.count, 2);
ck('第 1 张 jobId', r.cards[0].jobId, 'aaaa1111bbbb2222cccc');
ck('第 1 张标题', r.cards[0].title, 'AI工程师');
ck('第 2 张公司', r.cards[1].company, '乙公司');

console.log('\n' + '='.repeat(58));
console.log(fail ? `❌ ${fail} 项失败` : '✅ 全部通过（含 dry-run 绝不误触开聊这条红线）');


// ── 追加：输入/发送闸门（2026-09-19 实测校准后补）──
console.log('\n=== 7. ★ 发送闸门 + contenteditable 输入 ===');
let s = vm.runInNewContext(JK.buildSendScript(false), { document: { querySelector: () => ({ click() { throw new Error('不该点到') } }) } });
ck('confirm=false → 拒绝发送', s, { ok: false, reason: 'not-confirmed' });

s = vm.runInNewContext(JK.buildSendScript(true), { document: { querySelector: () => null } });
ck('找不到发送按钮 → 停', s, { ok: false, reason: 'send-button-not-found' });

let sent = false;
const disBtn = { disabled: true, className: 'btn-send disabled', getBoundingClientRect: () => ({ width: 40, height: 40 }), click: () => { sent = true; } };
s = vm.runInNewContext(JK.buildSendScript(true), { document: { querySelector: () => disBtn }, location: { href: 'x' } });
ck('按钮禁用 → 停（不硬点）', s, { ok: false, reason: 'send-button-disabled' });
ckTrue('★ 禁用时确实没点', sent === false);

const okBtn = { disabled: false, className: 'btn-send', getBoundingClientRect: () => ({ width: 40, height: 40 }), click: () => { sent = true; } };
s = vm.runInNewContext(JK.buildSendScript(true), { document: { querySelector: () => okBtn }, location: { href: 'x' } });
ck('可用按钮 → 发送', s.ok, true);
ckTrue('★ 确实点了发送', sent === true);

// 输入脚本必须走 contenteditable 路径（实测 textarea 命中 0）
const typed = [];
const fakeInput = {
  innerHTML: '', innerText: '', textContent: '',
  focus: () => { }, dispatchEvent: () => { },
};
s = vm.runInNewContext(JK.buildTypeGreetingScript('你好'), {
  document: { querySelector: (sel) => (sel === '#chat-input' ? fakeInput : null), execCommand: () => false },
  location: { href: 'x' },
  InputEvent: function () { },
});
ckTrue('输入脚本找得到 #chat-input', s.ok === true || s.reason === 'type-failed', JSON.stringify(s));

console.log('\n' + '='.repeat(58));
console.log(fail ? `❌ ${fail} 项失败` : '✅ 全部通过（含发送闸门 confirm 双分支 + 禁用按钮不硬点）');
process.exitCode = fail ? 1 : 0;

