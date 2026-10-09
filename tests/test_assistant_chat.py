import asyncio
from contextlib import asynccontextmanager
import json

import httpx
import pytest

import assistant_chat
import outlook_mcp
import runtime_settings
from test_ai_proxy import configure, mock_proxy
from test_server import client


def mail(ident='mail-1', **extra):
    return {'id': ident, 'subject': '左前 Vermessung',
            'sender': {'emailAddress': {'address': 'workshop@example.com'}},
            'receivedDateTime': '2026-10-09T12:00:00Z',
            'body': {'contentType': 'html', 'content': '<p>请先预约测量。</p><script>secret()</script>'},
            'web_link': 'https://outlook.live.com/owa/?ItemID=' + ident, **extra}


def tool_call(name='search_outlook_mail', arguments=None, ident='call-1'):
    return {'id': ident, 'type': 'function', 'function': {
        'name': name, 'arguments': json.dumps(arguments or {'query': 'Vermessung', 'size': 2})}}


def model_reply(content=None, calls=None):
    return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': content,
                                                             **({'tool_calls': calls} if calls else {})}}]})


class Mailbox:
    own_address = 'me@example.com'

    def __init__(self):
        self.calls = []

    async def profile(self):
        self.calls.append(('profile',))
        return self.own_address

    async def search_mail(self, query, size, offset):
        self.calls.append(('search', query, size, offset))
        return {'results': [mail()], 'has_more': False}

    async def list_watched_folders(self):
        self.calls.append(('folders',))
        return [{'id': 'sent-id', 'kind': 'sentitems', 'path': 'Sent Items', 'direction': 'sent'},
                {'id': 'child-id', 'kind': 'sentitems', 'path': 'Sent Items/Work', 'direction': 'sent'}]

    async def recent_mail(self, folder_id, **kwargs):
        self.calls.append(('list', folder_id, kwargs))
        return {'value': [mail(sender={'emailAddress': {'address': self.own_address}},
                               sentDateTime='2026-10-09T10:00:00Z')], 'has_more': False}

    async def read_mail(self, ident):
        self.calls.append(('read', ident))
        return mail(ident, body={'contentType': 'text', 'content': '完整正文。' * 2000})


def fake_mailbox(monkeypatch, mailbox=None):
    mailbox = mailbox or Mailbox()
    opened = []

    @asynccontextmanager
    async def connect():
        opened.append('open')
        try:
            yield mailbox
        finally:
            opened.append('close')

    monkeypatch.setattr(outlook_mcp, 'connect', connect)
    return mailbox, opened


def test_chat_uses_real_tool_results_and_returns_sources_without_mutating_projects(client, monkeypatch):
    configure(client)
    before = client.get('/api/snapshot').json()
    mailbox, opened = fake_mailbox(monkeypatch)
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            assert len(payload['tools']) == 3
            assert '只读 Outlook' in payload['messages'][0]['content']
            return model_reply(calls=[tool_call()])
        result = json.loads(payload['messages'][-1]['content'])
        assert payload['messages'][-1]['role'] == 'tool'
        assert payload['messages'][-1]['tool_call_id'] == 'call-1'
        assert result['mails'][0]['text'] == '请先预约测量。'
        assert result['mails'][0]['reference'] == 'M1'
        assert 'secret()' not in json.dumps(result)
        return model_reply('邮件要求先预约测量。[M1]')

    mock_proxy(monkeypatch, handler)
    response = client.post('/api/ai/chat', json={'message': '查询测量相关邮件', 'includeContext': True})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['mailAccess']['queried'] is True
    assert data['mailAccess']['count'] == 1
    assert data['sources'][0]['url'].startswith('https://outlook.live.com/')
    assert data['sources'][0]['date'] == '2026-10-09T14:00+02:00'
    assert 'text' not in data['sources'][0] and 'id' not in data['sources'][0]
    assert mailbox.calls == [('profile',), ('search', 'Vermessung', 2, 0)]
    assert opened == ['open', 'close']
    assert client.get('/api/snapshot').json() == before


def test_general_chat_and_disabled_mail_do_not_open_mcp(client, monkeypatch):
    configure(client)
    mailbox, opened = fake_mailbox(monkeypatch)
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return model_reply('你好。')

    mock_proxy(monkeypatch, handler)
    response = client.post('/api/ai/chat', json={'message': '你好'})
    assert response.json()['mailAccess'] == {'enabled': True, 'queried': False, 'queries': [], 'count': 0}
    response = client.post('/api/ai/chat', json={'message': '只看项目', 'includeMail': False})
    assert response.status_code == 200
    assert 'tools' not in requests[-1]
    assert '关闭了 Outlook 查询' in requests[-1]['messages'][0]['content']
    assert not mailbox.calls and not opened
    assert client.post('/api/ai/chat', json={'message': 'x', 'includeMail': 'true'}).status_code == 400


