"""Truthful graph projections and source detail through real HTTP entry points."""

import pytest
from fastapi.testclient import TestClient

from bmem.api import create_app
from bmem.store import Store


@pytest.fixture
def graph_authority(tmp_path, monkeypatch):
    monkeypatch.setenv("BMEM_CONFIG_ROOT", str(tmp_path / "config"))
    monkeypatch.setenv("BMEM_OLLAMA_URL", "http://127.0.0.1:1")
    with TestClient(create_app(tmp_path, start_worker=False), base_url="http://127.0.0.1") as client:
        yield client, Store(tmp_path)


def test_graph_projects_every_parent_with_actual_source_evidence(graph_authority):
    client, store = graph_authority
    parents = [store.declare({"body": f"父事实 {name}", "fact_key": name})["memory"]
               for name in ("first", "second")]
    child = store.declare({"body": "派生方法", "kind": "method",
                           "derived_from": [item["memory_id"] for item in parents]})["memory"]

    graph = client.get("/api/graph").json()

    edges = [edge for edge in graph["edges"]
             if edge["relation"] == "derived" and edge["target"] == child["memory_id"]]
    assert {edge["source"] for edge in edges} == {item["memory_id"] for item in parents}
    source_ids = {source["source_row_id"] for source in child["sources"]}
    for edge in edges:
        assert edge["evidence"]
        assert all(proof["source_row_id"] in source_ids for proof in edge["evidence"])
        assert all(proof["event_id"] for proof in edge["evidence"])


def test_graph_shared_source_is_a_real_event_not_a_semantic_guess(graph_authority):
    client, store = graph_authority
    event = store.ingest({"body": "同一对话中的两条事实", "source_type": "user_dialogue"})
    memories = [store.apply_candidate(event["event_id"], {
        "body": f"事实 {name}", "kind": "fact", "fact_key": name, "action": "ADD",
    }, "fixture")["memory"] for name in ("first", "second")]

    graph = client.get("/api/graph").json()

    hubs = [node for node in graph["nodes"] if node.get("node_type") == "source"]
    assert len(hubs) == 1
    assert hubs[0]["root_source_id"] == event["event_id"]
    assert hubs[0]["event_ids"] == [event["event_id"]]
    edges = [edge for edge in graph["edges"] if edge["source"] == hubs[0]["id"]]
    assert {edge["target"] for edge in edges} == {memory["memory_id"] for memory in memories}
    assert all(edge["relation"] == "evidence" for edge in edges)
    assert client.get(f"/api/events/{event['event_id']}").json()["body"] == "同一对话中的两条事实"


def test_graph_revision_identity_and_replacement_use_saved_history(graph_authority):
    client, store = graph_authority
    first = store.declare({"body": "服务旧端口", "fact_key": "service.port",
                           "occurred_at": "2026-01-01T00:00:00Z"})
    current = store.declare({"body": "服务新端口", "memory_id": first["memory"]["memory_id"],
                             "expected_revision": 1, "occurred_at": "2026-02-01T00:00:00Z"})["memory"]

    graph = client.get("/api/graph").json()

    previous = next(node for node in graph["nodes"] if node.get("node_type") == "revision")
    assert previous["id"] != current["memory_id"]
    assert previous["memory_id"] == current["memory_id"]
    assert previous["revision_no"] == 1
    assert previous["label"] == "服务旧端口"
    edge = next(edge for edge in graph["edges"] if edge["relation"] == "supersedes")
    assert (edge["source"], edge["target"]) == (previous["id"], current["memory_id"])
    assert edge["evidence"][0]["from_revision"] == 1
    assert edge["evidence"][0]["to_revision"] == 2
    assert edge["evidence"][0]["event_id"] == current["revisions"][-1]["source_event_id"]


def test_graph_preserves_invalidated_relationship_as_marked_history(graph_authority):
    client, store = graph_authority
    parent = store.declare({"body": "父事实旧版"})["memory"]
    child = store.declare({"body": "派生方法", "derived_from": [parent["memory_id"]]})["memory"]
    store.declare({"body": "父事实新版", "memory_id": parent["memory_id"], "expected_revision": 1})

    graph = client.get("/api/graph").json()

    edge = next(edge for edge in graph["edges"] if edge["relation"] == "derived"
                and edge["source"] == parent["memory_id"] and edge["target"] == child["memory_id"])
    assert edge["evidence"][0]["invalidated_at"] is not None


def test_graph_independent_records_have_no_fabricated_relationships(graph_authority):
    client, store = graph_authority
    for name in ("独立甲", "独立乙"):
        store.declare({"body": name})
    graph = client.get("/api/graph").json()
    assert len(graph["nodes"]) == 2
    assert graph["edges"] == []


def test_graph_and_event_details_enforce_private_and_erasure_boundaries(graph_authority):
    client, store = graph_authority
    public = store.declare({"body": "公开事实"})["memory"]
    private = store.declare({"body": "私密事实旧版", "sensitivity": "private"})
    store.declare({"body": "私密事实新版", "memory_id": private["memory"]["memory_id"],
                   "expected_revision": 1, "sensitivity": "private"})

    graph = client.get("/api/graph?target=cloud").json()
    assert {node["memory_id"] for node in graph["nodes"]} == {public["memory_id"]}
    assert graph["edges"] == []
    assert client.get(f"/api/events/{private['event_id']}?target=cloud").status_code == 404
    assert client.get(f"/api/events/{private['event_id']}").status_code == 200
    store.forget(private["memory"]["memory_id"])
    erased = client.get(f"/api/events/{private['event_id']}").json()
    assert erased["body"] == ""
    assert erased["has_evidence"] is False
    graph = client.get("/api/graph").json()
    assert {node["memory_id"] for node in graph["nodes"]} == {public["memory_id"]}
