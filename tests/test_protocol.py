from martian_redteam.protocol import (
    extract_graph_revision,
    failed_run_status,
    graph_state,
    parse_retry_after,
    parse_wait_body,
)


def test_parse_wait_body_strips_heartbeat_newlines(simulator_payload: dict) -> None:
    import json

    body = "\n\n" + json.dumps(simulator_payload) + "\n"
    assert parse_wait_body(body)["attack"] == simulator_payload["attack"]


def test_graph_state_unwraps_output_envelope(simulator_payload: dict) -> None:
    envelope = {"status": "success", "output": simulator_payload, "run_id": "abc"}
    assert graph_state(envelope)["attack"] == simulator_payload["attack"]


def test_graph_state_unwraps_values_envelope(evaluator_payload: dict) -> None:
    envelope = {"values": evaluator_payload}
    assert graph_state(envelope)["score"] == 1


def test_graph_state_passthrough_for_smoke_shape(simulator_payload: dict) -> None:
    assert graph_state(simulator_payload) is simulator_payload


def test_failed_run_status() -> None:
    assert failed_run_status({"status": "error"}) == "error"
    assert failed_run_status({"status": "success"}) is None


def test_parse_retry_after_seconds() -> None:
    assert parse_retry_after("2") == 2.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("nope") is None


def test_extract_graph_revision() -> None:
    assert extract_graph_revision({"graph_revision": "sha256:abc"}) == "sha256:abc"
    assert extract_graph_revision({"graph_revision": ""}) is None
