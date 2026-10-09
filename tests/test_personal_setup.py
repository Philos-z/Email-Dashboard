import asyncio
import base64
from contextlib import asynccontextmanager
import json
import stat

import pytest

from test_server import client
import ai_proxy
import gmail_mcp
import mail_provider
import mail_sync
import outlook_mcp
import runtime_settings
import server


def account(**changes):
    return {**runtime_settings.defaults(), 'email': 'owner@example.com', 'historySince':'2026-04-07', **changes}


def model_ready():
    ai_proxy.save_config({'baseUrl': 'https://api.example.com/custom/v1', 'apiKey':'synthetic-key', 'model':'test-model'})


def test_setup_guard_and_identity_change_invalidates_verification(client):
    runtime_settings.CONFIG_PATH.unlink()
    assert client.get('/api/mail/config').json()['email'] == ''
    assert client.post('/api/mail/refresh').status_code == 400
    assert client.put('/api/mail/schedule', json={'enabled':True}).status_code == 400
    saved = client.put('/api/mail/config', json=account()).json()
    assert saved['verified'] is False
    assert stat.S_IMODE(runtime_settings.CONFIG_PATH.stat().st_mode) == 0o600
    assert client.post('/api/mail/refresh').status_code == 400
    runtime_settings.write({**runtime_settings.read(), 'verified':True})
    assert client.post('/api/mail/refresh').status_code == 400  # Consent is explicit.
    client.put('/api/mail/config',json=account(shareWithModel=True))
    assert client.post('/api/mail/refresh').status_code == 400  # Model is not configured.
    saved=client.put('/api/mail/config',json=account(email='other@example.com')).json()
    assert saved['verified'] is False


def test_account_scope_is_locked_after_sync(client):
    with server.connect() as con:
        con.execute("INSERT INTO mail_folder_sync(folder_id,path,direction,cursor) VALUES('inbox','Inbox','received','2026-10-09T00:00:00Z')")
    current=client.get('/api/mail/config').json()
    assert current['locked'] is True
    for key, value in [('email','other@example.com'),('provider','gmail'),('timeZone','Asia/Shanghai'),('historySince','2026-03-01'),('mcpServer','other')]:
        assert client.put('/api/mail/config',json={**current,key:value}).status_code == 409
    assert client.put('/api/mail/config',json={**current,'shareWithModel':False}).status_code == 200
    assert client.get('/api/mail/status').json()['enabled'] is False


@pytest.mark.parametrize('changes', [{'provider':'imap'}, {'email':'not-an-email'}, {'timeZone':'invalid'}, {'historySince':'3000-01-01'}, {'timeZone':[]}, {'shareWithModel':'yes'}, {'mcpServer':'../../shell'}])
def test_invalid_account_settings(client,changes):
    assert client.put('/api/mail/config',json=account(**changes)).status_code == 400


def test_mail_setup_auth_and_origin(client):
    assert client.put('/api/mail/config',json=account(),headers={'Origin':'https://other.example'}).status_code == 403
    client.cookies.clear()
    for method,path in [('get','config'),('put','config'),('post','verify'),('post','refresh')]:
        assert getattr(client,method)('/api/mail/'+path).status_code == 401


def test_wrong_outlook_identity_never_lists_mail(monkeypatch):
    class Transport:
        own_address='other@example.com'
        async def profile(self):return self.own_address
        async def list_watched_folders(self):pytest.fail('Wrong identity must stop before folders or mail')
    @asynccontextmanager
    async def connect():yield Transport()
    monkeypatch.setattr(outlook_mcp,'connect',connect)
    with pytest.raises(outlook_mcp.OutlookError,match='已连接邮箱与设置不一致'):
        asyncio.run(mail_provider.verify(account()))


def test_verify_reads_profile_only_and_detects_changed_settings(client,monkeypatch):
    calls=[]
    class Transport:
        thread_id='test'
        async def rpc(self,method,params):
            calls.append(params['tool'])
            assert params['tool']=='gmail.get_profile'
            return {'structuredContent':{'emailAddress':'owner@example.com'}}
    @asynccontextmanager
    async def connect():yield Transport()
    monkeypatch.setattr(outlook_mcp,'connect',connect)
    client.put('/api/mail/config',json=account(provider='gmail'))
    result=client.post('/api/mail/verify')
    assert result.status_code==200,result.text
    assert calls==['gmail.get_profile']
    assert client.get('/api/mail/config').json()['verified'] is True
    assert client.get('/api/mail/status').json()['runs']==[]


def message(identity,when,text='A written update'):
    return {'id':identity,'internalDate':str(int(mail_sync.parse_date(when).timestamp()*1000)),
            'payload':{'mimeType':'multipart/mixed','parts':[
                {'mimeType':'text/plain','body':{'data':base64.urlsafe_b64encode(text.encode()).decode().rstrip('=')}},
                {'mimeType':'text/plain','filename':'secret.txt','body':{'data':base64.urlsafe_b64encode(b'attachment must not be read').decode()}}],
                'headers':[{'name':'Subject','value':'Example confirmation'},{'name':'From','value':'Sender <sender@example.com>'}]}}


def test_gmail_mime_body_and_internal_date():
    normalized=gmail_mcp.normalize(message('g1','2026-10-09T10:00:00Z'))
    assert normalized['receivedDateTime']=='2026-10-09T10:00:00Z'
    assert normalized['body']['content']=='A written update'
    assert normalized['from']['emailAddress']['address']=='sender@example.com'
    assert gmail_mcp.body_text({'mimeType':'application/pdf','body':{'attachmentId':'private'}})==''


