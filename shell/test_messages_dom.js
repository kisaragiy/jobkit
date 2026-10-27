/* Execute the shipped selected-message and navigation scripts without any platform. */
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const read=fs.readFileSync(path.join(__dirname,'../applicant_messages_read.js'),'utf8');
const open=fs.readFileSync(path.join(__dirname,'../applicant_conversation_open.js'),'utf8');
const node=(text='',extra={})=>({innerText:text,getClientRects:()=>[{}],...extra});
function fixture({count=1,text='请问何时到岗？',kind='text',direction='incoming',id='mid-1',header='虚构HR',selected=1,duplicate=false}={}){
 let clicks=0;
 const control=node('',{click:()=>{clicks++;}});
 const row=node('',{classList:{contains:()=>false},getAttribute:()=>null,querySelector:selector=>({
  '.friend-content':control,'.friend-content.selected':selected?control:null,'.name-text':node('虚构HR'),'.name-box':node('虚构HR | 虚构公司')
 })[selector]||null});
 const items=Array.from({length:count},(_,i)=>node('',{
  classList:{contains:cls=>cls==='item-myself'?direction==='outgoing':cls==='item-friend'?direction==='incoming':false},
  getAttribute:()=>id===null?null:duplicate?id:count===1?id:'mid-'+i,
  querySelectorAll:selector=>selector==='.text .text-content'?(kind==='card'?[]:[node(text)]):[],
  querySelector:selector=>selector==='.item-time .time'?node('今天 10:30'):
   selector==='.message-card-wrap'?(kind==='card'?node():null):selector.startsWith('.text img')?(kind==='media'?node():null):null
 }));
 const messageList=node('',{querySelectorAll:()=>items});
 const pane=node('',{querySelectorAll:selector=>selector==='.top-info-content .name-text'?[node(header)]:selector==='ul.im-list'?[messageList]:[]});
 const rows=selected===2?[row,row]:[row];
 const document={querySelector:()=>({querySelectorAll:()=>rows}),querySelectorAll:selector=>selector==='.chat-conversation'?[pane]:[]};
 return {document,clicks:()=>clicks};
}
function messages(options={},status='observed_previews'){
 const f=fixture(options);return vm.runInNewContext(read,{document:f.document})('本人',name=>{assert.equal(name,'本人');return {status};});
}
const result=messages();assert.equal(result.status,'observed_messages');
assert.equal(result.messages[0].message_id,'mid-1');assert.equal(result.messages[0].direction,'incoming');
assert.equal(result.messages[0].text,'请问何时到岗？');assert.equal(result.messages[0].full_text_available,true);
assert.equal(result.party.platform_conversation_id,null);assert.equal(result.recipient_verified,false);assert.equal(result.history_complete,false);
assert.equal(messages({header:'另一个人'}).status,'recipient_reference_mismatch');
assert.equal(messages({selected:0}).status,'conversation_selection_required');
assert.equal(messages({selected:2}).status,'conversation_selection_required');
assert.equal(messages({},'session_changed').messages.length,0);
assert.equal(messages({id:null}).messages[0].message_id,null);
assert.equal(messages({kind:'media'}).messages[0].full_text_available,false);
assert.equal(messages({kind:'card'}).messages[0].kind,'card');
assert.equal(messages({direction:'unknown'}).messages[0].direction,'unknown');
assert.equal(messages({text:'x'.repeat(4001)}).messages[0].text.length,4000);
assert.equal(messages({text:'x'.repeat(4001)}).messages[0].text_truncated,true);
assert.equal(messages({text:'x'.repeat(4001)}).messages[0].full_text_available,false);
assert.equal(Array.from(messages({text:'😀'.repeat(4001)}).messages[0].text).length,4000);
assert.equal(messages({text:'😀'.repeat(4001)}).messages[0].text_truncated,true);
assert.equal(messages({count:105}).messages.length,100);assert.equal(messages({count:105}).messages_truncated,true);
assert.equal(messages({count:2,duplicate:true}).status,'schema_changed');
const row={display_name:'虚构HR',party_summary:'虚构HR | 虚构公司',last_message_preview:'原预览',preview_truncated:false,
 unread_marker:false,platform_conversation_id:null,identity_source:'unavailable'};
const snapshot={rows:[row],list_truncated:false,status:'observed_previews'};
function navigate(expected=snapshot,current=snapshot,index=0){const f=fixture();
 const value=vm.runInNewContext(open,{document:f.document})('本人',expected,index,()=>current);return {value,clicks:f.clicks()};}
assert.equal(navigate().value.navigation_requested,true);assert.equal(navigate().clicks,1);
assert.equal(navigate().value.recipient_verified,false);
assert.equal(navigate(snapshot,{...snapshot,rows:[{...row,last_message_preview:'新的预览'}]}).clicks,0);
assert.equal(navigate({...snapshot,rows:[row,row]},{...snapshot,rows:[row,row]}).value.status,'conversation_reference_ambiguous');
assert.equal(navigate({...snapshot,rows:[{...row,preview_truncated:true}]},{...snapshot,rows:[{...row,preview_truncated:true}]}).value.status,'conversation_reference_incomplete');
assert.equal(navigate(snapshot,snapshot,true).clicks,0);
const observeInbox=fs.readFileSync(path.join(__dirname,'../applicant_inbox_read.js'),'utf8');
const foreign=fixture(),foreignObserve=vm.runInNewContext(observeInbox,{document:foreign.document,location:{origin:'https://untrusted.invalid',pathname:'/web/geek/chat'}});
assert.equal(vm.runInNewContext(read,{document:foreign.document})('本人',foreignObserve).status,'session_changed');
assert.equal(vm.runInNewContext(open,{document:foreign.document})('本人',snapshot,0,foreignObserve).navigation_requested,false);
assert.equal(foreign.clicks(),0);
console.log('PASS: selected messages, direction, truncation, IDs and guarded read-only navigation.');
