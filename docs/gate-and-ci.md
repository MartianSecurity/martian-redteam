# Gate and CI

This package is an **internal scaffold**. Do not point untrusted CI at the shared staging API key until martian-agents exposes dedicated `simulator` / `evaluator` graphs **and** per-key identity with quotas. Taxonomy IDs are Apache-2.0 (DeepTeam-derived; see `NOTICE`). This SDK is not a DeepTeam fork and does not implement Confident AI’s remote gate.

The local gate is fail-closed. DeepTeam’s SDK default `ignore_errors=True` is **not** copied. CI must not inherit a “keep going” SDK default, must not set `continue-on-error` on the assessment step, and must not treat a later SaaS policy call as the job result.

`evaluate_gate(assessment, ...)` raises `GateFailure` with every failing reason joined by `"; "`. The CLI prints it in red and exits `1`. Artifacts (JSON and Markdown) are written **before** the gate runs so a failed job still has reports.

## DeepTeam / Confident AI vs this gate

DeepTeam and DeepEval are built to **upload then remotely gate**:

1. A CI step runs `deepteam run` / `deepeval test run`, often with `OPENAI_API_KEY` because attack generation and judging happen in the customer process.
2. Those steps commonly use `continue-on-error: true` so a failing assessment does not stop the job.
3. Results go to Confident AI (`CONFIDENT_API_KEY`).
4. A later `deepeval gate` (or `POST /v1/governance/assess`) decides pass/fail from cloud policy.

That model is **not** this product. Martian red-team is the opposite shape:

| | DeepTeam / Confident AI | martian-redteam |
| --- | --- | --- |
| Who generates attacks | Customer process (OpenAI or other local judge models) | Martian Agents `simulator` graph |
| Who scores replies | Customer process or Confident AI | Martian Agents `evaluator` graph |
| What leaves the customer network | Often full traces to Confident AI; local LLMs see the target | Attack string + **target reply** to Martian Agents (see [security and data](security-and-data.md)) |
| CI secrets | `OPENAI_API_KEY`, `CONFIDENT_API_KEY` | `MARTIAN_API_KEY` only. Never `OPENAI_API_KEY` |
| Cheap PR check | Usually none (or a full run) | `martian-redteam validate` — no API key, no HTTP |
| Pass/fail | Remote `deepeval gate` after upload; eval steps often `continue-on-error` | Local `evaluate_gate` **is** the job result |
| Failed run artifacts | Platform dashboards | `results/risk-assessment.json` + `.md`, uploaded with `if: always()` |

Do not add `deepeval gate`, Confident AI login, or `continue-on-error` to a Martian pipeline. If the local gate fails, the GitHub job must be red.

## Error kinds

| `error_kind` | Source | Default gate |
| --- | --- | --- |
| `infrastructure` | `ClientError`: HTTP, timeout, connect, non-JSON, schema, failed run status | Fail if `fail_on_infrastructure_error` (default true) |
| `target` | Exception from `model_callback` | Fail unless `ignore_target_errors` |
| `null` (scored) | Evaluator returned `0` or `1` | Fail **only** if the type is `gated` and `pass_rate < threshold` |

Parse misses and non-binary scores are infrastructure, not `score=0`.

## Matrix

| Event | Test case | Gated vuln (leakage, agency) | Report-only vuln (toxicity, …) |
| --- | --- | --- | --- |
| HTTP 4xx/5xx, timeout, transport | `error` + `infrastructure` | Job **fails** | Job **fails** |
| Schema/parse miss, non-binary score, failed run status | `error` + `infrastructure` | Job **fails** | Job **fails** |
| Bedrock/content refusal with no `attack` | schema/`ClientError` | Job **fails** | Job **fails** |
| Target callback exception | `error` + `target` | Job **fails** unless `ignore_target_errors` | Same |
| `score == 0` | scored fail | Fails if pass-rate below threshold | Reported only |
| `score == 1` | scored pass | Counts toward pass-rate | Reported |

