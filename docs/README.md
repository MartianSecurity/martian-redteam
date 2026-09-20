# Martian red-team documentation

`martian-redteam` is the customer-side SDK and CLI for adversarial testing of AI agents. It does **not** host models. Attack generation and judging run on [martian-agents](https://github.com/MorningHQ/martian-agents) through Aegra’s Agent Protocol. The system under test stays in the customer’s process behind a local `model_callback`.

This repository is an **internal scaffold** until martian-agents exposes dedicated `simulator` and `evaluator` graphs **and** per-key identity with quotas. Do not ship `pip install martian-redteam` to third parties against the shared staging API key.

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
10. [Customer MCP integration](customer-integration.md) — runtime-provided MCP connections, tool binding, expiry and bounded local traces

## Repository

- GitHub: https://github.com/MorningHQ/martian-redteam
- License: Apache-2.0 (`LICENSE`, `NOTICE`)
- Package: `martian_redteam` (Python 3.12+, uv)
