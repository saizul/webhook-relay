import asyncio
import hashlib
import hmac
import json
import time

import httpx

from conftest import AUTH, ENV, Recorder, base_config, make_config


def gh_headers(body: bytes, secret: str = ENV["GH_SECRET"]) -> dict:
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {
        "X-Hub-Signature-256": sig,
        "X-GitHub-Event": "push",
        "Content-Type": "application/json",
        "Authorization": "Bearer sender-credential",
        "Cookie": "a=b",
    }


def post(h, body=b'{"ref":"main"}', headers=None, route="gh"):
    return h.client.post(f"/hooks/{route}", content=body, headers=headers if headers is not None else gh_headers(body))


def test_health(harness):
    h = harness()
    r = h.client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok" and r.json()["jobs"] == {"pending": 0, "succeeded": 0, "dead": 0}


def test_accepts_verified_hook_and_relays(harness):
    h = harness()
    r = post(h)
    assert r.status_code == 202
    delivery_id = r.json()["id"]
    assert h.run_once() == 1
    assert h.recorder.requests[0].content == b'{"ref":"main"}'
    delivery = h.store.get_delivery(delivery_id)
    assert delivery.status == "delivered"
    # Credentials of the sender are never stored.
    assert "authorization" not in delivery.headers and "cookie" not in delivery.headers


def test_rejects_bad_signature_and_stores_nothing(harness):
    h = harness()
    body = b'{"ref":"main"}'
    r = post(h, body, gh_headers(body, secret="wrong"))
    assert r.status_code == 401
    assert "signature" in r.json()["error"]
    assert h.store.list_deliveries() == []


def test_unknown_route(harness):
    assert post(harness(), route="nope").status_code == 404


def test_get_not_allowed(harness):
    assert harness().client.get("/hooks/gh").status_code == 405


def test_body_size_limit(harness):
    data = base_config()
    data["server"]["max_body_bytes"] = 16
    h = harness(make_config(data))
    body = b"x" * 17
    assert post(h, body, gh_headers(body)).status_code == 413

    # Without Content-Length (chunked upload) the limit is enforced while streaming.
    def chunks():
        yield b"x" * 10
        yield b"x" * 10

    r = h.client.post("/hooks/gh", content=chunks(), headers=gh_headers(b"x" * 20))
    assert r.status_code == 413
    ok = b"x" * 16
    assert post(h, ok, gh_headers(ok)).status_code == 202


