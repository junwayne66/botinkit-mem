"""Acceptance of actual CLI, importers and MCP transport."""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import zstandard
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from bmem.adapters import connect, import_history, sync_history
from bmem.store import Store


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BMEM = PROJECT_ROOT / ".venv/bin/bmem"


def test_module_cli_entry_works_outside_project(tmp_path):
    result = subprocess.run([sys.executable, "-m", "bmem.cli", "--help"],
                            cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "usage: bmem" in result.stdout


@pytest.fixture
def live_service(tmp_path, request):
    options = getattr(request, "param", {})
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    environment = os.environ.copy()
    environment["BMEM_OLLAMA_URL"] = "http://127.0.0.1:1"
    environment["BMEM_CONFIG_ROOT"] = str(tmp_path / "config")
    environment["BMEM_CODEX_APP_SERVER"] = "0"
    environment["BMEM_HISTORY_SYNC"] = options.get("history_sync", "0")
    environment["BMEM_HISTORY_INTERVAL"] = "5"
    environment["BMEM_WORKER_INTERVAL"] = options.get("worker_interval", "0.5")
    seed_count = options.get("seed_declarations", 0)
    if seed_count:
        store = Store(tmp_path / "data")
        for index in range(seed_count):
            store.declare({"body": f"启动前已确认事实 {index}", "fact_key": f"startup.fact.{index}"})
    process = subprocess.Popen(
        [str(BMEM), "--home", str(tmp_path / "data"), "start", "--port", str(port)],
        cwd=PROJECT_ROOT, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    try:
        for _ in range(100):
            if process.poll() is not None:
                pytest.fail(process.stderr.read().decode())
            try:
                if httpx.get(url + "/api/health", timeout=0.3).is_success:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            pytest.fail("Daemon did not become ready")
        yield url, tmp_path
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_cli_declare_and_search(live_service):
    url, root = live_service
    common = [str(BMEM), "--home", str(root / "data"), "--url", url]
    declared = subprocess.run(common + ["declare", "CLI 链路使用 SQLite", "--fact-key", "cli.storage",
                                       "--occurred-at", "2026-01-01T00:00:00Z",
                                       "--effective-at", "2026-02-01T00:00:00Z"],
                              capture_output=True, text=True, timeout=20)
    assert declared.returncode == 0, declared.stderr
    assert json.loads(declared.stdout)["memory"]["effective_at"].startswith("2026-02-01T")
    searched = subprocess.run(common + ["search", "SQLite"], capture_output=True, text=True, timeout=20)
    assert searched.returncode == 0, searched.stderr
    assert "SQLite" in searched.stdout


async def test_mcp_actual_streamable_http(live_service):
    url, _ = live_service
    async with streamable_http_client(url + "/mcp") as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            names = {tool.name for tool in (await session.list_tools()).tools}
            assert names == {"memory_recall", "memory_search", "memory_expand", "memory_save",
                             "memory_declare", "memory_forget", "memory_status"}
            saved = await session.call_tool("memory_save", {"body": "MCP 自动共享蓝莓布丁",
                                                            "occurred_at": "2026-01-01T00:00:00Z",
                                                            "effective_at": "2026-02-01T00:00:00Z"})
            assert not saved.isError, saved
            assert json.loads(saved.content[0].text)["memory"]["effective_at"].startswith("2026-02-01T")
            found = await session.call_tool("memory_search", {"query": "蓝莓布丁"})
            assert not found.isError
            assert "蓝莓布丁" in str(found.content)
            private = await session.call_tool("memory_save", {
                "body": "MCP 私密紫水晶", "sensitivity": "private",
            })
            assert "紫水晶" not in str(private.content)
            private_id = json.loads(private.content[0].text)["memory"]["memory_id"]
            expanded = await session.call_tool("memory_expand", {"memory_id": private_id})
            assert "紫水晶" not in str(expanded.content)
            local = await session.call_tool("memory_expand", {"memory_id": private_id, "target": "local"})
            assert not local.isError
            assert "紫水晶" in str(local.content)
            revision = json.loads(local.content[0].text)["revision_no"]
            corrected = await session.call_tool("memory_declare", {
                "body": "MCP 私密紫水晶已纠正", "memory_id": private_id,
                "expected_revision": revision,
            })
            assert not corrected.isError, corrected
            assert "紫水晶" not in str(corrected.content)
            derived = await session.call_tool("memory_declare", {
                "body": "由私密紫水晶推导的要求", "derived_from": [private_id],
            })
            assert not derived.isError, derived
            assert "紫水晶" not in str(derived.content)


def test_claude_hook_cross_harness_recall(live_service):
    url, root = live_service
    httpx.post(url + "/api/declarations", json={"body": "自动召回使用 SQLite WAL", "harness": "codex"})
    payload = {"session_id": "native-claude", "cwd": str(root), "prompt": "SQLite 存储方案"}
    result = subprocess.run([str(BMEM), "--url", url, "hook", "claude-code", "prompt"],
                            input=json.dumps(payload), capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    context = json.loads(result.stdout)
    assert "SQLite WAL" in str(context)
    assert "additionalContext" in str(context)


def test_chinese_explicit_remember_hook_immediately_declares_t1(live_service):
    url, root = live_service
    environment = os.environ.copy()
    environment["BMEM_CONFIG_ROOT"] = str(root / "config")
    environment["BMEM_HOME"] = str(root / "data")
    environment["BMEM_OLLAMA_URL"] = "http://127.0.0.1:1"
    for index, prompt in enumerate(("记住：中文声明使用青柠冻。", "记住以后晨会先看白板。")):
        payload = {"session_id": f"remember-chinese-{index}", "cwd": str(root), "prompt": prompt}
        result = subprocess.run([str(BMEM), "--url", url, "hook", "claude-code", "prompt"],
                                input=json.dumps(payload), capture_output=True, text=True,
                                env=environment, timeout=20)
        assert result.returncode == 0, result.stderr
        response = httpx.get(url + "/api/memories", timeout=5)
        assert response.status_code == 200, response.text
        matches = [item for item in response.json()["items"] if item["body"] == prompt]
        assert len(matches) == 1, response.json()
        assert matches[0]["status"] == "active"
        assert matches[0]["trust_tier"] == "T1"


def test_connect_preserves_user_config_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("BMEM_CONFIG_ROOT", str(tmp_path))
    settings = tmp_path / ".claude/settings.json"
    settings.parent.mkdir()
    original_hook = {"hooks": [{"type": "command", "command": "echo existing"}]}
    settings.write_text(json.dumps({"env": {"KEEP_ME": "retained"},
                                    "hooks": {"SessionStart": [original_hook]}}))
    first = connect("claude-code", config_root=tmp_path)
    before = json.loads(settings.read_text())
    second = connect("claude-code", config_root=tmp_path)
    after = json.loads(settings.read_text())
    assert first and second
    assert before == after
    assert after["env"]["KEEP_ME"] == "retained"
    assert original_hook in after["hooks"]["SessionStart"]
    assert first["backups"]
    backup = Path(first["backups"][0])
    assert json.loads(backup.read_text())["env"]["KEEP_ME"] == "retained"


@pytest.mark.parametrize("harness", ["claude-code", "codex"])
def test_real_history_formats_import_idempotently(tmp_path, harness):
    history = tmp_path / "history.jsonl"
    if harness == "claude-code":
        row = {"type": "user", "uuid": "claude-event-1", "sessionId": "session-1",
               "timestamp": "2026-11-12T10:00:00Z",
               "message": {"role": "user", "content": "记住历史导入偏好用中文"}}
    else:
        row = {"timestamp": "2026-11-12T10:00:00Z", "type": "response_item",
               "payload": {"type": "message", "role": "user", "content": [
                   {"type": "input_text", "text": "记住历史导入偏好用中文"}]}}
    history.write_text(json.dumps(row, ensure_ascii=False) + "\n")
    store = Store(tmp_path / "data")
    import_history(store, harness=harness, paths=[str(history)], config_root=tmp_path)
    count = store.stats()["events"]
    assert count >= 1
    import_history(store, harness=harness, paths=[str(history)], config_root=tmp_path)
    assert store.stats()["events"] == count
    assert store.records("import_cursors")


def test_compressed_dsh_history_is_discovered(tmp_path):
    history = tmp_path / "dsh.jsonl.zstd"
    row = {"type": "user/message", "seq": 1, "time": "2026-11-12T10:00:00Z",
           "data": {"id": "dsh-event-1", "source": {"kind": "user"},
                    "content": [{"type": "text", "text": "记住压缩历史必须保留来源"}]}}
    history.write_bytes(zstandard.ZstdCompressor().compress((json.dumps(row) + "\n").encode()))
    store = Store(tmp_path / "data")
    result = import_history(store, harness="dsh", paths=[str(history)], config_root=tmp_path)
    records = store.records("import_cursors")
    assert records, result
    assert any(record.get("discovered", 0) >= 1 for record in records)


def test_bad_history_does_not_block_other_files(tmp_path):
    bad = tmp_path / "bad.jsonl"
    good = tmp_path / "MEMORY.md"
    bad.write_text("{broken\n")
    good.write_text("# Verified memory\n\n记住先运行核心功能测试。\n")
    store = Store(tmp_path / "data")
    import_history(store, harness="claude-code", paths=[str(bad), str(good)], config_root=tmp_path)
    assert store.stats()["events"] >= 1
    records = store.records("import_cursors")
    assert any(record.get("error") or record.get("skipped") for record in records)


def test_history_read_error_retries_after_permissions_recover(tmp_path):
    history = tmp_path / "history.jsonl"
    history.write_text(json.dumps({"type": "user", "uuid": "retry-user", "sessionId": "retry-session",
                                    "message": {"role": "user", "content": "记住修复后重试导入"}}) + "\n")
    store = Store(tmp_path / "data")
    history.chmod(0)
    try:
        failed = import_history(store, harness="claude-code", paths=[str(history)], config_root=tmp_path)
        assert failed["errors"] >= 1
    finally:
        history.chmod(0o600)
    recovered = import_history(store, harness="claude-code", paths=[str(history)], config_root=tmp_path)
    assert recovered["imported"] == 1


def test_default_discovery_imports_private_claude_project_memory(tmp_path):
    memory_file = tmp_path / ".claude/projects/project-one/memory/MEMORY.md"
    memory_file.parent.mkdir(parents=True)
    memory_file.write_text(
        "---\nsensitivity: private\n---\n# Private memory\n\n私密代号紫藤花。\n",
        encoding="utf-8",
    )
    store = Store(tmp_path / "data")

    result = import_history(store, harness="claude-code", config_root=tmp_path)

    assert result["errors"] == 0, result
    assert result["imported"] == 1
    assert any(item["path"] == str(memory_file) for item in result["files"])
    assert store.search("紫藤花", target="cloud") == []
    local = store.search("紫藤花", target="local")
    assert len(local) == 1
    assert local[0]["sensitivity"] == "private"
    assert "sensitivity:" not in local[0]["body"]


def test_markdown_import_skips_only_known_inactive_statuses(tmp_path):
    directory = tmp_path / "shared-memory"
    directory.mkdir()
    expected_bodies = set()
    for status in ("active", "superseded", "expired", "custom-status", None):
        label = status or "default"
        body = f"状态样本 {label} 的记忆内容。"
        front_matter = f"---\nstatus: {status}\n---\n" if status else ""
        (directory / f"{label}.md").write_text(front_matter + "# Memory\n\n" + body, encoding="utf-8")
        if status not in ("superseded", "expired"):
            expected_bodies.add("# Memory\n\n" + body)
    store = Store(tmp_path / "data")

    result = import_history(store, harness="codex", paths=[str(directory)], config_root=tmp_path / "config")

    assert result["errors"] == 0, result
    assert result["imported"] == 3, result
    memories = store.list_memories()
    assert {item["body"] for item in memories} == expected_bodies
    assert all(item["status"] == "active" and item["trust_tier"] == "T1" for item in memories)
    assert store.stats()["events"] == 3


def test_history_sync_combines_configured_paths_and_exclusions(tmp_path):
    config_root = tmp_path / "config"
    native_directory = config_root / ".claude/memory"
    native_directory.mkdir(parents=True)
    default_memory = native_directory / "default.md"
    default_memory.write_text("# Default memory\n\n默认来源的山茶花。\n", encoding="utf-8")
    configured_excluded = native_directory / "configured-excluded.md"
    configured_excluded.write_text("# Excluded\n\n配置排除的玉米糖。\n", encoding="utf-8")
    explicit_excluded = native_directory / "explicit-excluded.md"
    explicit_excluded.write_text("# Excluded\n\n参数排除的南瓜糖。\n", encoding="utf-8")
    private_memory = tmp_path / "additional-private.md"
    private_memory.write_text(
        "---\nsensitivity: private\n---\n# Private\n\n额外私密来源的紫罗兰。\n",
        encoding="utf-8",
    )
    additional_directory = tmp_path / "additional-directory"
    additional_directory.mkdir()
    directory_memory = additional_directory / "memory.md"
    directory_memory.write_text("# Directory memory\n\n额外目录来源的迎春花。\n", encoding="utf-8")
    store = Store(tmp_path / "data")
    (store.home / "history-sync.json").write_text(json.dumps({
        "exclude_paths": [str(configured_excluded)],
        "additional_paths": [str(private_memory), str(additional_directory)],
    }))

    result = sync_history(store, config_root=config_root, exclude_paths=[str(explicit_excluded)])

    assert result["errors"] == 0, result
    assert result["imported"] == 3
    assert {item["path"] for item in result["files"]} == {
        str(default_memory), str(private_memory), str(directory_memory),
    }
    assert store.search("山茶花", target="local")
    assert store.search("迎春花", target="local")
    assert store.search("紫罗兰", target="local")
    assert store.search("紫罗兰", target="cloud") == []
    assert store.search("玉米糖", target="local") == []
    assert store.search("南瓜糖", target="local") == []
    repeated = sync_history(store, config_root=config_root, exclude_paths=[str(explicit_excluded)])
    assert repeated["imported"] == 0
    assert repeated["unchanged_files"] == 3
    assert store.stats()["events"] == 3


def test_old_import_cursor_remains_fast_after_more_than_1000_sources(tmp_path, monkeypatch):
    memory_file = tmp_path / "MEMORY.md"
    memory_file.write_text("# Existing memory\n\n先检查导入覆盖率。\n", encoding="utf-8")
    store = Store(tmp_path / "data")
    first = import_history(store, harness="claude-code", paths=[str(memory_file)], config_root=tmp_path)
    assert first["imported"] == 1
    for index in range(1001):
        store.record_import(f"newer-source-{index}", {
            "path": str(tmp_path / f"newer-{index}.jsonl"),
            "parser_version": "fixture",
            "last_cursor": "{}",
        })
    assert all(item["path"] != str(memory_file) for item in store.records("import_cursors"))
    original_read_text = Path.read_text

    def reject_old_file_read(path, *args, **kwargs):
        assert path != memory_file, "unchanged history was reopened instead of using its exact cursor"
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", reject_old_file_read)
    repeated = import_history(store, harness="claude-code", paths=[str(memory_file)], config_root=tmp_path)

    assert repeated["errors"] == 0, repeated
    assert repeated["files"][0]["unchanged"] is True
    assert repeated["imported"] == 0
    assert store.stats()["events"] == 1


@pytest.mark.parametrize("harness", ["codex", "dsh"])
def test_streamed_history_preserves_bad_lines_and_canonical_identity(tmp_path, monkeypatch, harness):
    history = tmp_path / ("history.jsonl.zstd" if harness == "dsh" else "history.jsonl")
    if harness == "codex":
        records = [
            {"type": "session_meta", "payload": {"id": "stream-session", "cwd": str(tmp_path)}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "记住：流式历史保留来源"}},
            {"type": "response_item", "payload": {"type": "message", "id": "user-1", "role": "user",
                                                    "content": [{"type": "input_text", "text": "记住：流式历史保留来源"}]}},
            {"type": "response_item", "payload": {"type": "message", "id": "assistant-1", "role": "assistant",
                                                    "content": [{"type": "output_text", "text": "已收到记录要求。"}]}},
        ]
    else:
        records = [
            {"type": "session", "id": "stream-session", "cwd": str(tmp_path)},
            {"type": "user/message", "seq": 1, "data": {"id": "user-1", "source": {"kind": "user"},
                                                            "content": [{"type": "text", "text": "记住：流式历史保留来源"}]}},
            {"type": "assistant/message", "seq": 2,
             "data": {"message": {"id": "assistant-1", "source": {"kind": "model"},
                                    "content": [{"type": "text", "text": "已收到记录要求。"}]}}},
        ]
    lines = [json.dumps(record, ensure_ascii=False) for record in records]
    lines.insert(2, "{broken")
    serialized = ("\n\n".join(lines) + "\n").encode()
    history.write_bytes(zstandard.ZstdCompressor().compress(serialized) if harness == "dsh" else serialized)
    store = Store(tmp_path / "data")
    original_read_text = Path.read_text
    original_read_bytes = Path.read_bytes

    def reject_whole_text_read(path, *args, **kwargs):
        assert path != history, "history import must stream transcript text"
        return original_read_text(path, *args, **kwargs)

    def reject_whole_bytes_read(path, *args, **kwargs):
        assert path != history, "history import must stream transcript bytes"
        return original_read_bytes(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", reject_whole_text_read)
    monkeypatch.setattr(Path, "read_bytes", reject_whole_bytes_read)
    imported = import_history(store, harness=harness, paths=[str(history)], config_root=tmp_path)

    assert imported["errors"] == 0, imported
    assert imported["imported"] == 2
    assert imported["files"][0]["skipped_reasons"]["json_decode"] == 1
    assert store.stats()["events"] == 2
    assert len(store.search("流式历史", target="local")) == 1
    repeated = import_history(store, harness=harness, paths=[str(history)], config_root=tmp_path)
    assert repeated["files"][0]["unchanged"] is True
    assert repeated["imported"] == 0
    assert store.stats()["events"] == 2


def test_streamed_codex_event_message_fallback_remains_available(tmp_path):
    history = tmp_path / "legacy.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": "legacy-session", "cwd": str(tmp_path)}},
        {"type": "event_msg", "payload": {"type": "user_message", "id": "legacy-user",
                                             "message": "记住：旧版历史仍可导入"}},
    ]
    history.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n")
    store = Store(tmp_path / "data")

    result = import_history(store, harness="codex", paths=[str(history)], config_root=tmp_path)

    assert result["errors"] == 0, result
    assert result["imported"] == 1
    assert store.search("旧版历史", target="local")


