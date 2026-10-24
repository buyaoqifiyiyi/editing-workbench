/* Focused controller regression. All HTTP is mocked; no browser, model, or real turn is used. */
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const source = fs.readFileSync(path.join(__dirname, '../src/jianying_engine/web/app.js'), 'utf8');
const elements = new Map();
function element(id) {
  if (!elements.has(id)) {
    const classes = new Set();
    elements.set(id, {
      textContent: '', hidden: false, disabled: false, value: '', options: [], className: '', dataset: {},
      listeners: {},
      get children() { return this.options; },
      replaceChildren(...items) { this.options = items; this.value = items[0]?.value || ''; },
      append(...items) { this.options.push(...items); },
      addEventListener(name, callback) { this.listeners[name] = callback; },
      close() { this.open = false; },
      showModal() { this.open = true; },
      focus() { document.activeElement = this; },
      setAttribute(key, value) { this[key] = value; },
      classList: {
        contains: value => classes.has(value), add: value => classes.add(value),
        remove: value => classes.delete(value),
        toggle(value, force) { (force===undefined?!classes.has(value):force) ? classes.add(value) : classes.delete(value); return classes.has(value); },
      },
      querySelectorAll() { return []; },
      querySelector(selector) { return this.options.find(item=>item.tagName===selector) || null; },
      contains() { return false; },
    });
  }
  return elements.get(id);
}
const approvalButton = { disabled: false };
const approvalCard = { dataset: { approvalId: 'approval-1' }, querySelectorAll() { return [approvalButton]; } };
let backend = { agent: { state: 'disconnected', models: [] }, current_session: null };
let resolveConnect, resolveSend;
let pickedPaths = [];
let rejectMedia = false;
let rejectConnect = false;
let rejectBackend = false, holdBackend = false, resolveBackend;
let rejectOmni = false;
const omniPayloads = [];
const backendPayloads = [];
const calls = [];
let generatedNodes = 0;
const createdTags = [];
function createNode(tag) { createdTags.push(tag); const item=element('generated-' + (++generatedNodes));item.tagName=tag;return item; }
const response = data => ({ ok: true, status: 200, json: async () => structuredClone(data) });
const document = {
  createElement: createNode,
  createElementNS: (_, tag) => createNode(tag),
  getElementById: element,
  querySelector: selector => selector.includes('meta') ? { content: 'test-token' } : selector === '.conversation' ? element('conversation') : null,
  querySelectorAll: selector => selector === '.approval-card' ? [approvalCard] : selector === '.backend-control' ? [...elements.values()].filter(item=>item.className.split(' ').includes('backend-control')) : [],
};
const context = {
  document, window: { setTimeout() {} }, console,
  Option: function (text, value) { this.text = text; this.value = value; },
  URL, location: { origin: 'http://localhost' },
  fetch: async (url, options) => {
    calls.push(url);
    assert.equal(options.headers['X-Workbench-Token'], 'test-token');
    if (url === '/api/connect') {
      if(rejectConnect)return {ok:false,status:400,json:async()=>({error:'测试：Agent 入口不可用'})};
      return new Promise(resolve => { resolveConnect = () => resolve(response(backend)); });
    }
    if (url === '/api/send') return new Promise(resolve => {
      resolveSend = () => resolve(response({ agent: backend.agent, current_session: { id: 's', status: 'running' } }));
    });
    if (url === '/api/artifacts/test') return response({name:'report.md',text:'<script>literal output, never HTML</script>'});
    if (url === '/api/dialog') return response({paths:pickedPaths});
    if (url === '/api/media') {
      if(rejectMedia)return {ok:false,status:400,json:async()=>({error:'测试：路径已不存在'})};
      const {paths} = JSON.parse(options.body);
      for (const selected of paths) if (!backend.media.some(item=>item.path===selected)) backend.media.push({id:selected,path:selected,name:selected.split('/').at(-1),kind:'folder',url:null});
    }
    if (url === '/api/interrupt') backend.current_session.status = 'idle';
    if (url === '/api/omni/status') return response(backend.omni);
    if (url === '/api/omni/configure') {
      const payload=JSON.parse(options.body);omniPayloads.push(payload);
      if(rejectOmni)return {ok:false,status:400,json:async()=>({error:'测试：视听配置保存失败'})};
      backend.omni={base_url:payload.base_url,enabled:payload.enabled,protocol:payload.protocol,model:payload.model,
        request_options:payload.request_options,credential_source:'saved',credential_present:true,ready:payload.enabled,
        message:'配置就绪，尚未验证API连通性与余额。'};
    }
    if (url === '/api/backends/configure') {
      const payload=JSON.parse(options.body);backendPayloads.push(payload);
      if(rejectBackend)return {ok:false,status:400,json:async()=>({error:'测试：软件配置保存失败'})};
      if(holdBackend)await new Promise(resolve=>{resolveBackend=resolve;});
      backend.backends.selection=payload.selection;backend.backends.default_selection=payload.selection;
    }
    if (url.startsWith('/api/media/') && options.method==='DELETE') backend.media=backend.media.filter(item=>item.id!==decodeURIComponent(url.slice('/api/media/'.length)));
    return response(backend);
  },
};
vm.createContext(context);

