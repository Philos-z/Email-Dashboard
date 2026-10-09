/* Priority overview. Display derives from saved records; no project data is changed here. */
const focusState = { priority: 'all', expanded: false, day: '' };

function todayInBerlin(now = new Date()) {
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: typeof model === 'undefined' ? 'Europe/Berlin' : model.timezone || 'Europe/Berlin', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(now);
  const value = type => parts.find(part => part.type === type).value;
  return `${value('year')}-${value('month')}-${value('day')}`;
}

function focusDateEntries(item, today = todayInBerlin()) {
  const entries = [];
  const valid = date => /^\d{4}-\d{2}-\d{2}$/.test(date) && !Number.isNaN(dateObj(date).getTime()) && dateObj(date).toISOString().slice(0, 10) === date;
  if (verifiedDeadline(item) && valid(item.deadline)) {
    entries.push({ item, date: item.deadline, time: '', label: item.title, kind: 'deadline' });
  }
  // Only explicit, unfinished user entries become planned events. Completed milestones
  // describe past progress; email suggestions and uncertain windows are not appointments.
  for (const milestone of item.milestones || []) {
    if (milestone.done || milestone.certainty !== 'user') continue;
    const text = String(milestone.text || '');
    if (/待确认|待核实|未确认|尚未|可能|暂定|可选|候选|取消|改期|未定|待定|预计|或|[—–~～至]|\d\s*[-/]\s*\d|日\s*[-/]\s*\d/.test(text)) continue;
    const match = text.match(/^\s*(?:(20\d{2})年)?(\d{1,2})月(\d{1,2})日(?:\s*(\d{1,2})(?:[:：点时](\d{1,2})?(半)?))?/u);
    if (!match) continue;
    const year = match[1] || String(item.last || today).slice(0, 4);
    const date = `${year}-${match[2].padStart(2, '0')}-${match[3].padStart(2, '0')}`;
    if (!valid(date)) continue;
    let time = '';
    if (match[4] !== undefined) {
      const hour = Number(match[4]);
      const minute = match[6] ? 30 : Number(match[5] || 0);
      if (hour > 23 || minute > 59) continue;
      time = `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`;
    }
    entries.push({ item, date, time, label: text, kind: 'plan' });
  }
  return entries.filter((entry, index, all) => all.findIndex(other => other.date === entry.date && other.time === entry.time) === index);
}

function focusSort(items, today = todayInBerlin()) {
  const score = item => {
    let value = (PRIORITY[item.priority] ?? 3) * 100;
    const days = verifiedDeadline(item) ? Math.round((dateObj(item.deadline) - dateObj(today)) / 86400000) : null;
    if (days !== null && days <= 0) value -= 40;
    else if (item.status === 'blocked') value -= 30;
    else if (days !== null && days <= 7) value -= 20;
    return value;
  };
  return [...items].sort((a, b) => score(a) - score(b) || String(b.last || '').localeCompare(String(a.last || '')) || a.title.localeCompare(b.title, 'zh-CN'));
}

function focusTitle(title) {
  return String(title || '').replace(/^\s*⚠\s*/, '');
}

function focusAction(item, today) {
  const days = verifiedDeadline(item) ? Math.round((dateObj(item.deadline) - dateObj(today)) / 86400000) : null;
  const due = days === null ? '无确认截止日' : days < 0 ? `已过 ${Math.abs(days)} 天` : days === 0 ? '今天' : dateShort(item.deadline);
  const urgent = days !== null && days <= 0;
  return `<button type="button" class="focus-action${urgent ? ' focus-action-urgent' : ''}" data-open="${attr(item.id)}"><span class="focus-priority ${item.priority.toLowerCase()}">${esc(item.priority)}</span><span class="focus-action-copy"><strong>${esc(focusTitle(item.title))}</strong><span>${esc(item.next || '打开项目补充下一步行动')}</span></span><span class="focus-action-meta"><strong>${esc(urgent ? due : stat(item).label)}</strong><span>${esc(urgent ? dateShort(item.deadline) : due)}</span></span></button>`;
}

