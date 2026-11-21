"""Archived evidence is attached to the right case and run."""

import asyncio
import base64
import hashlib
import json

import pytest

from martian.evals.local import open_stores
from martian.evals.promptfoo_import import PromptfooResultsError, import_promptfoo_results
from martian.evals.server.principal import SINGLE_USER
from tests.evals.test_promptfoo_import import WRITTEN, items_of, local_ui


def media(data):
    return {
        "hash": hashlib.sha256(data).hexdigest(),
        "mimeType": "application/octet-stream",
        "sizeBytes": len(data),
        "data": base64.b64encode(data).decode("ascii"),
    }


def archive():
    document = json.loads(WRITTEN.read_text())
    cases = document["results"]["results"]
    first = cases[0]
    unavailable = next(row for row in cases[1:] if row["provider"] == first["provider"])
    other = next(row for row in cases[1:] if row["provider"] != first["provider"])
    supplied = media(b"Exhibit map for the first hall")
    peer = media(b"Exhibit map for the other hall")
    missing = media(b"Media omitted from the archive")
    for row, asset in ((first, supplied), (unavailable, missing), (other, peer)):
        row["response"] = {**(row.get("response") or {}), "output": f"promptfoo://blob/{asset['hash']}"}
    first["traceId"] = "source-trace"
    first["evaluationId"] = document["evalId"]
    timestamp = 1_763_000_000_000
    document["traces"] = [
        {
            "traceId": "source-trace",
            "evaluationId": document["evalId"],
            "testCaseId": "source-case",
            "metadata": {"testIdx": first["testIdx"], "promptIdx": first["promptIdx"]},
            "spans": [
                {
                    "spanId": "2222222222222222",
                    "parentSpanId": "1111111111111111",
                    "name": "Read public exhibit map",
                    "startTime": timestamp + 3,
                    "endTime": timestamp + 8,
                    "statusCode": 1,
                },
                {
                    "spanId": "1111111111111111",
                    "name": "Exhibit fixture",
                    "startTime": timestamp,
                    "endTime": timestamp + 20,
                },
                {
                    "spanId": "3333333333333333",
                    "parentSpanId": "unexported-parent",
                    "name": "Check public exhibit map",
                    "startTime": timestamp + 9,
                    "endTime": timestamp + 15,
                    "statusCode": 2,
                },
            ],
        }
    ]
    document["blobAssets"] = [supplied, peer, media(b"Unreferenced artifact")]
    return document, first["provider"]["id"], supplied, peer, missing


def test_archived_trace_and_verified_media_are_readable_only_in_the_related_run(tmp_path):
    document, provider, supplied, peer, missing = archive()
    path = tmp_path / "archive.json"
    path.write_text(json.dumps(document))
    home = tmp_path / "home"
    runs = import_promptfoo_results([path], home)
    run = next(run for run in runs if run.provenance["promptfoo"]["provider"]["id"] == provider)
    with local_ui(home) as client:
        items = items_of(client, run.id)
        linked = next(item for item in items.values() if item["output"].endswith(supplied["hash"]))
        trace = client.get(f"/v1/evals/{run.id}/items/{linked['id']}/trace")
        assert trace.status_code == 200, trace.text
        spans = {span["name"]: span for span in trace.json()["spans"]}
        assert spans["Read public exhibit map"]["parent_id"] == spans["Exhibit fixture"]["id"]
        assert spans["Check public exhibit map"]["parent_id"] == spans["Promptfoo trace"]["id"]
        assert (
            spans["Check public exhibit map"]["attributes"]["promptfoo.original_parent_span_id"] == "unexported-parent"
        )
        assert spans["Read public exhibit map"]["duration_ms"] == 5
        assert spans["Check public exhibit map"]["status"] == {"code": "STATUS_CODE_ERROR"}

        info = client.get(f"/v1/evals/{run.id}/provenance").json()["admission"]["promptfoo"]["media"]
        assert info["cases"][linked["dataset_item_id"]] == [supplied["hash"]]
        assert info["assets"][missing["hash"]] == {"available": False}
        prefix = f"/v1/local/evals/runs/{run.id}/promptfoo-media/"
        download = client.get(prefix + supplied["hash"])
        assert download.status_code == 200 and download.content == base64.b64decode(supplied["data"])
        assert download.headers["content-type"] == "application/octet-stream"
        assert download.headers["content-disposition"].startswith("attachment;")
        assert download.headers["x-content-type-options"] == "nosniff"
        assert client.get(prefix + peer["hash"]).status_code == 404
        assert client.get(prefix + missing["hash"]).status_code == 404
        assert [item.id for item in import_promptfoo_results([path], home)] == [item.id for item in runs]

        store, records = open_stores(home)
        try:
            asyncio.run(
                store.put_artifact(
                    SINGLE_USER, run.project_id, run.id, f"promptfoo-media/{supplied['hash']}", b"changed"
                )
            )
        finally:
            records.close()
        assert client.get(prefix + supplied["hash"]).status_code == 409


@pytest.mark.parametrize("damage", ["media", "trace-cycle", "trace-scope"])
def test_invalid_linked_artifacts_stop_a_batch_before_writes(tmp_path, damage):
    document, *_ = archive()
    if damage == "media":
        document["blobAssets"][0]["data"] = base64.b64encode(b"A different file of same length").decode()
        document["blobAssets"][0]["sizeBytes"] = len(base64.b64decode(document["blobAssets"][0]["data"]))
    elif damage == "trace-cycle":
        document["traces"][0]["spans"][1]["parentSpanId"] = "2222222222222222"
    else:
        document["traces"][0]["evaluationId"] = "other-eval"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(document))
    home = tmp_path / "home"
    with pytest.raises(PromptfooResultsError, match="linked trace|SHA-256"):
        import_promptfoo_results([WRITTEN, path], home)
    assert not home.exists()
