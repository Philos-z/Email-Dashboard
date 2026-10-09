import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import json

import pytest

from test_server import client
import server
import mail_sync
import outlook_mcp
import ai_proxy


def raw_mail(identity='m1', received='2026-10-09T10:00:00Z'):
    return {'id': identity, 'subject': 'Confirmed project update', 'receivedDateTime': received,
            'body': {'contentType': 'html', 'content': '<p>Written confirmation received.</p>'}}


FOLDERS = [
    {'id': 'inbox', 'path': '收件箱', 'kind': 'inbox', 'direction': 'received'},
    {'id': 'archive', 'path': '存档/项目', 'kind': 'archive', 'direction': 'received'},
    {'id': 'sent', 'path': '已发送邮件', 'kind': 'sentitems', 'direction': 'sent'},
]


def mock_mailbox(monkeypatch, rows):
    calls = []
    folders = rows if isinstance(rows, dict) else {'inbox': rows}

    class Mailbox:
        async def list_watched_folders(self):
            return FOLDERS

        async def list_messages(self, since, through, skip=0, top=50, *, folder_id, direction='received'):
            calls.append((since, through, skip, top, folder_id, direction))
            clock = 'sentDateTime' if direction == 'sent' else 'receivedDateTime'
            rows = [row for row in folders.get(folder_id, [])
                    if mail_sync.parse_date(since) <= mail_sync.parse_date(row.get(clock) or row['receivedDateTime']) <= mail_sync.parse_date(through)]
            page = rows[skip:skip + top]
            return {'value': page, 'has_more': skip + top < len(rows), 'next_from_index': skip + len(page)}

    @asynccontextmanager
    async def connect():
        yield Mailbox()

    monkeypatch.setattr(outlook_mcp, 'connect', connect)
    return calls


def finish(client):
    async def wait():
        await asyncio.gather(*list(server.app.state.mail_sync.tasks))
    client.portal.call(wait)
    return client.get('/api/outlook/status').json()


def proposal(item, mail='m1', **fields):
    return json.dumps({'changes': [{'itemId': item, 'mailIds': [mail], 'summary': '已收到项目书面确认',
                                   'fields': fields, 'milestonesAdd': [{'text': '收到本次书面确认', 'done': True}]}],
                       'newItems': []})


def test_schedule_persists_and_manual_does_not_reset_clock(client, monkeypatch):
    mock_mailbox(monkeypatch, [])
    assert client.put('/api/outlook/schedule', json={'enabled': 'yes'}).status_code == 400
    enabled = client.put('/api/outlook/schedule', json={'enabled': True}).json()
    assert enabled['intervalSeconds'] == 3600
    assert client.get('/api/ai/config').json()['automaticCalls'] is True
    assert enabled['nextRunAt'].endswith(':00:00Z')
    client.post('/api/outlook/refresh')
    status = finish(client)
    assert status['nextRunAt'] == enabled['nextRunAt']
    assert status['runs'][0]['status'] == 'success'
    fresh = mail_sync.MailSyncService(server)
    assert fresh.status()['enabled'] is True
    assert fresh.status()['checkedThrough'] == status['checkedThrough']
    assert client.put('/api/outlook/schedule', json={'enabled': False}).json()['nextRunAt'] is None


def test_empty_mail_skips_model_but_records_success(client, monkeypatch):
    mock_mailbox(monkeypatch, [])
    async def unexpected(*args, **kwargs):
        pytest.fail('No new mail must not call the model')
    monkeypatch.setattr(ai_proxy, 'complete', unexpected)
    old = client.get('/api/snapshot').json()
    assert client.post('/api/outlook/refresh').status_code == 202
    status = finish(client)
    assert status['runs'][0]['fetched'] == 0
    assert status['runs'][0]['status'] == 'success'
    assert status['checkedThrough']
    assert client.get('/api/snapshot').json()['model']['items'] == old['model']['items']


