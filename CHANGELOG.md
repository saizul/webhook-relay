# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-11-18

### Added

- Webhook intake at `POST /hooks/<route>` with a streaming request size limit
  (`server.max_body_bytes`, default 1 MiB).
- Signature verification for GitHub (`X-Hub-Signature-256`), Stripe (`Stripe-Signature`
  with timestamp tolerance), Shopify (`X-Shopify-Hmac-Sha256`), Slack (signing secret,
  replay window and `url_verification` challenge) and a generic HMAC header
  (sha1/sha256/sha512, hex/base64, optional prefix); `none` for trusted networks.
- SQLite storage of every verified delivery with one job per target; sender credentials
  (`Authorization`, `Cookie`, `X-API-Key`) are never stored.
- Fan-out to multiple targets with retries, exponential backoff, jitter, per-route and
  per-target policies and a dead-letter list.
- Optional re-signing of forwarded requests (`X-Webhook-Relay-Signature`).
- Replay of one delivery, one target or every dead letter through the CLI and
  `POST /api/deliveries/{id}/replay`.
- Read-only JSON API (`/api/deliveries`, `/api/deliveries/{id}`, `/api/dead-letters`)
  protected by a bearer token, and `GET /healthz`.
- CLI: `serve`, `check-config`, `list`, `show`, `dead-letters`, `replay`.
- YAML configuration with strict validation; secrets only from environment variables.
- Container image on `ghcr.io/saizul/webhook-relay` (non-root, health check).

[Unreleased]: https://github.com/saizul/webhook-relay/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/saizul/webhook-relay/releases/tag/v0.1.0