def test_sent_listing_uses_actual_folder_and_sent_time(client, monkeypatch):
    configure(client)
    mailbox, _ = fake_mailbox(monkeypatch)
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return model_reply(calls=[tool_call('list_outlook_mail', {'folder': 'sentitems', 'size': 3})])
        result = json.loads(payload['messages'][-1]['content'])
        assert result['mails'][0]['direction'] == 'sent'
        assert result['mails'][0]['date'] == '2026-10-09T12:00+02:00'
        return model_reply('已发送测量询问。[M1]')

    mock_proxy(monkeypatch, handler)
    response = client.post('/api/ai/chat', json={'message': '最近发了哪些邮件'})
    assert response.status_code == 200, response.text
    assert mailbox.calls[-1] == ('list', 'sent-id', {'top': 3, 'skip': 0, 'direction': 'sent', 'unread': False})


def test_pagination_uses_returned_cursor_deduplicates_and_keeps_one_connection(client, monkeypatch):
    configure(client)

    class Paged(Mailbox):
        async def search_mail(self, query, size, offset):
            self.calls.append(('search', query, size, offset))
            return {'results': [mail() if offset == 0 else mail('mail-2')],
                    'has_more': offset == 0, 'next_from_index': 7 if offset == 0 else None}

    mailbox, opened = fake_mailbox(monkeypatch, Paged())
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return model_reply(calls=[tool_call()])
        if len(requests) == 2:
            assert json.loads(payload['messages'][-1]['content'])['next_from_index'] == 7
            return model_reply(calls=[tool_call(arguments={'query': 'Vermessung', 'size': 2, 'from_index': 7}, ident='call-2')])
        return model_reply('找到两封。[M1][M2]')

    mock_proxy(monkeypatch, handler)
    data = client.post('/api/ai/chat', json={'message': '查这件事的往来'}).json()
    assert data['mailAccess']['count'] == 2
    assert [s['reference'] for s in data['sources']] == ['M1', 'M2']
    assert mailbox.calls[-1] == ('search', 'Vermessung', 2, 7)
    assert opened == ['open', 'close']


def test_full_body_only_reads_a_discovered_source(client, monkeypatch):
    configure(client)
    mailbox, _ = fake_mailbox(monkeypatch)
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return model_reply(calls=[tool_call()])
        if len(requests) == 2:
            return model_reply(calls=[tool_call('read_outlook_mail', {'reference': 'M1'}, ident='call-2')])
        result = json.loads(payload['messages'][-1]['content'])
        assert result['mail']['text'].startswith('完整正文。')
        assert result['mail']['attachmentsRead'] is False
        return model_reply('根据邮件全文回复。[M1]')

    mock_proxy(monkeypatch, handler)
    response = client.post('/api/ai/chat', json={'message': '读这封邮件全文'})
    assert response.status_code == 200
    assert mailbox.calls[-1] == ('read', 'mail-1')
    assert len(response.json()['sources']) == 1


@pytest.mark.parametrize('call', [
    tool_call('send_email', {'to': ['x@example.com']}),
    tool_call(arguments={'query': 'x', 'size': True}),
    tool_call(arguments={'query': 'x', 'size': 500}),
    tool_call(arguments={'query': 'x', 'unexpected': 'x'}),
    tool_call('read_outlook_mail', {'reference': 'raw-message-id'}),
])
def test_invalid_or_write_tool_calls_are_rejected_before_mcp_opens(client, monkeypatch, call):
    configure(client)
    mailbox, opened = fake_mailbox(monkeypatch)
    mock_proxy(monkeypatch, lambda request: model_reply(calls=[call]))
    assert client.post('/api/ai/chat', json={'message': '查询邮件'}).status_code == 502
    assert not mailbox.calls and not opened


def test_unknown_cursor_and_source_do_not_open_mcp(client, monkeypatch):
    async def run():
        async with assistant_chat.AsyncExitStack() as stack:
            mailbox = assistant_chat.MailTools(stack)
            assert 'error' in await mailbox.call('read_outlook_mail', {'reference': 'M999'})
            assert 'error' in await mailbox.call('search_outlook_mail', {'query': 'x', 'from_index': 8})

    _, opened = fake_mailbox(monkeypatch)
    asyncio.run(run())
    assert not opened


def test_mcp_failure_is_visible_and_does_not_claim_success(client, monkeypatch):
    configure(client)

    class Broken(Mailbox):
        async def search_mail(self, *args):
            raise outlook_mcp.OutlookError('Outlook 连接已失效，请重新连接')

    _, opened = fake_mailbox(monkeypatch, Broken())
    mock_proxy(monkeypatch, lambda request: model_reply(calls=[tool_call()]))
    response = client.post('/api/ai/chat', json={'message': '查询邮件'})
    assert response.status_code == 502
    assert 'Outlook 连接已失效' in response.json()['detail']
    assert 'reply' not in response.json()
    assert opened == ['open', 'close']


