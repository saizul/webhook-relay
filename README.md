# webhook-relay

[![CI](https://github.com/saizul/webhook-relay/actions/workflows/ci.yml/badge.svg)](https://github.com/saizul/webhook-relay/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/saizul/webhook-relay?sort=semver)](https://github.com/saizul/webhook-relay/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Container image](https://img.shields.io/badge/ghcr.io-saizul%2Fwebhook--relay-2496ED?logo=docker&logoColor=white)](https://github.com/saizul/webhook-relay/pkgs/container/webhook-relay)
![Python](https://img.shields.io/badge/python-3.11%E2%80%933.13-blue)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

**A self-hosted webhook inbox and relay.** Point GitHub, Stripe, Shopify, Slack
or any HMAC-signing service at one small container. webhook-relay verifies the
signature, stores every delivery in SQLite, answers the sender right away and
then forwards the request to one or more internal services, retrying with
exponential backoff. Deliveries that keep failing land in a dead-letter list
that you can inspect and replay.

```text
 GitHub  ─┐                        ┌──▶ http://ci:8080/github
 Stripe  ─┼──▶  /hooks/<route>  ───┼──▶ http://billing:8000/stripe   (retries, backoff)
 Shopify ─┤     verify + store     └──▶ http://bot:3000/events
 Slack   ─┘     (SQLite)                    │ still failing?
                                            ▼
                               dead letters ──▶ replay (CLI or API)
```

## Contents

- [Features](#features)
- [Quick start](#quick-start)
- [Providers](#providers)
- [Configuration reference](#configuration-reference)
- [Delivery and retries](#delivery-and-retries)
- [HTTP API](#http-api)
- [CLI](#cli)
- [Security notes](#security-notes)
- [FAQ](#faq)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [Development](#development)
- [License](#license)

## Features

- **Signature verification** for GitHub, Stripe (with timestamp tolerance), Shopify, Slack
  (with replay window) and a configurable generic HMAC header; constant-time comparisons
- **Durable inbox**: every verified request (headers and raw body) is stored in SQLite before
  the sender gets `202 Accepted`, so nothing is lost when a target is down
- **Fan-out** to any number of targets per route, each tracked independently
- **Retries with exponential backoff and jitter**, per-route and per-target policies,
  smart handling of 4xx vs 5xx responses, and `Retry-After` support for rate-limited targets
- **Dead-letter list** plus **replay** of one delivery, one target or every dead letter
- **Read-only JSON API** to list and inspect deliveries (bearer token), and a CLI
- **Optional re-signing** of forwarded requests so targets can trust the relay
- **Safe defaults**: secrets only from environment variables and never logged, request size
  limit, sender credentials (`Authorization`, `Cookie`) never stored, non-root container
- Small: Starlette + httpx + PyYAML + uvicorn, no external database or queue

## Quick start

### Docker

```bash
mkdir webhook-relay && cd webhook-relay
cat > config.yaml <<'YAML'
routes:
  github:
    provider: github
    secret_env: GITHUB_WEBHOOK_SECRET
    targets:
      - http://host.docker.internal:3000/github
YAML

docker run -d --name webhook-relay -p 8080:8080 \
  -e GITHUB_WEBHOOK_SECRET='the secret you entered on GitHub' \
  -e WEBHOOK_RELAY_API_TOKEN="$(openssl rand -hex 24)" \
  -v "$PWD/config.yaml:/config/config.yaml:ro" \
  -v webhook-relay-data:/data \
  ghcr.io/saizul/webhook-relay:latest

curl http://localhost:8080/healthz
```

Then set the payload URL in GitHub to `https://your-host/hooks/github` (content type
`application/json`). A full example with every provider is in
[`examples/config.yaml`](examples/config.yaml) and
[`examples/docker-compose.yml`](examples/docker-compose.yml).

### pip

```bash
pipx install git+https://github.com/saizul/webhook-relay
export GITHUB_WEBHOOK_SECRET=...
webhook-relay -c config.yaml check-config
webhook-relay -c config.yaml serve --host 0.0.0.0 --port 8080
```

### Try it locally

```bash
body='{"zen":"Keep it logically awesome."}'
sig="sha256=$(printf '%s' "$body" | openssl dgst -sha256 -hmac "$GITHUB_WEBHOOK_SECRET" | awk '{print $NF}')"
curl -i http://localhost:8080/hooks/github \
  -H "Content-Type: application/json" -H "X-GitHub-Event: ping" \
  -H "X-Hub-Signature-256: $sig" -d "$body"
# HTTP/1.1 202 Accepted
# {"id":"5f0c...","status":"accepted"}
```

## Providers

| `provider` | Verifies | Headers forwarded by default | Options |
| --- | --- | --- | --- |
| `github` | `X-Hub-Signature-256: sha256=<hex HMAC-SHA256(body)>` | `X-GitHub-Event`, `X-GitHub-Delivery`, `X-GitHub-Hook-ID`, `X-Hub-Signature-256` | - |
| `stripe` | `Stripe-Signature: t=<ts>,v1=<hex HMAC-SHA256("ts.body")>` (any `v1` matches) | `Stripe-Signature` | `tolerance` (seconds, default 300, 0 = off) |
| `shopify` | `X-Shopify-Hmac-Sha256: <base64 HMAC-SHA256(body)>` | `X-Shopify-Topic`, `X-Shopify-Shop-Domain`, `X-Shopify-Webhook-Id`, `X-Shopify-API-Version`, `X-Shopify-Hmac-Sha256` | - |
| `slack` | `X-Slack-Signature: v0=<hex HMAC-SHA256("v0:ts:body")>` + `X-Slack-Request-Timestamp`; answers `url_verification` challenges | `X-Slack-Signature`, `X-Slack-Request-Timestamp` | `tolerance` (seconds, default 300, 0 = off) |
| `hmac` | `<header>: <prefix><hex or base64 HMAC(body)>` | - | `header` (default `X-Signature`), `algorithm` (`sha1`, `sha256`, `sha512`), `encoding` (`hex`, `base64`), `prefix` |
| `none` | Nothing. Only for trusted networks and testing | - | - |

`Content-Type` is always forwarded. Missing a provider? It is a great
[first contribution](CONTRIBUTING.md#add-a-provider).

## Configuration reference

The config is YAML, passed with `-c/--config` or `WEBHOOK_RELAY_CONFIG`
(the Docker image uses `/config/config.yaml`). Unknown keys are rejected so
typos fail fast. Run `webhook-relay check-config` to validate it.

### `server`

| Key | Default | Description |
| --- | --- | --- |
| `database` | `webhook-relay.db` | SQLite file. `WEBHOOK_RELAY_DATABASE` overrides it (the image sets `/data/webhook-relay.db`) |
| `max_body_bytes` | `1048576` | Larger requests get `413`; enforced while streaming, also without `Content-Length` |
| `api_token_env` | `WEBHOOK_RELAY_API_TOKEN` | Environment variable holding the API bearer token. The API is disabled when it is unset |
| `poll_interval` | `1.0` | Seconds between checks for due retries |
| `concurrency` | `4` | Maximum parallel outgoing requests (1-64) |

### `retry` (top level, per route, or per target)

| Key | Default | Description |
| --- | --- | --- |
| `max_attempts` | `5` | Total attempts, including the first |
| `initial_backoff` | `2` | Seconds before the first retry |
| `multiplier` | `2` | Growth factor between retries (>= 1) |
| `max_backoff` | `600` | Cap for one delay |
| `jitter` | `0.1` | Random spread as a fraction (0-1) |
| `timeout` | `10` | Per-request timeout in seconds |
| `retry_on_4xx` | `false` | Retry every 4xx. By default only 408, 409, 425 and 429 are retried |
| `respect_retry_after` | `true` | Wait as long as a `429`/`503` response's `Retry-After` asks (capped at `max_backoff`) |

A route's `retry` inherits from the top level and a target's `retry` from its route.

### `routes.<name>`

The route name is the URL path segment: `POST /hooks/<name>`.

| Key | Required | Description |
| --- | --- | --- |
| `provider` | yes | One of the [providers](#providers) |
| `secret_env` | yes (not for `none`) | Environment variable holding the signing secret |
| `options` | no | Provider options (see the table above) |
| `retry` | no | Overrides for this route |
| `targets` | yes | List of targets (a plain URL string is also accepted) |

### `routes.<name>.targets[]`

| Key | Default | Description |
| --- | --- | --- |
| `url` | required | Absolute `http://` or `https://` URL; the body is sent unchanged with `POST` |
| `name` | `<route>-<index>` | Unique per route; used in the API, logs and `replay --target` |
| `headers` | `{}` | Extra headers to send (for example an internal auth header) |
| `forward_headers` | `[]` | Additional request headers to copy from the original delivery |
| `sign_secret_env` | - | If set, adds `X-Webhook-Relay-Signature: sha256=<hex HMAC-SHA256(body)>` |
| `retry` | route policy | Overrides for this target |

Every forwarded request also carries `X-Webhook-Relay-Delivery` (delivery id),
`X-Webhook-Relay-Route` and `X-Webhook-Relay-Attempt`.

### Environment variables

| Variable | Description |
| --- | --- |
| `WEBHOOK_RELAY_CONFIG` | Config file path |
| `WEBHOOK_RELAY_DATABASE` | Overrides `server.database` |
| `WEBHOOK_RELAY_HOST`, `WEBHOOK_RELAY_PORT` | Listen address for `serve` (defaults `127.0.0.1:8080`, image `0.0.0.0:8080`) |
| `WEBHOOK_RELAY_LOG_LEVEL` | `debug`, `info` (default), `warning`, `error` |
| `WEBHOOK_RELAY_API_TOKEN` | Bearer token for the JSON API (name configurable) |
| anything referenced by `secret_env` / `sign_secret_env` | Signing secrets |

## Delivery and retries

1. A request to `/hooks/<route>` is size-checked, then its signature is verified.
   Invalid or stale signatures get `401` and are **not** stored.
2. The delivery is written to SQLite with one job per target, and the sender gets `202`.
3. The worker posts the original body to each target. A `2xx` marks the job `succeeded`.
4. Network errors, timeouts, `5xx`, `408`, `409`, `425` and `429` are retried after
   `initial_backoff * multiplier^(n-1)` seconds (capped, with jitter). Other `4xx`
   responses go straight to the dead-letter list unless `retry_on_4xx` is on.
   When a `429` or `503` response carries `Retry-After` (seconds or an HTTP date), the
   next attempt waits that long instead, capped at `max_backoff`; the job's `last_error`
   then reads like `HTTP 429 (Retry-After 120s)`. Set `respect_retry_after: false` to
   always use the computed backoff. `Retry-After` never adds attempts beyond `max_attempts`.
5. After `max_attempts` the job becomes `dead`. A delivery's status is `pending` while
   any job is pending, `dead` if any job is dead, otherwise `delivered`.

Jobs survive restarts: pending retries continue when the container comes back.

## HTTP API

| Method and path | Description |
| --- | --- |
| `POST /hooks/{route}` | Webhook intake. `202 {"id": ...}`, `401` bad signature, `404` unknown route, `413` too large |
| `GET /healthz` | `{"status": "ok", "version": ..., "routes": n, "jobs": {"pending": ..., "succeeded": ..., "dead": ...}}`; `503` if the database fails |
| `GET /api/deliveries` | Newest first. Query: `route`, `status` (`pending`, `delivered`, `dead`), `limit` (max 500), `offset` |
| `GET /api/deliveries/{id}` | Headers and per-target jobs. Add `?include_body=1` for the body (UTF-8 or base64) |
| `GET /api/dead-letters` | Jobs that ran out of attempts. Query: `route`, `limit`, `offset` |
| `POST /api/deliveries/{id}/replay` | Queue the delivery again (`?target=<name>` for one target). `202 {"queued": n}` |

`/api/*` needs `Authorization: Bearer $WEBHOOK_RELAY_API_TOKEN`. Apart from
replay the API is read-only.

```bash
curl -s -H "Authorization: Bearer $WEBHOOK_RELAY_API_TOKEN" \
  "http://localhost:8080/api/deliveries?status=dead&limit=10"
```

## CLI

```text
webhook-relay [-c CONFIG] serve [--host H] [--port P] [--log-level L] [--proxy-headers]
webhook-relay [-c CONFIG] check-config
webhook-relay [-c CONFIG] list [--route R] [--status pending|delivered|dead] [--limit N]
webhook-relay [-c CONFIG] show DELIVERY_ID
webhook-relay [-c CONFIG] dead-letters [--route R]
webhook-relay [-c CONFIG] replay DELIVERY_ID [--target NAME] [--deliver-now]
webhook-relay [-c CONFIG] replay --all-dead [--route R] [--deliver-now]
```

`replay` re-queues jobs in the database; the running server picks them up
within `poll_interval`. With `--deliver-now` the CLI sends them itself.
In Docker: `docker exec webhook-relay webhook-relay replay --all-dead`.

## Security notes

- Secrets are read only from environment variables, are excluded from object `repr`,
  and are never logged or returned by the API. Logs contain route names, delivery ids,
  target names and status codes, not headers, bodies or target URLs.
- `Authorization`, `Proxy-Authorization`, `Cookie` and `X-API-Key` request headers are
  dropped before storage. Bodies are stored as received: treat the database file as
  sensitive and keep `/data` private.
- Run behind a TLS-terminating reverse proxy (Caddy, nginx, Traefik). Only `/hooks/*`
  needs to be reachable from the internet; keep `/api/*` internal or protect it further.
- Stripe and Slack signatures include a timestamp; the default 5 minute tolerance limits
  replay attacks. GitHub and Shopify signatures do not, so a captured request could be
  re-sent; use their delivery id headers for idempotency in your targets.
- Targets come only from your config file, so the relay cannot be used to reach
  arbitrary URLs. Redirects from targets are not followed.

See [SECURITY.md](SECURITY.md) to report a vulnerability.

## FAQ

**Why does the sender get `202` before my service has received anything?**
Providers expect a fast answer (GitHub waits 10 seconds, Slack 3). Storing first and
delivering asynchronously means a slow or offline target never causes the provider to
mark the webhook as failed or disable it.

**Does it preserve the original body byte for byte?** Yes. Signatures are computed over
the raw body, and the same bytes are forwarded, so targets can verify the original
signature themselves (the provider's signature header is forwarded by default).

**Can targets trust that a request came through the relay?** Set `sign_secret_env` on the
target and verify `X-Webhook-Relay-Signature` there, or add a static auth header with
`headers`.

**What happens if the relay restarts during a retry?** Jobs are in SQLite; pending ones
continue after the restart. Delivery is at least once, so targets should be idempotent
(use `X-Webhook-Relay-Delivery` or the provider's delivery id).

**Can I run several replicas?** Not yet: the worker assumes it owns the SQLite file.
Run one instance (it handles a lot of traffic) or see the roadmap.

**How do I rotate a secret?** Update the environment variable and restart. Supporting two
secrets at once during rotation is on the roadmap.

**How big can the database get?** Every delivery is kept. Until retention lands
(roadmap), prune with `sqlite3 /data/webhook-relay.db "DELETE FROM deliveries WHERE received_at < strftime('%s','now','-30 days')"`.

## Roadmap

Items marked **good first issue** are small, well scoped and a great way to start;
see [CONTRIBUTING.md](CONTRIBUTING.md) for details.

- [ ] **good first issue** New providers: GitLab (`X-Gitlab-Token`), Twilio, Paddle,
      Standard Webhooks / Svix, Mailgun, Linear, Square, Bitbucket
- [ ] **good first issue** Accept several secrets per route for zero-downtime rotation
- [ ] **good first issue** `server.retention_days` with periodic pruning
- [ ] **good first issue** Prometheus `/metrics` endpoint (accepted, rejected, delivered, dead)
- [ ] **good first issue** Per-route IP allowlist (CIDR ranges)
- [ ] Filter which events go to which target (for example by `X-GitHub-Event`)
- [ ] Payload transforms (JSONPath to a new body) for chat targets
- [ ] Small read-only web UI for the inbox
- [ ] Postgres storage for multi-replica deployments

## Contributing

Contributions of all sizes are welcome, from a typo fix to a new provider.
Start with [CONTRIBUTING.md](CONTRIBUTING.md): it lists beginner-friendly issues,
the project layout and a step-by-step guide to adding a provider. Please follow the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Development

```bash
git clone https://github.com/saizul/webhook-relay
cd webhook-relay
python -m venv .venv && . .venv/bin/activate
pip install -e .[dev]
pytest
ruff check . && ruff format --check .
docker build -t webhook-relay:dev .
```

Tests use `httpx.MockTransport` and an in-memory database; they never touch the network.

## License

[MIT](LICENSE) © Saizul Amin
