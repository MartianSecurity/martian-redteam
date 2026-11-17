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
3. [Evals](evals.md) — running a suite file locally, auditing a suite with replies that do nothing or cheat, inspecting a run for an agent that reached its answers, tests or grader, repeated attempts with reliability and flaky cases, conversation cases graded per turn and as a whole, simulated users, personas from real conversations and the simulator's fidelity, a matrix of prompts and models, a cache of model calls, pass rates and their confidence intervals, comparing two runs, the judges, a panel of judges decided by a strict majority, their agreement with human labels, people's agreement with each other, and their calibration and its re-check, the deterministic checks and custom Python checks, and the dataset regression gate
   - [Shared eval data model](eval-model.md) — the objects the SDK, the local edition and the server have in common, and how each format maps onto them
   - [Evals core](evals-core.md) — the `/v1/evals` API that runs on its own, the three [editions](evals-core.md#editions-and-entitlements) (Community open source, Martian Cloud, On-Premises) and the feature keys each enforces, with the principal, entitlements, store, record store, worker and judge settings an edition injects, its read and write routes, suite audits, runs inspected for cheating, its OpenTelemetry traces endpoint, the worker interface, and the routes `martian evals push` sends local runs to
   - [Data export](data-export.md) — an organisation's data as JSON Lines files with a manifest, `martian evals export` resuming an interrupted export, and `martian evals import martian` loading it into a fresh install
   - [Trace store limits](trace-store-limits.md) — measured write and search latency and database growth of the in-process store's traces on Postgres, and the trace volume it holds
4. [Agent Protocol](agent-protocol.md) — typed Backend operations and explicit legacy graph adapter
5. [Orchestration](orchestration.md) — probe loop, encodings, callbacks, concurrency
   - [Red team data model](redteam-model.md) — the objects SDK runs, the local edition and hosted campaigns have in common, how each format maps onto them, and how a run is stored as an eval run
   - [Red team verdicts](redteam-verdicts.md) — evidence levels 0 to 5, the rules that turn checks into a verdict, and their test vectors
   - [Red team run file](redteam-run-file.md) — version 2: targets shared with suite files, models and the local or hosted service, goals and checks, the tool proxy, budget and gate, and how `martian redteam run` runs it
   - [Red team reports](redteam-reports.md) — the report standard: what attacked, the budget, success per attempt and within k target queries with intervals, refusals, findings by evidence level, verification and controls, and every number defined
   - [Red team API](redteam-api.md) — the `/v1/redteam` routes and their frozen contract
6. [Catalog](catalog.md) — vulnerability and attack IDs, frameworks, custom criteria
7. [Configuration](configuration.md) — env vars, YAML schema, CLI (`validate`, `-c`/`-a`/`-o`), Markdown reports
8. [Gate and CI](gate-and-ci.md) — fail-closed local gate (not `deepeval gate`), this repo’s split CI, customer reusable workflow / composite action, the evals pull-request action and its comment, WAF
9. [Security and data](security-and-data.md) — origin isolation, what leaves the customer network, and telemetry, which is off
10. [Development](development.md) — layout, tests, coverage artifacts, Dependabot, the Contributor License Agreement check
11. [Customer MCP integration](customer-integration.md) — runtime-provided MCP connections, tool binding, bounded local traces and optional outbound runtime metadata. These are integration extras, not a product line

## SDK release

- [Architecture](architecture.md): execution, scoring and trust boundaries
- [Release preparation](releasing.md): distributions, clean installs, the local edition's image, bills of materials and the licence check, and publishing
- [Validation](validation.md): reproducible package checks and running-service release gates

## Repository

- GitHub: https://github.com/MartianSecurity/martian-redteam
- License: Apache-2.0 (`LICENSE`, `NOTICE`). This repository is the home of the open-source
  evals core; see [its names and layout](architecture.md#open-source-evals-core) and
  [its editions](evals-core.md#editions-and-entitlements).
- Contributing: [CONTRIBUTING.md](../CONTRIBUTING.md), under a Contributor License Agreement with
  MartianSecurity ([`cla/`](../cla/))
- Distribution: `martian-sdk` (Python 3.12+, uv)
- Modules: `martian.evals`, `martian.redteam`; legacy imports remain supported
