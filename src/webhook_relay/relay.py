"""Fan-out of stored deliveries to targets, with retries and exponential backoff."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import Callable
from email.utils import parsedate_to_datetime

import httpx

from webhook_relay._version import __version__
from webhook_relay.config import Config, RetryPolicy, Target
from webhook_relay.signatures import PROVIDERS, sign_body
from webhook_relay.store import Job, Store

log = logging.getLogger("webhook_relay.relay")

RETRYABLE_4XX = frozenset({408, 409, 425, 429})
# Responses whose ``Retry-After`` header tells us when to come back.
RETRY_AFTER_CODES = frozenset({429, 503})


def parse_retry_after(value: str | None, now: float) -> float | None:
    """Seconds to wait according to a ``Retry-After`` header, or ``None`` if absent or invalid.

    Both forms from RFC 9110 are accepted: a number of seconds (``120``) and an
    HTTP date (``Wed, 21 Oct 2026 07:28:00 GMT``). Dates in the past give ``0``.
    """
    if value is None:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when is None or when.tzinfo is None:
        return None
    return max(0.0, when.timestamp() - now)


def retry_delay(
    attempt: int,
    policy: RetryPolicy,
    status_code: int | None,
    retry_after: str | None,
    now: float,
    rand: Callable[[], float] = random.random,
) -> tuple[float, bool]:
    """Delay before the next attempt and whether it came from the target's ``Retry-After``.

    For ``429`` and ``503`` responses with a valid ``Retry-After`` (and
    ``respect_retry_after`` on) the target's value is used, capped at
    ``max_backoff`` so a misbehaving target cannot park a job forever.
    Otherwise the usual exponential backoff applies.
    """
    if policy.respect_retry_after and status_code in RETRY_AFTER_CODES:
        seconds = parse_retry_after(retry_after, now)
        if seconds is not None:
            return min(seconds, policy.max_backoff), True
    return backoff_delay(attempt, policy, rand), False


def backoff_delay(
    attempt: int,
    policy: RetryPolicy,
    rand: Callable[[], float] = random.random,
) -> float:
    """Delay before retry number ``attempt`` (1 = first retry).

    ``initial_backoff * multiplier ** (attempt - 1)``, capped at ``max_backoff``,
    then spread by up to +/- ``jitter`` (a fraction) so retries do not arrive in bursts.
    """
    delay = min(policy.initial_backoff * policy.multiplier ** (attempt - 1), policy.max_backoff)
    if policy.jitter:
        delay *= 1 + policy.jitter * (2 * rand() - 1)
    return max(0.0, delay)


def is_retryable(status_code: int | None, policy: RetryPolicy) -> bool:
    """Network errors, 5xx and a few transient 4xx codes are retried."""
    if status_code is None or status_code >= 500:
        return True
    if 400 <= status_code < 500:
        return policy.retry_on_4xx or status_code in RETRYABLE_4XX
    return True  # 1xx/3xx: the target did not accept it, try again later


class Dispatcher:
    """Sends due jobs. ``run_once`` is used by the worker loop, the CLI and tests."""

    def __init__(
        self,
        config: Config,
        store: Store,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self.config = config
        self.store = store
        self.clock = clock
        self.rand = rand
        self._client = client
        self._own_client = client is None
        self._wake = asyncio.Event()
        self._stopping = False

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(follow_redirects=False)
        return self._client

    async def aclose(self) -> None:
        if self._own_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def wake(self) -> None:
        self._wake.set()

    def _target(self, job: Job) -> tuple[str, Target] | None:
        delivery = self.store.get_delivery(job.delivery_id)
        if delivery is None:
            return None
        route = self.config.routes.get(delivery.route)
        if route is None:
            return None
        for target in route.targets:
            if target.name == job.target:
                return route.provider, target
        return None

    def build_request(self, job: Job, target: Target, provider: str) -> tuple[dict[str, str], bytes]:
        delivery = self.store.get_delivery(job.delivery_id)
        assert delivery is not None
        wanted = {"content-type", *PROVIDERS[provider].forward_headers, *target.forward_headers}
        headers = {k: v for k, v in delivery.headers.items() if k in wanted}
        headers["user-agent"] = f"webhook-relay/{__version__}"
        headers["x-webhook-relay-delivery"] = delivery.id
        headers["x-webhook-relay-route"] = delivery.route
        headers["x-webhook-relay-attempt"] = str(job.attempts + 1)
        if target.sign_secret:
            headers["x-webhook-relay-signature"] = sign_body(target.sign_secret, delivery.body)
        headers.update({k.lower(): v for k, v in target.headers.items()})
        return headers, delivery.body

    async def deliver(self, job: Job) -> str:
        """Attempt one job. Returns the new job status."""
        found = self._target(job)
        now = self.clock()
        if found is None:
            self.store.record_failure(job.id, None, "route or target no longer in the configuration", now, None)
            log.warning(
                "delivery %s target %s: not configured any more, moved to dead letters", job.delivery_id, job.target
            )
            return "dead"
        provider, target = found
        headers, body = self.build_request(job, target, provider)
        status_code: int | None = None
        retry_after: str | None = None
        try:
            response = await self.client.post(target.url, content=body, headers=headers, timeout=target.retry.timeout)
            status_code = response.status_code
            if 200 <= status_code < 300:
                self.store.record_success(job.id, status_code, self.clock())
                log.info("delivery %s -> %s: %s", job.delivery_id, target.name, status_code)
                return "succeeded"
            error = f"HTTP {status_code}"
            retry_after = response.headers.get("retry-after")
        except httpx.HTTPError as exc:
            error = f"{type(exc).__name__}: {exc}"[:200] or type(exc).__name__
        attempts = job.attempts + 1
        now = self.clock()
        if attempts < target.retry.max_attempts and is_retryable(status_code, target.retry):
            delay, from_header = retry_delay(attempts, target.retry, status_code, retry_after, now, self.rand)
            if from_header:
                error = f"{error} (Retry-After {delay:g}s)"
            self.store.record_failure(job.id, status_code, error, now, now + delay)
            log.info("delivery %s -> %s: %s, retry %d in %.1fs", job.delivery_id, target.name, error, attempts, delay)
            return "pending"
        self.store.record_failure(job.id, status_code, error, now, None)
        log.warning(
            "delivery %s -> %s: %s after %d attempt(s), moved to dead letters",
            job.delivery_id,
            target.name,
            error,
            attempts,
        )
        return "dead"

    async def run_once(self) -> int:
        """Deliver every job that is due now. Returns the number of attempts made."""
        jobs = self.store.due_jobs(self.clock(), limit=100)
        if not jobs:
            return 0
        semaphore = asyncio.Semaphore(self.config.server.concurrency)

        async def one(job: Job) -> None:
            async with semaphore:
                await self.deliver(job)

        await asyncio.gather(*(one(j) for j in jobs))
        return len(jobs)

    async def run_forever(self) -> None:
        poll = self.config.server.poll_interval
        while not self._stopping:
            try:
                await self.run_once()
            except Exception:  # keep the worker alive; the job stays pending
                log.exception("dispatcher iteration failed")
            next_due = self.store.next_due_at()
            wait = poll if next_due is None else min(poll, max(0.0, next_due - self.clock()))
            self._wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=max(wait, 0.01))

    def stop(self) -> None:
        self._stopping = True
        self._wake.set()
