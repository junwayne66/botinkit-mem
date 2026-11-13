"""Use an isolated subprocess to verify the official CLI protocol boundary."""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from bmem import feishu


FAKE_CLI = r'''
import json, os, sys, time
from pathlib import Path
state_path = Path(os.environ["FEISHU_TEST_STATE"])
state = json.loads(state_path.read_text())
args = sys.argv[1:]
with open(os.environ["FEISHU_TEST_LOG"], "a") as output:
    output.write(json.dumps({"args": args, "workspace": "HERMES_HOME" in os.environ}) + "\n")
def save():
    state_path.write_text(json.dumps(state))
def emit(data, code=0):
    print(json.dumps(data), flush=True)
    sys.exit(code)
profile = None
if "--profile" in args:
    index = args.index("--profile")
    profile = args[index + 1]
    del args[index:index + 2]
if args == ["--version"]:
    print("lark-cli version 1.0.100")
    sys.exit()
if args == ["profile", "list"]:
    emit(state["profiles"])
if args[:2] == ["config", "init"]:
    assert "--new" in args and "--name" in args and not profile
    brand = args[args.index("--brand") + 1]
    print("https://open.feishu.cn/page/cli?user_code=SETUP", file=sys.stderr, flush=True)
    if state.get("setup_release"):
        while not Path(state["setup_release"]).exists():
            time.sleep(0.01)
    else:
        time.sleep(state.get("setup_delay", 0.4))
    entry = {"name": args[args.index("--name") + 1], "appId": "cli-created", "brand": brand}
    state["profiles"].append(entry)
    save()
    emit({"appId": entry["appId"], "appSecret": "****", "brand": brand})
if args[:2] == ["auth", "qrcode"]:
    Path(args[args.index("--output") + 1]).write_bytes(b"\x89PNG\r\n\x1a\nTEST")
    emit({"ok": True})
if args[:2] == ["auth", "status"]:
    entry = next(item for item in state["profiles"] if item["name"] == profile)
    emit({"appId": entry["appId"], "brand": entry["brand"], "identity": "bot",
          "identities": {"bot": {"status": "ready", "available": True},
                         "user": {"status": "ready" if state["ready"] else "missing",
                                  "available": state["ready"], "openId": state["open_id"],
                                  "userName": "Protocol User", "scope": " ".join(state["granted"])}}})
if args[:2] == ["auth", "check"]:
    required = args[args.index("--scope") + 1].split()
    missing = sorted(set(required) - set(state["granted"]))
    emit({"ok": state["ready"] and not missing, "missing": missing, "granted": state["granted"]},
         0 if state["ready"] and not missing else 1)
if args[:2] == ["auth", "login"]:
    if "--no-wait" in args:
        state["requested"] = args[args.index("--scope") + 1].split()
        save()
        emit({"verification_url": "https://accounts.feishu.cn/device?code=URL-ONLY",
              "device_code": "SECRET-DEVICE-CODE", "expires_in": 120})
    assert "--device-code" in args
    time.sleep(state.get("poll_delay", 0.4))
    if state.get("expired"):
        emit({"ok": False, "error": {"subtype": "expired_token"}}, 3)
    state["ready"] = True
    state["granted"] = state["requested"]
    save()
    emit({"event": "authorization_complete", "missing": []})
if args[:2] == ["api", "GET"]:
    assert args[args.index("--as") + 1] == "user" and "--json" not in args
    emit({"ok": True, "identity": "user", "data": {"items": []}})
emit({"ok": False, "error": {"message": "unsupported command"}}, 2)
'''


