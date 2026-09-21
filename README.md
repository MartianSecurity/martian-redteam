# Martian Red Team SDK

Python SDK and CLI for callback-based red teaming, with an optional customer-side MCP integration. Attack generation and response evaluation use the separate Martian Agents API.

## Start here

- [How to use it](docs/usage.md): prerequisites, configuration, commands and checks.
- [Architecture](docs/architecture.md): ownership, dependencies and code map.
- [How it works](docs/how-it-works.md): the request lifecycle and integration boundaries.
- [Agent instructions](AGENTS.md): working rules and when documentation must change.

## Current behavior

- Single-turn simulate → optional encoding → target callback → evaluate is implemented.
- CLI configuration validation, bounded concurrency, JSON/Markdown reports and local CI gates are implemented.
- The required simulator/evaluator graphs exist in martian-agents. A particular deployment still needs compatible graphs, authentication and provider configuration.
- The target callback executes in the caller's process; its response is sent to the remote evaluator.
- The optional mcp extra adds runtime session configuration, multi-server bind_tools and bounded local traces. See [customer integration](docs/customer-integration.md).
- Optional `VisibilityClient` exports metadata-only tool activity under a backend credential pinned to one deployment/environment/version. See [visibility telemetry](docs/usage.md#optional-visibility-telemetry). It is separate from assessments and requires no MCP extra.
- This SDK does not itself launch sandboxes, run the signed pentest campaign workflow or verify business state.

Visibility integration updated on 2026-09-18; other documented behavior was checked on 2026-09-15. This describes implemented behavior, not a certification of a deployed environment. Recheck the affected code when making changes.
