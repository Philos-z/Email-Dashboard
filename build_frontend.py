from pathlib import Path
import re
root=Path(__file__).resolve().parent
text=(root/'web/dashboard_original.html').read_text('utf-8')
def swap(old,new,once=True):
 global text
 n=text.count(old)
 if n!=(1 if once else n):
  raise RuntimeError(f'Replace mismatch count {n} for {old[:60]}')
 text=text.replace(old,new)

swap("<script>\n'use strict';\nconst BOOTSTRAP=", "<script>/*__SERVER_SNAPSHOT__*/</script>\n<script>\n'use strict';\nconst BOOTSTRAP=")
swap('let model=loadModel();', 'let model=window.__ATLAS_SERVER__.model;\nlet serverRevision=window.__ATLAS_SERVER__.revision;\nlet itemVersions=window.__ATLAS_SERVER__.itemRevisions;\nlet isSaving=false; let deferredSync=false;')
# block write to localstorage; server is only source
text,n=re.subn(r'function persist\(\)\{try\{localStorage.setItem\(STORAGE_KEY,JSON.stringify\(model\)\);\}catch\(e\)\{toast\(.+?\)\}\}',"function persist(){/* LAN: edits go to SQLite through API */}",text)
assert n==1,n
# override real editing and add
old='function updateItem(id,mutate,reDrawDrawer=false){let it=model.items.find(x=>x.id===id);if(!it)return;mutate(it);it.userUpdatedAt=new Date().toISOString();model.lastSavedAt=it.userUpdatedAt;persist();render();if(reDrawDrawer)openDrawer(id)}'
new='''async function updateItem(id,mutate,reDrawDrawer=false){
 if(isSaving){toast('上一次保存尚未完成');return;}
 const it=model.items.find(x=>x.id===id);if(!it)return;
 isSaving=true;const before=copy(it);
 mutate(it);it.userUpdatedAt=new Date().toISOString();model.lastSavedAt=it.userUpdatedAt;
 render();if(reDrawDrawer)openDrawer(id);
 try{const response=await api('/api/items/'+encodeURIComponent(id),{method:'PUT',body:JSON.stringify({item:it,expectedVersion:itemVersions[id]})});
  serverRevision=response.revision;itemVersions[id]=response.itemVersion;showNet('数据库已保存');
  if(response.contextUpdate){toast('已保存，正在整理事件上下文和下一步');loadItemContextStatus(id);}
 }catch(e){const ix=model.items.findIndex(x=>x.id===id);if(ix>=0)model.items[ix]=before;render();if(reDrawDrawer)openDrawer(id);
  toast(e.status===409?'⚠ 另一设备已修改此事项；请刷新后重新编辑':'保存失败：'+e.message);showNet('保存失败');deferredSync=true;
 }finally{isSaving=false;if(deferredSync&&!state.openId)refreshFromServer()}
}'''
swap(old,new)
# new item
text,n=re.subn(r'function addItem\(\)\{.*?\}\nfunction csvRow', '''async function addItem(){
 if(isSaving){toast('请等待当前保存完成');return;}
 isSaving=true;
 const id='local-'+Date.now()+'-'+Math.random().toString(36).slice(2,6);const today=ndate();
 const it={id,title:'新事项（点击编辑）',category:'软件与 AI 项目',status:'planned',priority:'P2',mode:'project',first:today,last:today,deadline:'',milestones:[],next:'请填写下一步',blocker:'',notes:'',source:'用户在局域网新增',tags:[],userUpdatedAt:new Date().toISOString()};
 try{const r=await api('/api/items',{method:'POST',body:JSON.stringify({item:it})});model.items.unshift(it);serverRevision=r.revision;itemVersions[id]=r.itemVersion;setPage('projects');openDrawer(id);toast('已保存到共享数据库');}
 catch(e){toast('新增失败：'+e.message)}
 finally{isSaving=false;}
}
function csvRow''',text,flags=re.S)
assert n==1,('add',n)
# import JSON: upload to server, not local
text,n=re.subn(r'function importJSON\(file\)\{.*?\}\nfunction reset\(\)', '''function importJSON(file){if(!file)return;const rd=new FileReader();rd.onload=async()=>{
  try{const data=JSON.parse(String(rd.result));if(data.schemaVersion!==1||!Array.isArray(data.items))throw Error('数据格式不符合当前版本');
  if(!confirm(`将用 ${data.items.length} 项替换共享数据库。所有局域网设备都会更新。建议先导出 JSON 备份。继续吗？`))return;
  const r=await api('/api/import',{method:'POST',body:JSON.stringify({model:data,expectedRevision:serverRevision})});serverRevision=r.revision;
  await refreshFromServer(true);closeDrawer();setPage('overview');toast('已导入共享数据库');
 }catch(e){toast('导入失败：'+e.message)}};rd.readAsText(file,'utf-8')}
function reset()''',text,flags=re.S)
assert n==1,('import',n)
text,n=re.subn(r'function reset\(\)\{.*?\}\nfunction globalAction', '''async function reset(){if(!confirm('确定让所有设备恢复到初始快照？这会覆盖 SQLite 中的全部修改。请先导出 JSON 备份。'))return;
 try{const r=await api('/api/reset',{method:'POST',body:JSON.stringify({expectedRevision:serverRevision})});serverRevision=r.revision;
 await refreshFromServer(true);closeDrawer();setPage('overview');toast('已重置共享数据库');}
 catch(e){toast('重置失败：'+e.message)}
}
function globalAction''',text,flags=re.S)
assert n==1,('reset',n)
# Context is derived after a saved manual edit; drafts survive background updates.
swap(' <div class="drawer-section"><h3>项目属性</h3>', ''' <div class="drawer-section"><h3>事件上下文</h3><p class="item-context-summary" id="itemContextSummary">${esc(it.contextSummary||'保存进展后，系统会整理本事件的上下文、当前状态和下一步。')}</p><p class="filterhint" id="itemContextStatus" role="status">正在读取整理状态…</p><button class="btn small" type="button" data-lan-action="item-context-refresh">重新整理当前状态和下一步</button></div>
 <div class="drawer-section"><h3>项目属性</h3>''')
