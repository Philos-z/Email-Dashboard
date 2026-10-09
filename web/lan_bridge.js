/* Browser controls/status only; Outlook scheduling runs on the Mac backend. */
async function api(path,options={}){
 const r=await fetch(path,{credentials:'same-origin',cache:'no-store',...options,headers:{'Content-Type':'application/json',...(options.headers||{})}});
 if(r.status===401){location.reload();throw Object.assign(new Error('会话已过期，请重新登录'),{status:401})}
 let obj;try{obj=await r.json()}catch{obj={detail:r.statusText}}
 if(!r.ok)throw Object.assign(new Error(typeof obj.detail==='string'?obj.detail:'请求失败'),{status:r.status});
 return obj;
}
function showNet(status){const e=document.getElementById('connectionStatus');if(e)e.textContent=status}
let refreshInProgress=false;
async function refreshFromServer(force=false){
 if(refreshInProgress)return;
 if((state.openId||isSaving)&&!force){deferredSync=true;showNet('其他设备有更新 · 关闭编辑后同步');return}
 refreshInProgress=true;
 try{const s=await api('/api/snapshot');
  if(s.revision!==serverRevision||force){model=s.model;serverRevision=s.revision;itemVersions=s.itemRevisions;closeDrawer();render();updateMcpLast();}
  deferredSync=false;showNet('已同步 · 版本 '+serverRevision);
 }catch(e){showNet('同步中断');toast(e.message)}finally{refreshInProgress=false}
}
let socket=null,retryMs=1000;
function connectSocket(){
 const proto=location.protocol==='https:'?'wss':'ws';socket=new WebSocket(proto+'://'+location.host+'/ws');
 socket.onopen=()=>{retryMs=1000;showNet('已连接 · 版本 '+serverRevision);refreshFromServer()};
 socket.onmessage=e=>{try{const obj=JSON.parse(e.data);if(obj.type==='changed'&&obj.revision>serverRevision){if(obj.source==='item_context')syncItemContext(obj.itemId);else refreshFromServer()}}catch{}};
 socket.onclose=()=>{showNet('实时连接中断 · 正在重连');setTimeout(connectSocket,retryMs);retryMs=Math.min(30000,retryMs*1.6)};
 socket.onerror=()=>socket.close();
}
setInterval(()=>{if(socket?.readyState===WebSocket.OPEN)socket.send('ping');},25000);
// When returning to a tab, refresh in case WebSocket was suspended during sleep.
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshFromServer()});
document.addEventListener('click',e=>{if(e.target.closest('[data-close]')&&deferredSync)setTimeout(()=>refreshFromServer(),60)});
function htmlEsc(s){return esc(String(s??''))}
const modal=document.getElementById('lanBackdrop');
const modalBody=document.getElementById('lanModalBody');
let currentProposalToken='';
function openModal(title,markup){document.getElementById('lanModalTitle').textContent=title;modalBody.innerHTML=markup;modal.hidden=false;}
function closeModal(){modal.hidden=true;currentProposalToken=''}
function updateMcpLast(){
 const last=model.lastInboxUpdate;
 let stamp=last?.date||'';
 if(stamp.includes('T')){const date=new Date(stamp);if(!Number.isNaN(date.getTime()))stamp=date.toLocaleString('zh-CN',{timeZone:model.timezone||'UTC',hour12:false});}
 const label=last?'最近邮件检查：'+stamp+'（'+(model.timezone||'UTC')+'）':'尚无邮件检查记录';
 for(const id of ['mcpLastChecked','mailCheckStatus','mailHistoryCheckStatus']){const el=document.getElementById(id);if(el)el.textContent=label;}
}
let mailStatus=null,mailStatusBusy=false;
function mailTime(value){if(!value)return '尚无记录';const d=new Date(value);return Number.isNaN(d.getTime())?value:d.toLocaleString('zh-CN',{timeZone:model.timezone||'UTC',hour12:false});}
function renderMailStatus(){
 if(!mailStatus)return;
 const s=mailStatus,last=s.runs[0];
 const schedule=s.enabled?(s.quietNow?'夜间暂停中':'每小时自动刷新')+' · 00:00–06:00 暂停 · 下次 '+mailTime(s.nextRunAt)+'（'+(model.timezone||'UTC')+'）':'自动刷新已暂停；仍可立即刷新';
 const scope='关注范围：'+s.scope.join('、');
 const setting=document.getElementById('mailScheduleStatus');if(setting)setting.textContent=schedule+' · '+scope;
 const el=document.getElementById('mailSyncDetails');if(!el)return;
 el.innerHTML=`<div class="lan-note"><strong>${htmlEsc(schedule)}</strong><br>${htmlEsc(scope)}<br>新增范围从 ${htmlEsc(s.historySince)} 起补查${s.backfillPending?'（分批处理中）':''}<br>最近成功：${htmlEsc(mailTime(s.lastSuccessAt))}<br>全部文件夹已检查至：${htmlEsc(mailTime(s.checkedThrough))}</div>
 <p role="status">${htmlEsc(last?.message||'尚未运行，点击立即刷新开始。')}${s.running?'…':''}</p>
 <div class="lan-actions"><button class="btn primary" type="button" data-lan-action="mail-refresh" ${s.running?'disabled':''}>${s.running?'正在刷新…':'立即刷新'}</button><button class="btn" type="button" data-lan-action="mail-schedule">${s.enabled?'暂停自动刷新':'开启每小时刷新'}</button></div>
 <h3>最近检查记录</h3>${s.runs.length?s.runs.map(r=>`<div class="lan-diff"><strong>${r.status==='success'?'已完成':r.status==='failed'?'失败':'进行中'}</strong> · ${r.trigger==='hourly'?'自动':'手动'} · ${htmlEsc(mailTime(r.started_at))}<p>${htmlEsc(r.message)}</p></div>`).join(''):'<p>尚无检查记录。</p>'}`;
}
async function loadMailStatus(){
 if(mailStatusBusy)return;mailStatusBusy=true;
 try{mailStatus=await api('/api/mail/status');renderMailStatus();}
 catch(e){const el=document.getElementById('mailSyncDetails');if(el)el.textContent=e.message;}
 finally{mailStatusBusy=false;}
}
async function openMcp(){
 openModal('邮箱刷新',`<p>后台检查自己的收件箱、存档和已发送邮件。自动刷新每小时运行，所选时区 00:00–06:00 暂停；手动刷新随时可用。新邮件的部分正文及项目摘要会发送至你配置的模型，不读取附件。</p><div class="lan-actions"><button class="btn" data-lan-action="mail-settings">邮箱与 API 设置</button></div><div id="mailSyncDetails" aria-live="polite">正在读取状态…</div>`);
 await loadMailStatus();
}

