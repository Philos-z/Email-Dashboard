/* Mail history uses the recorded German calendar date, not sync insertion order. */
function mailRecordDate(entry, snapshotDate = '') {
  // The original snapshot entries only stored a time. Their date is the snapshot
  // date, never the current day or the date of a later background import.
  const date = entry.date || snapshotDate;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return '';
  const parsed = new Date(date + 'T00:00:00Z');
  return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === date ? date : '';
}

function mailRecordSeconds(entry) {
  const match = String(entry.time || '').match(/^(\d{1,2}):(\d{2})(?::(\d{2}))?$/);
  if (!match) return null;
  const [hour, minute, second] = match.slice(1).map(value => Number(value || 0));
  return hour < 24 && minute < 60 && second < 60 ? hour * 3600 + minute * 60 + second : null;
}

function mailRecordPriority(entry, items = []) {
  const priority = entry.priority || items.find(item => item.id === entry.projectId)?.priority;
  return /^P[0-3]$/.test(priority) ? priority : entry.kind === 'urgent' ? 'P0' : 'P2';
}

function mailRecordTitle(entry) {
  return entry.kind === 'promotion' ? String(entry.title || '').replace(/（已排除）/g, '') : entry.title;
}

function selectMailRecords(entries, { filter = 'relevant', order = 'latest', snapshotDate = '', items = [] } = {}) {
  const direction = order === 'oldest' ? 1 : -1;
  return entries.filter(entry => {
    if (filter === 'all') return true;
    if (entry.kind === 'promotion') return false;
    return filter !== 'action' || ['P0', 'P1'].includes(mailRecordPriority(entry, items));
  }).sort((a, b) => {
    if (filter === 'action') {
      const priorityOrder = mailRecordPriority(a, items).localeCompare(mailRecordPriority(b, items));
      if (priorityOrder) return priorityOrder;
    }
    const aDate = mailRecordDate(a, snapshotDate), bDate = mailRecordDate(b, snapshotDate);
    // Unknown dates/times stay after dated records in either direction.
    if (!aDate || !bDate) return Number(!aDate) - Number(!bDate);
    const dateOrder = aDate.localeCompare(bDate);
    if (dateOrder) return direction * dateOrder;
    const aTime = mailRecordSeconds(a), bTime = mailRecordSeconds(b);
    if (aTime === null || bTime === null) return Number(aTime === null) - Number(bTime === null);
    return direction * (aTime - bTime);
  });
}

function todayCard(entry) {
  const date = mailRecordDate(entry, model.snapshotDate || BOOTSTRAP.snapshotDate);
  const time = mailRecordSeconds(entry) === null ? '' : entry.time;
  const priority = mailRecordPriority(entry, model.items);
  const direction = entry.direction === 'sent' ? ' · 已发送' : entry.direction === 'received' ? ' · 收到' : '';
  return `<article id="mail-record-${attr(entry.id)}" class="today-full-card ${entry.kind === 'urgent' ? 'urgent' : ''} ${entry.kind === 'promotion' ? 'today-promotional' : ''}" data-mail-id="${attr(entry.id)}" data-mail-date="${attr(date)}" data-mail-time="${attr(time)}" data-mail-priority="${priority}"><div class="today-time"><time${date ? ` datetime="${attr(date + (time ? 'T' + time : ''))}"` : ''}><span class="mail-record-date">${esc(date || '日期未记录')}</span>${time ? `<span class="mail-record-clock">${esc(time)}</span>` : ''}</time><small>德国时间${esc(direction)}</small></div><div><div class="today-category"><span class="small-badge ${priority.toLowerCase()}">${priority}</span> ${esc(entry.kind === 'promotion' ? '推广邮件' : TODAY_LABELS[entry.kind] || '邮件记录')} · ${esc(entry.count || 1)} 封原始邮件</div><h3>${esc(mailRecordTitle(entry))}</h3><p>${esc(entry.detail)}</p>${entry.action ? `<div class="today-next"><strong>下一步：</strong>${esc(entry.action)}</div>` : ''}<div class="today-source">Outlook · ${esc(date || '日期未记录')} · ${esc(entry.sourceSubject)}</div>${entry.projectId ? `<button type="button" class="today-project-button" data-open="${attr(entry.projectId)}">打开对应项目 ↗</button>` : ''}</div></article>`;
}

function renderToday() {
  const all = model.todayUpdates || [];
  const options = { order: state.todayOrder || 'latest', snapshotDate: model.snapshotDate || BOOTSTRAP.snapshotDate, items: model.items };
  const labels = { relevant: '非推广', action: '优先处理', all: '全部' };
  const counts = {};
  document.querySelectorAll('[data-today-filter]').forEach(button => {
    const filter = button.dataset.todayFilter;
    counts[filter] = selectMailRecords(all, { ...options, filter }).length;
    button.textContent = `${labels[filter]} ${counts[filter]} 条${filter === 'all' ? '（含推广）' : ''}`;
    const selected = filter === state.todayFilter;
    button.classList.toggle('active', selected);
    button.setAttribute('aria-pressed', String(selected));
  });
  const records = selectMailRecords(all, { ...options, filter: state.todayFilter });
  document.getElementById('mailTimeOrder').value = options.order;
  const timeOrder = options.order === 'oldest' ? '最早在前' : '最新在前';
  document.getElementById('mailListSummary').textContent = `${state.todayFilter === 'action' ? '优先处理 · 先 P0，再 P1' : state.todayFilter === 'all' ? '全部邮件（含推广）' : '邮件记录（不含推广）'} · 显示 ${records.length} 条 · ${state.todayFilter === 'action' ? '各优先级内' : ''}${timeOrder} · 德国时间`;
  const promotions = records.filter(entry => entry.kind === 'promotion');
  const promotionNotice = document.getElementById('mailPromotionNotice');
  promotionNotice.hidden = state.todayFilter !== 'all' || !promotions.length;
  promotionNotice.innerHTML = promotionNotice.hidden ? '' : `<strong>已包含 ${promotions.length} 条推广邮件</strong><p>完整列表仍按邮件时间排列。点击下列邮件可跳到对应记录。</p><ul>${promotions.map(entry => `<li><a href="#${attr(encodeURIComponent('mail-record-' + entry.id))}">${esc(mailRecordTitle(entry))}</a><span>${esc(mailRecordDate(entry, options.snapshotDate))} ${esc(entry.time)}</span></li>`).join('')}</ul>`;
  document.getElementById('todayMailList').innerHTML = records.length ? records.map(todayCard).join('') : '<div class="empty">当前筛选没有邮件记录。</div>';
}
