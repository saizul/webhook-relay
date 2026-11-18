# Contributing to webhook-relay

Thank you for helping! New providers, delivery improvements, documentation and
bug reports are all welcome.

- [Ways to contribute](#ways-to-contribute)
- [Good first issues](#good-first-issues)
- [Development setup](#development-setup)
- [Project layout](#project-layout)
- [Add a provider](#add-a-provider)
- [Guidelines](#guidelines)
- [Pull request process](#pull-request-process)

## Ways to contribute

| Size | Idea |
| --- | --- |
| 10 minutes | Fix a typo, clarify a config option, add a FAQ entry |
| 30 minutes | Improve an error message or add a missing test case |
| 1-2 hours | Add a provider (see below) or one of the good first issues |
| Larger | Event filters, payload transforms, a web UI, Postgres storage (open an issue first) |

Comment on an issue before you start so nobody else picks the same one.

## Good first issues

Each of these is self-contained and touches one or two files.

1. **GitLab provider.** GitLab sends the secret in plain text in `X-Gitlab-Token`.
   Compare it with `hmac.compare_digest`; forward `X-Gitlab-Event` and
   `X-Gitlab-Event-UUID`. (`signatures.py`, tests)
2. **Standard Webhooks / Svix provider.** Headers `webhook-id`, `webhook-timestamp`,
   `webhook-signature: v1,<base64>`; signed content is `id.timestamp.body`; the secret is
   base64 after a `whsec_` prefix; support several space-separated signatures and a
   tolerance option. (`signatures.py`, tests)
3. **Twilio provider.** HMAC-SHA1 over the full URL plus sorted form parameters,
   base64 in `X-Twilio-Signature`. Needs the public URL as an option (`public_url`)
   because the relay may sit behind a proxy. (`signatures.py`, tests)
4. **Honour `Retry-After`.** When a target answers `429` or `503` with `Retry-After`
   (seconds or an HTTP date), use it instead of the computed backoff, capped at
   `max_backoff`. (`relay.py`, `tests/test_relay.py`)
5. **Secret rotation.** Allow `secret_env` to be a list; a request is valid if any
   secret matches. (`config.py`, `app.py`, tests, README)
6. **Retention.** Add `server.retention_days`; the worker deletes deliveries whose jobs
   are all finished and older than that, at most once per hour. (`config.py`,
   `store.py`, `relay.py`, tests)
7. **`/metrics` endpoint.** Prometheus text format with counters for accepted, rejected
   (by reason), delivered and dead jobs, without a new dependency. (`app.py`, tests)
8. **IP allowlist per route.** `allow_ips: ["192.30.252.0/22"]` using `ipaddress`;
   respect `--proxy-headers`. (`config.py`, `app.py`, tests)
9. **`list --format table`.** A human-readable table for the CLI next to JSON. (`cli.py`)

## Development setup

```bash
git clone https://github.com/saizul/webhook-relay
cd webhook-relay
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e .[dev]
pytest
ruff check . && ruff format --check .
```

Python 3.11+ is needed. Tests must not use the network: use `httpx.MockTransport`
(see `Recorder` in `tests/conftest.py`) and the `Clock` fixture instead of sleeping.

To try the server by hand:

```bash
export GITHUB_WEBHOOK_SECRET=dev STRIPE_WEBHOOK_SECRET=dev SHOPIFY_WEBHOOK_SECRET=dev \
  SLACK_SIGNING_SECRET=dev PARTNER_WEBHOOK_SECRET=dev RELAY_SIGNING_SECRET=dev \
  WEBHOOK_RELAY_DATABASE=./dev.db WEBHOOK_RELAY_API_TOKEN=dev
webhook-relay -c examples/config.yaml serve --log-level debug
```

## Project layout

| Path | Responsibility |
| --- | --- |
| `src/webhook_relay/signatures.py` | `Provider` base class, one class per provider, `PROVIDERS` registry |
| `src/webhook_relay/config.py` | YAML loading, validation, secrets from the environment |
| `src/webhook_relay/store.py` | SQLite schema, deliveries, jobs, dead letters, replay |
| `src/webhook_relay/relay.py` | `Dispatcher`: forwarding, retries, backoff, the worker loop |
| `src/webhook_relay/app.py` | Starlette app: `/hooks`, `/healthz`, `/api` |
| `src/webhook_relay/cli.py` | `serve`, `check-config`, `list`, `show`, `dead-letters`, `replay` |
| `tests/` | pytest suite; `conftest.py` has the fake env, clock, recorder and harness |

## Add a provider

1. **Read the provider's docs** and note: which header(s), what exactly is signed
   (body only, or timestamp/URL too), the algorithm, the encoding (hex/base64), any
   prefix such as `sha256=`, and whether there is a replay window.

2. **Write the class** in `src/webhook_relay/signatures.py`:

   ```python
   class GitLab(Provider):
       """``X-Gitlab-Token: <secret>`` (a shared token, not an HMAC)."""

       name = "gitlab"                                   # value of `provider:` in the config
       forward_headers = ("x-gitlab-event", "x-gitlab-event-uuid")   # lower-case
       # default_options = {"tolerance": 300}            # only if the provider has options

       @classmethod
       def verify(cls, body, headers, secret, options, now=None) -> None:
           token = _header(headers, "X-Gitlab-Token")
           if not hmac.compare_digest(token.encode(), secret.encode()):
               raise SignatureError("token mismatch")
   ```

   - Always compare with `hmac.compare_digest`; never `==`.
   - Raise `SignatureError` with a short reason. **Never put the secret or the expected
     signature in the message**: it is returned to the caller and logged.
   - Use `_check_tolerance(timestamp, now, options["tolerance"])` for timestamped schemes
     and accept `now` from the caller (tests pass a fixed clock).
   - Override `validate_options` if your options need more than the default checks.
   - Set `needs_secret = False` only if the scheme really has no secret.

3. **Register it** by adding the class to the tuple that builds `PROVIDERS`.

4. **Test it** in `tests/test_signatures.py`:
   - a valid signature, ideally the **published test vector** from the provider's docs;
   - wrong secret, tampered body, missing header, malformed header;
   - timestamp outside the tolerance, if applicable.
   Update `test_registry_and_target_signature` with the new name. Use placeholder
   secrets such as `test-gitlab-secret`, never real ones.

5. **Document it**: add a row to the provider table in `README.md`, a route to
   `examples/config.yaml` if useful (and its env var to `test_example_config_is_valid`),
   and a line under "Unreleased" in `CHANGELOG.md`.

## Guidelines

- Keep runtime dependencies to Starlette, httpx, uvicorn and PyYAML; discuss new ones first.
- Never log or return secrets, signatures, request headers or bodies.
- Type hints everywhere; `ruff check` and `ruff format` clean.
- Tests are fast and deterministic: no network, no `sleep` (advance the `Clock` instead).
- Backwards compatible config: new keys get defaults; renamed keys need a deprecation path.

## Pull request process

1. Fork and create a branch (`provider/gitlab`, `fix/stripe-tolerance`).
2. Keep pull requests focused; fill in the checklist in the template.
3. Add an entry to `CHANGELOG.md` under "Unreleased".
4. A maintainer reviews within a few days.

By contributing you agree that your work is released under the MIT License and
that you follow the [Code of Conduct](CODE_OF_CONDUCT.md).
