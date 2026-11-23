# ADR-0001: optional Slack credential-broker transport

Status: Accepted

Date: 2026-11-23

## Context

AGENT-338 requires Hermes to hold no Slack bot or app token. Mira runs the v0.23.0 fork. The platform has gateway, Bolt request, Socket Mode, private file, upload and standalone cron paths; changing only the initial Web API client would leave traffic outside the broker.

## Decision

Select broker routing through an optional Slack config mapping. Use an agent bearer with separate API, Socket Mode setup and fixed files routes. Apply routing to Bolt request clients and standalone delivery as well as startup clients. Ignore saved Slack tokens in broker mode. Keep direct WebSockets after proxied setup. Refuse file redirects, foreign CDN origins and route traversal. Keep the existing direct mode when the mapping is absent.

## Consequences

Slack credentials stay at the broker. Operators must provision three grants and a protected bearer file or scoped secret. Files on unsupported origins fail explicitly. SDK upload and Bolt construction behavior require lasting smoke coverage. Runtime deployment and live acceptance remain separate attended work.

## Alternatives considered

Keeping bot tokens in Hermes fails the custody requirement. An HTTP proxy alone does not replace credentials or control every absolute file URL. Routing WebSockets through the broker exceeds the signed-off scope.
