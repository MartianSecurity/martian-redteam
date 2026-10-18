# Customer MCP integration

The mcp extra connects a customer agent to multiple existing MCP servers provided at runtime. It does not implement service-specific MCP servers or launch environments.

From this checkout, use `uv sync --locked --extra mcp`. The new API is source code in this workspace; no SDK release has been published by this change.

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

`parse_session_payload` validates a raw dict or `AssessmentSession` and verifies `ensure_active()`, while `bind_sandbox_mcp` combines validation with `bind_tools`. Each session requires session_id, campaign_id, target_id, expires_at and mcp_connections. Each connection requires a unique name, HTTP(S) url, explicit allowed_tools and optional headers. Headers use SecretStr in models and full runtime values on the wire. Do not pass model-generated connection configuration; the host application must authenticate the caller and approve destination hosts. URLs cannot contain embedded user credentials or fragments. This version supports Streamable HTTP, not stdio/OAuth login flows. These helpers validate and consume an existing trusted session; they do not provision twins or mint capabilities. The backend binding-bundle endpoint returns a configuration template. Actual bearer capabilities are issued for admitted runs at `/v1/assessment-sessions/{run_id}/mcp/{system}` and delivered to the approved customer session API. A local live SDK check exercised tool discovery/read, allowlist rejection, expiry and trace redaction against this gateway. See [backend binding limits](../../backend/docs/usage.md).

The binding discovers all listed servers, follows bounded tool-list pagination and rejects missing selected tools. Aliases combine the connection name with a stable hash of the remote name, allowing duplicate remote names across servers. Definitions retain the actual JSON schemas; arguments are validated locally without retrieving remote schema references. Business-schema conversion is not implemented. Protocol/application errors never trigger a switch to real services.

When `action_id` is a safe token (`[A-Za-z0-9_.:-]`, at most 128 characters), every MCP request of the binding carries it as `X-Martian-Action-Id`, so a gateway that records calls, such as the Martian backend's run-scoped assessment gateway, can attribute each call to the turn it served. A header of the same name configured on the connection wins.

The context must be entered, used and exited in the same async task. All clients are closed on exit. There is no global MCP routing state. The session expiry is checked before connection and tool invocation. The binding now allows up to 90 seconds for an MCP request/response; the previous 30-second limit failed a local sandbox read even though the upstream application logged HTTP 200. The host still owns the enclosing turn deadline, request/session correlation, conversation state and replay protection. A host using the 90-second binding should allow a longer turn deadline, as Mars Market's 120-second setting does. Mutations can complete even when a transport times out; this SDK does not promise exactly-once execution or add retries.

TraceRecorder holds bounded events locally (default 1000). Tool calls record start/end/error, server/tool/call/action identifiers, duration and MCP error status. Applications may add model/request events with record(). Payloads require capture_payloads=True; the binding automatically registers connection-header credentials for redaction. Additional secrets supplied to the recorder and common credential fields are also redacted. Payloads are bounded to 16000 characters per event. This is not comprehensive PII detection. Bindings do not automatically capture LLM inputs or export traces to a backend.

