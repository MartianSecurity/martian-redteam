"""`martian scan` against a stand-in backend: the contract, the exit codes and what it prints
(MartianSecurity/martian-notes#468, #469, #470)."""

from __future__ import annotations

import copy
import json
import subprocess
import uuid
from pathlib import Path

import httpx
import pytest

from martian.scan import EXIT_ERROR, EXIT_GATE_FAILED, EXIT_PASSED, ScanOptions, run_scan
from tests.scan.fake_backend import TOKEN, FakeScanner
from tests.scan.helpers import Clock, ContractDrift, Transport, config, contract, factory

FIXTURE = Path(__file__).resolve().parents[2] / "examples" / "scan" / "fixture"


@pytest.fixture(autouse=True)
def outside_ci(monkeypatch):
    """The scan's source and default name read CI variables; a test that wants them sets them."""
    for name in ("CI", "GITHUB_ACTIONS", "GITHUB_REPOSITORY", "GITHUB_SHA", "GITHUB_REF", "GITHUB_RUN_ID",
                 "GITHUB_EVENT_PATH"):  # fmt: skip
        monkeypatch.delenv(name, raising=False)


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
    assert any(text.startswith("Gate: failed (exit 1); evaluation ") for text in printed.err)
    assert "  1 findings block" in printed.err
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
    assert {key: value for key, value in upload["manifest"].items() if key != "git"} == {
        "subject": {"kind": "repository", "name": "acme/agents"}, "source": "cli",
        "options": {"model_analysis": True}}  # fmt: skip
    assert uuid.UUID(upload["request_id"])
    assert scanner.policies == ["", ""]  # no .martian/scan.yml: the empty policy, validated then judged
    assert upload["files"] == [".mcp.json", "AGENTS.md", "skills/release-notes/SKILL.md",
                               "skills/release-notes/scripts/collect.sh"]  # fmt: skip


