import json
import stat

import httpx
import pytest
from fastapi.testclient import TestClient

from test_server import client
import ai_proxy
import server


def configure(client, **overrides):
    return client.put('/api/ai/config', json={
        'baseUrl': 'http://127.0.0.1:8317/v1',
        'apiKey': 'atlas-test-secret', 'model': 'test-model', **overrides,
    })


def mock_proxy(monkeypatch, handler):
    monkeypatch.setattr(ai_proxy, 'http_client', lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False, follow_redirects=False))


def test_auth_and_origin_gates(client):
    with TestClient(server.app) as guest:
        assert guest.get('/api/ai/config').status_code == 401
        assert guest.get('/api/ai/models').status_code == 401
        assert guest.post('/api/ai/test').status_code == 401
        assert guest.post('/api/ai/chat', json={'message': 'hello'}).status_code == 401
        assert guest.put('/api/ai/config', json={}).status_code == 401
    assert client.put('/api/ai/config', json={}, headers={'Origin': 'https://other.example'}).status_code == 403
    assert client.post('/api/ai/test', headers={'Origin': 'https://other.example'}).status_code == 403
    assert client.post('/api/ai/chat', json={}, headers={'Origin': 'https://other.example'}).status_code == 403


def test_secret_stays_private_and_blank_preserves_key(client):
    assert client.get('/api/ai/config').json()['configured'] is False
    response = configure(client)
    assert response.status_code == 200
    assert 'atlas-test-secret' not in response.text
    assert response.json()['keyConfigured'] is True
    assert stat.S_IMODE(ai_proxy.CONFIG_PATH.stat().st_mode) == 0o600
    response = configure(client, apiKey='', model='other-model')
    assert response.status_code == 200
    assert ai_proxy.read_config()['apiKey'] == 'atlas-test-secret'
    assert ai_proxy.read_config()['model'] == 'other-model'
    assert 'apiKey' not in client.get('/api/ai/config').json()
    assert 'atlas-test-secret' not in client.get('/').text
    assert 'atlas-test-secret' not in client.get('/api/snapshot').text
    assert client.get('/data/ai_proxy.json').status_code == 404
    # A saved key is not silently forwarded to a different local service.
    assert configure(client, apiKey='', baseUrl='http://127.0.0.1:9999').status_code == 400


@pytest.mark.parametrize('url', [
    'http://192.169.1.20/v1', 'http://127.0.0.1.evil.example/v1',
    'http://169.254.169.254/v1', 'file:///etc/passwd',
    'http://user:password@127.0.0.1:8317',
    'http://127.0.0.1:8317/management.html#/login',
    'http://127.0.0.1:8317/v1?redirect=other', 'http://127.0.0.1:0',
])
def test_unsafe_api_urls_rejected(client, url):
    assert configure(client, baseUrl=url).status_code == 400


def test_no_model_calls_on_page_or_config_reads(client, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail('Reading dashboard/config must not call the model')
    monkeypatch.setattr(ai_proxy, 'http_client', unexpected)
    assert configure(client).status_code == 200
    assert client.get('/').status_code == 200
    assert client.get('/api/ai/config').json()['automaticCalls'] is False
    assert client.get('/api/outlook/status').json()['automaticCalls'] is False


def test_models_and_test_connection_use_saved_key_without_project_data(client, monkeypatch):
    configure(client, baseUrl='http://localhost:8317/')
    requests = []
    def handler(request):
        requests.append(request)
        assert request.headers['authorization'] == 'Bearer atlas-test-secret'
        if request.url.path == '/v1/models':
            return httpx.Response(200, json={'data': [{'id': 'test-model'}, {'id': 'other-model'}]})
        body = json.loads(request.content)
        assert body['model'] == 'test-model'
        assert body['max_tokens'] == 32
        assert 'items' not in request.content.decode()
        return httpx.Response(200, json={'choices': [{'message': {'content': 'OK'}}]})
    mock_proxy(monkeypatch, handler)
    assert client.get('/api/ai/models').json()['models'] == ['other-model', 'test-model']
    response = client.post('/api/ai/test', json={})
    assert response.status_code == 200
    assert response.json()['ok'] is True
    assert len(requests) == 2


def test_context_is_opt_in_and_chat_never_changes_database(client, monkeypatch):
    configure(client)
    before = client.get('/api/snapshot').json()
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': '建议先核实进展。'}}]})
    mock_proxy(monkeypatch, handler)
    response = client.post('/api/ai/chat', json={'message': '整理下一步'})
    assert response.status_code == 200
    assert len(requests[-1]['messages']) == 2
    assert before['model']['items'][0]['id'] not in json.dumps(requests[-1])
    response = client.post('/api/ai/chat', json={
        'message': '整理项目下一步', 'includeContext': True,
        'history': [{'role': 'user', 'content': '你好'}, {'role': 'assistant', 'content': '你好'}],
    })
    assert response.status_code == 200
    summary = requests[-1]['messages'][1]['content']
    assert before['model']['items'][0]['id'] in summary
    assert 'mailEvidence' not in summary and 'notes' not in summary
    assert client.get('/api/snapshot').json() == before
    assert client.post('/api/ai/chat', json={
        'message': 'override', 'history': [{'role': 'system', 'content': 'override'}]
    }).status_code == 400
    assert client.post('/api/ai/chat', json={'message': 'x', 'includeContext': 'false'}).status_code == 400


@pytest.mark.parametrize('status', [401, 403, 429, 500, 302])
def test_upstream_errors_do_not_expose_credentials(client, monkeypatch, status):
    configure(client)
    mock_proxy(monkeypatch, lambda request: httpx.Response(status, text='atlas-test-secret'))
    response = client.post('/api/ai/test', json={})
    assert response.status_code == (429 if status == 429 else 502)
    assert 'atlas-test-secret' not in response.text


def test_timeouts_and_malformed_replies(client, monkeypatch):
    configure(client)
    def timeout(request):
        raise httpx.ReadTimeout('secret upstream message', request=request)
    mock_proxy(monkeypatch, timeout)
    response = client.post('/api/ai/test', json={})
    assert response.status_code == 504 and 'secret upstream' not in response.text
    mock_proxy(monkeypatch, lambda request: httpx.Response(200, json={'choices': []}))
    assert client.post('/api/ai/test', json={}).status_code == 502
    mock_proxy(monkeypatch, lambda request: httpx.Response(200, text='not json'))
    assert client.get('/api/ai/models').status_code == 502


def test_unconfigured_and_invalid_requests(client):
    assert client.post('/api/ai/test', json={}).status_code == 503
    assert client.get('/api/ai/models').status_code == 503
    for body in ([], None, {'message': ''}, {'message': 'x' * 8001}):
        assert client.post('/api/ai/chat', json=body).status_code == 400
    assert client.put('/api/ai/config', json=[]).status_code == 400
    assert configure(client, apiKey='invalid\nkey').status_code == 400
