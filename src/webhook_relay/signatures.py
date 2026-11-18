"""Signature verification for webhook providers.

Every provider is a small class registered in ``PROVIDERS``. ``verify`` raises
``SignatureError`` with a short, secret-free reason when a request is rejected.
All comparisons use ``hmac.compare_digest``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import time
from collections.abc import Mapping
from typing import Any, ClassVar

ALGORITHMS = {"sha1": hashlib.sha1, "sha256": hashlib.sha256, "sha512": hashlib.sha512}


class SignatureError(Exception):
    """The request signature is missing, malformed, wrong or too old."""


def _hmac(secret: str, message: bytes, algorithm: str = "sha256") -> bytes:
    return hmac.new(secret.encode("utf-8"), message, ALGORITHMS[algorithm]).digest()


def _header(headers: Mapping[str, str], name: str) -> str:
    value = headers.get(name.lower())
    if value is None:
        value = headers.get(name)
    if not value:
        raise SignatureError(f"missing {name} header")
    return value.strip()


def _check_tolerance(timestamp: str, now: float, tolerance: int) -> int:
    try:
        ts = int(timestamp)
    except ValueError:
        raise SignatureError("timestamp is not an integer") from None
    if tolerance and abs(now - ts) > tolerance:
        raise SignatureError(f"timestamp outside the {tolerance}s tolerance")
    return ts


class Provider:
    """Base class for a signature scheme."""

    name: ClassVar[str]
    needs_secret: ClassVar[bool] = True
    #: Request headers forwarded to targets in addition to Content-Type.
    forward_headers: ClassVar[tuple[str, ...]] = ()
    #: Options accepted under ``routes.<name>.options`` with their defaults.
    default_options: ClassVar[dict[str, Any]] = {}

    @classmethod
    def validate_options(cls, options: Mapping[str, Any]) -> dict[str, Any]:
        unknown = sorted(set(options) - set(cls.default_options))
        if unknown:
            allowed = ", ".join(sorted(cls.default_options)) or "none"
            raise ValueError(f"unknown option(s) {', '.join(unknown)} (allowed: {allowed})")
        merged = {**cls.default_options, **options}
        if "tolerance" in merged and (
            isinstance(merged["tolerance"], bool) or not isinstance(merged["tolerance"], int) or merged["tolerance"] < 0
        ):
            raise ValueError("tolerance must be a non-negative integer (seconds; 0 disables the check)")
        return merged

    @classmethod
    def verify(
        cls,
        body: bytes,
        headers: Mapping[str, str],
        secret: str | None,
        options: Mapping[str, Any],
        now: float | None = None,
    ) -> None:
        raise NotImplementedError


class NoVerification(Provider):
    """Accept every request. Only for trusted networks and testing."""

    name = "none"
    needs_secret = False

    @classmethod
    def verify(cls, body, headers, secret, options, now=None) -> None:
        return None


class GitHub(Provider):
    """``X-Hub-Signature-256: sha256=<hex HMAC-SHA256 of the body>``."""

    name = "github"
    forward_headers = ("x-github-event", "x-github-delivery", "x-github-hook-id", "x-hub-signature-256")

    @classmethod
    def verify(cls, body, headers, secret, options, now=None) -> None:
        value = _header(headers, "X-Hub-Signature-256")
        if not value.startswith("sha256="):
            raise SignatureError("X-Hub-Signature-256 must start with 'sha256='")
        expected = "sha256=" + _hmac(secret, body).hex()
        if not hmac.compare_digest(expected.encode(), value.lower().encode()):
            raise SignatureError("signature mismatch")


class Stripe(Provider):
    """``Stripe-Signature: t=<unix>,v1=<hex HMAC-SHA256 of "t.body">`` with a timestamp tolerance."""

    name = "stripe"
    forward_headers = ("stripe-signature",)
    default_options: ClassVar[dict[str, Any]] = {"tolerance": 300}

    @classmethod
    def verify(cls, body, headers, secret, options, now=None) -> None:
        value = _header(headers, "Stripe-Signature")
        timestamp: str | None = None
        candidates: list[str] = []
        for part in value.split(","):
            key, sep, item = part.strip().partition("=")
            if not sep:
                continue
            if key == "t":
                timestamp = item
            elif key == "v1":
                candidates.append(item.lower())
        if timestamp is None:
            raise SignatureError("Stripe-Signature has no timestamp")
        if not candidates:
            raise SignatureError("Stripe-Signature has no v1 signature")
        _check_tolerance(timestamp, time.time() if now is None else now, options.get("tolerance", 300))
        expected = _hmac(secret, timestamp.encode() + b"." + body).hex().encode()
        if not any(hmac.compare_digest(expected, c.encode()) for c in candidates):
            raise SignatureError("signature mismatch")


class Shopify(Provider):
    """``X-Shopify-Hmac-Sha256: <base64 HMAC-SHA256 of the body>``."""

    name = "shopify"
    forward_headers = (
        "x-shopify-topic",
        "x-shopify-shop-domain",
        "x-shopify-webhook-id",
        "x-shopify-api-version",
        "x-shopify-hmac-sha256",
    )

    @classmethod
    def verify(cls, body, headers, secret, options, now=None) -> None:
        value = _header(headers, "X-Shopify-Hmac-Sha256")
        try:
            given = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            raise SignatureError("X-Shopify-Hmac-Sha256 is not valid base64") from None
        if not hmac.compare_digest(_hmac(secret, body), given):
            raise SignatureError("signature mismatch")


class Slack(Provider):
    """``X-Slack-Signature: v0=<hex HMAC-SHA256 of "v0:<ts>:<body>">`` plus ``X-Slack-Request-Timestamp``."""

    name = "slack"
    forward_headers = ("x-slack-signature", "x-slack-request-timestamp")
    default_options: ClassVar[dict[str, Any]] = {"tolerance": 300}

    @classmethod
    def verify(cls, body, headers, secret, options, now=None) -> None:
        timestamp = _header(headers, "X-Slack-Request-Timestamp")
        value = _header(headers, "X-Slack-Signature")
        if not value.startswith("v0="):
            raise SignatureError("X-Slack-Signature must start with 'v0='")
        _check_tolerance(timestamp, time.time() if now is None else now, options.get("tolerance", 300))
        expected = "v0=" + _hmac(secret, b"v0:" + timestamp.encode() + b":" + body).hex()
        if not hmac.compare_digest(expected.encode(), value.lower().encode()):
            raise SignatureError("signature mismatch")


class GenericHmac(Provider):
    """A configurable ``<header>: <prefix><hex|base64 HMAC of the body>`` scheme."""

    name = "hmac"
    default_options: ClassVar[dict[str, Any]] = {
        "header": "X-Signature",
        "algorithm": "sha256",
        "encoding": "hex",
        "prefix": "",
    }

    @classmethod
    def validate_options(cls, options):
        merged = super().validate_options(options)
        if not isinstance(merged["header"], str) or not merged["header"]:
            raise ValueError("header must be a non-empty string")
        if merged["algorithm"] not in ALGORITHMS:
            raise ValueError(f"algorithm must be one of {', '.join(ALGORITHMS)}")
        if merged["encoding"] not in {"hex", "base64"}:
            raise ValueError("encoding must be 'hex' or 'base64'")
        if not isinstance(merged["prefix"], str):
            raise ValueError("prefix must be a string")
        return merged

    @classmethod
    def verify(cls, body, headers, secret, options, now=None) -> None:
        opts = {**cls.default_options, **options}
        value = _header(headers, opts["header"])
        prefix = opts["prefix"]
        if prefix:
            if not value.startswith(prefix):
                raise SignatureError(f"{opts['header']} must start with {prefix!r}")
            value = value[len(prefix) :]
        digest = _hmac(secret, body, opts["algorithm"])
        if opts["encoding"] == "hex":
            ok = hmac.compare_digest(digest.hex().encode(), value.lower().encode())
        else:
            try:
                given = base64.b64decode(value, validate=True)
            except (binascii.Error, ValueError):
                raise SignatureError(f"{opts['header']} is not valid base64") from None
            ok = hmac.compare_digest(digest, given)
        if not ok:
            raise SignatureError("signature mismatch")


PROVIDERS: dict[str, type[Provider]] = {
    p.name: p for p in (NoVerification, GitHub, Stripe, Shopify, Slack, GenericHmac)
}


def sign_body(secret: str, body: bytes) -> str:
    """Signature added to forwarded requests when a target sets ``sign_secret_env``."""
    return "sha256=" + _hmac(secret, body).hex()
