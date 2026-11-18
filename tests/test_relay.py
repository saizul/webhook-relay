import asyncio

import httpx
import pytest

from conftest import Clock, Recorder, base_config, make_config
from webhook_relay.config import RetryPolicy
from webhook_relay.relay import Dispatcher, backoff_delay, is_retryable
from webhook_relay.store import Store


def test_backoff_exponential_and_capped():
    policy = RetryPolicy(initial_backoff=2, multiplier=3, max_backoff=50, jitter=0)
    assert [backoff_delay(n, policy) for n in range(1, 6)] == [2, 6, 18, 50, 50]


def test_backoff_jitter_bounds():
    policy = RetryPolicy(initial_backoff=10, multiplier=2, max_backoff=100, jitter=0.2)
    assert backoff_delay(1, policy, rand=lambda: 0.0) == pytest.approx(8)
    assert backoff_delay(1, policy, rand=lambda: 1.0) == pytest.approx(12)
    assert backoff_delay(1, policy, rand=lambda: 0.5) == pytest.approx(10)


@pytest.mark.parametrize(
    ("code", "retry_4xx", "expected"),
    [
        (None, False, True),
        (500, False, True),
        (503, False, True),
        (429, False, True),
        (408, False, True),
        (400, False, False),
        (404, False, False),
        (404, True, True),
        (302, False, True),
    ],
)
def test_is_retryable(code, retry_4xx, expected):
    assert is_retryable(code, RetryPolicy(retry_on_4xx=retry_4xx)) is expected


def setup(responses, data=None):
    clock = Clock()
    recorder = Recorder(responses)
    config = make_config(data or base_config())
    store = Store(":memory:")
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    dispatcher = Dispatcher(config, store, client=client, clock=clock, rand=lambda: 0.5)
    route = config.routes["gh"]
    delivery_id = store.add_delivery(
        "gh",
        {"content-type": "application/json", "x-github-event": "push", "x-secret-internal": "nope"},
        b'{"a":1}',
        [(t.name, t.url) for t in route.targets],
        clock(),
    )
    return clock, recorder, store, dispatcher, delivery_id


def test_retries_with_backoff_then_success():
    clock, recorder, store, dispatcher, delivery_id = setup([500, httpx.ConnectError("refused"), 200])
    assert asyncio.run(dispatcher.run_once()) == 1
    job = store.jobs_for(delivery_id)[0]
    assert (job.status, job.attempts, job.last_status_code) == ("pending", 1, 500)
    assert job.next_attempt_at == clock() + 10

    clock.advance(9)
    assert asyncio.run(dispatcher.run_once()) == 0  # not due yet
    clock.advance(1)
    assert asyncio.run(dispatcher.run_once()) == 1
    job = store.jobs_for(delivery_id)[0]
    assert job.attempts == 2 and job.last_status_code is None and "ConnectError" in job.last_error
    assert job.next_attempt_at == clock() + 20  # 10 * 2

    clock.advance(20)
    asyncio.run(dispatcher.run_once())
    job = store.jobs_for(delivery_id)[0]
    assert (job.status, job.attempts, job.last_status_code) == ("succeeded", 3, 200)
    assert store.get_delivery(delivery_id).status == "delivered"
    assert len(recorder.requests) == 3
    assert [r.headers["x-webhook-relay-attempt"] for r in recorder.requests] == ["1", "2", "3"]


def test_dead_letter_after_max_attempts():
    clock, _recorder, store, dispatcher, delivery_id = setup([502, 502, 502, 200])
    for _ in range(3):
        asyncio.run(dispatcher.run_once())
        clock.advance(1000)
    job = store.jobs_for(delivery_id)[0]
    assert (job.status, job.attempts, job.next_attempt_at) == ("dead", 3, None)
    assert store.get_delivery(delivery_id).status == "dead"
    assert [d["delivery_id"] for d in store.dead_letters()] == [delivery_id]
    assert asyncio.run(dispatcher.run_once()) == 0


def test_non_retryable_4xx_goes_straight_to_dead_letters():
    _, _recorder, store, dispatcher, delivery_id = setup([400])
    asyncio.run(dispatcher.run_once())
    job = store.jobs_for(delivery_id)[0]
    assert (job.status, job.attempts, job.last_error) == ("dead", 1, "HTTP 400")


def test_forwarded_request_headers_and_body():
    data = base_config()
    data["routes"]["gh"]["targets"][0].update(
        {"headers": {"X-Token": "abc"}, "forward_headers": ["X-Secret-Internal"], "sign_secret_env": "TARGET_SIGN"}
    )
    _, recorder, _store, dispatcher, delivery_id = setup([200], data)
    asyncio.run(dispatcher.run_once())
    req = recorder.requests[0]
    assert req.method == "POST" and str(req.url) == "http://ci.internal/hook"
    assert req.content == b'{"a":1}'
    assert req.headers["content-type"] == "application/json"
    assert req.headers["x-github-event"] == "push"  # provider default
    assert req.headers["x-secret-internal"] == "nope"  # explicitly forwarded
    assert req.headers["x-token"] == "abc"
    assert req.headers["x-webhook-relay-delivery"] == delivery_id
    assert req.headers["x-webhook-relay-route"] == "gh"
    assert req.headers["x-webhook-relay-signature"].startswith("sha256=")


def test_unlisted_headers_are_not_forwarded():
    _, recorder, _, dispatcher, _ = setup([200])
    asyncio.run(dispatcher.run_once())
    assert "x-secret-internal" not in recorder.requests[0].headers


def test_fan_out_targets_are_independent():
    data = base_config(targets=[{"url": "http://a.local/x", "name": "a"}, {"url": "http://b.local/x", "name": "b"}])

    def handler(request):
        return httpx.Response(200 if request.url.host == "a.local" else 503)

    clock = Clock()
    config = make_config(data)
    store = Store(":memory:")
    dispatcher = Dispatcher(
        config, store, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), clock=clock, rand=lambda: 0.5
    )
    delivery_id = store.add_delivery("gh", {}, b"x", [("a", "http://a.local/x"), ("b", "http://b.local/x")], clock())
    asyncio.run(dispatcher.run_once())
    jobs = {j.target: j.status for j in store.jobs_for(delivery_id)}
    assert jobs == {"a": "succeeded", "b": "pending"}
    assert store.get_delivery(delivery_id).status == "pending"


def test_job_for_removed_target_is_dead():
    clock, _, store, dispatcher, _ = setup([200])
    other = store.add_delivery("gh", {}, b"x", [("gone", "http://gone/x")], clock())
    asyncio.run(dispatcher.run_once())
    assert store.jobs_for(other)[0].status == "dead"
