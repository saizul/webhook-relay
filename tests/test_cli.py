import json

import pytest
import yaml

from conftest import ENV, base_config
from webhook_relay import __version__
from webhook_relay.cli import main
from webhook_relay.store import Store


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    data = base_config()
    data["server"]["database"] = str(tmp_path / "relay.db")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path, data["server"]["database"]


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_check_config(cfg, capsys):
    path, _ = cfg
    assert main(["-c", str(path), "check-config"]) == 0
    out = capsys.readouterr().out
    assert "config OK" in out and "route gh: provider=github" in out
    assert ENV["GH_SECRET"] not in out


def test_check_config_error(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("GH_SECRET", raising=False)
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump(base_config()), encoding="utf-8")
    assert main(["-c", str(path), "check-config"]) == 2
    assert "GH_SECRET is not set" in capsys.readouterr().err


def test_list_show_dead_letters_and_replay(cfg, capsys, monkeypatch):
    path, db = cfg
    store = Store(db)
    delivery_id = store.add_delivery(
        "gh", {"content-type": "text/plain"}, b"hi", [("ci", "http://ci.internal/hook")], 1.0
    )
    job = store.jobs_for(delivery_id)[0]
    store.record_failure(job.id, 500, "HTTP 500", 2.0, None)

    monkeypatch.setenv("WEBHOOK_RELAY_CONFIG", str(path))
    assert main(["list"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["status"] == "dead"
    assert main(["show", delivery_id]) == 0
    assert json.loads(capsys.readouterr().out)["jobs"][0]["last_error"] == "HTTP 500"
    assert main(["show", "missing"]) == 1
    capsys.readouterr()
    assert main(["dead-letters"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 1

    assert main(["replay", delivery_id]) == 0
    assert "queued 1 job(s)" in capsys.readouterr().out
    assert store.jobs_for(delivery_id)[0].status == "pending"
    assert main(["replay", "missing"]) == 1
    assert main(["replay"]) == 2


def test_replay_all_dead(cfg, capsys):
    path, db = cfg
    store = Store(db)
    ids = [store.add_delivery("gh", {}, b"x", [("ci", "http://ci.internal/hook")], 1.0) for _ in range(3)]
    for delivery_id in ids[:2]:
        store.record_failure(store.jobs_for(delivery_id)[0].id, 503, "HTTP 503", 2.0, None)
    assert main(["-c", str(path), "replay", "--all-dead"]) == 0
    assert "queued 2 job(s)" in capsys.readouterr().out
    assert store.counts() == {"pending": 3, "succeeded": 0, "dead": 0}