swap("document.body.style.overflow='hidden';}", "document.body.style.overflow='hidden';queueMicrotask(()=>loadItemContextStatus(id));}")

# dynamic email updates
swap('const sum=BOOTSTRAP.todayMailSummary;', 'const sum=model.todayMailSummary||BOOTSTRAP.todayMailSummary;')
swap('const important=BOOTSTRAP.todayUpdates.filter', 'const important=(model.todayUpdates||[]).filter')
swap('const all=BOOTSTRAP.todayUpdates;', 'const all=model.todayUpdates||[];')
swap('document.getElementById(\'todaySidebarCount\').textContent=BOOTSTRAP.todayMailSummary.receivedCount;',"document.getElementById('todaySidebarCount').textContent=(model.todayMailSummary||BOOTSTRAP.todayMailSummary).receivedCount;")
swap("document.getElementById('currentDate').textContent='今日新邮件 16 封 · 最近 11:46';", "document.getElementById('currentDate').textContent='共享数据库 · '+model.items.length+' 项';")
swap('model.lastSavedAt?\'上次修改 \'+prettyDate(model.lastSavedAt.slice(0,10)):\'尚未修改初始快照\'',"model.lastSavedAt?'最近修改 '+prettyDate(model.lastSavedAt.slice(0,10)):'尚未修改初始快照'")
swap('当前 ${model.items.length} 项事项 · 浏览器本地保存 · ', '当前 ${model.items.length} 项事项 · Mac SQLite 共享数据库 · ')
swap("toast('已保存到本机浏览器')","toast('正在保存至共享数据库')")
swap("toast('已添加里程碑')","toast('正在同步里程碑')")
swap("toast(checked?'已确认里程碑':'已调整为待完成')","toast('正在同步里程碑变更')")
swap(' · 已在本地编辑', ' · 已在共享数据库编辑')
swap('浏览器手动确认', '局域网手动确认', once=False)
swap('用户本地新增', '局域网用户新增',once=False)
swap('本地确认', '手动确认',once=False)
swap('本机浏览器', '共享数据库',once=False)
swap('浏览器本地保存', 'Mac SQLite 共享保存',once=False)
swap('你的更改保存在本机浏览器。建议定期导出 JSON 备份，换设备不会自动同步。','编辑自动同步至 Mac 的 SQLite 数据库，其他设备实时收到变化。',once=False)
swap('离线可编辑','局域网实时同步',once=False)
swap('修改项目或里程碑后会自动保存在当前浏览器的 localStorage。不同设备或清理浏览器数据后不会自动同步。请经常导出备份。','修改自动保存至 Mac 上的 SQLite，共享给同一局域网内访问的设备。可以导出 JSON 备份；不同设备修改同一事项时有版本冲突检测。',once=False)
# settings changes
swap('本地数据管理','共享数据库管理')
swap('数据与设置</h1><p>你拥有看板全部可编辑数据，并可随时导出备份、恢复或迁移。','数据与设置</h1><p>Mac SQLite 集中保存，局域网设备共享同一份数据；Outlook 邮件每小时自动刷新，德国时间 00:00–06:00 暂停；可随时手动触发。')
# page titles not claim today's fresh in future: retain snapshot historical date but add status
swap('<span class="todaychip" id="currentDate"></span>','<span class="todaychip" id="connectionStatus" aria-live="polite">连接中…</span><span class="todaychip" id="currentDate"></span><button class="btn" type="button" data-lan-action="open-ai">AI 助手</button><button class="btn" type="button" data-lan-action="open-mcp">✉ 刷新邮件</button>')
# insert settings cards
settings='''<div class="card settings-card"><h3>Outlook MCP · 邮件自动刷新</h3><p>每小时检查收件箱、Archive / 存档 / 归档及其子文件夹，以及已发送邮件；模型结合收发往来更新项目和邮件记录。德国时间 00:00–06:00 暂停自动刷新，06:00 恢复；可随时立即刷新。关闭网页不影响后台运行。</p><p id="mcpLastChecked"></p><p id="mailScheduleStatus" aria-live="polite">正在读取计划…</p><button class="btn primary" type="button" data-lan-action="open-mcp">✉ 刷新邮件 / 管理计划</button><button class="btn" type="button" data-lan-action="audit">查看修改历史</button><button class="btn" type="button" data-lan-action="sqlite-backup">备份 SQLite 数据库</button><button class="btn" type="button" data-lan-action="migrate">导入旧版浏览器修改</button><!--__ATLAS_LOGOUT__--></div>'''
ai_settings='''<div class="card settings-card"><h3>本地 API · AI 助手</h3><p>连接 Mac 上的模型代理，选择模型并测试连接。邮件刷新有新邮件时调用此模型；保存手动进展后也会整理事件上下文。AI 助手在发送问题时调用。</p><div class="lan-actions"><button class="btn primary" type="button" data-lan-action="ai-settings">配置 API / 选择模型</button><button class="btn" type="button" data-lan-action="open-ai">打开 AI 助手</button></div></div>'''
swap('<div class="settings-grid"><div class="card settings-card">', '<div class="settings-grid">'+ai_settings+settings+'<div class="card settings-card">')
# historical date shown as snapshot. keep it but notice
swap('<h1>今日新邮件 · 10月8日</h1>', '<h1>历史邮件快照 · 2026年10月8日</h1>')
swap('<h2>今天的收件箱变化</h2>','<h2>最近一次收件箱快照</h2>')
swap('<h2>今日关键进展</h2>','<h2>最近邮件关键进展</h2>')
swap('查看全部今日邮件','查看邮件快照',once=False)
swap('<strong><span class="online-indicator"></span>局域网实时同步</strong>','<strong><span class="online-indicator"></span>局域网实时同步</strong>') if False else None
# add dialog in body (not editable script)
modal='''<div class="lan-modal-backdrop" id="lanBackdrop" hidden><section class="lan-modal" role="dialog" aria-modal="true" aria-labelledby="lanModalTitle"><header><h2 id="lanModalTitle">Outlook 邮件刷新</h2><button class="btn" type="button" data-lan-action="close">关闭</button></header><div id="lanModalBody"></div></section></div>'''
swap('<script>/*__SERVER_SNAPSHOT__*/',modal+'\n<script>/*__SERVER_SNAPSHOT__*/')
css='''<style>
#connectionStatus{background:#eef5fc;color:#2f6885}.lan-modal-backdrop:not([hidden]){position:fixed;inset:0;z-index:100000;background:#0d1b33b8;display:grid;place-items:center;padding:18px}.lan-modal{background:white;width:min(780px,100%);max-height:90vh;overflow:auto;border-radius:18px;padding:24px;box-shadow:0 28px 90px #07182b55;color:#20314a}.lan-modal>header{display:flex;justify-content:space-between;align-items:center;gap:20px}.lan-modal h2{font-size:20px;margin:0 0 12px}.lan-modal textarea{width:100%;min-height:190px;margin:8px 0 12px;line-height:1.5;font-family:ui-monospace,monospace;font-size:12px;padding:12px;border:1px solid #dce5ed;border-radius:9px}.lan-modal label{display:block;font-size:12px;font-weight:650}.lan-modal select{padding:9px;border:1px solid #dce5ed;border-radius:8px;margin-right:10px}.lan-modal p{line-height:1.65;color:#60728a}.lan-actions{display:flex;gap:10px;flex-wrap:wrap;margin:12px 0}.lan-note{background:#f2f6fb;border:1px solid #e2e9f4;padding:12px;border-radius:10px;margin:14px 0;color:#526178}.lan-diff{margin:8px 0;padding:12px;border:1px solid #dce5ed;border-radius:10px}.lan-diff h4{margin:0 0 6px}.lan-diff code{word-break:break-all}#mcpLastChecked{font-size:12px}
.item-context-summary{font-size:13px;line-height:1.8;color:#425974;white-space:pre-wrap}.lan-input{display:block;box-sizing:border-box;width:100%;padding:11px 12px;margin:7px 0 16px;border:1px solid #dce5ed;border-radius:9px;font:inherit;color:inherit}.lan-checkbox{display:flex!important;align-items:center;gap:8px;font-weight:400!important;line-height:1.6}.lan-checkbox input{width:17px;height:17px;flex-shrink:0}.lan-ai-conversation{max-height:36vh;overflow:auto;margin:16px 0}.lan-ai-message{background:#f3f6fa;padding:14px;border-radius:12px;margin:10px 0}.lan-ai-message strong{display:block;font-size:12px;color:#65748a;margin-bottom:8px}.lan-ai-message>div{white-space:pre-wrap;overflow-wrap:anywhere;font-size:14px;line-height:1.75}.lan-ai-user{background:#eef1ff}.lan-modal button:disabled{opacity:.55;cursor:wait}#aiMessage{min-height:90px;box-sizing:border-box;font-family:inherit;font-size:14px}#aiChatStatus{color:#8c4963;line-height:1.6}
@media(max-width:1250px){.topcontrols #currentDate,.topcontrols [data-global-action="export-json"]{display:none}.topcontrols{gap:8px}}
@media(max-width:900px){#connectionStatus{display:none}}
@media(max-width:820px){.topcontrols [data-lan-action="open-mcp"]{display:none}}
@media(max-width:600px){.topcontrols .btn[data-lan-action="open-ai"]{display:inline-flex!important}.lan-modal{box-sizing:border-box;padding:18px}}
</style>'''
swap('</head>',css+'\n</head>')
# inject app runtime after old script only. Browser will execute old render first then bridge.
bridge=(root/'web/lan_bridge.js').read_text('utf-8')
swap('</script>\n</body></html>','</script>\n<script>\n'+bridge+'\n</script>\n</body></html>')
# Replace static legacy copy which must not imply browser-local sync or current live email freshness.
text=text.replace('你的更改保存在共享数据库。建议定期导出 JSON 备份，换设备不会自动同步。','你的更改保存在 Mac 的 SQLite 中，同一局域网的已登录设备自动同步。')
text=text.replace('今天的新邮件，哪些改变了进度？','项目进展 · 局域网实时协作')
text=text.replace('重点呈现 10 月 8 日 Outlook 新邮件带来的真实进展；历史六个月事项仍完整保留。','基于 10 月 8 日的邮件快照开始，后续邮件每小时自动刷新（德国时间 00:00–06:00 暂停），也可手动立即更新。六个月历史事项完整保留。')
text=text.replace('今天的新邮件；相同预约的多封自动邮件合并','10 月 8 日的历史邮件快照；相同预约的多封自动邮件合并')
text=text.replace('只使用今天收到的邮件','仅作为 10 月 8 日的邮件快照')
text=text.replace('今日新邮件','邮件快照')
text=text.replace('OUTLOOK INBOX · 08 OCT 2026','SQLITE · LAN SYNC')
text=text.replace('数据与设置</span>','数据与设置</span>')
text=text.replace('数据保存在当前浏览器','数据保存在 Mac SQLite')
text=text.replace('换设备不会自动同步','已登录设备可实时同步')
# Imported updates retain their own date rather than the initial snapshot date.
text=text.replace('Outlook · 2026-10-08 · ${esc(e.sourceSubject)}', "Outlook · ${esc(e.date||model.snapshotDate||'2026-10-08')} · ${esc(e.sourceSubject)}")
text=text.replace('${esc(e.time)}', "${esc(e.date?e.date.slice(5)+' '+e.time:e.time)}")
text=text.replace('<h1>历史邮件快照 · 2026年10月8日</h1>', '<h1>邮件更新记录</h1>')
text=text.replace('<h2>最近一次收件箱快照</h2>', '<h2>初始收件箱快照 · 10月8日</h2>')
text=text.replace('<h2>最近邮件关键进展</h2>', '<h2>最近邮件关键进展</h2><p id="mailCheckStatus" aria-live="polite"></p><button class="btn" type="button" data-lan-action="open-mcp">刷新邮件</button>')
text=text.replace('柏林本地时间 00:00 起，收件箱已核对 16 封邮件；同一个预约合并显示，营销邮件默认隐藏。', '收录10月8日初始快照与后续自动刷新的增量记录；同一个预约合并显示，推广默认隐藏。</p><p id="mailHistoryCheckStatus" aria-live="polite">')
text=text.replace('10月8日00:00至最近已收邮件，按德国时间换算。16封包括预约自动邮件与2封促销邮件；9条有效信息不等于9项申请、9笔交易或9笔新订单。尚未登录外部门户核实处理结果。', '初始快照与后续增量记录合并展示，日期按德国时间显示。记录条数不等于订单、申请或交易数量；外部门户结果及未读取的附件仍待核验。')
text=text.replace("document.getElementById('todaySidebarCount').textContent=(model.todayMailSummary||BOOTSTRAP.todayMailSummary).receivedCount;", "document.getElementById('todaySidebarCount').textContent=(model.todayUpdates||[]).filter(e=>e.kind!=='promotion').length;")
text=text.replace('<span>邮件快照</span>', '<span>邮件记录</span>')

