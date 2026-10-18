# Martian Red Team

Customer SDK for AI-agent assessment and runtime controls. The assessment CLI
orchestrates probes in your process, with attack generation and judging on the
Martian Agents API. Your application stays behind a local `model_callback`.
Optional runtime integrations bind MCP tools and enforce reviewed task, call and
output policies.

Optional [exact-action review](docs/customer-integration.md#exact-action-approval) binds one administrator-approved admission
to complete arguments and destination. Requires backend migration 0042, policy
schema 3 and explicit customer-side argument export.

The assessment CLI remains **internal** pending verified service identity, quotas
and release acceptance. Do not point untrusted CI at a shared staging API key.
The runtime integrations below have separate local Kubernetes acceptance; this
checkout does not imply a published package or production rollout.

The customer integration supports exact operation/argument permissions and optional
[shared runtime task policies](docs/customer-integration.md#shared-runtime-task-policies)
with durable per-task operation budgets. Optional [value-disclosure output policies](docs/customer-integration.md#exact-value-output-disclosure-policy) check complete responses locally before release, with explicit schema 2 Unicode and encoding options.

The optional [customer-managed MCP gateway](docs/runtime-gateway.md) runs these
deterministic tool controls in a separate Docker/Kubernetes process, with private
upstream credentials, pre-issued tasks and pinned tool metadata.

Selected task operations can require [human approval](docs/customer-integration.md#human-approval-of-task-operations)
of their bounded allowance. Waiting calls stop explicitly and are never retried automatically.

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

See [Customer MCP integration](docs/customer-integration.md) for the session, tool-binding and trace APIs. The optional [runtime reporter](docs/customer-integration.md#optional-runtime-reporting) sends model/tool metadata and explicitly configured opaque resource references to a separate discovery binding. Explicit [native Kubernetes mode](docs/customer-integration.md#explicit-kubernetes-request-reporting) additionally reports named GET coordinates and declared identity for RBAC evidence matching. The opt-in [SDK call gate](docs/customer-integration.md#sdk-call-gate) adds exact-target call-entry decisions, separate credentials and explicit fail-closed availability. Customer-owned [operation maps](docs/customer-integration.md#resolved-operations-for-generic-tools) distinguish actions inside generic MCP dispatchers. Application-issued [task permissions](docs/customer-integration.md#application-issued-task-authorization) add exact argument scopes, expiry, revocation and per-session call limits. These integrations are implemented in this source checkout; no package release or production rollout is implied.

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

The isolated [compact model lab](model-lab/README.md) runs the pinned ModernBERT encoder in Docker/Kubernetes, generates executable scope labels and trains/evaluates experimental numerical readouts. Its [model card](model-lab/MODEL_CARD.md) records actual results and limits. It is not a production security service and adds no ML dependency to the SDK.

[Static artifact inspection](docs/artifact-inspection.md) checks pinned MCP metadata and skill text locally, with bounded review signals and explicit coverage.