Optional ignore applies **only** to target exceptions, never to transport or schema errors. There is no YAML or CLI switch that ignores HTTP/schema failures.

## Pass rate

For each `(vulnerability, vulnerability_type)`:

```text
pass_rate = passing / (passing + failing)   # errors excluded from denominator
```

If every probe for a gated type errored, `pass_rate` is `0.0` **and** `infrastructure_errors` / `target_errors` are non-zero, so the job still fails on the error clause. That avoids “100% passed because nothing scored.”

Default `pass_rate_threshold` is `1.0` (every scored gated probe must resist). Lower it only with an explicit product decision.

Prompt leakage and excessive agency are `gated=True` in the catalog. Toxicity, illegal activity, and PII leakage are report-only until you wrap them in `CustomVulnerability(..., gated=True)` or change the catalog.

## CLI used in CI

DeepTeam-compatible flags on `run`. YAML `system_config` supplies defaults; flags override for that invocation only.

```text
martian-redteam validate CONFIG.yaml
martian-redteam run CONFIG.yaml [-o DIR] [-c N] [-a N]
```

| Flag | DeepTeam equivalent | Overrides | Default if omitted |
| --- | --- | --- | --- |
| `-o` / `--output` | `-o` | `system_config.output_folder` | YAML, else `results` |
| `-c` / `--max-concurrent` | `-c` | `system_config.max_concurrent` | YAML, else `2` |
| `-a` / `--attacks-per-vuln` | `-a` | `system_config.attacks_per_vulnerability_type` | YAML, else `1` |

