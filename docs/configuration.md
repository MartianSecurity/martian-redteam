# Configuration

## Environment (`Settings`)

Loaded from process env and optional `.env`, prefix `MARTIAN_`.

| Field | Env | Default | Validation |
| --- | --- | --- | --- |
| `api_url` | `MARTIAN_API_URL` | `https://api.agents.martiansecurity.ai` | Must start with `http://` or `https://`; trailing slash stripped |
| `api_key` | `MARTIAN_API_KEY` | empty | SecretStr. `Client()` raises `ClientError` if empty |
| `request_timeout_seconds` | `MARTIAN_REQUEST_TIMEOUT_SECONDS` | `120` | `1.0`–`240.0` inclusive |

Unknown env keys are ignored (`extra="ignore"`).

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
  output_folder: results

vulnerabilities:
  - name: PromptLeakage        # or prompt_leakage
    types: ["system_prompt"]
  - name: ExcessiveAgency
    types: ["functionality"]
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
  url: https://api.agents.martiansecurity.ai   # optional; key still from env
```

### Path resolution

`callback.file` is resolved **relative to the YAML file’s directory**, not the process CWD. `examples/config.yaml` therefore uses `file: callback.py` next to itself. Running `martian-redteam run examples/config.yaml` from the repo root still finds `examples/callback.py`.

The function is loaded with `importlib` into a private module name. The file must be readable Python. Import errors and missing function names fail the CLI before any probe.

### Gate fields

| Field | Default | Effect |
| --- | --- | --- |
| `fail_on_infrastructure_error` | `true` | Any `error_kind=infrastructure` fails the process |
| `ignore_target_errors` | `false` | When `true`, callback exceptions do not fail the gate (still recorded) |
| `pass_rate_threshold` | `1.0` | Gated vulnerability types with `pass_rate < threshold` fail the process |

There is no YAML switch that ignores HTTP/schema failures. That is intentional.

## CLI

```text
martian-redteam --version
martian-redteam catalog
martian-redteam run CONFIG.yaml [-o DIR]
```

`-o` overrides `system_config.output_folder`. The assessment is always written before the gate runs so artifacts exist on failure (`if: always()` in the example Action).

Exit codes:

| Code | Meaning |
| --- | --- |
| 0 | Run completed and gate passed |
| 1 | Gate failed (`GateFailure`) or Typer usage error |
| 2 | (Typer) invalid options |
| other | Uncaught Python exception (bad YAML, missing callback, missing API key) |

## GitHub Actions example

See `examples/github-actions.yml` and [gate and CI](gate-and-ci.md). Required:

- Secret `MARTIAN_API_KEY`
- Optional variable `MARTIAN_API_URL`

Do not set `continue-on-error: true` on the run step. The gate **is** the job result.