The [Mars Market refund customer agent](https://github.com/MorningHQ/MartianCustomerAgents/tree/main/refund-support-acceptance) demonstrates Bedrock model instrumentation, authenticated runtime configuration, customer-scoped commerce tools and a trace retrieval endpoint. Session authentication, expiry, replay protection and trace export remain responsibilities of the host application. No replacement service-specific MCP servers are bundled with this SDK.

Its standalone Docker build consumes this SDK as a named source context. The SDK's .dockerignore includes only package metadata, README and source; local credentials, environments and caches are excluded from the build context.

Private customer repositories can materialize a reviewed SDK revision without a personal access token when this repository is shared with the organization for Actions access:

```yaml
- uses: MorningHQ/martian-redteam/source@<full-reviewed-commit-sha>
  with:
    destination: martian-redteam
```

The source action copies only `pyproject.toml`, `README.md` and `src`; callers should pin a full commit SHA and pass that directory as their locked Python or Docker named-context dependency.

## Resolved operations for generic tools

`ToolOperationPolicy` is an opt-in, customer-owned mapping for MCP tools that dispatch
multiple operations. It is independent of model inference and does not inspect tool
descriptions to infer authority. Supply it as `operations=` to `bind_tools` or
`bind_sandbox_mcp`. Load the versioned JSON from trusted deployment configuration:

```python
from pathlib import Path
from martian_redteam import ToolOperationPolicy, bind_tools

operations = ToolOperationPolicy.model_validate_json(
    Path("/etc/martian/tool-operations.json").read_bytes()
)
async with bind_tools(session, runtime=trace, operations=operations) as tools:
    result = await tools.call(alias, arguments, call_id=call_id)
```

The [three-workflow example](../../customer-agents/refund-support-acceptance/examples/tool-operations.json)
contains explicit payment, Docs and CRM mappings. Each rule pins `system`, `tool`,
a dictionary-key `selector` path and a finite list of exact `operations`. For example,
`stripe / api_tools_call` with selector `["body", "name"]` resolves `create_refund`
to the v1 target `{kind: "tool", system: "stripe", name: "api_tools_call@create_refund"}`.
A direct named tool keeps its original coordinate. Rules for tools outside the
session allowlist are inactive; they do not expand that allowlist or establish coverage.

For a selected configured dispatcher, missing, non-string, unknown and noncanonical
selector values stop locally before transport, including when no runtime reporter is
installed. There is no case folding, whitespace normalization or alternate route.
Duplicate rules/operations and collisions between resolved coordinates and advertised
real tools are rejected. The local policy limits mappings to 32 dispatchers, 64
operations each and an eight-key selector path. Operation names are bounded metadata;
never use customer IDs, recipients, account numbers or other content as operation labels.

The SDK copies JSON arguments before validation and before awaiting authorization,
then resolves, validates and executes that same private copy. Non-JSON values,
non-string object keys, nonfinite numbers and argument bodies above 256 KB are rejected.
Session expiry is checked again immediately before transport. This prevents caller
mutation during the decision wait from changing the admitted operation or arguments;
it is not downstream transactional authorization or exactly-once execution.

When a gate is attached, both an enrolled raw dispatcher and an enrolled resolved
operation require their own fresh decisions. A pre-existing whole-dispatcher block
therefore still applies to every child. Enrolling only a write allows unrelated reads
to proceed under their own explicit scopes. Enrollment alone does not block anything;
review and activate the exact target in Discovery → Kill switch. Unknown operations
are rejected by the mapping even if that operation has no gate assignment.

Runtime reports use the resolved name; local assessment trace events retain `tool`
and `resolved_tool`. A denied/unavailable gate emits `tool.blocked` locally and no
executed runtime span. Resource mapping continues to use the underlying remote name.
Only the operation label leaves through metadata reporting: no arguments or content
are added to the backend contract. Gate v1 and its block receipts are unchanged.

Treat the map as part of the deployment artifact: version the ConfigMap, restart the
customer process, register a new runtime version/binding and install its reviewed
assignment when mappings change. The backend does not attest that an enrolled
coordinate is actually intercepted. Direct HTTP, other MCP clients, unselected
transport routes and already admitted calls remain outside this boundary. The map
selects an operation; trusted task/principal authorization, argument-level policy,
approvals, content evaluation and business-state verification are separate work.

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

The SDK now submits the [v3 contract](../../martian-collector/docs/contract.md#application-resource-runtime-contract-v3) to `/resource-events`. It retains UUID correlation, configured kind/system/name, completion time, duration and outcome, and adds bounded optional resource-response mapping. The backend keeps the earlier v2 endpoint for existing clients; there is no automatic downgrade. Names must be safe configured identifiers, not user content. This is not comprehensive PII detection. Backend and SDK schema copies must evolve together. The runtime view retains deployment/version boundaries and labels its latest-500 aggregate and latest-50 details limits. This source implementation was accepted locally on September 30 with two Bedrock calls and one commerce MCP lookup; it is not a published SDK release or agentless discovery capability.

### Security decision reporting

Set `RuntimeReporter(..., security_decisions=True)` only after deploying the
[backend decision contract](../../backend/docs/discovery.md#security-decision-reports).
Default false preserves the previous export surface. The same `mt_` credential
sends a separate v1 batch to `/security-decisions`.

SDK-bound task checks report the final local admission, local/shared task denials,
and unavailable shared authority. `RuntimeTrace.guarded_call` also reports Call
Gate denials/unavailability for tools and models, including the raw dispatcher
coordinate when a whole-tool block stops it. Gate admissions alone are not reported.
Task admission means the final synchronous reservation passed; execution and effects
remain separate. Early session expiry, allowlist, operation-resolution and input
schema failures before the control stage are outside this reporting coverage.

The strict mirrored `decision_schema.py` permits only generated event/trace UUIDs,
aware timestamps, configured target kind/system/name, fixed control/decision/reason
codes and local/shared task/policy UUID references. It excludes principal/session/
action/call IDs, arguments, raw content, result data and local HMACs. References
are application declarations, not authorization tokens or verified joins.

The decision queue independently holds at most 1,000 records and attempts up to 25
per exporter cycle after ordinary runtime export. Each HTTP request has a five-second
timeout; slow export can extend the two-second cycle. There is one delivery attempt,
no persistence or replay, and queued records drop on shutdown. Inspect
`decisions_sent`, `decisions_delivery_failed` and `decisions_dropped` locally; the
console cannot infer complete coverage. The backend can separately schedule
[30-day receipt-age retention](../../backend/docs/discovery.md#application-report-retention)
for persisted reports; SDK enrollment does not install that schedule. Export failure does not change control
policy or replay a business action. This reporting credential never grants task
issuance, budget reservation or Call Gate control authority.

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

This opt-in uses [runtime v4](../../martian-collector/docs/contract.md#application-native-request-contract-v4).
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
[gate v1 API](../../backend/docs/discovery.md#sdk-call-gate-v1) are required.

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
installation. The base contract contains no prompt, argument, output or provider
credentials; the separately configured Pod identity mode adds its protected credential
header on heartbeat only. It has one decision attempt, a three-second total request deadline,
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

## Application-issued task authorization

`bind_tools(..., authorization=guard)` adds local deterministic task enforcement.
Construct one `TaskGuard` from a trusted `TaskAuthorization`, a session ID and an
issuer label, then share that guard across **all turns and conversations** of that
session. Do not reconstruct it per call or from model/tool output. Without this
explicit argument, task enforcement is not enabled.

The v1 authorization record contains opaque UUID `principal_ref` and `policy_ref`,
an aware `expires_at`, and 1–32 grants. Each grant names its UUID, connection, raw
`tool`, `resolved_tool`, 1–100 maximum attempted calls and up to 64 argument rules.
The principal and policy references are assertions by the trusted application;
the SDK does not authenticate an IdP principal, look up a policy approval or verify
a remote issuer signature. The host must establish that authority before issuing
this record. See the [customer API integration](../../customer-agents/refund-support-acceptance/docs/usage.md#trusted-task-permissions).

Every argument leaf must have an explicit rule. Missing required leaves and all
undeclared leaves are denied; optional leaves still require validation when present.
Paths are arrays of exact object keys, not dotted strings, JSONPath or wildcards.
Nested objects are traversed; arrays and empty objects are atomic exact values.
Overlapping/duplicate paths are invalid. Supported rules are:

| Kind | Rule |
| --- | --- |
| `exact` | JSON-equal scalar, array or empty object. Strings, numbers, booleans and null remain distinct. Declare nonempty object fields individually. |
| `decimal` | `minimum`, `maximum` decimal strings and `places` (0–6). Accepts a bounded decimal string or integer; rejects floats, booleans, exponents, whitespace and excess fractional digits. No rounding. |
| `text` | Explicit variable string up to `max_length` (1–8192). This permits that text; it does not classify injection, secrets or unsafe language. |

Use exact rules for record IDs, recipients, permissions and currency, decimal rules
for amounts, and text rules only for intentionally variable fields. Pin sensitive
argument credentials exactly at issuance, or keep them in trusted connection headers.
The operation map remains independent; a grant never adds a tool to the MCP allowlist.
A selected operation without a matching grant is denied, including reads and login
operations. Multiple matching grants are additive capabilities; the issuer must avoid
unintended overlap. Per-call limits are not aggregate money budgets or business-state
checks: the downstream service must still enforce ownership, balance and transactional
preconditions.

The SDK snapshots arguments, validates the remote schema, checks the task, awaits any
configured raw/resolved operation gate and checks the task again immediately before
transport. The final synchronous check atomically consumes a grant allowance and
records `(action_id, call_id)` against replay. There is no await between that reservation
and invoking the MCP client. Concurrent turns share the same lock and counters.
Allowances stay consumed on downstream failure or cancellation because effects can be
uncertain. There is no retry or allowance restoration. A call already admitted may
finish after revocation; revocation does not undo effects or lock the remote database.

`guard.revoke()` is irreversible for that guard; `guard.status()` returns expiry,
revocation and remaining call counts without argument values. `TaskDenied.reason`
distinguishes session mismatch, revocation, expiry, missing identity, replay, operation
scope, argument scope and exhausted grants. A denial creates a local `tool.blocked`
event and no executed runtime span. An allowance creates `tool.authorized` with task,
grant and decision UUIDs plus a per-guard keyed HMAC over task/principal/policy,
operation, complete arguments and call identity. That HMAC is local correlation,
not an externally verifiable signature or reusable permit. `tool.end` remains the
separate tool result; neither event alone proves a downstream business effect.

Argument rules, local HMACs and trace payloads are **not uploaded** by
`RuntimeReporter`. Separately opting into security decision reporting exports fixed
reason codes and generated references as described below. Ordinary local
trace payload capture remains separately configured. Task counters, revocation and
receipts are in-memory, bounded by grant limits and trace capacity. Restart loses the
guard; the host must fail closed until a new trusted session is issued. This is a
cooperative SDK boundary. Optional shared policies below add durable operation budgets;
external identity adapters, direct-HTTP interception and learned evaluation remain unimplemented.

The backend campaign registrar can issue this exact wire contract from a reviewed
synthetic connection test profile. The Pydantic wire definitions live in
`task_authorization_schema.py` and are mirrored in the backend; `TaskGuard` remains
the customer execution boundary. No tensor dependency is added to the SDK. See
[assessment setup and privacy](../../backend/docs/usage.md#exact-task-grants-for-protected-assessments).

## Shared runtime task policies

For clients that need enforcement outside the agent process, the optional
[customer-managed MCP gateway](runtime-gateway.md) attaches these same pre-issued
tasks and keeps upstream credentials in a separate deployment. MCP client transports
ignore ambient proxy environment variables and do not follow redirects; configure
an approved upstream endpoint explicitly.
The optional MCP dependency now requires version 2.2 or later. Protected tool entry
uses the session's single-request API and accepts only ordinary `CallToolResult`
responses; interactive input/extended results stop explicitly instead of causing
the high-level client to issue additional tool calls. Reconcile any uncertain effect
before issuing another action.

`RuntimeTaskClient` in `martian_redteam.runtime_policy` connects local task permissions
to the [backend policy service](../../backend/docs/discovery.md#shared-runtime-task-policies).
Install the console-issued `PolicyChannelAssignment` and a separate `mp_` credential
file. Use a trusted HTTPS origin; HTTP requires explicit local `allow_http=True`.
Start/close one client in application lifespan. `await client.issue(authorization)`
requires `authorization.policy_ref` to identify the active approved policy. It sends
only opaque principal/policy references, expiry and aggregated operation call limits.
Arguments, credentials, session IDs, text and HMACs stay local.

Pass the returned authority to `TaskGuard(authorization, session_id=..., issuer=...,
authority=authority)`. Every bound call now checks local permissions, existing Call
Gate controls, a fresh shared decision, and local expiry/revocation again before entry.
The shared service commits the attempted-call allowance before replying. Unavailable,
malformed, mismatched or stale decisions stop the call with
`TaskAuthorityUnavailable`, a `TaskDenied` subclass. There is no cached allow,
automatic retry, quota restoration or transition to local-only mode.

For an explicitly coordinated worker handoff, `client.attach(authorization,
assignment)` reuses the original `TaskAssignment`. The assignment must match its
channel, deployment, principal, policy, expiry and operation budgets. Creating a new
task creates a separate allowance: the trusted issuer controls task creation. Local
argument rules still need trusted distribution; this is not an independent IdP or a
signature-verification protocol. Process-local guard counts can reset during a
handoff, but the shared aggregate per-operation budget cannot.

`await authority.revoke()` stops future decisions on all clients using that task.
A local `guard.revoke()` remains immediate and local; applications should perform it
before requesting shared revocation and report an unconfirmed remote response.
Approval of a replacement policy invalidates the earlier policy's tasks on their next
decision. Already admitted calls can finish and effects are not rolled back. The
backend audit records admission, not execution or independent business effects.

With backend migration 0039, reviewed task/principal suspensions deny fresh shared
decisions as `central_task_suspended` or `central_principal_suspended`. A principal
restriction also refuses new issuance for the same trusted-issuer UUID in that
deployment. Call `await authority.before_turn()` before model/MCP work to stop new
session turns without consuming operation budgets; each tool still calls
`before_call` at entry. This explicit hook is separate from tool interception and
adds one synchronous request per turn, with the same timeout and fail-closed rules.
It does not cancel admitted work. Resume or expiry never restores budgets or
overrides other restrictions, task/policy revocation or expiry. Principal references
remain issuer assertions. See [the control contract](../../backend/docs/discovery.md#session-and-principal-suspension).

### Exact-action approval

Backend migration 0042 and policy schema 3 support `approval_scope: "exact_action"`
with `require_approval: true` and a one-call budget. This is distinct from approving
an entire task allowance. Explicitly pass `action_review_targets=(CallTarget(...),)`
to the trusted `RuntimeTaskClient`, or configure the same list in the customer gateway.
Only those resolved tool coordinates export complete action context. Default task
decisions and all runtime reports remain metadata-only. The backend policy cannot
cause a customer SDK to upload arguments without this local opt-in.

`BoundMCPTools` passes its private argument snapshot, actual destination URL and Host
header, transport tool name and inspected metadata digest to the action endpoint.
An authenticated administrator reviews the complete request in Task policies. Pending
calls raise `central_approval_required`; changed arguments or destination raise
`central_action_mismatch`. A deployment without export configured receives
`central_action_context_required`. Nothing queues or automatically retries the action.
After approval, explicitly call again with the identical context and a fresh call ID.

Approval is valid for one admission, until the earlier of task expiry and 15 minutes
from its first request. Local scope, current policy, suspensions and Call Gate still
apply. Shared consumption is durable across replicas/restarts, and is not restored
after transport failure. Rejected, expired, revoked or consumed approvals cannot be
renewed. Issuing another task is a separate trusted application decision.

Canonical arguments are limited to 3 KiB and 16 levels; the entire context to 4 KiB.
URLs containing query strings, credentials or fragments are unsupported. Input is
never truncated. Unrepresentable context fails closed with authority unavailable.
Select only operations approved for this content export: encrypted context is retained
by the backend until task retention deletes it, and owner/admin review can decrypt it.
Authorization headers and secrets must remain in the connection configuration, not
in arguments. See the [data/retention contract](../../backend/docs/discovery.md#exact-action-approval).

The backend binds the submitted action, not live business state. Provider operations
must enforce current business constraints transactionally; native expected-version or
other concurrency preconditions, when required, must be included in the reviewed
arguments. This is one admission, not an exactly-once execution promise. No model
judges or changes the action.

### Human approval of task operations

Backend migrations 0040–0041 and policy schema 2 support `require_approval: true` for selected
operation budgets. `TaskAuthorization`, `TaskIssue` and `TaskAssignment` retain their
existing schema; the issuer cannot disable this policy requirement. On an otherwise
eligible call, `RuntimeTask.before_call` raises `TaskDenied` with
`central_approval_required` until an authenticated administrator reviews that task's
operation allowance. Waiting attempts do not consume local or shared allowances.
The terminal reasons are `central_approval_expired`, `central_approval_rejected`
and `central_approval_revoked`. Opt-in reports carry these fixed reasons without arguments.

Review approves the task's allocated **operation call budget**, not particular
arguments. The original customer-local argument checks remain mandatory. Choose a
one-call issued budget when one admission is intended. The backend receives only
task/principal/policy/operation metadata; no raw values or low-entropy argument hashes
cross this boundary. Review does not broaden scope or extend expiry.

There is no background queue or automatic retry: after review, an explicit new attempt
must pass every control again. Approved tasks remain subject to suspension, expiry,
revocation, policy replacement, Call Gates and atomic shared budgets. A lost response
consumes any committed budget; reconcile uncertain outcomes before another attempt.
The [backend contract](../../backend/docs/discovery.md#human-approval-of-task-operations)
defines review deadlines, roles, audit and retention. Older SDK builds fail closed on
the new reasons; update the SDK/gateway to display them specifically.

### Tool metadata inspection

Selected MCP tools are [statically inspected](artifact-inspection.md) at binding.
`bound.inspections` and local `tool.inspected` trace entries retain digests and review
signals; raw descriptions are not uploaded through the runtime reporter. Structural
schema failures stop binding. Review signals require interpretation and do not change
task permissions or Call Gate decisions.

### Assessment gateway operation alignment

The backend assessment gateway can separately declare the same dispatcher selector
and operation names in an administrator-approved sandbox binding. Its `tool_operations`
map also declares each operation as read or write. This configuration is pinned with
the assessment and records independent forwarding evidence; it is not delivered as
a replacement for the customer SDK’s trusted `ToolOperationPolicy`. Align both
configurations explicitly. A reproduced finding may recommend the exact resolved
SDK coordinate only when the gateway evidence is unambiguous. Historical unconfigured
receipts remain unresolved. See the [backend contract](../../backend/docs/usage.md#resolved-operations-in-assessment-evidence).


## Kubernetes Pod identity

`RuntimeCallGate(..., kubernetes_token_file=Path("/var/run/martian-pod-identity/token"))`
opts into live backend verification of a Kubernetes Pod-bound credential. Mount a
projected `serviceAccountToken` with audience `martian-call-gate:<gate UUID>`;
use a separate projection for each gate. The SDK re-reads this file on startup and
every 30-second heartbeat and sends it in `X-Martian-Kubernetes-Token`, never in
telemetry, tool arguments or decision bodies. Keep this credential out of logs and
request tracing. The normal gate token and pinned assignment remain required.

The backend must have the source-specific [verifier configuration and RBAC](../../backend/docs/discovery.md#verified-kubernetes-pod-membership).
Optional mode preserves callers without a Pod credential. An administrator can
require identity through the console: configured calls then stop if no successful
live verification exists within 90 seconds. Ordinary decisions do not refresh that
clock. Failed/missing token reads, rejected verification and unavailable decisions
use the existing `GateUnavailable` behavior; there is no offline allow or retry.
Once a process registers an identity, removing/changing it is rejected; restart with
a new process ID when changing the Pod or disabling this client configuration.

This verifies credential possession and Kubernetes workload membership, not code
attestation or complete call interception. Protect the projected file and ordinary
gate credential from other applications. Container deployment and the trusted
customer task issuer remain separate security boundaries.


## Exact-value output disclosure policy

`OutputPolicy` in `martian_redteam.output_policy` is a customer-local, nonstreaming
final-response check. A trusted operator configures a policy UUID, 1–128 rules
(each with a distinct UUID and exact protected value of 8–4,096 characters), and a
UTF-8 response limit (default 64 KiB, maximum 256 KiB). Protected values together
must fit 64 KiB; the input policy file is capped at 128 KiB. The
[example](../../customer-agents/refund-support-acceptance/examples/output-policy.json)
contains only a fictional specimen. Use a private immutable configuration file for
actual values, issue a new policy UUID when its contents change, and version the
customer deployment/binding. Policy representations redact values through SecretStr.
Malformed/unreadable configuration fails startup without exposing its input values.

```python
from pathlib import Path
from martian_redteam.output_policy import OutputPolicy, OutputDenied

policy = OutputPolicy.from_file(Path("/etc/martian/output/policy.json"))
# `complete_response` is the full response before sending, storing or streaming it.
decision = policy.evaluate(complete_response)
if runtime_trace is not None:
    runtime_trace.output_decision(decision)
if not decision.allowed:
    raise OutputDenied(decision)
# Only now may the host release this exact complete_response.
```

Schema **1** uses exact, case-sensitive substring comparison after the application
joins response text blocks. Schema **2** explicitly opts into `normalize_unicode`
and an `encodings` list drawn from `base64`, `hex`, `url`, `html`, `json`.
Normalization applies NFKC, case folding, removal of Unicode format controls and
whitespace collapse; normalized protected values must remain distinct and at least
eight characters. Base64 (standard/URL-safe, optional padding) and hex compare
complete UTF-8 encodings of each original configured value, ignoring whitespace
between encoded characters. URL percent and HTML entities receive one decoding
layer before comparison; JSON checks the exact escaped representation, including
escaped slashes. Malformed percent-encoded UTF-8 is denied when URL checks are
selected. These are bounded value transformations, not general decoding or semantic
classification. Arbitrary combinations, partial values, encryption and novel
encodings remain outside coverage. There is no redaction or model rewrite.
Non-text/invalid UTF-8 and over-budget responses are denied. A protected match gives
`output_restricted`; a valid bounded response with no match gives `output_clear`.
The immutable local decision contains only policy/rule UUIDs and reason. With existing
opt-in decision reporting, the backend receives an `output_policy` decision for
`output/application/final_response`, the policy UUID and a fixed reason; it receives
no output text, protected values, hashes, offsets, lengths or rule IDs. This is an
additive security-decision v1 event variant; deploy the matching backend reader
before enabling it. Older readers reject the new variant; enforcement remains local
and independent of best-effort telemetry delivery.

The host must apply the guard to **every final response path** before release.
Enabling the reporter does not install this guard. It does not authenticate recipients,
check data entitlement, detect paraphrases/unsupported encodings/fragments across turns, inspect
tool argument exfiltration, or reverse completed tool effects. A policy that omits a
secret cannot protect it. A clear result means only that these configured comparisons did not
match; it is not semantic safety or a learned detector score. No model checkpoint
participates in this decision.
