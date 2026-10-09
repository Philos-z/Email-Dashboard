import json

import pytest

from test_server import client
import ai_proxy
import item_context
import server


@pytest.fixture
def context_client(client, monkeypatch):
    client.portal.call(server.app.state.item_context.stop)
    monkeypatch.setattr(item_context, 'DEBOUNCE_SECONDS', 0)
    return client


def output(**fields):
    return {'reply': json.dumps({'contextSummary': '用户已预约10月16日14:00测量；结果与书面报告仍待取得。',
                                'fields': {'status': 'blocked', 'next': '按10月16日14:00预约测量，取得报告后核实后续安排。',
                                           'blocker': '仍待测量报告与安全结论', **fields}}, ensure_ascii=False)}


def edit(client, mutate):
    snap = client.get('/api/snapshot').json()
    item = snap['model']['items'][0]
    mutate(item)
    response = client.put('/api/items/'+item['id'], json={'item': item, 'expectedVersion': snap['itemRevisions'][item['id']]})
    assert response.status_code == 200, response.text
    return item, response.json()


def process(client):
    return client.portal.call(server.app.state.item_context.process_next)


def test_saved_milestone_updates_context_state_and_next(context_client, monkeypatch):
    c = context_client
    calls = []
    original = c.get('/api/snapshot').json()['model']['items'][0]
    async def complete(config, messages, **kwargs):
        calls.append(json.loads(messages[-1]['content']))
        return output()
    monkeypatch.setattr(ai_proxy, 'complete', complete)
    item, response = edit(c, lambda it: it['milestones'].append({'text': '10月16日14点会做vermessung', 'done': False, 'certainty': 'user', 'source': '用户手动添加'}))
    assert response['contextUpdate']['status'] == 'queued'
    assert c.get('/api/snapshot').json()['model']['items'][0]['next'] == original['next']
    assert process(c) is True
    saved = c.get('/api/snapshot').json()['model']['items'][0]
    assert '10月16日14:00' in saved['next']
    assert saved['status'] == 'blocked'
    assert saved['notes'] == original['notes']
    assert saved['mailEvidence'] == original.get('mailEvidence')
    assert saved['milestones'] == item['milestones']
    assert saved['contextSummary'] and saved['contextUpdatedAt']
    assert calls[0]['item']['milestones'][-1]['text'] == '10月16日14点会做vermessung'
    status = c.get('/api/items/'+item['id']+'/context-status').json()
    assert status['status'] == 'success'
    assert status['result_version'] == response['itemVersion'] + 1
    assert any(e['kind'] == 'item_context' for e in c.get('/api/audit').json()['events'])
    assert process(c) is False  # Analysis writes never enqueue another analysis.


def test_background_worker_runs_after_save_without_refresh_button(context_client, monkeypatch):
    import asyncio
    async def complete(*args, **kwargs):
        return output()
    monkeypatch.setattr(ai_proxy, 'complete', complete)
    item, _ = edit(context_client, lambda it: it.update(notes='已安排10月16日14:00测量'))
    async def run():
        service = server.app.state.item_context
        await service.start()
        for _ in range(50):
            if service.status(item['id'])['status'] == 'success':
                return
            await asyncio.sleep(.01)
        pytest.fail('Saved edit did not trigger background reconciliation')
    context_client.portal.call(run)
    assert context_client.get('/api/snapshot').json()['model']['items'][0]['contextSummary']


def test_noop_save_does_not_call_model(context_client, monkeypatch):
    async def unexpected(*args, **kwargs):
        pytest.fail('No-op saves should not consume model tokens')
    monkeypatch.setattr(ai_proxy, 'complete', unexpected)
    _, response = edit(context_client, lambda it: None)
    assert response['contextUpdate'] is None
    assert process(context_client) is False


def test_preserve_fields_user_directly_set(context_client, monkeypatch):
    captured = []
    async def complete(config, messages, **kwargs):
        captured.append(json.loads(messages[-1]['content']))
        return output(status='done', next='模型建议覆盖', blocker='')
    monkeypatch.setattr(ai_proxy, 'complete', complete)
    edit(context_client, lambda it: it.update(status='in_progress', next='用户指定的下一步', blocker='用户指定的阻碍'))
    process(context_client)
    saved = context_client.get('/api/snapshot').json()['model']['items'][0]
    assert saved['status'] == 'in_progress'
    assert saved['next'] == '用户指定的下一步'
    assert saved['blocker'] == '用户指定的阻碍'
    assert captured[0]['protectedFields'] == ['blocker', 'next', 'status']