def test_codex_late_canonical_layer_still_suppresses_event_messages(tmp_path):
    history = tmp_path / "late-canonical.jsonl"
    records = [{"type": "session_meta", "payload": {"id": "late-session", "cwd": str(tmp_path)}}]
    records.extend(
        {"type": "event_msg", "payload": {"type": "user_message", "id": f"event-{index}",
                                             "message": "同一段原生历史内容"}}
        for index in range(513)
    )
    records.append({"type": "response_item", "payload": {
        "type": "message", "id": "canonical-user", "role": "user",
        "content": [{"type": "input_text", "text": "同一段原生历史内容"}],
    }})
    history.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n")
    store = Store(tmp_path / "data")

    result = import_history(store, harness="codex", paths=[str(history)], config_root=tmp_path)

    assert result["errors"] == 0, result
    assert result["imported"] == 1
    assert store.stats()["events"] == 1


@pytest.mark.parametrize("live_service", [{"history_sync": "1"}], indirect=True)
def test_enabled_history_sync_discovers_new_native_memory(live_service):
    url, root = live_service
    native_memory = root / "config/.claude/projects/new-project/memory/MEMORY.md"
    native_memory.parent.mkdir(parents=True)
    native_memory.write_text("# Native memory\n\n后台补扫应发现桂花糕。\n", encoding="utf-8")
    deadline = time.monotonic() + 15
    health = {}
    matches = []
    while time.monotonic() < deadline:
        health = httpx.get(url + "/api/health", timeout=1).json()
        matches = httpx.get(url + "/api/search", params={"q": "桂花糕"}, timeout=1).json()["items"]
        if health["history_sync"]["scans"] >= 1 and matches:
            break
        time.sleep(0.1)

    assert health["history_sync"]["enabled"] is True
    assert health["history_sync"]["running"] is True
    assert health["history_sync"]["scans"] >= 1, health
    assert health["history_sync"]["errors"] == 0, health
    assert len(matches) == 1
    sources = httpx.get(url + "/api/imports", timeout=1).json()["items"]
    assert any(item["path"] == str(native_memory) for item in sources)
    assert all(str(root / "config") in item["path"] for item in sources)