def test_gmail_fixed_window_numeric_token_survives_restart(client,monkeypatch):
    runtime_settings.write({**account(provider='gmail',shareWithModel=True),'verified':True})
    queries=[]
    messages={'newer':message('newer','2026-10-09T10:00:00Z'), 'older':message('older','2026-10-09T09:00:00Z')}
    class Transport:
        thread_id='test'
        async def rpc(self,method,params):
            tool,args=params['tool'],params['arguments']
            if tool=='gmail.get_profile':return {'structuredContent':{'emailAddress':'owner@example.com'}}
            if tool=='gmail.search_email_ids':
                queries.append(args.copy())
                token=args['next_page_token']
                return {'structuredContent':{'ids':['older' if token else 'newer'], 'next_page_token':'' if token else '123'}}
            assert tool=='gmail.batch_read_email'
            return {'structuredContent':{'messages':[messages[i] for i in args['message_ids']]}}
    @asynccontextmanager
    async def connect():yield Transport()
    monkeypatch.setattr(outlook_mcp,'connect',connect)
    async def folders(self):return [{'id':'inbox','path':'Inbox','kind':'inbox','direction':'received'}]
    monkeypatch.setattr(gmail_mcp.GmailMCP,'list_watched_folders',folders)
    monkeypatch.setattr(mail_sync,'MAX_MESSAGES',1)
    service=server.app.state.mail_sync
    snapshot=server.read_snapshot()
    mails,checkpoint,partial=client.portal.call(service.fetch_mail,snapshot,'2026-10-09T11:00:00Z')
    assert partial and [m['id'] for m in mails]==['newer']
    empty={'changes':[],'newItems':[],'todayUpdatesAdd':[]}
    service.commit('synthetic',empty,mails,checkpoint,snapshot['revision'],partial)
    fresh=mail_sync.MailSyncService(server)
    mails,next_checkpoint,partial=client.portal.call(fresh.fetch_mail,server.read_snapshot(),'2026-10-09T12:00:00Z')
    assert not partial and [m['id'] for m in mails]==['older']
    assert queries[1]['query']==queries[0]['query']
    assert queries[1]['next_page_token']=='123'
    assert next_checkpoint['checkedThrough']=='2026-10-09T11:00:00Z'


def test_gmail_incomplete_batch_or_repeated_token_fails_closed():
    class Transport:
        thread_id='test'
        async def rpc(self,method,params):
            if params['tool']=='gmail.search_email_ids':
                return {'structuredContent':{'ids':['m1'],'next_page_token':'same'}}
            return {'structuredContent':{'messages':[]}}
    adapter=gmail_mcp.GmailMCP(Transport(),'codex_apps')
    with pytest.raises(outlook_mcp.OutlookError,match='不完整'):
        asyncio.run(adapter.list_messages('2026-10-09T00:00:00Z','2026-10-09T11:00:00Z',folder_id='inbox'))
    with pytest.raises(outlook_mcp.OutlookError,match='分页'):
        asyncio.run(adapter.list_messages('2026-10-09T00:00:00Z','2026-10-09T11:00:00Z',skip='same',folder_id='inbox'))


def test_custom_api_base_path_and_secret_rotation(client):
    model_ready()
    assert ai_proxy.read_config()['baseUrl']=='https://api.example.com/custom/v1'
    assert 'synthetic-key' not in client.get('/api/ai/config').text
    assert client.put('/api/ai/config',json={'baseUrl':'https://other.example/v1','apiKey':'','model':'test'}).status_code==400
    assert ai_proxy.normalize_base_url('https://api.openai.com/v1/')=='https://api.openai.com/v1'
    for invalid in ['http://api.example.com/v1','https://api.example.com/v1?key=secret','https://user:secret@api.example.com/v1','https://[fd00::1]/v1']:
        with pytest.raises(ValueError):ai_proxy.normalize_base_url(invalid)


def test_selected_timezone_changes_mail_display_and_quiet_hours(client):
    runtime_settings.write({**account(timeZone='Asia/Shanghai'),'verified':True})
    mail=mail_sync.normalize_message({'id':'m','receivedDateTime':'2026-10-09T18:00:00Z','body':{}},'received')
    assert mail['date']=='2026-10-10' and mail['time']=='02:00'
    assert mail_sync.quiet_hours(mail_sync.parse_date('2026-10-09T18:00:00Z'))


def test_initial_inbox_backfill_uses_chosen_date_not_snapshot_day(client,monkeypatch):
    from test_mail_sync import mock_mailbox, raw_mail
    mock_mailbox(monkeypatch,[raw_mail('historical','2026-09-12T10:00:00Z')])
    mails,_,_=client.portal.call(server.app.state.mail_sync.fetch_mail,server.read_snapshot(),'2026-10-09T11:00:00Z')
    assert [m['id'] for m in mails]==['historical']


def test_reset_rewinds_checkpoints_and_pauses_schedule(client):
    with server.connect() as con:
        con.execute("INSERT INTO mail_folder_sync(folder_id,path,direction,cursor) VALUES('inbox','Inbox','received','2026-10-09T00:00:00Z')")
        con.execute("INSERT INTO mail_seen(id) VALUES('test')")
        con.execute('UPDATE mail_sync_config SET enabled=1')
    before=client.get('/api/snapshot').json()
    assert client.post('/api/reset',json={'expectedRevision':before['revision']}).status_code==200
    with server.connect() as con:
        assert con.execute('SELECT COUNT(*) FROM mail_seen').fetchone()[0]==0
        assert con.execute('SELECT COUNT(*) FROM mail_folder_sync').fetchone()[0]==0
    assert client.get('/api/mail/status').json()['enabled'] is False
