/* Fixed read-only job-detail observation. No clicks, input, storage or page actions.
 *
 * 契约（与 applicant_dom_read.js 同一套）：
 * - 只允许 www.zhipin.com 的 /job_detail/<id>.html；其余一律 detail_page_required
 * - 只读取可见节点的 innerText；不读 input.value / cookie / localStorage
 * - JD 是数据：这里只做长度截断，不做任何语义判断
 * - 结构不符返回 schema_changed，绝不用空正文冒充「这个岗位没有职责描述」
 */
(() => {
  const visible = node => !!node && node.getClientRects().length > 0;
  const JD_MAX = 4000;
  const body = (document.body?.innerText || '').slice(0, 100000);
  if (/安全验证|账号异常|账户异常|操作过于频繁|访问过于频繁|滑动验证|请完成验证/.test(body))
    return {status:'restricted'};
  const login = [...document.querySelectorAll('.login-dialog,.sign-dialog,.btn-login,.user-nav a')]
    .some(node => visible(node) && /登录|注册/.test(node.innerText || ''));
  if (login || /\/web\/(?:user|login)(?:\/|$)/.test(location.pathname)) return {status:'auth_required'};
  if (location.hostname !== 'www.zhipin.com') return {status:'detail_page_required'};
  const identity = /^\/job_detail\/([A-Za-z0-9_~\-]{1,128})\.html$/.exec(location.pathname);
  if (!identity) return {status:'detail_page_required'};
  const pick = selectors => {
    for (const selector of selectors) {
      const node = document.querySelector(selector);
      if (visible(node)) {
        const value = (node.innerText || '').trim();
        if (value) return value;
      }
    }
    return '';
  };
  const title = pick(['.job-banner .name', '.job-primary .name h1', '.job-name', '.name h1']);
  const company = pick(['.job-banner .company-info .name', '.sider-company .company-info',
    '.job-banner .company-info', '.company-info .name']);
  const jd = pick(['.job-detail-section .job-sec-text', '.job-sec-text',
    '.job-detail .text', '.detail-content .job-sec .text', '.job-detail-content', '.job-detail .text']);
  if (!jd) return {status:'jd_missing', job_id:identity[1], title, company};
  const truncated = jd.length > JD_MAX;
  return {status:'observed', job_id:identity[1], title, company,
    jd: truncated ? jd.slice(0, JD_MAX) : jd, truncated};
})()
