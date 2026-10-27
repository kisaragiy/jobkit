/* Current rendered message bodies only; no scrolling, input values or delivery actions. */
((expectedName, observeInbox) => {
  const base={status:'conversation_selection_required',party:null,messages:[],messages_truncated:false,
    history_complete:false,recipient_verified:false,automation_ready:false};
  const inbox=observeInbox(expectedName);
  if(inbox.status!=='observed_previews')return {...base,status:inbox.status};
  const visible=n=>!!n&&n.getClientRects().length>0;
  const list=document.querySelector('.user-list-content');
  const selected=[...list.querySelectorAll('li')].filter(row=>visible(row)&&
    (row.getAttribute('aria-selected')==='true'||row.classList.contains('selected')||visible(row.querySelector('.friend-content.selected'))));
  if(selected.length!==1)return base;
  const row=selected[0],read=(root,selector,max)=>Array.from((root.querySelector(selector)?.innerText||'').trim()).slice(0,max).join('');
  const panes=[...document.querySelectorAll('.chat-conversation')].filter(visible);
  if(panes.length!==1)return {...base,status:'pane_not_ready'};
  const pane=panes[0],headers=[...pane.querySelectorAll('.top-info-content .name-text')].filter(visible);
  const lists=[...pane.querySelectorAll('ul.im-list')].filter(visible);
  if(headers.length!==1||lists.length!==1)return {...base,status:'pane_not_ready'};
  const name=read(row,'.name-text',80),header=Array.from((headers[0].innerText||'').trim()).slice(0,80).join('');
  if(!name||name!==header)return {...base,status:'recipient_reference_mismatch'};
  const rawConversation=row.getAttribute('data-conversation-id');
  const conversationId=rawConversation&&/^[A-Za-z0-9_~\-]{1,128}$/.test(rawConversation)?rawConversation:null;
  const party={display_name:name,party_summary:read(row,'.name-box',200),header_display_name:header,
    platform_conversation_id:conversationId,identity_source:conversationId?'dom_data_conversation_id':'unavailable'};
  const rendered=[...lists[0].querySelectorAll('li.message-item')].filter(visible);
  if(!rendered.length)return {...base,status:'empty_or_loading',party};
  const messages=[],seen=new Set();
  for(const item of rendered.slice(-100)){
    const rawId=item.getAttribute('data-mid'),id=rawId&&/^[A-Za-z0-9_~.\-]{1,128}$/.test(rawId)?rawId:null;
    if(id&&seen.has(id))return {...base,status:'schema_changed'};
    if(id)seen.add(id);
    const own=item.classList.contains('item-myself'),friend=item.classList.contains('item-friend');
    const direction=own&&!friend?'outgoing':friend&&!own?'incoming':'unknown';
    const spans=[...item.querySelectorAll('.text .text-content')].filter(visible);
    const body=spans.length===1?(spans[0].innerText||'').trim():'';
    const characters=Array.from(body);
    const media=!!item.querySelector('.text img,.text video,.text audio,.text iframe');
    const card=!!item.querySelector('.message-card-wrap');
    const kind=card?'card':media?'media':spans.length===1&&body?'text':'unknown';
    messages.push({message_id:id,message_id_source:id?'dom_data_mid':'unavailable',direction,kind,
      text:characters.slice(0,4000).join(''),text_truncated:characters.length>4000,
      full_text_available:kind==='text'&&characters.length<=4000,
      timestamp_label:read(item,'.item-time .time',80)});
  }
  return {...base,status:'observed_messages',party,messages,messages_truncated:rendered.length>100};
})
