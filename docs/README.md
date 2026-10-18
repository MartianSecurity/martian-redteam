# Martian red-team documentation

`martian-redteam` provides a customer-side assessment CLI and optional runtime
integration. The CLI calls attack generation and judging on
[martian-agents](https://github.com/MorningHQ/martian-agents); its target stays behind
a local `model_callback`. Runtime modules separately bind MCP tools, enforce
reviewed deterministic controls and report bounded metadata. Neither path hosts a
production learned security model.

The assessment CLI remains **internal** until deployed per-key identity, quotas
and service acceptance are verified. The sibling Agents source now registers the
dedicated graphs; that is not proof of a public service release. Do not distribute
the shared staging API key. Local runtime implementation and acceptance are
documented independently below.

## Read in this order

1. [Architecture](architecture.md) — three systems, data flow, cost model, what this repo is not
2. [Setup](setup.md) — install, environment, first local run
3. [Agent Protocol](agent-protocol.md) — `POST /runs/wait` contracts, graph state, retries
4. [Orchestration](orchestration.md) — probe loop, encodings, callbacks, concurrency
5. [Catalog](catalog.md) — vulnerability and attack IDs, frameworks, custom criteria
6. [Configuration](configuration.md) — env vars, YAML schema, CLI (`validate`, `-c`/`-a`/`-o`), Markdown reports
7. [Gate and CI](gate-and-ci.md) — fail-closed local gate (not `deepeval gate`), this repo’s split CI, customer reusable workflow / composite action, WAF
8. [Security and data](security-and-data.md) — tenancy blockers, what leaves the customer network
9. [Development](development.md) — layout, tests, coverage artifacts, Dependabot
10. [Customer MCP integration](customer-integration.md) — MCP binding, operation/task permissions, shared budgets, suspensions, output checks and runtime metadata
11. [Static artifact inspection](artifact-inspection.md) — bounded MCP/skill review, Docker/Kubernetes packaging and coverage limits
12. [Compact model lab](../model-lab/README.md) and [model card](../model-lab/MODEL_CARD.md) — isolated experiments, measured failures and promotion criteria
13. [Customer-managed MCP gateway](runtime-gateway.md) — separate enforcement process, trusted task configuration, credentials and Kubernetes lifecycle

## Repository

- GitHub: https://github.com/MorningHQ/martian-redteam
- License: Apache-2.0 (`LICENSE`, `NOTICE`)
- Package: `martian_redteam` (Python 3.12+, uv)
