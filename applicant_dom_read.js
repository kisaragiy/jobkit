/* Fixed read-only DOM observation. No input values, cookies, storage or page actions. */
(() => {
  const visible = node => !!node && node.getClientRects().length > 0;
  const text = (root, selectors, maximum) => {
    for (const selector of selectors) {
      const node = root.querySelector(selector);
      if (visible(node)) return (node.innerText || '').trim().slice(0, maximum);
    }
    return '';
  };
  const body = (document.body?.innerText || '').slice(0, 100000);
  if (/安全验证|账号异常|账户异常|操作过于频繁|访问过于频繁|滑动验证|请完成验证/.test(body))
    return {status:'restricted',jobs:[]};
  const login = [...document.querySelectorAll('.login-dialog,.sign-dialog,.btn-login,.user-nav a')]
    .some(node => visible(node) && /登录|注册/.test(node.innerText || ''));
  if (login || /\/web\/(?:user|login)(?:\/|$)/.test(location.pathname)) return {status:'auth_required',jobs:[]};
  const cards = [...document.querySelectorAll('li.job-card-box,.job-card-wrapper')].filter(visible);
  const jobs = [], seen = new Set();
  for (const card of cards.slice(0, 50)) {
    const anchor = card.querySelector('a[href*="/job_detail/"]');
    const href = anchor?.getAttribute('href') || '';
    let url;
    try { url = new URL(href, location.href); } catch { return {status:'schema_changed',jobs:[]}; }
    const identity = /^\/job_detail\/([A-Za-z0-9_~\-]{1,128})\.html$/.exec(url.pathname);
    if (url.hostname !== 'www.zhipin.com' || !identity || seen.has(identity[1])) return {status:'schema_changed',jobs:[]};
    seen.add(identity[1]);
    const tags = [...card.querySelectorAll('.tag-list li,.tag-list span,.job-info-list li')].filter(visible)
      .map(node => (node.innerText || '').trim());
    const degree = tags.filter(value => /^(?:学历不限|不限|高中|中专|大专|本科|硕士|博士|研究生)$/.test(value));
    const experience = tags.filter(value => /^(?:经验不限|在校\/应届|应届|应届生|\d+(?:-\d+)?年(?:以上|以内)?)$/.test(value));
    const area = text(card,['.job-area','.job-area-wrapper','.job-card__address','.company-location'],40);
    const job = {job_id:identity[1],title:text(card,['.job-name','.job-title'],200),
      company:text(card,['.company-name','.company-info a','.boss-name'],160),city:area.split('·')[0].trim(),
      salary:text(card,['.salary','.salary-text','.job-salary'],80),education:degree.length===1?degree[0]:'',
      experience:experience.length===1?experience[0]:''};
    if (!job.title || !job.company) return {status:'schema_changed',jobs:[]};
    jobs.push(job);
  }
  if (jobs.length) return {status:'observed',jobs};
  if (visible(document.querySelector('.job-list-box,.rec-job-list')) && /暂无相关职位|暂无符合|没有找到相关/.test(body)) return {status:'empty',jobs:[]};
  return {status:'schema_changed',jobs:[]};
})()