def test_auto_commit_evidence_and_dedup(client, monkeypatch):
    mock_mailbox(monkeypatch, [raw_mail()])
    old = client.get('/api/snapshot').json()
    item = old['model']['items'][0]
    calls = []
    async def complete(config, messages, **kwargs):
        calls.append(messages)
        return {'reply': proposal(item['id'], status='waiting', next='等待下一封确认')}
    monkeypatch.setattr(ai_proxy, 'complete', complete)
    client.post('/api/outlook/refresh')
    assert finish(client)['runs'][0]['changed'] == 1
    snap = client.get('/api/snapshot').json()
    updated = next(it for it in snap['model']['items'] if it['id'] == item['id'])
    assert updated['status'] == 'waiting'
    assert updated['notes'] == item['notes']
    assert updated['mailEvidence'][-1] == {'mailId': 'm1', 'date': '2026-10-09', 'subject': 'Confirmed project update', 'summary': '已收到项目书面确认', 'direction': 'received', 'folder': '收件箱'}
    event = snap['model']['todayUpdates'][0]
    assert event['projectId'] == item['id'] and event['count'] == 1
    assert event['action'] == '等待下一封确认'
    client.post('/api/outlook/refresh')
    assert finish(client)['runs'][0]['fetched'] == 0
    assert len(calls) == 1
    assert client.get('/api/snapshot').json()['model']['todayUpdates'] == snap['model']['todayUpdates']


@pytest.mark.parametrize('bad', ['not JSON', '{"changes":[],"newItems":{}}', 'unknown-evidence', 'protected-field', 'invalid-date'])
def test_invalid_model_response_is_atomic(client, monkeypatch, bad):
    mock_mailbox(monkeypatch, [raw_mail()])
    old = client.get('/api/snapshot').json()
    item = old['model']['items'][0]
    if bad == 'unknown-evidence':
        bad = proposal(item['id'], 'invented-id', status='done')
    elif bad == 'protected-field':
        bad = proposal(item['id'], notes='overwritten')
    elif bad == 'invalid-date':
        bad = proposal(item['id'], deadline='2026-02-30')
    async def complete(*args, **kwargs):
        return {'reply': bad}
    monkeypatch.setattr(ai_proxy, 'complete', complete)
    client.post('/api/outlook/refresh')
    status = finish(client)
    assert status['runs'][0]['status'] == 'failed'
    assert status['checkedThrough'] is None
    assert client.get('/api/snapshot').json() == old
    with server.connect() as con:
        assert con.execute('SELECT COUNT(*) FROM mail_seen').fetchone()[0] == 0


def test_conflict_reanalyzes_current_state(client, monkeypatch):
    mock_mailbox(monkeypatch, [raw_mail()])
    old = client.get('/api/snapshot').json()
    identity = old['model']['items'][0]['id']
    calls = []
    async def complete(config, messages, **kwargs):
        calls.append(json.loads(messages[-1]['content']))
        if len(calls) == 1:
            with server.DB_LOCK, server.connect() as con:
                row = con.execute('SELECT model,revision FROM state WHERE id=1').fetchone()
                model = json.loads(row['model'])
                model['items'][0].update(notes='用户刚保存的备注', next='用户刚修改的下一步')
                server.update_state(model, row['revision'] + 1, con)
        return {'reply': proposal(identity, status='waiting')}
    monkeypatch.setattr(ai_proxy, 'complete', complete)
    client.post('/api/outlook/refresh')
    assert finish(client)['runs'][0]['status'] == 'success'
    assert len(calls) == 2
    assert calls[1]['projects'][0]['next'] == '用户刚修改的下一步'
    current = client.get('/api/snapshot').json()['model']['items'][0]
    assert current['notes'] == '用户刚保存的备注'
    assert current['next'] == '用户刚修改的下一步'


def test_overlapping_clicks_share_one_job(client, monkeypatch):
    mock_mailbox(monkeypatch, [raw_mail()])
    async def complete(*args, **kwargs):
        await asyncio.sleep(.15)
        return {'reply': '{"changes":[],"newItems":[]}'}
    monkeypatch.setattr(ai_proxy, 'complete', complete)
    first = client.post('/api/outlook/refresh').json()
    second = client.post('/api/outlook/refresh').json()
    assert second == {'jobId': first['jobId'], 'alreadyRunning': True}
    assert len(finish(client)['runs']) == 1


def test_hourly_due_runs_without_browser_refresh(client, monkeypatch):
    monkeypatch.setattr(mail_sync, 'utcnow', lambda: mail_sync.parse_date('2026-10-09T12:00:00Z'))
    mock_mailbox(monkeypatch, [])
    client.put('/api/outlook/schedule', json={'enabled': True})
    with server.DB_LOCK, server.connect() as con:
        con.execute('UPDATE mail_sync_config SET next_run_at=?', (mail_sync.stamp(mail_sync.utcnow() - timedelta(seconds=1)),))
    async def tick():
        service = server.app.state.mail_sync
        service.scheduler.cancel()
        await asyncio.gather(service.scheduler, return_exceptions=True)
        await service.start()
        await asyncio.sleep(.05)
    client.portal.call(tick)
    status = finish(client)
    assert status['runs'][0]['trigger'] == 'hourly'
    assert status['runs'][0]['status'] == 'success'
    assert mail_sync.parse_date(status['nextRunAt']) > mail_sync.utcnow()


