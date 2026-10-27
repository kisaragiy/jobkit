/* Structural diagnostics only: no message text, input values, cookies or hidden state. */
(() => {
  const parents = node => {const result=[];for(let p=node.parentElement,i=0;p&&i<5;p=p.parentElement,i++)result.push({tag:p.tagName,classes:typeof p.className==='string'?p.className:'',children:[...p.children].slice(0,12).map(c=>typeof c.className==='string'?c.className:'')});return result;};
  const shape = node => ({ancestors:parents(node),tag:node.tagName,classes:typeof node.className==='string'?node.className:'',
    attributes:[...node.attributes].map(a=>a.name).filter(n=>n==='id'||n==='role'||n==='aria-selected'||n.startsWith('data-')),
    children:[...node.querySelectorAll('[class]')].slice(0,20).map(n=>({tag:n.tagName,classes:typeof n.className==='string'?n.className:''}))});
  const counts={};for(const node of document.querySelectorAll('[class]')){
    const name=typeof node.className==='string'?node.className:'';
    if(/user-list|im-list|chat-list|message|dialog|chat-window|chat-header|chat-info|item-myself|item-friend/.test(name))counts[name]=(counts[name]||0)+1;
  }
  return {page_path:location.pathname,viewport:{width:innerWidth,height:innerHeight,visibility:document.visibilityState},notices:{loading:!!document.querySelector('.pre-loading')?.getClientRects().length,verification:/安全验证|请完成验证|滑动验证/.test(document.body?.innerText||'')},rows:[...document.querySelectorAll('.user-list-content li')].slice(0,3).map(shape),
    messages:[...document.querySelectorAll('.top-info-content,.message-content,.chat-message,.im-list')].slice(0,4).map(shape).concat([...document.querySelectorAll('.im-list li')].slice(-3).map(shape)),classes:Object.entries(counts).slice(0,30)};
})()
