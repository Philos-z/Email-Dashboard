const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const context = {
  Intl, Date,
  document: { addEventListener() {} },
  setInterval() {},
  PRIORITY: { P0: 0, P1: 1, P2: 2, P3: 3 },
  dateObj: value => new Date(value + 'T00:00:00Z'),
  verifiedDeadline: item => !!item.deadline && item.deadlineVerified !== false && !['done', 'rejected'].includes(item.status),
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/atlas_focus.js'), 'utf8'), context);
const item = overrides => ({ id: 'test', title: 'Test', status: 'blocked', priority: 'P0', last: '2026-10-09', milestones: [], ...overrides });
const plan = text => ({ text, done: false, certainty: 'user' });

test('calendar dates use Berlin across UTC midnight and daylight saving time', () => {
  assert.equal(context.todayInBerlin(new Date('2026-10-09T22:30:00Z')), '2026-10-10');
  assert.equal(context.todayInBerlin(new Date('2026-12-09T23:30:00Z')), '2026-12-10');
});

test('explicit pending user appointments retain the date and time', () => {
  const entries = context.focusDateEntries(item({ milestones: [plan('10月16日14点会做vermessung')] }), '2026-10-09');
  assert.equal(entries.length, 1);
  assert.equal(entries[0].date, '2026-10-16');
  assert.equal(entries[0].time, '14:00');
  assert.equal(entries[0].kind, 'plan');
  assert.equal(context.focusDateEntries(item({ milestones: [plan('2026年10月16日14点半进行测量')] }))[0].time, '14:30');
});

test('completed history, email suggestions and uncertain dates never become appointments', () => {
  for (const milestone of [
    { ...plan('10月16日14点测量'), done: true },
    { ...plan('10月16日14点测量'), certainty: 'confirmed' },
    plan('10月16日14点测量，时间未确认'),
    plan('10月14日或10月16日可选面试'),
    plan('10月14日—16日反馈窗口'),
    plan('10月14日-16日反馈窗口'),
    plan('10月16日14点测量已取消'),
    plan('10月16日14点测量，预计安排'),
  ]) assert.equal(context.focusDateEntries(item({ milestones: [milestone] })).length, 0, milestone.text);
});

test('invalid dates and times are rejected instead of normalizing', () => {
  for (const text of ['2月30日14点测量', '10月16日25点测量', '10月16日14:75测量']) {
    assert.equal(context.focusDateEntries(item({ milestones: [plan(text)] })).length, 0, text);
  }
});

test('unverified and closed deadlines are excluded and duplicate dates are merged', () => {
  assert.equal(context.focusDateEntries(item({ deadline: '2026-10-16', deadlineVerified: false })).length, 0);
  assert.equal(context.focusDateEntries(item({ deadline: '2026-10-16', status: 'done' })).length, 0);
  const entries = context.focusDateEntries(item({ deadline: '2026-10-16', milestones: [plan('10月16日退回旧机')] }));
  assert.equal(entries.length, 1);
  assert.equal(entries[0].kind, 'deadline');
});

test('priority and urgent deadlines precede blocked tasks and routine work', () => {
  const items = [
    item({ id: 'p1', title: 'P1', priority: 'P1', deadline: '2026-10-09' }),
    item({ id: 'blocked', title: 'Blocked' }),
    item({ id: 'due', title: 'Due', status: 'verification', deadline: '2026-10-09' }),
    item({ id: 'soon', title: 'Soon', status: 'in_progress', deadline: '2026-10-16' }),
  ];
  assert.equal(context.focusSort(items, '2026-10-09').map(item => item.id).join(','), 'due,blocked,soon,p1');
});
