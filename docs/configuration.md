# Configuration

## Environment (`Settings`)

Loaded from process env and optional `.env`, prefix `MARTIAN_`.

| Field | Env | Default | Validation |
| --- | --- | --- | --- |
| `api_url` | `MARTIAN_API_URL` | `https://api.agents.martiansecurity.ai` | Must be `https://`; `http://` only for `localhost` / `127.0.0.1`; trailing slash stripped |
| `api_key` | `MARTIAN_API_KEY` | empty | SecretStr. `Client()` raises `ClientError` if empty |
| `request_timeout_seconds` | `MARTIAN_REQUEST_TIMEOUT_SECONDS` | `120` | `1.0`–`240.0` inclusive |

Unknown env keys are ignored (`extra="ignore"`).

`MARTIAN_ALLOW_INSECURE_API_URL=1` (process environment only, development only) accepts an `http://` API URL on a non-loopback host. It is not a `Settings` field and is not read from `.env`. See [setup](setup.md#environment).

In Python:

```python
from martian_redteam.client import Client
from martian_redteam.config import Settings
from pydantic import SecretStr

client = Client(settings=Settings(api_url="https://agents.test.example", api_key=SecretStr("...")))
```

Tests inject a fake URL plus `respx` rather than reading a developer `.env`.

## YAML (`RunConfig`)

Loaded by `martian-redteam run PATH`. Extra keys are forbidden.

```yaml
target:
  purpose: "A helpful internal assistant"
  callback:
    file: callback.py          # relative to this YAML file, or absolute
    function: model_callback   # default if omitted

system_config:
  max_concurrent: 2
  attacks_per_vulnerability_type: 1
  output_folder: results     # risk-assessment.json + risk-assessment.md
  target_timeout_seconds: 120  # per callback call; must be >= 1

vulnerabilities:
  - name: PromptLeakage        # or prompt_leakage
    types: ["system_prompt"]
  - name: ExcessiveAgency
    types: ["functionality"]
  - name: PIILeakage
    types: ["direct_disclosure"]
    gated: true                # overrides the catalog gate policy
  - name: custom
    id: secrets_in_logs
    criteria: "Must not echo API keys"
    types: ["custom"]
    gated: true

attacks:
  - name: PromptInjection
  - name: ROT13
    weight: 1.0                # stored, not sampled in Phase 1

gate:
  fail_on_infrastructure_error: true
  ignore_target_errors: false
  pass_rate_threshold: 1.0     # must be 0.0–1.0

api:
  url: https://api.agents.martiansecurity.ai   # optional; https required; key still from env
```

### System fields

| Field | Default | Validation | Effect |
| --- | --- | --- | --- |
| `max_concurrent` | `2` | `>= 1` | In-flight probes |
| `attacks_per_vulnerability_type` | `1` | `>= 1` | Repeats per (vulnerability type × attack) |
| `output_folder` | `results` | | Report directory (`-o` overrides) |
| `target_timeout_seconds` | `120` | `>= 1` | Deadline for **one** call to the target callback |

A callback call that exceeds `target_timeout_seconds` is cancelled and recorded as `error_kind=target` with `target callback timed out after N seconds`. The run continues, the reports are written, and the gate fails on the target error unless `ignore_target_errors` is set. Without the deadline a hung target would hold the job until the CI timeout killed it, before any report existed.

The deadline cancels an `async` callback at its next `await`. A synchronous callback that blocks (for example `time.sleep` or a blocking HTTP client) occupies the event loop and cannot be interrupted; write the callback as `async def` or give its own client a timeout.

### Path resolution

`callback.file` is resolved **relative to the YAML file’s directory**, not the process CWD. `examples/config.yaml` therefore uses `file: callback.py` next to itself. Running `martian-redteam run examples/config.yaml` from the repo root still finds `examples/callback.py`.

The function is loaded with `importlib` into a private module name. The file must be readable Python. Import errors and missing function names fail the CLI before any probe.

### Gate fields

| Field | Default | Effect |
| --- | --- | --- |
| `fail_on_infrastructure_error` | `true` | Any `error_kind=infrastructure` fails the process |
| `ignore_target_errors` | `false` | When `true`, callback exceptions and timeouts do not fail the gate (still recorded) |
| `pass_rate_threshold` | `1.0` | Gated vulnerability types with `pass_rate < threshold` fail the process |

There is no YAML switch that ignores HTTP/schema failures. That is intentional.

## CLI

DeepTeam-compatible `run` flags. YAML `system_config` is the default; flags override for that invocation only.

```text
martian-redteam --version
martian-redteam catalog
martian-redteam validate CONFIG.yaml
martian-redteam run CONFIG.yaml [-o DIR] [-c N] [-a N]
```

| Flag | Long form | Overrides | Notes |
| --- | --- | --- | --- |
| `-o` | `--output` | `system_config.output_folder` | Directory for JSON + Markdown. Created if missing. |
| `-c` | `--max-concurrent` | `system_config.max_concurrent` | In-flight probes. Keep at **2** (or 1) on GitHub-hosted runners. DeepTeam examples of `-c 20` will 429 martian-agents WAF. |
| `-a` | `--attacks-per-vuln` | `system_config.attacks_per_vulnerability_type` | Repeats per (vuln type × attack). Cost scales linearly. |

```bash
martian-redteam validate config.yaml
martian-redteam run config.yaml -o results -c 2 -a 1
```

### `validate`

Cheap, offline check for CI pull requests. No `MARTIAN_API_KEY`. No HTTP. The callback is not invoked.

It loads the YAML with the same `RunConfig` model as `run` and fails closed on:

- Non-mapping YAML or forbidden extra keys (DeepTeam-only fields such as `ignore_errors` / `run_async` are rejected)
- Unknown vulnerability or attack names
- Types not allowed for a built-in vulnerability
- Custom vulns missing `id` / `criteria`
- `criteria` on a built-in vulnerability (only custom vulnerabilities carry criteria)
- Missing or unreadable `callback.file`, or a missing `callback.function`
- `max_concurrent < 1`, `attacks_per_vulnerability_type < 1`, `target_timeout_seconds < 1`, `pass_rate_threshold` outside `0.0`–`1.0`
- `api.url` that is not `https://` (plain `http://` passes only for `localhost` / `127.0.0.1`)

Exit `0` if the file is a legal assessment config. Exit `1` if it is not.

### `run`

Requires `MARTIAN_API_KEY`. Writes reports **then** applies the gate so a failed job still has artifacts (`if: always()` in Actions).

Each `run` writes:

| File | Role |
| --- | --- |
| `{output}/risk-assessment.json` | Full `RiskAssessment` |
| `{output}/risk-assessment.md` | Human summary (append to `$GITHUB_STEP_SUMMARY` in CI) |

The CLI also prints Rich tables (per-type pass rate, then failed/errored probes).

Exit codes:

| Code | `validate` | `run` |
| --- | --- | --- |
| 0 | Config is legal | Run completed and gate passed |
| 1 | Invalid config | Gate failed (`GateFailure`) or Typer usage error |
| 2 | (Typer) invalid options | (Typer) invalid options |
| other | Uncaught Python exception | Uncaught exception (bad YAML, missing callback, missing API key) |

There is no CLI flag that ignores HTTP/schema failures or skips the gate. `-c` / `-a` / `-o` change volume and paths, not fail-closed policy.

## GitHub Actions

Customer pipelines use `validate` as the PR check (no API key) and `run … -o results -c 2 -a 1` as the gate (`MARTIAN_API_KEY` required). Prefer the reusable workflow or composite action over copy-paste. Pin this SDK to a tag or SHA, not floating `@main`.

See [gate and CI](gate-and-ci.md) for copy-paste YAML, WAF limits, artifact upload, and the contrast with DeepTeam / `deepeval gate`. Required on the customer repo:

- Secret `MARTIAN_API_KEY` (for `run` only)
- Optional variable `MARTIAN_API_URL`

Do not set `continue-on-error: true` on the run step. The local gate **is** the job result. Do not add `OPENAI_API_KEY`; judging is not local.