def test_history_sync_stays_disabled_without_opt_in(live_service):
    url, root = live_service
    native_memory = root / "config/.claude/memory/MEMORY.md"
    native_memory.parent.mkdir(parents=True)
    native_memory.write_text("# Native memory\n\n未开启补扫的松子糖。\n", encoding="utf-8")

    time.sleep(5.25)

    health = httpx.get(url + "/api/health", timeout=1).json()
    assert health["history_sync"]["enabled"] is False
    assert health["history_sync"]["running"] is False
    assert health["history_sync"]["scans"] == 0
    assert httpx.get(url + "/api/imports", timeout=1).json()["items"] == []
    assert httpx.get(url + "/api/search", params={"q": "松子糖"}, timeout=1).json()["items"] == []


@pytest.mark.parametrize("live_service", [{"worker_interval": "30", "seed_declarations": 4}], indirect=True)
def test_worker_drains_ready_jobs_without_idle_sleep(live_service):
    url, _ = live_service
    deadline = time.monotonic() + 5
    declaration_jobs = []
    while time.monotonic() < deadline:
        jobs = httpx.get(url + "/api/jobs", timeout=1).json()["items"]
        declaration_jobs = [item for item in jobs if item["kind"] == "distill"]
        if len(declaration_jobs) == 4 and all(item["status"] == "done" for item in declaration_jobs):
            break
        time.sleep(0.1)

    assert len(declaration_jobs) == 4
    assert all(item["status"] == "done" for item in declaration_jobs), declaration_jobs
    health = httpx.get(url + "/api/health", timeout=1).json()
    assert health["worker"]["running"] is True
    assert health["worker"]["errors"] == 0
