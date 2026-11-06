# SDK validation

Unit tests, installed-package checks and service/model qualification cover
different boundaries. Release reviews must identify the checks and exact
source/artifact identities used. Keep generated reports, checksums, screenshots,
credentials and raw model payloads outside source commits.

## Reproducible source and package checks

Run the same commands as repository CI from a clean checkout:

```bash
uv sync --locked --all-extras
uv run --all-extras ruff format --check src tests examples scripts
uv run --all-extras ruff check src tests examples scripts
uv run --all-extras mypy src
uv run --all-extras pytest -q
uv run --all-extras python -m compileall -q src
uv build
uv run --all-extras twine check --strict dist/*
uv run --all-extras python scripts/check_release.py --install
```

CI runs tests on Python 3.12 and 3.13. Package checks install the wheel and source
distribution into separate clean environments, verify dependencies, import the
public and compatibility namespaces outside the checkout and exercise the CLIs.
The generated `release-checksums.json` identifies artifacts; it is an output,
not a source file. See [release preparation](releasing.md) for tag and publisher
requirements. A tag build cannot publish without an explicit publishing dispatch.

## Actual single-key qualification

Use an authorized test tenant and real Backend, Agents, PostgreSQL, Redis and
organization-bound MLflow. Install the built wheel in a fresh customer environment
outside the checkout. Supply only `MARTIAN_API_URL` and `MARTIAN_API_TOKEN` for
Martian authentication; remove direct-Agents and MLflow service credentials.
Persisted CI also needs agent and Git attribution. Customer provider credentials
stay local; see [provider setup](customer-integration.md#target-provider-setup).

Verify these entry points with actual generation, target responses and judging:

- Standalone Python `Client` / `a_red_team`, and `martian redteam run` YAML.
- Python eval CI, redteam CLI CI, public Python pytest gate and combined CI.
- One persisted combined session with both test kinds and honest scoring
  provenance, plus independent Backend and native MLflow readback.
- Console login, one-time key creation/copy, secret removal on close and query
  navigation, metadata/CI result refresh and key revocation.

Exercise deliberately wrong answers, finite target timeout, callback error,
invalid/revoked key and required-upload failure. Missing probes, errors and
unacknowledged required uploads must not produce a passing gate. Exact-match
scores are Backend-owned; uploaded redteam/check judgments remain client-reported
evidence. Native uploaded spans must say `client-reported` and
`client_executed_server_scored`, not imply a server-observed target request.

Verify one-POST/same-ID recovery after an actual lost response, cross-process
budget/admission races, and cancellation while a physical model call is active.
The reservation must remain held until physical settlement; cancellation must
not return a passing result. Observe the same scoped operation through a second
native API process. Inspect native run/thread/checkpoint and Redis deletion
after expiry, including with Backend offline. Capture Redis presence before
proving deletion, and verify public 410 after purge. Revocation must deny the
next request before admission.

Retain sanitized receipts, exact source/artifact hashes and independent storage
readbacks with the release review. Loopback stubs and constructed assessments
establish contracts; they do not replace real service/model qualification.

## Hosted activation boundary

Local qualification does not prove production connectivity, provider entitlement,
capacity or retained-volume locking across hosted nodes. Deploy coordinated
Backend/Agents schemas and images, provision the dedicated internal signer and
qualify actual shared custody before enabling customer redteam admission.
Initialize/migrate empty Aegra state once before scaling API replicas; Aegra 0.11
has a concurrent first-boot system-assistant seed race. Publication and deployment
are separate authorized operations.
