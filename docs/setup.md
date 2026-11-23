# Setup

Use Python 3.12+. Install the published package with
`python -m pip install 'martian-sdk[evals]'`, pinning an exact version in CI. A
source checkout can use `uv sync --locked --extra evals`. The base installation
runs version 0.2 red team run files on the Martian platform, campaigns, and the
reports of a saved `redteam-run.json`. The `evals` extra adds pytest/MLflow,
persisted CI, the local store and version 2 red team run files, whose engine is
built on the evals core ([run file](redteam-run-file.md)).

Configure one approved **Backend origin** and one organisation API token.
Create a key under the console's API Keys settings with the needed evals/CI
capabilities and `redteam:run`; save its once-shown value in a secret manager.
Existing evals-only tokens do not acquire the new capability automatically.
The SDK APIs must be enabled for the organisation before hosted use.

```bash
# Inject these values from your approved secret manager/environment.
export MARTIAN_API_URL='https://your-approved-backend.example'
export MARTIAN_API_TOKEN
# Only persisted CI needs these attribution values:
export MARTIAN_AGENT_ID
export MARTIAN_EVALS_GIT_SHA
martian redteam validate examples/config.yaml
martian redteam run examples/config.yaml -o results
martian ci --evals-path tests/evals --redteam-config redteam.yaml --output results
```

No `.env` is loaded implicitly. `.env.example` documents names only; your host
application/secret manager owns injection. `catalog`, `validate`, local-only
`evaluate`, and explicit offline eval previews need no network credentials.
They cannot stand in for a persisted CI gate.

For constructor configuration, shared authentication and compatibility rules,
see [SDK and CI](sdk-and-ci.md#one-customer-connection). The customer's callback
owns target/provider authentication; Martian's key never replaces that key.

```python
from martian import MartianConfig
from martian.redteam import Client, PromptInjection, PromptLeakage, a_red_team

async def check_agent(answer, api_url, token):
    config = MartianConfig(api_url, token)
    async with Client(config=config) as client:
        report = await a_red_team(
            target=answer, client=client, purpose="Support assistant",
            vulnerabilities=[PromptLeakage(types=["system_prompt"])],
            attacks=[PromptInjection()],
        )
    report.save("results")
    return report
```

Targets can be sync or async callables returning `str` or `Turn`. Sync I/O
needs its own timeout because Python cannot kill a running worker thread.
Use `await a_red_team` inside an event loop; `red_team` is the synchronous
wrapper. The SDK calls only the approved Backend origin and the local target
callback; the service owns generation/judging model access.

The legacy direct operator client is explicitly `martian_redteam.Client` with
`Settings(api_url=..., api_key=...)`, or the dedicated complete
`MARTIAN_REDTEAM_API_URL` / `MARTIAN_REDTEAM_API_KEY` pair. It does not consume
canonical Backend credentials. See [the protocol](agent-protocol.md).
