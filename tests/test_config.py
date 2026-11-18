import copy
from pathlib import Path

import pytest

from conftest import ENV, base_config, make_config
from webhook_relay.config import ConfigError, load_config, parse_config

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "config.yaml"


def test_minimal_config_and_defaults():
    config = make_config()
    route = config.routes["gh"]
    assert route.provider == "github"
    assert route.secret == ENV["GH_SECRET"]
    assert route.targets[0].retry.max_attempts == 3  # inherited from top-level retry
    assert config.server.api_token == ENV["WEBHOOK_RELAY_API_TOKEN"]
    assert config.server.max_body_bytes == 1024 * 1024


def test_secrets_are_not_in_repr():
    data = base_config()
    data["routes"]["gh"]["targets"][0]["sign_secret_env"] = "TARGET_SIGN"
    config = make_config(data)
    text = repr(config)
    for value in ("test-github-secret", "test-target-signing", "test-api-token"):
        assert value not in text


def test_retry_override_per_route_and_target():
    data = base_config(retry={"max_attempts": 7})
    data["routes"]["gh"]["targets"].append(
        {"url": "https://b.example.com/x", "name": "b", "retry": {"max_attempts": 2, "timeout": 3}}
    )
    config = make_config(data)
    a, b = config.routes["gh"].targets
    assert a.retry.max_attempts == 7 and a.retry.initial_backoff == 10
    assert b.retry.max_attempts == 2 and b.retry.timeout == 3


def test_target_as_plain_string_and_header_options():
    data = base_config(targets=["http://a.local/x"])
    config = make_config(data)
    assert config.routes["gh"].targets[0].name == "gh-0"


def test_missing_secret_env_value():
    with pytest.raises(ConfigError, match="GH_SECRET is not set"):
        parse_config(base_config(), {})


def test_database_env_override():
    config = make_config(env={"WEBHOOK_RELAY_DATABASE": "/data/x.db"})
    assert config.server.database == "/data/x.db"


def _mutate(path, value):
    data = base_config()
    node = data
    for key in path[:-1]:
        node = node[key]
    if value is KeyError:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return data


@pytest.mark.parametrize(
    ("data", "match"),
    [
        (_mutate(["routes"], {}), "at least one route"),
        (_mutate(["routes", "gh", "provider"], "gitlab"), "not one of"),
        (_mutate(["routes", "gh", "secret_env"], KeyError), "required"),
        (_mutate(["routes", "gh", "secret_env"], "lower-case-bad!"), "not a valid environment"),
        (_mutate(["routes", "gh", "targets"], []), "at least one target"),
        (_mutate(["routes", "gh", "targets"], [{"url": "ftp://x/y"}]), "http"),
        (_mutate(["routes", "gh", "targets"], [{"url": "/relative"}]), "http"),
        (
            _mutate(
                ["routes", "gh", "targets"], [{"url": "http://a/x", "name": "a"}, {"url": "http://b/x", "name": "a"}]
            ),
            "unique",
        ),
        (
            _mutate(["routes", "gh", "targets"], [{"url": "http://a/x", "headers": {"Bad Header": "x"}}]),
            "invalid header",
        ),
        (_mutate(["routes", "gh", "targets"], [{"url": "http://a/x", "headers": {"X": "a\r\nb"}}]), "newlines"),
        (_mutate(["routes", "gh", "targets"], [{"url": "http://a/x", "colour": 1}]), "unknown key"),
        (_mutate(["routes", "gh", "options"], {"tolerance": 5}), "unknown option"),
        (_mutate(["retry", "max_attempts"], 0), ">= 1"),
        (_mutate(["retry", "max_attempts"], True), "expected int"),
        (_mutate(["retry", "multiplier"], 0.5), ">= 1"),
        (_mutate(["retry", "jitter"], 2), "between 0 and 1"),
        (_mutate(["retry", "max_backoff"], 1), "max_backoff"),
        (_mutate(["server", "max_body_bytes"], 0), "max_body_bytes"),
        (_mutate(["server", "concurrency"], 1000), "concurrency"),
        (_mutate(["server", "listen"], "0.0.0.0"), "unknown key"),
        ({"routes": {"bad/name": {}}}, "invalid route name"),
        ({"routes": []}, "expected dict"),
        (
            {"routes": {"x": {"provider": "none", "secret_env": "GH_SECRET", "targets": ["http://a/x"]}}},
            "does not use a secret",
        ),
    ],
)
def test_invalid_configs(data, match):
    with pytest.raises(ConfigError, match=match):
        parse_config(copy.deepcopy(data), ENV)


def test_stripe_tolerance_option_validation():
    data = {
        "routes": {
            "s": {
                "provider": "stripe",
                "secret_env": "STRIPE_SECRET",
                "targets": ["http://a/x"],
                "options": {"tolerance": -1},
            }
        }
    }
    with pytest.raises(ConfigError, match="tolerance"):
        parse_config(data, ENV)
    data["routes"]["s"]["options"]["tolerance"] = 60
    assert parse_config(data, ENV).routes["s"].options == {"tolerance": 60}


def test_load_config_errors(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "missing.yaml", ENV)
    bad = tmp_path / "bad.yaml"
    bad.write_text("routes: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(bad, ENV)


def test_example_config_is_valid():
    env = {
        "GITHUB_WEBHOOK_SECRET": "x",
        "STRIPE_WEBHOOK_SECRET": "x",
        "SHOPIFY_WEBHOOK_SECRET": "x",
        "SLACK_SIGNING_SECRET": "x",
        "PARTNER_WEBHOOK_SECRET": "x",
        "RELAY_SIGNING_SECRET": "x",
    }
    config = load_config(EXAMPLE, env)
    assert {r.provider for r in config.routes.values()} >= {"github", "stripe", "shopify", "slack", "hmac"}
