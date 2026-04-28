"""End-to-end API tests using FastAPI's TestClient."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fluxstudio.api.runs import _registry_size, _reset_registry_for_tests
from fluxstudio.server import create_app


@pytest.fixture
def client() -> TestClient:
    _reset_registry_for_tests()
    return TestClient(create_app())


def test_ops_endpoint_returns_schemas(client: TestClient) -> None:
    resp = client.get("/api/ops")
    assert resp.status_code == 200
    body = resp.json()
    assert "schemas" in body
    assert isinstance(body["schemas"], list)
    assert body["schemas"], "expected at least one discovered op"


def test_ops_endpoint_with_modules_query(client: TestClient) -> None:
    resp = client.get("/api/ops", params={"modules": "dataflux.ops.numpy"})
    assert resp.status_code == 200
    schemas = resp.json()["schemas"]
    assert all(s["module"] == "dataflux.ops.numpy" for s in schemas)


def test_ops_endpoint_rejects_empty_modules(client: TestClient) -> None:
    resp = client.get("/api/ops", params={"modules": ","})
    assert resp.status_code == 400


def test_visualizers_endpoint(client: TestClient) -> None:
    resp = client.get("/api/visualizers")
    assert resp.status_code == 200
    body = resp.json()
    assert "hints" in body and "types" in body


def test_graph_round_trip_via_api(client: TestClient, tiny_pipeline_yaml: Path) -> None:
    yaml_text = tiny_pipeline_yaml.read_text()
    parse = client.post("/api/graph/from-yaml", json={"yaml": yaml_text})
    assert parse.status_code == 200, parse.text
    graph = parse.json()
    assert len(graph["nodes"]) == 3

    emit = client.post("/api/graph/to-yaml", json=graph)
    assert emit.status_code == 200, emit.text
    text = emit.json()["yaml"]
    assert "!class:dataflux.core.Flux()" in text


def test_graph_from_yaml_rejects_garbage(client: TestClient) -> None:
    resp = client.post("/api/graph/from-yaml", json={"yaml": "::: not yaml"})
    assert resp.status_code == 400


def test_graph_to_yaml_rejects_empty_graph(client: TestClient) -> None:
    resp = client.post("/api/graph/to-yaml", json={"nodes": [], "edges": []})
    assert resp.status_code == 400


def test_run_lifecycle_streams_step_events(client: TestClient, tiny_pipeline_yaml: Path) -> None:
    yaml_text = tiny_pipeline_yaml.read_text()
    start = client.post("/api/run", json={"yaml": yaml_text, "mode": "continue"})
    assert start.status_code == 200, start.text
    run_id = start.json()["run_id"]

    # /history should populate as the background thread emits events. Poll for done.
    events: list = []
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        history = client.get(f"/api/run/{run_id}/history").json()
        events = history["events"]
        if history["done"]:
            break
        time.sleep(0.05)
    assert events, "expected events to be recorded in history"
    # 4 samples × 2 ops = 8 events.
    assert len(events) == 8
    assert all(e["payload"] is None or e["payload"]["input"]["kind"] in {"ndarray", "scalar"} for e in events)


def test_run_invalid_yaml_returns_400(client: TestClient) -> None:
    resp = client.post("/api/run", json={"yaml": "not even close to confluid", "mode": "continue"})
    assert resp.status_code == 400


def test_run_rejects_unknown_mode(client: TestClient) -> None:
    resp = client.post("/api/run", json={"yaml": "x", "mode": "skip-everything"})
    assert resp.status_code == 400


def test_control_unknown_run_returns_404(client: TestClient) -> None:
    resp = client.post("/api/run/does-not-exist/control", json={"command": "abort"})
    assert resp.status_code == 404


def test_control_rejects_unknown_command(client: TestClient, tiny_pipeline_yaml: Path) -> None:
    yaml_text = tiny_pipeline_yaml.read_text()
    start = client.post("/api/run", json={"yaml": yaml_text, "mode": "continue"})
    run_id = start.json()["run_id"]
    resp = client.post(f"/api/run/{run_id}/control", json={"command": "fly"})
    assert resp.status_code == 400


def test_websocket_replays_history_then_signals_final(client: TestClient, tiny_pipeline_yaml: Path) -> None:
    yaml_text = tiny_pipeline_yaml.read_text()
    start = client.post("/api/run", json={"yaml": yaml_text, "mode": "continue"})
    run_id = start.json()["run_id"]

    # Wait briefly for the background thread to finish, so the WS sees full history.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if client.get(f"/api/run/{run_id}/history").json()["done"]:
            break
        time.sleep(0.05)

    with client.websocket_connect(f"/api/run/{run_id}/ws") as ws:
        received: list = []
        while True:
            msg = ws.receive_json()
            if msg.get("event_kind") == "stream_end":
                break
            received.append(msg)
            if len(received) > 50:
                pytest.fail("runaway WebSocket: too many messages")
    assert len(received) == 8


def test_websocket_unknown_run_closes(client: TestClient) -> None:
    """Connecting to an unknown run_id closes the socket immediately with code 4404."""
    from starlette.websockets import WebSocketDisconnect

    with client.websocket_connect("/api/run/missing/ws") as ws:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            ws.receive_json()
    assert excinfo.value.code == 4404


def test_landing_page_when_frontend_not_built(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"FluxStudio" in resp.content


def test_registry_grows_then_can_be_reset(client: TestClient, tiny_pipeline_yaml: Path) -> None:
    yaml_text = tiny_pipeline_yaml.read_text()
    before = _registry_size()
    client.post("/api/run", json={"yaml": yaml_text, "mode": "continue"})
    assert _registry_size() == before + 1
    _reset_registry_for_tests()
    assert _registry_size() == 0