def test_tool_loop_is_bounded_and_cleans_up(client, monkeypatch):
    configure(client)
    _, opened = fake_mailbox(monkeypatch)
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        return model_reply(calls=[tool_call(ident='call-' + str(len(requests)))])

    mock_proxy(monkeypatch, handler)
    response = client.post('/api/ai/chat', json={'message': '查询邮件'})
    assert response.status_code == 502
    assert len(requests) == 5 and requests[-1]['tool_choice'] == 'none'
    assert opened == ['open', 'close']


def test_read_only_mcp_wrappers_use_exact_connector_tools():
    async def run():
        mcp = outlook_mcp.OutlookMCP(None)
        mcp.thread_id = 'test'
        mcp.server_name = 'personal_mcp'
        calls = []

        async def rpc(method, params):
            calls.append((method, params))
            return {'structuredContent': {'results': []}}

        mcp.rpc = rpc
        await mcp.search_mail('test', 2, 0)
        await mcp.recent_mail('sent-id', top=2, direction='sent', unread=True)
        await mcp.read_mail('mail-1')
        assert [c[1]['tool'] for c in calls] == [
            'microsoft_outlook_email.search_messages', 'microsoft_outlook_email.list_messages',
            'microsoft_outlook_email.fetch_message']
        assert calls[1][1]['arguments']['order_by'] == 'sentDateTime desc'
        assert calls[1][1]['arguments']['filter'] == 'isRead eq false'
        assert all(c[0] == 'mcpServer/tool/call' for c in calls)
        assert all(c[1]['server'] == 'personal_mcp' for c in calls)

    asyncio.run(run())


@pytest.mark.parametrize('url', ['javascript:alert(1)', 'https://evil.example/',
                                 'https://outlook.live.com.evil.example/',
                                 'https://user:secret@outlook.live.com/'])
def test_mail_links_only_accept_actual_outlook_https_urls(url):
    assert assistant_chat.safe_mail_url(url) == ''


@pytest.mark.parametrize('changes', [{'shareWithModel': False}, {'verified': False},
                                    {'provider': 'gmail'}, {'email': ''}])
def test_installation_preferences_gate_chat_mail_access(client, monkeypatch, changes):
    configure(client)
    saved = runtime_settings.read()
    if changes.get('email') == '':
        runtime_settings.CONFIG_PATH.unlink()
    else:
        runtime_settings.write({**saved, **changes})
    _, opened = fake_mailbox(monkeypatch)
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return model_reply('可以分析项目摘要。')

    mock_proxy(monkeypatch, handler)
    assert client.get('/api/ai/config').json()['mailChat']['enabled'] is False
    assert client.post('/api/ai/chat', json={'message': '查邮件', 'includeMail': True}).status_code == 400
    response = client.post('/api/ai/chat', json={'message': '你好'})
    assert response.status_code == 200
    assert response.json()['mailAccess']['enabled'] is False
    assert 'tools' not in requests[-1]
    assert not opened


def test_chat_checks_configured_identity_before_any_mail_read(client, monkeypatch):
    configure(client)
    mailbox = Mailbox()
    mailbox.own_address = 'different@example.com'
    _, opened = fake_mailbox(monkeypatch, mailbox)
    mock_proxy(monkeypatch, lambda request: model_reply(calls=[tool_call()]))
    response = client.post('/api/ai/chat', json={'message': '查邮件'})
    assert response.status_code == 502
    assert '已连接邮箱与设置不一致' in response.json()['detail']
    assert mailbox.calls == [('profile',)]
    assert opened == ['open', 'close']


def test_changed_consent_stops_before_mail_is_sent_to_model(client, monkeypatch):
    configure(client)

    class Changed(Mailbox):
        async def search_mail(self, *args):
            runtime_settings.write({**runtime_settings.read(), 'shareWithModel': False})
            return {'results': [mail()], 'has_more': False}

    fake_mailbox(monkeypatch, Changed())
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return model_reply(calls=[tool_call()])

    mock_proxy(monkeypatch, handler)
    response = client.post('/api/ai/chat', json={'message': '查邮件'})
    assert response.status_code == 409
    assert len(requests) == 1


def test_source_times_use_installation_timezone(client):
    runtime_settings.write({**runtime_settings.read(), 'timeZone': 'Asia/Shanghai'})
    record = assistant_chat.mail_record(mail(), 'M1')
    assert record['date'] == '2026-10-09T20:00+08:00'
