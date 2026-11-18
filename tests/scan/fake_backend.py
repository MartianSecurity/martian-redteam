"""A stand-in for the backend's customer /v1/scanner routes, for `martian scan`'s tests and the scan
action's smoke job. Standard library only, so CI can run it with the runner's own ``python3``.

It speaks the shapes of ``tests/scan/contract/scanner-scans.json``; the tests hold every answer to that
file. With ``routes`` it can leave out the findings, export and gate routes, as a server from before
them does. It holds no rule: with ``seeded`` it reports one fixed finding on the first ``SKILL.md`` in
the archive, so a gate can be seen failing. It does not read YAML: every policy file validates to
``policy`` (the defaults unless a test sets it), except one holding ``refuse-me``, which is refused. The
gate blocks on every open finding, and with ``new_findings_only`` compares with the newest completed scan
of the subject whose ``git`` states its default branch. An ``Idempotency-Key`` used again answers its scan.

    python3 tests/scan/fake_backend.py --port-file PATH [--clean] [--token TOKEN]
"""

from __future__ import annotations

import argparse
import email.parser
import email.policy
import hashlib
import io
import json
import threading
import uuid
import zipfile
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

TOKEN = "mce_test_scan_key_0123456789"
ALL_ROUTES = frozenset({"findings", "export", "gate"})
RULE = "SKL-SEEDED-FIXTURE"
FAMILIES = ["skills", "mcp", "plugins", "subagents", "harness-config", "agent-code"]


