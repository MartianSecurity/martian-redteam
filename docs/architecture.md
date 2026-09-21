# Architecture

Caller process contains the SDK, target callback, encoding, aggregation, report writer and gate. Martian Agents provides the simulator and evaluator over Aegra POST /runs/wait.

| Code | Owns |
| --- | --- |
| `src/martian_redteam/client.py`, `protocol.py` | Authenticated HTTP, wait response parsing and transport errors |
| `orchestrator.py` in that package | Probe expansion, concurrency and single-turn loop |
| `callback.py` | Local sync/async callback loading and normalization |
| `catalog/`, `encoding.py` | Vulnerability/attack IDs, framework maps and local encodings |
| `types.py` | Simulator/evaluator contracts, test cases and RiskAssessment |
| `config.py`, `cli.py` | YAML/env configuration and CLI commands |
| `report.py`, `gate.py` | Report artifacts and gate verdict |
| `action.yml`, `.github/workflows/red-team.yml`, `examples/` | Customer CI integration |
| `integration.py`, `mcp_binding.py` | Optional runtime-supplied MCP connections, tool allowlists and local traces |

No database or long-running server is required by the SDK. Probe outputs are local JSON/Markdown. For callback-based probes, the simulator/evaluator model provider runs behind Martian Agents, so that client does not need the provider's secret. An independently hosted customer agent configures its own model provider.

The optional customer integration connects multiple supplied Streamable HTTP MCP servers within one async scope. It selects explicit tool names and exposes provider-neutral definitions plus call(). It does not patch arbitrary framework tools, implement MCP servers, launch sandboxes or authenticate a product session by itself. The customer application authenticates configuration delivery and associates requests with sessions. See [the integration contract](customer-integration.md).

The score is a remote evaluator's binary judgment of the response, not independent evidence of a sandbox side effect. Only vulnerability types marked gated participate in the score threshold; transport and target errors have separate policies.

## Optional visibility export

`visibility.py` provides `ToolActivity` and `VisibilityClient`. The backend owns the
binding identity and stores reported activity; the application owns explicit emission.
This client does not discover infrastructure, hook arbitrary framework tools, export
TraceRecorder payloads, reuse assessment credentials or enforce runtime policy.
Tool metadata excludes prompts, arguments, responses and arbitrary attributes.
No additional dependency or MCP extra is needed. [Receiver contract](../../martian-collector/docs/contract.md#application-telemetry-contract-v1).

[README](../README.md) · [How it works](how-it-works.md) · [Usage](usage.md)