text=text.replace('Progress Atlas · 邮件快照专版 · 2026-10-08', 'Progress Atlas · 邮件自动刷新')
text=text.replace('PROGRESS ATLAS 1.1 · TODAY FIX · OFFLINE-FIRST · PRIVATE LOCAL STORAGE', 'PROGRESS ATLAS · OUTLOOK MCP · HOURLY SYNC')
text=text.replace('今日邮箱专项核验 · 10月8日德国时间 · 离线快照，不自动刷新', '每小时自动检查邮件 · 00:00–06:00 暂停（德国时间） · 支持立即刷新')
text=text.replace('已使用 Outlook 截至 2026-10-08 的邮件核对部分事项；', '项目包含初始邮件快照及后续增量记录；')

text=text.replace('最近一次收件箱快照', '最近邮件往来')
text=text.replace('✉ OUTLOOK INBOX · 08 OCT', '✉ OUTLOOK · 收件与已发送')
text=text.replace('<div class="evidence-date">${esc(e.date)}</div>', '<div class="evidence-date">${esc(e.date)}${e.direction?\' · \'+(e.direction===\'sent\'?\'已发送\':\'收到\'):\'\'}</div>')

# The current overview and theme are kept separately from the legacy snapshot.
# Rebuilding the LAN HTML must preserve the selected priority-first interface.
overview=(root/'web/focus_overview.html').read_text('utf-8')
start=text.index('<section class="view active" id="view-overview">')
end=text.index('<section class="view" id="view-today">',start)
text=text[:start]+overview+'\n\n'+text[end:]
sidebar=re.search(r'<aside class="sidebar".*?</aside>',text,re.S)
navigation=re.search(r'<nav aria-label="页面">(.*?)</nav>',sidebar.group(),re.S).group(1)
navigation=navigation.replace('<span>总览</span>','<span>今日焦点</span>')
text=text[:sidebar.start()]+text[sidebar.end():]
text=text.replace('<header class="topbar">','<header class="topbar"><div class="atlas-brand" aria-label="Progress Atlas"><span>Progress Atlas</span></div>',1)
text=text.replace('<div class="content">','<nav class="workspace-nav" aria-label="页面">'+navigation+'</nav>\n<div class="content">',1)
theme=(root/'web/atlas_theme.css').read_text('utf-8')
text=text.replace('</head>','<link rel="stylesheet" href="/assets/fonts.css">\n<style id="atlas-theme">\n'+theme+'\n</style>\n</head>',1)
focus_js=(root/'web/atlas_focus.js').read_text('utf-8')
text=text.replace("document.getElementById('overlay').addEventListener('click',closeDrawer);\nrender();", "document.getElementById('overlay').addEventListener('click',closeDrawer);\n"+focus_js+'\nrender();',1)
text,n=re.subn(r'function ndate\(\)\{[^\n]+\}', 'function ndate(){return todayInBerlin()}',text)
assert n==1,('Berlin date',n)
text=text.replace("overview:'总览'","overview:'今日焦点'")
text=text.replace('<strong id="crumb">总览</strong>','<strong id="crumb">今日焦点</strong>')
snapshot_stats='''<details class="snapshot-statistics"><summary>初始邮件快照统计 · 2026年10月8日</summary><div class="today-totals" id="todayTotals"></div><p>相同预约的多封通知合并；统计仅代表初始快照。</p></details>'''
text=text.replace('<h3>数据来源与精度</h3>','<h3>数据来源与精度</h3>'+snapshot_stats,1)
# Snapshot counts are only needed in Settings; the home screen starts with actions.
text=text.replace('function renderSettings(){','function renderSettings(){const sum=model.todayMailSummary||BOOTSTRAP.todayMailSummary;document.getElementById(\'todayTotals\').innerHTML=[[\'原始收件\',sum.receivedCount],[\'去重有效信息\',sum.groupedRelevantCount],[\'排除推广\',sum.promotionalCount]].map(([label,value])=>`<div class="today-total"><strong>${esc(value)}</strong><span>${esc(label)}</span></div>`).join(\'\');',1)
text=re.sub(r'function renderTodayQuick\(\)\{.*?\n\}', '', text, count=1, flags=re.S)
text=re.sub(r'function renderPriorities\(\)\{[^\n]*\}\n', '', text, count=1)
text=re.sub(r'function renderOverview\(\)\{renderTodayQuick\(\);[^\n]*\}\n', '', text, count=1)
# History filters and time order are independent of the database insertion order.
mail_js=(root/'web/atlas_mail.js').read_text('utf-8')
text,n=re.subn(r'function todayCard\(e,compact=false\)\{.*?\n\}', '', text, count=1, flags=re.S)
assert n==1,('mail card',n)
text,n=re.subn(r'function renderToday\(\)\{.*?\n\}', lambda match: mail_js, text, count=1, flags=re.S)
assert n==1,('mail history',n)
text=text.replace("todayFilter:'relevant'", "todayFilter:'relevant',todayOrder:'latest'",1)
text=text.replace("today:'邮件快照'", "today:'邮件更新记录'",1)
mail_controls='''<div class="mail-controls"><div class="today-toolbar" role="group" aria-label="邮件记录筛选">
 <button class="btn small active" type="button" data-today-filter="relevant" aria-pressed="true">非推广</button>
 <button class="btn small" type="button" data-today-filter="action" aria-pressed="false" title="显示 P0 / P1 重点邮件，P0 排在最前">优先处理</button>
 <button class="btn small" type="button" data-today-filter="all" aria-pressed="false">全部（含推广）</button>
 </div><label class="mail-order" for="mailTimeOrder">时间排序 <select id="mailTimeOrder" data-filter="todayOrder"><option value="latest">最新在前</option><option value="oldest">最早在前</option></select></label></div>
 <p class="mail-list-summary" id="mailListSummary" role="status" aria-live="polite"></p>
 <aside class="mail-promotion-notice" id="mailPromotionNotice" aria-label="已包含的推广邮件" hidden></aside>'''
text,n=re.subn(r'<div class="today-toolbar"[^>]*>.*?</div>', lambda match: mail_controls, text, count=1, flags=re.S)
assert n==1,('mail controls',n)
text=text.replace('PROGRESS ATLAS · OUTLOOK MCP · HOURLY SYNC','PROGRESS ATLAS · 项目进展')
(root/'web/dashboard_lan.html').write_text(text,'utf-8')
print('Dashboard built',len(text),'chars')
