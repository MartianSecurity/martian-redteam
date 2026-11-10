# Configuration

## Platform connection

Modern `martian.redteam.Client`, `martian redteam`, `martian ci` and evals share
`MartianConfig(api_url, token, request_timeout_seconds=120, trust_env=False)`.
Default resolution uses only `MARTIAN_API_URL` / `MARTIAN_API_TOKEN`; no `.env`.
Supply a complete constructor pair or one config, never competing sources.
Evals uses this timeout for each Backend HTTP phase, including dataset reads and
session uploads. Redteam additionally applies it as an overall operation wait limit.
`CiContext(agent_id, git_sha, repo="", ref="", run_url="")` separately attributes
persisted sessions. Standalone redteam/dataset reads need no CI metadata.
See [the shared connection and compatibility matrix](sdk-and-ci.md#one-customer-connection).

Legacy direct `martian_redteam.Client(settings=Settings(...))` uses an explicit
complete `api_url`/`api_key` pair or only the dedicated `MARTIAN_REDTEAM_*` pair.
It never reads generic platform variables or `.env`. Its optional timeout uses
`MARTIAN_REDTEAM_REQUEST_TIMEOUT_SECONDS` (1–240 seconds). Direct Agents use is
an operator compatibility interface, not an alternate modern authentication path.

## YAML (`RunConfig`)

Loaded by `martian redteam run PATH`. Extra keys are forbidden.

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
```

### System fields

| Field | Default | Validation | Effect |
| --- | --- | --- | --- |
| `max_concurrent` | `2` | `>= 1` | In-flight probes |
| `attacks_per_vulnerability_type` | `1` | `>= 1` | Repeats per (vulnerability type × attack) |
| `output_folder` | `results` | | Report directory (`-o` overrides) |
| `target_timeout_seconds` | `120` | `>= 1` | Deadline for **one** call to the target callback |

A callback call that exceeds `target_timeout_seconds` is cancelled and recorded as `error_kind=target` with `target callback timed out after N seconds`. The run continues, the reports are written, and the gate fails on the target error unless `ignore_target_errors` is set. Without the deadline a hung target would hold the job until the CI timeout killed it, before any report existed.

Async callbacks are cooperatively cancelled. Synchronous callbacks use bounded worker threads and require their own I/O timeout: Python cannot kill a running worker, and the SDK awaits owned cleanup before returning. Late replies never become passing judgments.

### Path resolution

`callback.file` is resolved **relative to the YAML file’s directory**, not the process CWD. `examples/config.yaml` therefore uses `file: callback.py` next to itself. Running `martian redteam run examples/config.yaml` from the repo root still finds `examples/callback.py`.

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
martian redteam --version
martian redteam catalog
martian redteam validate CONFIG.yaml
martian redteam run CONFIG.yaml [-o DIR] [-c N] [-a N]
```

| Flag | Long form | Overrides | Notes |
| --- | --- | --- | --- |
| `-o` | `--output` | `system_config.output_folder` | Directory for JSON + Markdown. Created if missing. |
| `-c` | `--max-concurrent` | `system_config.max_concurrent` | In-flight probes. Keep at **2** (or 1) unless the granted Backend admission limits support more. |
| `-a` | `--attacks-per-vuln` | `system_config.attacks_per_vulnerability_type` | Repeats per (vuln type × attack). Cost scales linearly. |

```bash
martian redteam validate config.yaml
martian redteam run config.yaml -o results -c 2 -a 1
```

### `validate`

Cheap, offline check for CI pull requests. No `MARTIAN_API_TOKEN`. No HTTP. The callback is not invoked.

It loads the YAML with the same `RunConfig` model as `run` and fails closed on:

- Non-mapping YAML or forbidden extra keys (DeepTeam-only fields such as `ignore_errors` / `run_async` are rejected)
- Unknown vulnerability or attack names
- Types not allowed for a built-in vulnerability
- Custom vulns missing `id` / `criteria`
- `criteria` on a built-in vulnerability (only custom vulnerabilities carry criteria)
- Missing or unreadable `callback.file`, or a missing `callback.function`
- `max_concurrent < 1`, `attacks_per_vulnerability_type < 1`, `target_timeout_seconds < 1`, `pass_rate_threshold` outside `0.0`–`1.0`
- Any `api.url` in modern platform mode; the approved origin comes from connection configuration only. Legacy YAML may name only its already selected origin, never redirect the credential.

Exit `0` if the file is a legal assessment config. Exit `1` if it is not.

### `run`

Requires `MARTIAN_API_TOKEN`. Writes reports **then** applies the gate so a failed job still has artifacts (`if: always()` in Actions).

Each `run` writes:

| File | Role |
| --- | --- |
| `{output}/risk-assessment.json` | Safe `RiskAssessment` (digest content by default) |
| `{output}/risk-assessment.md` | Human summary (append to `$GITHUB_STEP_SUMMARY` in CI) |

The CLI also prints Rich tables (per-type pass rate, then failed/errored probes).

Exit codes:

| Code | `validate` | `run` |
| --- | --- | --- |
| 0 | Config is legal | Run completed and gate passed |
| 1 | Invalid config | Gate failed (`GateFailure`) or Typer usage error |
| 2 | (Typer) invalid options | Setup/execution or invalid options |


There is no CLI flag that ignores HTTP/schema failures or skips the gate. `-c` / `-a` / `-o` change volume and paths, not fail-closed policy.

## GitHub Actions

Use [the combined example](../examples/combined-ci.yml) for persisted gates or
[the root composite action](../action.yml) for a local redteam report/gate.
Configure a Backend URL and token with `redteam:run`; persisted CI additionally
requires the agent/revision and evals/CI capabilities. There is no Agents key
in modern workflows. See [gate and CI](gate-and-ci.md).
