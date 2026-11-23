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
