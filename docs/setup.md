# Setup

## Prerequisites

- Python **3.12 or newer**
- [uv](https://docs.astral.sh/uv/) (CI pins `0.10.0`)
- A Martian Agents API URL and key (internal/staging only today)

This package is not published to PyPI yet. Install from a git checkout.

## Clone and install

```bash
git clone https://github.com/MorningHQ/martian-redteam.git
cd martian-redteam
uv sync
```

`uv sync` creates `.venv`, installs runtime deps from `uv.lock`, and installs the package editable. Dev tools (`pytest`, `ruff`, `mypy`, `respx`) live in the `dev` dependency group and are included by default `uv sync`.

## Environment

```bash
cp .env.example .env
```

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `MARTIAN_API_URL` | no | `https://api.agents.martiansecurity.ai` | Aegra base URL, no trailing slash. Must be `https://`; `http://` only for `localhost` / `127.0.0.1` |
| `MARTIAN_API_KEY` | **yes** for live runs | empty | Sent as `X-API-Key`. `Client()` raises if unset |
| `MARTIAN_REQUEST_TIMEOUT_SECONDS` | no | `120` | httpx timeout per `/runs/wait`. Minimum 1, maximum 240 (under the ALB idle timeout of 300s) |
| `MARTIAN_ALLOW_INSECURE_API_URL` | no | unset | Development only. Exactly `1` accepts an `http://` API URL on a non-loopback host. Read from the process environment, not from `.env` |

Pydantic-settings loads `.env` automatically when constructing `Settings()`. The CLI constructs `Settings()` on each `run`.

Retrieve a staging key only through an approved operator session (Secrets Manager id `martian-agents-<environment>-api-key` in `us-west-1`). Do not commit `.env`. Do not put the key in browser-delivered JavaScript.

YAML may override **only the URL**:

```yaml
api:
  url: https://api.agents.martiansecurity.ai
```

The API key always comes from the environment. That keeps secrets out of committed YAML.

The override is not harmless: the client sends `X-API-Key` to whatever host the URL names. `api.url` and `MARTIAN_API_URL` therefore must use `https://`. Plain `http://` is accepted only when the host is `localhost` or `127.0.0.1`. Anything else fails `validate` and `run` before a request is made:

```text
api.url must use https:// (http:// is accepted only for localhost and 127.0.0.1; …)
```

To reach a development server over plain HTTP on another host, export `MARTIAN_ALLOW_INSECURE_API_URL=1` in the shell that runs the CLI. Never set it in CI. Review any change to `api.url` as you would a change to where a secret is sent; `https://` does not make an unknown host trustworthy.

## First local run (dummy target)

`examples/callback.py` refuses every probe. That is enough to exercise the CLI **once simulator and evaluator graphs exist**. Against today’s generic `martian_agents` assistant the wait call will not satisfy `SimulatorOutput` / `EvaluatorOutput` and the gate will fail closed on infrastructure/schema errors — that is correct.

```bash
export MARTIAN_API_KEY=...
uv run martian-redteam catalog
uv run martian-redteam run examples/config.yaml -o results/
```

Outputs:

- Terminal tables (per-type pass rate, then failed/errored probes)
- `results/risk-assessment.json` and `results/risk-assessment.md` (plus `$GITHUB_STEP_SUMMARY` when set in GitHub Actions)

Exit code `1` if the gate fails.

## Python API

```python
from martian_redteam import Client, PromptInjection, PromptLeakage, ROT13, red_team

async def model_callback(prompt: str) -> str:
    return await my_app.reply(prompt)

assessment = red_team(
    target=model_callback,
    vulnerabilities=[PromptLeakage(types=["system_prompt"])],
    attacks=[PromptInjection(), ROT13()],
    client=Client(),
    purpose="Internal assistant with calculator tools",
)
assessment.save("results/")
```

- `red_team()` is synchronous (`asyncio.run`). Do not call it from a running event loop.
- Inside async apps, `await a_red_team(...)` and manage `async with Client()`.
- `purpose` is sent to the simulator as context. Write it the way you would describe the product to an attacker: role, tools, data it can see.

## Callback shapes

All of these are valid:

```python
def model_callback(prompt: str) -> str: ...
async def model_callback(prompt: str) -> str: ...

from martian_redteam import Turn

async def model_callback(prompt: str, turns=None) -> Turn:
    reply = await my_agent(prompt)
    return Turn(role="ai", content=reply.text, tools_called=reply.tool_names)
```

Returning anything other than `str` or `Turn` is a **target error**. Phase 1 always passes `turns=None` (single-turn). If the callable declares a `turns` parameter it receives `None`; otherwise it is called with the prompt only.

## Listing the catalog

```bash
uv run martian-redteam --version
uv run martian-redteam catalog
```

YAML names (`PromptLeakage`) and snake_case ids (`prompt_leakage`) both resolve.

## What “working” means today

| Check | Expected |
| --- | --- |
| `uv run pytest -q` | Pass without network |
| `uv run martian-redteam catalog` | Prints vulns/attacks/frameworks |
| Live `run` against staging | Fails until Phase 0 graphs exist, or succeeds against recorded-compatible graphs |

Unit tests never call production. CI in this repo is lint, types, pytest, packaging, and Action smoke (`validate` only); live `run` is not a required PR check.
