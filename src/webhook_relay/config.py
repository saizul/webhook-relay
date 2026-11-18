"""Load and validate the YAML configuration.

Secrets are never written in the config file itself: a route or target names an
environment variable (``secret_env``) and the value is read from the environment
when the config is loaded. Secret values are excluded from ``repr`` so they do
not end up in logs or tracebacks.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from webhook_relay.signatures import PROVIDERS

ROUTE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ConfigError(ValueError):
    """The configuration file is missing, malformed or refers to unset secrets."""


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 5
    initial_backoff: float = 2.0
    multiplier: float = 2.0
    max_backoff: float = 600.0
    jitter: float = 0.1
    timeout: float = 10.0
    retry_on_4xx: bool = False


@dataclass(frozen=True)
class Target:
    url: str
    name: str
    headers: dict[str, str] = field(default_factory=dict)
    forward_headers: tuple[str, ...] = ()
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    sign_secret: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Route:
    name: str
    provider: str
    targets: tuple[Target, ...]
    secret: str | None = field(default=None, repr=False)
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ServerSettings:
    database: str = "webhook-relay.db"
    max_body_bytes: int = 1024 * 1024
    api_token: str | None = field(default=None, repr=False)
    poll_interval: float = 1.0
    concurrency: int = 4


@dataclass(frozen=True)
class Config:
    server: ServerSettings
    routes: dict[str, Route]


def _expect(value: Any, kind: type | tuple[type, ...], where: str) -> Any:
    if not isinstance(value, kind) or (isinstance(value, bool) and bool not in _as_tuple(kind)):
        names = "/".join(k.__name__ for k in _as_tuple(kind))
        raise ConfigError(f"{where}: expected {names}, got {type(value).__name__}")
    return value


def _as_tuple(kind: type | tuple[type, ...]) -> tuple[type, ...]:
    return kind if isinstance(kind, tuple) else (kind,)


def _check_keys(data: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(unknown)} (allowed: {', '.join(sorted(allowed))})")


def _secret(env_name: Any, where: str, env: Mapping[str, str]) -> str:
    _expect(env_name, str, where)
    if not ENV_NAME.match(env_name):
        raise ConfigError(f"{where}: {env_name!r} is not a valid environment variable name")
    value = env.get(env_name)
    if not value:
        raise ConfigError(f"{where}: environment variable {env_name} is not set or empty")
    return value


def _number(data: Mapping[str, Any], key: str, default: float, where: str, *, minimum: float = 0) -> float:
    value = data.get(key, default)
    _expect(value, (int, float), f"{where}.{key}")
    if value < minimum:
        raise ConfigError(f"{where}.{key}: must be >= {minimum}")
    return float(value)


def _retry(data: Any, base: RetryPolicy, where: str) -> RetryPolicy:
    if data is None:
        return base
    _expect(data, dict, where)
    _check_keys(data, set(RetryPolicy.__dataclass_fields__), where)
    max_attempts = data.get("max_attempts", base.max_attempts)
    _expect(max_attempts, int, f"{where}.max_attempts")
    if max_attempts < 1:
        raise ConfigError(f"{where}.max_attempts: must be >= 1")
    jitter = _number(data, "jitter", base.jitter, where)
    if jitter > 1:
        raise ConfigError(f"{where}.jitter: must be between 0 and 1")
    retry_on_4xx = data.get("retry_on_4xx", base.retry_on_4xx)
    _expect(retry_on_4xx, bool, f"{where}.retry_on_4xx")
    policy = RetryPolicy(
        max_attempts=max_attempts,
        initial_backoff=_number(data, "initial_backoff", base.initial_backoff, where),
        multiplier=_number(data, "multiplier", base.multiplier, where, minimum=1),
        max_backoff=_number(data, "max_backoff", base.max_backoff, where),
        jitter=jitter,
        timeout=_number(data, "timeout", base.timeout, where, minimum=0.1),
        retry_on_4xx=retry_on_4xx,
    )
    if policy.max_backoff < policy.initial_backoff:
        raise ConfigError(f"{where}: max_backoff must be >= initial_backoff")
    return policy


def _headers(data: Any, where: str) -> dict[str, str]:
    if data is None:
        return {}
    _expect(data, dict, where)
    out: dict[str, str] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not HEADER_NAME.match(key):
            raise ConfigError(f"{where}: invalid header name {key!r}")
        _expect(value, (str, int), f"{where}.{key}")
        if "\n" in str(value) or "\r" in str(value):
            raise ConfigError(f"{where}.{key}: header values must not contain newlines")
        out[key] = str(value)
    return out


def _target(data: Any, index: int, route: str, base: RetryPolicy, env: Mapping[str, str]) -> Target:
    where = f"routes.{route}.targets[{index}]"
    if isinstance(data, str):
        data = {"url": data}
    _expect(data, dict, where)
    _check_keys(data, {"url", "name", "headers", "forward_headers", "retry", "sign_secret_env"}, where)
    url = _expect(data.get("url"), str, f"{where}.url")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError(f"{where}.url: must be an absolute http(s) URL, got {url!r}")
    forward = data.get("forward_headers", [])
    _expect(forward, list, f"{where}.forward_headers")
    for name in forward:
        if not isinstance(name, str) or not HEADER_NAME.match(name):
            raise ConfigError(f"{where}.forward_headers: invalid header name {name!r}")
    name = data.get("name", f"{route}-{index}")
    _expect(name, str, f"{where}.name")
    sign_env = data.get("sign_secret_env")
    return Target(
        url=url,
        name=name,
        headers=_headers(data.get("headers"), f"{where}.headers"),
        forward_headers=tuple(h.lower() for h in forward),
        retry=_retry(data.get("retry"), base, f"{where}.retry"),
        sign_secret=_secret(sign_env, f"{where}.sign_secret_env", env) if sign_env is not None else None,
    )


def _route(name: Any, data: Any, base: RetryPolicy, env: Mapping[str, str]) -> Route:
    if not isinstance(name, str) or not ROUTE_NAME.match(name):
        raise ConfigError(f"routes: invalid route name {name!r} (letters, digits, '.', '_', '-'; max 64)")
    where = f"routes.{name}"
    _expect(data, dict, where)
    _check_keys(data, {"provider", "secret_env", "targets", "retry", "options"}, where)
    provider = data.get("provider")
    if provider not in PROVIDERS:
        raise ConfigError(f"{where}.provider: {provider!r} is not one of {', '.join(sorted(PROVIDERS))}")
    verifier = PROVIDERS[provider]
    options = data.get("options") or {}
    _expect(options, dict, f"{where}.options")
    try:
        options = verifier.validate_options(options)
    except ValueError as exc:
        raise ConfigError(f"{where}.options: {exc}") from None
    secret = None
    if verifier.needs_secret:
        if "secret_env" not in data:
            raise ConfigError(f"{where}.secret_env: required for provider {provider!r}")
        secret = _secret(data["secret_env"], f"{where}.secret_env", env)
    elif "secret_env" in data:
        raise ConfigError(f"{where}.secret_env: provider {provider!r} does not use a secret")
    retry = _retry(data.get("retry"), base, f"{where}.retry")
    targets = data.get("targets")
    _expect(targets, list, f"{where}.targets")
    if not targets:
        raise ConfigError(f"{where}.targets: at least one target is required")
    parsed = tuple(_target(t, i, name, retry, env) for i, t in enumerate(targets))
    names = [t.name for t in parsed]
    if len(names) != len(set(names)):
        raise ConfigError(f"{where}.targets: target names must be unique")
    return Route(name=name, provider=provider, targets=parsed, secret=secret, options=options)


def _server(data: Any, env: Mapping[str, str]) -> ServerSettings:
    if data is None:
        data = {}
    _expect(data, dict, "server")
    _check_keys(data, {"database", "max_body_bytes", "api_token_env", "poll_interval", "concurrency"}, "server")
    database = env.get("WEBHOOK_RELAY_DATABASE") or data.get("database", ServerSettings.database)
    _expect(database, str, "server.database")
    max_body = data.get("max_body_bytes", ServerSettings.max_body_bytes)
    _expect(max_body, int, "server.max_body_bytes")
    if not 1 <= max_body <= 100 * 1024 * 1024:
        raise ConfigError("server.max_body_bytes: must be between 1 and 104857600")
    concurrency = data.get("concurrency", ServerSettings.concurrency)
    _expect(concurrency, int, "server.concurrency")
    if not 1 <= concurrency <= 64:
        raise ConfigError("server.concurrency: must be between 1 and 64")
    token_env = data.get("api_token_env", "WEBHOOK_RELAY_API_TOKEN")
    _expect(token_env, str, "server.api_token_env")
    return ServerSettings(
        database=database,
        max_body_bytes=max_body,
        api_token=env.get(token_env) or None,
        poll_interval=_number(data, "poll_interval", ServerSettings.poll_interval, "server", minimum=0.05),
        concurrency=concurrency,
    )


def parse_config(data: Any, env: Mapping[str, str] | None = None) -> Config:
    """Validate an already-parsed YAML document."""
    env = os.environ if env is None else env
    if data is None:
        data = {}
    _expect(data, dict, "config")
    _check_keys(data, {"server", "retry", "routes"}, "config")
    base = _retry(data.get("retry"), RetryPolicy(), "retry")
    routes_data = data.get("routes")
    _expect(routes_data, dict, "routes")
    if not routes_data:
        raise ConfigError("routes: define at least one route")
    routes = {name: _route(name, value, base, env) for name, value in routes_data.items()}
    return Config(server=_server(data.get("server"), env), routes=routes)


def load_config(path: str | os.PathLike[str], env: Mapping[str, str] | None = None) -> Config:
    """Read and validate a YAML config file."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config file {p}: {exc.strerror}") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{p}: invalid YAML: {exc}") from None
    return parse_config(data, env)
