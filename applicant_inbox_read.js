/* Visible inbox previews only. No selection, full-message claims or generated identities. */
((expectedDisplayName) => {
  const visible=n=>!!n&&n.getClientRects().length>0;
  const base={status:'identity_unavailable',display_name:'',rows:[],automation_ready:false,list_truncated:false};
  if(location.origin!=='https://www.zhipin.com'||location.pathname!=='/web/geek/chat')
    return {...base,status:'session_changed'};
  const body=(document.body?.innerText||'').slice(0,100000);
  if(/安全验证|账号异常|账户异常|操作过于频繁|访问过于频繁|滑动验证|请完成验证/.test(body))return {...base,status:'restricted'};
  if([...document.querySelectorAll('.login-dialog,.sign-dialog,.btn-login,.user-nav a')].some(n=>visible(n)&&/登录|注册/.test(n.innerText||'')))return {...base,status:'auth_required'};
  const names=[...new Set([...document.querySelectorAll('.nav-figure .label-text,.nav-figure-con .label-text,.nav-figure-con .name,.user-nav .user-name,.nav-figure-con .geek-name')].filter(visible).map(n=>(n.innerText||'').trim()).filter(Boolean))];
  if(names.length!==1)return base;
  if(names[0]!==expectedDisplayName)return {...base,status:'session_changed'};
  const list=document.querySelector('.user-list-content');
  if(!list)return {...base,status:'schema_changed',display_name:names[0]};
  const rows=[],seen=new Set();
  const rendered=[...list.querySelectorAll('li')].filter(visible);
  for(const row of rendered.slice(0,30)){
    const read=(selector,max)=>(row.querySelector(selector)?.innerText||'').trim().slice(0,max);
    const raw=(row.querySelector('.last-msg-text')?.innerText||'').trim();
    const rawId=row.getAttribute('data-conversation-id');
    const id=rawId&&/^[A-Za-z0-9_~\-]{1,128}$/.test(rawId)?rawId:null;
    if(id&&seen.has(id))return {...base,status:'schema_changed',display_name:names[0]};
    if(id)seen.add(id);
    rows.push({display_name:read('.name-text',80),party_summary:read('.name-box',200),
      last_message_preview:raw.slice(0,500),preview_truncated:raw.length>500,
      unread_marker:visible(row.querySelector('.notice-badge')),
      platform_conversation_id:id,identity_source:id?'dom_data_conversation_id':'unavailable',
      full_message_available:false,message_id_available:false});
  }
  if(!rows.length)return {...base,status:'empty_or_loading',display_name:names[0]};
  return {...base,status:'observed_previews',display_name:names[0],rows,list_truncated:rendered.length>30};
})
