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
 if(stamp.includes('T')){const date=new Date(stamp);if(!Number.isNaN(date.getTime()))stamp=date.toLocaleString('zh-CN',{timeZone:'Europe/Berlin',hour12:false});}
 const label=last?'最近邮件检查：'+stamp+'（德国时间）':'尚无邮件检查记录';
 for(const id of ['mcpLastChecked','mailCheckStatus','mailHistoryCheckStatus']){const el=document.getElementById(id);if(el)el.textContent=label;}
}
let mailStatus=null,mailStatusBusy=false;
function mailTime(value){if(!value)return '尚无记录';const d=new Date(value);return Number.isNaN(d.getTime())?value:d.toLocaleString('zh-CN',{timeZone:'Europe/Berlin',hour12:false});}
function renderMailStatus(){
 if(!mailStatus)return;
 const s=mailStatus,last=s.runs[0];
 const schedule=s.enabled?(s.quietNow?'夜间暂停中':'每小时自动刷新')+' · 00:00–06:00 暂停 · 下次 '+mailTime(s.nextRunAt)+'（德国时间）':'自动刷新已暂停；仍可立即刷新';
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
 try{mailStatus=await api('/api/outlook/status');renderMailStatus();}
 catch(e){const el=document.getElementById('mailSyncDetails');if(el)el.textContent=e.message;}
 finally{mailStatusBusy=false;}
}
async function openMcp(){
 openModal('Outlook 邮件刷新',`<p>Mac 后台每小时检查收件箱、Archive / 存档 / 归档及其子文件夹，以及已发送邮件；自动刷新和立即刷新的范围相同。模型会区分收到的回复与我已发送的申请、回复和催办；已发送不等于对方已确认或事项已完成。德国时间 00:00–06:00 暂停自动刷新，06:00 恢复；手动立即刷新随时可用。关闭网页也会继续运行。没有新邮件时不调用模型。</p><p>邮件检查会向所选模型发送项目摘要和新邮件的部分正文；不读取附件。运行结果和错误会显示在下方。</p><div id="mailSyncDetails" aria-live="polite">正在读取刷新状态…</div>`);
 await loadMailStatus();
}
async function triggerMailRefresh(){
 const button=document.querySelector('[data-lan-action="mail-refresh"]');if(button)button.disabled=true;
 try{const result=await api('/api/outlook/refresh',{method:'POST',body:'{}'});toast(result.alreadyRunning?'已有刷新正在进行':'已开始刷新邮件');await loadMailStatus();}
 catch(e){toast(e.message);if(button?.isConnected)button.disabled=false;}
}
async function toggleMailSchedule(){
 if(!mailStatus)return;
 try{mailStatus=await api('/api/outlook/schedule',{method:'PUT',body:JSON.stringify({enabled:!mailStatus.enabled})});renderMailStatus();toast(mailStatus.enabled?'已开启每小时刷新':'已暂停自动刷新');}
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
 openModal('本地 API 设置','<p id="aiSettingsLoading">正在读取配置…</p>');
 try{
  const config=await api('/api/ai/config');
  if(!document.getElementById('aiSettingsLoading'))return;
  openModal('本地 API 设置',`<p>让看板使用这台 Mac 上的模型代理。手机和其他电脑也通过看板使用同一配置。</p>
  <form id="aiSettingsForm" autocomplete="off">
   <label for="aiBaseUrl">API 地址</label><input class="lan-input" id="aiBaseUrl" type="url" value="${htmlEsc(config.baseUrl)}" required>
   <label for="aiApiKey">API 密钥 ${config.keyConfigured?'· 已保存，留空保留':''}</label><input class="lan-input" id="aiApiKey" type="password" autocomplete="new-password" placeholder="${config.keyConfigured?'留空保留现有密钥':'填写代理 API Keys 中的密钥'}">
   <label for="aiModel">模型</label><input class="lan-input" id="aiModel" list="aiModelList" value="${htmlEsc(config.model)}" placeholder="点击加载模型，或输入模型名"><datalist id="aiModelList"></datalist>
   <p>密钥只保存在 Mac 后端。请输入 API 密钥；管理页面的登录密码不能用于模型调用。</p>
   <div class="lan-actions"><button class="btn" type="button" data-lan-action="ai-models">加载模型</button><button class="btn primary" type="submit">保存配置</button><button class="btn" type="button" data-lan-action="ai-test">保存并测试连接</button><button class="btn" type="button" data-lan-action="open-ai">打开 AI 助手</button></div>
   <p>测试连接会调用模型一次，不附带项目数据。</p><div id="aiSettingsStatus" class="lan-note" role="status">${config.configured?'已配置 '+htmlEsc(config.model)+'；可测试连接。':'填写配置后保存。'}</div>
  </form>`);
 }catch(e){if(document.getElementById('aiSettingsLoading'))openModal('本地 API 设置',`<p>${htmlEsc(e.message)}</p>`)}
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
  const config=await saveAiSettings();
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
  openModal('AI 助手',`<p>当前模型：<strong>${htmlEsc(config.model)}</strong>。可以按问题查询 Outlook 邮件，并结合看板里的事件上下文回答。</p>
   <div id="aiConversation" class="lan-ai-conversation" role="log" aria-live="polite"></div>
   <form id="aiChatForm"><label for="aiMessage">你的问题</label><textarea id="aiMessage" maxlength="8000" placeholder="例如：查询最近与项目有关的邮件，告诉我下一步" required></textarea>
   <label class="lan-checkbox"><input id="aiIncludeMail" type="checkbox" checked> 按需查询 Outlook 邮件（相关正文发送至当前代理模型，不读取附件）</label>
   <label class="lan-checkbox"><input id="aiIncludeContext" type="checkbox" checked> 附上当前项目摘要和事件上下文（不含备注或完整邮件证据）</label>
   <div class="lan-actions"><button class="btn primary" type="submit" ${aiRequestBusy?'disabled':''}>${aiRequestBusy?'正在回复…':'发送'}</button><button class="btn" type="button" data-lan-action="ai-clear" ${aiRequestBusy?'disabled':''}>清空对话</button><button class="btn" type="button" data-lan-action="ai-settings">API 设置</button></div><p>回复仅供参考，不会修改项目或邮件。对话只保留在当前页面中；夜间也可主动查询。</p><div id="aiChatStatus" role="status"></div></form>`);
  renderAiConversation();
 }catch(e){if(document.getElementById('aiChatLoading'))openModal('AI 助手',`<p>${htmlEsc(e.message)}</p>`)}
}
function renderAiConversation(){
 const el=document.getElementById('aiConversation');if(!el)return;
 el.innerHTML=aiConversation.map(entry=>`<div class="lan-ai-message ${entry.role==='user'?'lan-ai-user':''}"><strong>${entry.role==='user'?'你':'AI 助手'}</strong><div>${htmlEsc(entry.content)}</div>${renderAiMailSources(entry)}</div>`).join('')||'<p>可以直接询问邮件内容、最近往来或项目下一步。助手按需查询，并在回复下方列出实际邮件来源。</p>';
 el.scrollTop=el.scrollHeight;
}
function renderAiMailSources(entry){
 if(entry.role!=='assistant'||!entry.mailAccess)return '';
 const access=entry.mailAccess,sources=Array.isArray(entry.sources)?entry.sources:[];
 const status=access.queried?'已通过 Outlook 查询 · '+sources.length+' 封邮件':access.enabled?'本次未查询邮件':'本次已关闭邮件查询';
 const more=(access.queries||[]).some(query=>query.hasMore)?' · 部分结果还有下一页':'';
 const links=sources.map(source=>{
  let url='';try{const parsed=new URL(source.url);if(parsed.protocol==='https:'&&!parsed.username&&!parsed.password&&['outlook.live.com','outlook.office.com','outlook.office365.com'].includes(parsed.hostname))url=parsed.href}catch(e){}
  const title='['+source.reference+'] '+source.subject;
  const label=url?`<a href="${htmlEsc(url)}" target="_blank" rel="noopener noreferrer">${htmlEsc(title)} ↗</a>`:htmlEsc(title);
  return `<li>${label}<small>${htmlEsc(source.date)} · ${source.direction==='sent'?'已发送':'收到'}${source.from?' · '+htmlEsc(source.from):''}${source.truncated?' · 正文片段':''}</small></li>`;
 }).join('');
 return `<div class="lan-ai-sources"><p>${htmlEsc(status+more)}</p>${sources.length?`<details><summary>邮件来源（${sources.length}）</summary><ul>${links}</ul></details>`:''}</div>`;
}
async function sendAiMessage(){
 if(aiRequestBusy)return;
 const form=document.getElementById('aiChatForm');if(!form)return;
 const message=form.querySelector('#aiMessage').value.trim();if(!message)return;
 const history=aiConversation.slice(-8).map(({role,content,sources})=>({role,content:content.slice(0,25000)+(sources?.length?'\n上轮邮件来源（仅供定位后重新查询）：\n'+sources.slice(0,20).map(source=>'['+source.reference+'] '+source.date+' '+source.subject).join('\n'):'')}));
 const includeContext=form.querySelector('#aiIncludeContext').checked;
 const includeMail=form.querySelector('#aiIncludeMail').checked;
 aiRequestBusy=true;aiConversation.push({role:'user',content:message});renderAiConversation();
 form.querySelector('#aiMessage').value='';form.querySelectorAll('button').forEach(el=>el.disabled=true);
 document.getElementById('aiChatStatus').textContent=includeMail?'正在处理问题；如涉及邮件，将查询 Outlook 后回复…':'正在等待模型回复…';
 try{
  const data=await api('/api/ai/chat',{method:'POST',body:JSON.stringify({message,history,includeContext,includeMail})});
  aiConversation.push({role:'assistant',content:data.reply,sources:data.sources,mailAccess:data.mailAccess});
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
