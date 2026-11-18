"""The ASGI application: webhook intake, health check and the JSON API."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import json
import logging
import time
from collections.abc import AsyncIterator, Callable

import httpx
from starlette.applications import Starlette
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route as HttpRoute

from webhook_relay._version import __version__
from webhook_relay.config import Config
from webhook_relay.relay import Dispatcher
from webhook_relay.signatures import PROVIDERS, SignatureError
from webhook_relay.store import Store, filter_headers

log = logging.getLogger("webhook_relay.app")

MAX_PAGE = 500


class BodyTooLarge(Exception):
    pass


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def read_limited(request: Request, limit: int) -> bytes:
    """Read the request body, refusing anything over ``limit`` bytes without buffering it."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                raise BodyTooLarge
        except ValueError:
            raise BodyTooLarge from None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise BodyTooLarge
        chunks.append(chunk)
    return b"".join(chunks)


def _int_param(request: Request, name: str, default: int, maximum: int) -> int:
    raw = request.query_params.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None
    if value < 0:
        raise ValueError(f"{name} must be >= 0")
    return min(value, maximum)


def create_app(
    config: Config,
    store: Store | None = None,
    *,
    client: httpx.AsyncClient | None = None,
    clock: Callable[[], float] = time.time,
    run_worker: bool = True,
) -> Starlette:
    """Build the application. Tests pass ``run_worker=False`` and drive the dispatcher themselves."""
    store = store or Store(config.server.database)
    dispatcher = Dispatcher(config, store, client=client, clock=clock)

    def authorized(request: Request) -> Response | None:
        token = config.server.api_token
        if not token:
            return _error(403, "API disabled: set the WEBHOOK_RELAY_API_TOKEN environment variable to enable it")
        given = request.headers.get("authorization", "")
        scheme, _, value = given.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(value.strip().encode(), token.encode()):
            return JSONResponse({"error": "unauthorized"}, status_code=401, headers={"www-authenticate": "Bearer"})
        return None

    async def receive_hook(request: Request) -> Response:
        name = request.path_params["route"]
        route = config.routes.get(name)
        if route is None:
            return _error(404, "unknown route")
        try:
            body = await read_limited(request, config.server.max_body_bytes)
        except BodyTooLarge:
            log.info("route %s: rejected body over %d bytes", name, config.server.max_body_bytes)
            return _error(413, f"body larger than {config.server.max_body_bytes} bytes")
        except ClientDisconnect:
            return Response(status_code=400)
        headers = {k.lower(): v for k, v in request.headers.items()}
        try:
            PROVIDERS[route.provider].verify(body, headers, route.secret, route.options, now=clock())
        except SignatureError as exc:
            log.info("route %s: signature rejected (%s)", name, exc)
            return _error(401, f"invalid signature: {exc}")

        # Slack sends a one-time URL verification challenge when an events URL is configured.
        if route.provider == "slack" and headers.get("content-type", "").startswith("application/json"):
            with contextlib.suppress(ValueError, AttributeError):
                payload = json.loads(body)
                if isinstance(payload, dict) and payload.get("type") == "url_verification":
                    return JSONResponse({"challenge": str(payload.get("challenge", ""))})

        delivery_id = store.add_delivery(
            name,
            filter_headers(request.headers.items()),
            body,
            [(t.name, t.url) for t in route.targets],
            clock(),
        )
        log.info("route %s: accepted delivery %s (%d bytes)", name, delivery_id, len(body))
        dispatcher.wake()
        return JSONResponse({"id": delivery_id, "status": "accepted"}, status_code=202)

    async def health(request: Request) -> Response:
        try:
            ok = store.ping()
        except Exception:
            ok = False
        payload = {"status": "ok" if ok else "error", "version": __version__, "routes": len(config.routes)}
        if ok:
            payload["jobs"] = store.counts()
        return JSONResponse(payload, status_code=200 if ok else 503)

    async def list_deliveries(request: Request) -> Response:
        if (denied := authorized(request)) is not None:
            return denied
        status = request.query_params.get("status")
        if status not in (None, "pending", "delivered", "dead"):
            return _error(400, "status must be pending, delivered or dead")
        try:
            limit = _int_param(request, "limit", 50, MAX_PAGE)
            offset = _int_param(request, "offset", 0, 10**9)
        except ValueError as exc:
            return _error(400, str(exc))
        items = store.list_deliveries(request.query_params.get("route"), status, limit, offset)
        return JSONResponse({"items": items, "limit": limit, "offset": offset})

    async def get_delivery(request: Request) -> Response:
        if (denied := authorized(request)) is not None:
            return denied
        delivery = store.get_delivery(request.path_params["id"])
        if delivery is None:
            return _error(404, "delivery not found")
        data = delivery.summary()
        data["headers"] = delivery.headers
        data["jobs"] = [j.to_dict() for j in store.jobs_for(delivery.id)]
        if request.query_params.get("include_body") in {"1", "true", "yes"}:
            try:
                data["body"] = delivery.body.decode("utf-8")
                data["body_encoding"] = "utf-8"
            except UnicodeDecodeError:
                data["body"] = base64.b64encode(delivery.body).decode()
                data["body_encoding"] = "base64"
        return JSONResponse(data)

    async def dead_letters(request: Request) -> Response:
        if (denied := authorized(request)) is not None:
            return denied
        try:
            limit = _int_param(request, "limit", 50, MAX_PAGE)
            offset = _int_param(request, "offset", 0, 10**9)
        except ValueError as exc:
            return _error(400, str(exc))
        items = store.dead_letters(request.query_params.get("route"), limit, offset)
        return JSONResponse({"items": items, "limit": limit, "offset": offset})

    async def replay(request: Request) -> Response:
        if (denied := authorized(request)) is not None:
            return denied
        delivery = store.get_delivery(request.path_params["id"])
        if delivery is None:
            return _error(404, "delivery not found")
        target = request.query_params.get("target")
        if target is not None and target not in {j.target for j in store.jobs_for(delivery.id)}:
            return _error(404, "target not found for this delivery")
        queued = store.replay(delivery.id, clock(), target)
        log.info("delivery %s: replay requested, %d job(s) queued", delivery.id, queued)
        dispatcher.wake()
        return JSONResponse({"id": delivery.id, "queued": queued}, status_code=202)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        task = asyncio.create_task(dispatcher.run_forever()) if run_worker else None
        try:
            yield
        finally:
            dispatcher.stop()
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=5)
            await dispatcher.aclose()

    app = Starlette(
        routes=[
            HttpRoute("/hooks/{route}", receive_hook, methods=["POST"]),
            HttpRoute("/healthz", health, methods=["GET"]),
            HttpRoute("/api/deliveries", list_deliveries, methods=["GET"]),
            HttpRoute("/api/deliveries/{id}", get_delivery, methods=["GET"]),
            HttpRoute("/api/deliveries/{id}/replay", replay, methods=["POST"]),
            HttpRoute("/api/dead-letters", dead_letters, methods=["GET"]),
        ],
        lifespan=lifespan,
    )
    app.state.store = store
    app.state.dispatcher = dispatcher
    app.state.config = config
    return app
