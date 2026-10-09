import httpx
import pytest

import ai_proxy
from test_ai_proxy import configure, mock_proxy
from test_server import client


@pytest.mark.parametrize('url', [
    'http://192.168.1.20:8317/v1', 'https://192.168.1.20:8317/v1',
    'http://10.0.0.1:8317/v1', 'http://10.255.255.254:8317/v1',
    'http://172.16.0.1:8317/v1', 'http://172.31.255.254:8317/v1',
    'http://192.168.255.254:8317/v1',
])
def test_private_lan_api_urls_can_be_saved(client, url):
    response = configure(client, baseUrl=url)
    assert response.status_code == 200, response.text
    assert response.json()['baseUrl'] == url
    assert 'atlas-test-secret' not in response.text


@pytest.mark.parametrize('url', [
    'http://172.15.255.254/v1', 'http://172.32.0.1/v1', 'http://192.169.0.1/v1',
    'http://169.254.169.254/v1', 'https://169.254.169.254/v1',
    'http://100.100.100.200/v1', 'http://0.0.0.0/v1', 'http://8.8.8.8/v1',
    'http://192.168.1.20.evil.example/v1', 'http://[fd00::1]/v1',
    'http://user:secret@192.168.1.20/v1', 'http://192.168.1.20/v1?key=secret',
    'http://192.168.1.20/management.html#/login', 'http://192.168.1.20:0/v1',
])
def test_lan_support_does_not_allow_unsafe_destinations(client, url):
    assert configure(client, baseUrl=url).status_code == 400


def test_lan_host_change_requires_reentering_key(client):
    configure(client)
    response = configure(client, baseUrl='http://192.168.1.20:8317/v1', apiKey='')
    assert response.status_code == 400
    assert ai_proxy.read_config()['baseUrl'] == 'http://127.0.0.1:8317/v1'


def test_backend_calls_saved_lan_endpoint_with_private_key(client, monkeypatch):
    configure(client, baseUrl='http://192.168.1.20:8317/v1')
    calls = []

    def handler(request):
        calls.append(request)
        assert str(request.url) == 'http://192.168.1.20:8317/v1/models'
        assert request.headers['authorization'] == 'Bearer atlas-test-secret'
        return httpx.Response(200, json={'data': [{'id': 'test-model'}]})

    mock_proxy(monkeypatch, handler)
    response = client.get('/api/ai/models')
    assert response.status_code == 200
    assert response.json()['models'] == ['test-model']
    assert len(calls) == 1
    assert 'atlas-test-secret' not in response.text
