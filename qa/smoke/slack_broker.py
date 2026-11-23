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
from gateway.config import PlatformConfig
from plugins.platforms.slack.adapter import SlackAdapter, _standalone_send


async def main():
    calls = []
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
        if request.path.startswith("/api/files/"):
            if request.method == "POST":
                assert await request.read() == b"upload-bytes"
                return web.Response(text="OK")
            return web.Response(body=b"download-bytes", content_type="application/octet-stream")
        method = request.path.rsplit("/", 1)[-1]
        responses = {
            "auth.test": {"team_id": "TSMOKE", "team": "Smoke", "user_id": "USMOKE", "bot_id": "BSMOKE"},
            "chat.postMessage": {"ts": "1.2", "channel": "CSMOKE"},
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
        adapter = SlackAdapter(config)
        try:
            assert await adapter.connect()
            await asyncio.wait_for(socket_open.wait(), 10)
            client = adapter._team_clients["TSMOKE"]
            assert (await client.chat_postMessage(channel="CSMOKE", text="smoke"))["ok"]
            data = await adapter._download_slack_file_bytes("https://files.slack.com/files-pri/TSMOKE-FSMOKE/a.bin")
            assert data == b"download-bytes"
            assert (await client.files_upload_v2(content="upload-bytes", channel="CSMOKE"))["ok"]
            result = await _standalone_send(config, "USMOKE", "cron smoke")
            assert result["success"], result
            socket_open.clear()
            assert await adapter.connect(is_reconnect=True)
            await asyncio.wait_for(socket_open.wait(), 10)
            for path in ("/api/slack/auth.test", "/api/socket/apps.connections.open",
                         "/api/slack/chat.postMessage", "/api/slack/conversations.open",
                         "/api/files/upload/smoke", "/api/slack/files.completeUploadExternal"):
                assert path in calls, (path, calls)
            print(f"PASS: {len(calls)} broker requests; no Slack tokens; API, Socket Mode, files, cron, reconnect")
        finally:
            await adapter.disconnect()
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
