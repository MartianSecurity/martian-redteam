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

[README](../README.md) · [Architecture](architecture.md) · [How it works](how-it-works.md)