Keep `-c 2` (or `1`) on GitHub-hosted runners. DeepTeam’s docs often show `-c 20` against local OpenAI; that will 429 martian-agents WAF. See [WAF](#waf-and-github-hosted-ips).

`run` always writes reports, then applies the gate:

```bash
martian-redteam run config.yaml -o results -c 2 -a 1
```

Writes (directory created if missing):

| File | Role |
| --- | --- |
| `results/risk-assessment.json` | Full `RiskAssessment` (machine-readable) |
| `results/risk-assessment.md` | Human summary; append to `$GITHUB_STEP_SUMMARY` |

Exit codes:

| Code | `validate` | `run` |
| --- | --- | --- |
| 0 | YAML, catalog names, callback path, and gate fields are legal | Assessment finished and gate passed |
| 1 | Invalid config | `GateFailure` or Typer usage error |
| 2 | (Typer) invalid options | (Typer) invalid options |
| other | Uncaught Python exception | Uncaught exception (bad YAML, missing callback, missing API key) |

`validate` does **not** read `MARTIAN_API_KEY`, does **not** call Martian Agents, and does **not** invoke `model_callback`. It is the cheap PR check.

`run` requires `MARTIAN_API_KEY`. Missing key is a client error, not a skipped gate.

## This repository’s CI

`.github/workflows/ci.yml` runs on `pull_request` and `push` to `main`. It tests **this SDK**, not a customer agent. Split jobs plus a required aggregator:

| Job | What | Python |
| --- | --- | --- |
| `lint` | `uv sync --locked`, `ruff format --check`, `ruff check` | 3.12 |
| `types` | `uv sync --locked`, `mypy src`, `compileall` | 3.12 |
| `test` | `uv sync --locked`, `pytest` with coverage + JUnit | **matrix 3.12 and 3.13** |
| `packaging` | `uv build`, install the wheel in a throwaway venv, `validate examples/config.yaml` | 3.12 |
| `action-smoke` | `uses: ./.github/actions/validate` (composite install path, no API key) | 3.12 |
| `ci` | Aggregator: succeeds only if every job above succeeded | — |

Protect `main` with the single required check **`ci`**, not the matrix cells. That way a skipped/failed 3.13 cell cannot look green. The aggregator checks each `needs.*.result == success`; adding a job only to `needs` without that check would leave `ci` green on failure.

This workflow does **not**:

- Call staging or production Martian Agents
- Set `MARTIAN_API_KEY` or `OPENAI_API_KEY`
- Treat `martian_agents` as a red-team target
- Run `martian-redteam run`

Live `run` dogfood (composite `action.yml` / reusable `red-team.yml` against staging graphs) is **layer 3** and is not a required PR check. See [development](development.md#testing-this-library).

Concurrency cancels in-progress runs on the same ref. Third-party actions are **SHA-pinned** (tag in the comment only).

### Dependabot

`.github/dependabot.yml` opens weekly PRs for:

- `package-ecosystem: uv` (lockfile + `pyproject.toml`)
- `package-ecosystem: github-actions` (action SHAs)

Review action bumps by SHA, not by floating major tags. See [development](development.md).

## Customer GitHub Actions

Three supported ways to run an assessment against **your** agent. Prefer the reusable workflow. All three must:

- Fail closed (no `continue-on-error` on `run` / the composite)
- Upload `results/` with `if: always()` (JSON **and** Markdown)
- Append Markdown to `$GITHUB_STEP_SUMMARY`
- Keep `-c` at 2 (or 1)
- Use `permissions: contents: read`
- Pin this SDK to a **tag or commit SHA** in production, not floating `@main`

Start with `workflow_dispatch` only. Add `pull_request` for `validate` immediately; add `pull_request` for `run` only when the simulator/evaluator graphs exist and you understand WAF quota.

Required secrets / variables on the **customer** repo:

- Secret `MARTIAN_API_KEY` (required for `run`)
- Optional variable `MARTIAN_API_URL`

Fork pull requests do not receive repository secrets. Keep `validate` on `pull_request` (no key). Keep `run` on `workflow_dispatch`, `push` to default, or `pull_request` from the same repo — never as the only check on a public fork PR.

### 1. Cheap PR check (`validate`, no API key)

Run on every pull request. Catches broken YAML, unknown catalog names, missing callback files, and illegal gate fields before anyone spends Bedrock.

```yaml
name: Validate red-team config

on:
  pull_request:
  push:
    branches: [main]

permissions:
  contents: read

concurrency:
  group: redteam-validate-${{ github.ref }}
  cancel-in-progress: true

jobs:
  validate:
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false

      - uses: astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d # v10.0.1
        with:
          version: "0.10.0"

      - name: Install martian-redteam
        run: uv pip install --system "martian-redteam @ git+https://github.com/MorningHQ/martian-redteam.git@v0.1.0"

      - name: Validate config
        run: martian-redteam validate config.yaml
```

Replace `v0.1.0` with the tag or SHA you actually depend on. Do not use `@main` once you are past dogfood.

Same check via this repo’s validate composite (still no API key; the root `action.yml` stays the `run` gate):

```yaml
      - uses: MorningHQ/martian-redteam/.github/actions/validate@v0.1.0
        with:
          config: config.yaml
```

### 2. Reusable workflow (recommended gate)

The reusable workflow checks out **your** repository, installs this SDK, runs the local gate (`martian-redteam run`), always uploads artifacts, and appends Markdown to `$GITHUB_STEP_SUMMARY` when `save()` runs. It does **not** run `validate`; keep that in a separate PR job with no API key.

```yaml
name: Red team

on:
  workflow_dispatch:
  # pull_request:   # enable when graphs exist and WAF headroom is understood
  #   paths:
  #     - "config.yaml"
  #     - "callback.py"
  #     - ".github/workflows/red-team.yml"

permissions:
  contents: read

jobs:
  red-team:
    uses: MorningHQ/martian-redteam/.github/workflows/red-team.yml@v0.1.0
    with:
      config: config.yaml
      output: results
      max-concurrent: "2"
      attacks-per-vuln: "1"
    secrets:
      MARTIAN_API_KEY: ${{ secrets.MARTIAN_API_KEY }}
```

Optional API host: set `MARTIAN_API_URL` as a repository variable on the caller, or pass it if the reusable workflow exposes a matching input. Do not put the API key in `with:`.

**Production pin** (tag or SHA, not `@main`):

```yaml
jobs:
  red-team:
    uses: MorningHQ/martian-redteam/.github/workflows/red-team.yml@v0.1.0
    # or:
    # uses: MorningHQ/martian-redteam/.github/workflows/red-team.yml@0123456789abcdef0123456789abcdef01234567
```

Dogfood only:

```yaml
uses: MorningHQ/martian-redteam/.github/workflows/red-team.yml@main
```

The called workflow’s `run` step is the job result. It does not set `continue-on-error`. A `GateFailure` fails the reusable job, which fails your caller.

Typical `workflow_call` inputs (names may match the workflow file; keep concurrency at 2):

| Input | Meaning | Default |
| --- | --- | --- |
| `config` | Path to YAML in the **caller** repo | `examples/config.yaml` |
| `output` | Directory for JSON + Markdown | `results` |
| `sdk-ref` | Git ref of this repo to `pip install` | `main` (pin in production) |
| `max-concurrent` | Passed as `-c` when non-empty | empty (use YAML) |
| `attacks-per-vuln` | Passed as `-a` when non-empty | empty (use YAML) |

| Secret | Required |
| --- | --- |
| `MARTIAN_API_KEY` | yes for the run job |

### 3. Composite action

Use when you already have a workflow and only need this repo to install the CLI and run the gate. Check out **your** repository first; the action files come from `MorningHQ/martian-redteam`. The reusable workflow runs `run` only. Keep `validate` in a prior job that has **no** API key.

```yaml
name: Red team

on:
  workflow_dispatch:

permissions:
  contents: read

jobs:
  red-team:
    runs-on: ubuntu-24.04
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false

      - name: Run assessment
        uses: MorningHQ/martian-redteam@v0.1.0
        with:
          config: config.yaml
          output: results
          max-concurrent: "2"
          attacks-per-vuln: "1"
        env:
          MARTIAN_API_KEY: ${{ secrets.MARTIAN_API_KEY }}
          MARTIAN_API_URL: ${{ vars.MARTIAN_API_URL }}

      - name: Job summary
        if: always()
        run: |
          if [ -f results/risk-assessment.md ]; then
            cat results/risk-assessment.md >> "$GITHUB_STEP_SUMMARY"
          fi

      - name: Upload results
        if: always()
        uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4.6.2
        with:
          name: red-team-results
          path: results/
```

The composite maps `max-concurrent` → `-c` and `attacks-per-vuln` → `-a` when those inputs are non-empty, then runs:

```bash
martian-redteam run <config> -o <output> [-c N] [-a N]
```

If the composite already appends Markdown to `$GITHUB_STEP_SUMMARY` and uploads artifacts, omit the duplicate steps — but keep `if: always()` on upload so a failed gate still leaves JSON + Markdown. **Do not** put `continue-on-error: true` on `Run assessment`. The composite’s exit code **is** the job result.

Pin the action the same way:

```yaml
- uses: MorningHQ/martian-redteam@v0.1.0
# production SHA:
- uses: MorningHQ/martian-redteam@0123456789abcdef0123456789abcdef01234567
# dogfood only:
- uses: MorningHQ/martian-redteam@main
```

### 4. Copy-paste workflow

`examples/github-actions.yml` is the same gate without calling back into this repo’s action/workflow. Use it when you vendor the CLI yourself.

```yaml
name: Red team

on:
  workflow_dispatch:

permissions:
  contents: read

jobs:
  red-team:
    runs-on: ubuntu-24.04
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false

      - uses: astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d # v10.0.1
        with:
          version: "0.10.0"

      - name: Install
        run: uv sync --locked

      - name: Validate config
        run: uv run martian-redteam validate examples/config.yaml

      - name: Run assessment
        env:
          MARTIAN_API_URL: ${{ vars.MARTIAN_API_URL }}
          MARTIAN_API_KEY: ${{ secrets.MARTIAN_API_KEY }}
        run: uv run martian-redteam run examples/config.yaml -o results -c 2 -a 1

      - name: Job summary
        if: always()
        run: |
          if [ -f results/risk-assessment.md ]; then
            cat results/risk-assessment.md >> "$GITHUB_STEP_SUMMARY"
          fi

      - name: Upload results
        if: always()
        uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4.6.2
        with:
          name: red-team-results
          path: results/
```

Suggested split when you want validate on every PR and the live gate on demand:

```yaml
name: Red team

on:
  pull_request:
  workflow_dispatch:

permissions:
  contents: read

jobs:
  validate:
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      - uses: astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d # v10.0.1
        with:
          version: "0.10.0"
      - run: uv sync --locked
      - run: uv run martian-redteam validate config.yaml

  red-team:
    needs: validate
    if: github.event_name == 'workflow_dispatch'
    runs-on: ubuntu-24.04
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      - uses: astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d # v10.0.1
        with:
          version: "0.10.0"
      - run: uv sync --locked
      - name: Run assessment
        env:
          MARTIAN_API_KEY: ${{ secrets.MARTIAN_API_KEY }}
          MARTIAN_API_URL: ${{ vars.MARTIAN_API_URL }}
        run: uv run martian-redteam run config.yaml -o results -c 2 -a 1
      - name: Job summary
        if: always()
        run: cat results/risk-assessment.md >> "$GITHUB_STEP_SUMMARY"
      - name: Upload results
        if: always()
        uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4.6.2
        with:
          name: red-team-results
          path: results/
```

## WAF and GitHub-hosted IPs

martian-agents public ingress uses WAF **2000 requests / 5 minutes / IP**. GitHub-hosted runners share NAT pools. Two workflows on the same egress IP can 429 each other even if they use different API keys.

Each probe is two `/runs/wait` calls (simulator + evaluator). At `-c 2` that is a small concurrent burst, which is the intended default. Raising `-c` toward DeepTeam’s examples (`10`–`20`) is how you hit the WAF.

The SDK retries 429 three times, then fails the gate as **infrastructure**. That is fail-closed, not “try a bigger concurrency.”

Keep `max_concurrent` / `-c` at **2** or **1**. Prefer self-hosted runners if you dogfood many jobs. Application-level per-key quotas are a martian-agents Phase 0/public blocker; do not treat WAF as tenancy.

## Artifacts and the job summary

Upload `results/` on success **and** failure (`if: always()`). A red gate with no artifact is harder to debug than a red gate with JSON.

`risk-assessment.md` is the human report: per-type pass rates, gated vs report-only, infrastructure/target error counts, and failed or errored probes (reason + what the target saw). Append it to GitHub’s job summary:

```bash
cat results/risk-assessment.md >> "$GITHUB_STEP_SUMMARY"
```

The reusable workflow and composite action do this for you. The Markdown is **not** a remote policy document. It does not replace `evaluate_gate`.

## Interpreting `risk-assessment.json`

Inspect `infrastructure_errors` and `target_errors` first. A pile of `score: 1` with `errored > 0` is **not** a green run if the gate defaults are on.

For scored fails, read `reason` (evaluator prose) and `input` (what the target saw, including encodings). Evaluator calls send that reply to Martian Agents; treat production-app CI as a data-sharing decision.

## What not to copy from DeepTeam CI snippets

- `continue-on-error: true` on the assessment
- `deepeval gate` / Confident AI upload as the pass/fail signal
- `OPENAI_API_KEY` in this SDK’s CI (judging is not local)
- `ignore_errors: true` in YAML (this schema rejects DeepTeam-only keys; extra keys are forbidden)
- Floating `@main` on the composite action or reusable workflow in production
- `-c 10` or `-c 20` on GitHub-hosted runners
