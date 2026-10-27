/* Fixed job-detail reader exercised against a fictional detail page. No platform writes. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const script = fs.readFileSync(path.join(__dirname, '../applicant_job_detail_read.js'), 'utf8');
const node = text => ({innerText: text, getClientRects: () => [{}]});

function read({jd = '负责 Agent 编排与工具调用链路的开发与维护。'.repeat(3),
               title = 'Agent 开发', company = '虚构公司', body = '',
               login = false, pathname = '/job_detail/fictional~id.html',
               hostname = 'www.zhipin.com'} = {}) {
  const fields = {'.job-banner .name': node(title), '.job-banner .company-info .name': node(company),
    '.job-sec-text': jd === null ? null : node(jd)};
  const document = {
    body: {innerText: body},
    querySelector: selector => fields[selector] || null,
    querySelectorAll: selector => selector.startsWith('.login-dialog')
      ? (login ? [node('登录')] : []) : [],
  };
  return JSON.parse(JSON.stringify(vm.runInNewContext(script,
    {document, location: {hostname, pathname}})));
}

const actual = read();
assert.equal(actual.status, 'observed');
assert.equal(actual.job_id, 'fictional~id');
assert.equal(actual.truncated, false);
assert.ok(actual.jd.includes('Agent 编排'));
assert.equal(actual.title, 'Agent 开发');

// 职责区缺失 ≠ 岗位没有职责
assert.equal(read({jd: null}).status, 'jd_missing');

// 超长正文截断到 4000，并显式标记
const long = read({jd: '甲'.repeat(5000)});
assert.equal(long.status, 'observed');
assert.equal(long.jd.length, 4000);
assert.equal(long.truncated, true);

// 终止态：验证码 / 登录 / 不是详情页 / 不是平台域
assert.equal(read({body: '请完成验证'}).status, 'restricted');
assert.equal(read({login: true}).status, 'auth_required');
assert.equal(read({pathname: '/web/geek/jobs'}).status, 'detail_page_required');
assert.equal(read({pathname: '/job_detail/bad/path.html'}).status, 'detail_page_required');
assert.equal(read({hostname: 'evil.example.com'}).status, 'detail_page_required');

console.log('PASS: job-detail JD reader (observed, jd_missing, truncation, auth, wrong page).');