def test_batch_limit_never_skips_unprocessed_mail(client, monkeypatch):
    rows = [raw_mail(f'm{i}', f'2026-10-09T10:{i:02d}:00Z') for i in range(5)]
    mock_mailbox(monkeypatch, rows)
    monkeypatch.setattr(mail_sync, 'MAX_MESSAGES', 3)
    result = client.portal.call(server.app.state.mail_sync.fetch_mail, server.read_snapshot(), '2026-10-09T11:00:00Z')
    assert [m['id'] for m in result[0]] == ['m0']
    state = next(row for row in result[1]['folders'] if row['folder_id'] == 'inbox')
    assert state['next_skip'] == 1
    assert state['window_through'] == '2026-10-09T11:00:00Z'
    assert result[2] is True


def test_new_item_appears_in_mail_records(client, monkeypatch):
    mock_mailbox(monkeypatch, [raw_mail()])
    async def complete(*args, **kwargs):
        return {'reply': json.dumps({'changes': [], 'newItems': [{
            'title': '新事项', 'category': '个人事务', 'status': 'waiting', 'priority': 'P2',
            'mode': 'project', 'next': '等待回复', 'blocker': '', 'mailIds': ['m1'], 'summary': '已确认收到新事项通知'}]})}
    monkeypatch.setattr(ai_proxy, 'complete', complete)
    client.post('/api/outlook/refresh')
    assert finish(client)['runs'][0]['added'] == 1
    model = client.get('/api/snapshot').json()['model']
    item = next(it for it in model['items'] if it['title'] == '新事项')
    assert model['todayUpdates'][0]['projectId'] == item['id']
    assert model['todayUpdates'][0]['action'] == '等待回复'


def test_mcp_failure_keeps_cursor_for_retry(client, monkeypatch):
    @asynccontextmanager
    async def broken():
        raise outlook_mcp.OutlookError('Outlook 授权失效')
        yield
    monkeypatch.setattr(outlook_mcp, 'connect', broken)
    old = client.get('/api/snapshot').json()
    client.post('/api/outlook/refresh')
    status = finish(client)
    assert status['checkedThrough'] is None
    assert status['runs'][0]['status'] == 'failed'
    assert status['runs'][0]['message'] == 'Outlook 授权失效'
    assert client.get('/api/snapshot').json() == old


def test_auth_origin_and_read_only_mcp(client, monkeypatch):
    assert client.post('/api/outlook/refresh', headers={'Origin': 'https://evil.example'}).status_code == 403
    saved = client.cookies.get('atlas_session')
    client.cookies.clear()
    assert client.post('/api/outlook/refresh').status_code == 401
    assert client.put('/api/outlook/schedule', json={'enabled': True}).status_code == 401
    client.cookies.set('atlas_session', saved)
    mcp = outlook_mcp.OutlookMCP(None)
    mcp.thread_id = 'ephemeral-tool-only'
    calls = []
    async def rpc(method, params):
        calls.append((method, params))
        return {'structuredContent': {'value': []}}
    monkeypatch.setattr(mcp, 'rpc', rpc)
    asyncio.run(mcp.list_messages('2026-10-09T00:00:00Z', '2026-10-09T01:00:00Z', folder_id='sent-id', direction='sent'))
    assert calls[0][0] == 'mcpServer/tool/call'
    assert calls[0][1]['tool'] == 'microsoft_outlook_email.list_messages'
    assert calls[0][1]['arguments'] == {
        'folder_id': 'sent-id', 'filter': 'sentDateTime ge 2026-10-09T00:00:00Z and sentDateTime le 2026-10-09T01:00:00Z',
        'order_by': 'sentDateTime asc', 'skip': 0, 'top': 50}


