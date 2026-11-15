# Martian SDK documentation

`martian-sdk` is the customer entry point for Martian Protect outside the
console. It contains evals and redteam modules with one scoped customer API
token and approved Backend origin. Customer target callbacks stay local;
Backend admits/stores work and Agents performs generation/judging. Start with
[Unified SDK and CI](sdk-and-ci.md) for standalone Python, YAML and CI use.
The package is published on [PyPI](https://pypi.org/project/martian-sdk/).
Hosted features also need the SDK APIs enabled for the organisation.

## Read in this order

1. [Architecture](architecture.md) — three systems, data flow, cost model, what this repo is not
2. [Setup](setup.md) — install, environment, first local run
3. [Evals](evals.md) — running a suite file locally, the judges and deterministic checks, and the dataset regression gate
   - [Shared eval data model](eval-model.md) — the objects the SDK, the local edition and the server have in common, and how each format maps onto them
   - [Evals core](evals-core.md) — the `/v1/evals` API that runs on its own, with the principal, store, record store, entitlements and worker an edition injects, its read and write routes, and the worker interface
4. [Agent Protocol](agent-protocol.md) — typed Backend operations and explicit legacy graph adapter
5. [Orchestration](orchestration.md) — probe loop, encodings, callbacks, concurrency
6. [Catalog](catalog.md) — vulnerability and attack IDs, frameworks, custom criteria
7. [Configuration](configuration.md) — env vars, YAML schema, CLI (`validate`, `-c`/`-a`/`-o`), Markdown reports
8. [Gate and CI](gate-and-ci.md) — fail-closed local gate (not `deepeval gate`), this repo’s split CI, customer reusable workflow / composite action, the evals pull-request action and its comment, WAF
9. [Security and data](security-and-data.md) — origin isolation and what leaves the customer network
10. [Development](development.md) — layout, tests, coverage artifacts, Dependabot
11. [Customer MCP integration](customer-integration.md) — runtime-provided MCP connections, tool binding, bounded local traces and optional outbound runtime metadata. These are integration extras, not a product line

## SDK release

- [Architecture](architecture.md): execution, scoring and trust boundaries
- [Release preparation](releasing.md): distributions, clean installs and Trusted Publishing
- [Validation](validation.md): reproducible package checks and running-service release gates

## Repository

- GitHub: https://github.com/MorningHQ/martian-redteam
- License: Apache-2.0 (`LICENSE`, `NOTICE`). This repository is the home of the open-source
  evals core; see [its names and layout](architecture.md#open-source-evals-core).
- Distribution: `martian-sdk` (Python 3.12+, uv)
- Modules: `martian.evals`, `martian.redteam`; legacy imports remain supported
