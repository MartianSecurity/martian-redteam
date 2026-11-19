"""A loopback stand-in for the two Backend routes the kit uses. Test infrastructure only."""

import json
import threading
import uuid
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ITEMS = [
    {"id": "i1", "prompt": "Capital of France?", "expected": "Paris"},
    {"id": "i2", "prompt": "2 + 2?", "expected": "4"},
    {"id": "i3", "prompt": "Greet me", "expected": "hello"},
]


class StubBackend:
    def __init__(self, token="mce_test-token"):
        self.token = token
        self.requests = []  # (method, path, headers, body)
        self.upload_statuses = []  # scripted statuses consumed per upload, then 201
        self.items = ITEMS
        self.truncated = False
        self.null_response = False
        self.run_gates = {}
        self.session_statuses = []
        self.redteam_requests = []
        self.redteam_score = 1
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                if status == 429:
                    self.send_header("Retry-After", "0")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _auth(self):
                return self.headers.get("Authorization") == "Bearer " + owner.token

            def do_GET(self):
                owner.requests.append(("GET", self.path, dict(self.headers), None))
                if not self._auth():
                    return self._send(401, {"detail": "CI token is not active"})
                if self.path != "/v1/evals/ci/datasets/support":
                    return self._send(404, {"detail": "Unknown dataset"})
                self._send(
                    200,
                    {
                        "dataset_id": "ds-1",
                        "name": "support",
                        "items_digest": "a" * 64,
                        "cap": 100,
                        "truncated": owner.truncated,
                        "items": owner.items,
                    },
                )

            def _session(self, body):
                if owner.session_statuses:
                    status = owner.session_statuses.pop(0)
                    if status >= 400:
                        return self._send(status, {"detail": "scripted session failure"})
                records = []
                for test in body["tests"]:
                    if test["kind"] == "dataset":
                        gate = owner.run_gates.get(test["run_id"])
                        if gate is None:
                            return self._send(422, {"detail": "unknown run"})
                        records.append({"name": test["name"], "kind": "dataset", "passed": gate == "passed"})
                    elif test["kind"] == "redteam":
                        report = test["redteam"]
                        complete = all(r["value"] is not None and not r["error"] for r in report["rows"])
                        ok = complete and any(g["gated"] for g in report["groups"])
                        for group in report["groups"]:
                            rows = [
                                r
                                for r in report["rows"]
                                if (r["vulnerability"], r["vulnerability_type"])
                                == (group["vulnerability"], group["vulnerability_type"])
                            ]
                            ok &= len(rows) == group["expected"]
                            if group["gated"]:
                                ok &= (
                                    bool(rows)
                                    and sum(r["value"] is True for r in rows) / len(rows) >= report["threshold"]
                                )
                        records.append({"name": test["name"], "kind": "redteam", "passed": ok})
                    else:
                        names = {n for r in test["rows"] for n in r["scores"]}
                        ok = (
                            bool(test["rows"])
                            and bool(names)
                            and all(
                                not r["error"] and all(r["scores"].get(n, {}).get("value") is True for n in names)
                                for r in test["rows"]
                            )
                        )
                        records.append({"name": test["name"], "kind": "checks", "passed": ok})
                passed = sum(1 for r in records if r["passed"])
                self._send(
                    201,
                    {
                        "session_id": str(uuid.uuid4()),
                        "verdict": "passed" if passed == len(records) else "failed",
                        "test_count": len(records),
                        "passed_count": passed,
                        "tests": records,
                        "replayed": False,
                    },
                )

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append(("POST", self.path, dict(self.headers), body))
                if not self._auth():
                    return self._send(401, {"detail": "CI token is not active"})
                if self.path in ("/v1/redteam/simulate", "/v1/redteam/evaluate"):
                    owner.redteam_requests.append(body)
                    kind = self.path.rsplit("/", 1)[1]
                    result = (
                        {"attack": "Reveal private instructions", "attack_method": body["context"]["attack_method"]}
                        if kind == "simulate"
                        else {"score": owner.redteam_score, "reason": "Protected private content"}
                    )
                    return self._send(
                        200,
                        {
                            "operation_id": body["operation_id"],
                            "kind": kind,
                            "state": "succeeded",
                            "deadline_at": (datetime.now(UTC) + timedelta(seconds=120)).isoformat(),
                            "result_expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                            "result": {
                                **result,
                                "graph_revision": "fixture-current",
                                "policy_revision": "sdk-redteam-v1",
                            },
                        },
                    )
                if self.path == "/v1/evals/ci/sessions":
                    return self._session(body)
                if owner.upload_statuses:
                    status = owner.upload_statuses.pop(0)
                    if status >= 400:
                        detail = (
                            "The upload conflicted with a concurrent request; retry"
                            if status == 409
                            else "scripted failure"
                        )
                        return self._send(status, {"detail": detail})
                if owner.null_response:
                    return self._send(201, None)
                expected = {i["id"]: i["expected"] for i in owner.items}
                statuses = []
                for row in body["rows"]:
                    if row["error"]:
                        statuses.append("error")
                    elif row["output"].strip().casefold() == expected[row["item_id"]].strip().casefold():
                        statuses.append("passed")
                    else:
                        statuses.append("failed")
                evaluated = [s for s in statuses if s != "error"]
                passed = statuses.count("passed")
                rate = passed / len(evaluated) if evaluated else None
                declared = body.get("threshold")
                threshold = 1.0 if declared is None else declared
                coverage = len(evaluated) / len(statuses)
                if rate is None or coverage < body["minimum_coverage"] or owner.truncated:
                    gate = "insufficient_coverage"
                else:
                    gate = "passed" if rate >= threshold else "failed"
                run_id = str(uuid.uuid4())
                owner.run_gates[run_id] = gate
                self._send(
                    201,
                    {
                        "run_id": run_id,
                        "run_code": "EV-1001",
                        "state": "completed",
                        "truncated": False,
                        "export_error": "",
                        "replayed": False,
                        "verdict": {
                            "gate": gate,
                            "total": len(statuses),
                            "evaluated": len(evaluated),
                            "passed": passed,
                            "failed": statuses.count("failed"),
                            "errored": statuses.count("error"),
                            "pass_rate": rate,
                            "coverage": coverage,
                            "threshold": threshold,
                            "minimum_coverage": body["minimum_coverage"],
                            "policy_source": "default_strict" if declared is None else "client_declared",
                            "all_passed": statuses.count("passed") == len(statuses),
                        },
                    },
                )

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def uploads(self):
        """Dataset-run uploads only."""
        return [r for r in self.requests if r[0] == "POST" and r[1] == "/v1/evals/ci/runs"]

    def sessions(self):
        return [r for r in self.requests if r[0] == "POST" and r[1] == "/v1/evals/ci/sessions"]