@pytest.mark.parametrize('current,expected', [
    ('2026-10-09T22:20:00+02:00', '2026-10-09T23:00:00+02:00'),
    ('2026-10-09T23:00:00+02:00', '2026-10-10T06:00:00+02:00'),
    ('2026-10-10T00:00:00+02:00', '2026-10-10T06:00:00+02:00'),
    ('2026-10-10T05:59:59+02:00', '2026-10-10T06:00:00+02:00'),
    ('2026-10-10T06:00:00+02:00', '2026-10-10T07:00:00+02:00'),
    ('2026-03-29T00:30:00+01:00', '2026-03-29T06:00:00+02:00'),
    ('2026-10-25T00:30:00+02:00', '2026-10-25T06:00:00+01:00'),
])
def test_next_hour_skips_german_night_including_clock_changes(monkeypatch, current, expected):
    monkeypatch.setattr(mail_sync, 'utcnow', lambda: mail_sync.parse_date(current))
    assert mail_sync.next_hour() == mail_sync.stamp(mail_sync.parse_date(expected))


def test_night_blocks_overdue_auto_jobs_but_allows_manual_refresh(client, monkeypatch):
    monkeypatch.setattr(mail_sync, 'utcnow', lambda: mail_sync.parse_date('2026-10-10T02:00:00+02:00'))
    calls = mock_mailbox(monkeypatch, [])
    result = server.app.state.mail_sync.trigger('hourly')
    assert result['deferred'] is True
    assert not calls
    assert client.get('/api/outlook/status').json()['runs'] == []
    status = client.put('/api/outlook/schedule', json={'enabled': True}).json()
    assert status['nextRunAt'] == '2026-10-10T04:00:00Z'
    assert status['quietNow'] is True
    assert status['quietHours']['timeZone'] == 'Europe/Berlin'
    client.post('/api/outlook/refresh')
    status = finish(client)
    assert status['runs'][0]['status'] == 'success'
    assert status['runs'][0]['trigger'] == 'manual'
    assert status['nextRunAt'] == '2026-10-10T04:00:00Z'


def test_old_midnight_schedule_moves_to_six_on_restart(client):
    with server.connect() as con:
        con.execute("UPDATE mail_sync_config SET enabled=1,next_run_at='2026-10-09T22:00:00Z'")
    service = mail_sync.MailSyncService(server)
    status = service.status()
    assert status['enabled'] is True
    assert status['nextRunAt'] == '2026-10-10T04:00:00Z'


def test_watch_scope_includes_sent_and_archive_descendants_only():
    def folder(identity, name, parent='root', path=None, known=None):
        return {'id': identity, 'display_name': name, 'parent_folder_id': parent,
                'path': path or name, 'well_known_name': known}
    rows = [folder('inbox', '收件箱'), folder('archive', 'Archive'), folder('zh-archive', '存档'),
            folder('sent', '已发送邮件'), folder('draft', '草稿'), folder('outbox', '发件箱'),
            folder('junk', '垃圾邮件'), folder('deleted', '已删除邮件'), folder('custom', '其他'),
            folder('nested-sent', '已发送邮件', 'custom', '其他/已发送邮件'),
            folder('deep', '子案件', 'case', '存档/案件/子案件'),
            folder('case', '案件', 'zh-archive', '存档/案件'),
            folder('sent-child', '申请', 'sent', '已发送邮件/申请')]
    watched = outlook_mcp.watched_folders(rows)
    assert {f['id'] for f in watched} == {'inbox', 'archive', 'zh-archive', 'sent', 'case', 'deep', 'sent-child'}
    assert {f['id'] for f in watched if f['direction'] == 'sent'} == {'sent', 'sent-child'}
    with pytest.raises(outlook_mcp.OutlookError):
        outlook_mcp.watched_folders([row for row in rows if row['id'] != 'sent'])


def test_new_sent_scope_backfills_before_existing_inbox_cursor(client, monkeypatch):
    sent = {**raw_mail('sent-old', '2026-10-07T11:00:00Z'), 'sentDateTime': '2026-10-07T10:00:00Z'}
    calls = mock_mailbox(monkeypatch, {'sent': [sent], 'archive': [raw_mail('archived', '2026-10-07T12:00:00Z')]})
    with server.connect() as con:
        con.execute("UPDATE mail_sync_config SET cursor='2026-10-09T10:00:00Z'")
    captured = []
    async def complete(config, messages, **kwargs):
        captured.append(messages)
        return {'reply': '{"changes":[],"newItems":[]}'}
    monkeypatch.setattr(ai_proxy, 'complete', complete)
    client.post('/api/outlook/refresh')
    status = finish(client)
    assert status['runs'][0]['status'] == 'success'
    assert status['runs'][0]['fetched'] == 2
    assert '已发送 1 封' in status['runs'][0]['message']
    mails = json.loads(captured[0][-1]['content'])['newMail']
    assert mails[0]['id'] == 'sent-old' and mails[0]['direction'] == 'sent'
    assert mails[0]['occurred'] == '2026-10-07T10:00:00Z'
    assert '不代表对方收悉' in captured[0][0]['content']
    assert next(c for c in calls if c[4] == 'sent')[0] < '2026-10-07T00:00:00Z'
    assert next(c for c in calls if c[4] == 'inbox')[0] == '2026-10-09T09:55:00Z'


