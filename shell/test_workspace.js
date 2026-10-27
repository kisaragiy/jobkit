/* Exercise the shipped page handlers with a fake DOM and clipboard, no browser/platform. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../web/workspace.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1]
  .replace(/load\(\)\.catch\(handleError\);\s*$/, '');

function fixture(respond) {
  const nodes = new Map(), copied = [], blobs = [], revoked = [], downloads = [];
  let responseNumber=0;
  function node(id) {
    if (!nodes.has(id)) nodes.set(id, {
      value: '', textContent: '', dataset: {}, disabled: false,
      click(){downloads.push({href:this.href,download:this.download});},removeAttribute(name){delete this[name];}, children: [], replaceChildren() { this.children = []; }, append(child) { this.children.push(child); }, closest() { return {}; }
    });
    return nodes.get(id);
  }
  const ctx = vm.createContext({
    document: { getElementById: node, createElement: () => node(Symbol()) },
    fetch: async (url, options) => ({ ok: true, json: async () => {const data=await respond(url, options);return {ok:true,trace_id:(++responseNumber).toString(16).padStart(32,'0'),data:url.endsWith('/tasks/search')?{stage_counts:{queued:0,drafted:0,needs_review:0,attention:0,running:0,history:0},...(Array.isArray(data)?{tasks:data,total:data.length,next_cursor:null}:data)}:data};} }),
    navigator: { clipboard: { writeText: async text => copied.push(text) } },
    crypto: { randomUUID: () => 'test-key' }, Blob, URL:{createObjectURL:blob=>{blobs.push(blob);return 'blob:local-'+blobs.length;},revokeObjectURL:url=>revoked.push(url)}, console
  });
  vm.runInContext(script, ctx);
  vm.runInContext("context={context_token:'scope-a',role:'recruiter'}; revision=1; draftSnapshot={token:'scope-a',task_id:'task-one',revision:1,text:'approved draft'};", ctx);
  return { node, ctx, copied, blobs, revoked, downloads };
}

(async () => {
  const stale = fixture(async () => ({ document_revision: 2, text:'approved draft' }));
  await stale.node('copy').onclick();
  assert.equal(stale.copied.length, 0, 'updated facts must prevent clipboard writes');
  assert.equal(stale.node('copy').disabled, true);
  assert.match(stale.node('notice').textContent, /资料已更新/);

  const valid = fixture(async url => { assert.ok(url.endsWith('/tasks/task-one/copy')); return {document_revision:1,text:'approved draft'}; });
  await valid.node('copy').onclick();
  assert.deepEqual(valid.copied, ['approved draft']);
  assert.match(valid.node('notice').textContent, /尚未在平台发送/);

  let release;
  const delayed = fixture(async url => {
    if (url.endsWith('/tasks')) return { task_id: 'task-1' };
    return await new Promise(resolve => { release = resolve; });
  });
  delayed.node('message').value = '薪资多少？';
  const generating = delayed.node('generate').onclick();
  // Allow submit's async JSON adapter to resolve, without timers or real IO.
  for (let i = 0; i < 20 && !release; i++) await Promise.resolve();
  assert.equal(typeof release, 'function');
  vm.runInContext('revision=2;', delayed.ctx);
  release({ status: 'completed', result: { text: 'old salary', document_revision: 1 } });
  await generating;
  assert.equal(delayed.node('copy').disabled, true);
  assert.doesNotMatch(delayed.node('reply').textContent, /old salary/);
  assert.match(delayed.node('reply').textContent, /资料已更新/);
  let recovered = false;
  const recovery = fixture(async url => {
    if (url.endsWith('/recover')) { recovered = true; return {status:'failed',error_code:'INTERRUPTED'}; }
    return [{task_id:'abandoned',status:recovered?'failed':'executing',error_code:recovered?'INTERRUPTED':null,created_at:1}];
  });
  await vm.runInContext('loadTasks()', recovery.ctx);
  const taskNode = recovery.node('tasks').children[0];
  const recoverButton = taskNode.children.find(child => child.textContent === '核对中断任务');
  assert.ok(recoverButton, 'executing task has a recovery action');
  await recoverButton.onclick();
  assert.equal(recovered, true);
  assert.match(recovery.node('notice').textContent, /没有重新执行/);
  assert.ok(recovery.node('tasks').children[0].children.some(child => /上次处理已中断/.test(child.textContent)));
  assert.ok(!recovery.node('tasks').children[0].children.some(child => child.textContent === '核对中断任务'));
  let controlRecovered = false;
  const controls = fixture(async url => {
    if (url.endsWith('/recover')) { controlRecovered = true; return {status:'completed',error_code:'RECOVERED_COMMIT'}; }
    return controlRecovered ? [] : [{task_id:'control-one',tool:'runtime.document.save',status:'executing'}];
  });
  await vm.runInContext('loadControls()', controls.ctx);
  assert.equal(controls.node('controlPanel').hidden, false);
  const controlButton = controls.node('controlTasks').children[0].children.find(child => child.textContent === '核对提交结果');
  await controlButton.onclick();
  assert.equal(controlRecovered, true);
  assert.match(controls.node('notice').textContent, /已提交，没有重新执行/);
  assert.equal(controls.node('controlPanel').hidden, true);
  const lateControls = fixture(async () => { vm.runInContext("context={context_token:'another-scope'};", lateControls.ctx); return [{task_id:'private-control',tool:'runtime.document.save',status:'executing'}]; });
  lateControls.node('controlPanel').hidden = true;
  await vm.runInContext('loadControls()', lateControls.ctx);
  assert.equal(lateControls.node('controlPanel').hidden, true);
  assert.equal(lateControls.node('controlTasks').children.length, 0);
  let closedQueued = false;
  const queued = fixture(async url => {
    if (url.endsWith('/recover')) { closedQueued = true; return {status:'cancelled',error_code:'NOT_STARTED'}; }
    return closedQueued ? [] : [{task_id:'queued-control',tool:'runtime.document.save',status:'queued'}];
  });
  await vm.runInContext('loadControls()', queued.ctx);
  const closeButton = queued.node('controlTasks').children[0].children.find(child => child.textContent === '结束未开始的请求');
  assert.ok(closeButton);
  await closeButton.onclick();
  assert.match(queued.node('notice').textContent, /没有执行原操作/);
  assert.equal(queued.node('controlPanel').hidden, true);
  const parser = fixture(async () => []);
  assert.deepEqual(JSON.parse(JSON.stringify(vm.runInContext("parseManualBatch('A | 地点？\\nB | 薪资 | 班次？')", parser.ctx))), [
    {conversation_note:'A',message:'地点？'}, {conversation_note:'B',message:'薪资 | 班次？'}
  ]);
  assert.throws(() => vm.runInContext("parseManualBatch('missing separator')", parser.ctx), /第 1 行/);

  const batchKeys = [];
  const importer = fixture(async (url, options) => {
    if (url.endsWith('/tasks/batch')) {
      batchKeys.push(JSON.parse(options.body).idempotency_key);
      if (batchKeys.length === 1) throw new Error('simulated lost response');
      return {tasks:[],created:0,reused:1};
    }
    return [];
  });
  importer.node('batchMessages').value = 'A | 地点？';
  await importer.node('batchForm').onsubmit({preventDefault(){}});
  assert.equal(importer.node('batchMessages').value,'A | 地点？');
  await importer.node('batchForm').onsubmit({preventDefault(){}});
  assert.equal(batchKeys.length,2);
  assert.equal(batchKeys[0],batchKeys[1], 'network retry preserves batch identity');
  assert.match(importer.node('notice').textContent,/复用 1 条/);

  const rowCopy = fixture(async url => url.endsWith('/copy') ? {document_revision:2,text:'old office'} : [
    {task_id:'row-copy',status:'completed',message:'地点？',conversation_note:'A',review_status:'pending',review_revision:0,created_at:1,result:{text:'old office',document_revision:1}}
  ]);
  await vm.runInContext('loadTasks()',rowCopy.ctx);
  const rowCopyButton = rowCopy.node('tasks').children[0].children.find(child => child.textContent==='复制此草稿');
  await rowCopyButton.onclick();
  assert.equal(rowCopy.copied.length,0);
  assert.equal(rowCopyButton.disabled,true);

  let releaseOld;
  const loadFence = fixture(async () => await new Promise(resolve => {releaseOld=resolve;}));
  const loading = vm.runInContext('loadTasks()',loadFence.ctx);
  for(let i=0;i<20&&!releaseOld;i++) await Promise.resolve();
  assert.equal(typeof releaseOld,'function');
  vm.runInContext("clearPrivate();context={context_token:'scope-a',role:'recruiter'};",loadFence.ctx);
  releaseOld([{task_id:'old',status:'completed',message:'private old message',created_at:1}]);
  await loading;
  assert.equal(loadFence.node('tasks').children.length,0,'reload invalidates old list responses');
  let replacement;
  const staleRow = fixture(async (url,options) => {
    if(url.endsWith('/replace')) {replacement=JSON.parse(options.body);return {task_id:'new'};}
    return [{task_id:'stale',status:'completed',message:'地点？',conversation_note:'A',review_status:'pending',review_revision:0,
      draft_stale:true,current_document_revision:2,created_at:1,result:{text:'old office',document_revision:1}}];
  });
  await vm.runInContext('loadTasks()',staleRow.ctx);
  const peerCard = staleRow.node('tasks').children[0];
  assert.equal(peerCard.children.find(n=>n.textContent==='按当前资料重建草稿任务').disabled,true,'unseen peer facts require workspace reload');
  vm.runInContext('revision=2;',staleRow.ctx);
  await vm.runInContext('loadTasks()',staleRow.ctx);
  const staleCard = staleRow.node('tasks').children[0];
  assert.equal(staleCard.children.find(n=>n.textContent==='复制此草稿').disabled,true);
  assert.equal(staleCard.children.find(n=>n.textContent==='按当前资料重建草稿任务').disabled,false);
  await staleCard.children.find(n=>n.textContent==='按当前资料重建草稿任务').onclick();
  assert.deepEqual(replacement,{expected_review_revision:0,expected_document_revision:2});
  let posts = 0, batchPayload;
  const queuedRows = Array.from({length:12},(_,i)=>({task_id:'q'+i,status:'queued',review_status:'pending',
    document_revision:1,current_document_revision:1,input_state:'readable',created_at:1}));
  const bulk = fixture(async(url,options)=>{
    if(url.endsWith('/tasks/batch/execute')){posts++;batchPayload=JSON.parse(options.body);return {drafted:8,needs_review:1,failed:1,remaining_task_ids:[],stopped_code:'TOOL_FAILED'};}
    return queuedRows;
  });
  await vm.runInContext('loadTasks()',bulk.ctx);
  assert.equal(bulk.node('draftBatch').disabled,true);
  await bulk.node('selectBatch').onclick();
  assert.match(bulk.node('batchSelection').textContent,/已选 10 条/);
  const eleventh=bulk.node('tasks').children[10].children.find(n=>n.children?.some(c=>c.type==='checkbox')).children[0];
  eleventh.checked=true;eleventh.onchange();
  assert.equal(eleventh.checked,false,'manual checkbox cannot exceed ten');
  await bulk.node('draftBatch').onclick();
  assert.equal(posts,1,'one batch POST, no renderer execution loop');
  assert.deepEqual(batchPayload,{task_ids:queuedRows.slice(0,10).map(t=>t.task_id),expected_document_revision:1});
  assert.match(bulk.node('notice').textContent,/8 条草稿，1 条需人工处理，1 条未完成/);
  assert.match(bulk.node('notice').textContent,/已停止/);
  assert.match(bulk.node('batchOutcome').textContent,/8 条草稿/,'result stays beside the records');
  assert.equal(bulk.node('draftBatch').disabled,true,'selection cleared after result');
  let resolveBatch;
  const fencedBatch = fixture(async(url)=>url.endsWith('/tasks/batch/execute')?await new Promise(r=>{resolveBatch=r;}):queuedRows);
  await vm.runInContext('loadTasks()',fencedBatch.ctx);
  await fencedBatch.node('selectBatch').onclick();
  const batchPending=fencedBatch.node('draftBatch').onclick();
  for(let i=0;i<20&&!resolveBatch;i++)await Promise.resolve();
  assert.equal(fencedBatch.node('selectBatch').disabled,true);
  assert.equal(fencedBatch.node('clearBatch').disabled,true);
  await fencedBatch.node('draftBatch').onclick();
  vm.runInContext('clearPrivate();',fencedBatch.ctx);
  resolveBatch({drafted:10,needs_review:0,failed:0,remaining_task_ids:[],stopped_code:null});
  await batchPending;
  assert.doesNotMatch(fencedBatch.node('notice').textContent,/本批/,'old batch response fenced after reload');
  const staleBulk=fixture(async()=>queuedRows.map(t=>({...t,current_document_revision:2,draft_stale:true})));
  await vm.runInContext('loadTasks()',staleBulk.ctx);
  assert.equal(staleBulk.node('selectBatch').disabled,true,'stale facts cannot be selected');
  let lostPosts=0;
  const lostBatch=fixture(async url=>{
    if(url.endsWith('/tasks/batch/execute')){lostPosts++;throw new Error('lost response');}
    return url.endsWith('/recovery')?[]:queuedRows;
  });
  await vm.runInContext('loadTasks()',lostBatch.ctx);
  await lostBatch.node('selectBatch').onclick();
  await lostBatch.node('draftBatch').onclick();
  assert.equal(lostPosts,1,'lost response is never retried automatically');
  assert.equal(lostBatch.node('draftBatch').disabled,true);
  assert.match(lostBatch.node('batchOutcome').textContent,/未能完整确认.*不会自动继续/);
  const manualRevisionFence=fixture(async()=>({document_revision:1,text:'approved draft',manual_revision:2}));
  await manualRevisionFence.node('copy').onclick();
  assert.equal(manualRevisionFence.copied.length,0,'same text with a newer manual revision is not an old copy proof');
  let savedManual, manualText=null, manualVersion=0;
  const manualRow=()=>({task_id:'editable',status:'completed',review_status:'pending',review_revision:0,
    document_revision:1,current_document_revision:1,input_state:'readable',created_at:1,
    result:{text:'original text',document_revision:1},manual_text:manualText,manual_revision:manualVersion});
  const manualUi=fixture(async(url,options)=>{
    if(url.endsWith('/edit')){savedManual=JSON.parse(options.body);manualText=savedManual.text;manualVersion=1;return manualRow();}
    if(url.endsWith('/edits'))return {total:1,edits:[{revision:1,document_revision:1,text:manualText,created_at:1}]};
    if(url.endsWith('/copy'))return {text:manualText,manual_revision:1,document_revision:1};
    return [manualRow()];
  });
  await vm.runInContext('loadTasks()',manualUi.ctx);
  let editable=manualUi.node('tasks').children[0];
  const editorDetails=editable.children.find(n=>n.children?.some(c=>c.textContent==='人工调整草稿'));
  const textarea=editorDetails.children.find(n=>n.textContent==='修改后的回复').children[0];
  const confirmation=editorDetails.children.find(n=>n.textContent==='我已核对修改内容，确认用于人工回复').children[0];
  const saveManual=editorDetails.children.find(n=>n.textContent==='保存已核对的人工稿');
  assert.equal(saveManual.disabled,true);
  textarea.value='您好，已核对的人工回复。';textarea.oninput();
  assert.equal(editable.children.find(n=>n.textContent==='复制此草稿').disabled,true,'unsaved edit disables card copying');
  await saveManual.onclick();
  assert.equal(savedManual,undefined,'confirmation is required even if handler is invoked directly');
  confirmation.checked=true;confirmation.onchange();
  assert.equal(saveManual.disabled,false);
  await saveManual.onclick();
  assert.deepEqual(savedManual,{text:'您好，已核对的人工回复。',confirmed:true,expected_manual_revision:0,expected_document_revision:1});
  editable=manualUi.node('tasks').children[0];
  assert.ok(editable.children.some(n=>n.textContent==='系统原稿（保留）'));
  assert.ok(editable.children.some(n=>n.textContent==='original text'));
  assert.ok(editable.children.some(n=>n.textContent===manualText));
  await editable.children.find(n=>n.textContent==='复制已确认人工稿').onclick();
  assert.deepEqual(manualUi.copied,[manualText]);
  const historyDetails=editable.children.find(n=>n.children?.some(c=>c.textContent==='人工修改记录'));
  await historyDetails.children.find(n=>n.textContent==='查看最近 20 次修改').onclick();
  assert.ok(historyDetails.children.some(n=>n.children?.some(c=>c.textContent.includes('人工稿版本 1'))));
  const oldManual=fixture(async()=>[{...manualRow(),draft_stale:true,current_document_revision:2}]);
  await vm.runInContext('loadTasks()',oldManual.ctx);
  assert.equal(oldManual.node('tasks').children[0].children.find(n=>n.textContent==='复制已确认人工稿').disabled,true);
  assert.ok(!oldManual.node('tasks').children[0].children.some(n=>n.children?.some(c=>c.textContent==='人工调整草稿')));
  const searchRequests=[];
  const pages=fixture(async(url,options)=>{
    assert.ok(url.endsWith('/tasks/search'),'search uses POST body rather than a query URL');
    const request=JSON.parse(options.body);searchRequests.push(request);
    return request.cursor?{tasks:[{...queuedRows[0],task_id:'older'}],total:65,next_cursor:null}:
      {tasks:queuedRows,total:65,next_cursor:'page-two'};
  });
  await vm.runInContext('loadTasks()',pages.ctx);
  assert.equal(pages.node('previousTasks').disabled,true);
  assert.equal(pages.node('nextTasks').disabled,false);
  await pages.node('selectBatch').onclick();
  await pages.node('nextTasks').onclick();
  assert.equal(searchRequests.at(-1).cursor,'page-two');
  assert.match(pages.node('taskPageInfo').textContent,/第 2 页.*当前匹配 65 条/);
  assert.match(pages.node('batchSelection').textContent,/已选 0 条/);
  assert.equal(pages.node('nextTasks').disabled,true);
  await pages.node('previousTasks').onclick();
  assert.equal(searchRequests.at(-1).cursor,null);
  pages.node('taskSearch').value=' 候选人 A ';
  await pages.node('taskSearchForm').onsubmit({preventDefault(){}});
  assert.deepEqual(searchRequests.at(-1),{review:'pending',query:'候选人 A',cursor:null,stage:'all'});
  await pages.node('nextTasks').onclick();
  await pages.node('clearSearch').onclick();
  assert.equal(searchRequests.at(-1).query,'');
  assert.equal(searchRequests.at(-1).cursor,null);
  assert.equal(pages.node('taskSearch').value,'');
  let resolveSearchA,resolveSearchB;
  const searchRace=fixture(async(url,options)=>await new Promise(resolve=>{
    if(JSON.parse(options.body).query==='A')resolveSearchA=resolve;else resolveSearchB=resolve;
  }));
  searchRace.node('taskSearch').value='A';
  const searchA=searchRace.node('taskSearchForm').onsubmit({preventDefault(){}});
  searchRace.node('taskSearch').value='B';
  const searchB=searchRace.node('taskSearchForm').onsubmit({preventDefault(){}});
  for(let i=0;i<20&&(!resolveSearchA||!resolveSearchB);i++)await Promise.resolve();
  resolveSearchB({tasks:[{...queuedRows[0],conversation_note:'new B'}],total:1,next_cursor:null});
  await searchB;
  resolveSearchA({tasks:[{...queuedRows[0],conversation_note:'old A'}],total:100,next_cursor:'old-cursor'});
  await searchA;
  assert.match(searchRace.node('taskPageInfo').textContent,/当前匹配 1 条.*关键词：B/);
  assert.equal(searchRace.node('nextTasks').disabled,true);
  assert.ok(searchRace.node('tasks').children[0].children.some(n=>n.textContent.startsWith('new B')));
  vm.runInContext('clearPrivate();',searchRace.ctx);
  assert.equal(searchRace.node('taskSearch').value,'');
  assert.equal(searchRace.node('previousTasks').disabled,true);
  let triagePayload;
  const triageUi=fixture(async(url,options)=>{
    triagePayload=JSON.parse(options.body);
    return {tasks:[{task_id:'human-only',status:'completed',review_status:'pending',queue_stage:'needs_review',
      document_revision:1,current_document_revision:1,created_at:1,result:{text:'',reason:'需要本人核对承诺',document_revision:1}}],
      total:1,next_cursor:null,stage_counts:{queued:2,drafted:3,needs_review:1,attention:1,running:0,history:0}};
  });
  vm.runInContext("taskPage=1;taskCursors=[null,'old-page'];taskQuery='承诺';selectedDrafts.add('old-selection');",triageUi.ctx);
  triageUi.node('stageFilter').value='needs_review';
  await triageUi.node('stageFilter').onchange();
  assert.deepEqual(triagePayload,{review:'pending',query:'承诺',cursor:null,stage:'needs_review'});
  assert.match(triageUi.node('queueSummary').textContent,/有文字草稿 3 条.*需人工答复 1 条/);
  assert.match(triageUi.node('batchSelection').textContent,/已选 0 条/);
  const humanCard=triageUi.node('tasks').children[0];
  assert.ok(humanCard.children.some(n=>n.textContent.includes('需人工答复 · 待处理')),'completed work needs a business label');
  assert.ok(!humanCard.children.some(n=>n.textContent==='复制此草稿'));
  assert.ok(!humanCard.children.some(n=>n.children?.some(c=>c.textContent==='人工调整草稿')));
  vm.runInContext('clearPrivate();',triageUi.ctx);
  assert.equal(triageUi.node('stageFilter').value,'all');
  assert.equal(triageUi.node('queueSummary').textContent,'');
  const brokenResult=fixture(async()=>({tasks:[{task_id:'broken',status:'completed',review_status:'pending',queue_stage:'attention',
    result:null,result_state:'unreadable',created_at:1}],total:1,next_cursor:null}));
  await vm.runInContext('loadTasks()',brokenResult.ctx);
  assert.ok(brokenResult.node('tasks').children[0].children.some(n=>n.textContent.includes('保存的草稿结果无法读取')));
  const exportSnapshot={format:'jobkit.workspace-records',format_version:1,created_at:'2026-10-26T00:00:00Z',
    workspace:{scope_id:'scope-a'},document:{revision:1,content:{location:'虚构地点'}},tasks:[{message:'地点在哪里？'}],
    draft_edits:[{text:'人工稿'}],counts:{tasks:1,draft_edits:1},restore_supported:false,platform_send_evidence:false};
  let exportCalls=0;
  const exporter=fixture(async(url,options)=>{
    exportCalls++;assert.ok(url.endsWith('/workspace/export'));assert.deepEqual(JSON.parse(options.body),{});return exportSnapshot;
  });
  vm.runInContext("context.scope_id='scope-a';",exporter.ctx);
  await exporter.node('prepareExport').onclick();
  assert.equal(exportCalls,1);
  assert.equal(exporter.blobs.length,1);
  assert.equal(exporter.node('downloadExport').hidden,false);
  assert.match(exporter.node('downloadExport').download,/^jobkit-records-.*\.json$/);
  assert.match(exporter.node('exportStatus').textContent,/1 条消息、1 次人工修改.*不支持导入恢复/);
  assert.deepEqual(JSON.parse(await exporter.blobs[0].text()),exportSnapshot);
  const combinedExport=fixture(async()=>({...exportSnapshot,format_version:2,origins:[]}));
  vm.runInContext("context.scope_id='scope-a';",combinedExport.ctx);
  await combinedExport.node('prepareExport').onclick();
  assert.equal(combinedExport.blobs.length,1,'download adapter accepts the backend v2 envelope');
  assert.equal(JSON.parse(await combinedExport.blobs[0].text()).format_version,2);
  await exporter.node('prepareExport').onclick();
  assert.deepEqual(exporter.revoked,['blob:local-1'],'preparing a new file revokes the previous link');
  vm.runInContext('clearPrivate();',exporter.ctx);
  assert.deepEqual(exporter.revoked,['blob:local-1','blob:local-2']);
  assert.equal(exporter.node('downloadExport').hidden,true);
  assert.equal(exporter.node('downloadExport').href,undefined);
  let finishExport,pendingCalls=0;
  const lateExport=fixture(async()=>{pendingCalls++;return await new Promise(resolve=>{finishExport=resolve;});});
  vm.runInContext("context.scope_id='scope-a';",lateExport.ctx);
  const exporting=lateExport.node('prepareExport').onclick();
  for(let i=0;i<20&&!finishExport;i++)await Promise.resolve();
  await lateExport.node('prepareExport').onclick();
  assert.equal(pendingCalls,1,'repeated clicks do not create parallel exports');
  vm.runInContext("clearPrivate();context={context_token:'scope-a',scope_id:'scope-a'};",lateExport.ctx);
  finishExport(exportSnapshot);await exporting;
  assert.equal(lateExport.blobs.length,0,'even same-token reload fences a late export');
  const wrongExport=fixture(async()=>({...exportSnapshot,workspace:{scope_id:'other'}}));
  vm.runInContext("context.scope_id='scope-a';",wrongExport.ctx);
  await wrongExport.node('prepareExport').onclick();
  assert.equal(wrongExport.blobs.length,0);
  assert.match(wrongExport.node('exportStatus').textContent,/未生成/);
  const saver=fixture(async url=>url.endsWith('/workspace/export')?exportSnapshot:{revision:1,content:{}});
  vm.runInContext("context.scope_id='scope-a';",saver.ctx);
  await saver.node('prepareExport').onclick();
  let prevented=0;
  await saver.node('downloadExport').onclick({preventDefault(){prevented++;}});
  assert.equal(prevented,1,'normal navigation cannot bypass the asynchronous scope check');
  assert.equal(saver.downloads.length,1);
  assert.equal(saver.downloads[0].href,'blob:local-1');
  assert.match(saver.node('exportStatus').textContent,/已交给浏览器保存.*准备时的快照/);
  const switchedSaver=fixture(async()=>exportSnapshot);
  vm.runInContext("context.scope_id='scope-a';",switchedSaver.ctx);
  await switchedSaver.node('prepareExport').onclick();
  vm.runInContext("fetch=async()=>({ok:false,json:async()=>({ok:false,code:'STALE_CONTEXT',error:'身份已切换'})});",switchedSaver.ctx);
  await switchedSaver.node('downloadExport').onclick({preventDefault(){}});
  assert.equal(switchedSaver.downloads.length,0,'another window switching scope must block the old file');
  assert.equal(switchedSaver.node('downloadExport').hidden,true);
  assert.equal(switchedSaver.node('downloadExport').href,undefined);
  let finishSave,saveChecks=0;
  const slowSaver=fixture(async url=>{
    if(url.endsWith('/workspace/export'))return exportSnapshot;
    saveChecks++;return await new Promise(resolve=>{finishSave=resolve;});
  });
  vm.runInContext("context.scope_id='scope-a';",slowSaver.ctx);
  await slowSaver.node('prepareExport').onclick();
  const saving=slowSaver.node('downloadExport').onclick({preventDefault(){}});
  for(let i=0;i<20&&!finishSave;i++)await Promise.resolve();
  await slowSaver.node('downloadExport').onclick({preventDefault(){}});
  assert.equal(saveChecks,1,'repeat clicks do not start parallel save checks');
  vm.runInContext('clearPrivate();',slowSaver.ctx);
  finishSave({revision:1,content:{}});await saving;
  assert.equal(slowSaver.downloads.length,0,'reload during save check invalidates the file');
  const applicantPreset={keywords:['Agent','AI'],primary_cities:['广州','深圳'],secondary_cities:['东莞','惠州','佛山','珠海'],salary_min:6000,daily_greetings:150,daily_replies:0,excluded_terms:[],greeting_template:'您好，对{job_title}感兴趣。'};
  let savedApplicant;
  const applicant=fixture(async (url,options)=>{assert.ok(url.endsWith('/applicant/plan'));if(options.method==='POST'){savedApplicant=JSON.parse(options.body);return {kind:'applicant_plan',revision:1,content:savedApplicant.content};}return {kind:'applicant_plan',revision:0,content:{},suggested_preset:applicantPreset};});
  vm.runInContext("context={context_token:'scope-a',role:'applicant'};",applicant.ctx);
  await vm.runInContext("loadApplicantPlan('scope-a',0)",applicant.ctx);
  assert.equal(applicant.node('planSalary').value,'6000');
  assert.equal(applicant.node('planGreetings').value,'150');
  assert.equal(applicant.node('planReplies').value,'0');
  assert.equal(applicant.node('applicantPlanPanel').hidden,false);
  applicant.node('planConfirmed').checked=true;
  await applicant.node('applicantPlanForm').onsubmit({preventDefault(){}});
  assert.equal(savedApplicant.content.daily_replies,0);
  assert.equal(savedApplicant.content.daily_greetings,150);
  assert.equal(savedApplicant.expected_revision,0);
  assert.match(applicant.node('applicantPlanStatus').textContent,/尚未启动自动发送/);

  let latePlan;
  const applicantLate=fixture(async()=>await new Promise(resolve=>{latePlan=resolve;}));
  vm.runInContext("context={context_token:'scope-a',role:'applicant'};paintApplicantPlan("+JSON.stringify(applicantPreset)+");",applicantLate.ctx);
  applicantLate.node('planConfirmed').checked=true;
  const planSaving=applicantLate.node('applicantPlanForm').onsubmit({preventDefault(){}});
  for(let i=0;i<20&&!latePlan;i++)await Promise.resolve();
  assert.equal(typeof latePlan,'function');
  vm.runInContext("clearPrivate();context={context_token:'scope-b',role:'applicant'};",applicantLate.ctx);
  latePlan({kind:'applicant_plan',revision:1,content:applicantPreset});
  await planSaving;
  assert.equal(applicantLate.node('planGreeting').value,'');
  assert.equal(applicantLate.node('applicantPlanPanel').hidden,true);
  assert.doesNotMatch(applicantLate.node('applicantPlanStatus').textContent,/第 1 版/);

  const applicantOffline=fixture(async url=>{assert.ok(url.endsWith('/applicant/channel'));return {status:'unavailable',boss_page_count:0,account_verified:false,sending_enabled:false};});
  vm.runInContext("context={context_token:'scope-a',role:'applicant'};",applicantOffline.ctx);
  await applicantOffline.node('checkApplicantChannel').onclick();
  assert.match(applicantOffline.node('applicantChannelStatus').textContent,/尚未连接/);

  const salaryJob={job_id:'fictional~',title:'Agent 开发',company:'虚构公司',city:'广州',salary:'\ue032-\ue033K',decision:'needs_review',needs:['可确认的月薪范围']};
  const salaryEvidence={job_id:'fictional~',raw_text:salaryJob.salary,status:'needs_confirmation',recognized_text:'8-12K'};
  const jobResponse={status:'observed',matching_ready:true,profile_revision:1,plan_revision:1,candidates:[salaryJob],counts:{eligible:0,needs_review:1,rejected:0}};
  let confirms=0,prepares=0;
  const salaryPage=fixture(async(url,options)=>{
    if(url.endsWith('/applicant/jobs/read'))return jobResponse;
    if(url.endsWith('/applicant/salaries/read'))return {status:'observed',salaries:[salaryEvidence]};
    if(url.endsWith('/applicant/salaries/confirm')){
      confirms++;const body=JSON.parse(options.body);assert.equal(body.confirmed,true);assert.equal(body.expected_salary_text,'8-12K');
      assert.equal(body.job_observation_task_id,'1'.padStart(32,'0'));assert.equal(body.salary_observation_task_id,'2'.padStart(32,'0'));
      return {confirmation_id:'a'.repeat(64)};
    }
    if(url.endsWith('/applicant/jobs/confirmed-preview'))return {...jobResponse,candidates:[{...salaryJob,decision:'eligible',needs:[]}],counts:{eligible:1,needs_review:0,rejected:0}};
    if(url.endsWith('/applicant/greetings/prepare')){prepares++;assert.equal(JSON.parse(options.body).observation_task_id,'4'.padStart(32,'0'));return {text:'虚构招呼草稿',sent:false};}
    throw new Error('Unexpected salary request '+url);
  });
  vm.runInContext("context={context_token:'scope-a',role:'applicant'};revision=1;applicantPlanRevision=1;",salaryPage.ctx);
  await salaryPage.node('reviewApplicantSalaries').onclick();
  assert.equal(confirms,0,'reading must never automatically confirm amounts');
  const salaryCard=salaryPage.node('salaryReviews').children[0];
  const salaryBox=salaryCard.children.find(n=>n.children?.some(c=>c.type==='checkbox')).children.find(c=>c.type==='checkbox');
  const confirmSalary=salaryCard.children.find(n=>n.textContent==='确认金额并重新匹配');
  assert.equal(confirmSalary.disabled,true);
  await confirmSalary.onclick();assert.equal(confirms,0,'a checkbox is required even if handler is invoked directly');
  salaryBox.checked=true;salaryBox.onchange();await confirmSalary.onclick();
  assert.equal(confirms,1);assert.equal(prepares,0);assert.equal(confirmSalary.disabled,true);
  assert.match(salaryPage.node('salaryReviewStatus').textContent,/符合 1 条/);
  const prepareSalary=salaryCard.children.find(n=>n.textContent==='准备招呼草稿');
  await prepareSalary.onclick();assert.equal(prepares,1);assert.match(salaryCard.children.map(n=>n.textContent).join(' '),/招呼草稿已准备/);
  assert.equal(salaryPage.copied.length,0);

  const staleSalary=fixture(async url=>{
    if(url.endsWith('/applicant/jobs/read'))return jobResponse;
    if(url.endsWith('/applicant/salaries/read'))return {status:'observed',salaries:[salaryEvidence]};
    throw new Error('Stale profile must stop before confirmation');
  });
  vm.runInContext("context={context_token:'scope-a',role:'applicant'};revision=2;applicantPlanRevision=1;",staleSalary.ctx);
  await staleSalary.node('reviewApplicantSalaries').onclick();
  const staleSalaryCard=staleSalary.node('salaryReviews').children[0];
  const staleSalaryBox=staleSalaryCard.children.find(n=>n.children?.some(c=>c.type==='checkbox')).children.find(c=>c.type==='checkbox');
  staleSalaryBox.checked=true;staleSalaryBox.onchange();
  await staleSalaryCard.children.find(n=>n.textContent==='确认金额并重新匹配').onclick();
  assert.match(staleSalaryCard.children.map(n=>n.textContent).join(' '),/资料或方案已变化/);

  let releaseSalary;
  const lateSalary=fixture(async url=>url.endsWith('/applicant/jobs/read')?jobResponse:new Promise(resolve=>{releaseSalary=resolve;}));
  vm.runInContext("context={context_token:'scope-a',role:'applicant'};",lateSalary.ctx);
  const salaryReading=lateSalary.node('reviewApplicantSalaries').onclick();
  for(let i=0;i<25&&!releaseSalary;i++)await Promise.resolve();
  assert.equal(typeof releaseSalary,'function');
  vm.runInContext("clearPrivate();context={context_token:'scope-b',role:'applicant'};",lateSalary.ctx);
  releaseSalary({status:'observed',salaries:[salaryEvidence]});await salaryReading;
  assert.equal(lateSalary.node('salaryReviews').children.length,0,'late salary data must not enter another workspace');

  let browserConfirmations=0;
  const inboxRow={display_name:'虚构HR',party_summary:'虚构HR · 虚构公司',last_message_preview:'请问何时到岗？',unread_marker:true,preview_truncated:false};
  const bindingPage=fixture(async(url,options)=>{
    if(url.endsWith('/applicant/browser/binding')&&options.method==='GET')return {revision:1,content:{display_name:'虚构姓名'}};
    if(url.endsWith('/applicant/identity/read'))return {status:'display_name_observed',display_name:'虚构姓名',browser_session_fingerprint:'a'.repeat(64)};
    if(url.endsWith('/applicant/browser/binding/check'))return {status:'session_matches_confirmation',session_matches_confirmation:true,sending_enabled:false};
    if(url.endsWith('/applicant/browser/binding')){browserConfirmations++;const body=JSON.parse(options.body);assert.equal(body.expected_revision,1);assert.equal(body.confirmed,true);return {revision:2};}
    if(url.endsWith('/applicant/conversations/read'))return {status:'observed_previews',rows:[inboxRow],automation_ready:false,list_truncated:true};
    throw new Error('No sending or reply request expected '+url);
  });
  vm.runInContext("context={context_token:'scope-a',role:'applicant'};",bindingPage.ctx);
  await bindingPage.node('readApplicantIdentity').onclick();
  assert.equal(browserConfirmations,0);assert.match(bindingPage.node('browserBindingStatus').textContent,/虚构姓名/);
  await bindingPage.node('saveBrowserBinding').onclick();assert.equal(browserConfirmations,0);
  bindingPage.node('browserBindingConfirmed').checked=true;bindingPage.node('browserBindingConfirmed').onchange();
  await bindingPage.node('saveBrowserBinding').onclick();assert.equal(browserConfirmations,1);
  await bindingPage.node('checkApplicantBinding').onclick();assert.match(bindingPage.node('browserBindingStatus').textContent,/一致/);
  await bindingPage.node('readApplicantInbox').onclick();assert.equal(bindingPage.node('inboxRows').children.length,1);
  assert.match(bindingPage.node('inboxStatus').textContent,/读取上限/);assert.match(bindingPage.node('inboxStatus').textContent,/没有发送/);
  assert.equal(bindingPage.copied.length,0);

  let releaseInbox;
  const lateInbox=fixture(async()=>new Promise(resolve=>{releaseInbox=resolve;}));
  vm.runInContext("context={context_token:'scope-a',role:'applicant'};",lateInbox.ctx);
  const inboxReading=lateInbox.node('readApplicantInbox').onclick();
  for(let i=0;i<20&&!releaseInbox;i++)await Promise.resolve();
  assert.equal(typeof releaseInbox,'function');
  vm.runInContext("clearPrivate();context={context_token:'scope-b',role:'applicant'};",lateInbox.ctx);
  releaseInbox({status:'observed_previews',rows:[inboxRow],automation_ready:false,list_truncated:false});await inboxReading;
  assert.equal(lateInbox.node('inboxRows').children.length,0,'private previews must not follow a workspace switch');

  const selectedMessageResponse={status:'observed_messages',party:{display_name:'虚构HR',party_summary:'虚构HR · 虚构公司'},
    recipient_verified:false,automation_ready:false,messages_truncated:false,messages:[{direction:'incoming',kind:'text',
      text:'虚构完整问题',timestamp_label:'今天',full_text_available:true,text_truncated:false}]};
  const selectedPage=fixture(async url=>{assert.ok(url.endsWith('/applicant/messages/read'));return selectedMessageResponse;});
  vm.runInContext("context={context_token:'scope-a',role:'applicant'};",selectedPage.ctx);
  await selectedPage.node('readSelectedMessages').onclick();
  assert.match(selectedPage.node('selectedMessagesStatus').textContent,/1 条/);
  assert.match(selectedPage.node('selectedMessagesStatus').textContent,/未开启自动回复/);
  assert.match(selectedPage.node('selectedMessages').children.map(n=>n.children?.map(c=>c.textContent).join(' ')).join(' '),/虚构完整问题/);
  assert.equal(selectedPage.copied.length,0);
  let releaseMessages;
  const lateMessages=fixture(async()=>new Promise(resolve=>{releaseMessages=resolve;}));
  vm.runInContext("context={context_token:'scope-a',role:'applicant'};",lateMessages.ctx);
  const messagesReading=lateMessages.node('readSelectedMessages').onclick();
  for(let i=0;i<20&&!releaseMessages;i++)await Promise.resolve();
  vm.runInContext("clearPrivate();context={context_token:'scope-b',role:'applicant'};",lateMessages.ctx);
  releaseMessages(selectedMessageResponse);await messagesReading;
  assert.equal(lateMessages.node('selectedMessages').children.length,0);

  const observedQuestions={...selectedMessageResponse,binding_revision:1,messages:[
    {...selectedMessageResponse.messages[0],message_id:'mid-1',message_id_source:'dom_data_mid',text:'最高学历是什么？'},
    {...selectedMessageResponse.messages[0],message_id:'mid-2',message_id_source:'dom_data_mid',text:'何时到岗？'}]};
  const observedTask={task_id:'work-id',status:'completed',result:{text:'已确认学历：本科，一周内可以到岗。'},manual_text:null};
  let observedPrepares=0,observedExecutes=0;
  const observedPage=fixture(async(url,options)=>{
    if(url.endsWith('/applicant/messages/read'))return observedQuestions;
    if(url.endsWith('/applicant/replies/prepare')){observedPrepares++;const payload=JSON.parse(options.body);
      assert.deepEqual(payload.message_ids,['mid-1','mid-2']);assert.equal(payload.expected_profile_revision,1);
      assert.equal(payload.expected_binding_revision,1);assert.equal(payload.message_observation_task_id,'1'.padStart(32,'0'));
      return {intent:{intent_id:'a'.repeat(32)},task:{...observedTask,status:observedPrepares===1?'queued':'completed'}};
    }
    if(url.endsWith('/tasks/work-id/execute')){observedExecutes++;return observedTask;}
    if(url.endsWith('/applicant/replies/'+'a'.repeat(32)))return {task:observedTask,profile_stale:false,binding_stale:false,context_stale:false};
    throw new Error('No platform sending request allowed '+url);
  });
  vm.runInContext("context={context_token:'scope-a',role:'applicant'};revision=1;",observedPage.ctx);
  await observedPage.node('readSelectedMessages').onclick();assert.equal(observedPrepares,0);
  const observedButton=observedPage.node('selectedMessages').children.find(n=>n.textContent.startsWith('基于本次消息准备回复草稿'));
  assert.ok(observedButton);await observedButton.onclick();
  assert.match(observedPage.node('observedReplyText').textContent,/一周内/);
  assert.match(observedPage.node('observedReplyStatus').textContent,/未发送/);
  await observedButton.onclick();assert.equal(observedPrepares,2);assert.equal(observedExecutes,1,'existing completed draft must not execute again');
  assert.equal(observedPage.copied.length,0);

  const repliedPage=fixture(async()=>({...observedQuestions,messages:[{...observedQuestions.messages[0],direction:'outgoing'}]}));
  vm.runInContext("context={context_token:'scope-a',role:'applicant'};",repliedPage.ctx);
  await repliedPage.node('readSelectedMessages').onclick();
  assert.equal(repliedPage.node('selectedMessages').children.some(n=>n.textContent.startsWith('基于本次消息准备回复草稿')),false);
  const unclearBoundary=fixture(async()=>({...observedQuestions,messages:[{...observedQuestions.messages[0],direction:'unknown'},observedQuestions.messages[1]]}));
  vm.runInContext("context={context_token:'scope-a',role:'applicant'};",unclearBoundary.ctx);
  await unclearBoundary.node('readSelectedMessages').onclick();
  assert.equal(unclearBoundary.node('selectedMessages').children.some(n=>n.textContent.startsWith('基于本次消息准备回复草稿')),false);

  let releaseObserved;
  const lateObserved=fixture(async url=>{
    if(url.endsWith('/applicant/messages/read'))return observedQuestions;
    if(url.endsWith('/applicant/replies/prepare'))return new Promise(resolve=>{releaseObserved=resolve;});
    throw new Error('Switched workspace must never execute previous draft '+url);
  });
  vm.runInContext("context={context_token:'scope-a',role:'applicant'};revision=1;",lateObserved.ctx);
  await lateObserved.node('readSelectedMessages').onclick();
  const preparingObserved=lateObserved.node('selectedMessages').children.find(n=>n.textContent.startsWith('基于本次消息准备回复草稿')).onclick();
  for(let i=0;i<20&&!releaseObserved;i++)await Promise.resolve();
  assert.equal(typeof releaseObserved,'function');
  vm.runInContext("clearPrivate();context={context_token:'scope-b',role:'applicant'};",lateObserved.ctx);
  releaseObserved({intent:{intent_id:'a'.repeat(32)},task:{task_id:'work-id',status:'queued'}});await preparingObserved;
  assert.equal(lateObserved.node('observedReplyText').textContent,'');assert.equal(lateObserved.node('observedReplyText').hidden,true);
  console.log('PASS: scoped local export, queue triage, search, pagination, editing, batch, recovery, privacy');
})().catch(error => { console.error(error); process.exitCode = 1; });
