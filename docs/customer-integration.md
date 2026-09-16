# Customer MCP integration

The mcp extra connects a customer agent to multiple existing MCP servers provided at runtime. It does not implement service-specific MCP servers or launch environments.

From this checkout, use `uv sync --locked --extra mcp`. The new API is source code in this workspace; no SDK release has been published by this change.

```python
from martian_redteam import AssessmentSession, TraceRecorder, bind_tools

async def use_connections(trusted_runtime_config: dict):
    session = AssessmentSession.model_validate(trusted_runtime_config)
    trace = TraceRecorder()  # Metadata only by default.
    async with bind_tools(session, trace=trace, action_id="action-1") as tools:
        definitions = tools.definitions
        # Hand these name/description/input_schema definitions to your model adapter.
        # Execute a model-selected call with:
        # await tools.call(name, arguments, call_id=model_call_id)
        return definitions, trace.snapshot()
```

Each session requires session_id, campaign_id, target_id, expires_at and mcp_connections. Each connection requires a unique name, HTTP(S) url, explicit allowed_tools and optional headers. Headers use SecretStr in models and full runtime values on the wire. Do not pass model-generated connection configuration; the host application must authenticate the caller and approve destination hosts. URLs cannot contain embedded user credentials or fragments. This version supports Streamable HTTP, not stdio/OAuth login flows.

The binding discovers all listed servers, follows bounded tool-list pagination and rejects missing selected tools. Aliases combine the connection name with a stable hash of the remote name, allowing duplicate remote names across servers. Definitions retain the actual JSON schemas; arguments are validated locally without retrieving remote schema references. Business-schema conversion is not implemented. Protocol/application errors never trigger a switch to real services.

The context must be entered, used and exited in the same async task. All clients are closed on exit. There is no global MCP routing state. The session expiry is checked before connection and tool invocation. The host owns request/session correlation, timeouts, conversation state and replay protection. Mutations can complete even when a transport times out; this SDK does not promise exactly-once execution or add retries.

TraceRecorder holds bounded events locally (default 1000). Tool calls record start/end/error, server/tool/call/action identifiers, duration and MCP error status. Applications may add model/request events with record(). Payloads require capture_payloads=True; the binding automatically registers connection-header credentials for redaction. Additional secrets supplied to the recorder and common credential fields are also redacted. Payloads are bounded to 16000 characters per event. This is not comprehensive PII detection. Bindings do not automatically capture LLM inputs or export traces to a backend.

The [refund customer agent](../../customer-agents/refund-support-acceptance/README.md) demonstrates Gemini model instrumentation, authenticated runtime configuration and a trace retrieval endpoint. Backend session setup/trace collection remains a follow-up integration. No new test cases or MCP test servers were introduced for this feature.

Its standalone Docker build consumes this SDK as a named source context. The SDK's .dockerignore includes only package metadata, README and source; local credentials, environments and caches are excluded from the build context.