function renderFocusAgenda(items, today) {
  const upcoming = items.flatMap(item => focusDateEntries(item, today))
    .filter(entry => { const days = Math.round((dateObj(entry.date) - dateObj(today)) / 86400000); return days >= 0 && days <= 30; })
    .sort((a, b) => a.date.localeCompare(b.date) || a.time.localeCompare(b.time) || (PRIORITY[a.item.priority] ?? 3) - (PRIORITY[b.item.priority] ?? 3));
  const dates = [...new Set(upcoming.map(entry => entry.date))];
  document.getElementById('focusAgenda').innerHTML = dates.length ? dates.map(date => {
    const day = dateObj(date);
    const weekday = new Intl.DateTimeFormat('zh-CN', { timeZone: 'UTC', weekday: 'short' }).format(day);
    const entries = upcoming.filter(entry => entry.date === date);
    return `<section class="focus-agenda-day"><time datetime="${date}" class="focus-agenda-date"><strong>${date.slice(8)}</strong><span>${Number(date.slice(5, 7))}月 · ${date === today ? '今天' : esc(weekday)}</span></time><div class="focus-agenda-events">${entries.map(entry => `<button type="button" class="focus-agenda-event" data-open="${attr(entry.item.id)}"><strong>${entry.time ? `${esc(entry.time)} · ` : ''}${esc(focusTitle(entry.item.title))}</strong><span class="focus-agenda-kind">${esc(entry.item.priority)} · ${entry.kind === 'plan' ? '手动计划' : '确认日程'}</span>${entry.kind === 'plan' ? `<span class="focus-agenda-context">${esc(entry.label)}</span>` : ''}</button>`).join('')}</div></section>`;
  }).join('') : '<div class="focus-empty">未来 30 天暂无已记录节点。<span>没有确定日期的事项仍保留在优先行动中。</span></div>';
}

function renderFocus() {
  const today = todayInBerlin();
  focusState.day = today;
  const date = dateObj(today);
  document.getElementById('focusDate').textContent = new Intl.DateTimeFormat('zh-CN', { timeZone: 'UTC', year: 'numeric', month: 'long', day: 'numeric', weekday: 'long' }).format(date);
  const all = model.items.filter(item => isLive(item) && ['P0', 'P1'].includes(item.priority));
  const items = focusSort(all.filter(item => focusState.priority === 'all' || item.priority === focusState.priority), today);
  document.querySelectorAll('[data-focus-priority]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.focusPriority === focusState.priority)));
  const shown = focusState.expanded ? items : items.slice(0, 7);
  document.getElementById('focusSummary').textContent = `${items.length} 项重点 · 按优先级、时限与阻碍排序`;
  document.getElementById('focusActions').innerHTML = shown.length ? shown.map(item => focusAction(item, today)).join('') : '<div class="focus-empty">当前优先级没有待办事项。</div>';
  const more = document.getElementById('focusMore');
  more.hidden = items.length <= 7;
  more.textContent = focusState.expanded ? '收起其他重点 ↑' : `展开其余 ${items.length - shown.length} 项重点 ↓`;
  more.setAttribute('aria-expanded', String(focusState.expanded));
  renderFocusAgenda(items, today);
  const waiting = items.filter(item => item.status === 'waiting');
  document.getElementById('focusWaitingSection').hidden = !waiting.length;
  document.getElementById('focusWaitingCount').textContent = `${waiting.length} 项`;
  document.getElementById('focusWaitingPreview').textContent = waiting.slice(0, 3).map(item => focusTitle(item.title)).join(' · ');
  document.getElementById('focusWaiting').innerHTML = waiting.map(item => `<button type="button" data-open="${attr(item.id)}"><span class="focus-priority ${item.priority.toLowerCase()}">${esc(item.priority)}</span><span><strong>${esc(focusTitle(item.title))}</strong><span>${esc(item.blocker || item.next || '等待后续反馈')}</span></span><span aria-hidden="true">↗</span></button>`).join('');
}

function renderOverview() {
  renderFocus();
  metrics();
  drawCharts();
  renderRecent();
  document.getElementById('overviewProjects').innerHTML = focusSort(model.items.filter(isLive)).slice(0, 6).map(card).join('');
}

document.addEventListener('click', event => {
  const priority = event.target.closest('[data-focus-priority]');
  if (priority) {
    focusState.priority = priority.dataset.focusPriority;
    focusState.expanded = false;
    renderFocus();
  }
  if (event.target.closest('[data-focus-expand]')) {
    focusState.expanded = !focusState.expanded;
    renderFocus();
  }
});

// Keep the German calendar day current without making any model or mail requests.
setInterval(() => { if (state.page === 'overview' && focusState.day !== todayInBerlin()) renderFocus(); }, 60000);
