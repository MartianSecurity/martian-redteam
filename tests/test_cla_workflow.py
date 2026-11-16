"""The Contributor License Agreement check, .github/workflows/cla.yml, and the agreements it links in cla/."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/cla.yml").read_text(encoding="utf-8"))
JOB = WORKFLOW["jobs"]["cla"]
SIGNATURE = "I have read the CLA Document and I hereby sign the CLA"
PUBLISHED = "https://github.com/MartianSecurity/martian-redteam/blob/main/"


def test_the_check_runs_on_pull_request_events_and_comments_only():
    # PyYAML reads the bare key `on` as True.
    assert WORKFLOW[True] == {
        "pull_request_target": {"types": ["opened", "closed", "synchronize"]},
        "issue_comment": {"types": ["created"]},
    }
    assert list(WORKFLOW["jobs"]) == ["cla"]
    condition = JOB["if"]
    assert "github.event.issue.pull_request &&" in condition
    assert f"github.event.comment.body == '{SIGNATURE}'" in condition
    assert "github.event.comment.body == 'recheck'" in condition


def test_the_write_token_never_reaches_the_pull_requests_code():
    # pull_request_target gives the job a write token, so its one step is the pinned action: no
    # checkout, no script, no other action.
    (step,) = JOB["steps"]
    assert set(step) == {"name", "uses", "env", "with"}
    assert re.fullmatch(r"contributor-assistant/github-action@[0-9a-f]{40}", step["uses"])
    assert step["env"] == {"GITHUB_TOKEN": "${{ secrets.GITHUB_TOKEN }}"}
    assert WORKFLOW["permissions"] == {}
    assert JOB["permissions"] == {"actions": "write", "contents": "write", "pull-requests": "write"}


def test_signatures_have_their_own_branch_and_the_allowlist_names_exact_accounts():
    inputs = JOB["steps"][0]["with"]
    assert inputs["branch"] == "cla-signatures"
    assert inputs["path-to-signatures"] == "signatures/cla.json"
    # The action's "*" is an unanchored pattern.
    assert inputs["allowlist"].split(",") == ["kloudd", "dependabot[bot]", "github-actions[bot]"]


def test_the_bot_links_agreements_in_this_repository_that_give_its_signature_line():
    inputs = JOB["steps"][0]["with"]
    assert inputs["path-to-document"] == PUBLISHED + "cla/individual.md"
    linked = [inputs["path-to-document"], *re.findall(r"\((https://[^)]+)\)", inputs["custom-notsigned-prcomment"])]
    assert len(linked) == 3, linked
    for url in linked:
        assert url.startswith(PUBLISHED), url
        assert (ROOT / url.removeprefix(PUBLISHED)).is_file(), url
    for document in ("cla/individual.md", "CONTRIBUTING.md"):
        assert SIGNATURE in (ROOT / document).read_text(encoding="utf-8"), document