def test_stripe_route_with_tolerance(harness, clock):
    data = {
        "server": {"database": ":memory:"},
        "routes": {"pay": {"provider": "stripe", "secret_env": "STRIPE_SECRET", "targets": ["http://billing/x"]}},
    }
    h = harness(make_config(data))
    body = b'{"type":"invoice.paid"}'
    ts = int(clock())
    v1 = hmac.new(ENV["STRIPE_SECRET"].encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    headers = {"Stripe-Signature": f"t={ts},v1={v1}"}
    assert post(h, body, headers, route="pay").status_code == 202
    clock.advance(301)
    assert post(h, body, headers, route="pay").status_code == 401
    h.run_once()
    assert h.recorder.requests[0].headers["stripe-signature"] == f"t={ts},v1={v1}"


def test_slack_url_verification(harness, clock):
    data = {
        "server": {"database": ":memory:"},
        "routes": {"slack": {"provider": "slack", "secret_env": "SLACK_SECRET", "targets": ["http://bot/x"]}},
    }
    h = harness(make_config(data))
    body = json.dumps({"type": "url_verification", "challenge": "abc123"}).encode()
    ts = str(int(clock()))
    sig = "v0=" + hmac.new(ENV["SLACK_SECRET"].encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
    r = post(
        h,
        body,
        {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig, "Content-Type": "application/json"},
        route="slack",
    )
    assert r.status_code == 200 and r.json() == {"challenge": "abc123"}
    assert h.store.list_deliveries() == []


def test_api_requires_token(harness):
    h = harness()
    assert h.client.get("/api/deliveries").status_code == 401
    assert h.client.get("/api/deliveries", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert h.client.get("/api/deliveries", headers=AUTH).status_code == 200


def test_api_disabled_without_token(harness):
    h = harness(make_config(env={"WEBHOOK_RELAY_API_TOKEN": ""}))
    r = h.client.get("/api/deliveries", headers=AUTH)
    assert r.status_code == 403 and "disabled" in r.json()["error"]


def test_api_list_filter_and_detail(harness, clock):
    h = harness(recorder=Recorder([200, 400]))
    first = post(h).json()["id"]
    clock.advance(1)
    second = post(h, b"\xff\xfe").json()["id"]
    h.run_once()

    items = h.client.get("/api/deliveries", headers=AUTH).json()["items"]
    assert [i["id"] for i in items] == [second, first]  # newest first
    dead = h.client.get("/api/deliveries?status=dead", headers=AUTH).json()["items"]
    assert [i["id"] for i in dead] == [second]
    assert h.client.get("/api/deliveries?status=weird", headers=AUTH).status_code == 400
    assert h.client.get("/api/deliveries?limit=x", headers=AUTH).status_code == 400
    assert h.client.get("/api/deliveries?limit=1&offset=1", headers=AUTH).json()["items"][0]["id"] == first

    detail = h.client.get(f"/api/deliveries/{second}?include_body=1", headers=AUTH).json()
    assert detail["body_encoding"] == "base64" and detail["jobs"][0]["status"] == "dead"
    detail = h.client.get(f"/api/deliveries/{first}?include_body=1", headers=AUTH).json()
    assert detail["body"] == '{"ref":"main"}' and detail["headers"]["x-github-event"] == "push"
    assert "body" not in h.client.get(f"/api/deliveries/{first}", headers=AUTH).json()
    assert h.client.get("/api/deliveries/missing", headers=AUTH).status_code == 404

    letters = h.client.get("/api/dead-letters", headers=AUTH).json()["items"]
    assert [d["delivery_id"] for d in letters] == [second]


def test_api_replay(harness):
    h = harness(recorder=Recorder([400, 200]))
    delivery_id = post(h).json()["id"]
    h.run_once()
    assert h.store.get_delivery(delivery_id).status == "dead"

    assert h.client.post(f"/api/deliveries/{delivery_id}/replay").status_code == 401
    assert h.client.post(f"/api/deliveries/{delivery_id}/replay?target=nope", headers=AUTH).status_code == 404
    assert h.client.post("/api/deliveries/missing/replay", headers=AUTH).status_code == 404
    r = h.client.post(f"/api/deliveries/{delivery_id}/replay", headers=AUTH)
    assert r.status_code == 202 and r.json()["queued"] == 1
    h.run_once()
    job = h.store.jobs_for(delivery_id)[0]
    assert (job.status, job.replays, job.attempts) == ("succeeded", 1, 1)
    assert len(h.recorder.requests) == 2
    assert h.recorder.requests[0].content == h.recorder.requests[1].content


def test_replay_single_target(harness):
    data = base_config(targets=[{"url": "http://a.local/x", "name": "a"}, {"url": "http://b.local/x", "name": "b"}])
    h = harness(make_config(data))
    delivery_id = post(h).json()["id"]
    h.run_once()
    r = h.client.post(f"/api/deliveries/{delivery_id}/replay?target=b", headers=AUTH)
    assert r.json()["queued"] == 1
    h.run_once()
    assert [str(r.url.host) for r in h.recorder.requests][-1] == "b.local"
    assert {j.target: j.replays for j in h.store.jobs_for(delivery_id)} == {"a": 0, "b": 1}


def test_worker_delivers_in_background(tmp_path):
    """End to end with the real lifespan worker (no manual run_once)."""
    from starlette.testclient import TestClient

    from webhook_relay.app import create_app
    from webhook_relay.store import Store

    recorder = Recorder()
    data = base_config()
    data["server"].update({"database": str(tmp_path / "relay.db"), "poll_interval": 0.05})
    config = make_config(data)
    app = create_app(
        config, Store(config.server.database), client=httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    )
    with TestClient(app) as client:
        body = b'{"ref":"main"}'
        delivery_id = client.post("/hooks/gh", content=body, headers=gh_headers(body)).json()["id"]
        deadline = time.time() + 5
        while time.time() < deadline and not recorder.requests:
            asyncio.run(asyncio.sleep(0.05))
    assert len(recorder.requests) == 1
    assert app.state.store.get_delivery(delivery_id).status == "delivered"
