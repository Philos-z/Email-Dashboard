import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server
import ai_proxy


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('ATLAS_REQUIRE_LOGIN', '1')
    monkeypatch.setattr(server, 'SEED_PATH', Path(__file__).parent/'fixtures/model.json')
    monkeypatch.setattr(server, 'DATA', tmp_path)
    monkeypatch.setattr(server, 'DB_PATH', tmp_path/'atlas.sqlite3')
    monkeypatch.setattr(server, 'AUTH_PATH', tmp_path/'auth.json')
    monkeypatch.setattr(server, 'SESSION_SECRET_PATH', tmp_path/'session.key')
    monkeypatch.setattr(ai_proxy, 'CONFIG_PATH', tmp_path/'ai_proxy.json')
    salt=os.urandom(16)
    password='strong-password-test-!'
    (tmp_path/'auth.json').write_text(json.dumps({'salt':salt.hex(),'iterations':2000,'hash':hashlib.pbkdf2_hmac('sha256',password.encode(),salt,2000).hex()}))
    with TestClient(server.app) as c:
        r=c.post('/api/login',json={'password':password})
        assert r.status_code==200,r.text
        yield c


def test_login_gate_and_data(client):
    r=client.get('/')
    assert 'window.__ATLAS_SERVER__=' in r.text
    assert 'Outlook MCP' in r.text
    snap=client.get('/api/snapshot').json()
    assert len(snap['model']['items'])==2
    assert snap['revision']==1
    assert client.get('/api/outlook/status').json()['automaticCalls'] is False
    with TestClient(server.app) as other:
        assert other.get('/api/snapshot').status_code == 401
        assert 'window.__ATLAS_SERVER__=' not in other.get('/').text


def test_edit_conflict_and_history(client):
    snap=client.get('/api/snapshot').json()
    item=snap['model']['items'][0]
    p={'item':{**item,'notes':'LAN shared test'},'expectedVersion':snap['itemRevisions'][item['id']]}
    r=client.put('/api/items/'+item['id'],json=p)
    assert r.status_code==200,r.text
    assert client.get('/api/snapshot').json()['model']['items'][0]['notes']=='LAN shared test'
    assert client.put('/api/items/'+item['id'],json=p).status_code==409
    assert any(x['kind']=='edit' for x in client.get('/api/audit').json()['events'])


def test_manual_mcp_preview_and_commit(client):
    snap=client.get('/api/snapshot').json()
    item=snap['model']['items'][0]
    original=item['notes']
    payload={'checkedThrough':'2026-10-09T14:00:00+02:00',
       'changes':[{'itemId':item['id'],'last':'2026-10-09','fields':{'notes':'仅人工确认写入'},
       'mailEvidenceAdd':[{'date':'2026-10-09','subject':'Case update','summary':'测试邮件摘要'}],
       'milestonesAdd':[{'text':'2026-10-09 已核对邮件','done':True,'date':'2026-10-09','source':'Outlook · 2026-10-09 · Case update','certainty':'confirmed'}]}],
       'newItems':[], 'todayUpdatesAdd':[]}
    r=client.post('/api/outlook/preview',json=payload)
    assert r.status_code==200,r.text
    mid=client.get('/api/snapshot').json()
    assert mid['model']['items'][0]['notes']==original
    token=r.json()['token']
    r=client.post('/api/outlook/commit',json={'token':token})
    assert r.status_code==200,r.text
    after=client.get('/api/snapshot').json()['model']['items'][0]
    assert after['notes']=='仅人工确认写入'
    assert after['mailEvidence'][-1]['subject']=='Case update'
    assert client.post('/api/outlook/commit',json={'token':token}).status_code==409


def test_preview_conflict_with_concurrent_edit(client):
    snap=client.get('/api/snapshot').json()
    item=snap['model']['items'][0]
    p={'changes':[{'itemId':item['id'],'fields':{'notes':'pending'}}]}
    token=client.post('/api/outlook/preview',json=p).json()['token']
    item['notes']='edited separately'
    client.put('/api/items/'+item['id'],json={'item':item,'expectedVersion':snap['itemRevisions'][item['id']]})
    assert client.post('/api/outlook/commit',json={'token':token}).status_code==409


