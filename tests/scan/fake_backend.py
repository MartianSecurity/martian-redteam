"""A stand-in for the backend's customer /v1/scanner routes, for `martian scan`'s tests and the scan
action's smoke job. Standard library only, so CI can run it with the runner's own ``python3``.

It speaks the shapes of ``tests/scan/contract/scanner-scans.json`` and, for the findings, check and
export routes the contract does not hold yet, the shapes the backend documents for them
(MartianSecurity/martian-notes#398, #400, #404). It holds no rule: with ``seeded`` it reports one fixed
finding on the first ``SKILL.md`` in the archive, so a gate can be seen failing.

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
ALL_ROUTES = frozenset({"findings", "check", "export"})
RULE = "SKL-SEEDED-FIXTURE"


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
        if method != "GET" or len(rest) < 2 or rest[0] != "scans" or rest[1] not in self.scans:
            return _not_found()
        scan_id = rest[1]
        if len(rest) == 2:
            return _json(200, self._read(scan_id))
        if len(rest) != 3 or rest[2] not in self.routes:
            return _not_found()
        scan = self.scans[scan_id]
        if rest[2] == "findings":
            return _json(200, {"items": self._findings(scan_id), "next_cursor": None})
        if scan["state"] != "completed":
            return _refused(409, "scan_not_completed", "The scan has not completed")
        if rest[2] == "check":
            blocking = [finding["id"] for finding in self._findings(scan_id) if finding["status"] == "open"]
            return _json(
                200,
                {
                    "scan_id": scan_id,
                    "passed": not blocking,
                    "coverage_shortfalls": [],
                    "blocking": blocking,
                    "counted": {"open": len(blocking)} if blocking else {},
                    "evaluated_at": _now(),
                },
            )
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
        scan_id = str(uuid.uuid4())
        self.uploads.append({"manifest": manifest, "files": sorted(files)})
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
        skill = next((path for path in sorted(self.files[scan_id]) if path.rsplit("/", 1)[-1] == "SKILL.md"), None)
        if skill is None:
            return []
        scan = self.scans[scan_id]
        fingerprint = "sha256:" + hashlib.sha256(f"{RULE}\0{skill}".encode()).hexdigest()
        return [
            {
                "id": str(uuid.uuid5(uuid.NAMESPACE_URL, scan_id + fingerprint)),
                "scan_id": scan_id,
                "subject_id": scan["subject"]["id"],
                "position": 0,
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
            }
        ]

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
