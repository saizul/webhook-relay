import asyncio
from email.utils import formatdate

import httpx
import pytest

from conftest import Clock, Recorder, base_config, make_config
from webhook_relay.config import ConfigError, RetryPolicy
from webhook_relay.relay import Dispatcher, parse_retry_after, retry_delay
from webhook_relay.store import Store

NOW = 1_700_000_000.0


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("120", 120.0),
        (" 0 ", 0.0),
        (formatdate(NOW + 90, usegmt=True), 90.0),
        (formatdate(NOW - 300, usegmt=True), 0.0),
        (None, None),
        ("", None),
        ("-5", None),
        ("1.5", None),
        ("soon", None),
        ("Wed, 99 Foo 2026 99:99:99 GMT", None),
    ],
)
def test_parse_retry_after(value, expected):
    assert parse_retry_after(value, NOW) == expected


def test_retry_delay_uses_header_for_429_and_503():
    policy = RetryPolicy(initial_backoff=2, max_backoff=600, jitter=0)
    assert retry_delay(1, policy, 429, "30", NOW) == (30.0, True)
    assert retry_delay(1, policy, 503, "45", NOW) == (45.0, True)


def test_retry_delay_caps_header_at_max_backoff():
    policy = RetryPolicy(initial_backoff=2, max_backoff=60, jitter=0)
    assert retry_delay(1, policy, 429, "86400", NOW) == (60.0, True)


def test_retry_delay_falls_back_to_backoff():
    policy = RetryPolicy(initial_backoff=2, multiplier=2, max_backoff=600, jitter=0)
    assert retry_delay(3, policy, 429, None, NOW) == (8.0, False)
    assert retry_delay(3, policy, 429, "later", NOW) == (8.0, False)
    assert retry_delay(3, policy, 500, "30", NOW) == (8.0, False)  # only 429/503
    assert retry_delay(3, policy, None, "30", NOW) == (8.0, False)


def test_retry_delay_can_be_disabled():
    policy = RetryPolicy(initial_backoff=2, max_backoff=600, jitter=0, respect_retry_after=False)
    assert retry_delay(1, policy, 429, "30", NOW) == (2.0, False)


def _dispatch(responses, data=None):
    clock = Clock(NOW)
    recorder = Recorder(responses)
    config = make_config(data or base_config())
    store = Store(":memory:")
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    dispatcher = Dispatcher(config, store, client=client, clock=clock, rand=lambda: 0.5)
    delivery_id = store.add_delivery("gh", {"content-type": "application/json"}, b"{}", [("ci", "http://ci")], clock())
    return clock, store, dispatcher, delivery_id


def test_dispatcher_schedules_retry_from_header():
    clock, store, dispatcher, delivery_id = _dispatch([httpx.Response(429, headers={"Retry-After": "42"}), 200])
    asyncio.run(dispatcher.run_once())
    job = store.jobs_for(delivery_id)[0]
    assert (job.status, job.last_status_code) == ("pending", 429)
    assert job.next_attempt_at == clock() + 42
    assert job.last_error == "HTTP 429 (Retry-After 42s)"

    clock.advance(41)
    assert asyncio.run(dispatcher.run_once()) == 0
    clock.advance(1)
    asyncio.run(dispatcher.run_once())
    assert store.jobs_for(delivery_id)[0].status == "succeeded"


def test_dispatcher_http_date_and_cap():
    # base_config caps max_backoff at 100 seconds.
    when = formatdate(NOW + 3600, usegmt=True)
    clock, store, dispatcher, delivery_id = _dispatch([httpx.Response(503, headers={"Retry-After": when})])
    asyncio.run(dispatcher.run_once())
    assert store.jobs_for(delivery_id)[0].next_attempt_at == clock() + 100


def test_dispatcher_ignores_header_when_disabled():
    data = base_config()
    data["retry"]["respect_retry_after"] = False
    clock, store, dispatcher, delivery_id = _dispatch([httpx.Response(429, headers={"Retry-After": "42"})], data)
    asyncio.run(dispatcher.run_once())
    job = store.jobs_for(delivery_id)[0]
    assert job.next_attempt_at == clock() + 10
    assert job.last_error == "HTTP 429"


def test_header_does_not_extend_attempts():
    data = base_config()
    data["retry"]["max_attempts"] = 1
    _, store, dispatcher, delivery_id = _dispatch([httpx.Response(429, headers={"Retry-After": "5"})], data)
    asyncio.run(dispatcher.run_once())
    assert store.jobs_for(delivery_id)[0].status == "dead"


def test_config_respect_retry_after_inherits_and_validates():
    data = base_config()
    data["retry"]["respect_retry_after"] = False
    data["routes"]["gh"]["targets"][0]["retry"] = {"respect_retry_after": True}
    config = make_config(data)
    assert config.routes["gh"].targets[0].retry.respect_retry_after is True
    assert make_config().routes["gh"].targets[0].retry.respect_retry_after is True

    data["retry"]["respect_retry_after"] = "yes"
    with pytest.raises(ConfigError, match="respect_retry_after"):
        make_config(data)
