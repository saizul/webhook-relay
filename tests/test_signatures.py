import base64
import hashlib
import hmac

import pytest

from webhook_relay.signatures import PROVIDERS, GenericHmac, GitHub, Shopify, SignatureError, Slack, Stripe, sign_body

BODY = b'{"event":"ping","id":42}'
NOW = 1_700_000_000


def hexmac(secret: str, msg: bytes, algo=hashlib.sha256) -> str:
    return hmac.new(secret.encode(), msg, algo).hexdigest()


# --- GitHub -------------------------------------------------------------------


def test_github_published_vector():
    # Example from GitHub's "Validating webhook deliveries" documentation.
    headers = {"x-hub-signature-256": "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"}
    GitHub.verify(b"Hello, World!", headers, "It's a Secret to Everybody", {})


def test_github_valid_and_uppercase_hex():
    sig = "sha256=" + hexmac("s3", BODY)
    GitHub.verify(BODY, {"x-hub-signature-256": sig}, "s3", {})
    GitHub.verify(BODY, {"x-hub-signature-256": "sha256=" + hexmac("s3", BODY).upper()}, "s3", {})


@pytest.mark.parametrize(
    ("headers", "reason"),
    [
        ({}, "missing"),
        ({"x-hub-signature-256": "sha1=abc"}, "sha256="),
        ({"x-hub-signature-256": "sha256=" + "0" * 64}, "mismatch"),
    ],
)
def test_github_rejects(headers, reason):
    with pytest.raises(SignatureError, match=reason):
        GitHub.verify(BODY, headers, "s3", {})


def test_github_rejects_tampered_body():
    sig = "sha256=" + hexmac("s3", BODY)
    with pytest.raises(SignatureError):
        GitHub.verify(BODY + b" ", {"x-hub-signature-256": sig}, "s3", {})


# --- Stripe -------------------------------------------------------------------


def stripe_header(secret: str, body: bytes, ts: int, extra: str = "") -> str:
    v1 = hexmac(secret, f"{ts}.".encode() + body)
    return f"t={ts},v1={v1}{extra}"


def test_stripe_known_vector():
    # Independently computed: HMAC-SHA256("whsec_test", "1700000000.{}")
    expected = hmac.new(b"whsec_test", b"1700000000.{}", hashlib.sha256).hexdigest()
    Stripe.verify(b"{}", {"stripe-signature": f"t=1700000000,v1={expected}"}, "whsec_test", {"tolerance": 300}, NOW)


def test_stripe_valid_with_multiple_signatures_and_v0():
    good = stripe_header("whsec_x", BODY, NOW)
    header = f"t={NOW},v1={'a' * 64},{good.split(',')[1]},v0={'b' * 64}"
    Stripe.verify(BODY, {"stripe-signature": header}, "whsec_x", {"tolerance": 300}, NOW + 10)


def test_stripe_tolerance():
    header = stripe_header("whsec_x", BODY, NOW)
    Stripe.verify(BODY, {"stripe-signature": header}, "whsec_x", {"tolerance": 300}, NOW + 300)
    with pytest.raises(SignatureError, match="tolerance"):
        Stripe.verify(BODY, {"stripe-signature": header}, "whsec_x", {"tolerance": 300}, NOW + 301)
    with pytest.raises(SignatureError, match="tolerance"):
        Stripe.verify(BODY, {"stripe-signature": header}, "whsec_x", {"tolerance": 300}, NOW - 301)
    # tolerance 0 disables the check
    Stripe.verify(BODY, {"stripe-signature": header}, "whsec_x", {"tolerance": 0}, NOW + 10_000)


@pytest.mark.parametrize(
    ("header", "reason"),
    [
        (f"v1={'a' * 64}", "no timestamp"),
        (f"t={NOW}", "no v1"),
        (f"t=soon,v1={'a' * 64}", "not an integer"),
        (f"t={NOW},v1={'a' * 64}", "mismatch"),
    ],
)
def test_stripe_rejects(header, reason):
    with pytest.raises(SignatureError, match=reason):
        Stripe.verify(BODY, {"stripe-signature": header}, "whsec_x", {"tolerance": 300}, NOW)


def test_stripe_signature_is_bound_to_timestamp():
    header = stripe_header("whsec_x", BODY, NOW).replace(f"t={NOW}", f"t={NOW + 1}")
    with pytest.raises(SignatureError, match="mismatch"):
        Stripe.verify(BODY, {"stripe-signature": header}, "whsec_x", {"tolerance": 300}, NOW)


