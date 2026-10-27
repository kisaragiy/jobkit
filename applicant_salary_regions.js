/* Fixed salary-only viewport regions; no hidden state, navigation or page mutations. */
(() => {
  const visible = n => !!n && n.getClientRects().length > 0;
  const body = (document.body?.innerText || '').slice(0,100000);
  if (/安全验证|账号异常|账户异常|操作过于频繁|访问过于频繁|滑动验证|请完成验证/.test(body))
    return {status:'restricted',regions:[]};
  if ([...document.querySelectorAll('.login-dialog,.sign-dialog,.btn-login,.user-nav a')]
      .some(n => visible(n) && /登录|注册/.test(n.innerText || '')))
    return {status:'auth_required',regions:[]};
  if(document.fonts && document.fonts.status!=='loaded')return {status:'fonts_loading',regions:[]};
  if(document.visibilityState==='hidden' || innerWidth<600 || innerHeight<300)
    return {status:'browser_not_visible',regions:[]};
  const regions=[], seen=new Set();
  for(const card of document.querySelectorAll('li.job-card-box,.job-card-wrapper')) {
    if(!visible(card))continue;
    const salary=card.querySelector('.job-salary,.salary,.salary-text');
    if(!visible(salary))continue;
    const rect=salary.getBoundingClientRect();
    if(rect.left<0 || rect.top<0 || rect.right>innerWidth || rect.bottom>innerHeight || rect.width<4 || rect.height<4)continue;
    const anchor=card.querySelector('a[href*="/job_detail/"]');
    let url;try{url=new URL(anchor?.getAttribute('href')||'',location.href);}catch{return {status:'schema_changed',regions:[]};}
    const id=/^\/job_detail\/([A-Za-z0-9_~\-]{1,128})\.html$/.exec(url.pathname);
    if(url.hostname!=='www.zhipin.com'||!id||seen.has(id[1]))return {status:'schema_changed',regions:[]};
    seen.add(id[1]);
    regions.push({job_id:id[1],raw_text:(salary.innerText||'').trim().slice(0,80),
      rect:{x:rect.left,y:rect.top,width:rect.width,height:rect.height}});
    if(regions.length===8)break;
  }
  return regions.length?{status:'observed',regions,viewport:{width:innerWidth,height:innerHeight}}:
    {status:'no_visible_salary',regions};
})()