def test_bad_mcp_proposal_rejected(client):
    snap=client.get('/api/snapshot').json()
    id=snap['model']['items'][0]['id']
    p={'changes':[{'itemId':id,'fields':{'__proto__':{}}}]}
    assert client.post('/api/outlook/preview',json=p).status_code==400


def test_websocket_changes(client):
    with client.websocket_connect('/ws') as ws:
        snap=client.get('/api/snapshot').json()
        item=snap['model']['items'][0]
        item['notes']='ping'
        client.put('/api/items/'+item['id'],json={'item':item,'expectedVersion':snap['itemRevisions'][item['id']]})
        msg=ws.receive_json()
        assert msg['type']=='changed' and msg['revision']==2


def test_origin_check(client):
    assert client.post('/api/reset',json={'expectedRevision':1},headers={'Origin':'https://evil.example'}).status_code==403


def test_passwordless_access_and_sync(client, monkeypatch):
    monkeypatch.setenv('ATLAS_REQUIRE_LOGIN', '0')
    server.AUTH_PATH.unlink()
    with TestClient(server.app) as guest:
        page=guest.get('/').text
        assert 'window.__ATLAS_SERVER__=' in page
        assert 'data-lan-action="logout"' not in page
        snap=guest.get('/api/snapshot').json()
        assert snap['loginRequired'] is False
        assert len(snap['model']['items'])==2
        assert not guest.cookies
        with guest.websocket_connect('/ws') as ws:
            item=snap['model']['items'][0]
            item['notes']='免登录设备同步测试'
            response=guest.put('/api/items/'+item['id'],json={'item':item,'expectedVersion':snap['itemRevisions'][item['id']]},headers={'Origin':'http://testserver'})
            assert response.status_code==200
            assert ws.receive_json()['type']=='changed'
        assert guest.get('/api/snapshot').json()['model']['items'][0]['notes']=='免登录设备同步测试'
        assert guest.get('/api/backup.sqlite3').status_code==200
        assert guest.get('/api/ai/config').status_code==200


def test_passwordless_origin_checks(client, monkeypatch):
    from starlette.websockets import WebSocketDisconnect
    monkeypatch.setenv('ATLAS_REQUIRE_LOGIN', '0')
    with TestClient(server.app) as guest:
        assert guest.post('/api/reset',json={'expectedRevision':1},headers={'Origin':'https://evil.example'}).status_code==403
        with pytest.raises(WebSocketDisconnect) as exc:
            with guest.websocket_connect('/ws',headers={'Origin':'https://evil.example'}):
                pass
        assert exc.value.code==4403

def test_sqlite_backup(client):
    r=client.get('/api/backup.sqlite3')
    assert r.status_code==200
    assert r.content.startswith(b'SQLite format 3')
    assert len(r.content)>20000


def test_legacy_safe_merge_preserves_evidence(client):
    snap=client.get('/api/snapshot').json()
    original=snap['model']['items'][0]
    legacy=json.loads(json.dumps(snap['model']))
    older=legacy['items'][0]
    older['notes']='从旧浏览器合并来的个人备注'
    older['userUpdatedAt']='2026-10-09T12:00:00Z'
    older['status']='done'  # Must not overwrite current status from Outlook evidence.
    older['mailEvidence']=[] # Must not remove evidence.
    older['milestones'].append({'text':'在旧浏览器里做过的手动笔记','done':False,'certainty':'user','source':'本地手动添加'})
    preview=client.post('/api/legacy/preview',json={'model':legacy})
    assert preview.status_code==200,preview.text
    assert preview.json()['items']
    token=preview.json()['token']
    r=client.post('/api/legacy/commit',json={'token':token})
    assert r.status_code==200,r.text
    latest=client.get('/api/snapshot').json()['model']['items'][0]
    assert latest['status']==original['status']
    assert latest['mailEvidence']==original.get('mailEvidence',[])
    assert latest['notes']=='从旧浏览器合并来的个人备注'
    assert any(m['text']=='在旧浏览器里做过的手动笔记' for m in latest['milestones'])
