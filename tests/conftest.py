from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx
import pytest
from starlette.testclient import TestClient

from webhook_relay.app import create_app
from webhook_relay.config import Config, parse_config
from webhook_relay.store import Store

# Obviously fake values used only by the tests.
ENV = {
    "GH_SECRET": "test-github-secret",
    "STRIPE_SECRET": "whsec_test_not_real",
    "SHOPIFY_SECRET": "test-shopify-secret",
    "SLACK_SECRET": "test-slack-secret",
    "HMAC_SECRET": "test-hmac-secret",
    "TARGET_SIGN": "test-target-signing",
    "WEBHOOK_RELAY_API_TOKEN": "test-api-token",
}

AUTH = {"Authorization": "Bearer test-api-token"}


class Clock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Recorder:
    """httpx MockTransport handler that records requests and replies from a script."""

    def __init__(self, responses: list[int | Exception] | None = None, default: int = 200) -> None:
        self.responses = list(responses or [])
        self.default = default
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        result = self.responses.pop(0) if self.responses else self.default
        if isinstance(result, Exception):
            raise result
        return httpx.Response(result, request=request)


def base_config(**route_overrides) -> dict:
    route = {
        "provider": "github",
        "secret_env": "GH_SECRET",
        "targets": [{"url": "http://ci.internal/hook", "name": "ci"}],
    }
    route.update(route_overrides)
    return {
        "server": {"database": ":memory:"},
        "retry": {"max_attempts": 3, "initial_backoff": 10, "multiplier": 2, "max_backoff": 100, "jitter": 0},
        "routes": {"gh": route},
    }


def make_config(data: dict | None = None, env: dict | None = None) -> Config:
    return parse_config(data or base_config(), {**ENV, **(env or {})})


class Harness:
    def __init__(self, config: Config, recorder: Recorder, clock: Clock) -> None:
        self.config = config
        self.recorder = recorder
        self.clock = clock
        self.store = Store(":memory:")
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
        self.app = create_app(config, self.store, client=self.http, clock=clock, run_worker=False)
        self.client = TestClient(self.app)
        self.dispatcher = self.app.state.dispatcher

    def run_once(self) -> int:
        return asyncio.run(self.dispatcher.run_once())


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def harness(clock: Clock) -> Callable[..., Harness]:
    def build(config: Config | None = None, recorder: Recorder | None = None) -> Harness:
        return Harness(config or make_config(), recorder or Recorder(), clock)

    return build
