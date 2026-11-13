# Releasing the SDK

The canonical distribution is `martian-sdk`; source stays in the
`MorningHQ/martian-redteam` repository. Read the version to release from
`src/martian/_version.py` and verify existing Git tags and PyPI versions before
publishing. Backward-compatible fixes use a patch bump; new functionality uses
a minor bump. A release request authorizes only its identified repositories
and version, not deployment of the hosted services.

## Name and ownership

`martian-sdk` is an existing PyPI project. Reuse the configured Trusted Publisher;
do not recreate account trust for each version. Confirm the project, current
versions, publisher, and maintainer access before publishing.

## Build and inspect

```bash
uv sync --locked --all-extras
uv run --all-extras ruff format --check src tests examples scripts
uv run --all-extras ruff check src tests examples scripts
uv run --all-extras mypy src
uv run --all-extras pytest -q
uv build
uv run --all-extras twine check --strict dist/*
uv run --all-extras python scripts/check_release.py --install
```

The guard requires one wheel and one sdist, matching versions, Apache-2.0
metadata and license files, Python 3.12+, both modules, typed markers, CLI
entry points and the documented extras. It rejects repository/direct-URL
runtime dependencies and private/generated source archive content. It installs
each distribution in a fresh environment, checks dependencies and imports from
outside the checkout using isolated Python, and exercises the installed CLIs.
`release-checksums.json` is a delivery manifest **outside** `dist/`; only the
two distributions go to the publisher. The release workflow retains the
manifest separately as `sdk-release-checksums`, alongside `sdk-distributions`. Rebuild after any code/docs change and
retain the checksums of the actual validated artifacts.

The base dependencies support redteam. `evals` adds MLflow 3.16.1/pandas/pytest
and its local SQL store prerequisites. `mcp` retains the optional MCP runtime.
CI checks Python 3.12 and 3.13. The sdist contains source, tests, examples,
documentation, build configuration, lockfile and license/notice; no task-owned
credentials, raw live logs, local MLflow stores or browser auth state are bundled.

## Trusted Publishing setup

Prepare each version on `release/<version>` and open
a PR into `main`. PyPI consumes package metadata and does not require a branch
name. After review and required CI, merge the release PR and tag the reviewed
commit `v<version>`. Tags use the canonical PEP 440 version without a local
version label; do not move a published tag or reuse a published version.

A maintainer with authority over the PyPI project should configure:

| Field | Value |
| --- | --- |
| Project | `martian-sdk` |
| GitHub owner | `MorningHQ` |
| Repository | `martian-redteam` |
| Workflow filename | `release.yml` |
| Environment | `pypi` |

Honor the GitHub `pypi` environment's configured protection and restricted refs.
Publication is explicitly dispatched from reviewed `main`; any configured
reviewer gate still applies. Do not silently weaken controls or treat a past
plan limitation as permanent authorization. The workflow filename/environment
must match the publisher. No PyPI password or long-lived API token belongs in
GitHub secrets. Only the isolated publish job has `id-token: write`.

After the implementation is reviewed/committed and real model qualification
passes, a maintainer can create an immutable `v<version>` tag matching the canonical
version. The workflow checks out the fully qualified `refs/tags/<tag>`, verifies
the peeled tag commit equals HEAD and belongs to `origin/main`, and records
the source commit in its summary. A branch with the same name is not a release.
A tag push builds/checks and retains distributions; it does **not**
publish. To publish the reviewed tag, explicitly dispatch **SDK release** with
that tag and `publish: true` **from `main`**, satisfying the `pypi` environment's protection.
The workflow rebuilds/validates the tag, clean-installs both artifacts and
publishes them through SHA-pinned `pypa/gh-action-pypi-publish`. Actual publication
requires an explicit release request.

Check PyPI's receipt/version and independently install the published package
before announcing it. Create the matching GitHub release at that immutable tag
with the same publishing run's wheel, sdist, and checksum manifest. Verify asset
digests after upload. Do not rebuild a different set of files for GitHub.
The legacy metadata-only `martian-evals-kit` package
requires a matching available SDK version; publish its compatibility update
only after the canonical distribution exists. Do not release another copy of
the old eval engine.

## Customer GitHub Actions

[The combined example](../examples/combined-ci.yml) installs the delivered,
hash-checked wheel from the customer's reviewed vendor directory, so testing
it can be tested before a version is published.
Copy the wheel and `release-checksums.json` into `ci/vendor/`, add the customer's
agent dependencies/real callback and eval tests, and configure the Backend origin and one scoped API token. For a published version the install step can use an
exact released `martian-sdk[evals]` version instead. The workflow excludes local
MLflow stores and uses only safe result artifacts. Hosted workflow execution
still requires customer repository setup and credentials; local validation is
reported separately.

## Qualification and remaining release operations

Use [the validation guide](validation.md) to qualify actual behavior; the existence
of an artifact is not runtime evidence. Retain sanitized run receipts and
independent readback with the release review, outside source distributions.
Historical two-credential runs do not qualify the single-key contract.
Repeat affected checks when contracts change. Hosted tenant
provisioning, provider entitlement, per-key quotas and retention/AUP remain
service-owner responsibilities. No credential-dependent check is replaced by
a synthetic RiskAssessment or an HTTP fixture.

Primary references: [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/using-a-publisher/),
[official publisher action](https://github.com/pypa/gh-action-pypi-publish).
