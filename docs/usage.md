# How to use it

## Install and configure

Requires Python 3.12+ and uv. Install from this checkout:

```sh
uv sync --locked
cp .env.example .env
```

Set MARTIAN_API_URL to your approved Martian Agents endpoint (for a local stack, http://localhost:2026), and MARTIAN_API_KEY to that service's client key. MARTIAN_REQUEST_TIMEOUT_SECONDS controls request timeout. The SDK defaults to a hosted API; set the URL explicitly when testing locally.

## CLI

```sh
uv run martian-redteam catalog
uv run martian-redteam validate examples/config.yaml
uv run martian-redteam run examples/config.yaml -o results -c 2 -a 1
```

Validate makes no SDK model request and does not invoke the callback function, but it imports the callback module, executing its top-level Python code. Use an isolated, unprivileged job for untrusted pull-request code. Run makes paid remote model requests. The example callback refuses prompts; replace it with your approved target to perform a meaningful assessment.

Configuration has target.purpose/callback, vulnerabilities, attacks, system_config and gate sections; use [examples/config.yaml](../examples/config.yaml) as the starting point. Callback paths resolve relative to YAML. Secrets come from environment settings, not YAML. `-c`, `-a` and `-o` override concurrency, repeats and output directory.

## Python

For a customer agent that consumes runtime-provided MCP servers, install the mcp extra and use [the customer integration](customer-integration.md). The probe API below remains separate.

```python
from martian_redteam import Client, PromptInjection, PromptLeakage, a_red_team
from martian_redteam.gate import evaluate_gate

async def model_callback(prompt: str) -> str:
    return "I cannot help with that."  # Replace with your authorized application.

async def assess():
    async with Client() as client:
        result = await a_red_team(
            target=model_callback,
            vulnerabilities=[PromptLeakage()],
            attacks=[PromptInjection()],
            client=client,
            purpose="Internal assistant",
        )
    result.save("results")
    evaluate_gate(result)
    return result
```

Use red_team outside an existing event loop. The async example is a function to call from your application's loop, not an automatic background task.

## CI and checks

See [gate and CI](gate-and-ci.md). Pin the SDK/action to a reviewed commit or release. Keep secrets out of untrusted pull-request runs; use offline validate there. Live runs require the API key and spend model budget. Upload reports even if the gate fails.

Existing checks: `uv run ruff check .`, `uv run --extra mcp mypy src`, `uv run pytest`. The type check includes the optional MCP dependencies. Check `.github/workflows/ci.yml` for the pinned matrix. Do not use live hosted credentials for unit checks.

## Optional visibility telemetry

Infrastructure discovery works without SDK installation. To add tool activity, an
admin opens a discovered record's **Runtime observations**, pins deployment,
environment and version, and issues a one-time token. Keep the binding UUID and token
in the application's secret configuration. Use the customer backend origin, not the
Martian Agents model runtime. Install this source version through the usual release
process; this change does not publish an SDK release.

```python
import os
from typing import Literal
from martian_redteam import ToolActivity, VisibilityClient

visibility = VisibilityClient(
    backend_url=os.environ["MARTIAN_VISIBILITY_URL"],
    binding_id=os.environ["MARTIAN_VISIBILITY_BINDING_ID"],
    token=os.environ["MARTIAN_VISIBILITY_TOKEN"],
)

async def report_completed_call(tool_id: str, tool_name: str,
                                outcome: Literal["success", "error"], duration_ms: int) -> int:
    event = ToolActivity(
        tool_id=tool_id, tool_name=tool_name,
        outcome=outcome, duration_ms=duration_ms,
    )
    return await visibility.send([event])
```

Call this from trusted application instrumentation with outcome `success` or `error`;
do not put sensitive values into tool names/IDs. Batches contain 1–100 events, under
128 KiB; events need aware timestamps from the last 24 hours (30-second future
tolerance). HTTPS is required; `allow_http=True` is explicit local-development opt-in.
No tool arguments, responses or prompts are accepted. AssessmentSession/TraceRecorder
remain separate and are not automatically exported.

`send()` returns the newly accepted count; exact repeats return zero. Preserve event
IDs/content if explicitly resubmitting within the time window. Conflicting IDs fail.
The client performs no automatic retries, durable queueing or alternate transport;
handle export failures in the application according to its operational requirements.
Issue a new binding for a new deployment/version and revoke the old credential in the
console. Revocation stops telemetry ingestion, not the agent itself.

Receiver behavior is documented in the [backend contract](../../martian-collector/docs/contract.md#application-telemetry-contract-v1).
The existing 55 SDK tests and full MCP-extra type check pass locally. On 2026-09-18
the actual visibility module reported a synthetic Kubernetes read to the running
local backend. Exact replay, conflicting-event rejection and revocation were also
verified; see [local acceptance](../../martian-collector/docs/usage.md#local-acceptance--2026-09-18).
The SDK has not been published by this change.

[README](../README.md) · [Architecture](architecture.md) · [How it works](how-it-works.md)
