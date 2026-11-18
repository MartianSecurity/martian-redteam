"""`martian scan` against a stand-in backend: the contract, the exit codes and what it prints
(MartianSecurity/martian-notes#468, #469)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import httpx
import pytest

from martian.scan import EXIT_ERROR, EXIT_GATE_FAILED, EXIT_PASSED, ScanOptions, run_scan
from tests.scan.fake_backend import TOKEN, FakeScanner
from tests.scan.helpers import Clock, ContractDrift, Transport, config, contract, factory

FIXTURE = Path(__file__).resolve().parents[2] / "examples" / "scan" / "fixture"


class Printed:
    def __init__(self) -> None:
        self.out: list[str] = []
        self.err: list[str] = []

    def __call__(self, text: str = "", err: bool = False) -> None:
        (self.err if err else self.out).append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.out + self.err)


def _run(transport, tmp_path=None, clock=None, **options):
    printed = Printed()
    code = run_scan(
        ScanOptions(folder=options.pop("folder", FIXTURE), **options),
        echo=printed,
        config=config(),
        client_factory=factory(transport, clock),
    )
    return code, printed


def test_a_seeded_finding_fails_the_gate_with_file_line_and_level(tmp_path):
    scanner = FakeScanner()
    transport = Transport(scanner)
    sarif, export = tmp_path / "out" / "scan.sarif", tmp_path / "out" / "scan.json"
    code, printed = _run(transport, sarif_path=sarif, json_path=export, name="acme/agents")
    assert code == EXIT_GATE_FAILED
    assert "Findings: 1" in printed.out
    line = next(text for text in printed.out if "SKL-SEEDED-FIXTURE" in text)
    assert "S2" in line and "level 2 (structural)" in line and "skills/release-notes/SKILL.md:1-4" in line
    assert "Gate: failed: 1 blocking finding(s)." in printed.err
    assert "Coverage: 4 files read; 0 skipped; 0 not inspected; evidence levels reached: 0 to 2" in printed.text
    # The exports are written as the server sent them, also when the gate fails.
    scan_id = next(iter(scanner.scans))
    assert (
        sarif.read_bytes()
        == scanner.handle(
            "GET", f"/v1/scanner/scans/{scan_id}/export?format=sarif", {"authorization": f"Bearer {TOKEN}"}, b""
        )[2]
    )
    assert json.loads(export.read_text())["format"] == "martian-scanner-export"
    upload = scanner.uploads[0]
    assert upload["manifest"] == {"subject": {"kind": "repository", "name": "acme/agents"}, "source": "cli",
                                  "options": {"model_analysis": True}}  # fmt: skip
    assert upload["files"] == [".mcp.json", "AGENTS.md", "skills/release-notes/SKILL.md",
                               "skills/release-notes/scripts/collect.sh"]  # fmt: skip


def test_a_clean_scan_passes(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/agents")
    scanner = FakeScanner(seeded=False)
    code, printed = _run(Transport(scanner), kind="skill", model_analysis=False)
    assert code == EXIT_PASSED
    assert "Findings: none reported." in printed.out and "Gate: passed." in printed.out
    assert scanner.uploads[0]["manifest"] == {
        "subject": {"kind": "skill", "name": "acme/agents"},
        "source": "ci",
        "options": {"model_analysis": False},
    }


def test_every_route_the_client_calls_is_in_the_contract(tmp_path):
    transport = Transport(FakeScanner())
    _run(transport, sarif_path=tmp_path / "s.sarif", json_path=tmp_path / "s.json")
    assert {route for _, route in transport.routes} == {
        "/v1/scanner/scans",
        "/v1/scanner/scans/{scan_id}",
        "/v1/scanner/scans/{scan_id}/findings",
        "/v1/scanner/scans/{scan_id}/check",
        "/v1/scanner/scans/{scan_id}/export",
    }


def test_a_route_that_drifts_from_the_contract_fails():
    drifted = copy.deepcopy(contract())
    scan = drifted["components"]["schemas"]["ScanOut"]
    scan["properties"]["digest"] = scan["properties"].pop("content_digest")
    scan["required"] = ["digest" if name == "content_digest" else name for name in scan["required"]]
    scan["additionalProperties"] = False
    with pytest.raises(ContractDrift, match="does not match the contract"):
        _run(Transport(FakeScanner(), document=drifted))
    renamed = copy.deepcopy(contract())
    renamed["paths"]["/v1/scanner/uploads"] = renamed["paths"].pop("/v1/scanner/scans")
    with pytest.raises(ContractDrift, match="is not a route of the contract"):
        _run(Transport(FakeScanner(), document=renamed))
    manifest = copy.deepcopy(contract())
    upload = manifest["paths"]["/v1/scanner/scans"]["post"]["requestBody"]["content"]["multipart/form-data"]
    upload["schema"]["properties"]["manifest"]["properties"]["source"]["enum"] = ["console", "api"]
    with pytest.raises(ContractDrift, match="the manifest does not match"):
        _run(Transport(FakeScanner(), document=manifest))


def test_a_transport_error_on_the_upload_exits_2_and_is_not_sent_again():
    posts = []

    def broken(request: httpx.Request):
        if request.method == "POST":
            posts.append(request)
            raise httpx.ConnectError("refused", request=request)
        return None

    code, printed = _run(Transport(FakeScanner(), before=broken))
    assert code == EXIT_ERROR and len(posts) == 1
    assert "whether the server queued a scan is unknown" in printed.text
    assert "Look for a scan of sha256:" in printed.text


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_a_gateway_status_on_the_upload_is_not_sent_again(status):
    posts = []

    def gateway(request: httpx.Request):
        if request.method == "POST":
            posts.append(request)
            return httpx.Response(status, json={"detail": "upstream"})
        return None

    code, printed = _run(Transport(FakeScanner(), before=gateway))
    assert code == EXIT_ERROR and len(posts) == 1
    assert "it was not sent again" in printed.text


def test_a_refused_upload_exits_2_with_the_servers_code_and_limit_only():
    scanner = FakeScanner(archive_limit=10)
    code, printed = _run(Transport(scanner))
    assert code == EXIT_ERROR
    assert "[http_413]" in printed.text and "Server code: archive_too_large (limit 10)." in printed.text
    assert "The archive is larger than the limit" not in printed.text


def test_a_key_without_the_scanner_is_told_what_it_needs():
    def forbidden(request: httpx.Request):
        return httpx.Response(403, json={"detail": "The Community edition does not include the Scanner"})

    code, printed = _run(Transport(FakeScanner(), before=forbidden))
    assert code == EXIT_ERROR
    assert "Permission denied" in printed.text and "scan:run and scan:read" in printed.text
    assert "Community edition" not in printed.text


def test_reads_are_retried_on_transient_statuses():
    failures = iter([503, 429, None, 502])

    def flaky(request: httpx.Request):
        if request.method == "GET" and request.url.path.count("/") == 4:
            status = next(failures, None)
            if status is not None:
                return httpx.Response(status, headers={"Retry-After": "2"})
        return None

    clock = Clock()
    code, _ = _run(Transport(FakeScanner(), before=flaky), clock=clock)
    assert code == EXIT_GATE_FAILED
    assert 2.0 in clock.slept


def test_a_scan_that_does_not_end_in_time_exits_2():
    clock = Clock()
    code, printed = _run(Transport(FakeScanner(polls=10_000)), clock=clock, timeout_seconds=30)
    assert code == EXIT_ERROR
    assert "was still running after 30 seconds. It is not a result" in printed.text
    assert clock.now <= 30


def test_a_server_that_stays_down_while_polling_exits_2():
    def down(request: httpx.Request):
        if request.method == "GET":
            raise httpx.ConnectError("down", request=request)
        return None

    clock = Clock()
    code, printed = _run(Transport(FakeScanner(), before=down), clock=clock, timeout_seconds=60)
    assert code == EXIT_ERROR and "Could not reach Martian" in printed.text
    assert clock.now <= 60


def test_a_failed_scan_exits_2():
    code, printed = _run(Transport(FakeScanner(fail=True)))
    assert code == EXIT_ERROR
    assert "failed (unpack_failed: The bundle could not be unpacked). It is not a result." in printed.text


def test_a_server_without_the_finding_routes_exits_on_the_scans_state():
    code, printed = _run(Transport(FakeScanner(routes=frozenset())))
    assert code == EXIT_PASSED
    assert "Findings: this server does not list a scan's findings." in printed.out
    assert "does not evaluate a scan gate, so the exit code is the scan's state: completed" in printed.text


def test_an_export_the_server_cannot_give_exits_2(tmp_path):
    code, printed = _run(Transport(FakeScanner(routes=frozenset({"findings", "check"}))), sarif_path=tmp_path / "x")
    assert code == EXIT_ERROR
    assert "This server does not export a scan as SARIF" in printed.text
    assert not (tmp_path / "x").exists()


def test_a_404_that_is_not_a_missing_route_is_an_error():
    def gone(request: httpx.Request):
        if request.url.path.endswith("/check"):
            return httpx.Response(404, json={"detail": "Scan not found"})
        return None

    code, printed = _run(Transport(FakeScanner(), before=gone))
    assert code == EXIT_ERROR and "[http_404]" in printed.text


def test_an_answer_that_is_not_a_gate_is_an_error():
    def odd(request: httpx.Request):
        if request.url.path.endswith("/check"):
            return httpx.Response(200, json={"passed": "yes"})
        return None

    code, printed = _run(Transport(FakeScanner(), before=odd))
    assert code == EXIT_ERROR and "invalid response" in printed.text


def test_content_other_than_was_packed_is_an_error():
    scanner = FakeScanner()
    original = scanner._upload

    def other(headers, body):
        status, answer_headers, answer = original(headers, body)
        scan = json.loads(answer)
        scan["content_digest"] = "sha256:" + "0" * 64
        scanner.scans[scan["id"]]["content_digest"] = scan["content_digest"]
        return status, answer_headers, json.dumps(scan).encode()

    scanner._upload = other  # type: ignore[method-assign]
    code, printed = _run(Transport(scanner))
    assert code == EXIT_ERROR and "not the sha256:" in printed.text


def test_server_text_cannot_write_escape_sequences_or_the_token():
    scanner = FakeScanner()
    original = scanner._findings

    def hostile(scan_id):
        found = original(scan_id)
        for finding in found:
            finding["title"] = f"\x1b[2J\x1b]0;owned\x07 leaked Bearer {TOKEN}\nsecond line"
        return found

    scanner._findings = hostile  # type: ignore[method-assign]
    code, printed = _run(Transport(scanner))
    assert code == EXIT_GATE_FAILED
    assert "\x1b" not in printed.text and "\x07" not in printed.text and TOKEN not in printed.text
    assert "second line" in next(text for text in printed.out if "SKL-SEEDED-FIXTURE" in text)


def test_a_dry_run_lists_the_files_and_needs_no_token(monkeypatch):
    monkeypatch.delenv("MARTIAN_API_TOKEN", raising=False)
    monkeypatch.delenv("MARTIAN_API_URL", raising=False)
    printed = Printed()
    assert run_scan(ScanOptions(folder=FIXTURE, dry_run=True), echo=printed) == EXIT_PASSED
    assert "  skills/release-notes/SKILL.md" in printed.out and "Dry run: nothing was uploaded." in printed.out


def test_no_token_exits_2(monkeypatch):
    monkeypatch.delenv("MARTIAN_API_TOKEN", raising=False)
    monkeypatch.setenv("MARTIAN_API_URL", "https://backend.example")
    printed = Printed()
    assert run_scan(ScanOptions(folder=FIXTURE), echo=printed) == EXIT_ERROR
    assert "missing MARTIAN_API_TOKEN" in printed.text and "scan:run and scan:read" in printed.text
