const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const context = { Date };
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/atlas_mail.js'), 'utf8'), context);
const ids = entries => Array.from(entries, entry => entry.id);
const entry = (id, date, time, extra = {}) => ({ id, date, time, kind: 'action', priority: 'P1', ...extra });

test('historical backfills, same-day mail and undated seed records sort by mail time', () => {
  // Backfills arrive first in storage; seed entries have no individual date.
  const records = [
    entry('backfill', '2026-07-29', '12:10'),
    entry('morning', '2026-10-09', '08:56'),
    entry('seed', undefined, '11:46'),
    entry('evening', '2026-10-09', '17:13'),
    entry('prior-year', '2025-12-31', '23:59'),
    entry('afternoon', '2026-10-09', '14:03'),
  ];
  const original = JSON.stringify(records);
  const options = { snapshotDate: '2026-10-08' };
  assert.deepEqual(ids(context.selectMailRecords(records, options)), ['evening', 'afternoon', 'morning', 'seed', 'backfill', 'prior-year']);
  assert.deepEqual(ids(context.selectMailRecords(records, { ...options, order: 'oldest' })), ['prior-year', 'backfill', 'seed', 'morning', 'afternoon', 'evening']);
  assert.equal(JSON.stringify(records), original, 'rendering must not reorder or change saved data');
});

test('priority filtering excludes low-priority actions and promotions, including important info/operations', () => {
  const records = [
    entry('low-action', '2026-10-09', '18:00', { priority: 'P2' }),
    entry('important-info', '2026-10-09', '17:00', { kind: 'info' }),
    entry('promo', '2026-10-09', '16:00', { kind: 'promotion', priority: 'P0' }),
    entry('important-operation', '2026-10-09', '15:00', { kind: 'operation', priority: 'P0' }),
    entry('linked', '2026-10-09', '14:00', { priority: undefined, projectId: 'project' }),
    entry('recorded-low-priority', '2026-10-09', '13:00', { priority: 'P2', projectId: 'project' }),
  ];
  const options = { items: [{ id: 'project', priority: 'P1' }] };
  assert.deepEqual(ids(context.selectMailRecords(records, { ...options, filter: 'action' })), ['important-operation', 'important-info', 'linked']);
  assert.equal(context.selectMailRecords(records, options).length, 5);
  assert.equal(context.selectMailRecords(records, { ...options, filter: 'all' }).length, 6);
});

test('every filter retains the selected chronological order', () => {
  const records = [
    entry('old', '2026-10-08', '23:00'),
    entry('new', '2026-10-09', '01:00'),
    entry('middle', '2026-10-09', '00:30', { kind: 'operation' }),
  ];
  for (const filter of ['relevant', 'action', 'all']) {
    assert.deepEqual(ids(context.selectMailRecords(records, { filter })), ['new', 'middle', 'old']);
    assert.deepEqual(ids(context.selectMailRecords(records, { filter, order: 'oldest' })), ['old', 'middle', 'new']);
  }
});

test('priority mode places P0 first and sorts chronologically within each priority', () => {
  const records = [
    entry('p1-new', '2026-10-09', '17:13'),
    entry('p0-old', '2026-10-08', '11:46', { priority: 'P0' }),
    entry('p1-old', '2026-10-09', '08:56'),
    entry('p0-new', '2026-10-09', '15:10', { priority: 'P0' }),
  ];
  assert.deepEqual(ids(context.selectMailRecords(records, { filter: 'action' })), ['p0-new', 'p0-old', 'p1-new', 'p1-old']);
  assert.deepEqual(ids(context.selectMailRecords(records, { filter: 'action', order: 'oldest' })), ['p0-old', 'p0-new', 'p1-old', 'p1-new']);
  for (const filter of ['relevant', 'all']) {
    assert.deepEqual(ids(context.selectMailRecords(records, { filter })), ['p1-new', 'p0-new', 'p1-old', 'p0-old']);
  }
});

test('invalid dates and missing times do not become current or newest mail', () => {
  const records = [
    entry('invalid-date', '2026-02-30', '09:00'),
    entry('missing-time', '2026-10-09', ''),
    entry('invalid-time', '2026-10-09', '25:00'),
    entry('valid', '2026-10-09', '9:05'),
  ];
  for (const order of ['latest', 'oldest']) {
    assert.deepEqual(ids(context.selectMailRecords(records, { order })), ['valid', 'missing-time', 'invalid-time', 'invalid-date']);
  }
  assert.equal(context.mailRecordDate({ time: '12:00' }, '2026-10-08'), '2026-10-08');
});
