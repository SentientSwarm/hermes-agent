# ADR-0002: isolate Slack broker clients and share credential availability

Status: Accepted

Date: 2026-11-23

## Context

F-019 corrects failures found reviewing F-011. Bolt's Socket Mode handler defaults to the application's WebClient. Reconfiguring that shared client for the socket route redirects proactive Web API calls there too. Gateway startup, reconnect and multiplex credential gates previously considered only local bot tokens, dropping a configured broker identity. The send_message user resolver also used a fixed Slack endpoint before reaching the platform sender.

## Decision

Give Socket Mode its own WebClient in broker mode. Keep the Bolt application and per-request clients on the Web API route. Reuse the registry's config_connected_fn verdict when the gateway checks a plugin without a local token; keep that check in gateway/run_adapter_credentials.py and use it across startup, primary and secondary reconnect, and multiplex setup.

Pass the platform config into send_message's Slack user resolver so users.list and conversations.open use the broker's API route and agent bearer. Standalone cron delivery accepts the same user targets. Reject invalid or unreadable broker inputs with a warning and disconnected result; connect records a permanent config error. Never fall back to Slack credentials on a broker config error.

Select proxies against the actual broker destination, honoring its NO_PROXY host for Web API, socket setup, per-request clients, files and standalone delivery. Keep direct-mode behavior when no broker is configured.

## Consequences

The socket route needs only apps.connections.open. Proactive messages to channels not seen inbound remain on the API route. A temporary broker outage retains the identity in the gateway retry queue. The recording-broker smoke rejects other socket methods and exercises real gateway recovery, user resolution, files and ambient proxy bypass without Slack credentials or Slack writes.

## Alternatives considered

Resetting the application's base URL after Socket Mode starts still shares mutable client state across concurrent calls. Adding a dummy Slack token to enable reconnect violates the custody model. Fixing only cron or only gateway resolution leaves the other delivery path broken. Checking NO_PROXY against Slack hostnames does not describe broker traffic.