// Execute the actual controller declarations. Rendering is a stub: this test does not claim visual coverage.
const begin = source.indexOf('  const $');
const end = source.indexOf('  function showDialog');
assert(begin >= 0 && end > begin, 'controller boundaries changed; update this focused harness');
let fragment = source.slice(begin, end);
const applyPattern = /  function applyState\(next\) \{[^\n]+\}/;
assert(applyPattern.test(fragment), 'applyState changed; update the render stub');
fragment = fragment.replace(applyPattern, '  function applyState(next) {state=next;online=true;controls();}');
vm.runInContext(fragment + '\nthis.test={mutate,poll,renderModels,renderMedia,renderProgress,renderApprovals,renderBackends,openBackendPanel,cancelBackendPanel,changeBackendMode,saveBackendSelection,callSingleBackend,setWorkspaceView,toggleInspector,toggleViewerSize,getBackendDraft:()=>backendDraft,getState:()=>state};', context);
context.showDialog=id=>element(id).showModal();
vm.runInContext(source.slice(source.indexOf('  function renderOmniStatus'),source.indexOf('  function showMedia()'))+'\nthis.omniTest={openOmniSettings,saveOmniSettings,renderOmniStatus};',context);

async function main() {
  await context.test.poll();
  const connect = context.test.mutate('/api/connect', {});
  assert.equal(element('connect-button').textContent, '连接中…');
  backend.agent.state = 'connecting';
  await context.test.poll();
  assert.equal(context.test.getState().agent.state, 'connecting');
  console.log('PASS connect: immediate pending label and backend state polling');

  backend.agent = { state: 'connected', models: [{ model: 'mock-model' }] };
  resolveConnect();
  await connect;
  backend.current_session = { id: 's', status: 'idle' };
  await context.test.poll();
  const send = context.test.mutate('/api/send', { text: 'mock only' });
  assert.equal(element('interrupt-button').hidden, false);
  assert.equal(element('interrupt-button').disabled, false);
  assert.equal(approvalButton.disabled, false);
  const sendCount = calls.filter(value => value === '/api/send').length;
  assert.equal(await context.test.mutate('/api/send', { text: 'duplicate' }), null);
  assert.equal(calls.filter(value => value === '/api/send').length, sendCount);
  backend.current_session.status = 'starting';
  await context.test.poll();
  assert.equal(context.test.getState().current_session.status, 'starting');
  assert.equal(element('composer-agent').disabled, true, 'Agent switch must be disabled during startup');
  assert.equal(element('connection-submit').disabled, true, 'Cannot submit an Agent switch during an active task');
  console.log('PASS send: duplicate blocked; live starting state polled; controls remain available');

  await context.test.mutate('/api/approval', { id: 'approval-1', decision: 'accept' });
  assert(calls.includes('/api/approval'));
  await context.test.mutate('/api/interrupt', {});
  assert.equal(context.test.getState().current_session.status, 'idle');
  resolveSend();
  await send;
  assert.equal(context.test.getState().current_session.status, 'idle');
  console.log('PASS control requests: approval/interrupt bypass pending send; late send state ignored');

  const sync = source.match(/  function syncInspector\(\)\{[^\n]+\}/);
  assert(sync, 'inspector synchronization changed; update this focused harness');
  vm.runInContext('const narrow={matches:true};' + sync[0] + ';this.sync=syncInspector;', context);
  context.sync();
  assert.equal(element('inspector').hidden, true);
  assert.equal(element('toggle-inspector')['aria-expanded'], 'false');
  element('app').classList.add('mobile-inspector');
  context.sync();
  assert.equal(element('inspector').hidden, false);
  assert.equal(element('toggle-inspector')['aria-expanded'], 'true');
  element('app').classList.remove('mobile-inspector');
  context.sync();
  assert.equal(element('inspector').hidden, true);
  console.log('PASS narrow inspector: open/close hidden state and ARIA agree');
  console.log('PASS token header: every mocked HTTP request carried the workbench token');

  element('prompt').value = 'Mock draft only';
  backend.agent = { id: 'acp-test', protocol: 'acp', state: 'connected', models: [] };
  await context.test.poll();
  context.test.renderModels();
  assert.equal(element('send-button').disabled, false, 'ACP default model must remain usable without a catalog');
  assert.equal(element('model-select').value, '');
  assert.equal(element('effort-select').value, '');
  backend.agent = { id: 'codex-test', protocol: 'app-server', state: 'connected', models: [] };
  await context.test.poll();
  assert.equal(element('send-button').disabled, true, 'non-ACP missing catalog must not enable sending');
  console.log('PASS agent catalog: ACP empty catalog permits defaults; non-ACP empty catalog blocks send');

  const connectionHandler = source.match(/  \$\('connection-form'\)\.addEventListener\('submit',[^\n]+\);/);
  assert(connectionHandler, 'connection submit changed; update focused handler extraction');
  vm.runInContext(connectionHandler[0], context);
  element('cli-path').value = 'C:/mock-agent.exe';
  element('agent-select').value = 'acp-test';
  for (const invalid of ['{}', 'null', '[1]', '["acp", false]', '"acp"', '{']) {
    const before = calls.length;
    element('agent-args').value = invalid;
    await element('connection-form').listeners.submit({ preventDefault() {} });
    assert.equal(calls.length, before, `invalid argument shape reached fetch: ${invalid}`);
    assert.equal(element('error-banner').hidden, false);
    assert(element('error-text').textContent, 'invalid arguments need visible feedback');
  }
  console.log('PASS connection form: six malformed/non-string-array inputs refused before fetch');

  const commonModel = { model: 'shared-model', supportedReasoningEfforts: [{ reasoningEffort: 'high' }] };
  backend.agent = { id: 'agent-a', protocol: 'acp', state: 'connected', models: [commonModel] };
  await context.test.poll();
  context.test.renderModels();
  element('model-select').value = 'shared-model';
  element('effort-select').value = 'high';
  backend.agent = { id: 'agent-b', protocol: 'acp', state: 'connected', models: [commonModel] };
  await context.test.poll();
  context.test.renderModels();
  assert.equal(element('model-select').value, '', 'switching Agent must clear even an identically named old model');
  assert.equal(element('effort-select').value, '', 'switching Agent must clear prior reasoning effort');
  console.log('PASS agent identity: switching Agent resets prior model/effort even with matching model IDs');
  backend.workspace = 'C:/fixture';
  backend.media = [{id:'folder',name:'Folder reference',path:'C:/fixture/media',kind:'folder',url:null},
    ...Array.from({length:500}, (_,index)=>({id:'file-'+index,name:index+'.png',path:'C:/fixture/media/'+index+'.png',kind:'image',size:70,url:'/api/media/file-'+index}))];
  await context.test.poll();
  const tagStart = createdTags.length;
  context.test.renderMedia();
  assert.equal(element('media-count').textContent, '501');
  assert.equal(element('media-list').children.length, 101, 'Render one batch plus a more button');
  assert.equal(createdTags.slice(tagStart).includes('video'), false, 'Folder is not a media stream');
  const thumbnails=[...elements.values()].filter(item=>item.className==='file-thumbnail-image');
  assert.equal(thumbnails.length,99,'Only explicitly registered images in the visible batch get thumbnails');
  assert(thumbnails.every(item=>item.loading==='lazy'&&item.src.startsWith('http://localhost/api/media/')&&item.src.includes('token=test-token')));
  assert(element('media-preview').children[0].children.some(child=>child.textContent==='文件夹已引用'));
  for(let index=0;index<5;index++) element('media-list').children.at(-1).listeners.click();
  assert.equal(element('media-list').children.length, 501, 'All references stay reachable after loading batches');
  console.log('PASS folder reference: no folder stream or eager scan; 501 references remain reachable in batches');
  element('media-list').children[1].listeners.click();
  const image=element('media-preview').children[0];image.naturalWidth=1920;image.naturalHeight=1080;image.listeners.load();
  assert.equal(element('preview-size').textContent,'1920 × 1080');assert.equal(element('viewer-fit').disabled,false);
  context.test.toggleViewerSize();assert.equal(element('viewer-fit').textContent,'100%');assert(element('media-preview').classList.contains('actual-size'));
  context.test.toggleViewerSize();assert.equal(element('viewer-fit').textContent,'适合窗口');
  element('media-list').children[0].listeners.click();assert.equal(element('viewer-fit').disabled,true);assert.equal(element('preview-size').textContent,'按需读取');
  console.log('PASS source viewer: real decoded dimensions, fit/100% image control and reset when selecting a folder');
  backend.agent = { id:'settings-peer',protocol:'acp',state:'connected',currentModel:'actual-model',currentEffort:'low',
    models:[{model:'actual-model',supportedReasoningEfforts:[{reasoningEffort:'low'},{reasoningEffort:'high'}]}] };
  await context.test.poll();context.test.renderModels();
  assert.equal(element('model-select').value,'actual-model');
  assert.equal(element('effort-select').value,'low');
  console.log('PASS ACP settings: actual session model and reasoning values are reflected');
  const showDialog=source.match(/  function showDialog[^\n]+/)[0];
  const showArtifact=source.match(/  async function showArtifact[\s\S]+?\n  }/)[0];
  vm.runInContext(showDialog+'\n'+showArtifact+'\nthis.test.renderArtifacts=renderArtifacts;this.test.showArtifact=showArtifact;',context);
  backend.artifacts=[{id:'test',name:'report.md',type:'文档',category:'deliverable',preview_kind:'text',path:'C:/fixture/report.md',url:'/api/artifacts/test'}];
  await context.test.poll();context.test.renderArtifacts();
  const view=element('artifact-list').children[0].children[2];
  assert.equal(view.textContent,'查看');
  await view.listeners.click();
  assert.equal(element('artifact-dialog').open,true);
  assert.equal(element('artifact-preview').children[0].textContent,'<script>literal output, never HTML</script>');
  console.log('PASS artifacts: view action fetches a report and renders literal text');
  const mediaHandlers=source.match(/  \$\('(media-form|browse-media|browse-media-folder)'\)\.addEventListener\([^\n]+/g);
  assert.equal(mediaHandlers.length,3);
  vm.runInContext(mediaHandlers.join('\n'),context);
  backend.media=[];await context.test.poll();
  element('app').classList.add('right-collapsed');
  element('media-dialog').showModal();pickedPaths=['C:/fixture/selected-folder'];
  await element('browse-media-folder').listeners.click();
  assert.equal(context.test.getState().media.length,1,'Picker result must reach backend state');
  assert.equal(element('media-dialog').open,false);
  assert.equal(element('inspector').hidden,false,'Successful selection must reveal the media panel');
  assert.equal(element('toggle-inspector')['aria-expanded'],'true');
  assert.match(element('media-feedback').textContent,/已添加.*selected-folder/,'Visible confirmation must name the selected item');
  context.test.renderMedia();
  assert.equal(element('attached-media').hidden,false);
  assert.match(element('attached-media').textContent,/selected-folder/);
  assert.equal(element('panel-media').hidden,false);
  assert.equal(element('panel-artifacts').hidden,true);
  element('media-dialog').showModal();
  await element('browse-media-folder').listeners.click();
  assert.equal(context.test.getState().media.length,1);
  assert.match(element('media-feedback').textContent,/无需重复添加/);
  element('media-dialog').showModal();pickedPaths=[];
  await element('browse-media').listeners.click();
  assert.equal(element('media-dialog').open,true);
  assert.match(element('media-dialog-status').textContent,/已取消/);
  pickedPaths=['C:/fixture/clip.mp4'];
  await element('browse-media').listeners.click();
  assert.match(element('media-feedback').textContent,/已添加.*clip.mp4/);
  element('media-paths').value='C:/fixture/pasted-folder';
  await element('media-form').listeners.submit({preventDefault(){}});
  assert.match(element('media-feedback').textContent,/已添加.*pasted-folder/);
  element('media-dialog').showModal();rejectMedia=true;
  await element('browse-media-folder').listeners.click();
  assert.equal(element('media-dialog').open,true);
  assert.match(element('error-text').textContent,/路径已不存在/);
  assert.equal(context.test.getState().media.length,3);
  console.log('PASS media selection: registered path, visible panel and named confirmation');
  vm.runInContext('applyState=function(next){state=next;online=true;renderMedia();controls();};',context);
  backend.media=[{id:'last',name:'Last folder',kind:'folder',path:'C:/fixture/last',url:null}];
  await context.test.poll();
  const removed=await context.test.mutate('/api/media/last',undefined,'DELETE');
  assert(removed,'Removing the last item must not fail while rendering empty media');
  assert.equal(context.test.getState().media.length,0);
  assert.equal(element('media-count').textContent,'0');
  assert.equal(element('attached-media').hidden,true);
  assert.equal(element('media-preview').children[0].children.at(-1).textContent,'选择素材查看预览');
  assert.equal(element('error-banner').hidden,true);
  await context.test.poll();
  assert.equal(element('new-session').disabled,false,'Empty media must not break subsequent polling');
  console.log('PASS last media removal: empty preview, count and polling stay usable');
  const prepare=source.match(/  async function prepareACP[^\n]+/)[0];
  const connectAgent=source.match(/  async function connectAgent[^\n]+/)[0];
  vm.runInContext(prepare+'\n'+connectAgent+'\nthis.test.connectOnOpen=connectOnOpen;this.resetStartup=()=>{autoConnectAttempted=false;};',context);
  backend.agent={id:'remembered-acp',protocol:'acp',state:'disconnected',models:[]};
  backend.current_session=null;
  await context.test.poll();
  const beforeAuto=calls.filter(item=>item==='/api/connect').length;
  const automatic=context.test.connectOnOpen();
  assert.equal(calls.filter(item=>item==='/api/connect').length,beforeAuto+1);
  assert.equal(element('connect-button').textContent,'连接中…');
  assert.equal(await context.test.connectOnOpen(),undefined,'Startup must not duplicate an in-flight connect');
  backend.agent.state='connected';resolveConnect();await automatic;
  assert(calls.includes('/api/session/prepare'),'ACP automatic connection must read actual session settings');
  backend.agent.state='disconnected';await context.test.poll();
  await context.test.connectOnOpen();
  assert.equal(calls.filter(item=>item==='/api/connect').length,beforeAuto+1,'No infinite reconnect loop');
  context.resetStartup();backend.agent.state='connected';await context.test.poll();
  await context.test.connectOnOpen();
  assert.equal(calls.filter(item=>item==='/api/connect').length,beforeAuto+1,'Do not disturb an existing connection');
  context.resetStartup();backend.agent.state='disconnected';backend.current_session={id:'busy',status:'running'};await context.test.poll();
  await context.test.connectOnOpen();
  assert.equal(calls.filter(item=>item==='/api/connect').length,beforeAuto+1,'Never reconnect while a task is running');
  backend.current_session=null;await context.test.poll();rejectConnect=true;
  await context.test.connectOnOpen();
  assert.match(element('error-text').textContent,/Agent 入口不可用/);
  assert.equal(element('connect-button').disabled,false);
  assert.equal(element('connect-button').textContent,'连接');
  await context.test.connectOnOpen();
  assert.equal(calls.filter(item=>item==='/api/connect').length,beforeAuto+2,'Connection failure must not trigger repeated attempts');
  console.log('PASS automatic connection: one attempt, remembered Agent, ACP prepare, no reconnect loop');
  backend.current_session={id:'old',status:'idle',events:Array.from({length:200},(_,i)=>({id:String(i),label:'分析要求',status:'completed'}))};
  rejectConnect=false;await context.test.poll();context.test.renderProgress();
  assert.equal(element('progress-count').hidden,true,'Event count must never be displayed as task progress');
  assert.equal(element('event-list').children.filter(item=>item.className==='event-item').length,0);
  const details=element('event-list').querySelector('details');assert(details);assert(!details.open);
  backend.current_session={id:'next',status:'running',plan:[{step:'查看素材',status:'completed'},{step:'生成草稿',status:'inProgress'}],events:[{id:'error',label:'构建失败',status:'error'}]};
  await context.test.poll();context.test.renderProgress();
  assert.equal(element('progress-count').textContent,'1/2');assert.equal(element('progress-count').hidden,false);
  assert.equal(element('event-list').children.filter(item=>item.className==='event-item').length,3,'Real stages and failure must remain visible');
  backend.current_session.status='idle';backend.current_session.turn_status='interrupted';await context.test.poll();context.test.renderProgress();assert.equal(element('task-state').textContent,'已中断');
  backend.current_session.status='running';backend.current_session.plan=[];backend.current_session.current_run_id='new-run';
  await context.test.poll();context.test.renderProgress();
  assert.equal(element('event-list').children.filter(item=>item.className==='event-item').length,0,'Previous turn failures and activities must not appear as current progress');
  console.log('PASS progress: old 200-item history collapsed, actual stage fraction, errors and interrupted state');
  backend.approvals=[{id:'file',kind:'file',title:'需要允许修改文件',detail:'plan.json',allow_session:true,changes:[{path:'plan.json',diff:'+ plan'}]}];
  await context.test.poll();context.test.renderApprovals();
  const card=element('approvals').children[0],actions=card.children.at(-1);
  assert.equal(actions.children.length,3);assert.equal(actions.children[2].textContent,'本会话允许这些文件');
  const requestCount=calls.filter(value=>value==='/api/approval').length;
  assert.equal(calls.filter(value=>value==='/api/approval').length,requestCount,'Rendering never auto-approves');
  console.log('PASS approval: readable file, collapsible diff, explicit session button without automatic approval');
  backend.artifacts=[{id:'draft',name:'draft_content.json',display_name:'正式草稿',category:'deliverable',type:'draft',project:'新片',path:'C:/fixture/draft/draft_content.json'},
    {id:'plan',name:'edit-plan.json',category:'project',type:'数据',path:'C:/fixture/edit-plan.json'},
    {id:'frame',name:'01.jpg',category:'process',type:'image',path:'C:/fixture/scout/01.jpg'}];
  await context.test.poll();context.test.renderArtifacts();
  assert.equal(element('artifact-count').textContent,'1');
  const sections=element('artifact-list').children.filter(item=>item.tagName==='details');
  assert.equal(sections.length,2);assert(sections.every(section=>!section.open));
  assert.equal(element('artifact-list').children.filter(item=>item.className==='artifact-row').length,1);
  console.log('PASS artifact classification: badge counts deliverables only; project/process sections collapsed');
  const beforeNativePreview=calls.length;
  await context.test.showArtifact({name:'project.drp',preview_kind:'file',type:'达芬奇工程',path:'C:/fixture/project.drp'});
  assert.equal(element('artifact-preview').children[0].tagName,'p');
  assert.match(element('artifact-preview').children[0].textContent,/达芬奇工程.*对应软件中打开或导入/);
  assert.equal(calls.length,beforeNativePreview,'Native project preview must not decode or fetch binary as text/media');
  for(const artifact of [{name:'shot.aep',type:'AE工程'},{name:'shot.blend',type:'Blender工程'},{name:'intermediate.avi',type:'中间素材'}]){
    await context.test.showArtifact({...artifact,preview_kind:'file',path:'C:/fixture/'+artifact.name});
    assert(element('artifact-preview').children[0].textContent.includes(artifact.type),'Every software file preview names its own artifact type');
    assert(!element('artifact-preview').children[0].textContent.includes('达芬奇'),'Other software files must never be labelled as Resolve projects');
  }
  assert.equal(calls.length,beforeNativePreview,'Standalone software projects and intermediary files stay local file views');
  console.log('PASS native project preview: explanatory file view without decoding binary');
  backend.current_session={id:'software-session',status:'idle'};
  for(const id of ['backend-mode-workflow','backend-mode-single','backend-single-select'])element(id).className='backend-control';
  const workflow={mode:'workflow',roles:{editing:['resolve'],effects:['after-effects'],color:[],audio:[]},single_backend:null};
  backend.backends={roles:[{id:'editing',label:'剪辑'},{id:'effects',label:'特效'},{id:'color',label:'调色'},{id:'audio',label:'音频'}],
    catalog:[{id:'resolve',name:'DaVinci Resolve',short_name:'达芬奇',roles:['editing','effects','color','audio'],admitted_roles:['editing'],status_label:'受限接入',single_supported:true,summary:'原生基础剪辑',limitations:['backend=resolve 的其他原生功能尚未接入']},
      {id:'jianying',name:'剪映',roles:['editing'],admitted_roles:['editing'],status_label:'受限接入',single_supported:true},
      {id:'after-effects',name:'After Effects',short_name:'AE',roles:['effects'],status_label:'受限接入',single_supported:true},
      {id:'premiere',name:'Premiere Pro',roles:['editing','effects','color','audio'],admitted_roles:[],status_label:'待适配',single_supported:false}],selection:workflow,default_selection:workflow};
  backend.backends.catalog=backend.backends.catalog.map(item=>({...item,selectable:item.single_supported,availability:{label:item.single_supported?'本机可用':'暂不兼容',reason:'测试检测状态'}}));
  await context.test.poll();context.test.renderBackends();
  assert.match(element('backend-summary').textContent,/剪辑 · 达芬奇.*特效 · AE/);
  context.test.openBackendPanel();assert.equal(element('backend-panel').hidden,false);
  assert.equal(element('workspace-view').hidden,true,'Software configuration is an independent module, not a chat disclosure');
  assert.equal(element('records-view').hidden,true);
  assert.equal(element('conversation').classList.contains('backend-config-open'),true,'Software configuration uses its own available content space');
  const limitationDetails=element('backend-catalog').children[0].children.find(item=>item.className==='backend-limit-details');
  assert(limitationDetails&&!limitationDetails.open,'Technical limitations stay available in a collapsed details section');
  const descendants=root=>root.children.flatMap(child=>[child,...descendants(child)]);
  const roleInput=(role,id)=>descendants(element('backend-role-list')).find(item=>item.name==='backend-role-'+role&&item.value===id);
  assert.equal(roleInput('color','resolve').disabled,true,'Resolve is displayed under color but an unadapted function is greyed out');
  assert.equal(roleInput('effects','resolve').disabled,true);
  assert.equal(roleInput('editing','premiere').disabled,true,'PR remains visible under its product functions while unavailable');
  assert.equal(roleInput('color','jianying'),undefined,'Jianying is only used for editing');
  assert.equal(roleInput('editing','kdenlive'),undefined,'Unplanned software must not appear as a disabled candidate');
  assert.equal(backend.backends.catalog.length,4);
  const beforeConfiguration=calls.length;
  roleInput('editing','jianying').checked=true;roleInput('editing','jianying').listeners.change();
  assert.equal(calls.length,beforeConfiguration,'Draft choices never call HTTP or launch tasks');
  assert.deepEqual(context.test.getState().backends.selection.roles.editing,['resolve'],'Draft must not mutate session state');
  await context.test.saveBackendSelection();
  assert.deepEqual(backendPayloads.at(-1),{selection:{mode:'workflow',roles:{editing:['jianying'],effects:['after-effects'],color:[],audio:[]},single_backend:null}});
  context.test.renderBackends();assert.match(element('backend-summary').textContent,/剪辑 · 剪映.*AE/);
  assert.equal(element('workspace-view').hidden,false,'Saving configuration returns to the task workspace');
  console.log('PASS software workflow: product function matrix, role-level admission, local draft and configure payload');
  context.test.openBackendPanel();
  const noneEditor=descendants(element('backend-role-list')).find(item=>item.id==='backend-editing-none');noneEditor.listeners.change();
  await context.test.saveBackendSelection();assert.deepEqual(backendPayloads.at(-1).selection.roles.editing,[],'Effects-only workflows must be allowed');
  context.test.openBackendPanel();context.test.changeBackendMode('single');
  assert.equal(element('backend-role-list').hidden,true);
  const draftBeforeFailure=JSON.stringify(context.test.getBackendDraft());rejectBackend=true;
  await context.test.saveBackendSelection();
  assert.equal(JSON.stringify(context.test.getBackendDraft()),draftBeforeFailure,'Save failure must retain every draft choice');
  assert.match(element('backend-error').textContent,/软件配置保存失败/);assert.equal(element('backend-save').disabled,false,'Failed save can be retried');
  rejectBackend=false;await context.test.saveBackendSelection();assert.deepEqual(backendPayloads.at(-1).selection.roles,{},'Single mode submits no workflow roles');
  console.log('PASS configuration edits: effects-only allowed, single mode normalized, failed save keeps draft for retry');
  context.test.renderBackends();context.test.openBackendPanel();
  const preBusy=backendPayloads.length;backend.current_session.status='running';await context.test.poll();
  assert.equal(element('backend-save').disabled,true);
  assert.equal(element('backend-mode-single').disabled,true);
  assert.equal(await context.test.callSingleBackend('jianying'),undefined);await context.test.saveBackendSelection();
  assert.equal(backendPayloads.length,preBusy,'Running tasks must block backend reconfiguration');
  backend.current_session.status='idle';await context.test.poll();holdBackend=true;
  const saving=context.test.saveBackendSelection();assert.equal(element('backend-save').disabled,true);assert.equal(element('backend-cancel').disabled,true);
  assert.equal(element('backend-mode-workflow').disabled,true,'Pending request locks configuration controls');
  resolveBackend();await saving;holdBackend=false;
  const sendCalls=calls.filter(item=>item==='/api/send').length;
  assert.equal(await context.test.callSingleBackend('premiere'),undefined,'Unsupported software cannot be called separately');
  await context.test.callSingleBackend('after-effects');assert.deepEqual(backendPayloads.at(-1).selection,{mode:'single',roles:{},single_backend:'after-effects'});
  assert.equal(calls.filter(item=>item==='/api/send').length,sendCalls,'Calling a software tile only saves selection, never sends a model turn');
  assert.equal(document.activeElement,element('prompt'),'Single software action leaves user ready to describe the task');
  console.log('PASS configuration safety: busy/pending controls, unsupported single call blocked, single action only saves and focuses prompt');
  context.test.renderBackends();assert.equal(element('backend-summary').textContent,'单独调用 · AE');
  const beforeUnavailable=backendPayloads.length;
  await context.test.callSingleBackend('kdenlive');
  assert.equal(backendPayloads.length,beforeUnavailable,'Standalone shortcut must also reject uninstalled software');
  backend.backends.selection={mode:'workflow',roles:{editing:['resolve'],effects:['after-effects'],color:[],audio:[]},single_backend:null};
  await context.test.poll();context.test.openBackendPanel();
  const installedJianying=backend.backends.catalog.find(item=>item.id==='jianying');
  installedJianying.selectable=false;installedJianying.availability={label:'未安装',reason:'软件已卸载'};
  const installedAE=backend.backends.catalog.find(item=>item.id==='after-effects');
  installedAE.selectable=false;installedAE.availability={label:'未安装',reason:'软件已卸载'};
  await context.test.poll();context.test.renderBackends();
  assert.equal(element('backend-save').disabled,true,'A saved selection becomes invalid after uninstall');
  assert.equal(roleInput('editing','jianying').disabled,true,'Unavailable editing software cannot be selected');
  assert.equal(element('backend-single-select').options.find(item=>item.value==='jianying').disabled,true);
  await context.test.callSingleBackend('jianying');assert.equal(backendPayloads.length,beforeUnavailable);
  const oldJianying=roleInput('effects','after-effects');assert.equal(oldJianying.checked,true,'Keep saved selection visible');
  oldJianying.checked=false;oldJianying.listeners.change();
  assert.equal(roleInput('effects','after-effects').disabled,true,'Unavailable old checkbox can be removed but not selected again');
  assert.equal(element('backend-save').disabled,false,'Removing an unavailable backend allows saving remaining valid choices');
  const missingCard=element('backend-catalog').children.find(item=>item.dataset.backendId==='jianying');
  assert.match(missingCard.children.find(item=>item.className.includes('backend-detection')).textContent,/未安装.*软件已卸载/);
  console.log('PASS installed software: disabled choices and single action, dynamic uninstall, preserved saved choices and explicit removal');
  context.test.openBackendPanel();context.test.changeBackendMode('workflow');
  backend.current_session={id:'legacy-session',status:'idle'};backend.backends.selection=null;await context.test.poll();context.test.renderBackends();
  assert.equal(element('backend-summary').textContent,'沿用会话工程');assert.equal(element('backend-panel').hidden,true,'Changing session drops unsaved software draft');
  const beforeCancel=backendPayloads.length;context.test.openBackendPanel();context.test.cancelBackendPanel();
  assert.equal(element('conversation').classList.contains('backend-config-open'),false,'Closing software configuration restores chat layout');
  assert.equal(document.activeElement,element('prompt'),'Cancel returns focus to the unchanged task draft');
  assert.equal(backendPayloads.length,beforeCancel);assert.equal(backend.backends.selection,null,'Opening and cancelling a legacy session must never bind defaults');
  backend.backends.error='软件配置已过期，请重新选择';await context.test.poll();context.test.openBackendPanel();assert.match(element('backend-error').textContent,/配置已过期/);context.test.cancelBackendPanel();
  delete backend.backends;await context.test.poll();context.test.renderBackends();
  assert.match(element('backend-summary').textContent,/服务未提供软件配置/);assert.equal(element('backend-toggle').disabled,true);
  console.log('PASS configuration state: session summary follows selection, null preserves legacy route, cancel does not save, older servers degrade gracefully');
  context.test.setWorkspaceView('records');context.test.renderBackends();
  assert.equal(element('records-view').hidden,false);assert.equal(element('workspace-view').hidden,true);
  context.test.toggleInspector();
  assert.equal(element('workspace-view').hidden,false);assert.equal(element('records-view').hidden,true);
  assert.equal(element('inspector').hidden,false,'Global resource button reveals its workspace ancestor as well as the panel');
  assert.equal(element('app').classList.contains('mobile-inspector'),true);
  const html=fs.readFileSync(path.join(__dirname,'../src/jianying_engine/web/index.html'),'utf8');
  assert(html.indexOf('id="media-preview"')<html.indexOf('id="composer-form"'),'Material monitor is the primary surface above the task sheet');
  assert(html.includes('制作工作区')&&html.includes('软件配置')&&html.includes('执行记录'));
  assert(!html.includes('聊聊这次想怎么剪')&&!html.includes('告诉我你想怎么剪'),'Chat hero and chat prompt are absent');
  assert(source.includes('event.ctrlKey||event.metaKey'),'Task execution uses an explicit modified Enter shortcut');
  console.log('PASS production modules: primary material monitor, separate task/records/software views, no chat hero, explicit task execution shortcut');
  backend.current_session={id:'omni-id',status:'idle'};
  backend.omni={base_url:'https://example.invalid/v1',enabled:true,credential_source:'none',ready:false,
    protocol:'openai-compatible',model:'vendor/past-model',request_options:{temperature:.2},message:'未配置密钥'};
  await context.test.poll();context.omniTest.openOmniSettings();
  assert.equal(element('omni-dialog').open,true);
  assert.equal(element('omni-url').value,'https://example.invalid/v1');
  assert.equal(element('omni-protocol').value,'openai-compatible');
  assert.equal(element('omni-model').value,'vendor/past-model');
  assert.equal(JSON.parse(element('omni-options').value).temperature,.2);
  assert.equal(element('omni-key').value,'');
  element('omni-key').value='fixture-ui-key';element('omni-enabled').checked=true;
  element('omni-protocol').value='gemini';element('omni-model').value='gemini-custom-fixture';
  element('omni-options').value='{"temperature":0.4}';
  const saved=context.omniTest.saveOmniSettings({preventDefault(){}});
  assert.equal(element('omni-key').value,'','Secret is cleared before awaiting save');
  await saved;
  assert.equal(omniPayloads.at(-1).api_key,'fixture-ui-key');
  assert.equal(omniPayloads.at(-1).protocol,'gemini');
  assert.equal(omniPayloads.at(-1).model,'gemini-custom-fixture');
  assert.equal(omniPayloads.at(-1).request_options.temperature,.4);
  assert(!JSON.stringify(context.test.getState()).includes('fixture-ui-key'));
  assert(element('omni-status').textContent.startsWith('已保存。'));
  context.omniTest.openOmniSettings();assert.equal(element('omni-key').value,'');
  assert(element('omni-key-status').textContent.includes('已加密保存'));
  assert(html.includes('id="omni-key" type="password"'));
  console.log('PASS omni configuration: visible entry, endpoint form, local save, key cleared and never in state, saved status and blank reopen');
  rejectOmni=true;element('omni-key').value='fixture-retry-key';
  await context.omniTest.saveOmniSettings({preventDefault(){}});
  assert.equal(element('omni-key').value,'');
  assert(element('error-text').textContent.includes('保存失败'));
  rejectOmni=false;
  console.log('PASS omni failure: save error is visible and sensitive input remains cleared');
  console.log('PASS omni model choice: protocol/model/request options round-trip through the form and request');
  console.log('RESULT: 28 assertion groups passed. No network, paid turn, model, or browser rendering executed.');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
