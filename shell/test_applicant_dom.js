/* Fixed DOM reader exercised against the observed card structure, no platform writes. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const script = fs.readFileSync(path.join(__dirname, '../applicant_dom_read.js'), 'utf8');
const node = (text, extra={}) => ({innerText:text, getClientRects:()=>[{}], ...extra});
function read({body='', login=false, empty=false, id='fictional~', duplicate=false, salary='\ue039-\ue032K'}={}) {
  const fields={'.job-name':node('Agent 开发'), '.boss-name':node('虚构公司'),
    '.company-location':node('广州·天河区'), '.job-salary':node(salary)};
  const anchor=node('', {getAttribute:()=>`/job_detail/${id}.html?security=not-an-identity`});
  const card=node('', {querySelector:s=>s.startsWith('a[')?anchor:fields[s]||null,
    querySelectorAll:s=>s.includes('.tag-list')?[node('本科'),node('1年以内')]:[]});
  const document={body:{innerText:body}, querySelector:()=>empty?node(''):null,
    querySelectorAll:s=>s.startsWith('.login-dialog')?(login?[node('登录')]:[]):
      s==='li.job-card-box,.job-card-wrapper'?(empty?[]:duplicate?[card,card]:[card]):[]};
  return JSON.parse(JSON.stringify(vm.runInNewContext(script,{document,URL,
    location:{href:'https://www.zhipin.com/web/geek/jobs',pathname:'/web/geek/jobs'}})));
}
const actual = read();
assert.equal(actual.status,'observed');
assert.deepEqual(actual.jobs[0],{job_id:'fictional~',title:'Agent 开发',company:'虚构公司',
  city:'广州',salary:'\ue039-\ue032K',education:'本科',experience:'1年以内'});
assert.equal(read({login:true}).status,'auth_required');
assert.equal(read({body:'请完成验证'}).status,'restricted');
assert.equal(read({empty:true,body:'暂无相关职位'}).status,'empty');
assert.equal(read({empty:true}).status,'schema_changed');
assert.equal(read({duplicate:true}).status,'schema_changed');
assert.equal(read({id:'bad/path'}).status,'schema_changed');
console.log('PASS: applicant DOM cards, opaque salaries, identity and stop states.');

const identityScript = fs.readFileSync(path.join(__dirname, '../applicant_identity_read.js'), 'utf8');
function identity(names, body='', login=false) {
  return vm.runInNewContext(identityScript,{document:{body:{innerText:body},
    querySelectorAll:s=>s.startsWith('.login-dialog')?(login?[node('登录')]:[]):names.map(n=>node(n))}});
}
assert.equal(identity(['虚构姓名']).status,'display_name_observed');
assert.equal(identity(['一个姓名','另一个姓名']).status,'identity_unavailable');
assert.equal(identity([]).status,'identity_unavailable');
assert.equal(identity(['stale'],'请完成验证').display_name,'');
assert.equal(identity(['stale'],'',true).status,'auth_required');
assert.equal(identity(['<script>']).status,'identity_unavailable');
console.log('PASS: account display name, ambiguity, restriction and login states.');

const salaryScript = fs.readFileSync(path.join(__dirname, '../applicant_salary_regions.js'), 'utf8');
function salaryRegions({hidden=false, fonts='loaded', left=200, duplicate=false, body=''}={}) {
  const region=node('\ue032-\ue033K',{getBoundingClientRect:()=>({left,top:200,right:left+80,bottom:224,width:80,height:24})});
  const anchor=node('',{getAttribute:()=>'/job_detail/fictional~.html?security=not-an-identity'});
  const card=node('',{querySelector:s=>s.startsWith('a[')?anchor:region});
  return vm.runInNewContext(salaryScript,{URL,innerWidth:1280,innerHeight:800,scrollX:0,scrollY:0,
    location:{href:'https://www.zhipin.com/web/geek/jobs'},document:{body:{innerText:body},
      fonts:{status:fonts},visibilityState:hidden?'hidden':'visible',
      querySelectorAll:s=>s.startsWith('.login-dialog')?[]:duplicate?[card,card]:[card]}});
}
assert.equal(salaryRegions().regions[0].job_id,'fictional~');
assert.equal(salaryRegions().regions[0].rect.width,80);
assert.equal(salaryRegions({hidden:true}).status,'browser_not_visible');
assert.equal(salaryRegions({fonts:'loading'}).status,'fonts_loading');
assert.equal(salaryRegions({left:-1}).status,'no_visible_salary');
assert.equal(salaryRegions({duplicate:true}).status,'schema_changed');
assert.equal(salaryRegions({body:'请完成验证'}).status,'restricted');
console.log('PASS: salary-only visible rectangles, fonts and hidden-window stops.');

const inboxScript=fs.readFileSync(path.join(__dirname,'../applicant_inbox_read.js'),'utf8');
function inbox({name='虚构姓名',expected='虚构姓名',count=1,id=null,message='请问何时到岗？',body='',origin='https://www.zhipin.com'}={}){
  const row=node('',{getAttribute:key=>key==='data-conversation-id'?id:null,querySelector:selector=>({
    '.name-text':node('虚构HR'),'.name-box':node('虚构HR · 虚构公司'),'.last-msg-text':node(message),'.notice-badge':node('1')
  })[selector]||null});
  const document={body:{innerText:body},querySelector:selector=>selector==='.user-list-content'?{querySelectorAll:()=>Array(count).fill(row)}:null,
    querySelectorAll:selector=>selector.startsWith('.login-dialog')?[]:[node(name)]};
  return vm.runInNewContext(inboxScript,{document,location:{origin,pathname:'/web/geek/chat'}})(expected);
}
assert.equal(inbox().status,'observed_previews');
assert.equal(inbox().rows[0].platform_conversation_id,null);
assert.equal(inbox().rows[0].message_id_available,false);
assert.equal(inbox().automation_ready,false);
assert.equal(inbox({name:'另一账号'}).status,'session_changed');
assert.equal(inbox({name:'另一账号'}).rows.length,0);
assert.equal(inbox({message:'x'.repeat(501)}).rows[0].preview_truncated,true);
assert.equal(inbox({message:'x'.repeat(501)}).rows[0].last_message_preview.length,500);
assert.equal(inbox({count:35}).rows.length,30);assert.equal(inbox({count:35}).list_truncated,true);
assert.equal(inbox({count:2,id:'duplicate'}).status,'schema_changed');
assert.equal(inbox({id:'observed-dom-id'}).rows[0].identity_source,'dom_data_conversation_id');
assert.equal(inbox({count:0}).status,'empty_or_loading');
assert.equal(inbox({body:'请完成验证'}).status,'restricted');
assert.equal(inbox({origin:'https://untrusted.invalid'}).status,'session_changed');
assert.equal(inbox({origin:'https://untrusted.invalid'}).rows.length,0);
console.log('PASS: bound inbox previews, no invented IDs, truncation and changed accounts.');
