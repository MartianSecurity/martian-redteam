# Releasing the SDK

The canonical distribution is `martian-sdk`; source stays in the
`MartianSecurity/martian-redteam` repository. Read the version to release from
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
uv run --all-extras python scripts/sbom.py wheel
docker build -t ghcr.io/martiansecurity/martian-evals:<version> .
uv run --all-extras python scripts/check_image.py --image ghcr.io/martiansecurity/martian-evals:<version>
uv run --all-extras python scripts/sbom.py image --image ghcr.io/martiansecurity/martian-evals:<version>
```

The two `sbom.py` commands write the [bills of materials](#bills-of-materials-and-the-licence-check)
into `release-sbom/` and stop on a licence outside the allowed list. `docker build` and
`check_image.py` build [the local edition's image](#the-local-editions-image) from the wheel in
`dist/` and check it. `check_release.py` requires one wheel and one sdist, matching versions, Apache-2.0
metadata and license files, Python 3.12+, both modules, typed markers, CLI
entry points, the documented extras and the pinned Evals UI
([below](#the-evals-ui-in-the-wheel)). It rejects repository/direct-URL
runtime dependencies and private/generated source archive content. It installs
each distribution in a fresh environment, checks dependencies and imports from
outside the checkout using isolated Python, exercises the installed CLIs and
serves the packaged UI with `martian ui`.
`release-checksums.json` is a delivery manifest **outside** `dist/`; only the
two distributions go to the publisher. The release workflow retains the
manifest separately as `sdk-release-checksums`, alongside `sdk-distributions`. Rebuild after any code/docs change and
retain the checksums of the actual validated artifacts.

The base dependencies support redteam. `evals` adds MLflow-skinny 3.18.0/pandas/pytest,
its local SQL store prerequisites, and `zstandard` for reading Inspect AI's `.eval` logs.
`mcp` retains the optional MCP runtime.
CI checks Python 3.12 and 3.13. The sdist contains source (with the built Evals UI), tests, examples,
documentation, build configuration (with the `Dockerfile` and `.dockerignore`), lockfile and
license/notice; no task-owned credentials, raw live logs, local MLflow stores or browser auth
state are bundled.

## The local edition's image

One tagged release produces the distributions and the image of the local edition
([evals core](evals-core.md#the-local-edition-in-a-container)), and the image holds the wheel the
release publishes to PyPI:

- **Built from the wheel.** The `Dockerfile` installs the one wheel in `dist/` with the `evals`
  extra and the dependencies `uv.lock` pins. It never builds the package: `.dockerignore` lets in
  only `pyproject.toml`, `uv.lock` and `dist/*.whl`, so the image cannot hold a second build of
  the checkout. The release builds it after `check_release.py` has checked that wheel, as
  `ghcr.io/martiansecurity/martian-evals:<version>`, labelled with the version and the source commit.
- **The version check.** `scripts/check_image.py` compares the wheel in `dist/` with the image.
  It runs the image's Python, which reads the installed `martian-sdk` version and every file of
  the UI that `martian ui` serves, and requires the SDK version and the UI's version, commit,
  `dirty` flag and digest (`ui_digest`, as for the pin) to equal the wheel's. It then starts the
  image as it ships, with its port on 127.0.0.1, and requires `/` to be the Evals UI and
  `/ui-version.json` the wheel's stamp. It writes `image-release.json`: the image, its ID, the
  SDK version and the UI. Any difference stops the release.
- **Kept, then pushed.** The build job saves the checked image with `docker save` and keeps it
  with `image-release.json` as the `evals-image` artifact. The `publish-image` job loads that
  file, refuses it unless its ID is the one checked, and pushes it. Nothing in that job builds an
  image, so the registry gets the bytes that were checked. It pushes the version tag only; no
  `latest` tag is moved.

| Job | Permissions | Environment | What it does |
| --- | --- | --- | --- |
| `build` | `contents: read` | None | Checks, builds and keeps the distributions, the image and their bills of materials; publishes nothing |
| `publish` | `id-token: write` (OIDC for PyPI Trusted Publishing) | `pypi` | Uploads the two distributions to PyPI |
| `publish-image` | `packages: write` (the job's `GITHUB_TOKEN` pushes to GitHub Container Registry) | `ghcr` | Pushes the checked image, after `publish` succeeded |

No registry password or long-lived token is stored. **The owner approves the first real
publish.** Before dispatching a release with `publish: true` for a version that ships the image:

1. Create the `ghcr` environment under the repository's settings, with required reviewers and
   deployments limited to `main`. GitHub creates an environment a job names when it does not
   exist, without protection, so create it first.
2. Let the organisation's package settings allow the repository to create the container package.
   The first push creates `ghcr.io/martiansecurity/martian-evals`, linked to this repository by its
   `org.opencontainers.image.source` label, and private like the repository. Make it public at
   the open-source launch.

## The Evals UI in the wheel

`martian ui` serves the Evals UI that the wheel ships in `martian/ui/`. It is a build of the
stand-alone Evals app in MartianSecurity/MartianFrontend (`packages/evals/app`, `npm run build:evals`),
copied into `src/martian/ui/` and committed here. A build from a checkout or from the sdist
therefore needs no Node.js, no network and no access to that private repository, and the release
workflow builds exactly what the tag holds. The frontend build is reproducible: the same commit
and lockfile give the same files.

- **Identity.** The build writes `ui-version.json`: the Evals package's `version`, the frontend
  `commit` it was built from, and `dirty`, true when that tree had uncommitted changes. The
  `[tool.martian.ui]` table in `pyproject.toml` pins the `version`, the `commit` and a `digest`,
  SHA-256 over every file of the build (`ui_digest` in `scripts/check_release.py`). The release
  workflow writes the pinned commit into its summary.
- **Once.** The UI is part of the `martian` package, so the wheel holds it once, in `martian/ui/`,
  and the sdist once, in `src/martian/ui/`. `.gitattributes` keeps those files byte for byte (no
  line-ending conversion), out of diffs and merges, and marked as generated.
- **The check.** `scripts/check_release.py` refuses a wheel or sdist that holds a file twice, holds
  no UI or two, or whose UI is not the pinned build: a dirty stamp, another version or commit, or
  a file added, removed or changed. With `--install` it starts each clean install's `martian ui`
  without `--ui-dir` and reads `/`, `/ui-version.json`, the session call and the bootstrap.
  `tests/test_packaged_ui.py` checks the committed copy against the pin on every pull request.
- **Without it.** If an install has no `martian/ui/index.html`, `martian ui` stops with exit code
  `2` and says so; `--ui-dir` names another build.

To ship another UI, build the frontend commit to ship, on its `main`, from a clean checkout, and
copy the build in:

```bash
# In MartianSecurity/MartianFrontend, at the commit to ship:
git status --porcelain     # prints nothing
npm ci
npm run build:evals        # -> dist-evals/
# Here:
uv run python scripts/vendor_ui.py <frontend>/dist-evals
git add src/martian/ui pyproject.toml
```

`vendor_ui.py` refuses a build from a dirty tree, replaces `src/martian/ui/` and rewrites the pin.
A reviewer can rebuild the pinned commit the same way and run
`uv run python scripts/vendor_ui.py --check <frontend>/dist-evals`, which writes nothing and names
any file that differs.

A new build can call a route the core does not serve yet. `tests/evals/test_local.py` reads every
`/v1` path the vendored build's JavaScript names and fails when the local edition does not serve
one, or serves one of the gaps it lists (`UI_ROUTES_NOT_SERVED`; the pages they break are in
[the local edition](evals-core.md#the-local-edition-martian-ui)). It reads paths, not pages, so also
open each page with `martian ui --data-dir <a scratch folder>` and look for failed `/v1` requests
before committing the build.

**Size.** Installed, the UI is about 1.1 MB in 43 files: JavaScript, CSS, the Inter font files
(about 220 KB) and `third-party-licenses.md` (about 50 KB). Compressed, it adds about 470 KB to the
wheel and about 460 KB to the sdist. `ls -l dist` after `uv build` shows the current figures.

**Licences.** The build's own code is the `@martian/evals`, `@martian/design-system` and
`@martian/client` packages of the frontend, which are Apache-2.0; `NOTICE` attributes them, with the
MIT notice of the shadcn/ui code in the design system. `third-party-licenses.md` in the build holds
the licences of the third-party packages bundled in it: MIT, ISC and Apache-2.0, and OFL-1.1 for the
Inter font. The wheel's bill of materials lists the UI under Apache-2.0 and those packages from
that file ([below](#bills-of-materials-and-the-licence-check)). The image ships the same UI.

## Bills of materials and the licence check

Each release writes two CycloneDX 1.6 JSON bills of materials into `release-sbom/`, and the
release workflow keeps them as the `release-sbom` artifact:

| File | What it lists | How |
| --- | --- | --- |
| `martian-sdk-<version>.cdx.json` | The wheel with the `evals` extra (the server and the worker): the SDK, each Python dependency at the version `uv.lock` pins, and the Evals UI in `martian/ui/` with the npm packages bundled in it | `sbom.py wheel` installs the one wheel in `dist/` as the `Dockerfile` does, in a fresh Python 3.12 environment, and reads every installed distribution's metadata and the UI's `third-party-licenses.md` |
| `martian-evals-image-<version>.cdx.json` | The local edition's image: the same, read from inside the image, plus its CPython interpreter, the base image's Python packages and its Debian packages | `sbom.py image` runs the same probe with the image's Python, with no network |

Each component has its name, version, package URL and licence: the SPDX expression its metadata
declares (`acknowledgement: declared`), or the one the policy concluded after reading its licence
file (`concluded`, with the declared text in the `martian:declared-licence` property). The
`martian:licence-policy` property records how the check treated it. A Debian package lists the
`License:` names of its machine-readable copyright file, which are Debian's names, not SPDX. The
lists cover the `evals` extra only; the `mcp` and `postgres` extras are not in either.

**The check.** Both commands hold every third-party component to `scripts/licence-policy.json` and
exit with code 1 after writing the file when one fails. In the release workflow the wheel's check
runs before the image is built and the image's before anything is kept, so either stops the release.
CI's `packaging` job runs `sbom.py wheel` on every pull request. The policy has, in one file:

- **`allowed`**: the SPDX licences we ship: 0BSD, Apache-2.0, BSD-2-Clause, BSD-3-Clause, BSL-1.0,
  CC0-1.0, ISC, MIT, MIT-0, MPL-2.0, PSF-2.0, Python-2.0, Unlicense and Zlib. An expression passes
  when one side of each `OR` and both sides of each `AND` are allowed; a licence `WITH` an exception
  passes when the licence does. GPL, LGPL, AGPL, SSPL, any other licence, a `LicenseRef-`, a
  licence the metadata does not name (such as "BSD License", which names no variant) and no
  licence all fail.
- **`first_party`**: `martian-sdk` and the `@martian/*` packages of the Evals UI, which are ours and
  not held to the allowed list. The `@martian/*` entry names Apache-2.0: the build's
  `ui-version.json` names no licence, so the bill records the UI under that one, and a `@martian/*`
  component that declared another licence would fail.
- **`concluded`**: a component whose metadata does not name an SPDX licence, with the licence read
  from its licence file. It applies only while the metadata declares the text it was reviewed
  against, and the concluded licence must itself be allowed. Today: `gitdb`, `pyasn1-modules` and
  `sqlparse` (metadata "BSD", no variant) and the image's CPython (no metadata).
- **`exceptions`**: one component accepted under one licence outside the list, with the reason.
  Today the only one is the Inter font, `@fontsource-variable/inter`, under OFL-1.1.
- **`not_checked`**: the Debian packages of the image's base image (`deb:*`). They are listed, but
  many are GPL or LGPL system programs and libraries that Debian distributes; they are not held to
  the allowed list.

A new dependency under another licence fails the check. Change the dependency, or add a reviewed
entry with its reason to the policy in the same pull request. MartianSecurity/MartianFrontend runs the
same check on its builds with the same allowed list, in its own `scripts/licence-policy.json`;
change both together.

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
| GitHub owner | `MartianSecurity` |
| Repository | `martian-redteam` |
| Workflow filename | `release.yml` |
| Environment | `pypi` |

Honor the GitHub `pypi` environment's configured protection and restricted refs.
Publication is explicitly dispatched from reviewed `main`; any configured
reviewer gate still applies. Do not silently weaken controls or treat a past
plan limitation as permanent authorization. The workflow filename/environment
must match the publisher. No PyPI password or long-lived API token belongs in
GitHub secrets. Only the isolated publish job has `id-token: write`, and only `publish-image`
has `packages: write`.

After the implementation is reviewed/committed and real model qualification
passes, a maintainer can create an immutable `v<version>` tag matching the canonical
version. The workflow checks out the fully qualified `refs/tags/<tag>`, verifies
the peeled tag commit equals HEAD and belongs to `origin/main`, and records
the source commit in its summary. A branch with the same name is not a release.
A tag push builds/checks and retains the distributions, the image and their bills of materials; it does **not**
publish. To publish the reviewed tag, explicitly dispatch **SDK release** with
that tag and `publish: true` **from `main`**, satisfying the `pypi` and `ghcr` environments'
protection. The workflow rebuilds/validates the tag, clean-installs both artifacts, builds and
checks the image, publishes the distributions through SHA-pinned `pypa/gh-action-pypi-publish`
and then pushes the checked image. Actual publication requires an explicit release request.

Check PyPI's receipt/version and independently install the published package
before announcing it. Create the matching GitHub release at that immutable tag
with the same publishing run's wheel, sdist, checksum manifest and the two bills of materials
from `release-sbom`, and name the image digest the `publish-image` job's summary records. Verify asset
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
