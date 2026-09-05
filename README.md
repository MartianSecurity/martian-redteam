"""# Martian Red Team

Adversarial testing SDK for AI agents. This package orchestrates probes in
your process. Attack generation and judging run on the Martian Agents API.
Your application stays behind a local `model_callback`.

This release is an **internal scaffold**. Do not point untrusted CI at the
shared staging API key until per-tenant identity exists on the service.

## Install

```bash
uv sync
```

## Configure

```bash
cp .env.example .env
# MARTIAN_API_URL and MARTIAN_API_KEY
```

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

## CLI

```bash
uv run martian-redteam run examples/config.yaml -o results/
```

The target callback is never invoked from Martian's network. Evaluator calls
send the target's reply to the Martian Agents API.

## Layout

- `Client` — `POST /runs/wait` for `simulator` and `evaluator`
- Catalog — vulnerability and attack **ids** only; rubrics live in martian-agents
- Encodings — ROT13, Base64, and leetspeak run locally

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