def test_coalesce_rapid_edits_and_keep_latest_context(context_client, monkeypatch):
    calls = []
    async def complete(config, messages, **kwargs):
        calls.append(json.loads(messages[-1]['content']))
        return output()
    monkeypatch.setattr(ai_proxy, 'complete', complete)
    edit(context_client, lambda it: it.update(notes='旧时间10月15日'))
    item, result = edit(context_client, lambda it: it.update(notes='更正为10月16日14:00'))
    assert result['contextUpdate']['generation'] == 2
    process(context_client)
    assert len(calls) == 1
    assert calls[0]['item']['notes'] == '更正为10月16日14:00'
    assert item_context.ItemContextService(server).status(item['id'])['status'] == 'success'


@pytest.mark.parametrize('bad', ['private raw response', '{"contextSummary":"x","fields":{"notes":"erase"}}',
                                '{"contextSummary":"x","fields":{"status":"invented","next":"x","blocker":""}}'])
def test_invalid_analysis_never_rolls_back_saved_manual_edit(context_client, monkeypatch, bad):
    async def complete(*args, **kwargs):
        return {'reply': bad}
    monkeypatch.setattr(ai_proxy, 'complete', complete)
    item, response = edit(context_client, lambda it: it.update(notes='应当始终保存的用户备注'))
    before = context_client.get('/api/snapshot').json()
    process(context_client)
    assert context_client.get('/api/snapshot').json() == before
    status = context_client.get('/api/items/'+item['id']+'/context-status').json()
    assert status['status'] == 'failed'
    assert 'private raw response' not in status['message']


def test_new_manual_edit_during_analysis_discards_old_result(context_client, monkeypatch):
    calls = []
    async def complete(config, messages, **kwargs):
        calls.append(json.loads(messages[-1]['content']))
        if len(calls) == 1:
            with server.DB_LOCK, server.connect() as con:
                row = con.execute('SELECT model,revision FROM state WHERE id=1').fetchone()
                model = json.loads(row['model'])
                item = model['items'][0]
                item['notes'] = '最新用户说明'
                version = con.execute('SELECT version FROM revisions WHERE id=?', (item['id'],)).fetchone()[0] + 1
                con.execute('UPDATE revisions SET version=? WHERE id=?', (version, item['id']))
                server.update_state(model, row['revision'] + 1, con)
                server.app.state.item_context.enqueue(con, item['id'], version, {'notes'})
            return output(next='过期分析结果')
        return output(next='基于最新说明的下一步')
    monkeypatch.setattr(ai_proxy, 'complete', complete)
    item, _ = edit(context_client, lambda it: it.update(notes='第一次说明'))
    process(context_client)
    assert context_client.get('/api/items/'+item['id']+'/context-status').json()['status'] == 'queued'
    assert context_client.get('/api/snapshot').json()['model']['items'][0]['next'] != '过期分析结果'
    process(context_client)
    saved = context_client.get('/api/snapshot').json()['model']['items'][0]
    assert saved['notes'] == '最新用户说明'
    assert saved['next'] == '基于最新说明的下一步'
    assert calls[-1]['item']['notes'] == '最新用户说明'


def test_email_version_conflict_reanalyzes_and_preserves_other_items(context_client, monkeypatch):
    calls = []
    async def complete(config, messages, **kwargs):
        calls.append(json.loads(messages[-1]['content']))
        with server.DB_LOCK, server.connect() as con:
            row = con.execute('SELECT model,revision FROM state WHERE id=1').fetchone()
            model = json.loads(row['model'])
            if len(calls) == 1:
                item = model['items'][0]
                item['blocker'] = '邮件刚更新的阻碍'
                con.execute('UPDATE revisions SET version=version+1 WHERE id=?', (item['id'],))
            else:
                model['items'][1]['notes'] = '另一项目同时编辑'
            server.update_state(model, row['revision'] + 1, con)
        return output()
    monkeypatch.setattr(ai_proxy, 'complete', complete)
    item, _ = edit(context_client, lambda it: it.update(notes='新说明'))
    process(context_client)
    assert context_client.get('/api/items/'+item['id']+'/context-status').json()['status'] == 'queued'
    process(context_client)
    assert calls[-1]['item']['blocker'] == '邮件刚更新的阻碍'
    assert context_client.get('/api/snapshot').json()['model']['items'][1]['notes'] == '另一项目同时编辑'


def test_refresh_endpoint_auth_and_origin(context_client):
    c = context_client
    identity = c.get('/api/snapshot').json()['model']['items'][0]['id']
    assert c.post('/api/items/'+identity+'/refresh-context', headers={'Origin': 'https://evil.example'}).status_code == 403
    assert c.get('/api/items/missing/context-status').status_code == 404
    assert c.post('/api/items/missing/refresh-context').status_code == 404
    c.cookies.clear()
    assert c.post('/api/items/'+identity+'/refresh-context').status_code == 401
    assert c.get('/api/items/'+identity+'/context-status').status_code == 401
