"""Command line interface: ``webhook-relay serve | check-config | list | show | dead-letters | replay``."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from collections.abc import Sequence

from webhook_relay._version import __version__
from webhook_relay.config import Config, ConfigError, load_config
from webhook_relay.store import Store

DEFAULT_CONFIG = "config.yaml"


def _config_path(args: argparse.Namespace) -> str:
    return args.config or os.environ.get("WEBHOOK_RELAY_CONFIG") or DEFAULT_CONFIG


def _load(args: argparse.Namespace) -> Config:
    return load_config(_config_path(args))


def _print(data: object) -> None:
    print(json.dumps(data, indent=2, sort_keys=True, default=str))


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from webhook_relay.app import create_app

    config = _load(args)
    app = create_app(config)
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        access_log=False,
        server_header=False,
        proxy_headers=args.proxy_headers,
    )
    return 0


def cmd_check_config(args: argparse.Namespace) -> int:
    config = _load(args)
    for route in config.routes.values():
        targets = ", ".join(t.name for t in route.targets)
        print(f"route {route.name}: provider={route.provider} targets=[{targets}]")
    api = "enabled" if config.server.api_token else "disabled (no API token set)"
    print(f"database: {config.server.database}  max body: {config.server.max_body_bytes} bytes  API: {api}")
    print("config OK")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    store = Store(_load(args).server.database)
    _print(store.list_deliveries(args.route, args.status, args.limit, 0))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    store = Store(_load(args).server.database)
    delivery = store.get_delivery(args.id)
    if delivery is None:
        print(f"delivery {args.id} not found", file=sys.stderr)
        return 1
    data = delivery.summary()
    data["headers"] = delivery.headers
    data["jobs"] = [j.to_dict() for j in store.jobs_for(delivery.id)]
    _print(data)
    return 0


def cmd_dead_letters(args: argparse.Namespace) -> int:
    store = Store(_load(args).server.database)
    _print(store.dead_letters(args.route, args.limit, 0))
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    config = _load(args)
    store = Store(config.server.database)
    now = time.time()
    if args.all_dead:
        queued = store.replay_dead(now, args.route)
    else:
        if not args.id:
            print("give a delivery id or --all-dead", file=sys.stderr)
            return 2
        if store.get_delivery(args.id) is None:
            print(f"delivery {args.id} not found", file=sys.stderr)
            return 1
        queued = store.replay(args.id, now, args.target)
    print(f"queued {queued} job(s)")
    if args.deliver_now and queued:
        from webhook_relay.relay import Dispatcher

        async def run() -> int:
            dispatcher = Dispatcher(config, store)
            try:
                return await dispatcher.run_once()
            finally:
                await dispatcher.aclose()

        sent = asyncio.run(run())
        print(f"attempted {sent} job(s); see `webhook-relay dead-letters` for failures")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="webhook-relay", description="Self-hosted webhook inbox and relay.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-c", "--config", help="YAML config file (default: $WEBHOOK_RELAY_CONFIG or config.yaml)")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the HTTP server and the delivery worker")
    serve.add_argument("--host", default=os.environ.get("WEBHOOK_RELAY_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("WEBHOOK_RELAY_PORT", "8080")))
    serve.add_argument(
        "--log-level",
        default=os.environ.get("WEBHOOK_RELAY_LOG_LEVEL", "info"),
        choices=["critical", "error", "warning", "info", "debug"],
    )
    serve.add_argument("--proxy-headers", action="store_true", help="trust X-Forwarded-* from a reverse proxy")
    serve.set_defaults(func=cmd_serve)

    check = sub.add_parser("check-config", help="validate the config and the referenced environment variables")
    check.set_defaults(func=cmd_check_config)

    lst = sub.add_parser("list", help="list recent deliveries")
    lst.add_argument("--route")
    lst.add_argument("--status", choices=["pending", "delivered", "dead"])
    lst.add_argument("--limit", type=int, default=20)
    lst.set_defaults(func=cmd_list)

    show = sub.add_parser("show", help="show one delivery and its per-target jobs")
    show.add_argument("id")
    show.set_defaults(func=cmd_show)

    dead = sub.add_parser("dead-letters", help="list jobs that ran out of attempts")
    dead.add_argument("--route")
    dead.add_argument("--limit", type=int, default=50)
    dead.set_defaults(func=cmd_dead_letters)

    replay = sub.add_parser("replay", help="queue a delivery (or every dead letter) again")
    replay.add_argument("id", nargs="?")
    replay.add_argument("--target", help="only this target name")
    replay.add_argument("--all-dead", action="store_true", help="replay every dead-letter job")
    replay.add_argument("--route", help="with --all-dead: only this route")
    replay.add_argument(
        "--deliver-now",
        action="store_true",
        help="send immediately from this process instead of waiting for the server worker",
    )
    replay.set_defaults(func=cmd_replay)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs full target URLs, which may carry tokens in the query string.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