def default_policy() -> dict[str, Any]:
    return {
        "version": 1,
        "paths": ["."],
        "include": list(FAMILIES),
        "external": {"follow": False, "depth": 1},
        "gate": {
            "fail_on": {"evidence": 0, "severity": "S5", "categories": None},
            "min_coverage": {},
            "new_findings_only": False,
        },
        "waivers": [],
    }


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(status: int, body: Any, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    return status, {"content-type": "application/json", **(headers or {})}, json.dumps(body).encode()


def _not_found() -> tuple[int, dict[str, str], bytes]:
    return _json(404, {"detail": "Not Found"})


def _refused(status: int, code: str, text: str, limit: int | None = None) -> tuple[int, dict[str, str], bytes]:
    detail: dict[str, Any] = {"code": code, "message": text}
    if limit is not None:
        detail["limit"] = limit
    return _json(status, {"detail": detail})


def content_digest(files: dict[str, bytes]) -> str:
    hasher = hashlib.sha256()
    for path in sorted(files):
        hasher.update(path.encode() + b"\0" + hashlib.sha256(files[path]).hexdigest().encode() + b"\n")
    return f"sha256:{hasher.hexdigest()}"


class FakeScanner:
    """The routes as a function of one request. ``routes`` lists the optional read routes this server
    has; ``fail`` ends every scan as failed; ``polls`` is how many reads a scan takes to end."""

    def __init__(
        self,
        *,
        token: str = TOKEN,
        routes: frozenset[str] = ALL_ROUTES,
        seeded: bool = True,
        fail: bool = False,
        polls: int = 2,
        archive_limit: int = 50 * 1024 * 1024,
    ) -> None:
        self.token, self.routes, self.seeded, self.fail, self.polls = token, routes, seeded, fail, polls
        self.archive_limit = archive_limit
        self.policy = default_policy()
        self.request_ids: dict[str, tuple[str, str]] = {}
        self.gates: list[dict[str, Any]] = []
        self.policies: list[str] = []
        self.scans: dict[str, dict[str, Any]] = {}
        self.files: dict[str, dict[str, bytes]] = {}
        self.reads: dict[str, int] = {}
        self.uploads: list[dict[str, Any]] = []
        self.requests: list[tuple[str, str]] = []
        self.lock = threading.Lock()

    def handle(
        self, method: str, target: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        with self.lock:
            return self._handle(method, target, {key.lower(): value for key, value in headers.items()}, body)

    def _handle(self, method: str, target: str, headers: dict[str, str], body: bytes) -> tuple[int, dict, bytes]:
        url = urlsplit(target)
        query = dict(parse_qsl(url.query))
        parts = url.path.strip("/").split("/")
        self.requests.append((method, url.path))
        if parts[:2] != ["v1", "scanner"]:
            return _not_found()
        if headers.get("authorization") != f"Bearer {self.token}":
            return _json(401, {"detail": "Not authenticated"})
        rest = parts[2:]
        if method == "POST" and rest == ["scans"]:
            return self._upload(headers, body)
        if method == "POST" and rest == ["gate-policy", "validate"] and "gate" in self.routes:
            return self._validate(body)
        if method == "POST" and len(rest) == 3 and rest[0] == "scans" and rest[2] == "gate" and rest[1] in self.scans:
            return self._gate(rest[1], body) if "gate" in self.routes else _not_found()
        if method != "GET" or len(rest) < 2 or rest[0] != "scans" or rest[1] not in self.scans:
            return _not_found()
        scan_id = rest[1]
        if len(rest) == 2:
            return _json(200, self._read(scan_id))
        if len(rest) != 3 or rest[2] not in self.routes:
            return _not_found()
        scan = self.scans[scan_id]
        if rest[2] not in ("findings", "export"):
            return _not_found()
        if rest[2] == "findings":
            return _json(200, {"items": self._findings(scan_id), "next_cursor": None})
        if scan["state"] != "completed":
            return _refused(409, "scan_not_completed", "The scan has not completed")
        form = query.get("format", "json")
        if form == "sarif":
            return 200, {"content-type": "application/sarif+json"}, json.dumps(self._sarif(scan_id)).encode()
        return _json(
            200,
            {
                "format": "martian-scanner-export",
                "version": 1,
                "scan": scan,
                "findings": self._findings(scan_id),
                "references": [],
            },
        )

    def _policy_text(self, body: bytes) -> str | None:
        try:
            text = json.loads(body)["policy"]
        except (ValueError, KeyError, TypeError):
            return None
        return text if isinstance(text, str) else None

    def _validate(self, body: bytes) -> tuple[int, dict, bytes]:
        text = self._policy_text(body)
        if text is None:
            return _json(422, {"detail": [{"loc": ["body", "policy"], "msg": "Field required", "type": "missing"}]})
        if "refuse-me" in text:
            return _json(
                422,
                {
                    "detail": {
                        "code": "invalid_gate_policy",
                        "message": "The policy file is not valid",
                        "errors": [{"loc": "gate.fail_on.severty", "message": "Extra inputs are not permitted"}],
                    }
                },
            )
        self.policies.append(text)
        return _json(200, {"policy": self.policy, "policy_sha256": hashlib.sha256(text.encode()).hexdigest()})

    def _baseline(self, scan_id: str) -> dict[str, Any] | None:
        scan = self.scans[scan_id]
        earlier = [
            other
            for other in self.scans.values()
            if other["id"] != scan_id
            and other["subject"]["id"] == scan["subject"]["id"]
            and other["state"] == "completed"
            and other["on_default_branch"]
            and other["created_at"] < scan["created_at"]
        ]
        return earlier[-1] if earlier else None

    def _gate(self, scan_id: str, body: bytes) -> tuple[int, dict, bytes]:
        text = self._policy_text(body)
        status, _, answer = self._validate(body)
        if status != 200 or text is None:
            return status, {"content-type": "application/json"}, answer
        scan = self.scans[scan_id]
        if scan["state"] not in ("completed", "failed"):
            return _refused(409, "scan_not_ended", "The scan has not ended")
        new_only = bool(self.policy["gate"]["new_findings_only"])
        baseline = self._baseline(scan_id) if new_only else None
        known = {finding["fingerprint"] for finding in self._findings(baseline["id"])} if baseline else set()
        findings = []
        for finding in self._findings(scan_id) if scan["state"] == "completed" else []:
            outcome = (
                "not_gating"
                if not finding["gating"]
                else "in_baseline"
                if finding["fingerprint"] in known
                else "blocking"
                if finding["status"] == "open"
                else finding["status"]
            )
            findings.append(
                {
                    "finding_id": finding["id"],
                    "rule_id": finding["rule_id"],
                    "category": finding["category"],
                    "severity": finding["severity"],
                    "evidence_level": finding["evidence_level"],
                    "title": finding["title"],
                    "path": finding["path"],
                    "line_start": finding["line_start"],
                    "fingerprint": finding["fingerprint"],
                    "outcome": outcome,
                }
            )
        counts = {name: 0 for name in ("blocking", "in_baseline", "waived_in_file", "waived", "accepted", "not_gating")}
        for item in findings:
            counts[item["outcome"]] += 1
        counts["below_threshold"] = 0
        if scan["state"] == "failed":
            verdict, reasons = "not_judged", [f"The scan failed ({scan['error']['code']}); it cannot be judged"]
        else:
            verdict = "failed" if counts["blocking"] else "passed"
            reasons = [f"{counts['blocking']} findings block" if counts["blocking"] else "No finding blocks"]
            if new_only:
                reasons.append(
                    f"Compared with baseline scan {baseline['id']}: {counts['in_baseline']} findings were already there"
                    if baseline
                    else "No completed scan of the default branch precedes this scan; every finding counts as new"
                )
            reasons += [
                f"Blocking: {item['rule_id']} at {item['path']}" for item in findings if item["outcome"] == "blocking"
            ]
        gate = {
            "id": str(uuid.uuid4()),
            "scan_id": scan_id,
            "subject": {key: scan["subject"][key] for key in ("id", "kind", "name")},
            "verdict": verdict,
            "exit_code": {"passed": 0, "failed": 1, "not_judged": 2}[verdict],
            "reasons": reasons,
            "new_findings_only": new_only,
            "baseline": {
                "scan_id": baseline["id"],
                "created_at": baseline["created_at"],
                "completed_at": baseline["completed_at"],
                "git": baseline["git"],
            }
            if baseline
            else None,
            "coverage_shortfalls": [],
            "counts": counts,
            "findings": findings,
            "waivers": [],
            "scan": {
                "state": scan["state"],
                "source": scan["source"],
                "content_digest": scan["content_digest"],
                "git": scan["git"],
                "on_default_branch": scan["on_default_branch"],
                "completed_at": scan["completed_at"],
                "error": scan["error"],
            },
            "policy": self.policy,
            "policy_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "evaluated_at": _now(),
        }
        self.gates.append(gate)
        return _json(201, gate)

    def _upload(self, headers: dict[str, str], body: bytes) -> tuple[int, dict, bytes]:
        if "content-length" not in headers:
            return _refused(411, "length_required", "Send a Content-Length")
        if not headers.get("content-type", "").startswith("multipart/form-data"):
            return _refused(415, "invalid_upload", "Send multipart/form-data")
        message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
            b"Content-Type: " + headers["content-type"].encode() + b"\r\n\r\n" + body
        )
        parts = list(message.iter_parts()) if message.is_multipart() else []
        names = [part.get_param("name", header="content-disposition") for part in parts]
        if names != ["manifest", "archive"]:
            return _refused(422, "invalid_upload", "Send two parts, 'manifest' then 'archive'")
        archive = parts[1].get_payload(decode=True) or b""
        if len(archive) > self.archive_limit:
            return _refused(413, "archive_too_large", "The archive is larger than the limit", self.archive_limit)
        try:
            manifest = json.loads(parts[0].get_payload(decode=True) or b"")
            subject = manifest["subject"]
        except (ValueError, KeyError, TypeError):
            return _refused(422, "invalid_manifest", "The manifest is not valid")
        try:
            with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                files = {info.filename: bundle.read(info) for info in bundle.infolist() if not info.is_dir()}
        except zipfile.BadZipFile:
            return _refused(422, "not_a_zip", "The archive is not a ZIP file")
        key = headers.get("idempotency-key")
        digest = hashlib.sha256(parts[0].get_payload(decode=True) + b"\0" + archive).hexdigest()
        if key is not None and key in self.request_ids:
            first, first_digest = self.request_ids[key]
            if first_digest != digest:
                return _refused(409, "request_id_reused", "This request id was already used for a different upload")
            return _json(200, self.scans[first])
        scan_id = str(uuid.uuid4())
        if key is not None:
            self.request_ids[key] = (scan_id, digest)
        self.uploads.append({"manifest": manifest, "files": sorted(files), "request_id": key})
        git = manifest.get("git")
        on_default = bool(
            git
            and git.get("ref")
            and git.get("default_branch")
            and git["ref"] in (git["default_branch"], "refs/heads/" + git["default_branch"])
        )
        self.files[scan_id] = files
        self.reads[scan_id] = 0
        self.scans[scan_id] = {
            "id": scan_id,
            "subject": {
                "id": str(uuid.uuid5(uuid.NAMESPACE_URL, subject["kind"] + "/" + subject["name"])),
                "kind": subject["kind"],
                "name": subject["name"],
                "source": "upload",
                "agent_id": subject.get("agent_id"),
                "created_at": _now(),
            },
            "content_digest": content_digest(files),
            "bundle": {
                "sha256": hashlib.sha256(archive).hexdigest(),
                "format": "zip",
                "size_bytes": len(archive),
                "file_count": len(files),
                "unpacked_bytes": sum(len(data) for data in files.values()),
            },
            "source": manifest.get("source", "api"),
            "state": "queued",
            "stage": None,
            "attempts": 0,
            "options": manifest.get("options", {}),
            "coverage": None,
            "counts": None,
            "error": None,
            "scanner_version": None,
            "git": git,
            "on_default_branch": on_default,
            "created_at": _now(),
            "started_at": None,
            "completed_at": None,
        }
        return _json(201, self.scans[scan_id])

    def _read(self, scan_id: str) -> dict[str, Any]:
        scan = self.scans[scan_id]
        self.reads[scan_id] += 1
        if scan["state"] not in ("completed", "failed"):
            if self.reads[scan_id] >= self.polls:
                self._end(scan_id)
            else:
                scan.update(state="running", stage="inventory", attempts=1, started_at=_now())
        files = self.files[scan_id]
        return {
            **scan,
            "coverage_statement": None,
            "inventory": [
                {"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "kind": "other"}
                for path, data in sorted(files.items())
            ]
            if scan["state"] == "completed"
            else None,
        }

    def _end(self, scan_id: str) -> None:
        scan = self.scans[scan_id]
        if self.fail:
            scan.update(
                state="failed",
                stage="unpack",
                attempts=3,
                error={"code": "unpack_failed", "message": "The bundle could not be unpacked"},
            )
            return
        files, findings = self.files[scan_id], self._findings(scan_id)
        severities = {f"S{level}": 0 for level in range(1, 6)}
        for finding in findings:
            severities[finding["severity"]] += 1
        scan.update(
            state="completed",
            stage="rules",
            attempts=1,
            completed_at=_now(),
            scanner_version="0.1.0",
            coverage={
                "files_read": len(files),
                "bytes_read": sum(len(data) for data in files.values()),
                "files_skipped": 0,
                "files_not_inspected": 0,
                "complete": True,
                "stages_run": ["inventory", "parse", "rules"],
                "levels_reached": 2,
            },
            counts={
                "by_category": {"instruction_integrity": len(findings)} if findings else {},
                "by_severity": severities,
                "by_evidence_level": {
                    str(level): sum(f["evidence_level"] == level for f in findings) for level in range(7)
                },
            },
        )

    def _findings(self, scan_id: str) -> list[dict[str, Any]]:
        if not self.seeded:
            return []
        skills = [path for path in sorted(self.files[scan_id]) if path.rsplit("/", 1)[-1] == "SKILL.md"]
        return [self._finding(scan_id, position, skill) for position, skill in enumerate(skills)]

    def _finding(self, scan_id: str, position: int, skill: str) -> dict[str, Any]:
        scan = self.scans[scan_id]
        fingerprint = "sha256:" + hashlib.sha256(f"{RULE}\0{skill}".encode()).hexdigest()
        return {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, scan_id + fingerprint)),
            "scan_id": scan_id,
            "subject_id": scan["subject"]["id"],
            "position": position,
            "rule_id": RULE,
            "category": "instruction_integrity",
            "severity": "S2",
            "reported_severity": "S2",
            "severity_source": "scanner",
            "evidence_level": 2,
            "evidence_level_name": "structural",
            "title": "Seeded finding for the scan fixture",
            "message": "The stand-in server reports this on every SKILL.md.",
            "path": skill,
            "line_start": 1,
            "line_end": 4,
            "excerpt": None,
            "reference_id": None,
            "fix": "Nothing to fix: this is a test finding.",
            "fingerprint": fingerprint,
            "match": "new",
            "status": "open",
            "triage": {
                "accepted": False,
                "reason": None,
                "by": None,
                "at": None,
                "accepted_until": None,
                "waiver": None,
            },
            "first_seen_scan_id": scan_id,
            "first_seen_at": scan["created_at"],
            "last_seen_scan_id": scan_id,
            "last_seen_at": scan["created_at"],
            "fixed_in_scan_id": None,
            "fixed_at": None,
            "evidence_count": 0,
            "gating": True,
        }

    def _sarif(self, scan_id: str) -> dict[str, Any]:
        findings = self._findings(scan_id)
        rules = (
            [
                {
                    "id": RULE,
                    "name": "SeededFixture",
                    "shortDescription": {"text": "Seeded finding"},
                    "help": {"text": "Nothing to fix: this is a test finding."},
                }
            ]
            if findings
            else []
        )
        results = [
            {
                "ruleId": finding["rule_id"],
                "level": "error",
                "message": {"text": finding["title"]},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": finding["path"]},
                            "region": {"startLine": finding["line_start"], "endLine": finding["line_end"]},
                        }
                    }
                ],
                "fingerprints": {"martianScannerFinding/v1": finding["fingerprint"]},
                "partialFingerprints": {"martianScannerFinding/v1": finding["fingerprint"].removeprefix("sha256:")},
                "baselineState": "new",
            }
            for finding in findings
        ]
        return {
            "version": "2.1.0",
            "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
            "runs": [
                {
                    "tool": {"driver": {"name": "Martian Scanner", "version": "0.1.0", "rules": rules}},
                    "results": results,
                }
            ],
        }


def serve(scanner: FakeScanner, port: int = 0) -> ThreadingHTTPServer:
    """Serve ``scanner`` on loopback; the caller runs ``serve_forever`` and ``shutdown``."""

    class Handler(BaseHTTPRequestHandler):
        def _answer(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            status, headers, payload = scanner.handle(self.command, self.path, dict(self.headers.items()), body)
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = _answer

        def log_message(self, format: str, *args: Any) -> None:
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port-file", type=Path, required=True, help="Write the port here once listening")
    parser.add_argument("--clean", action="store_true", help="Report no finding")
    parser.add_argument("--token", default=TOKEN)
    arguments = parser.parse_args()
    server = serve(FakeScanner(token=arguments.token, seeded=not arguments.clean))
    arguments.port_file.write_text(str(server.server_address[1]))
    server.serve_forever()


if __name__ == "__main__":
    main()
