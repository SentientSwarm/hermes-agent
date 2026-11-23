"""Optional credential-broker transport for the bundled Slack adapter."""
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from gateway.platforms._shared import get_scoped_secret


active_transport = ContextVar("slack_broker_transport", default=None)


@dataclass(frozen=True, repr=False)
class BrokerTransport:
    api_base_url: str
    socket_base_url: str
    files_base_url: str
    bearer: str

    @classmethod
    def from_config(cls, config):
        extra = getattr(config, "extra", None) or {}
        values = extra.get("broker")
        if values is None:
            return None
        if not isinstance(values, dict):
            raise ValueError("Slack broker must be a mapping")
        urls = []
        for key in ("api_base_url", "socket_base_url", "files_base_url"):
            value = values.get(key, "")
            parsed = urlsplit(value)
            if (parsed.scheme not in ("http", "https") or not parsed.hostname
                    or parsed.username or parsed.password or parsed.query or parsed.fragment):
                raise ValueError(f"Slack broker {key} must be an absolute HTTP(S) URL without credentials")
            urls.append(value.rstrip("/") + "/")
        token_file = values.get("agent_token_file")
        bearer = (Path(token_file).expanduser().read_text(encoding="utf-8-sig").strip() if token_file
                  else get_scoped_secret("SLACK_BROKER_TOKEN", ""))
        if not bearer or any(c.isspace() for c in bearer) or bearer.startswith(("xox", "xapp-")):
            raise ValueError("Slack broker requires an agent bearer via agent_token_file or SLACK_BROKER_TOKEN")
        return cls(*urls, bearer)

    def file_url(self, url):
        parsed = urlsplit(url)
        # A fixed upstream route cannot represent arbitrary Slack CDN origins.
        if (parsed.scheme != "https" or parsed.hostname != "files.slack.com"
                or parsed.port not in (None, 443) or parsed.username or parsed.password):
            raise ValueError("Slack broker files route accepts only https://files.slack.com URLs")
        path = parsed.path
        for _ in range(8):
            decoded = unquote(path)
            if decoded == path:
                break
            path = decoded
        else:
            raise ValueError("Slack broker file path has excessive encoding")
        if "\\" in path or any(part in (".", "..") for part in path.split("/")):
            raise ValueError("Slack broker file path cannot traverse routes")
        return self.files_base_url + parsed.path.lstrip("/") + ("?" + parsed.query if parsed.query else "")

    def configure_client(self, client, *, socket=False):
        client.base_url = self.socket_base_url if socket else self.api_base_url
        client.token = self.bearer
        # SDK uploads use a provider-returned absolute URL, bypassing base_url.
        # Keep their bytes behind the same fixed files route, too.
        async def upload(*, url, **kwargs):
            import aiohttp
            from slack_sdk.web.file_upload_v2_result import FileUploadV2Result
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.file_url(url), data=kwargs["data"],
                    headers={"Authorization": f"Bearer {self.bearer}"},
                    timeout=aiohttp.ClientTimeout(total=kwargs["timeout"]),
                    allow_redirects=False,
                ) as response:
                    return FileUploadV2Result(status=response.status, body=await response.text())
        client._upload_file = upload
