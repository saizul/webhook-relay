# Security Policy

## Supported versions

| Version | Supported |
| --- | --- |
| 0.1.x | yes |

## What counts as a vulnerability

- A request with a missing, wrong or expired signature being accepted, stored or relayed
- Timing or other side channels that leak a signing secret or the API token
- A secret, the API token or a stored request credential appearing in logs, API responses
  or error messages
- Bypassing the request size limit or exhausting memory or disk with crafted requests
- Reaching the JSON API without a valid token, or making the relay send requests to a
  URL that is not configured as a target
- SQL injection or path traversal through route names, query parameters or headers

Webhooks being delivered more than once is expected (at-least-once delivery) and is not
a vulnerability.

## Reporting

Please **do not** open a public issue. Use
[GitHub private vulnerability reporting](https://github.com/saizul/webhook-relay/security/advisories/new)
with the version, the impact and steps to reproduce. Do not include real secrets or
customer payloads.

You will get an answer within 5 working days. Fixes are released as soon as
possible and credited in the changelog unless you prefer otherwise.

## Hardening checklist for operators

- Terminate TLS in a reverse proxy and expose only `/hooks/*` publicly.
- Set a long random `WEBHOOK_RELAY_API_TOKEN`, or leave it unset to disable the API.
- Keep the `/data` volume private; it contains webhook bodies.
- Use the `none` provider only on trusted networks.