async function triggerMailRefresh(){
 const button=document.querySelector('[data-lan-action="mail-refresh"]');if(button)button.disabled=true;
 try{const result=await api('/api/mail/refresh',{method:'POST',body:'{}'});toast(result.alreadyRunning?'已有刷新正在进行':'已开始刷新邮件');await loadMailStatus();}
 catch(e){toast(e.message);if(button?.isConnected)button.disabled=false;}
}
async function toggleMailSchedule(){
 if(!mailStatus)return;
 try{mailStatus=await api('/api/mail/schedule',{method:'PUT',body:JSON.stringify({enabled:!mailStatus.enabled})});renderMailStatus();toast(mailStatus.enabled?'已开启每小时刷新':'已暂停自动刷新');}
 catch(e){toast(e.message);}
}
let mailStatusTicks=0;
setInterval(()=>{mailStatusTicks++;if(!document.hidden&&((!modal.hidden&&document.getElementById('mailSyncDetails'))||mailStatusTicks%6===0))loadMailStatus();},5000);
async function showAudit(){
 try{const r=await api('/api/audit?limit=60');openModal('SQLite 数据库修改历史',`<p>历史仅记录操作摘要，不存储完整邮件原文。</p>${r.events.map(x=>`<div class="lan-diff"><small>${htmlEsc(x.changed_at)}</small> · <b>${htmlEsc(x.kind)}</b> · ${htmlEsc(x.item_id)}<p>${htmlEsc(x.details)}</p></div>`).join('')}`)}catch(e){toast(e.message)}
}
async function exportDb(){
 try{const r=await fetch('/api/backup.sqlite3',{credentials:'same-origin'});if(!r.ok)throw Error('下载失败 '+r.status);
 const url=URL.createObjectURL(await r.blob());const a=document.createElement('a');a.href=url;a.download='Progress_Atlas_SQLite_'+ndate()+'.sqlite3';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);toast('SQLite 一致性备份已下载')}
 catch(e){toast('备份失败：'+e.message)}
}
function legacyImport(){
 const keys=['progress-atlas-six-month-v3','progress-atlas-six-month-v2','progress-atlas-six-month-v1'];const entries=[];
 for(const key of keys){try{const raw=localStorage.getItem(key);if(!raw)continue;const obj=JSON.parse(raw);if(obj.items&&Array.isArray(obj.items))entries.push({key,obj});}catch{}}
 if(!entries.length){openModal('迁移旧浏览器编辑','<p>当前浏览器没有检测到旧版 localStorage 数据。如果你之前在另一个浏览器或设备中修改了旧版，请在那台设备里先导出 JSON，然后使用「数据与设置 → 导入 JSON」功能。</p>');return;}
 const content=entries.map((x,i)=>`<div class="lan-diff"><p>${htmlEsc(x.key)} · ${x.obj.items.length} 项 · ${htmlEsc(x.obj.lastSavedAt||'未知更新日期')}</p><button class="btn" data-legacy-index="${i}">导出 JSON</button><button class="btn primary" data-legacy-preview="${i}">安全合并用户备注/里程碑</button></div>`).join('');
 window.__legacyEntries=entries;openModal('旧版浏览器数据迁移',`<p>可以安全合并旧版手动备注、手动里程碑和本地新增事项；已有邮件证据、当前状态不会被覆盖。需要全部替换时才使用「导入 JSON」。</p>${content}<div id="legacyDiff"></div>`)
}
let legacyProposalToken='';
async function showLegacyPreview(data){
 try{const r=await api('/api/legacy/preview',{method:'POST',body:JSON.stringify({model:data})});legacyProposalToken=r.token;
  let el=document.getElementById('legacyDiff');if(!el){openModal('旧版手动修改安全合并','<div id="legacyDiff"></div>');el=document.getElementById('legacyDiff')}
  el.innerHTML=`<div class="lan-note">${htmlEsc(r.notice)}</div><p>${r.items.length} 项备注/里程碑修改；${r.newTitles.length} 个旧版新增项目。</p>${r.items.map(x=>`<div class="lan-diff">${htmlEsc(x.title)} · ${htmlEsc(x.fields.join('、'))}</div>`).join('')}${r.newTitles.map(x=>`<div class="lan-diff">新增：${htmlEsc(x)}</div>`).join('')}<button class="btn primary" data-lan-action="legacy-commit">确认安全合并</button>`;
 }catch(e){toast('旧版合并预览失败：'+e.message)}
}
async function legacyCommit(){if(!legacyProposalToken)return;
 if(!confirm('确认安全合并手动备注和里程碑？现有 Outlook 邮件记录不会被替换。'))return;
 try{const r=await api('/api/legacy/commit',{method:'POST',body:JSON.stringify({token:legacyProposalToken})});closeModal();await refreshFromServer(true);toast('安全合并完成：修改 '+r.changed+' 项，新增 '+r.added+' 项')}
 catch(e){toast('合并失败：'+e.message)}
}
const contextFormFields={editTitle:'title',editCategory:'category',editStatus:'status',editPriority:'priority',editMode:'mode',editDeadline:'deadline',editNext:'next',editBlocker:'blocker',editNotes:'notes'};
function captureContextDraft(){
 const item=model.items.find(it=>it.id===state.openId),values={};
 if(item)for(const [id,key] of Object.entries(contextFormFields)){const el=document.getElementById(id);if(el&&el.value!==String(item[key]??''))values[id]=el.value;}
 const active=document.activeElement;
 return {values,milestone:document.getElementById('newMilestone')?.value||'',focus:active?.id,selectionStart:active?.selectionStart,selectionEnd:active?.selectionEnd,scroll:document.getElementById('drawerBody')?.scrollTop||0};
}
function restoreContextDraft(draft){
 for(const [id,value] of Object.entries(draft.values)){const el=document.getElementById(id);if(el)el.value=value;}
 const milestone=document.getElementById('newMilestone');if(milestone)milestone.value=draft.milestone;
 const active=document.getElementById(draft.focus);if(active){active.focus({preventScroll:true});if(typeof draft.selectionStart==='number'&&['text','textarea'].includes(active.type))active.setSelectionRange(draft.selectionStart,draft.selectionEnd);}
 const body=document.getElementById('drawerBody');if(body)body.scrollTop=draft.scroll;
}
let contextSyncBusy=false,contextStatusBusy=false;
async function syncItemContext(itemId){
 if(contextSyncBusy||isSaving)return;
 if(state.openId&&state.openId!==itemId){deferredSync=true;showNet('项目有更新 · 关闭编辑后同步');return;}
 contextSyncBusy=true;
 try{
  const snapshot=await api('/api/snapshot');
  if(isSaving)return;
  if(state.openId&&state.openId!==itemId){deferredSync=true;return;}
  const openId=state.openId,draft=openId?captureContextDraft():null;
  model=snapshot.model;serverRevision=snapshot.revision;itemVersions=snapshot.itemRevisions;
  render();updateMcpLast();deferredSync=false;
  if(openId){openDrawer(openId);restoreContextDraft(draft);}
  showNet('事件上下文已同步 · 版本 '+serverRevision);
 }catch(e){toast('上下文同步失败：'+e.message)}finally{contextSyncBusy=false;}
}
async function loadItemContextStatus(itemId=state.openId){
 if(!itemId||contextStatusBusy||state.openId!==itemId)return;
 contextStatusBusy=true;
 try{
  const status=await api('/api/items/'+encodeURIComponent(itemId)+'/context-status');
  if(state.openId!==itemId)return;
  const el=document.getElementById('itemContextStatus');if(el)el.textContent=status.message;
  const button=document.querySelector('[data-lan-action="item-context-refresh"]');if(button)button.disabled=['queued','running'].includes(status.status);
  if(status.status==='success'&&status.result_version>itemVersions[itemId])await syncItemContext(itemId);
 }catch(e){const el=document.getElementById('itemContextStatus');if(state.openId===itemId&&el)el.textContent=e.message;}
 finally{contextStatusBusy=false;}
}
async function refreshItemContext(){
 const id=state.openId;if(!id)return;
 const draft=captureContextDraft();if(Object.keys(draft.values).length||draft.milestone.trim())return toast('请先保存变更或添加里程碑，再重新整理');
 try{await api('/api/items/'+encodeURIComponent(id)+'/refresh-context',{method:'POST',body:'{}'});toast('正在重新整理事件上下文和下一步');await loadItemContextStatus(id);}
 catch(e){toast(e.message);}
}
setInterval(()=>{if(!document.hidden&&state.openId)loadItemContextStatus();},2000);
// API credentials are write-only in the browser and never enter the shared project model.
async function openAiSettings(){
 openModal('模型 API 设置','<p id="aiSettingsLoading">正在读取配置…</p>');
 try{
  const config=await api('/api/ai/config');
  if(!document.getElementById('aiSettingsLoading'))return;
  openModal('模型 API 设置',`<p>填写自己的 OpenAI 兼容 API 地址、密钥和模型。支持 HTTPS 服务和本机代理。</p>
  <form id="aiSettingsForm" autocomplete="off">
   <label for="aiBaseUrl">API 地址</label><input class="lan-input" id="aiBaseUrl" type="url" value="${htmlEsc(config.baseUrl)}" required>
   <label for="aiApiKey">API 密钥 ${config.keyConfigured?'· 已保存，留空保留':''}</label><input class="lan-input" id="aiApiKey" type="password" autocomplete="new-password" placeholder="${config.keyConfigured?'留空保留现有密钥':'填写自己的 API 密钥'}">
   <label for="aiModel">模型</label><input class="lan-input" id="aiModel" list="aiModelList" value="${htmlEsc(config.model)}" placeholder="点击加载模型，或输入模型名"><datalist id="aiModelList"></datalist>
   <p>密钥只保存在自己的部署设备中。请输入 API 密钥；管理页面的登录密码不能用于模型调用。</p>
   <div class="lan-actions"><button class="btn" type="button" data-lan-action="ai-models">加载模型</button><button class="btn primary" type="submit">保存配置</button><button class="btn" type="button" data-lan-action="ai-test">保存并测试连接</button><button class="btn" type="button" data-lan-action="open-ai">打开 AI 助手</button></div>
   <p>测试连接会调用模型一次，不附带项目数据。</p><div id="aiSettingsStatus" class="lan-note" role="status">${config.configured?'已配置 '+htmlEsc(config.model)+'；可测试连接。':'填写配置后保存。'}</div>
  </form>`);
 }catch(e){if(document.getElementById('aiSettingsLoading'))openModal('模型 API 设置',`<p>${htmlEsc(e.message)}</p>`)}
}
function aiSettingsStatus(message){const el=document.getElementById('aiSettingsStatus');if(el)el.textContent=message}
function lockAiSettings(busy){document.querySelectorAll('#aiSettingsForm button').forEach(el=>el.disabled=busy)}
async function saveAiSettings(){
 const form=document.getElementById('aiSettingsForm');if(!form)throw Error('请先打开 API 设置');
 const config=await api('/api/ai/config',{method:'PUT',body:JSON.stringify({baseUrl:form.querySelector('#aiBaseUrl').value,apiKey:form.querySelector('#aiApiKey').value,model:form.querySelector('#aiModel').value})});
 if(form.isConnected){form.querySelector('#aiApiKey').value='';form.querySelector('#aiApiKey').placeholder='留空保留现有密钥';form.querySelector('#aiBaseUrl').value=config.baseUrl}
 return config;
}
async function runAiSettings(action){
 lockAiSettings(true);aiSettingsStatus(action==='test'?'正在测试模型连接…':'正在保存配置…');
 try{
  const config=await saveAiSettings();await loadSetupStatus();
  if(action==='models'){
   aiSettingsStatus('正在读取可用模型…');const data=await api('/api/ai/models');
   const list=document.getElementById('aiModelList');if(list)list.innerHTML=data.models.map(name=>`<option value="${htmlEsc(name)}"></option>`).join('');
   aiSettingsStatus('已加载 '+data.models.length+' 个模型。点击模型输入框选择，选择后保存。');
  }else if(action==='test'){
   const data=await api('/api/ai/test',{method:'POST',body:'{}'});
   aiSettingsStatus('连接成功 · '+data.model+' · '+(data.latencyMs/1000).toFixed(1)+' 秒');
  }else aiSettingsStatus('配置已保存'+(config.model?' · '+config.model:'。请选择模型后再次保存。'));
 }catch(e){aiSettingsStatus(e.message)}finally{lockAiSettings(false)}
}
let aiConversation=[],aiRequestBusy=false;
async function openAi(){
 openModal('AI 助手','<p id="aiChatLoading">正在读取模型配置…</p>');
 try{
  const config=await api('/api/ai/config');
  if(!document.getElementById('aiChatLoading'))return;
  if(!config.configured){openModal('AI 助手','<p>请先配置本地 API 地址、密钥和模型。</p><button class="btn primary" type="button" data-lan-action="ai-settings">打开 API 设置</button>');return}
  openModal('AI 助手',`<p>当前模型：<strong>${htmlEsc(config.model)}</strong>。可以分析已保存的项目；邮件通过每小时自动刷新或「刷新邮件」更新。</p>
   <div id="aiConversation" class="lan-ai-conversation" role="log" aria-live="polite"></div>
   <form id="aiChatForm"><label for="aiMessage">你的问题</label><textarea id="aiMessage" maxlength="8000" placeholder="例如：帮我整理这周最优先的三件事" required></textarea>
   <label class="lan-checkbox"><input id="aiIncludeContext" type="checkbox"> 附上当前项目摘要（发送至代理所用模型，不含邮件原文）</label>
   <div class="lan-actions"><button class="btn primary" type="submit" ${aiRequestBusy?'disabled':''}>${aiRequestBusy?'正在回复…':'发送'}</button><button class="btn" type="button" data-lan-action="ai-clear" ${aiRequestBusy?'disabled':''}>清空对话</button><button class="btn" type="button" data-lan-action="ai-settings">API 设置</button></div><p>回复仅供参考，不会修改项目。对话只保留在当前页面中。</p><div id="aiChatStatus" role="status"></div></form>`);
  renderAiConversation();
 }catch(e){if(document.getElementById('aiChatLoading'))openModal('AI 助手',`<p>${htmlEsc(e.message)}</p>`)}
}
function renderAiConversation(){
 const el=document.getElementById('aiConversation');if(!el)return;
 el.innerHTML=aiConversation.map(entry=>`<div class="lan-ai-message ${entry.role==='user'?'lan-ai-user':''}"><strong>${entry.role==='user'?'你':'AI 助手'}</strong><div>${htmlEsc(entry.content)}</div></div>`).join('')||'<p>发送问题即可开始；如需分析项目，请勾选附上项目摘要。</p>';
 el.scrollTop=el.scrollHeight;
}
async function sendAiMessage(){
 if(aiRequestBusy)return;
 const form=document.getElementById('aiChatForm');if(!form)return;
 const message=form.querySelector('#aiMessage').value.trim();if(!message)return;
 const history=aiConversation.slice(-8).map(({role,content})=>({role,content}));
 const includeContext=form.querySelector('#aiIncludeContext').checked;
 aiRequestBusy=true;aiConversation.push({role:'user',content:message});renderAiConversation();
 form.querySelector('#aiMessage').value='';form.querySelectorAll('button').forEach(el=>el.disabled=true);
 document.getElementById('aiChatStatus').textContent='正在等待模型回复…';
 try{
  const data=await api('/api/ai/chat',{method:'POST',body:JSON.stringify({message,history,includeContext})});
  aiConversation.push({role:'assistant',content:data.reply});
  aiConversation=aiConversation.slice(-40);
  const status=document.getElementById('aiChatStatus');if(status)status.textContent='';
 }catch(e){
  aiConversation.pop();
  const draft=document.getElementById('aiMessage');if(draft&&!draft.value)draft.value=message;
  const status=document.getElementById('aiChatStatus');if(status)status.textContent=e.message;else toast('AI 回复失败：'+e.message);
 }finally{
  aiRequestBusy=false;renderAiConversation();
  document.querySelectorAll('#aiChatForm button').forEach(el=>el.disabled=false);
  const send=document.querySelector('#aiChatForm button[type="submit"]');if(send)send.textContent='发送';
 }
}
document.addEventListener('submit',async e=>{
 if(e.target.id==='mailSettingsForm'){e.preventDefault();await saveMailSettings()}
 if(e.target.id==='aiSettingsForm'){e.preventDefault();await runAiSettings('save')}
 if(e.target.id==='aiChatForm'){e.preventDefault();await sendAiMessage()}
});
document.addEventListener('click',async e=>{
 const preview=e.target.closest('[data-legacy-preview]');if(preview){const info=window.__legacyEntries?.[Number(preview.dataset.legacyPreview)];if(info)await showLegacyPreview(info.obj);return}
 const old=e.target.closest('[data-legacy-index]');if(old){const info=window.__legacyEntries?.[Number(old.dataset.legacyIndex)];if(info)download('Progress_Atlas_legacy_'+info.key+'.json',JSON.stringify(info.obj,null,2));return}
 const a=e.target.closest('[data-lan-action]');if(!a)return;
 switch(a.dataset.lanAction){
 case 'item-context-refresh':await refreshItemContext();break;
 case 'open-ai':await openAi();break;
 case 'mail-settings':await openMailSettings();break;
 case 'mail-verify':await verifyMailSettings();break;
 case 'ai-settings':await openAiSettings();break;
 case 'ai-models':await runAiSettings('models');break;
 case 'ai-test':await runAiSettings('test');break;
 case 'ai-clear':if(!aiRequestBusy){aiConversation=[];renderAiConversation()}break;
 case 'open-mcp':await openMcp();break;
 case 'mail-refresh':await triggerMailRefresh();break;
 case 'mail-schedule':await toggleMailSchedule();break;
 case 'close':closeModal();break;
 case 'audit':await showAudit();break;
 case 'sqlite-backup':await exportDb();break;
 case 'migrate':legacyImport();break;
 case 'legacy-commit':await legacyCommit();break;
 case 'logout':await api('/api/logout',{method:'POST',body:'{}'});location.reload();break;
 }
});
updateMcpLast();loadMailStatus();connectSocket();
loadSetupStatus();

