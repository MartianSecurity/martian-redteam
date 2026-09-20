# Martian Red Team

Adversarial testing SDK for AI agents. This package orchestrates probes in
your process. Attack generation and judging run on the Martian Agents API.
Your application stays behind a local `model_callback`.

This release is an **internal scaffold**. Do not point untrusted CI at the
shared staging API key until per-tenant identity exists on the service.

**Documentation:** [docs/](docs/README.md) (architecture, protocol, catalog, gate, security, and customer MCP integration).

**CI/CD:** this repo’s GitHub Actions are lint, types, and pytest only (Python 3.12/3.13, no live Martian Agents, no `OPENAI_API_KEY`). Customer pipelines should `validate` on PRs, then `run … -c 2 -a 1` as a fail-closed local gate — not DeepTeam’s upload-then-`deepeval gate` model. See [docs/gate-and-ci.md](docs/gate-and-ci.md).

## Install

```bash
uv sync
cp .env.example .env   # MARTIAN_API_URL and MARTIAN_API_KEY
```

Applications that bind runtime-provided MCP tools use the optional extra:

```bash
uv sync --extra mcp
```

See [Customer MCP integration](docs/customer-integration.md) for the session, tool-binding and trace APIs.

## Python

```python
from martian_redteam import Client, PromptInjection, PromptLeakage, ROT13, red_team

async def model_callback(prompt: str) -> str:
    return "I cannot help with that."

assessment = red_team(
    target=model_callback,
    vulnerabilities=[PromptLeakage()],
    attacks=[PromptInjection(), ROT13()],
    client=Client(),
    purpose="Internal assistant",
)
assessment.save("results/")
```

Inside an existing event loop, use `await a_red_team(...)` instead of `red_team()`.

## CLI

```bash
uv run martian-redteam catalog
uv run martian-redteam validate examples/config.yaml
uv run martian-redteam run examples/config.yaml -o results/ -c 2 -a 1
```

The target callback is never invoked from Martian's network. Evaluator calls
send the target's reply to the Martian Agents API.

## Layout

- `Client` — `POST /runs/wait` for `simulator` and `evaluator`
- Catalog — vulnerability and attack **ids** only; rubrics live in martian-agents
- Encodings — ROT13, Base64, and leetspeak run locally
- Gate — fail-closed on transport/schema errors and on gated pass rate

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