@pytest.fixture
def protocol(tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    state_path, log_path = tmp_path / "state.json", tmp_path / "calls.jsonl"
    state_path.write_text(json.dumps({
        "profiles": [{"name": "existing", "appId": "cli-existing", "brand": "feishu", "active": True}],
        "ready": True, "open_id": "ou-user", "granted": ["offline_access"]}))
    cli = tools / "lark-cli"
    cli.write_text(f"#!{sys.executable}\n" + FAKE_CLI)
    cli.chmod(0o755)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv("PATH", str(tools))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "unrelated-hermes"))
    monkeypatch.setenv("LARKSUITE_CLI_CONFIG_DIR", str(tmp_path / "official-config"))
    monkeypatch.setenv("FEISHU_TEST_STATE", str(state_path))
    monkeypatch.setenv("FEISHU_TEST_LOG", str(log_path))
    home = tmp_path / "bmem"
    home.mkdir()
    connector = feishu.FeishuConnector(SimpleNamespace(home=home))
    imported = []
    def import_history(store, run_cli, brand, account_id, sources, progress=None, sensitivity="private"):
        imported.append((brand, account_id, list(sources), sensitivity))
        assert run_cli(["api", "GET", "/read", "--as", "user", "--format", "json"])["ok"]
        if progress:
            progress({"stage": "documents", "imported": 1, "skipped": 2, "errors": 0})
        return {"imported": 1, "skipped": 2, "errors": 0}
    monkeypatch.setattr(feishu, "import_history", import_history)
    def update(**changes):
        state = json.loads(state_path.read_text())
        state.update(changes)
        state_path.write_text(json.dumps(state))
    def calls():
        return [json.loads(line) for line in log_path.read_text().splitlines()] if log_path.exists() else []
    yield SimpleNamespace(connector=connector, update=update, calls=calls, state_path=state_path,
                          tools=tools, cli=cli, home=home, imported=imported)
    connector.close()


