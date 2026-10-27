/* Read visible account header only. A display name does not establish account ownership. */
(() => {
  const visible = n => !!n && n.getClientRects().length > 0;
  const body = (document.body?.innerText || '').slice(0, 100000);
  const result = {status:'identity_unavailable',display_name:'',role_evidence:'applicant_page'};
  if (/安全验证|账号异常|账户异常|操作过于频繁|访问过于频繁|滑动验证|请完成验证/.test(body))
    return {...result,status:'restricted'};
  if ([...document.querySelectorAll('.login-dialog,.sign-dialog,.btn-login,.user-nav a')]
      .some(n => visible(n) && /登录|注册/.test(n.innerText || '')))
    return {...result,status:'auth_required'};
  const names = [...document.querySelectorAll('.nav-figure .label-text,.nav-figure-con .label-text,.nav-figure-con .name,.user-nav .user-name,.nav-figure-con .geek-name')]
    .filter(visible).map(n => (n.innerText || '').trim()).filter(Boolean);
  const unique = [...new Set(names)];
  if (unique.length === 1 && /^[\p{L}\p{N}·._ -]{1,40}$/u.test(unique[0]))
    return {...result,status:'display_name_observed',display_name:unique[0]};
  return result;
})()