async function loadSetupStatus(){
 try{
  const [mail, ai] = await Promise.all([api('/api/mail/config'), api('/api/ai/config')]);
  const ready = mail.verified && mail.shareWithModel && ai.configured;
  const banner = document.getElementById('setupBanner');
  if(banner){banner.hidden=ready;banner.querySelector('p').textContent=mail.email?'邮箱 '+mail.email+' · '+(mail.verified?'已验证':'待验证')+' · '+(ai.configured?'模型已配置':'待配置模型'):'连接自己的 Outlook 或 Gmail，再填写 API 和模型。自动刷新默认暂停。';}
  const status = document.getElementById('accountSetupStatus');
  if(status)status.textContent=ready?'设置完成 · '+mail.provider+' · '+mail.email+' · '+mail.timeZone:'尚未完成设置，点击下方按钮开始。';
 }catch(e){toast(e.message)}
}
async function openMailSettings(){
 openModal('邮箱与 API 设置','<p id="mailSettingsLoading">正在读取设置…</p>');
 try{
  const [mail, ai] = await Promise.all([api('/api/mail/config'), api('/api/ai/config')]);
  if(!document.getElementById('mailSettingsLoading'))return;
  const locked=mail.locked?'disabled':'';
  openModal('邮箱与 API 设置',`<p>每人自行部署，数据由自己的设备保存。在本机 Codex / ChatGPT 中连接自己的邮箱后，填写同一个邮箱地址。</p>
  <form id="mailSettingsForm" autocomplete="off">
   <label for="mailProvider">邮箱服务</label><select class="lan-input" id="mailProvider" ${locked}><option value="outlook" ${mail.provider==='outlook'?'selected':''}>Outlook</option><option value="gmail" ${mail.provider==='gmail'?'selected':''}>Gmail</option></select>
   <label for="mailEmail">自己的邮箱地址</label><input class="lan-input" id="mailEmail" type="email" required value="${htmlEsc(mail.email)}" placeholder="you@example.com" ${locked}>
   <label for="mailTimeZone">时区</label><input class="lan-input" id="mailTimeZone" required value="${htmlEsc(mail.email?mail.timeZone:Intl.DateTimeFormat().resolvedOptions().timeZone)}" placeholder="Asia/Shanghai" ${locked}>
   <label for="mailHistorySince">从哪一天开始整理邮件</label><input class="lan-input" id="mailHistorySince" type="date" required value="${htmlEsc(mail.historySince)}" max="${ndate()}" ${locked}>
   <details><summary>高级连接设置</summary><label for="mailMcpServer">Codex 中的 MCP server 名称</label><input class="lan-input" id="mailMcpServer" value="${htmlEsc(mail.mcpServer)}" ${locked}><p>默认 codex_apps；其他 server 需提供文档规定的相同工具和返回格式。</p></details>
   <label class="lan-checkbox"><input id="mailShareWithModel" type="checkbox" ${mail.shareWithModel?'checked':''}>允许把新邮件的部分正文及项目摘要发送至我配置的模型，用于整理看板。</label>
   <p>${mail.locked?'此数据目录已经绑定邮箱与同步范围。更换账户请使用新的数据目录。':'连接验证只核对邮箱身份，不读取邮件、不调用模型。'}</p>
   <div class="lan-actions"><button class="btn primary" type="submit">保存邮箱设置</button><button class="btn" type="button" data-lan-action="mail-verify">保存并验证邮箱</button><button class="btn" type="button" data-lan-action="ai-settings">配置自己的 API / 模型</button></div>
   <div class="lan-note" id="mailSettingsStatus" role="status">${mail.verified?'邮箱身份已验证':'请保存设置并验证邮箱'} · ${ai.configured?'模型已配置':'模型尚未配置'}。保存邮箱设置后，自动刷新会暂停。</div>
  </form>`);
 }catch(e){openModal('邮箱与 API 设置',`<p>${htmlEsc(e.message)}</p>`)}
}
async function saveMailSettings(){
 const form=document.getElementById('mailSettingsForm');if(!form)return false;
 const message=form.querySelector('#mailSettingsStatus');
 try{
  await api('/api/mail/config',{method:'PUT',body:JSON.stringify({provider:form.querySelector('#mailProvider').value,email:form.querySelector('#mailEmail').value,timeZone:form.querySelector('#mailTimeZone').value,historySince:form.querySelector('#mailHistorySince').value,mcpServer:form.querySelector('#mailMcpServer').value,shareWithModel:form.querySelector('#mailShareWithModel').checked})});
  message.textContent='邮箱设置已保存；自动刷新已暂停。';await loadSetupStatus();await refreshFromServer(true);return true;
 }catch(e){message.textContent=e.message;return false;}
}
async function verifyMailSettings(){
 const form=document.getElementById('mailSettingsForm');if(!form)return;
 form.querySelectorAll('button').forEach(b=>b.disabled=true);
 try{
  if(!await saveMailSettings())return;
  const message=document.getElementById('mailSettingsStatus');message.textContent='正在核对邮箱身份…';
  const result=await api('/api/mail/verify',{method:'POST',body:'{}'});
  message.textContent='已验证自己的邮箱：'+result.email+'。配置模型后可立即刷新或开启每小时刷新。';await loadSetupStatus();
 }catch(e){const message=document.getElementById('mailSettingsStatus');if(message)message.textContent=e.message;}
 finally{if(form.isConnected)form.querySelectorAll('button').forEach(b=>b.disabled=false);}
}