def wait_for(connector, condition, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = connector.status("feishu")
        if condition(status):
            return status
        time.sleep(0.01)
    raise AssertionError(f"unexpected connector state: {connector.status('feishu')}")


def idle(connector):
    return wait_for(connector, lambda status: not status["busy"])


def test_profile_reuse_and_safe_dto(protocol):
    connector = protocol.connector
    connector.connect("feishu")
    status = idle(connector)
    assert status["phase"] == "connected" and status["authenticated"]
    assert status["history"] == {"confirmed": False, "sources": [], "enabled": False, "sensitivity": "private"}
    assert set(status["account"]) == {"id", "name"}
    calls = protocol.calls()
    assert not any(call["workspace"] for call in calls)
    assert not any(call["args"][:2] in (["config", "init"], ["auth", "login"]) for call in calls)
    assert not protocol.imported and "device_code" not in json.dumps(status)


def test_missing_scopes_starts_auth_and_keeps_challenge_ephemeral(protocol):
    connector = protocol.connector
    connector.connect("feishu", sources=["documents"])
    waiting = wait_for(connector, lambda status: status["authorization"] is not None)
    assert waiting["phase"] == "authorizing" and set(waiting["authorization"]) == {"url", "expires_at"}
    expiry = datetime.fromisoformat(waiting["authorization"]["expires_at"].replace("Z", "+00:00"))
    assert 100 < (expiry - datetime.now(timezone.utc)).total_seconds() <= 121
    wait_for(connector, lambda status: connector.qrcode("feishu") is not None)
    assert connector.qrcode("feishu").startswith(b"\x89PNG")
    connector.connect("feishu", sources=["private_chats"])
    result = idle(connector)
    assert result["authenticated"] and result["error"] is None
    calls = [call["args"] for call in protocol.calls()]
    assert sum("--no-wait" in args for args in calls) == 1
    requested = next(args[args.index("--scope") + 1].split() for args in calls if "--no-wait" in args)
    assert set(requested) == feishu.required_scopes(["documents"])
    persisted = protocol.home / "connections" / "feishu.json"
    assert persisted.stat().st_mode & 0o777 == 0o600
    assert all(value not in persisted.read_text() for value in ("SECRET", "device_code", "accounts.feishu"))
    assert result["history"]["sources"] == ["documents"]
    assert protocol.imported[0][2:] == (["documents"], "private")


def test_new_app_preserves_profiles_and_exposes_setup_qr(protocol):
    release = protocol.home / "continue-setup"
    protocol.update(profiles=[{"name": "other", "appId": "cli-other", "brand": "lark", "active": True}],
                    setup_release=str(release))
    connector = protocol.connector
    connector.connect("feishu", sources=[])
    waiting = wait_for(connector, lambda status: status["phase"] == "configuring" and status["authorization"] is not None)
    assert "SETUP" in waiting["authorization"]["url"]
    wait_for(connector, lambda status: connector.qrcode("feishu") is not None)
    release.touch()
    assert idle(connector)["authenticated"]
    profiles = json.loads(protocol.state_path.read_text())["profiles"]
    assert profiles[0]["name"] == "other" and profiles[0]["active"]
    args = next(call["args"] for call in protocol.calls() if call["args"][:2] == ["config", "init"])
    assert "--new" in args and args[args.index("--name") + 1].startswith("bmem-feishu")
    assert not protocol.imported


def test_bot_only_expired_auth_and_retry(protocol):
    protocol.update(ready=False, expired=True)
    connector = protocol.connector
    connector.connect("feishu", sources=[])
    result = idle(connector)
    assert not result["authenticated"] and result["account"] is None and result["error"]
    protocol.update(expired=False)
    connector.connect("feishu")
    assert idle(connector)["authenticated"]


def test_selection_sensitivity_namespace_and_tick(protocol):
    connector = protocol.connector
    connector.configure("feishu", ["documents"], sensitivity="normal")
    connector.connect("feishu")
    assert idle(connector)["sync"]["imported"] == 1
    first_id = protocol.imported[-1][1]
    assert protocol.imported[-1][3] == "normal" and not connector.tick()
    protocol.update(open_id="ou-different")
    connector.sync("feishu")
    idle(connector)
    assert protocol.imported[-1][1] != first_id
    connector.configure("feishu", [], enabled=False)
    count = len(protocol.imported)
    connector.tick()
    assert len(protocol.imported) == count
    restarted = feishu.FeishuConnector(SimpleNamespace(home=protocol.home))
    assert restarted.status("feishu")["history"] == {
        "confirmed": True, "sources": [], "enabled": False, "sensitivity": "private"}
    restarted.close()


def test_invalid_public_inputs(protocol):
    for brand, sources, sensitivity in (("slack", [], "private"), ("feishu", ["all"], "private"),
                                       ("feishu", [], "public"), ("feishu", "documents", "private")):
        with pytest.raises(ValueError):
            protocol.connector.configure(brand, sources, sensitivity=sensitivity)


def test_restart_rechecks_auth_without_login_then_resumes_tick(protocol, monkeypatch):
    connector = protocol.connector
    connector.connect("feishu", sources=["documents"])
    assert idle(connector)["authenticated"]
    connector.close()
    previous_calls = len(protocol.calls())
    resumed = feishu.FeishuConnector(SimpleNamespace(home=protocol.home))
    try:
        resumed.tick()
        assert idle(resumed)["authenticated"]
        assert not any("--no-wait" in call["args"] for call in protocol.calls()[previous_calls:])
        assert len(protocol.imported) == 1
        monkeypatch.setattr(feishu, "SYNC_INTERVAL", 0)
        assert resumed.tick() == ["feishu"]
        assert idle(resumed)["sync"]["imported"] == 2
    finally:
        resumed.close()


def test_close_stops_owned_auth_process(protocol):
    protocol.update(ready=False, poll_delay=30)
    connector = protocol.connector
    connector.connect("feishu", sources=[])
    wait_for(connector, lambda status: status["phase"] == "authorizing" and status["authorization"] is not None)
    started = time.monotonic()
    connector.close()
    assert time.monotonic() - started < 3 and not connector.status("feishu")["busy"]


def test_managed_install_and_missing_npm(protocol, monkeypatch):
    protocol.cli.unlink()
    npm = protocol.tools / "npm"
    npm.write_text(f"#!{sys.executable}\n" + r'''
import os, sys
from pathlib import Path
args = sys.argv[1:]
assert args[-1] == "@larksuite/cli@1.0.100" and "-g" not in args
prefix = Path(args[args.index("--prefix") + 1])
target = prefix / "node_modules" / ".bin" / "lark-cli"
target.parent.mkdir(parents=True)
target.write_text(os.environ["FEISHU_TEST_CLI_SOURCE"])
target.chmod(0o755)
''')
    npm.chmod(0o755)
    monkeypatch.setenv("FEISHU_TEST_CLI_SOURCE", f"#!{sys.executable}\n" + FAKE_CLI)
    protocol.connector.connect("feishu", sources=[])
    assert idle(protocol.connector)["authenticated"]
    assert (protocol.home / "runtime" / "lark-cli" / "node_modules" / ".bin" / "lark-cli").exists()
    protocol.connector.close()
    npm.unlink()
    other_home = protocol.home / "other"
    other_home.mkdir()
    other = feishu.FeishuConnector(SimpleNamespace(home=other_home))
    other.connect("feishu")
    result = idle(other)
    assert result["phase"] == "error" and "npm" in json.dumps(result["error"]).lower()
    other.close()
