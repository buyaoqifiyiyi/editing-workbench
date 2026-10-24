'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const token = document.querySelector('meta[name="workbench-token"]').content;
  let state = null, online = false, pending = false, pendingAction = '', epoch = 0, polling = false;
  const controlRequests = new Set();
  let selectedMedia = null, previewKey = '', sessionKey = '', modelKey = '', modelAgentId = '', approvalsKey = '';
  let currentTab = 'media';
  let mediaVisible = 100, mediaContext = '';
  let autoConnectAttempted = false;
  let backendOpen = false, backendDraft = null, backendContext = '', backendRenderKey = '', backendError = '';
  let workspaceView = 'workspace';
  const busyStates = new Set(['starting', 'running', 'stopping', 'awaiting_approval']);
  const busy = () => busyStates.has(state?.current_session?.status);
  const labels = {disconnected:'未连接', connecting:'连接中', connected:'已连接', error:'错误',
    idle:'等待指令', starting:'正在启动', running:'正在执行', stopping:'正在停止', awaiting_approval:'等待确认',
    completed:'已完成', complete:'已完成', succeeded:'已完成', done:'已完成', failed:'失败',
    pending:'等待中', inProgress:'进行中', cancelled:'已停止', interrupted:'已中断'};
  const text = (element, value) => { const next = String(value ?? ''); if (element.textContent !== next) element.textContent = next; };
  const node = (tag, className, value) => { const el = document.createElement(tag); if (className) el.className = className; if (value !== undefined) text(el, value); return el; };
  function icon(name) { const el = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); el.setAttribute('class','icon'); el.setAttribute('aria-hidden','true'); const use = document.createElementNS(el.namespaceURI, 'use'); use.setAttribute('href', '#i-' + name); el.append(use); return el; }
  function actionButton(name, title, callback) { const el = node('button','icon-button'); el.type = 'button'; el.title = title; el.setAttribute('aria-label', title); el.append(icon(name)); el.addEventListener('click', event => { event.stopPropagation(); callback(); }); return el; }
  function setError(error) { const message=error?.message || error || '请求失败';text($('error-text'),message);$('error-banner').hidden=false;const dialog=document.querySelector('dialog[open]');if(dialog){let alert=dialog.querySelector('.dialog-error');if(!alert){alert=node('p','dialog-error');alert.setAttribute('role','alert');dialog.querySelector('form').append(alert);}text(alert,message);} }
  function clearError() { $('error-banner').hidden = true; document.querySelectorAll('.dialog-error').forEach(el=>el.remove()); }
  async function fetchAPI(path, method = 'GET', body) {
    const response = await fetch(path, {method, credentials:'same-origin', headers:{'X-Workbench-Token':token, ...(body === undefined ? {} : {'Content-Type':'application/json'})}, ...(body === undefined ? {} : {body:JSON.stringify(body)})});
    let result;
    try { result = await response.json(); } catch (_) { throw new Error(`本地服务未返回有效 JSON（HTTP ${response.status}）`); }
    if (!response.ok || result?.error) throw new Error(result?.error || `请求失败（HTTP ${response.status}）`);
    return result;
  }
  async function perform(work, controlKey = '', action = '') {
    if (controlKey ? controlRequests.has(controlKey) : pending) return null;
    if(controlKey)controlRequests.add(controlKey);else{pending=true;pendingAction=action;}
    const generation=++epoch;clearError();controls();
    try { return await work(generation); }
    catch (error) { if(generation===epoch)setError(error);return null; }
    finally { if(controlKey)controlRequests.delete(controlKey);else{pending=false;pendingAction='';}controls(); }
  }
  function applyState(next) { if (!next || !next.agent) throw new Error('服务响应缺少工作台状态');if(state&&!online&&!next.agent.error&&!next.current_session?.error)clearError();state=next;online=true;render();void connectOnOpen(); }
  function connectOnOpen(){if(autoConnectAttempted||!online||pending||busy()||!state?.agent)return;autoConnectAttempted=true;if(state.agent.state==='disconnected')return connectAgent({});}
  function applyResponse(next,generation){if(generation===epoch){applyState(next);epoch++;}}
  const mutate = (path, body = {}, method = 'POST') => {
    const controlKey=path==='/api/interrupt'?path:path==='/api/approval'?path+':'+body.id:'';
    return perform(async generation=>{const next=await fetchAPI(path,method,body);applyResponse(next,generation);return next;},controlKey,path);
  };
  async function poll() {
    if (!polling) {
      polling = true; const generation = epoch;
      try { const next = await fetchAPI('/api/state'); if (generation === epoch) applyState(next); }
      catch (error) { if (generation === epoch) { online = false; setError(error); controls(); } }
      finally { polling = false; }
    }
    window.setTimeout(poll, document.hidden ? 2000 : 700);
  }
  const modelId = model => String(model.model || model.id || '');
  const models = () => (state?.agent?.models || []).filter(model => modelId(model) && !model.hidden);
  function effortOptions() {
    const selected = models().find(model => modelId(model) === $('model-select').value);
    const old = $('effort-select').value; $('effort-select').replaceChildren();
    $('effort-select').append(new Option('随 CLI 配置', ''));
    const names = {none:'无', minimal:'最低', low:'低', medium:'中', high:'高', xhigh:'更高', max:'最高（Max）', ultra:'极限（Ultra）'};
    for (const item of selected?.supportedReasoningEfforts || []) {
      const value = typeof item === 'string' ? item : item.reasoningEffort;
      if (!value) continue;
      const option = new Option(names[value] || value, value); option.title = item.description || value; $('effort-select').append(option);
    }
    const values = [...$('effort-select').options].map(option => option.value);
    const wanted=state?.agent?.protocol==='acp'?state.agent.currentEffort:old;
    $('effort-select').value = selected && values.includes(wanted) ? wanted : '';
    $('effort-select').disabled = pending || busy() || !selected || $('effort-select').options.length < 2;
  }
  function renderModels() {
    const signature = JSON.stringify([state?.agent?.id,state?.agent?.state,state?.agent?.currentModel,state?.agent?.currentEffort,models()]);
    if (signature === modelKey) return;
    modelKey = signature;
    const changedAgent = modelAgentId !== state?.agent?.id;
    modelAgentId = state?.agent?.id;
    const previous = changedAgent ? '' : $('model-select').value;
    if(changedAgent)$('effort-select').value='';
    $('model-select').replaceChildren(new Option(state?.agent?.state === 'connected' ? '使用 Agent 默认模型' : '连接后读取模型', ''));
    for (const model of models()) { const option = new Option(model.displayName || modelId(model), modelId(model)); option.title = model.description || modelId(model); $('model-select').append(option); }
    const wanted=state?.agent?.protocol==='acp'?state.agent.currentModel:previous;
    $('model-select').value = models().some(model => modelId(model) === wanted) ? wanted : '';
    effortOptions();
  }
  function controls() {
    $('omni-settings').disabled=pending||!online;
    $('omni-check').disabled=pending||!online;
    const connected = online && state?.agent?.state === 'connected';
    const hasModels = models().length > 0 || state?.agent?.protocol === 'acp';
    const working = busy();
    const connecting=pendingAction==='/api/connect'||state?.agent?.state==='connecting';
    const submitting=pendingAction==='/api/send';
    const connectionState=connecting&&!connected?'connecting':state?.agent?.state;
    $('composer-agent').dataset.state=connectionState||'disconnected';
    $('composer-agent').disabled=pending||working;
    text($('connection-label'),(state?.agent?.name ? state.agent.name+' · ' : '')+(labels[connectionState]||'未连接'));$('connection-dot').className='status-dot '+(connectionState||'disconnected');
    text($('connect-button'),connecting?'连接中…':connected?'重连':'连接');text($('welcome-connect'),connecting?'正在连接…':'连接 Agent');
    $('send-button').disabled = pending || !connected || !hasModels || working || !$('prompt').value.trim();
    $('interrupt-button').hidden = !working&&!submitting;
    $('interrupt-button').disabled = controlRequests.has('/api/interrupt') || !online || state?.current_session?.status === 'stopping';
    $('model-select').disabled = pending || working || !connected || !models().length;
    $('effort-select').disabled = pending || working || !connected || !$('model-select').value || $('effort-select').options.length < 2;
    for (const id of ['new-session','workspace-button']) $(id).disabled = pending || working || !online;
    for (const id of ['add-media','attach-button','welcome-media','refresh-artifacts']) $(id).disabled = pending || !online;
    for (const id of ['connect-button','welcome-connect','connection-settings','connection-submit']) $(id).disabled = pending || working || state?.agent?.state === 'connecting';
    document.querySelectorAll('.session-item').forEach(button => { button.disabled = pending || working; });
    document.querySelectorAll('dialog button[type="submit"],#browse-media,#browse-media-folder,#browse-workspace').forEach(button => { button.disabled = pending; });
    $('connection-submit').disabled=pending||working||state?.agent?.state==='connecting';
    $('omni-save').disabled=pending||!online||working;
    $('mode-select').disabled=pending||working||!connected;
    document.querySelectorAll('.approval-card').forEach(card=>card.querySelectorAll('.approval-actions button').forEach(button=>{button.disabled=!online||controlRequests.has('/api/approval:'+card.dataset.approvalId);}));
    document.querySelectorAll('.row-actions button,.artifact-row button').forEach(button=>{button.disabled=pending||!online;});
    let status = '填写要求后执行制作任务';
    if (!online) status = state ? '本地服务连接中断，正在重试…' : '正在读取本地服务状态…';
    else if (!connected) status = connecting ? '正在连接本机执行引擎…' : '连接执行引擎后即可开始';
    else if (!hasModels) status = '未获取到可用模型，请重新连接';
    else if (working) status = labels[state.current_session.status] || state.current_session.status;
    else if (pending) status = submitting?'正在提交要求…':connecting?'正在连接本机 Agent…':'正在处理请求…';
    text($('composer-status'), status);
    text($('workspace-status'),working?(labels[state.current_session.status]||'正在执行'):state?.current_session?.turn_status==='completed'?'本轮已完成':'等待任务');
    text($('send-label'),submitting?'正在提交':'执行任务');
    backendControls();
  }
  const backendCatalog = () => state?.backends?.catalog || [];
  const backendName = id => {const item=backendCatalog().find(entry=>entry.id===id);return item?.short_name||item?.name||id;};
  const copySelection = value => JSON.parse(JSON.stringify(value || {mode:'workflow',roles:{editing:[],effects:[],color:[],audio:[]},single_backend:null}));
  const roleAvailability = (item,role) => item.role_availability?.[role] || {selectable:!!item.selectable&&(item.admitted_roles||item.roles||[]).includes(role),label:item.availability?.label||'未适配',reason:item.availability?.reason||'该功能尚未适配'};
  function setWorkspaceView(view) {
    workspaceView=view;
    if(view!=='software'){backendOpen=false;backendDraft=null;backendError='';backendRenderKey='';}
    syncWorkspaceView();
  }
  function syncWorkspaceView() {
    const software=backendOpen;
    if(software)workspaceView='software';
    else if(workspaceView==='software')workspaceView='workspace';
    $('workspace-view').hidden=workspaceView!=='workspace';
    $('records-view').hidden=workspaceView!=='records';
    for(const [id,view] of [['view-workspace','workspace'],['backend-toggle','software'],['view-records','records']]){
      $(id).classList.toggle('active',workspaceView===view);
      $(id).setAttribute('aria-current',workspaceView===view?'page':'false');
    }
    document.querySelector('.conversation')?.classList.toggle('records-open',workspaceView==='records');
  }
  function backendSummary(selection) {
    if(!selection)return state?.backends?'沿用会话工程':'沿用现有工程 · 服务未提供软件配置';
    if(selection.mode==='single')return '单独调用 · '+backendName(selection.single_backend);
    return (state?.backends?.roles||[]).map(role=>{const ids=selection.roles?.[role.id]||[];return ids.length?role.label+' · '+ids.map(backendName).join(' + '):'';}).filter(Boolean).join('  /  ')||'尚未选择软件';
  }
  function normalizedBackendDraft() {
    const selection=copySelection(backendDraft);
    if(selection.mode==='single')selection.roles={};
    else selection.single_backend=null;
    return selection;
  }
  function backendDraftValid() {
    if(!backendDraft)return false;
    if(backendDraft.mode==='single')return backendCatalog().some(item=>item.id===backendDraft.single_backend&&item.selectable);
    const selected=Object.entries(backendDraft.roles||{}).flatMap(([role,ids])=>ids.map(id=>({role,id})));
    return selected.length>0&&selected.every(({role,id})=>backendCatalog().some(item=>item.id===id&&roleAvailability(item,role).selectable));
  }
  function backendControls() {
    const locked=pending||busy()||!online||!state?.backends;
    $('backend-toggle').disabled=!state?.backends;
    document.querySelectorAll('.backend-control').forEach(control=>{control.disabled=locked||control.dataset.unavailable==='true';});
    $('backend-save').disabled=locked||!backendDraftValid();
    $('backend-cancel').disabled=pending;
    $('backend-refresh').disabled=pending||!online;
    text($('backend-save'),pendingAction==='/api/backends/configure'?'正在保存…':'保存配置');
    text($('backend-save-note'),busy()?'当前任务进行中，结束后可调整软件配置。':!online?'本地服务连接中断，恢复后可保存。':'保存到当前任务，并作为新任务的初始配置。');
  }
  function openBackendPanel() {
    if(!state?.backends)return;
    backendOpen=true;workspaceView='software';backendError='';backendDraft=copySelection(state.backends.selection||state.backends.default_selection);backendRenderKey='';renderBackends();
  }
  function cancelBackendPanel() {if(pending)return;backendOpen=false;backendDraft=null;backendError='';renderBackends();$('prompt').focus();}
  function changeBackendMode(mode) {
    if(!backendDraft||pending||busy())return;
    backendDraft.mode=mode;
    if(mode==='single'&&!backendCatalog().some(item=>item.id===backendDraft.single_backend&&item.selectable))backendDraft.single_backend=backendCatalog().find(item=>item.selectable)?.id||null;
    backendError='';renderBackends();
  }
  async function saveBackendSelection() {
    if(pending||busy()||!online||!backendDraftValid())return null;
    const selection=normalizedBackendDraft();backendError='';
    return perform(async generation=>{
      try {
        const next=await fetchAPI('/api/backends/configure','POST',{selection});
        if(generation===epoch){backendOpen=false;backendDraft=null;backendRenderKey='';applyResponse(next,generation);$('prompt').focus();}
        return next;
      } catch(error) {if(generation===epoch){backendError=error.message||'保存失败，请重试。';renderBackends();}return null;}
    },'','/api/backends/configure');
  }
  function callSingleBackend(id) {
    if(pending||busy()||!online||!backendCatalog().some(item=>item.id===id&&item.selectable))return;
    backendOpen=true;backendDraft={mode:'single',roles:{},single_backend:id};backendRenderKey='';renderBackends();return saveBackendSelection();
  }
  function renderBackends() {
    const context=JSON.stringify([state?.workspace,state?.current_session?.id]);
    if(backendContext!==context){backendContext=context;backendOpen=false;backendDraft=null;backendError='';backendRenderKey='';}
    text($('backend-summary'),backendSummary(state?.backends?.selection));
    document.querySelector('.conversation')?.classList.toggle('backend-config-open',backendOpen);
    $('backend-summary').title=backendSummary(state?.backends?.selection);
    $('backend-panel').hidden=!backendOpen;
    $('backend-toggle').setAttribute('aria-expanded',String(backendOpen));
    text($('backend-toggle-label'),backendOpen?'软件配置已打开':'打开软件配置');
    syncWorkspaceView();
    const error=backendError||state?.backends?.error||'';
    $('backend-error').hidden=!error;text($('backend-error'),error);
    if(backendOpen&&backendDraft){
      const single=backendDraft.mode==='single';
      $('backend-role-list').hidden=single;$('backend-single-choice').hidden=!single;
      for(const mode of ['workflow','single'])$('backend-mode-'+mode).setAttribute('aria-pressed',String(backendDraft.mode===mode));
      const signature=JSON.stringify([state.backends.catalog,state.backends.roles,backendDraft]);
      if(signature!==backendRenderKey){
        const focusId=document.activeElement?.id;
        backendRenderKey=signature;const roles=$('backend-role-list');roles.replaceChildren();
        for(const role of state.backends.roles||[]){
          const row=node('fieldset','backend-role');const legend=node('legend','',role.label);row.append(legend);const choices=node('div','backend-role-options');
          if(role.description)legend.title=role.description;
          if(role.id==='editing'){const none=node('label','backend-option');const input=node('input','backend-control');input.id='backend-editing-none';input.type='radio';input.name='backend-role-'+role.id;input.checked=!(backendDraft.roles?.[role.id]||[]).length;input.setAttribute('aria-label',role.label+'：不使用');input.addEventListener('change',()=>{backendDraft.roles={...backendDraft.roles,[role.id]:[]};backendError='';renderBackends();});none.append(input,node('span','','不使用'));choices.append(none);}
          for(const item of backendCatalog().filter(entry=>entry.roles?.includes(role.id))){
            const availability=roleAvailability(item,role.id);
            const option=node('label','backend-option');const input=node('input','backend-control');input.id='backend-'+role.id+'-'+item.id;input.type=role.id==='editing'?'radio':'checkbox';input.name='backend-role-'+role.id;input.value=item.id;input.checked=(backendDraft.roles?.[role.id]||[]).includes(item.id);input.setAttribute('aria-label',role.label+'：'+item.name);
            input.dataset.unavailable=String(!availability.selectable&&!(input.type==='checkbox'&&input.checked));option.classList.toggle('is-unavailable',!availability.selectable);
            input.addEventListener('change',()=>{if(pending||busy())return;if(!availability.selectable&&input.checked){input.checked=false;return;}const previous=backendDraft.roles?.[role.id]||[];backendDraft.roles={...backendDraft.roles,[role.id]:role.id==='editing'?[item.id]:input.checked?[...new Set([...previous,item.id])]:previous.filter(id=>id!==item.id)};backendError='';renderBackends();});
            option.append(input,node('span','software-monogram software-'+item.id,({resolve:'DR',premiere:'Pr','after-effects':'Ae',jianying:'剪'})[item.id]||''),node('span','',item.short_name||item.name));option.append(node('span','backend-option-state',availability.selectable?'已接入':availability.label||'不可用'));option.title=availability.selectable?item.status_label+' · '+(item.summary||''):availability.reason||'当前不可用';choices.append(option);
          }
          if(!choices.children.length)choices.append(node('span','muted','暂无已接入软件'));row.append(choices);roles.append(row);
        }
        const select=$('backend-single-select');select.replaceChildren(new Option('选择软件',''));
        for(const item of backendCatalog()){const option=new Option(item.name+' · '+(item.availability?.label||'状态未知')+' · '+item.status_label,item.id);option.disabled=!item.selectable;select.append(option);}select.value=backendDraft.single_backend||'';
        const catalog=$('backend-catalog');catalog.replaceChildren();
        for(const item of backendCatalog()){
          const article=node('article','backend-software');article.dataset.backendId=item.id;article.classList.toggle('is-unavailable',!item.selectable);const heading=node('div','backend-software-heading');const name=node('strong','',item.name);const status=node('span','backend-status',item.status_label||item.status);heading.append(node('span','software-monogram software-'+item.id,({resolve:'DR',premiere:'Pr','after-effects':'Ae',jianying:'剪'})[item.id]||''),name,status);article.append(heading,node('p','backend-software-roles',(state.backends.roles||[]).filter(role=>item.roles?.includes(role.id)).map(role=>role.label).join(' / ')),node('p','backend-software-summary',item.summary));
          const availability=item.availability||{};const detection=node('p','backend-detection'+(item.selectable?' is-available':' is-unavailable'),(availability.label||'状态未知')+(availability.reason?' · '+availability.reason:''));detection.title=availability.path||'';article.append(detection);
          if(item.limitations?.length){const details=node('details','backend-limit-details');details.append(node('summary','','能力范围与限制'));const list=node('ul','backend-limit');for(const limitation of item.limitations)list.append(node('li','',limitation));details.append(list);article.append(details);}
          const call=node('button','backend-control backend-single-action',item.single_supported?'单独调用 ↗':'暂不支持单独调用');call.type='button';call.dataset.unavailable=String(!item.selectable);call.title=item.selectable?'单独调用'+item.name:availability.reason||'当前不可用';call.setAttribute('aria-label','单独调用'+item.name);call.addEventListener('click',()=>callSingleBackend(item.id));article.append(call);catalog.append(article);
        }
        if(focusId&&focusId.startsWith('backend-'))$(focusId)?.focus({preventScroll:true});
      }
    }
    backendControls();
  }
  function renderSessions() {
    const list = $('session-list');
    const signature = JSON.stringify([state.sessions, state.current_session?.id]);
    if (list.dataset.signature === signature) return;
    list.dataset.signature = signature; const scroll = list.scrollTop; list.replaceChildren();
    text($('session-count'), state.sessions?.length || '');
    if (!state.sessions?.length) list.append(node('p','muted list-empty','暂无制作任务。新建任务后添加素材和要求。'));
    for (const session of state.sessions || []) {
      const button = node('button','session-item' + (session.id === state.current_session?.id ? ' active' : ''));
      button.type = 'button'; button.title = session.title || '未命名会话';
      button.append(node('span','session-name',(session.title || '未命名会话')+' · '+(session.agent_id||'codex')));
      if (session.updated_at) { const date = new Date(typeof session.updated_at === 'number' && session.updated_at < 1e12 ? session.updated_at * 1000 : session.updated_at); if (!Number.isNaN(date.valueOf())) button.append(node('span','session-date',date.toLocaleString('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}))); }
      button.addEventListener('click', async()=>{if(await mutate('/api/session/select',{id:session.id}))await prepareACP();}); list.append(button);
    }
    list.scrollTop = scroll;
  }
  function renderMessages() {
    const scroll = $('messages-scroll'), container = $('messages');
    const newSession = sessionKey !== (state.current_session?.id || '');
    const follow = newSession || scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 75;
    if (newSession) { sessionKey = state.current_session?.id || ''; container.replaceChildren(); }
    const messages = state.current_session?.messages || [];
    const existing = new Map([...container.children].map(el => [el.dataset.id,el]));
    messages.forEach((message, index) => {
      const key = String(message.id ?? index); let item = existing.get(key);
      if (!item) { item = node('article','message'); item.dataset.id = key; const heading = node('div','message-heading'); heading.append(node('span','message-avatar'),node('span','message-role')); item.append(heading,node('div','message-text')); }
      item.className = 'message ' + (['user','assistant','system'].includes(message.role) ? message.role : 'system');
      const role = message.role === 'user' ? '任务要求' : message.role === 'assistant' ? '执行反馈' : '系统记录';
      text(item.querySelector('.message-avatar'), message.role === 'assistant' ? '剪' : message.role === 'user' ? '你' : '·');
      text(item.querySelector('.message-role'),role); text(item.querySelector('.message-text'),message.text);
      if (container.children[index] !== item) container.insertBefore(item,container.children[index] || null);
      existing.delete(key);
    });
    existing.forEach(item => item.remove());
    $('welcome').hidden = messages.length > 0;
    text($('record-count'),messages.length||'');
    document.querySelector('.conversation').classList.toggle('home',messages.length === 0);
    $('welcome-connect').hidden = state.agent?.state === 'connected';
    $('running-indicator').hidden = !busy();
    text($('running-label'),labels[state.current_session?.status] || '正在执行');
    if (follow) scroll.scrollTop = scroll.scrollHeight;
  }
  function bytes(size) { if (!Number.isFinite(size)) return ''; if (size < 1024) return size + ' B'; if (size < 1048576) return (size / 1024).toFixed(1) + ' KB'; return (size / 1048576).toFixed(1) + ' MB'; }
  function toggleViewerSize(){
    if($('viewer-fit').disabled)return;
    const actual=$('media-preview').classList.toggle('actual-size');
    $('viewer-fit').setAttribute('aria-pressed',String(actual));
    text($('viewer-fit'),actual?'100%':'适合窗口');
    $('viewer-fit').title=actual?'适合窗口显示':'查看原始像素大小';
  }
  function localMediaURL(media){
    try{const url=new URL(media.url,location.origin);if(url.origin!==location.origin||!url.pathname.startsWith('/api/media/'))return null;url.searchParams.set('token',token);return url.href;}catch(_){return null;}
  }
  function preview() {
    const media = state.media?.find(item => item.id === selectedMedia);
    const key = media ? JSON.stringify([media.id,media.url,media.kind]) : '';
    if (key === previewKey) return;
    previewKey = key; const panel = $('media-preview'); panel.replaceChildren();
    panel.classList.remove('actual-size');
    const zoom=$('viewer-fit');zoom.disabled=true;zoom.setAttribute('aria-pressed','false');text(zoom,'适合窗口');zoom.title='查看原始像素大小';
    text($('preview-kind'),({image:'图像',video:'视频',audio:'音频',folder:'文件夹'})[media?.kind]||'');
    text($('preview-size'),media?.kind==='folder'?'按需读取':bytes(media?.size));
    text($('preview-caption'),media?.name || '尚未添加素材'); $('preview-caption').title = media?.path || '';
    if (!media) { const empty = node('div','preview-empty'); empty.append(icon('film'),node('span','','选择素材查看预览')); panel.append(empty); return; }
    if(media.kind==='folder'){
      const folder=node('div','preview-empty');folder.append(icon('folder'),node('strong','','文件夹已引用'),node('span','','Agent 将按需查看目录中的内容，不预先导入全部文件。'));panel.append(folder);return;
    }
    const url=localMediaURL(media);
    if(!url){panel.append(node('div','preview-empty','素材预览地址不可用'));return;}
    const element = node(media.kind === 'image' ? 'img' : media.kind === 'audio' ? 'audio' : 'video');
    if (media.kind === 'image') element.alt = media.name || '素材预览';
    else { element.controls = true; element.preload = 'metadata'; }
    element.src = url;
    element.addEventListener(media.kind==='image'?'load':'loadedmetadata',()=>{
      if(previewKey!==key)return;
      const width=media.kind==='image'?element.naturalWidth:element.videoWidth;
      const height=media.kind==='image'?element.naturalHeight:element.videoHeight;
      if(width&&height)text($('preview-size'),`${width} × ${height}`);
      else if(media.kind==='audio'&&Number.isFinite(element.duration))text($('preview-size'),`${element.duration.toFixed(1)} 秒`);
      zoom.disabled=media.kind!=='image'||!element.naturalWidth;
    });
    element.addEventListener('error', () => { if (previewKey === key) { panel.replaceChildren(node('div','preview-empty','无法预览此格式；素材路径仍可用于剪辑。')); } });
    panel.append(element);
  }
  function renderMedia() {
    const media = state.media || [];
    text($('stage-media-count'),media.length+' 项素材');
    const summary=$('attached-media');summary.hidden=!media.length;
    text(summary,!media.length?'':media.length===1?`${media[0].kind==='folder'?'文件夹':'素材'} · ${media[0].name}`:`已引用 ${media.length} 项素材 · ${media[0].name}`);
    summary.title='查看已添加的文件和文件夹';
    const context=JSON.stringify([state.workspace,state.current_session?.id]);
    if(context!==mediaContext){mediaContext=context;mediaVisible=100;$('media-feedback').hidden=true;}
    if (!media.some(item => item.id === selectedMedia)) selectedMedia = media[0]?.id || null;
    const signature = JSON.stringify([media,selectedMedia,mediaVisible]); const list = $('media-list');
    if (list.dataset.signature !== signature) {
      $('media-feedback').hidden=true;
      list.dataset.signature = signature; list.replaceChildren(); text($('media-count'),media.length);
      if (!media.length) list.append(node('p','panel-empty','添加视频、图片或音频。原文件保留在本机。'));
      media.slice(0,mediaVisible).forEach(item => {
        const row = node('div','media-row' + (item.id === selectedMedia ? ' selected' : '')); row.tabIndex = 0; row.setAttribute('role','button'); row.setAttribute('aria-label','预览 ' + item.name); row.title = item.path;
        const mark=node('span','file-mark',({video:'视频',image:'图片',audio:'音频',folder:'文件夹'})[item.kind] || '文件');
        const thumbnailURL=item.kind==='image'?localMediaURL(item):null;
        if(thumbnailURL){const thumbnail=node('img','file-thumbnail-image');thumbnail.loading='lazy';thumbnail.decoding='async';thumbnail.alt='';thumbnail.src=thumbnailURL;mark.classList.add('file-thumbnail');mark.replaceChildren(thumbnail);thumbnail.addEventListener('error',()=>{mark.classList.remove('file-thumbnail');text(mark,'图片');});}
        row.append(mark);
        const info = node('div','file-info'); info.append(node('span','file-name',item.name),node('span','file-meta',item.kind==='folder'?'文件夹引用 · 按需读取':bytes(item.size))); row.append(info);
        const actions = node('div','row-actions'); actions.append(actionButton('folder','打开素材所在目录',()=>mutate('/api/reveal',{path:item.path})),actionButton('close','移除素材引用',()=>mutate('/api/media/' + encodeURIComponent(item.id),undefined,'DELETE'))); row.append(actions);
        const choose = () => { selectedMedia = item.id; renderMedia(); };
        row.addEventListener('click',choose); row.addEventListener('keydown',event=>{if(event.target===row && (event.key==='Enter'||event.key===' ')){event.preventDefault();choose();}}); list.append(row);
      });
      if(media.length>mediaVisible){const more=node('button','secondary-button media-more',`已显示 ${mediaVisible} / ${media.length} · 再显示 ${Math.min(100,media.length-mediaVisible)} 项`);more.type='button';more.addEventListener('click',()=>{mediaVisible+=100;renderMedia();});list.append(more);}
    }
    preview();
  }
  function setTab(name){currentTab=name;for(const key of ['media','progress','artifacts']){const active=key===name;$('tab-'+key).classList.toggle('active',active);$('tab-'+key).setAttribute('aria-selected',String(active));$('panel-'+key).hidden=!active;}}
  function showMediaPanel(){setTab('media');$('app').classList.remove('right-collapsed');if(narrow.matches)$('app').classList.add('mobile-inspector');syncInspector();}
  function toggleInspector(){
    if(workspaceView!=='workspace'){
      setWorkspaceView('workspace');renderBackends();
      $('app').classList.remove('right-collapsed');
      if(narrow.matches)$('app').classList.add('mobile-inspector');
    }else $('app').classList.toggle(narrow.matches?'mobile-inspector':'right-collapsed');
    syncInspector();
  }
  async function addSelectedMedia(paths,generation){
    const previous=new Set((state?.media||[]).map(item=>item.id));
    const next=await fetchAPI('/api/media','POST',{paths});
    if(generation!==epoch)return;
    applyResponse(next,generation);
    const added=(state.media||[]).filter(item=>!previous.has(item.id));
    const selected=added[0]||(state.media||[]).find(item=>paths.includes(item.path));
    if(selected)selectedMedia=selected.id;
    mediaVisible=Math.max(mediaVisible,Math.ceil(((state.media||[]).findIndex(item=>item.id===selectedMedia)+1)/100)*100);
    renderMedia();showMediaPanel();
    text($('media-feedback'),added.length===1?`已添加：${added[0].name}`:added.length?`已添加 ${added.length} 项素材`:'所选素材已在列表中，无需重复添加。');
    $('media-feedback').hidden=false;$('media-paths').value='';$('media-dialog').close();
  }
  async function browseMedia(kind){return perform(async generation=>{
    const result=await fetchAPI('/api/dialog','POST',{kind});
    if(result.paths?.length)await addSelectedMedia(result.paths,generation);
    else{text($('media-dialog-status'),'已取消选择，未添加素材。');$('media-dialog-status').hidden=false;}
  });}
  function badge(id,count) { text($(id),count); $(id).hidden = !count; }
  function renderProgress() {
    const session = state.current_session || {}, events = session.events || [], plan = session.plan || [];
    const done = plan.filter(step=>step.status==='completed').length;
    text($('progress-count'),plan.length?`${done}/${plan.length}`:'');$('progress-count').hidden=!plan.length;
    const taskStatus = busy() ? session.status : session.turn_status || session.status;
    text($('task-state'),labels[taskStatus] || '尚未开始');
    const list = $('event-list'), signature = JSON.stringify([session.id,session.current_run_id,events,plan,taskStatus]);
    if (list.dataset.signature === signature) return;
    const sameSession=list.dataset.session===(session.id || '');
    const expanded=sameSession && (list.querySelector('details')?.open || false);
    list.dataset.session=session.id || '';
    list.dataset.signature = signature; const scroll = sameSession?list.scrollTop:0; list.replaceChildren();
    const row = event => { const item = node('div','event-item'); const category = ['completed','succeeded','done','complete'].includes(event.status) ? 'done' : ['failed','error'].includes(event.status) ? 'failed' : ['running','inProgress'].includes(event.status) ? 'running' : ''; item.append(node('span','event-dot ' + category)); const body = node('div'); body.append(node('div','event-label',event.label || event.step),node('div','event-status',labels[event.status] || event.status)); item.append(body); return item; };
    if(plan.length) for(const step of plan)list.append(row(step));
    else list.append(node('p','panel-empty',busy()?'正在处理剪辑任务，等待 Agent 更新阶段。':session.turn_status==='completed'?'本轮任务已结束，成果见「成果」页。':'暂无阶段计划。'));
    const visible=events.filter(event=>event.type!=='reasoning'&&event.label!=='分析要求');
    const current=visible.filter(event=>!session.current_run_id||event.run_id===session.current_run_id);
    const errors=current.filter(event=>['error','failed'].includes(event.status));
    if(errors.length){list.append(node('p','progress-caption',`执行中有 ${errors.length} 项异常，详见执行记录。`));list.append(row(errors.at(-1)));}
    const latest=current.at(-1);
    if(busy()&&latest&&!errors.includes(latest))list.append(row({...latest,label:'当前活动 · '+latest.label}));
    if(events.length){
      const details=node('details','execution-details');details.open=expanded;
      details.append(node('summary','',`查看执行记录（${visible.length} 项）`));
      if(!visible.length)details.append(node('p','panel-empty','内部分析记录已合并，不计为任务阶段。'));
      for(const event of visible)details.append(row(event));
      list.append(details);
    }
    list.scrollTop = scroll;
  }
  function renderArtifacts() {
    const artifacts = state.artifacts || [], delivered=artifacts.filter(item=>item.category==='deliverable');
    badge('artifact-count',delivered.length);
    const list = $('artifact-list'), signature = JSON.stringify([state.current_session?.id,artifacts]);
    if (list.dataset.signature === signature) return;
    const sameSession=list.dataset.session===(state.current_session?.id || '');
    const expanded=new Set(sameSession?[...list.querySelectorAll('details[open]')].map(item=>item.dataset.category):[]);
    list.dataset.session=state.current_session?.id || '';
    list.dataset.signature = signature; list.replaceChildren();
    const rows=(items,target)=>{
      const groups=new Map();for(const item of items){const project=item.project||'';if(!groups.has(project))groups.set(project,[]);groups.get(project).push(item);}
      for(const [project,entries] of groups){
        if(project)target.append(node('h3','artifact-project-title',project));
        for(const artifact of entries){
          const row=node('div','artifact-row');row.title=artifact.path;
          const kind=({draft:'草稿',video:'视频',audio:'音频',image:'图片'})[artifact.type] || artifact.type || '文件';
          row.append(node('span','file-mark',kind.slice(0,6)));
          const info=node('div','file-info');info.append(node('span','file-name',artifact.display_name || artifact.name),node('span','file-meta',artifact.path));
          const view=node('button','text-button','查看');view.type='button';view.addEventListener('click',()=>showArtifact(artifact));
          row.append(info,view,actionButton('folder',artifact.bundle?'打开完整草稿目录':'打开文件所在目录',()=>mutate('/api/reveal',{path:artifact.path})));target.append(row);
        }
      }
    };
    if(!delivered.length)list.append(node('p','panel-empty','尚未登记交付成果。项目资料和过程文件可在下方展开查看。'));
    rows(delivered,list);
    for(const [category,title] of [['project','项目资料'],['process','过程文件']]){
      const items=artifacts.filter(item=>(item.category || 'project')===category);if(!items.length)continue;
      const details=node('details','artifact-secondary');details.dataset.category=category;details.open=expanded.has(category);
      details.append(node('summary','',`${title} · ${items.length}`));rows(items,details);list.append(details);
    }
  }
  function renderApprovals() {
    const approvals = state.approvals || [], signature = JSON.stringify(approvals);
    $('approvals').hidden = !approvals.length;
    if (signature === approvalsKey) return;
    const drafts=new Map([...$('approvals').querySelectorAll('input[id],textarea[id],select[id]')].map(el=>[el.id,el.value]));
    const focused=$('approvals').contains(document.activeElement)?document.activeElement:null;
    const focusId=focused?.id, selection=focused && 'selectionStart' in focused ? [focused.selectionStart,focused.selectionEnd] : null;
    approvalsKey = signature; $('approvals').replaceChildren();
    for (const approval of approvals) {
      const card = node('section','approval-card');card.dataset.approvalId=approval.id; card.append(node('h3','approval-heading',approval.title || ({command:'确认命令执行',file:'确认文件修改',permissions:'确认权限请求',user_input:'需要你的回答'})[approval.kind] || '需要确认'));
      if (approval.detail) card.append(node('div','approval-detail',typeof approval.detail === 'string' ? approval.detail : JSON.stringify(approval.detail,null,2)));
      if(Array.isArray(approval.changes)&&approval.changes.some(change=>change.diff)){
        const details=node('details','approval-diff');details.append(node('summary','','查看文件改动'));
        for(const change of approval.changes)if(change.diff)details.append(node('pre','',change.path+'\n'+change.diff));
        card.append(details);
      }
      const fields = [];
      if (approval.kind === 'user_input') for (const [index, question] of (approval.questions || []).entries()) {
        const wrapper = node('div','approval-question'), fieldId = 'question-' + approval.id + '-' + index;
        const label = node('label','', (question.header ? question.header + ' · ' : '') + (question.question || '请补充信息')); label.htmlFor = fieldId; wrapper.append(label);
        let select = null;
        if (question.options?.length) { select = node('select');select.id=fieldId+'-select'; select.setAttribute('aria-label',question.question || '选择回答'); select.append(new Option('选择一项，或在下方填写','')); for (const option of question.options) { const value = typeof option === 'string' ? option : option.label; const el = new Option(value,value); el.title = option.description || ''; select.append(el); }if(drafts.has(select.id))select.value=drafts.get(select.id); wrapper.append(select); }
        const input = node(question.isSecret ? 'input' : 'textarea'); if (question.isSecret) input.type = 'password'; else input.rows = 2; input.id = fieldId; input.placeholder = '自由填写'; wrapper.append(input);
        if(drafts.has(fieldId))input.value=drafts.get(fieldId);fields.push({id:question.id || String(index),input,select}); card.append(wrapper);
      }
      const actions = node('div','approval-actions');
      const decline = node('button','secondary-button','拒绝'); decline.type = 'button'; decline.title = '拒绝此次请求'; decline.addEventListener('click',()=>mutate('/api/approval',{id:approval.id,decision:'decline'}));
      const accept = node('button','primary-button',approval.kind === 'user_input' ? '提交回答' : '允许本次'); accept.type = 'button'; accept.title = approval.kind === 'user_input' ? '将本次回答发送给 Agent' : '仅批准当前请求';
      accept.addEventListener('click',()=>{const payload={id:approval.id,decision:'accept'}; if(approval.kind==='user_input'){payload.answers=Object.create(null);for(const field of fields){const answer=field.input.value.trim()||field.select?.value||'';if(!answer){setError('请回答所有问题后再提交。');field.input.focus();return;}payload.answers[field.id]={answers:[answer]};}}mutate('/api/approval',payload);});
      actions.append(decline,accept);
      if(approval.allow_session){const sessionAllow=node('button','secondary-button','本会话允许这些文件');sessionAllow.type='button';sessionAllow.title='由 Agent 记住这些文件的批准；仅对当前会话有效';sessionAllow.addEventListener('click',()=>mutate('/api/approval',{id:approval.id,decision:'acceptForSession'}));actions.append(sessionAllow);}
      card.append(actions); $('approvals').append(card);
    }
    if(focusId&&$(focusId)){$(focusId).focus({preventScroll:true});if(selection)$(focusId).setSelectionRange(...selection);}
  }
  function render() {
    const agent = state.agent || {};
    text($('composer-agent-name'),agent.name || (state.agents||[]).find(a=>a.id===agent.id)?.name || '选择 Agent');
    text($('connection-label'),labels[agent.state] || agent.state || '未连接'); $('connection-label').title = agent.error || agent.version || '';
    $('connection-dot').className = 'status-dot ' + (agent.state || 'disconnected');
    text($('connect-button'),agent.state === 'connected' ? '重连' : agent.state === 'connecting' ? '连接中' : '连接');
    text($('session-title'),state.current_session?.title || '新建任务');
    text($('workspace-path'),state.workspace || '选择工作区，添加素材，然后描述剪辑要求。'); $('workspace-path').title = state.workspace || '';
    text($('workspace-name'),state.workspace ? state.workspace.replace(/[\\/]$/,'').split(/[\\/]/).pop() : '尚未选择'); $('workspace-button').title = state.workspace || '选择工作区目录';
    text($('connection-detail'),agent.error || [agent.version,agent.cli_path].filter(Boolean).join(' · ') || '使用本机 CLI 的账号、模型与权限设置。');
    $('omni-settings').title=state.omni?.message||'配置原生音视频分析服务';
    renderModels(); renderSessions(); renderMessages(); renderMedia(); renderProgress(); renderArtifacts(); renderApprovals(); renderBackends(); controls();
    const modes=agent.modes||[],mode=$('mode-select');const modeSignature=JSON.stringify([modes,agent.currentMode]);
    if(mode.dataset.signature!==modeSignature){mode.dataset.signature=modeSignature;mode.replaceChildren(...modes.map(item=>{const option=new Option(item.name||item.id,item.id);option.title=item.description||item.id;return option;}));mode.value=agent.currentMode||'';}
    mode.hidden=!modes.length;
    if (agent.error) setError(agent.error); else if (state.current_session?.error) setError(state.current_session.error);
  }
  function showDialog(id) { if (!$(''+id).open) $(id).showModal(); }
  function renderOmniStatus(value, saved=false) {
    text($('omni-status'),(saved?'已保存。':'')+(value?.message||'尚未配置视听API。'));
    $('omni-status').dataset.ready=String(!!value?.ready);
    text($('omni-key-status'),value?.credential_source==='saved'?'密钥已加密保存；留空可保留。':value?.credential_source==='environment'?'已使用本机环境变量中的密钥。':'尚未提供密钥。');
  }
  function openOmniSettings() {
    const cfg=state?.omni||{};
    $('omni-protocol').value=cfg.protocol||'openai-compatible';$('omni-model').value=cfg.model||'';
    $('omni-options').value=JSON.stringify(cfg.request_options||{},null,2);updateOmniProtocolHint();
    $('omni-url').value=cfg.base_url||'';$('omni-key').value='';
    $('omni-enabled').checked=cfg.enabled??true;$('omni-clear-key').checked=false;
    renderOmniStatus(cfg);showDialog('omni-dialog');
  }
  function updateOmniProtocolHint() {
    text($('omni-protocol-hint'),$('omni-protocol').value==='gemini'?'使用 Gemini 原生 generateContent，例如官方根地址 https://generativelanguage.googleapis.com/v1beta。':'使用服务的 Chat Completions 根地址；模型和服务必须支持实际音视频输入。');
  }
  async function saveOmniSettings(event) {
    event.preventDefault();
    let requestOptions;
    try{requestOptions=JSON.parse($('omni-options').value||'{}');if(!requestOptions||Array.isArray(requestOptions)||typeof requestOptions!=='object')throw new Error();}
    catch(_){setError('高级请求参数必须是有效的 JSON 对象。');return;}
    const payload={protocol:$('omni-protocol').value,model:$('omni-model').value.trim(),request_options:requestOptions,base_url:$('omni-url').value.trim(),api_key:$('omni-key').value,enabled:$('omni-enabled').checked,clear_key:$('omni-clear-key').checked};
    // Remove the secret from the DOM before awaiting the local save; never put it in state.
    $('omni-key').value='';
    const result=await mutate('/api/omni/configure',payload);
    if(result){$('omni-clear-key').checked=false;renderOmniStatus(result.omni,true);}
  }
  function showMedia() { $('media-dialog-status').hidden=true;showDialog('media-dialog'); }
  async function prepareACP(){if(state?.agent?.state==='connected'&&state.agent.protocol==='acp'&&(!state.current_session||state.current_session.agent_id===state.agent.id))return mutate('/api/session/prepare',{});}
  async function connectAgent(options){const result=await mutate('/api/connect',options);if(result)await prepareACP();return result;}
  async function configureACP(options){const result=await mutate('/api/session/configure',options);if(!result){modelKey='';renderModels();}return result;}
  async function showArtifact(artifact){
    text($('artifact-title'),artifact.name);const content=$('artifact-preview');content.replaceChildren();showDialog('artifact-dialog');
    if(artifact.preview_kind==='file'){
      content.append(node('p','muted',`这是${artifact.type||'本地成果'}文件，请在对应软件中打开或导入。可打开所在目录查看。`));
      content.append(actionButton('folder','打开工程所在目录',()=>mutate('/api/reveal',{path:artifact.path})));
      return;
    }
    if(artifact.preview_kind==='text')return perform(async()=>{const result=await fetchAPI(artifact.url);content.append(node('pre','artifact-text',result.text));});
    const url=new URL(artifact.url,location.origin);if(url.origin!==location.origin||!url.pathname.startsWith('/api/artifacts/')){setError('成果预览地址无效。');return;}
    url.searchParams.set('token',token);const media=node(artifact.preview_kind==='image'?'img':artifact.preview_kind==='audio'?'audio':'video');
    if(artifact.preview_kind==='image')media.alt=artifact.name;else{media.controls=true;media.preload='metadata';}media.src=url.href;content.append(media);
  }
  $('dismiss-error').addEventListener('click',clearError);
  $('backend-toggle').addEventListener('click',()=>{if(backendOpen)cancelBackendPanel();else openBackendPanel();});
  $('task-software').addEventListener('click',openBackendPanel);
  $('view-workspace').addEventListener('click',()=>{setWorkspaceView('workspace');renderBackends();});
  $('view-records').addEventListener('click',()=>{setWorkspaceView('records');renderBackends();});
  $('open-progress').addEventListener('click',()=>{setTab('progress');$('app').classList.remove('right-collapsed');if(narrow.matches)$('app').classList.add('mobile-inspector');syncInspector();});
  $('backend-cancel').addEventListener('click',cancelBackendPanel);
  $('backend-refresh').addEventListener('click',()=>mutate('/api/backends/refresh',{}));
  $('backend-mode-workflow').addEventListener('click',()=>changeBackendMode('workflow'));
  $('backend-mode-single').addEventListener('click',()=>changeBackendMode('single'));
  $('backend-single-select').addEventListener('change',()=>{if(!backendDraft||pending||busy())return;backendDraft.single_backend=$('backend-single-select').value||null;backendError='';renderBackends();});
  $('backend-save').addEventListener('click',()=>saveBackendSelection());
  $('backend-panel').addEventListener('keydown',event=>{if(event.key==='Escape'){event.preventDefault();cancelBackendPanel();}});
  document.querySelectorAll('[data-close-dialog]').forEach(button => button.addEventListener('click',()=>button.closest('dialog').close()));
  ['add-media','attach-button','welcome-media'].forEach(id=>$(id).addEventListener('click',showMedia));
  $('attached-media').addEventListener('click',showMediaPanel);
  $('viewer-fit').addEventListener('click',toggleViewerSize);
  $('workspace-button').addEventListener('click',()=>{$('workspace-input').value=state?.workspace||'';showDialog('workspace-dialog');});
  function fillAgentFields() {
    const entry=(state?.agents||[]).find(a=>a.id===$('agent-select').value);
    $('cli-path').value=entry?.command?.[0]||'';
    $('agent-args').value=JSON.stringify(entry?.command?.slice(1)||entry?.default_args||[]);
    $('agent-args').disabled=entry?.protocol==='codex';
    $('agent-advanced').open=!entry?.available;
    text($('agent-status'),entry?.status||'请选择 Agent');
  }
  function showAgents(selected) {
    $('agent-select').replaceChildren(...(state?.agents||[]).map(a=>new Option(a.name+' · '+(a.available?'已找到':'未安装/未找到'),a.id)));
    $('agent-select').value=selected||state?.agent?.id||'codex';fillAgentFields();
  }
  ['connection-settings','composer-agent'].forEach(id=>$(id).addEventListener('click',()=>{showAgents();showDialog('connection-dialog');}));
  $('omni-settings').addEventListener('click',openOmniSettings);
  $('omni-protocol').addEventListener('change',updateOmniProtocolHint);
  $('omni-form').addEventListener('submit',saveOmniSettings);
  $('omni-dialog').addEventListener('close',()=>{$('omni-key').value='';});
  $('omni-check').addEventListener('click',()=>perform(async()=>{renderOmniStatus(await fetchAPI('/api/omni/status'));}));
  $('agent-select').addEventListener('change',fillAgentFields);
  $('rescan-agents').addEventListener('click',async()=>{const selected=$('agent-select').value;if(await mutate('/api/agents/refresh',{}))showAgents(selected);});
  ['connect-button','welcome-connect'].forEach(id=>$(id).addEventListener('click',()=>connectAgent({})));
  $('connection-form').addEventListener('submit',async event=>{event.preventDefault();try{const value=$('cli-path').value.trim();const args=JSON.parse($('agent-args').value||'[]');if(!Array.isArray(args)||args.some(x=>typeof x!=='string'))throw new Error('参数应为JSON文本数组，例如 ["acp"]');if(await connectAgent({agent_id:$('agent-select').value,...(value?{cli_path:value,args}:{})}))$('connection-dialog').close();}catch(error){setError(error);}});
  $('workspace-form').addEventListener('submit',async event=>{event.preventDefault();const path=$('workspace-input').value.trim().replace(/^"|"$/g,'');if(!path){setError('请输入工作区目录。');return;}if(await mutate('/api/workspace',{path})){$('workspace-dialog').close();await prepareACP();}});
  $('media-form').addEventListener('submit',event=>{event.preventDefault();const paths=$('media-paths').value.split(/\r?\n/).map(path=>path.trim().replace(/^"|"$/g,'')).filter(Boolean);if(!paths.length){setError('请选择文件或填写素材路径。');return;}return perform(generation=>addSelectedMedia(paths,generation));});
  $('browse-media').addEventListener('click',()=>browseMedia('files'));
  $('browse-media-folder').addEventListener('click',()=>browseMedia('folder'));
  $('browse-workspace').addEventListener('click',()=>perform(async()=>{const result=await fetchAPI('/api/dialog','POST',{kind:'folder'});if(result.paths?.length)$('workspace-input').value=result.paths[0];}));
  $('new-session').addEventListener('click',async()=>{if(await mutate('/api/sessions',{})){await prepareACP();$('prompt').focus();}});
  $('refresh-artifacts').addEventListener('click',()=>mutate('/api/artifacts/refresh',{}));
  $('interrupt-button').addEventListener('click',()=>mutate('/api/interrupt',{}));
  $('model-select').addEventListener('change',async()=>{const model=$('model-select').value;$('effort-select').value='';effortOptions();controls();if(state?.agent?.protocol==='acp'&&model)await configureACP({model});});
  $('effort-select').addEventListener('change',()=>{if(state?.agent?.protocol==='acp'&&$('effort-select').value)configureACP({effort:$('effort-select').value});});
  $('mode-select').addEventListener('change',()=>{if($('mode-select').value)configureACP({mode:$('mode-select').value});});
  $('artifact-dialog').addEventListener('close',()=>{$('artifact-preview').replaceChildren();});
  $('prompt').addEventListener('input',()=>{const field=$('prompt');field.style.height='auto';field.style.height=Math.min(175,Math.max(79,field.scrollHeight))+'px';controls();});
  $('prompt').addEventListener('keydown',event=>{if(event.key==='Enter'&&(event.ctrlKey||event.metaKey)&&!event.isComposing){event.preventDefault();if(!$('send-button').disabled)$('composer-form').requestSubmit();}});
  $('composer-form').addEventListener('submit',async event=>{event.preventDefault();if($('send-button').disabled)return;const value=$('prompt').value;const result=await mutate('/api/send',{text:value.trim(),model:$('model-select').value||null,effort:$('effort-select').value||null});if(result&&$('prompt').value===value){$('prompt').value='';$('prompt').style.height='79px';}controls();$('prompt').focus();});
  document.querySelectorAll('[data-tab]').forEach(tab=>tab.addEventListener('click',()=>setTab(tab.dataset.tab)));
  const narrow=matchMedia('(max-width:980px)');
  function syncInspector(){const open=narrow.matches?$('app').classList.contains('mobile-inspector'):!$('app').classList.contains('right-collapsed');$('inspector').hidden=!open;$('toggle-inspector').setAttribute('aria-expanded',String(open));$('toggle-inspector').title=open?'折叠右侧面板':'展开右侧面板';}
  $('toggle-inspector').addEventListener('click',toggleInspector);
  const mobileClose=actionButton('close','折叠右侧面板',()=>{$('app').classList.remove('mobile-inspector');syncInspector();});mobileClose.classList.add('mobile-panel-close');document.querySelector('.inspector-tabs').append(mobileClose);
  narrow.addEventListener('change',syncInspector);document.addEventListener('keydown',event=>{if(event.key==='Escape'&&narrow.matches&&!document.querySelector('dialog[open]')){$('app').classList.remove('mobile-inspector');syncInspector();}});syncInspector();
  const resize = $('inspector-resize');
  function setWidth(value) {const width=Math.max(260,Math.min(500,value));document.documentElement.style.setProperty('--inspector-width',width+'px');resize.setAttribute('aria-valuenow',String(Math.round(width)));}
  resize.addEventListener('pointerdown',event=>{if(event.button!==0)return;resize.setPointerCapture(event.pointerId);document.body.classList.add('resizing');});
  resize.addEventListener('pointermove',event=>{if(resize.hasPointerCapture(event.pointerId))setWidth(window.innerWidth-event.clientX);});
  const finishResize=()=>document.body.classList.remove('resizing');resize.addEventListener('pointerup',event=>{if(resize.hasPointerCapture(event.pointerId))resize.releasePointerCapture(event.pointerId);finishResize();});resize.addEventListener('lostpointercapture',finishResize);
  resize.addEventListener('keydown',event=>{if(event.key==='ArrowLeft'||event.key==='ArrowRight'){event.preventDefault();setWidth(Number(resize.getAttribute('aria-valuenow'))+(event.key==='ArrowLeft'?16:-16));}});
  controls();poll();
})();




