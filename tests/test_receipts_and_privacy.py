"""Receipt integrity and report privacy are release-gate boundaries."""

import json
import uuid

import pytest

from martian.evals.client import BackendError
from martian.evals.receipt import session_receipt
from martian.redteam import RiskAssessment, TestCase, Turn


@pytest.mark.parametrize("change", ["missing", "count", "duplicate", "false_pass"])
def test_incomplete_or_inconsistent_acknowledgment_cannot_pass(change):
    tests = [{"name": "eval", "kind": "checks"}, {"name": "red", "kind": "redteam"}]
    response = {
        "session_id": str(uuid.uuid4()),
        "verdict": "passed",
        "test_count": 2,
        "passed_count": 2,
        "tests": [{**t, "passed": True} for t in tests],
    }
    if change == "missing":
        response["tests"].pop()
    elif change == "count":
        response["passed_count"] = 0
    elif change == "duplicate":
        response["tests"][1] = response["tests"][0]
    else:
        response["tests"][0]["passed"] = False
        response["passed_count"] = 1
    with pytest.raises(BackendError, match="incomplete or inconsistent"):
        session_receipt(response, tests)


def test_report_content_is_opt_in_and_credential_redaction_cannot_be_disabled(tmp_path, monkeypatch):
    secret = "test-private-provider-credential"
    monkeypatch.setenv("MARTIAN_REDTEAM_API_KEY", secret)
    report = RiskAssessment(
        catalog_version="v1",
        purpose="private-purpose",
        test_cases=[
            TestCase(
                vulnerability="prompt_leakage",
                vulnerability_type="system_prompt",
                attack_method="prompt_injection",
                input="private-input",
                actual_output="private-output " + secret,
                reason="private-reason",
                error="private-exception " + secret,
                error_kind="target",
                turns=[Turn(role="ai", content="private-turn")],
            )
        ],
    )
    path = report.save(tmp_path)
    safe = path.read_text()
    for value in (
        secret,
        "private-purpose",
        "private-input",
        "private-output",
        "private-reason",
        "private-turn",
        "private-exception",
    ):
        assert value not in safe
    report.save(tmp_path, include_target_content=True)
    included = path.read_text()
    assert "private-output" in included
    assert secret not in included
    monkeypatch.setenv("MARTIAN_EVALS_REDACT_OUTPUTS", "1")
    data = json.loads(report.for_report(include_target_content=True).model_dump_json())
    assert data["test_cases"][0]["reason"].startswith("sha256:")
    assert data["test_cases"][0]["turns"][0]["content"].startswith("sha256:")
