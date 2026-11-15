# Customer MCP integration

The mcp extra connects a customer agent to multiple existing MCP servers provided at runtime. It does not implement service-specific MCP servers or launch environments.

Install the `mcp` extra of the published package, or from this checkout use `uv sync --locked --extra mcp`. The MCP binding, runtime metadata reporter and call gate described here are integration extras with their own credentials. They are not a Martian product line and are separate from the evals and redteam modules.

## Target provider setup

The callback owns its provider dependencies and credentials. For AWS Bedrock
callbacks using an AWS CLI `aws login` profile (`login_session`), install
`boto3[crt]` in the callback's environment; Boto3's optional AWS Common Runtime
support is required for that credential provider. Plain `boto3` can raise
`MissingDependencyException` when constructing the client. Select the authorized
default/named profile through the normal Boto3 session configuration. See the
official [login-session prerequisites](https://github.com/boto/boto3/blob/develop/docs/source/guide/credentials.rst)
and [CRT installation guide](https://github.com/boto/boto3/blob/develop/docs/source/guide/quickstart.rst).
These are customer callback dependencies; they are separate from the Martian API
token and are not added to the Martian SDK's base dependencies.

## MCP connections

```python
from martian_redteam import AssessmentSession, TraceRecorder, bind_sandbox_mcp, bind_tools, parse_session_payload

async def use_connections(trusted_runtime_config: dict):
    session = parse_session_payload(trusted_runtime_config)
    trace = TraceRecorder()  # Metadata only by default.
    async with bind_sandbox_mcp(session, trace=trace, action_id="action-1") as tools:
        definitions = tools.definitions
        # Hand these name/description/input_schema definitions to your model adapter.
        # Execute a model-selected call with:
        # await tools.call(name, arguments, call_id=model_call_id)
        return definitions, trace.snapshot()
```

`parse_session_payload` validates a raw dict or `AssessmentSession` and verifies `ensure_active()`, while `bind_sandbox_mcp` combines validation with `bind_tools`. Each session requires session_id, campaign_id, target_id, expires_at and mcp_connections. Each connection requires a unique name, HTTP(S) url, explicit allowed_tools and optional headers. Headers use SecretStr in models and full runtime values on the wire. Do not pass model-generated connection configuration; the host application must authenticate the caller and approve destination hosts. URLs cannot contain embedded user credentials or fragments. This version supports Streamable HTTP, not stdio/OAuth login flows. These helpers validate and consume an existing trusted session; they do not provision twins or mint capabilities. The backend binding-bundle endpoint returns a configuration template. Actual bearer capabilities are issued for admitted runs at `/v1/assessment-sessions/{run_id}/mcp/{system}` and delivered to the approved customer session API. A local live SDK check exercised tool discovery/read, allowlist rejection, expiry and trace redaction against this gateway. See [backend binding limits](https://github.com/MorningHQ/MartianBackend/blob/main/docs/usage.md#connect-running-sandbox-mcp-services).

The binding discovers all listed servers, follows bounded tool-list pagination and rejects missing selected tools. Aliases combine the connection name with a stable hash of the remote name, allowing duplicate remote names across servers. Definitions retain the actual JSON schemas; arguments are validated locally without retrieving remote schema references. Business-schema conversion is not implemented. Protocol/application errors never trigger a switch to real services.

When `action_id` is a safe token (`[A-Za-z0-9_.:-]`, at most 128 characters), every MCP request of the binding carries it as `X-Martian-Action-Id`, so a gateway that records calls, such as the Martian backend's run-scoped assessment gateway, can attribute each call to the turn it served. A header of the same name configured on the connection wins.

The context must be entered, used and exited in the same async task. All clients are closed on exit. There is no global MCP routing state. The session expiry is checked before connection and tool invocation. The binding now allows up to 90 seconds for an MCP request/response; the previous 30-second limit failed a local sandbox read even though the upstream application logged HTTP 200. The host still owns the enclosing turn deadline, request/session correlation, conversation state and replay protection. A host using the 90-second binding should allow a longer turn deadline, as Mars Market's 120-second setting does. Mutations can complete even when a transport times out; this SDK does not promise exactly-once execution or add retries.

TraceRecorder holds bounded events locally (default 1000). Tool calls record start/end/error, server/tool/call/action identifiers, duration and MCP error status. Applications may add model/request events with record(). Payloads require capture_payloads=True; the binding automatically registers connection-header credentials for redaction. Additional secrets supplied to the recorder and common credential fields are also redacted. Payloads are bounded to 16000 characters per event. This is not comprehensive PII detection. Bindings do not automatically capture LLM inputs or export traces to a backend.

The [Mars Market refund customer agent](https://github.com/MorningHQ/MartianCustomerAgents/tree/main/refund-support-acceptance) demonstrates Bedrock model instrumentation, authenticated runtime configuration, customer-scoped commerce tools and a trace retrieval endpoint. Session authentication, expiry, replay protection and trace export remain responsibilities of the host application. No replacement service-specific MCP servers are bundled with this SDK.

Its standalone Docker build consumes this SDK as a named source context. The SDK's .dockerignore includes only package metadata, the lockfile, README, license files and source; local credentials, environments and caches are excluded from the build context.

Private customer repositories can materialize a reviewed SDK revision without a personal access token when this repository is shared with the organization for Actions access:

```yaml
- uses: MorningHQ/martian-redteam/source@<full-reviewed-commit-sha>
  with:
    destination: martian-redteam
```

The source action copies only `pyproject.toml`, `README.md`, `LICENSE`, `NOTICE` and `src`; callers should pin a full commit SHA and pass that directory as their locked Python or Docker named-context dependency.

## Optional runtime reporting

The source checkout now supports metadata-only discovery reporting, separately from the assessment `TraceRecorder`. In the Martian console, an owner/admin opens **Discovery → Sources → Runtime reporting**, selects a discovered workload, and declares deployment, environment and deployed version. Save its once-shown `mt_` credential in a read-only application secret file. These labels attribute reports; they do not attest the running binary. Create a separate binding for each version/environment and for assessment traffic. The credential cannot read console data or authorize assessments, and revoking it stops ingestion without stopping the application.

Create one reporter per application process and manage its lifetime alongside the application. Use a trusted HTTPS origin and UUID returned by enrollment; only an explicitly trusted local deployment should set `allow_http=True`. The file is validated at construction and reread for each batch. Do not embed the token in configuration examples or URLs.

```python
from pathlib import Path
from uuid import UUID
from martian_redteam.runtime import RuntimeReporter

# Values are approved application configuration, not model-generated input.
reporter = RuntimeReporter(backend_url, UUID(binding_id), Path(token_file))
await reporter.start()
try:
    runtime = reporter.trace()  # A new generated UUID per local invocation.
    async with bind_tools(session, runtime=runtime) as tools:
        with runtime.call(kind="model", system="aws_bedrock", name=model_id) as span:
            response = await invoke_model(tools.definitions)
            span.outcome = "success"
        tools.runtime_parent_event_id = span.id
        # Execute only the application's normally authorized model-selected calls.
        # tools.call(...) reports the connection alias and remote tool name.
finally:
    await reporter.close()
```

This illustrates one invocation; a server normally starts once in its lifespan, creates a trace per request and closes at shutdown. MCP calls retain the trace ID and optional `runtime_parent_event_id`; the application must update that parent for each model step. No parent/resource authorization is inferred. Explicit call contexts default to `unknown` until the caller marks success. Exceptions produce `error`, cancellation produces `unknown`, and both propagate. MCP protocol success/error is recorded without tool arguments or results. A successful model RPC or MCP response does not prove that a business action happened; errors do not prove that nothing happened.

The exporter buffers at most 1,000 metadata events in memory and sends up to 25 v3 events every two seconds with a five-second HTTP timeout. It never retries, persists or replays uncertain delivery. Queue overflow or reporting while stopped increments `dropped`; failed/uncertain export increments `delivery_failed`; acknowledged events increment `sent`. These counters are available on the reporter and bounded metadata logs, but are not uploaded to the dashboard. Shutdown cancels export and counts queued events as dropped; it does not flush. Business calls continue independently of export failures, without re-executing tools or changing their reported application result. Invalid startup configuration fails explicitly. Monitor application delivery logs/counters; absence of backend events does not establish inactivity.

The SDK now submits the [v3 wire models](../src/martian_redteam/runtime_schema.py) to `/resource-events`. It retains UUID correlation, configured kind/system/name, completion time, duration and outcome, and adds bounded optional resource-response mapping. The backend keeps the earlier v2 endpoint for existing clients; there is no automatic downgrade. Names must be safe configured identifiers, not user content. This is not comprehensive PII detection. Backend and SDK schema copies must evolve together. The runtime view retains deployment/version boundaries and labels its latest-500 aggregate and latest-50 details limits. This source implementation was accepted locally on September 30 with two Bedrock calls and one commerce MCP lookup; it is not a published SDK release or agentless discovery capability.

### Explicit resource response mappings

`RuntimeResourceRule` defines a safe configured service alias, resource type, action and ID field/path inside a successful MCP `structuredContent` response. Pass rules into `reporter.trace(resource_rules=...)`, keyed by exact `(connection, remote_tool_name)`. Rules are application code; no names/actions are guessed from a tool name, permission or network destination.

```python
from martian_redteam.runtime_resources import RuntimeResourceRule

rules = {
    ("commerce", "get_order"): (
        RuntimeResourceRule(
            service="commerce", resource_type="order", action="read", id_field="order_id"
        ),
    ),
}
runtime = reporter.trace(resource_rules=rules)
# Pass this runtime to bind_tools as above.
```

For lists, set `path=("orders",)` and `many=True`. The mapper reads only the configured ID field, caps references at ten per event, deduplicates identical tuples and reports omitted response entries/mapping paths. Missing structured responses and unsupported/error calls remain unreported. Text, arguments, names, emails and record contents are not extracted. Native ID strings are limited to 256 characters locally and become HMAC-SHA256 references before enqueueing. A domain-separated key is derived once from the credential at reporter construction; binding UUID/service/type/native ID define the reference. Do not reuse a reporter across credential/binding changes. References correlate within one binding, not across environments or versions. They are pseudonymous and still subject to customer data policy.

`reported` covers configured response-ID fields, not every downstream access; an empty response can map zero references. `partial` exposes omissions. The action describes the configured operation; successful create responses may be idempotent replays and do not prove a new mutation. The result and native IDs never enter the runtime queue. The backend must support v3 before upgrading this reporter. Model calls keep resource reporting empty. The exporter remains a single-attempt volatile queue; no persistence/recovery/retry path was added.

## Explicit Kubernetes request reporting

This opt-in uses [runtime v4 wire models](../src/martian_redteam/runtime_schema.py).
Upgrade the backend first; the reporter makes one delivery attempt with no downgrade.
Leave `native_kubernetes=False` (default) to retain v3 and its data boundary.

```python
from martian_redteam.runtime import RuntimeReporter
from martian_redteam.runtime_schema import KubernetesRequest

reporter = RuntimeReporter(backend_url, binding_id, token_file, native_kubernetes=True)
await reporter.start()
trace = reporter.trace()
request = KubernetesRequest(
    cluster_id="customer-cluster", service_account_namespace="agents",
    service_account_name="orders-agent", namespace="agents",
    api_group="", resource="configmaps", name="sample-catalog",
)
with trace.call(kind="tool", system="kubernetes", name="read_catalog",
                kubernetes_request=request) as span:
    result = await approved_kubernetes_client.read_namespaced_config_map(
        name=request.name, namespace=request.namespace,
    )
    span.outcome = "success"
# Keep the reporter running in application lifespan; close it at shutdown.
```

The Kubernetes client is supplied by the customer; the SDK neither creates it nor
selects credentials, performs the request or reads the result. Use trusted application
configuration for the source alias and actual credential identity, never model-supplied
identity metadata. Wrap one named, namespaced primary-resource GET per call; no
impersonation, subresources, list/watch or mutation support is implied. Exceptions
produce error metadata, while the original exception propagates. A report with only
a tool call does not qualify as a runtime-observed AI agent.

This explicitly exports native resource and service-account **names**, unlike the
opaque commerce response mappings. Approve those fields before enabling it. Tokens,
headers, URLs and object contents do not enter the queue. The existing volatile
queue/loss counters and binding isolation are unchanged. Backend matching uses the
latest collected structured RBAC scope; it does not attest the service account,
prove authorization at call time or evaluate business impact. Old collector receipts
must be recollected before they can match. The Sources credential workflow is unchanged.

## SDK call gate

`RuntimeCallGate` is a separately opted-in call-entry control, independent of the
metadata export queue and the CI release gate. First enroll the deployment under
Discovery → Kill switch. Download its non-secret assignment and separate `mg_`
credential; mount the credential as a private file. Backend migration 0020 and the
[gate v1 API](https://github.com/MorningHQ/MartianBackend/blob/main/docs/discovery.md#sdk-call-gate-v1) are required.

```python
from pathlib import Path
from martian_redteam.call_gate import RuntimeCallGate, CallBlocked, GateUnavailable
from martian_redteam.call_gate_schema import GateAssignment
from martian_redteam.runtime import RuntimeReporter

assignment = GateAssignment.model_validate_json(Path("/run/martian/gate.json").read_bytes())
gate = RuntimeCallGate(
    "https://console-api.example.com", assignment, Path("/run/martian/gate-token")
)
reporter = RuntimeReporter(
    "https://console-api.example.com", assignment.binding_id,
    Path("/run/martian/reporting-token"), call_gate=gate,
)
await reporter.start()  # validates the customer-pinned assignment; starts heartbeat
try:
    trace = reporter.trace()
    async with trace.guarded_call(kind="model", system="aws_bedrock", name="configured-model") as span:
        result = await invoke_configured_model()
        span.outcome = "success"
finally:
    await reporter.close()
```

The illustrative callable above belongs to the customer application. Use the exact
configured provider/tool identifiers in the assignment. `guarded_call` validates
scope, obtains a fresh decision, then opens a normal runtime span only if admitted.
`CallBlocked` contains the denied decision; `GateUnavailable` stops a selected call
when there is no usable decision. Catch these at the application boundary to tell
the caller the operation was stopped; do not call the provider in either handler.
Denied/unavailable calls do not create executed runtime spans. Passing this trace to
`bind_tools(..., runtime=trace)` gates MCP calls by exact connection/remote-tool name.
The older synchronous `trace.call()` explicitly refuses when a gate is attached so
it cannot silently bypass it. Without a gate, reporting behavior is unchanged.

The gate pins one binding and backend origin, reads its separate token file per
request, and sends only generated process/request UUIDs plus configured target
identifiers. TLS is required unless `allow_http=True` is explicitly set for a local
installation. There are no prompt, argument, output or provider credentials in this
contract. It has one decision attempt, a three-second total request deadline,
two-second transport timeouts, a 16-KiB response limit, no redirects/proxy environment,
and no cache/replay/retry. Its 30-second heartbeat is liveness reporting, not an
alternate authorization path. A failed block acknowledgement does not allow the
call. Cancellation propagates without entering the callable.

**Availability contract:** selected calls require a live allow decision, even when
no control is active or after timed expiry/manual resume. An unreachable backend,
revoked gate token or stale process causes them to stop. Startup assignment failure
fails startup explicitly. Targets outside the locally pinned assignment are not
gated. Neither a heartbeat nor an acknowledgement proves all replicas use the SDK;
direct calls and calls already admitted/in flight remain outside this boundary.
The SDK cannot reverse completed effects or stop network connectivity. This is an
opt-in cooperative control, not a tamper-resistant infrastructure kill switch.

Owner/admin reviews expire after five minutes; activation blocks one enrolled target
for 1/5/15/30/60 minutes, evaluated by backend time. Resume ends that control before
its expiry. Gate revocation instead invalidates the decision credential and leaves
configured calls closed. Rotation requires installing the newly returned secret;
the unchanged assignment heartbeat then registers the process again. Reporting
credential revocation affects uploads only. Application deployment/version changes
need a new runtime binding and gate enrollment. No published SDK release is implied
by this source checkout.