# --- Shopify ------------------------------------------------------------------


def test_shopify_valid():
    sig = base64.b64encode(hmac.new(b"shp", BODY, hashlib.sha256).digest()).decode()
    Shopify.verify(BODY, {"x-shopify-hmac-sha256": sig}, "shp", {})


def test_shopify_known_vector():
    # base64(HMAC-SHA256(key="key", "The quick brown fox jumps over the lazy dog"))
    sig = "97yD9DBThCSxMpjmqm+xQ+9NWaFJRhdZl0edvC0aPNg="
    Shopify.verify(b"The quick brown fox jumps over the lazy dog", {"x-shopify-hmac-sha256": sig}, "key", {})


@pytest.mark.parametrize("value", ["not base64!!", base64.b64encode(b"x" * 32).decode()])
def test_shopify_rejects(value):
    with pytest.raises(SignatureError):
        Shopify.verify(BODY, {"x-shopify-hmac-sha256": value}, "shp", {})


# --- Slack --------------------------------------------------------------------


def slack_headers(secret: str, body: bytes, ts: int) -> dict:
    return {
        "x-slack-request-timestamp": str(ts),
        "x-slack-signature": "v0=" + hexmac(secret, f"v0:{ts}:".encode() + body),
    }


def test_slack_valid():
    Slack.verify(BODY, slack_headers("slk", BODY, NOW), "slk", {"tolerance": 300}, NOW + 5)


def test_slack_replay_window():
    with pytest.raises(SignatureError, match="tolerance"):
        Slack.verify(BODY, slack_headers("slk", BODY, NOW), "slk", {"tolerance": 300}, NOW + 600)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda h: h.pop("x-slack-request-timestamp"),
        lambda h: h.update({"x-slack-signature": h["x-slack-signature"][3:]}),
        lambda h: h.update({"x-slack-signature": "v0=" + "f" * 64}),
    ],
)
def test_slack_rejects(mutate):
    headers = slack_headers("slk", BODY, NOW)
    mutate(headers)
    with pytest.raises(SignatureError):
        Slack.verify(BODY, headers, "slk", {"tolerance": 300}, NOW)


# --- Generic HMAC -------------------------------------------------------------


@pytest.mark.parametrize("algorithm", ["sha1", "sha256", "sha512"])
def test_generic_hex_algorithms(algorithm):
    opts = GenericHmac.validate_options({"algorithm": algorithm, "header": "X-Sig", "prefix": f"{algorithm}="})
    digest = hexmac("gen", BODY, getattr(hashlib, algorithm))
    GenericHmac.verify(BODY, {"x-sig": f"{algorithm}={digest}"}, "gen", opts)


def test_generic_known_vector_sha256_hex():
    # RFC 4231 test case 2: key "Jefe", data "what do ya want for nothing?"
    expected = "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"
    opts = GenericHmac.validate_options({})
    GenericHmac.verify(b"what do ya want for nothing?", {"x-signature": expected}, "Jefe", opts)


def test_generic_base64_and_prefix_errors():
    opts = GenericHmac.validate_options({"encoding": "base64", "prefix": "hmac "})
    sig = base64.b64encode(hmac.new(b"gen", BODY, hashlib.sha256).digest()).decode()
    GenericHmac.verify(BODY, {"x-signature": "hmac " + sig}, "gen", opts)
    with pytest.raises(SignatureError, match="start with"):
        GenericHmac.verify(BODY, {"x-signature": sig}, "gen", opts)
    with pytest.raises(SignatureError, match="base64"):
        GenericHmac.verify(BODY, {"x-signature": "hmac %%%"}, "gen", opts)


@pytest.mark.parametrize(
    "options",
    [{"algorithm": "md5"}, {"encoding": "base32"}, {"header": ""}, {"prefix": 3}, {"colour": "blue"}],
)
def test_generic_invalid_options(options):
    with pytest.raises(ValueError):
        GenericHmac.validate_options(options)


def test_registry_and_target_signature():
    assert set(PROVIDERS) == {"none", "github", "stripe", "shopify", "slack", "hmac"}
    assert sign_body("k", b"x") == "sha256=" + hexmac("k", b"x")
    PROVIDERS["none"].verify(b"anything", {}, None, {})
