"""Credential-broker opt-in, destination confinement, and legacy behavior."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.slack.broker_transport import BrokerTransport, active_transport
from plugins.platforms.slack.adapter import (
    _apply_yaml_config, _config_connected, _slack_per_request_proxy_middleware,
    _standalone_send,
)


@pytest.fixture
def config(tmp_path):
    secret = tmp_path / "bearer"
    secret.write_text("broker-agent")
    return PlatformConfig(enabled=True, extra={"broker": {
        "api_base_url": "http://127.0.0.1:9300/api/slack/",
        "socket_base_url": "http://127.0.0.1:9300/api/socket/",
        "files_base_url": "http://127.0.0.1:9300/api/files/",
        "agent_token_file": str(secret),
    }})


def test_opt_in_and_yaml_config(config):
    assert BrokerTransport.from_config(PlatformConfig()) is None
    assert _config_connected(config)
    assert _apply_yaml_config({}, {"broker": config.extra["broker"]})["broker"] == config.extra["broker"]
    assert "broker-agent" not in repr(BrokerTransport.from_config(config))


@pytest.mark.parametrize("url", [
    "https://files.slack.com.evil.test/a", "http://files.slack.com/a",
    "https://files.slack.com@evil.test/a", "https://evil.test/a",
    "https://files.slack.com:444/a", "https://files-origin.slack.com/a",
])
def test_files_destination_is_confined(config, url):
    with pytest.raises(ValueError, match="files route"):
        BrokerTransport.from_config(config).file_url(url)


def test_files_preserve_path_and_query(config):
    broker = BrokerTransport.from_config(config)
    assert broker.file_url("https://files.slack.com/files-pri/T-F/a?x=1") == (
        "http://127.0.0.1:9300/api/files/files-pri/T-F/a?x=1")


@pytest.mark.parametrize("value", ["", "xoxb-secret", "xapp-secret", "two words"])
def test_reject_slack_tokens_as_agent_bearer(config, value):
    from pathlib import Path
    Path(config.extra["broker"]["agent_token_file"]).write_text(value)
    with pytest.raises(ValueError, match="agent bearer"):
        BrokerTransport.from_config(config)


@pytest.mark.parametrize("value", ["", "ftp://host/a", "http://user:pass@host/a", "https://host/a?q=1"])
def test_invalid_broker_config_fails_closed(config, value):
    config.extra["broker"]["socket_base_url"] = value
    with pytest.raises(ValueError, match="socket_base_url"):
        BrokerTransport.from_config(config)


@pytest.mark.asyncio
async def test_bolt_request_clients_keep_broker_routes(config):
    broker = BrokerTransport.from_config(config)
    client = SimpleNamespace(token="wrong", base_url="https://slack.com/api/", _upload_file=AsyncMock(), proxy=None)
    next_call = AsyncMock()
    await _slack_per_request_proxy_middleware(None, broker)(client, next_call)
    assert client.token == "broker-agent"
    assert client.base_url == broker.api_base_url
    next_call.assert_awaited_once()


@pytest.mark.asyncio
async def test_standalone_resets_context_after_error(config, monkeypatch):
    from plugins.platforms.slack import adapter
    failing = AsyncMock(side_effect=RuntimeError("failure"))
    monkeypatch.setattr(adapter, "_standalone_send_direct", failing)
    with pytest.raises(RuntimeError, match="failure"):
        await _standalone_send(config, "C1", "text")
    assert active_transport.get() is None


@pytest.mark.parametrize("path", ["/../slack/auth.test", "/%2e%2e/slack/auth.test", "/%252e%252e/a", "/a\\b"])
def test_file_path_cannot_escape_fixed_route(config, path):
    with pytest.raises(ValueError, match="traverse"):
        BrokerTransport.from_config(config).file_url("https://files.slack.com" + path)


@pytest.mark.asyncio
async def test_socket_handler_keeps_app_client_on_web_api(config, monkeypatch):
    from plugins.platforms.slack import adapter
    from slack_bolt.async_app import AsyncApp
    slack = adapter.SlackAdapter(config)
    slack._app = AsyncApp(client=slack._new_web_client('broker-agent', None))
    slack._app_token = 'broker-agent'
    monkeypatch.setattr(adapter.AsyncSocketModeHandler, 'start_async', AsyncMock())
    slack._start_socket_mode_handler()
    try:
        assert slack._app.client.base_url.endswith('/api/slack/')
        assert slack._handler.client.web_client is not slack._app.client
        assert slack._handler.client.web_client.base_url.endswith('/api/socket/')
    finally:
        await slack._stop_socket_mode_handler()


@pytest.mark.parametrize('invalid', ['missing_file', 'bad_url', 'bad_type'])
@pytest.mark.asyncio
async def test_bad_config_reports_disconnected(config, invalid, caplog):
    from gateway.config import GatewayConfig, Platform
    from plugins.platforms.slack.adapter import SlackAdapter
    if invalid == 'missing_file':
        config.extra['broker']['agent_token_file'] += '.absent'
    elif invalid == 'bad_url':
        config.extra['broker']['api_base_url'] = 'secret-invalid-url'
    else:
        config.extra['broker']['api_base_url'] = 7
    assert not GatewayConfig(platforms={Platform.SLACK: config}).get_connected_platforms()
    slack = SlackAdapter(config)
    assert not await slack.connect()
    assert slack._fatal_error_retryable is False
    assert not (await _standalone_send(config, 'C1', 'text')).get('success')
    assert 'broker' in caplog.text.lower()
    assert 'secret-invalid-url' not in caplog.text


def test_broker_credential_survives_gateway_reconnect_gate(config):
    from gateway.config import Platform
    from gateway.run_adapter_credentials import _platform_has_bot_credential
    assert _platform_has_bot_credential(Platform.SLACK, config)
    assert not _platform_has_bot_credential(Platform.SLACK, PlatformConfig())


@pytest.mark.asyncio
async def test_proxy_bypass_uses_broker_host(config, monkeypatch):
    from plugins.platforms.slack import adapter
    monkeypatch.setenv('HTTPS_PROXY', 'http://proxy.test:3128')
    monkeypatch.setenv('HTTP_PROXY', 'http://proxy.test:3128')
    monkeypatch.setenv('ALL_PROXY', 'http://proxy.test:3128')
    monkeypatch.setenv('NO_PROXY', '127.0.0.1')
    broker = BrokerTransport.from_config(config)
    client = adapter.SlackAdapter(config)._new_web_client('broker-agent', None)
    assert client.proxy is None
    with_broker = active_transport.set(broker)
    try:
        assert adapter._standalone_proxy_kwargs() == ({}, {})
    finally:
        active_transport.reset(with_broker)


@pytest.mark.parametrize('target', ['user:U12345678', 'user_name:@smoke', 'U12345678'])
@pytest.mark.asyncio
async def test_tool_user_resolution_uses_broker(config, monkeypatch, target):
    import asyncio
    import aiohttp
    from tools.send_message_tool import _slack_dm_chat_id
    calls = []

    class Response:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def json(self):
            return {'ok': True, 'members': [{'id': 'U12345678', 'name': 'smoke'}], 'channel': {'id': 'DSMOKE'}}

    class Session(Response):
        def __init__(self, **_):
            pass

        def post(self, url, *, headers, json, **_):
            calls.append((url, headers, json))
            return Response()

    monkeypatch.setattr(aiohttp, 'ClientSession', Session)
    dm, error = await asyncio.to_thread(_slack_dm_chat_id, config, target)
    assert dm == 'DSMOKE' and error is None
    assert calls[-1][0] == config.extra['broker']['api_base_url'] + 'conversations.open'
    assert calls[-1][2] == {'users': 'U12345678'}
    assert all(headers['Authorization'] == 'Bearer broker-agent' for _, headers, _ in calls)
    if target.startswith('user_name:'):
        assert calls[0][0].endswith('/api/slack/users.list')


@pytest.mark.parametrize('no_proxy,expected', [('127.0.0.1', None), ('slack.com', 'http://proxy.test:3128')])
@pytest.mark.asyncio
async def test_broker_proxy_ignores_slack_host_bypass(config, monkeypatch, no_proxy, expected):
    from gateway.platforms import base
    from plugins.platforms.slack import adapter
    monkeypatch.setattr(base, 'resolve_proxy_url', lambda: 'http://proxy.test:3128')
    monkeypatch.setenv('NO_PROXY', no_proxy)
    broker = BrokerTransport.from_config(config)
    client = adapter.SlackAdapter(config)._new_web_client('broker-agent', None)
    assert client.proxy == expected
    request_client = SimpleNamespace()
    await adapter._slack_per_request_proxy_middleware(None, broker)(request_client, AsyncMock())
    assert request_client.proxy == expected
    broker.configure_client(client, socket=True)
    assert client.proxy == expected
