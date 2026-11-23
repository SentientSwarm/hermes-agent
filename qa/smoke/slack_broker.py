#!/usr/bin/env python3
"""Run real Slack SDK/Bolt against a local recording credential-broker stub.

Usage: .venv/bin/python qa/smoke/slack_broker.py
No Slack credentials or workspace writes. Includes Socket Mode WebSocket setup,
API calls, private downloads, uploads, standalone/cron DM delivery, and reconnect.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from aiohttp import web
from gateway.config import Platform, PlatformConfig
from gateway.run import GatewayRunner
from tools.send_message_tool import _slack_dm_chat_id
from plugins.platforms.slack.adapter import SlackAdapter, _standalone_send


async def main():
    calls = []
    broker_up = True
    socket_open = asyncio.Event()
    bearer = "broker-smoke-agent"
    base = ""

    async def upstream(request):
        if request.path == "/socket":
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_json({"type": "hello"})
            socket_open.set()
            async for _ in ws:
                pass
            return ws
        assert request.headers.get("Authorization") == f"Bearer {bearer}", request.path
        calls.append(request.path)
        if request.path == "/api/socket/apps.connections.open":
            return web.json_response({"ok": True, "url": base.replace("http:", "ws:") + "/socket"})
        if request.path.startswith("/api/socket/"):
            return web.json_response({"ok": False, "error": "socket_route_method_refused"}, status=403)
        if request.path.startswith("/api/files/"):
            if request.method == "POST":
                assert await request.read() == b"upload-bytes"
                return web.Response(text="OK")
            return web.Response(body=b"download-bytes", content_type="application/octet-stream")
        method = request.path.rsplit("/", 1)[-1]
        if not broker_up:
            return web.json_response({"ok": False, "error": "broker_unavailable"}, status=503)
        responses = {
            "auth.test": {"team_id": "TSMOKE", "team": "Smoke", "user_id": "USMOKE", "bot_id": "BSMOKE"},
            "chat.postMessage": {"ts": "1.2", "channel": "CSMOKE"},
            "users.info": {"user": {"id": "USMOKE", "name": "smoke"}},
            "users.list": {"members": [{"id": "USMOKE", "name": "smoke"}]},
            "conversations.info": {"channel": {"id": "CSMOKE", "is_im": False}},
            "conversations.open": {"channel": {"id": "DSMOKE"}},
            "files.getUploadURLExternal": {"file_id": "FSMOKE", "upload_url": "https://files.slack.com/upload/smoke"},
            "files.completeUploadExternal": {"files": [{"id": "FSMOKE"}]},
        }
        assert method in responses, method
        return web.json_response({"ok": True, **responses[method]})

    with tempfile.TemporaryDirectory() as scratch:
        os.environ["HERMES_HOME"] = scratch
        os.environ.pop("SLACK_BOT_TOKEN", None)
        os.environ.pop("SLACK_APP_TOKEN", None)
        # A stale OAuth token file must never be loaded in broker mode.
        await asyncio.to_thread(Path(scratch, "slack_tokens.json").write_text,
                                json.dumps({"bad": {"token": "xoxb-never-use"}}), encoding="utf-8")
        secret = Path(scratch, "agent-token")
        await asyncio.to_thread(secret.write_text, bearer, encoding="utf-8")
        await asyncio.to_thread(secret.chmod, 0o600)
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", upstream)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        config = PlatformConfig(enabled=True, extra={"broker": {
            "api_base_url": base + "/api/slack/",
            "socket_base_url": base + "/api/socket/",
            "files_base_url": base + "/api/files/",
            "agent_token_file": str(secret),
        }})
        # Ambient proxies must be bypassed for this broker, including cron delivery.
        os.environ["HTTPS_PROXY"] = os.environ["HTTP_PROXY"] = "http://127.0.0.1:1"
        os.environ["NO_PROXY"] = "127.0.0.1"
        adapter = SlackAdapter(config)
        try:
            assert await adapter.connect()
            await asyncio.wait_for(socket_open.wait(), 10)
            client = adapter._app.client
            assert (await client.users_info(user="USMOKE"))["ok"]
            assert (await adapter.send("CUNSEEN", "proactive home notice")).success
            assert (await client.chat_postMessage(channel="CSMOKE", text="smoke"))["ok"]
            data = await adapter._download_slack_file_bytes("https://files.slack.com/files-pri/TSMOKE-FSMOKE/a.bin")
            assert data == b"download-bytes"
            assert (await client.files_upload_v2(content="upload-bytes", channel="CSMOKE"))["ok"]
            for target in ("user:USMOKE", "user_name:@smoke", "U12345678"):
                dm, error = await asyncio.to_thread(_slack_dm_chat_id, config, target)
                assert dm == "DSMOKE" and error is None, (dm, error)
                assert (await adapter.send(dm, "gateway tool smoke")).success
                result = await _standalone_send(config, target, "cron smoke")
                assert result["success"], result
            socket_open.clear()
            assert await adapter.connect(is_reconnect=True)
            await asyncio.wait_for(socket_open.wait(), 10)
            for path in ("/api/slack/auth.test", "/api/socket/apps.connections.open",
                         "/api/slack/chat.postMessage", "/api/slack/conversations.open",
                         "/api/files/upload/smoke", "/api/slack/files.completeUploadExternal"):
                assert path in calls, (path, calls)
            # Exercise the gateway reconnect queue, not merely adapter.connect twice.
            await adapter.disconnect()
            broker_up = False
            failed = SlackAdapter(config)
            assert not await failed.connect()
            await failed.disconnect()
            gateway = object.__new__(GatewayRunner)
            gateway._failed_platforms = {}
            gateway.adapters = {}
            gateway._update_platform_runtime_status = lambda *_args, **_kwargs: None
            gateway._startup_queue_transient_failure(Platform.SLACK, failed, config, "broker down", [])
            assert Platform.SLACK in gateway._failed_platforms
            gateway._flag_reconnect_needs_attention = lambda *_: None
            gateway._wire_adapter_handlers = lambda *_: None
            gateway._create_adapter = lambda *_: SlackAdapter(config)
            async def connect(current, _, **kwargs):
                return await current.connect(**kwargs)
            async def install(_, current):
                gateway.adapters[Platform.SLACK] = current
                del gateway._failed_platforms[Platform.SLACK]
            gateway._connect_adapter_with_timeout = connect
            gateway._install_reconnected_adapter = install
            broker_up = True
            await gateway._reconnect_failed_platform(Platform.SLACK, float("inf"))
            assert Platform.SLACK in gateway.adapters
            assert not gateway._failed_platforms
            adapter = gateway.adapters[Platform.SLACK]
            print(f"PASS: {len(calls)} broker requests; no Slack tokens; proactive API, strict socket, files, gateway/cron user targets, NO_PROXY, gateway reconnect")
        finally:
            await adapter.disconnect()
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