def test_a_clean_scan_passes(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/agents")
    scanner = FakeScanner(seeded=False)
    code, printed = _run(Transport(scanner), kind="skill", model_analysis=False)
    assert code == EXIT_PASSED
    assert "Findings: none reported." in printed.out and "  No finding blocks" in printed.out
    assert any(text.startswith("Gate: passed (exit 0)") for text in printed.out)
    assert {key: value for key, value in scanner.uploads[0]["manifest"].items() if key != "git"} == {
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
        "/v1/scanner/scans/{scan_id}/export",
        "/v1/scanner/scans/{scan_id}/gate",
        "/v1/scanner/gate-policy/validate",
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


def test_an_upload_that_gets_no_answer_is_retried_under_one_request_id_then_exits_2():
    posts = []

    def broken(request: httpx.Request):
        if request.url.path == "/v1/scanner/scans":
            posts.append(request.headers["idempotency-key"])
            raise httpx.ConnectError("refused", request=request)
        return None

    clock = Clock()
    code, printed = _run(Transport(FakeScanner(), before=broken), clock=clock)
    assert code == EXIT_ERROR and len(posts) == 4 and len(set(posts)) == 1
    assert f"no answer in 4 attempt(s) under request id {posts[0]}" in printed.text
    assert "Look for a scan of sha256:" in printed.text
    assert clock.slept == [0.5, 1.0, 2.0]


def test_an_answer_lost_after_the_scan_was_made_gives_the_same_scan_on_the_retry():
    scanner = FakeScanner()
    lost = []

    def drop_first_answer(request: httpx.Request):
        if request.url.path == "/v1/scanner/scans" and not lost:
            lost.append(scanner.handle("POST", "/v1/scanner/scans", dict(request.headers), request.content))
            raise httpx.ReadTimeout("no answer", request=request)
        return None

    code, printed = _run(Transport(scanner, before=drop_first_answer))
    assert code == EXIT_GATE_FAILED
    assert lost[0][0] == 201 and len(scanner.scans) == 1 and len(scanner.uploads) == 1
    assert "(the server already had this upload under its request id)" in printed.text


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_a_transient_status_on_the_upload_is_retried_with_the_same_request_id(status):
    keys = []

    def gateway(request: httpx.Request):
        if request.url.path == "/v1/scanner/scans":
            keys.append(request.headers["idempotency-key"])
            if len(keys) == 1:
                return httpx.Response(status, json={"detail": "upstream"}, headers={"Retry-After": "3"})
        return None

    scanner, clock = FakeScanner(), Clock()
    code, _ = _run(Transport(scanner, before=gateway), clock=clock)
    assert code == EXIT_GATE_FAILED and len(keys) == 2 and keys[0] == keys[1] and len(scanner.scans) == 1
    assert clock.slept[0] == 3.0


def test_a_request_id_the_server_says_was_reused_is_not_retried():
    posts = []

    def reused(request: httpx.Request):
        if request.url.path == "/v1/scanner/scans":
            posts.append(request)
            return httpx.Response(409, json={"detail": {"code": "request_id_reused", "message": "used"}})
        return None

    code, printed = _run(Transport(FakeScanner(), before=reused))
    assert code == EXIT_ERROR and len(posts) == 1
    assert "Server code: request_id_reused." in printed.text


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


def test_a_server_without_the_gate_exits_2_before_uploading():
    scanner = FakeScanner(routes=frozenset({"findings", "export"}))
    code, printed = _run(Transport(scanner))
    assert code == EXIT_ERROR and scanner.uploads == []
    assert "this server does not evaluate a gate policy file" in printed.text


def test_an_export_the_server_cannot_give_exits_2(tmp_path):
    code, printed = _run(Transport(FakeScanner(routes=frozenset({"findings", "gate"}))), sarif_path=tmp_path / "x")
    assert code == EXIT_ERROR
    assert "This server does not export a scan as SARIF" in printed.text
    assert not (tmp_path / "x").exists()


def test_a_404_that_is_not_a_missing_route_is_an_error():
    def gone(request: httpx.Request):
        if request.url.path.endswith("/gate"):
            return httpx.Response(404, json={"detail": "Scan not found"})
        return None

    code, printed = _run(Transport(FakeScanner(), before=gone))
    assert code == EXIT_ERROR and "[http_404]" in printed.text


def test_an_answer_that_is_not_a_gate_is_an_error():
    def odd(request: httpx.Request):
        if request.url.path.endswith("/gate"):
            return httpx.Response(201, json={"passed": "yes"})
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


# --- the policy file, the commit and the gate (MartianSecurity/martian-notes#470, #472) --------------


def _git(folder: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True, check=True)
    return done.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    """A Git repository on ``main`` with one committed skill and a policy file."""
    folder = tmp_path / "repo"
    (folder / "skills" / "one").mkdir(parents=True)
    (folder / "skills" / "one" / "SKILL.md").write_text("---\nname: one\n---\nOne.\n")
    (folder / ".martian").mkdir()
    (folder / ".martian" / "scan.yml").write_text("gate:\n  new_findings_only: true\n")
    _git(tmp_path, "init", "-q", "-b", "main", str(folder))
    for key, value in (("user.name", "Test"), ("user.email", "test@example.invalid"), ("commit.gpgsign", "false")):
        _git(folder, "config", key, value)
    _git(folder, "add", "-A")
    _git(folder, "commit", "-q", "-m", "one skill")
    return folder


def test_the_policy_file_is_checked_first_and_a_refused_one_uploads_nothing(tmp_path):
    folder = _repository(tmp_path)
    (folder / ".martian" / "scan.yml").write_text("gate:\n  fail_on:\n    severty: high  # refuse-me\n")
    scanner = FakeScanner()
    code, printed = _run(Transport(scanner), folder=folder)
    assert code == EXIT_ERROR and scanner.uploads == []
    assert "  gate.fail_on.severty: Extra inputs are not permitted" in printed.err
    assert "the server refused the policy file" in printed.text


def test_the_policy_files_paths_and_include_choose_what_is_sent(tmp_path):
    scanner = FakeScanner(seeded=False)
    scanner.policy["include"] = ["mcp", "harness-config"]
    code, printed = _run(Transport(scanner))
    assert code == EXIT_PASSED
    assert scanner.uploads[0]["files"] == [".mcp.json", "AGENTS.md"]
    assert "Left out by the policy file's include: 2 file(s)." in printed.out
    scanner = FakeScanner(seeded=False)
    scanner.policy["paths"] = ["skills"]
    _run(Transport(scanner))
    assert scanner.uploads[0]["files"] == ["skills/release-notes/SKILL.md", "skills/release-notes/scripts/collect.sh"]


def test_the_policy_text_is_what_the_server_judges(tmp_path):
    folder = _repository(tmp_path)
    text = (folder / ".martian" / "scan.yml").read_text()
    scanner = FakeScanner()
    _run(Transport(scanner), folder=folder)
    assert scanner.policies == [text, text]
    assert scanner.gates[0]["policy_sha256"] == __import__("hashlib").sha256(text.encode()).hexdigest()


def test_a_policy_file_that_is_a_link_is_refused(tmp_path):
    folder = _repository(tmp_path)
    (folder / ".martian" / "scan.yml").unlink()
    (folder / ".martian" / "scan.yml").symlink_to("/etc/hosts")
    code, printed = _run(Transport(FakeScanner()), folder=folder)
    assert code == EXIT_ERROR and "is not a regular file" in printed.text


def test_new_findings_only_passes_an_old_finding_and_fails_a_new_one(tmp_path):
    folder = _repository(tmp_path)
    scanner = FakeScanner()
    scanner.policy["gate"]["new_findings_only"] = True
    main = _git(folder, "rev-parse", "HEAD")
    code, printed = _run(Transport(scanner), folder=folder, default_branch="main")
    assert code == EXIT_GATE_FAILED  # the first scan of main is judged without a baseline
    assert scanner.uploads[0]["manifest"]["git"] == {"sha": main, "ref": "refs/heads/main", "default_branch": "main"}
    assert "Baseline: none: no completed scan of the default branch precedes this one." in printed.err
    _git(folder, "checkout", "-q", "-b", "feature")
    code, printed = _run(Transport(scanner), folder=folder, default_branch="main")
    assert code == EXIT_PASSED  # the same finding is in main's baseline
    baseline = next(iter(scanner.scans))
    assert f"Baseline: scan {baseline} of the default branch (refs/heads/main at {main[:12]})." in printed.text
    (folder / "skills" / "two").mkdir()
    (folder / "skills" / "two" / "SKILL.md").write_text("---\nname: two\n---\nTwo.\n")
    _git(folder, "add", "-A")
    _git(folder, "commit", "-q", "-m", "another skill")
    code, printed = _run(Transport(scanner), folder=folder, default_branch="main")
    assert code == EXIT_GATE_FAILED
    assert "  Blocking: SKL-SEEDED-FIXTURE at skills/two/SKILL.md" in printed.err
    assert scanner.gates[-1]["counts"]["in_baseline"] == 1


def test_files_that_are_not_the_commit_state_no_commit(tmp_path):
    folder = _repository(tmp_path)
    (folder / "skills" / "one" / "SKILL.md").write_text("changed\n")
    scanner = FakeScanner(seeded=False)
    code, printed = _run(Transport(scanner), folder=folder)
    assert code == EXIT_PASSED and "git" not in scanner.uploads[0]["manifest"]
    assert "Commit: not stated (1 of the files sent differ from commit" in printed.text
    outside = FakeScanner(seeded=False)
    _run(Transport(outside))  # the fixture is committed in this repository
    assert len(outside.uploads[0]["manifest"].get("git", {}).get("sha", "")) in (0, 40)


def test_a_run_that_must_be_of_a_commit_refuses_other_files(tmp_path):
    folder = _repository(tmp_path)
    scanner = FakeScanner()
    code, printed = _run(Transport(scanner), folder=folder, expect_sha="f" * 40)
    assert code == EXIT_ERROR and scanner.uploads == []
    assert "the files sent are not commit ffffffffffff" in printed.text


def test_a_failed_scan_is_not_judged_and_exits_2_with_its_evaluation():
    scanner = FakeScanner(fail=True)
    code, printed = _run(Transport(scanner))
    assert code == EXIT_ERROR and scanner.gates[0]["verdict"] == "not_judged"
    assert any(text.startswith("Gate: not_judged (exit 2)") for text in printed.err)