def test_same_time_mail_resumes_each_folder_without_loss_or_duplicates(client, monkeypatch):
    mock_mailbox(monkeypatch, {folder: [raw_mail(f'{folder}-{i}') for i in range(count)]
                             for folder, count in [('inbox', 4), ('archive', 2), ('sent', 7)]})
    monkeypatch.setattr(mail_sync, 'MAX_MESSAGES', 3)
    analyzed = []
    async def complete(config, messages, **kwargs):
        mails = json.loads(messages[-1]['content'])['newMail']
        assert len(mails) <= 3
        analyzed.extend(m['id'] for m in mails)
        return {'reply': '{"changes":[],"newItems":[]}'}
    monkeypatch.setattr(ai_proxy, 'complete', complete)
    for iteration in range(15):
        client.post('/api/outlook/refresh')
        status = finish(client)
        assert status['runs'][0]['status'] == 'success'
        if iteration == 0:
            fresh = mail_sync.MailSyncService(server)
            assert fresh.status()['backfillPending'] is True
            with server.connect() as con:
                sent = con.execute("SELECT * FROM mail_folder_sync WHERE folder_id='sent'").fetchone()
            assert sent['next_skip'] == 1 and sent['window_through']
            result = client.portal.call(fresh.fetch_mail, server.read_snapshot(), mail_sync.stamp())
            assert 'sent-0' not in {mail['id'] for mail in result[0]}
        if not status['backfillPending']:
            break
    assert not status['backfillPending']
    assert len(analyzed) == len(set(analyzed)) == 13
    with server.connect() as con:
        assert con.execute('SELECT COUNT(*) FROM mail_seen').fetchone()[0] == 13


def test_sent_failure_does_not_commit_other_folder_progress(client, monkeypatch):
    class Mailbox:
        async def list_watched_folders(self):
            return FOLDERS

        async def list_messages(self, since, through, **kwargs):
            if kwargs['direction'] == 'sent':
                raise outlook_mcp.OutlookError('已发送邮件读取失败')
            return {'value': [raw_mail()], 'has_more': False}

    @asynccontextmanager
    async def connect():
        yield Mailbox()
    monkeypatch.setattr(outlook_mcp, 'connect', connect)
    old = client.get('/api/snapshot').json()
    client.post('/api/outlook/refresh')
    status = finish(client)
    assert status['runs'][0]['status'] == 'failed'
    assert status['checkedThrough'] is None
    assert client.get('/api/snapshot').json() == old
    with server.connect() as con:
        assert con.execute('SELECT COUNT(*) FROM mail_folder_sync').fetchone()[0] == 0
        assert con.execute('SELECT COUNT(*) FROM mail_seen').fetchone()[0] == 0


def test_historical_sent_evidence_cannot_regress_current_project(client):
    snapshot = server.read_snapshot()
    item = snapshot['model']['items'][0]
    mail = mail_sync.normalize_message({**raw_mail('old-sent'), 'sentDateTime': '2026-07-20T10:00:00Z'}, 'sent', '已发送邮件')
    payload = mail_sync.build_proposal(proposal(item['id'], mail='old-sent', status='done', blocker=''), [mail], snapshot, server)
    change = payload['changes'][0]
    assert change['fields'] == {}
    assert change['mailEvidenceAdd'][0]['direction'] == 'sent'
    assert change['mailEvidenceAdd'][0]['date'] == '2026-07-20'
    assert '已发送' in change['milestonesAdd'][0]['source']


def test_own_message_remains_sent_after_archiving():
    raw = {**raw_mail(), 'sentDateTime': '2026-10-08T10:00:00Z',
           'sender': {'emailAddress': {'address': 'ME@example.com'}}}
    mail = mail_sync.normalize_message(raw, 'received', '存档/项目', 'me@example.com')
    assert mail['direction'] == 'sent'
    assert mail['occurred'] == '2026-10-08T10:00:00Z'
    assert mail['folder'] == '存档/项目'
